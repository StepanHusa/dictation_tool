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


def start_recording() -> None:
    """Select device and launch recording thread."""
    global _recording_active, _audio_chunks, _record_thread
    device = select_input_device(_config)
    _audio_chunks = []
    _recording_active = True
    log.info("recording: started on device %r", device or "system default")
    _record_thread = threading.Thread(
        target=_record_worker, args=(device,), daemon=True
    )
    _record_thread.start()


def stop_recording() -> None:
    """Stop the recording thread and store the audio in _last_audio."""
    global _recording_active, _record_thread, _last_audio
    _recording_active = False
    if _record_thread is not None:
        _record_thread.join(timeout=3)
        _record_thread = None
    if _audio_chunks:
        import numpy as np  # lazily imported
        _last_audio = np.concatenate(_audio_chunks, axis=0).flatten()
        duration = len(_last_audio) / SAMPLE_RATE
        log.info("recording: stopped — %.1f s of audio captured", duration)
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
    language = _config.get("language", "en")
    vad_filter = _config.get("vad_filter", True)
    segments, _ = _whisper_model.transcribe(
        audio, language=language, vad_filter=vad_filter
    )
    text = "".join(seg.text for seg in segments).strip()
    return text


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
            start_recording()
            notify("🎤 Dictation", "Listening…", urgency="low", timeout=0, replace_id=NOTIFY_ID)
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
    elif action == "transcript":
        return {"transcript": _last_transcript, "state": get_state()}
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

    def _center(self):
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        x = (sw - self.WIDTH) // 2
        y = (sh - self.HEIGHT) // 2
        self._root.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")

    def _unbind_all(self):
        for key in ("<Return>", "<Escape>", "<space>"):
            self._root.unbind(key)

    def run(self):
        self.phase_listening()
        self._root.mainloop()

    def phase_listening(self):
        self._unbind_all()
        self._label.config(text="🎤 Listening…")
        self._hint.config(text="Enter = stop   Esc = cancel")
        try:
            send_command("start")
        except Exception:
            pass
        self._root.bind("<Return>", lambda e: self.phase_transcribing())
        self._root.bind("<Escape>", lambda e: self._do_cancel())

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
            except Exception:
                text = ""
            self.phase_review(text)
        else:
            self._root.after(200, self._poll_transcribing)

    def phase_review(self, text: str):
        self._original_transcript = text
        self._unbind_all()
        display = text if text else "(empty transcript)"
        self._label.config(text=display)
        self._hint.config(text="Enter=insert   Space=edit   Esc=drop")
        self._root.bind("<Return>", lambda e: self._do_insert(text))
        self._root.bind("<space>", lambda e: self.phase_edit(text))
        self._root.bind("<Escape>", lambda e: self._do_drop())

    def phase_edit(self, text: str):
        self._original_transcript = text
        self._unbind_all()
        self._label.pack_forget()
        self._hint.config(text="Enter = confirm   Esc = cancel")

        import tkinter as tk
        if self._text_widget:
            self._text_widget.destroy()
        self._text_widget = tk.Text(self._frame, wrap="word", height=5)
        self._text_widget.pack(fill="both", expand=True, before=self._hint)
        self._text_widget.insert("1.0", text)
        self._text_widget.focus_set()
        self._text_widget.bind("<Return>", self._on_edit_confirm)
        self._text_widget.bind("<Escape>", lambda e: self._do_drop())

    def _on_edit_confirm(self, event):
        edited = self._text_widget.get("1.0", "end-1c")
        original = self._original_transcript
        if edited != original:
            self._do_save_training(original, edited)
        self._do_insert(edited)
        return "break"

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

    def _do_save_training(self, original: str, edited: str):
        from datetime import datetime
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        train_dir = os.path.expanduser("~/.local/share/dictation_tool/training")
        os.makedirs(train_dir, exist_ok=True)
        wav_path = os.path.join(train_dir, f"{ts}.wav")
        json_path = os.path.join(train_dir, f"{ts}.json")
        try:
            send_command("save_audio", path=wav_path)
        except Exception:
            wav_path = None
        meta = {
            "timestamp": ts,
            "audio": wav_path,
            "transcript": original,
            "edited": edited,
        }
        try:
            with open(json_path, "w") as f:
                json.dump(meta, f, indent=2)
            log.info("training data saved: %s", json_path)
        except Exception as exc:
            log.warning("training data save failed: %s", exc)


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
