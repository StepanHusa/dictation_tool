#!/usr/bin/env python3
"""dictate — Linux push-to-talk dictation tool (daemon + CLI)."""

import argparse
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
from logging.handlers import RotatingFileHandler

PID_FILE = "/tmp/dictation_tool.pid"
SOCK_FILE = "/tmp/dictation_tool.sock"
CONFIG_PATH = os.path.expanduser("~/.config/dictation_tool/config.toml")
VOCAB_FILE = os.path.expanduser("~/.config/dictation_tool/vocabulary.txt")
LOG_FILE = os.path.expanduser("~/.local/state/dictation_tool/dictation_tool.log")

SAMPLE_RATE = 16000
NOTIFY_ID = 31415  # fixed ID so dictation notifications replace each other

log = logging.getLogger("dictate")


# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging(also_stderr: bool = False) -> None:
    """Configure the 'dictate' logger. Called once at daemon startup."""
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
    file_handler.setFormatter(fmt)
    handlers: list[logging.Handler] = [file_handler]
    if also_stderr:
        stderr_handler = logging.StreamHandler()
        stderr_handler.setFormatter(fmt)
        handlers.append(stderr_handler)
    logging.basicConfig(level=logging.INFO, handlers=handlers)


# ── Config ───────────────────────────────────────────────────────────────────

def load_config():
    """Load config.toml; return empty dict if file is absent."""
    try:
        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def load_vocabulary() -> list[str]:
    """Return the custom vocabulary list, one entry per line."""
    try:
        with open(VOCAB_FILE) as f:
            return [w.strip() for w in f if w.strip()]
    except FileNotFoundError:
        return []


def add_to_vocabulary(word: str) -> None:
    """Append *word* to the vocabulary file (no-op if already present)."""
    word = word.strip()
    if not word:
        return
    existing = load_vocabulary()
    if word in existing:
        return
    os.makedirs(os.path.dirname(VOCAB_FILE), exist_ok=True)
    with open(VOCAB_FILE, "a") as f:
        f.write(word + "\n")
    log.info("vocabulary: added %r", word)


# ── State (daemon-side only) ────────────────────────────────────────────────

_state = "idle"  # idle | recording | transcribing
_config = {}

# Audio recording state
_recording_active = False
_audio_chunks: list = []
_record_thread: threading.Thread | None = None
_last_audio = None  # numpy float32 1-D array; set after a successful stop

# Bluetooth state (daemon-side only)
_bt_card_info: dict | None = None
_bt_previous_profile: str | None = None

# Transcription state (daemon-side only)
_override_language: str | None = None  # set by set_language command; overrides config
_whisper_model = None          # faster-whisper WhisperModel; loaded on daemon startup
_model_ready = threading.Event()  # set once model is loaded (or failed)
_last_transcript: str | None = None  # most recent transcription result


def get_state():
    return _state


def set_state(s):
    global _state
    log.info("state: %s → %s", _state, s)
    _state = s


# ── Audio device selection ───────────────────────────────────────────────────

def list_bt_headset_source() -> str | None:
    """Return the pactl source name of the first BT handsfree/headset source, or None."""
    try:
        result = subprocess.run(
            ["pactl", "list", "sources", "short"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                name = parts[1]
                # BT HFP/HSP sources are named e.g.
                # bluez_source.AA_BB_CC_DD_EE_FF.handsfree_head_unit
                if "bluez_source" in name and (
                    "handsfree" in name or "headset" in name
                ):
                    return name
    except Exception:
        pass
    return None


def select_input_device(config: dict):
    """Return a sounddevice-compatible device name/index, or None for the default.

    Priority:
    1. config['device'] override
    2. Auto-detected BT handsfree/headset source via pactl
    3. None → sounddevice default input
    """
    override = config.get("device")
    if override:
        log.info("input device: using config override %r", override)
        return override

    bt_source = list_bt_headset_source()
    if bt_source:
        log.info("input device: auto-selected BT source %r", bt_source)
        return bt_source

    log.info("input device: using system default")
    return None  # sounddevice will use the system default


# ── Bluetooth profile switching ──────────────────────────────────────────────

def _pactl_list_cards() -> str:
    """Return stdout of `pactl list cards`, or empty string on error."""
    try:
        result = subprocess.run(
            ["pactl", "list", "cards"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout
    except Exception:
        return ""


def find_bt_card_info(output: str) -> dict | None:
    """Parse `pactl list cards` output.

    Return info about the first BT card that has both an A2DP profile and an
    HFP/HSP profile, or None if no such card is found.
    Keys: name, active_profile, a2dp_profile, hfp_profile
    """
    cards: list[dict] = []
    current: dict | None = None
    in_profiles = False

    for line in output.splitlines():
        if not line:
            continue

        if re.match(r"^Card #\d+", line):
            current = {"name": None, "profiles": [], "active": None}
            cards.append(current)
            in_profiles = False
            continue

        if current is None:
            continue

        # 1-tab-indented top-level fields
        m1 = re.match(r"^\t(\S.*)$", line)
        if m1:
            field = m1.group(1)
            in_profiles = False
            if field.startswith("Name:"):
                name = field.split(":", 1)[1].strip()
                if "bluez" in name:
                    current["name"] = name
            elif field.startswith("Active Profile:"):
                current["active"] = field.split(":", 1)[1].strip()
            elif field.rstrip() == "Profiles:":
                in_profiles = True
            continue

        # 2-tab-indented profile entries: "\t\tprofile_name: Description"
        if in_profiles:
            m2 = re.match(r"^\t\t(\S+):\s+", line)
            if m2:
                current["profiles"].append(m2.group(1))

    for card in cards:
        if not card.get("name"):
            continue
        profiles: list[str] = card["profiles"]
        a2dp = next((p for p in profiles if "a2dp" in p.lower()), None)
        hfp = next(
            (
                p for p in profiles
                if any(x in p.lower() for x in ("headset", "hfp", "hsp", "handsfree"))
            ),
            None,
        )
        if a2dp and hfp:
            return {
                "name": card["name"],
                "active_profile": card["active"],
                "a2dp_profile": a2dp,
                "hfp_profile": hfp,
            }
    return None


def bt_switch_to_hfp() -> None:
    """Switch the BT card from A2DP to HFP/HSP before recording.

    Stores the previous profile so it can be restored later.
    No-op (with a warning) if no suitable BT card is found or
    config option auto_switch_bt is False.
    """
    global _bt_card_info, _bt_previous_profile
    if not _config.get("auto_switch_bt", True):
        return

    output = _pactl_list_cards()
    if not output:
        return

    info = find_bt_card_info(output)
    if not info:
        log.info("BT: no card with A2DP+HFP found; skipping profile switch")
        return

    _bt_card_info = info
    _bt_previous_profile = info["active_profile"]

    if info["active_profile"] == info["a2dp_profile"]:
        try:
            subprocess.run(
                ["pactl", "set-card-profile", info["name"], info["hfp_profile"]],
                timeout=5, check=True, capture_output=True,
            )
            log.info(
                "BT: %s — switched %r → %r",
                info["name"], info["a2dp_profile"], info["hfp_profile"],
            )
        except Exception as exc:
            log.warning("BT: failed to switch profile: %s", exc)
    else:
        log.info(
            "BT: %s — not on A2DP (active=%r); not switching",
            info["name"], info["active_profile"],
        )


def bt_restore_profile() -> None:
    """Restore the BT card to the profile it had before recording started.

    No-op if bt_switch_to_hfp() was never called or config option
    auto_switch_bt is False.
    """
    global _bt_card_info, _bt_previous_profile

    if not _config.get("auto_switch_bt", True):
        return

    if _bt_card_info is None or _bt_previous_profile is None:
        return

    info = _bt_card_info
    prev = _bt_previous_profile
    _bt_card_info = None
    _bt_previous_profile = None

    try:
        subprocess.run(
            ["pactl", "set-card-profile", info["name"], prev],
            timeout=5, check=True, capture_output=True,
        )
        log.info("BT: %s — profile restored to %r", info["name"], prev)
    except Exception as exc:
        log.warning("BT: failed to restore profile: %s", exc)


# ── Audio recording ──────────────────────────────────────────────────────────

def _record_worker(device) -> None:
    """Background thread: fill _audio_chunks until _recording_active is False."""
    global _audio_chunks, _recording_active
    try:
        import sounddevice as sd  # imported lazily — only needed in daemon
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            device=device,
        ) as stream:
            while _recording_active:
                chunk, _ = stream.read(4096)
                _audio_chunks.append(chunk.copy())
    except Exception as exc:
        log.error("recording error: %s", exc)
        # Drop back to idle so the daemon stays usable
        _recording_active = False
        set_state("idle")


_arecord_tmp_path: str | None = None  # set when arecord fallback is active


def _record_worker_arecord(alsa_device: str, tmp_path: str) -> None:
    """Background thread: record via arecord to a temp WAV file."""
    global _recording_active
    cmd = [
        "arecord",
        "-D", alsa_device,
        "-f", "S16_LE",
        "-r", "44100",
        "-c", "2",
        tmp_path,
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        while _recording_active:
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        proc.terminate()
        proc.wait(timeout=2)
        stderr_out = proc.stderr.read().decode(errors="replace").strip()
        if stderr_out:
            log.warning("arecord stderr: %s", stderr_out)
    except Exception as exc:
        log.error("arecord recording error: %s", exc)
        _recording_active = False
        set_state("idle")


def start_recording() -> str | None:
    """Select device and launch recording thread. Returns the device label."""
    global _recording_active, _audio_chunks, _record_thread
    device = select_input_device(_config)
    _audio_chunks = []
    _recording_active = True

    if device is None and _config.get("alsa_fallback"):
        alsa_device = _config.get("alsa_device", "plughw:0,0")
        capture_pct = _config.get("alsa_capture_pct", 40)
        mic_boost = _config.get("alsa_mic_boost", 3)
        try:
            subprocess.run(["amixer", "-c", "0", "sset", "Capture", f"{capture_pct}%"],
                           capture_output=True, timeout=2)
            subprocess.run(["amixer", "-c", "0", "sset", "Internal Mic Boost", str(mic_boost)],
                           capture_output=True, timeout=2)
            log.info("alsa: capture=%d%% mic_boost=%d", capture_pct, mic_boost)
        except Exception as exc:
            log.warning("alsa: failed to set mixer levels: %s", exc)
        global _arecord_tmp_path
        _arecord_tmp_path = f"/tmp/dictation_arecord_{int(time.time())}.wav"
        log.info("recording: using arecord fallback on ALSA device %r", alsa_device)
        _record_thread = threading.Thread(
            target=_record_worker_arecord, args=(alsa_device, _arecord_tmp_path), daemon=True
        )
        _record_thread.start()
        return f"alsa:{alsa_device}"

    log.info("recording: started on device %r", device or "system default")
    _record_thread = threading.Thread(
        target=_record_worker, args=(device,), daemon=True
    )
    _record_thread.start()
    return device


def stop_recording() -> None:
    """Stop the recording thread and store the audio in _last_audio."""
    global _recording_active, _record_thread, _last_audio, _arecord_tmp_path
    _recording_active = False
    if _record_thread is not None:
        _record_thread.join(timeout=3)
        _record_thread = None

    if _arecord_tmp_path and os.path.exists(_arecord_tmp_path):
        import numpy as np  # noqa: PLC0415
        import wave  # noqa: PLC0415
        try:
            with wave.open(_arecord_tmp_path) as wf:
                hw_rate = wf.getframerate()
                hw_channels = wf.getnchannels()
                nframes = wf.getnframes()
                raw = wf.readframes(nframes)
            raw_samples = np.frombuffer(raw, dtype=np.int16)
            raw_rms = float(np.sqrt(np.mean(raw_samples.astype(np.float32) ** 2)))
            log.info("arecord wav: rate=%d ch=%d nframes=%d raw_bytes=%d raw_rms=%.1f",
                     hw_rate, hw_channels, nframes, len(raw), raw_rms)
            stereo = raw_samples.reshape(-1, hw_channels)
            mono = stereo[:, 0].astype(np.float32) / 32768.0
            new_len = int(len(mono) * SAMPLE_RATE / hw_rate)
            indices = np.linspace(0, len(mono) - 1, new_len)
            _last_audio = np.interp(indices, np.arange(len(mono)), mono).astype(np.float32)
        finally:
            os.unlink(_arecord_tmp_path)
            _arecord_tmp_path = None
        duration = len(_last_audio) / SAMPLE_RATE
        rms = float(np.sqrt(np.mean(_last_audio ** 2)))
        peak = float(np.max(np.abs(_last_audio)))
        log.info("recording: stopped — %.1f s of audio captured (rms=%.4f peak=%.4f)", duration, rms, peak)
    elif _audio_chunks:
        import numpy as np  # lazily imported
        _last_audio = np.concatenate(_audio_chunks, axis=0).flatten()
        duration = len(_last_audio) / SAMPLE_RATE
        rms = float(np.sqrt(np.mean(_last_audio ** 2)))
        peak = float(np.max(np.abs(_last_audio)))
        log.info("recording: stopped — %.1f s of audio captured (rms=%.4f peak=%.4f)", duration, rms, peak)
    else:
        _last_audio = None
        log.info("recording: stopped — no audio captured")


def cancel_recording() -> None:
    """Stop the recording thread and discard the audio."""
    global _recording_active, _record_thread, _last_audio
    _recording_active = False
    if _record_thread is not None:
        _record_thread.join(timeout=3)
        _record_thread = None
    _last_audio = None
    log.info("recording: cancelled — audio discarded")


# ── Transcription ────────────────────────────────────────────────────────────

def load_whisper_model_bg(config: dict) -> None:
    """Load the faster-whisper model in a background thread; signal _model_ready when done."""
    global _whisper_model
    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415
        model_size = config.get("model", "base")
        log.info("whisper: loading model %r…", model_size)
        _whisper_model = WhisperModel(model_size, device="cpu", compute_type="int8")
        log.info("whisper: model %r loaded", model_size)
    except Exception as exc:
        log.error("whisper: failed to load model: %s", exc)
    finally:
        _model_ready.set()


def transcribe_audio(audio) -> str:
    """Transcribe a float32 numpy audio array; return stripped text.

    Blocks until the model is ready (or failed to load).
    """
    _model_ready.wait()
    if _whisper_model is None:
        return ""
    language = _override_language if _override_language else _config.get("language", "en")
    vad_filter = _config.get("vad_filter", True)
    vocab = load_vocabulary()
    initial_prompt = ", ".join(vocab) if vocab else None
    segments, _ = _whisper_model.transcribe(
        audio, language=language, vad_filter=vad_filter, initial_prompt=initial_prompt
    )
    text = "".join(seg.text for seg in segments).strip()
    return text


def _save_history_entry(text: str, duration: float) -> None:
    from datetime import datetime  # noqa: PLC0415
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    train_dir = os.path.expanduser("~/.local/share/dictation_tool/training")
    os.makedirs(train_dir, exist_ok=True)
    payload = {
        "timestamp": ts,
        "source": "auto",
        "audio_file": None,
        "audio_saved": False,
        "duration_s": round(duration, 3),
        "sample_rate": SAMPLE_RATE,
        "transcript": text,
        "edited": None,
        "was_edited": False,
        "model": _config.get("model", "base"),
        "language": _config.get("language", "en"),
    }
    try:
        with open(os.path.join(train_dir, f"{ts}.json"), "w") as f:
            json.dump(payload, f, indent=2)
    except Exception as exc:
        log.warning("history: save failed: %s", exc)


def _transcribe_worker(no_inject: bool = False) -> None:
    """Background thread: transcribe _last_audio, store result, set state idle."""
    global _last_transcript
    if _last_audio is None:
        log.info("transcription: no audio available; skipping")
        set_state("idle")
        return
    duration = len(_last_audio) / SAMPLE_RATE
    log.info("transcription: starting (%.1f s of audio)", duration)
    t0 = time.monotonic()
    try:
        text = transcribe_audio(_last_audio)
        _last_transcript = text
        if text:
            _save_history_entry(text, duration)
        word_count = len(text.split()) if text else 0
        elapsed = time.monotonic() - t0
        preview = text[:80] + ("…" if len(text) > 80 else "")
        log.info(
            "transcription: done in %.1f s — %d word(s): %r",
            elapsed, word_count, preview,
        )
        notify("✅ Dictation", f"Done — {word_count} words", replace_id=NOTIFY_ID)
        if not no_inject:
            inject_text(text, _config)
    except Exception as exc:
        log.error("transcription: error: %s", exc)
        notify("⚠ Dictation", str(exc))
        _last_transcript = None
    finally:
        set_state("idle")


# ── Notifications ────────────────────────────────────────────────────────────

def notify(title: str, message: str, urgency: str | None = None, timeout: int | None = None, replace_id: int | None = None) -> None:
    """Send a desktop notification via notify-send; silently ignore errors."""
    cmd = ["notify-send"]
    if urgency:
        cmd += ["-u", urgency]
    if timeout is not None:
        cmd += ["-t", str(timeout)]
    if replace_id is not None:
        cmd += ["-r", str(replace_id)]
    cmd += [title, message]
    try:
        subprocess.run(cmd, timeout=5, capture_output=True)
    except Exception:
        pass


# ── Text injection ───────────────────────────────────────────────────────────

def inject_text(text: str, config: dict) -> None:
    """Type *text* into the focused window using the configured injection method.

    injection_method = "xdotool"  (default)
        xdotool type --clearmodifiers --delay 0 -- TEXT
    injection_method = "clipboard"
        pipe TEXT to xclip, then xdotool key ctrl+v
    """
    if not text:
        return

    method = config.get("injection_method", "xdotool")

    if method == "clipboard":
        try:
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text.encode(),
                timeout=5,
                check=True,
            )
            subprocess.run(
                ["xdotool", "key", "ctrl+v"],
                timeout=5,
                check=True,
            )
            log.info("inject: clipboard (%d chars)", len(text))
        except Exception as exc:
            log.error("inject: clipboard error: %s", exc)
    else:
        # Default: xdotool
        try:
            subprocess.run(
                ["xdotool", "type", "--clearmodifiers", "--delay", "0", "--", text],
                timeout=30,
                check=True,
            )
            log.info("inject: xdotool (%d chars)", len(text))
        except Exception as exc:
            log.error("inject: xdotool error: %s", exc)


# ── Daemon helpers ──────────────────────────────────────────────────────────

def write_pid():
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()) + "\n")


def read_pid():
    """Return PID from PID_FILE as int, or None if missing/invalid."""
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def pid_is_alive(pid):
    """Return True if a process with the given PID exists."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists but we can't signal it


def cleanup():
    for path in (PID_FILE, SOCK_FILE):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def daemonize():
    """Double-fork to fully detach from the controlling terminal."""
    # First fork
    pid = os.fork()
    if pid > 0:
        sys.exit(0)

    os.setsid()
    os.chdir("/")

    # Second fork (prevents re-acquiring a terminal)
    pid = os.fork()
    if pid > 0:
        sys.exit(0)

    # Redirect stdio to /dev/null
    sys.stdout.flush()
    sys.stderr.flush()
    devnull_fd = os.open(os.devnull, os.O_RDWR)
    for fd in (sys.stdin.fileno(), sys.stdout.fileno(), sys.stderr.fileno()):
        os.dup2(devnull_fd, fd)
    os.close(devnull_fd)


def handle_command(cmd):
    """Process a command dict received over the socket. Return a response dict."""
    action = cmd.get("action", "")
    state = get_state()
    log.info("command: %r (state=%s)", action, state)
    if action == "status":
        return {"state": state}
    elif action == "start":
        if state == "idle":
            set_state("recording")
            bt_switch_to_hfp()
            device = start_recording()
            device_label = device if device else "default"
            notify("🎤 Dictation", f"Listening… [{device_label}]", urgency="low", timeout=0, replace_id=NOTIFY_ID)
        else:
            log.info("command: 'start' ignored — already in state %r", state)
        return {"ok": True, "state": get_state()}
    elif action == "stop":
        if state == "recording":
            no_inject = cmd.get("no_inject", False)
            set_state("transcribing")
            stop_recording()
            bt_restore_profile()
            notify("⏳ Dictation", "Transcribing…", replace_id=NOTIFY_ID)
            threading.Thread(target=_transcribe_worker, args=(no_inject,), daemon=True).start()
        else:
            log.info("command: 'stop' ignored — state is %r", state)
        return {"ok": True, "state": get_state()}
    elif action == "cancel":
        if state == "recording":
            cancel_recording()
            bt_restore_profile()
            notify("❌ Dictation", "Cancelled", replace_id=NOTIFY_ID)
        set_state("idle")
        return {"ok": True, "state": get_state()}
    elif action == "history":
        train_dir = os.path.expanduser("~/.local/share/dictation_tool/training")
        entries = []
        seen = set()
        try:
            for fname in sorted(os.listdir(train_dir), reverse=True):
                if not fname.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(train_dir, fname)) as f:
                        data = json.load(f)
                    t = data.get("transcript", "").strip()
                    if t and t not in seen:
                        entries.append(t)
                        seen.add(t)
                        if len(entries) >= 20:
                            break
                except Exception:
                    continue
        except FileNotFoundError:
            pass
        return {"history": entries}
    elif action == "transcript":
        duration = len(_last_audio) / SAMPLE_RATE if _last_audio is not None else None
        vocab = load_vocabulary()
        return {
            "transcript": _last_transcript,
            "state": get_state(),
            "duration_s": round(duration, 3) if duration is not None else None,
            "sample_rate": SAMPLE_RATE,
            "model": _config.get("model", "base"),
            "language": _config.get("language", "en"),
            "initial_prompt": ", ".join(vocab) if vocab else None,
        }
    elif action == "save_audio":
        path = cmd.get("path")
        if _last_audio is not None and path:
            try:
                import wave
                import numpy as np
                audio_int16 = (_last_audio * 32767).clip(-32768, 32767).astype(np.int16)
                with wave.open(path, "w") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(SAMPLE_RATE)
                    wf.writeframes(audio_int16.tobytes())
                log.info("save_audio: wrote %s", path)
                return {"ok": True}
            except Exception as exc:
                log.error("save_audio: error: %s", exc)
                return {"ok": False, "error": str(exc)}
        return {"ok": False, "error": "no audio available"}
    elif action == "set_language":
        global _override_language
        _override_language = cmd.get("language") or None
        log.info("command: language set to %r", _override_language)
        return {"ok": True, "language": _override_language}
    elif action == "quit":
        log.info("command: quit received — shutting down")
        return {"ok": True, "quit": True}
    else:
        log.warning("command: unknown action %r", action)
        return {"error": f"unknown action: {action!r}"}


def run_daemon():
    """Load config, open the Unix socket, write PID file, and serve commands."""
    global _config

    # Stale PID detection: if a PID file exists but the process is gone, remove it.
    # If the process is still alive, refuse to start a second daemon.
    existing_pid = read_pid()
    if existing_pid is not None:
        if pid_is_alive(existing_pid):
            print(
                f"error: daemon already running (pid={existing_pid})",
                file=sys.stderr,
            )
            sys.exit(1)
        else:
            try:
                os.unlink(PID_FILE)
            except FileNotFoundError:
                pass

    # Remove stale socket from a previous run
    try:
        os.unlink(SOCK_FILE)
    except FileNotFoundError:
        pass

    _config = load_config()
    if _config:
        log.info("daemon: config loaded from %s", CONFIG_PATH)
    else:
        log.info("daemon: no config found; using defaults")

    write_pid()
    log.info("daemon: started (pid=%d)", os.getpid())

    import atexit
    atexit.register(cleanup)

    def _sig_handler(*_):
        log.info("daemon: signal received — shutting down")
        cleanup()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_FILE)
    srv.listen(5)
    log.info("daemon: socket bound at %s", SOCK_FILE)

    # Start loading the Whisper model in the background (socket is already up)
    threading.Thread(
        target=load_whisper_model_bg, args=(_config,), daemon=True
    ).start()

    try:
        while True:
            conn, _ = srv.accept()
            with conn:
                data = b""
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    if b"\n" in data:
                        break
                if data:
                    try:
                        cmd = json.loads(data.strip())
                    except json.JSONDecodeError:
                        cmd = {}
                    resp = handle_command(cmd)
                    conn.sendall(json.dumps(resp).encode() + b"\n")
                    if resp.get("quit"):
                        break
    finally:
        log.info("daemon: shutting down")
        srv.close()
        cleanup()


# ── Subcommand implementations ──────────────────────────────────────────────

def cmd_daemon(args):
    if args.fork:
        daemonize()
    setup_logging(also_stderr=not args.fork)
    run_daemon()


def send_command(action, **kwargs):
    """Send a JSON command to the daemon and return the response dict.
    Raises ConnectionRefusedError / FileNotFoundError if daemon is not running."""
    payload = {"action": action, **kwargs}
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(SOCK_FILE)
    sock.sendall(json.dumps(payload).encode() + b"\n")
    data = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if b"\n" in data:
            break
    sock.close()
    return json.loads(data.strip())


def is_daemon_running():
    """Return True if the daemon socket is reachable."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1)
        sock.connect(SOCK_FILE)
        sock.close()
        return True
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        return False


def ensure_daemon_running():
    """Start the daemon if not already running; wait up to 3 s for socket."""
    if is_daemon_running():
        return
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "daemon"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    for _ in range(30):
        time.sleep(0.1)
        if is_daemon_running():
            return
    print("error: daemon did not start in time", file=sys.stderr)
    sys.exit(1)


def cmd_status(_args):
    try:
        resp = send_command("status")
        print(resp.get("state", "unknown"))
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        print("not-running")


def cmd_toggle(_args):
    ensure_daemon_running()
    resp = send_command("status")
    state = resp.get("state", "idle")
    if state == "idle":
        send_command("start")
    else:
        send_command("stop")


def cmd_start(_args):
    ensure_daemon_running()
    send_command("start")


def cmd_stop(_args):
    try:
        send_command("stop")
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        print("not-running")


def cmd_cancel(_args):
    try:
        send_command("cancel")
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        print("not-running")


# ── App window ──────────────────────────────────────────────────────────────

class DictateAppWindow:
    """Floating always-on-top tkinter window for guided dictation."""

    WIDTH = 420
    HEIGHT = 160

    def __init__(self, original_wid: str):
        self._original_wid = original_wid
        self._config = load_config()
        self._original_transcript: str = ""

        import tkinter as tk
        self._tk = tk
        self._root = tk.Tk()
        self._root.title("Dictate")
        self._root.resizable(False, False)
        self._root.attributes("-topmost", True)
        self._center()

        self._frame = tk.Frame(self._root, padx=16, pady=12)
        self._frame.pack(fill="both", expand=True)

        self._label = tk.Label(
            self._frame,
            text="",
            wraplength=self.WIDTH - 40,
            justify="left",
            anchor="nw",
        )
        self._label.pack(fill="both", expand=True)

        self._hint = tk.Label(
            self._frame,
            text="",
            fg="#888888",
            font=("TkDefaultFont", 9),
        )
        self._hint.pack(side="bottom")

        self._text_widget = None
        self._transcript_meta: dict = {}
        self._lang: str = self._config.get("language", "en")

    def _center(self):
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        x = (sw - self.WIDTH) // 2
        y = (sh - self.HEIGHT) // 2
        self._root.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")

    def _unbind_all(self):
        for key in ("<Return>", "<Shift-Return>", "<Escape>", "<space>", "l", "c", "r", "t"):
            self._root.unbind(key)

    def run(self):
        self.phase_listening()
        self._root.mainloop()

    def _toggle_language(self):
        self._lang = "cs" if self._lang == "en" else "en"
        try:
            send_command("set_language", language=self._lang)
        except Exception:
            pass
        self._label.config(text=f"🎤 Listening… [{self._lang.upper()}]")

    def phase_listening(self):
        self._unbind_all()
        self._label.config(text=f"🎤 Listening… [{self._lang.upper()}]")
        self._hint.config(text="Enter/Space=stop  R=recall  T=history  L=lang  Esc=cancel")
        try:
            send_command("start")
        except Exception:
            pass
        self._root.bind("<Return>", lambda e: self.phase_transcribing())
        self._root.bind("<space>", lambda e: self.phase_transcribing())
        self._root.bind("l", lambda e: self._toggle_language())
        self._root.bind("<Escape>", lambda e: self._do_cancel())
        self._root.bind("r", lambda e: self._do_recall())
        self._root.bind("t", lambda e: self._do_history())

    def phase_transcribing(self):
        self._unbind_all()
        self._label.config(text="⏳ Transcribing…")
        self._hint.config(text="")
        try:
            send_command("stop", no_inject=True)
        except Exception:
            pass
        self._root.after(200, self._poll_transcribing)

    def _poll_transcribing(self):
        try:
            resp = send_command("status")
            state = resp.get("state", "idle")
        except Exception:
            state = "idle"
        if state != "transcribing":
            try:
                tresp = send_command("transcript")
                text = tresp.get("transcript") or ""
                self._transcript_meta = tresp
            except Exception:
                text = ""
                self._transcript_meta = {}
            self.phase_review(text)
        else:
            self._root.after(200, self._poll_transcribing)

    def phase_review(self, text: str):
        self._original_transcript = text
        self._unbind_all()
        display = text if text else "(empty transcript)"
        self._label.config(text=display)
        self._hint.config(text="Enter=insert  Shift+Enter=insert+save  C=copy  Space=edit  Esc=drop")
        self._root.bind("<Return>", lambda e: self._do_insert(text))
        self._root.bind("<Shift-Return>", lambda e: self._save_sample(text, None, was_edited=False) or setattr(self, '_pending_shift_insert', text))
        self._root.bind("<KeyRelease-Shift_L>", lambda e: self._on_shift_release())
        self._root.bind("<KeyRelease-Shift_R>", lambda e: self._on_shift_release())
        self._root.bind("<space>", lambda e: self.phase_edit(text))
        self._root.bind("<Escape>", lambda e: self._do_drop())
        self._root.bind("c", lambda e: self._do_copy(text))

    def phase_edit(self, text: str):
        self._original_transcript = text
        self._unbind_all()
        self._label.pack_forget()

        import tkinter as tk
        if self._text_widget:
            self._text_widget.destroy()
        self._text_widget = tk.Text(self._frame, wrap="word", height=5)
        self._text_widget.pack(fill="both", expand=True, before=self._hint)
        self._text_widget.insert("1.0", text)
        self._text_widget.focus_set()
        self._text_widget.bind("<Return>", self._on_edit_confirm)
        self._text_widget.bind("<Escape>", lambda e: self._do_drop())
        self._text_widget.bind("<Control-w>", self._add_selection_to_vocab)
        self._text_widget.bind("<Control-d>", self._start_inline_dictation)
        self._text_widget.bind("<Control-BackSpace>", self._ctrl_backspace)
        self._text_widget.bind("<Control-Delete>", self._ctrl_delete)
        self._hint.config(text="Enter=confirm  Ctrl+D=re-dictate selection  Esc=cancel  Ctrl+W=save word")

    def _ctrl_backspace(self, event):
        w = event.widget
        insert = w.index("insert")
        word_start = w.index("insert -1c wordstart")
        if w.compare(word_start, "<", insert):
            w.delete(word_start, insert)
        elif w.compare(insert, ">", "1.0"):
            w.delete("insert -1c", insert)
        return "break"

    def _ctrl_delete(self, event):
        w = event.widget
        insert = w.index("insert")
        word_end = w.index("insert wordend")
        if w.compare(word_end, ">", insert):
            w.delete(insert, word_end)
        return "break"

    def _on_edit_confirm(self, event):
        edited = self._text_widget.get("1.0", "end-1c")
        original = self._original_transcript
        self._save_sample(original, edited, was_edited=True)
        self._do_insert(edited)
        return "break"

    def _add_selection_to_vocab(self, event):
        try:
            word = self._text_widget.get(self._tk.SEL_FIRST, self._tk.SEL_LAST).strip()
        except self._tk.TclError:
            return "break"  # nothing selected
        if word:
            add_to_vocabulary(word)
            self._hint.config(text=f"Saved {word!r}  |  Enter=confirm  Esc=cancel  Ctrl+W=save word")
            self._root.after(2000, lambda: self._hint.config(
                text="Enter=confirm  Esc=cancel  Ctrl+W=save word"
            ))
        return "break"

    def _start_inline_dictation(self, event=None):
        try:
            sel_start = self._text_widget.index(self._tk.SEL_FIRST)
            sel_end = self._text_widget.index(self._tk.SEL_LAST)
        except self._tk.TclError:
            self._hint.config(text="Select text first!  Ctrl+D=re-dictate selection")
            return "break"
        self._inline_sel = (sel_start, sel_end)
        try:
            send_command("start")
        except Exception:
            pass
        self._hint.config(text="🎤 Recording… Enter/Space=stop  Esc=cancel")
        self._text_widget.bind("<Return>", lambda e: (self._stop_inline_dictation(), "break"))
        self._text_widget.bind("<space>", lambda e: (self._stop_inline_dictation(), "break"))
        self._text_widget.bind("<Escape>", lambda e: self._cancel_inline_dictation())
        return "break"

    def _cancel_inline_dictation(self):
        try:
            send_command("cancel")
        except Exception:
            pass
        self._hint.config(text="Enter=confirm  Ctrl+D=re-dictate selection  Esc=cancel  Ctrl+W=save word")
        self._text_widget.bind("<Return>", self._on_edit_confirm)
        self._text_widget.bind("<space>", lambda e: None)
        self._text_widget.bind("<Escape>", lambda e: self._do_drop())

    def _stop_inline_dictation(self):
        try:
            send_command("stop", no_inject=True)
        except Exception:
            pass
        self._hint.config(text="⏳ Transcribing…")
        self._root.after(200, self._poll_inline_transcription)

    def _poll_inline_transcription(self):
        try:
            state = send_command("status").get("state", "idle")
        except Exception:
            state = "idle"
        if state == "transcribing":
            self._root.after(200, self._poll_inline_transcription)
            return
        try:
            text = send_command("transcript").get("transcript") or ""
        except Exception:
            text = ""
        if text:
            sel_start, sel_end = self._inline_sel
            self._text_widget.delete(sel_start, sel_end)
            self._text_widget.insert(sel_start, text)
        self._hint.config(text="Enter=confirm  Ctrl+D=re-dictate selection  Esc=cancel  Ctrl+W=save word")
        self._text_widget.bind("<Return>", self._on_edit_confirm)
        self._text_widget.bind("<space>", lambda e: None)
        self._text_widget.bind("<Escape>", lambda e: self._do_drop())

    def _do_recall(self):
        try:
            send_command("cancel")
        except Exception:
            pass
        text = ""
        try:
            text = send_command("transcript").get("transcript") or ""
        except Exception:
            pass
        if not text:
            try:
                history = send_command("history").get("history", [])
                text = history[0] if history else ""
            except Exception:
                pass
        self._transcript_meta = {}
        self.phase_review(text if text else "(nothing to recall)")

    def _do_history(self):
        try:
            send_command("cancel")
        except Exception:
            pass
        try:
            resp = send_command("history")
            history = resp.get("history", [])
        except Exception:
            history = []
        self.phase_history(history)

    def phase_history(self, history: list):
        self._unbind_all()
        if not history:
            self._label.config(text="(no history yet)")
            self._hint.config(text="Esc=close")
            self._root.bind("<Escape>", lambda e: self._do_drop())
            return

        self._label.config(text="Select a transcript:")
        self._hint.config(text="↑↓=navigate  Enter=insert  Esc=close")

        lb_frame = self._tk.Frame(self._frame)
        lb_frame.pack(fill="both", expand=True, pady=(4, 0))

        scrollbar = self._tk.Scrollbar(lb_frame, orient="vertical")
        listbox = self._tk.Listbox(
            lb_frame,
            yscrollcommand=scrollbar.set,
            selectmode="single",
            activestyle="dotbox",
            height=min(len(history), 6),
        )
        scrollbar.config(command=listbox.yview)
        scrollbar.pack(side="right", fill="y")
        listbox.pack(side="left", fill="both", expand=True)

        for item in history:
            preview = item[:60] + ("…" if len(item) > 60 else "")
            listbox.insert("end", preview)
        listbox.selection_set(0)
        listbox.activate(0)
        listbox.focus_set()

        def move(delta):
            cur = listbox.index("active")
            new = max(0, min(listbox.size() - 1, cur + delta))
            listbox.selection_clear(0, "end")
            listbox.selection_set(new)
            listbox.activate(new)
            listbox.see(new)
            return "break"

        def on_select(e=None):
            idx = listbox.curselection()
            i = idx[0] if idx else listbox.index("active")
            lb_frame.destroy()
            self.phase_review(history[i])

        listbox.bind("<Up>", lambda e: move(-1))
        listbox.bind("<Down>", lambda e: move(1))
        listbox.bind("<Return>", on_select)
        listbox.bind("<Double-Button-1>", on_select)
        self._root.bind("<Escape>", lambda e: (lb_frame.destroy(), self._do_drop()))

    def _do_copy(self, text: str):
        try:
            subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), timeout=3)
            self._hint.config(text="Copied!  Enter=insert  Shift+Enter=insert+save  Space=edit  Esc=drop")
        except Exception:
            pass

    def _on_shift_release(self):
        text = getattr(self, '_pending_shift_insert', None)
        if text is not None:
            self._pending_shift_insert = None
            self._do_insert(text)

    def _do_insert(self, text: str):
        self._root.destroy()
        if self._original_wid:
            try:
                subprocess.run(
                    ["xdotool", "windowfocus", "--sync", self._original_wid],
                    timeout=3, capture_output=True,
                )
            except Exception:
                pass
        inject_text(text, self._config)

    def _do_cancel(self):
        try:
            send_command("cancel")
        except Exception:
            pass
        self._root.destroy()

    def _do_drop(self):
        self._root.destroy()

    def _save_sample(self, original: str, edited: str | None, was_edited: bool):
        from datetime import datetime
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        train_dir = os.path.expanduser("~/.local/share/dictation_tool/training")
        os.makedirs(train_dir, exist_ok=True)
        wav_name = f"{ts}.wav"
        wav_path = os.path.join(train_dir, wav_name)
        json_path = os.path.join(train_dir, f"{ts}.json")

        audio_saved = False
        audio_error = None
        try:
            resp = send_command("save_audio", path=wav_path)
            audio_saved = resp.get("ok", False)
            if not audio_saved:
                audio_error = resp.get("error")
        except Exception as exc:
            audio_error = str(exc)

        meta = self._transcript_meta
        payload = {
            "timestamp": ts,
            "audio_file": wav_name if audio_saved else None,
            "audio_saved": audio_saved,
            "audio_error": audio_error,
            "duration_s": meta.get("duration_s"),
            "sample_rate": meta.get("sample_rate", SAMPLE_RATE),
            "transcript": original,
            "edited": edited,
            "was_edited": was_edited,
            "model": meta.get("model"),
            "language": meta.get("language"),
            "initial_prompt": meta.get("initial_prompt"),
        }
        try:
            with open(json_path, "w") as f:
                json.dump(payload, f, indent=2)
            log.info("training sample saved: %s (was_edited=%s)", json_path, was_edited)
        except Exception as exc:
            log.warning("training sample save failed: %s", exc)


def cmd_app(_args):
    ensure_daemon_running()
    original_wid = subprocess.run(
        ["xdotool", "getactivewindow"],
        capture_output=True, text=True,
    ).stdout.strip()
    app = DictateAppWindow(original_wid)
    app.run()


# ── CLI entry point ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="dictate",
        description="Push-to-talk dictation tool",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_daemon = sub.add_parser("daemon", help="Start the background daemon")
    p_daemon.add_argument(
        "--no-fork",
        dest="fork",
        action="store_false",
        default=True,
        help="Run in foreground instead of forking to background",
    )
    p_daemon.set_defaults(func=cmd_daemon)

    p_app = sub.add_parser("app", help="Open floating window for guided dictation")
    p_app.set_defaults(func=cmd_app)

    for name, fn in [
        ("toggle", cmd_toggle),
        ("start", cmd_start),
        ("stop", cmd_stop),
        ("cancel", cmd_cancel),
        ("status", cmd_status),
    ]:
        p = sub.add_parser(name)
        p.set_defaults(func=fn)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
