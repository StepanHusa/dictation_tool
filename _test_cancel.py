#!/usr/bin/env python3
"""Integration test: verify that 'dictate cancel' aborts without typing anything.

Success criterion tested:
  - `dictate cancel` aborts without typing anything

Verification strategy:
  - After cancel, state is immediately 'idle' (not 'transcribing')
  - State never becomes 'transcribing' in the 3 seconds after cancel
    (the transcription worker is only launched after 'stop', not after 'cancel')
  - cancel_recording() discards audio (_last_audio=None), so even if a worker
    ran it would produce nothing; the absence of 'transcribing' state confirms
    the worker was never started.
"""

import json
import os
import socket
import subprocess
import sys
import time

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DICTATE_PY = os.path.join(THIS_DIR, "dictate.py")
SOCK_FILE = "/tmp/dictation_tool.sock"
PID_FILE = "/tmp/dictation_tool.pid"

VENV_PYTHON = os.path.expanduser("~/.local/share/dictation_tool/venv/bin/python3")
PYTHON = VENV_PYTHON if os.path.exists(VENV_PYTHON) else sys.executable


def _cleanup_stale():
    for p in (SOCK_FILE, PID_FILE):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass


def _wait_for_socket(timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(0.2)
            s.connect(SOCK_FILE)
            s.close()
            return True
        except (FileNotFoundError, ConnectionRefusedError, OSError):
            time.sleep(0.1)
    return False


def _send(action):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(SOCK_FILE)
    s.sendall(json.dumps({"action": action}).encode() + b"\n")
    data = b""
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
        if b"\n" in data:
            break
    s.close()
    return json.loads(data.strip())


def _get_state():
    return _send("status").get("state", "unknown")


PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}" + (f": {detail}" if detail else ""))


def main():
    _cleanup_stale()

    daemon_proc = subprocess.Popen(
        [PYTHON, DICTATE_PY, "daemon", "--no-fork"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    try:
        sock_up = _wait_for_socket(timeout=8)
        check("daemon starts and socket is available", sock_up)
        if not sock_up:
            print("Cannot continue without daemon socket")
            return

        # 1 — Initial state is idle
        state = _get_state()
        check("initial state is idle", state == "idle", f"got {state!r}")

        # ── Put daemon into recording state ──────────────────────────────────
        _send("start")
        time.sleep(0.15)
        state_after_start = _get_state()
        # Audio may fail immediately in test env; either recording or idle is acceptable
        check(
            "start command accepted (state is recording or idle)",
            state_after_start in ("recording", "idle"),
            f"got {state_after_start!r}",
        )

        # ── Send cancel ───────────────────────────────────────────────────────
        cancel_resp = _send("cancel")

        # 2 — Cancel returns ok
        check("cancel command returns ok", cancel_resp.get("ok") is True, str(cancel_resp))

        # 3 — State after cancel is immediately idle (not transcribing)
        state_after_cancel = cancel_resp.get("state", "unknown")
        check(
            "state after cancel is immediately idle (not transcribing)",
            state_after_cancel == "idle",
            f"got {state_after_cancel!r}",
        )

        # 4 — State stays idle for 3 seconds (transcription worker was NOT started)
        transcribing_observed = False
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            s = _get_state()
            if s == "transcribing":
                transcribing_observed = True
                break
            time.sleep(0.2)
        check(
            "state never becomes 'transcribing' after cancel (no injection)",
            not transcribing_observed,
            "transcribing state was observed after cancel",
        )

        # 5 — Confirm state is still idle after the observation window
        final_state = _get_state()
        check(
            "state remains idle after cancel observation window",
            final_state == "idle",
            f"got {final_state!r}",
        )

        # ── Verify second cancel when already idle is a no-op ─────────────────
        idle_cancel_resp = _send("cancel")
        check(
            "cancel when already idle returns ok",
            idle_cancel_resp.get("ok") is True,
            str(idle_cancel_resp),
        )
        check(
            "cancel when already idle keeps state idle",
            idle_cancel_resp.get("state") == "idle",
            f"got {idle_cancel_resp.get('state')!r}",
        )

    finally:
        try:
            _send("quit")
        except Exception:
            pass
        daemon_proc.terminate()
        try:
            daemon_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            daemon_proc.kill()
            daemon_proc.wait(timeout=2)
        _cleanup_stale()

    print()
    print(f"Results: {len(PASS)} passed, {len(FAIL)} failed")
    return len(FAIL)


if __name__ == "__main__":
    rc = main()
    sys.exit(rc)
