#!/usr/bin/env python3
"""Integration test: daemon survives terminal close; second call reuses running daemon.

Success criterion tested:
  - Daemon survives terminal close; second call reuses running daemon

Verification strategy:
  1. Start daemon via the normal forked 'daemon' subcommand (double-fork detaches it).
     The Popen call returns almost immediately because the parent process exits after
     the first fork, leaving only the grandchild daemon process.
  2. Wait for the Unix socket to become available.
  3. Record the PID from the PID file.
  4. Verify the daemon responds to a 'status' command.
  5. Simulate "terminal close" by just waiting — the daemon is already fully detached;
     there is no parent process that could SIGHUP it.
  6. After the wait, confirm the socket is still reachable (daemon survived).
  7. Call ensure_daemon_running() — since the socket is already up, it must NOT spawn
     a second daemon.  Verify the PID in the PID file is unchanged.
  8. Send another 'status' command and confirm the daemon still responds correctly.
  9. Clean up by sending 'quit' and removing stale files.
"""

import importlib.util
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


# ── Load dictate module ───────────────────────────────────────────────────────

def _load_dictate():
    spec = importlib.util.spec_from_file_location("dictate", DICTATE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Socket / file helpers ─────────────────────────────────────────────────────

def _cleanup_stale():
    for p in (SOCK_FILE, PID_FILE):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass


def _wait_for_socket(timeout=10.0):
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


def _socket_reachable():
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(SOCK_FILE)
        s.close()
        return True
    except (FileNotFoundError, ConnectionRefusedError, OSError):
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


def _read_pid():
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, TypeError):
        return False
    except PermissionError:
        return True


# ── Test harness ──────────────────────────────────────────────────────────────

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}" + (f": {detail}" if detail else ""))


# ── Integration test ──────────────────────────────────────────────────────────

def run_test(mod):
    print("── Integration test: daemon survival ──")
    _cleanup_stale()

    # ── Step 1: Start daemon in forked mode ───────────────────────────────────
    # We Popen the 'daemon' subcommand without --no-fork.  daemonize() will
    # double-fork; the Popen process exits after the first fork, so wait()
    # returns quickly.  The grandchild daemon is fully detached.
    proc = subprocess.Popen(
        [PYTHON, DICTATE_PY, "daemon"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    # Wait for the Popen process to exit (it does so after first fork)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    # ── Step 2: Wait for socket ───────────────────────────────────────────────
    sock_up = _wait_for_socket(timeout=10)
    check("daemon socket is available after forked start", sock_up)
    if not sock_up:
        print("Cannot continue without daemon socket")
        return

    # ── Step 3: Record PID ────────────────────────────────────────────────────
    pid_before = _read_pid()
    check("PID file was written by daemon", pid_before is not None, f"got {pid_before!r}")
    check("daemon process is alive", _pid_alive(pid_before), f"pid={pid_before}")

    # ── Step 4: Daemon responds to status ─────────────────────────────────────
    resp = _send("status")
    check(
        "daemon responds to status command",
        resp.get("state") == "idle",
        str(resp),
    )

    # ── Step 5 & 6: Simulate terminal close → daemon must still be alive ──────
    # The daemon is already fully detached (double-forked, setsid, stdio→/dev/null).
    # There is no controlling terminal to close.  We just wait a moment and then
    # confirm the daemon is still running.
    time.sleep(1.5)

    check(
        "daemon socket still reachable after simulated terminal-close delay",
        _socket_reachable(),
    )
    check(
        "daemon process still alive after delay",
        _pid_alive(pid_before),
        f"pid={pid_before}",
    )

    # ── Step 7: ensure_daemon_running() must NOT start a second daemon ─────────
    # We call it directly from the loaded module (it writes to sys.executable,
    # which is PYTHON used here).
    mod.SOCK_FILE = SOCK_FILE
    mod.PID_FILE = PID_FILE

    pid_file_before = _read_pid()

    mod.ensure_daemon_running()  # should be a no-op — daemon is already up

    pid_after = _read_pid()
    check(
        "ensure_daemon_running() does not change PID (no second daemon spawned)",
        pid_after == pid_before,
        f"before={pid_before}, after={pid_after}",
    )

    # ── Step 8: Daemon still responds after ensure_daemon_running() no-op ─────
    resp2 = _send("status")
    check(
        "daemon still responds after ensure_daemon_running() no-op",
        resp2.get("state") == "idle",
        str(resp2),
    )

    # ── Cleanup ───────────────────────────────────────────────────────────────
    try:
        _send("quit")
    except Exception:
        pass

    # Give daemon a moment to exit, then confirm it is gone
    deadline = time.monotonic() + 4.0
    daemon_exited = False
    while time.monotonic() < deadline:
        if not _pid_alive(pid_before):
            daemon_exited = True
            break
        time.sleep(0.2)

    check("daemon exits cleanly after quit command", daemon_exited)

    _cleanup_stale()


def main():
    print("=== Daemon survival / reuse test ===")
    print()

    try:
        mod = _load_dictate()
    except Exception as exc:
        print(f"  ERROR loading dictate module: {exc}")
        FAIL.append("module load")
        print()
        print(f"Results: {len(PASS)} passed, {len(FAIL)} failed")
        return len(FAIL)

    run_test(mod)

    print()
    print(f"Results: {len(PASS)} passed, {len(FAIL)} failed")
    return len(FAIL)


if __name__ == "__main__":
    rc = main()
    sys.exit(rc)
