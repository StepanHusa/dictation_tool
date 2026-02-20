#!/usr/bin/env python3
"""Integration + unit test: BT switching is a graceful no-op when no BT device is present.

Success criterion tested:
  - BT switching is a graceful no-op when no BT device is present

Verification strategy (unit tests):
  1. find_bt_card_info() returns None for empty pactl output
  2. find_bt_card_info() returns None for pactl output with no BT cards
  3. find_bt_card_info() returns None for a BT card with only A2DP (no HFP profile)
  4. bt_switch_to_hfp() does not crash and does not set _bt_card_info when no BT card present
  5. bt_restore_profile() is a no-op when _bt_card_info is None

Verification strategy (integration):
  6. Start/stop cycle completes without error in daemon (no BT in test env)
  7. Start/cancel cycle completes without error in daemon (no BT in test env)
  8. Daemon state returns to 'idle' after each cycle (not crashed)
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


# ── Load dictate module for unit tests ──────────────────────────────────────

def _load_dictate():
    spec = importlib.util.spec_from_file_location("dictate", DICTATE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Socket helpers ───────────────────────────────────────────────────────────

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


# ── Test harness ─────────────────────────────────────────────────────────────

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}" + (f": {detail}" if detail else ""))


# ── Unit tests ───────────────────────────────────────────────────────────────

def run_unit_tests(mod):
    print("── Unit tests ──")

    # 1 — Empty output → None
    result = mod.find_bt_card_info("")
    check("find_bt_card_info('') returns None", result is None, f"got {result!r}")

    # 2 — Non-BT card only → None
    non_bt_output = (
        "Card #0\n"
        "\tName: alsa_card.pci-0000_00_1f.3\n"
        "\tActive Profile: output:analog-stereo\n"
        "\tProfiles:\n"
        "\t\toutput:analog-stereo: Analog Stereo Output\n"
        "\t\toff: Off\n"
    )
    result = mod.find_bt_card_info(non_bt_output)
    check("find_bt_card_info(non-BT output) returns None", result is None, f"got {result!r}")

    # 3 — BT card with A2DP only (no HFP) → None
    bt_a2dp_only = (
        "Card #1\n"
        "\tName: bluez_card.AA_BB_CC_DD_EE_FF\n"
        "\tActive Profile: a2dp-sink\n"
        "\tProfiles:\n"
        "\t\ta2dp-sink: High Fidelity Playback (A2DP Sink)\n"
        "\t\toff: Off\n"
    )
    result = mod.find_bt_card_info(bt_a2dp_only)
    check(
        "find_bt_card_info(BT A2DP-only, no HFP) returns None",
        result is None,
        f"got {result!r}",
    )

    # 4 — bt_switch_to_hfp() with auto_switch_bt=True but no BT card present:
    #     should not raise, and _bt_card_info must remain None
    mod._bt_card_info = None
    mod._bt_previous_profile = None
    mod._config = {"auto_switch_bt": True}
    try:
        mod.bt_switch_to_hfp()
        raised = False
    except Exception as exc:
        raised = True
        print(f"    exception: {exc}")
    check("bt_switch_to_hfp() does not raise when no BT card", not raised)
    check(
        "bt_switch_to_hfp() leaves _bt_card_info=None when no BT card",
        mod._bt_card_info is None,
        f"got {mod._bt_card_info!r}",
    )

    # 5 — bt_restore_profile() when _bt_card_info is None → no-op, no crash
    mod._bt_card_info = None
    mod._bt_previous_profile = None
    mod._config = {"auto_switch_bt": True}
    try:
        mod.bt_restore_profile()
        raised = False
    except Exception as exc:
        raised = True
        print(f"    exception: {exc}")
    check("bt_restore_profile() does not raise when _bt_card_info is None", not raised)
    check(
        "bt_restore_profile() leaves _bt_card_info=None (no-op)",
        mod._bt_card_info is None,
        f"got {mod._bt_card_info!r}",
    )

    # 6 — bt_switch_to_hfp() with auto_switch_bt=False → immediate no-op
    mod._bt_card_info = None
    mod._bt_previous_profile = None
    mod._config = {"auto_switch_bt": False}
    try:
        mod.bt_switch_to_hfp()
        raised = False
    except Exception as exc:
        raised = True
        print(f"    exception: {exc}")
    check("bt_switch_to_hfp() does not raise with auto_switch_bt=False", not raised)


# ── Integration tests ────────────────────────────────────────────────────────

def run_integration_tests():
    print()
    print("── Integration tests (daemon) ──")

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
            print("Cannot continue integration tests without daemon socket")
            return

        # Confirm idle at start
        state = _get_state()
        check("initial state is idle", state == "idle", f"got {state!r}")

        # ── start → cancel (BT switch + restore must be graceful no-ops) ──
        _send("start")
        time.sleep(0.15)
        cancel_resp = _send("cancel")
        check(
            "start+cancel cycle: cancel returns ok (BT no-op, no crash)",
            cancel_resp.get("ok") is True,
            str(cancel_resp),
        )
        check(
            "start+cancel cycle: state is idle after cancel",
            cancel_resp.get("state") == "idle",
            f"got {cancel_resp.get('state')!r}",
        )

        # Poll for a moment to ensure daemon is still alive
        time.sleep(0.3)
        state = _get_state()
        check(
            "daemon still running and idle after start+cancel cycle",
            state == "idle",
            f"got {state!r}",
        )

        # ── start → stop (BT switch + restore must be graceful no-ops) ──
        _send("start")
        time.sleep(0.15)
        stop_resp = _send("stop")
        check(
            "start+stop cycle: stop returns ok (BT no-op, no crash)",
            stop_resp.get("ok") is True,
            str(stop_resp),
        )
        # State will be 'transcribing' or 'idle' (audio may fail immediately)
        state_after_stop = stop_resp.get("state", "unknown")
        check(
            "start+stop cycle: state is transcribing or idle after stop",
            state_after_stop in ("transcribing", "idle"),
            f"got {state_after_stop!r}",
        )

        # Wait for state to settle back to idle
        settled = False
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if _get_state() == "idle":
                settled = True
                break
            time.sleep(0.2)
        check(
            "daemon returns to idle after start+stop cycle (no crash from BT no-op)",
            settled,
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


def main():
    print("=== BT graceful no-op test ===")
    print()

    try:
        mod = _load_dictate()
        run_unit_tests(mod)
    except Exception as exc:
        print(f"  ERROR loading dictate module: {exc}")
        FAIL.append("module load")

    run_integration_tests()

    print()
    print(f"Results: {len(PASS)} passed, {len(FAIL)} failed")
    return len(FAIL)


if __name__ == "__main__":
    rc = main()
    sys.exit(rc)
