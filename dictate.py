#!/usr/bin/env python3
"""dictate — Linux push-to-talk dictation tool (daemon + CLI)."""

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib

PID_FILE = "/tmp/dictation_tool.pid"
SOCK_FILE = "/tmp/dictation_tool.sock"
CONFIG_PATH = os.path.expanduser("~/.config/dictation_tool/config.toml")

SAMPLE_RATE = 16000


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
        return override

    bt_source = list_bt_headset_source()
    if bt_source:
        print(f"dictate: auto-selected BT source: {bt_source}", flush=True)
        return bt_source

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
        print("dictate: no BT card with A2DP+HFP found; skipping profile switch", flush=True)
        return

    _bt_card_info = info
    _bt_previous_profile = info["active_profile"]

    if info["active_profile"] == info["a2dp_profile"]:
        try:
            subprocess.run(
                ["pactl", "set-card-profile", info["name"], info["hfp_profile"]],
                timeout=5, check=True, capture_output=True,
            )
            print(
                f"dictate: BT profile switched {info['a2dp_profile']!r} → {info['hfp_profile']!r}",
                flush=True,
            )
        except Exception as exc:
            print(f"dictate: warning: failed to switch BT profile: {exc}", flush=True)
    else:
        print(
            f"dictate: BT card not on A2DP (active={info['active_profile']!r}); not switching",
            flush=True,
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
        print(f"dictate: BT profile restored to {prev!r}", flush=True)
    except Exception as exc:
        print(f"dictate: warning: failed to restore BT profile: {exc}", flush=True)


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
        print(f"dictate: recording error: {exc}", flush=True)
        # Drop back to idle so the daemon stays usable
        _recording_active = False
        set_state("idle")


def start_recording() -> None:
    """Select device and launch recording thread."""
    global _recording_active, _audio_chunks, _record_thread
    device = select_input_device(_config)
    _audio_chunks = []
    _recording_active = True
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
    else:
        _last_audio = None


def cancel_recording() -> None:
    """Stop the recording thread and discard the audio."""
    global _recording_active, _record_thread, _last_audio
    _recording_active = False
    if _record_thread is not None:
        _record_thread.join(timeout=3)
        _record_thread = None
    _last_audio = None


# ── Transcription ────────────────────────────────────────────────────────────

def load_whisper_model_bg(config: dict) -> None:
    """Load the faster-whisper model in a background thread; signal _model_ready when done."""
    global _whisper_model
    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415
        model_size = config.get("model", "base")
        print(f"dictate: loading Whisper model {model_size!r}…", flush=True)
        _whisper_model = WhisperModel(model_size, device="cpu", compute_type="int8")
        print("dictate: Model loaded", flush=True)
    except Exception as exc:
        print(f"dictate: failed to load Whisper model: {exc}", flush=True)
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


def _transcribe_worker() -> None:
    """Background thread: transcribe _last_audio, store result, set state idle."""
    global _last_transcript
    if _last_audio is None:
        set_state("idle")
        return
    try:
        text = transcribe_audio(_last_audio)
        _last_transcript = text
        print(
            f"dictate: transcription done: {len(text.split())} word(s)",
            flush=True,
        )
        inject_text(text, _config)
    except Exception as exc:
        print(f"dictate: transcription error: {exc}", flush=True)
        _last_transcript = None
    finally:
        set_state("idle")


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
            print("dictate: text injected via clipboard", flush=True)
        except Exception as exc:
            print(f"dictate: clipboard injection error: {exc}", flush=True)
    else:
        # Default: xdotool
        try:
            subprocess.run(
                ["xdotool", "type", "--clearmodifiers", "--delay", "0", "--", text],
                timeout=30,
                check=True,
            )
            print("dictate: text injected via xdotool", flush=True)
        except Exception as exc:
            print(f"dictate: xdotool injection error: {exc}", flush=True)


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
    if action == "status":
        return {"state": get_state()}
    elif action == "start":
        if get_state() == "idle":
            set_state("recording")
            bt_switch_to_hfp()
            start_recording()
        return {"ok": True, "state": get_state()}
    elif action == "stop":
        if get_state() == "recording":
            set_state("transcribing")
            stop_recording()
            bt_restore_profile()
            threading.Thread(target=_transcribe_worker, daemon=True).start()
        return {"ok": True, "state": get_state()}
    elif action == "cancel":
        if get_state() == "recording":
            cancel_recording()
            bt_restore_profile()
        set_state("idle")
        return {"ok": True, "state": get_state()}
    elif action == "quit":
        return {"ok": True, "quit": True}
    else:
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

    write_pid()

    import atexit
    atexit.register(cleanup)

    def _sig_handler(*_):
        cleanup()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_FILE)
    srv.listen(5)

    # Start loading the Whisper model in the background (socket is already up)
    threading.Thread(
        target=load_whisper_model_bg, args=(_config,), daemon=True
    ).start()

    # Log to syslog-style: will be invisible when daemonized, visible in --no-fork mode
    print(f"dictate daemon started (pid={os.getpid()})", flush=True)

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
        srv.close()
        cleanup()


# ── Subcommand implementations ──────────────────────────────────────────────

def cmd_daemon(args):
    if args.fork:
        daemonize()
    run_daemon()


def send_command(action):
    """Send a JSON command to the daemon and return the response dict.
    Raises ConnectionRefusedError / FileNotFoundError if daemon is not running."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(SOCK_FILE)
    sock.sendall(json.dumps({"action": action}).encode() + b"\n")
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
