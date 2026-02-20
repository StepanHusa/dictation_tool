#!/usr/bin/env python3
"""Unit test: verify notify-send notifications fire at the correct stages.

Success criterion tested:
  - All notify-send notifications appear at the correct stages

Verification strategy:
  - Import dictate module and patch notify() to record calls
  - Patch audio/BT helpers to no-ops for determinism
  - Exercise handle_command(start/stop/cancel) and _transcribe_worker directly
  - Verify each stage fires the correct notification with correct args
"""

import os
import sys
import threading
import time
from unittest.mock import patch

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, THIS_DIR)

import dictate  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}" + (f": {detail}" if detail else ""))


def reset_state():
    dictate.set_state("idle")
    dictate._recording_active = False
    dictate._audio_chunks = []
    dictate._record_thread = None
    dictate._last_audio = None
    dictate._bt_card_info = None
    dictate._bt_previous_profile = None
    dictate._config = {}


def main():
    # ── Test 1: start → "🎤 Dictation / Listening…" (urgency=low, timeout=3000) ──
    reset_state()
    notify_calls = []
    with patch.object(dictate, "notify", side_effect=lambda *a, **kw: notify_calls.append((a, kw))), \
         patch.object(dictate, "bt_switch_to_hfp"), \
         patch.object(dictate, "start_recording"):
        dictate.handle_command({"action": "start"})

    check(
        "start fires exactly one notification",
        len(notify_calls) == 1,
        f"got {notify_calls}",
    )
    if notify_calls:
        args, kwargs = notify_calls[0]
        check(
            "start notification title contains 'Dictation'",
            "Dictation" in args[0],
            f"title={args[0]!r}",
        )
        check(
            "start notification body contains 'Listening'",
            "Listening" in args[1],
            f"body={args[1]!r}",
        )
        check(
            "start notification urgency is 'low'",
            kwargs.get("urgency") == "low",
            f"urgency={kwargs.get('urgency')!r}",
        )
        check(
            "start notification timeout is 3000",
            kwargs.get("timeout") == 3000,
            f"timeout={kwargs.get('timeout')!r}",
        )

    # ── Test 2: cancel (from recording) → "❌ Dictation / Cancelled" ──────────
    reset_state()
    dictate.set_state("recording")
    notify_calls = []
    with patch.object(dictate, "notify", side_effect=lambda *a, **kw: notify_calls.append((a, kw))), \
         patch.object(dictate, "cancel_recording"), \
         patch.object(dictate, "bt_restore_profile"):
        dictate.handle_command({"action": "cancel"})

    check(
        "cancel fires exactly one notification",
        len(notify_calls) == 1,
        f"got {notify_calls}",
    )
    if notify_calls:
        args, kwargs = notify_calls[0]
        check(
            "cancel notification body contains 'Cancelled'",
            "Cancelled" in args[1],
            f"body={args[1]!r}",
        )

    # ── Test 3: stop (from recording) → "⏳ Dictation / Transcribing…" ────────
    reset_state()
    dictate.set_state("recording")
    notify_calls = []
    # We patch threading.Thread to prevent the worker from running here
    mock_thread = type("T", (), {"start": lambda self: None, "__init__": lambda self, **kw: None})()

    with patch.object(dictate, "notify", side_effect=lambda *a, **kw: notify_calls.append((a, kw))), \
         patch.object(dictate, "stop_recording"), \
         patch.object(dictate, "bt_restore_profile"), \
         patch("threading.Thread", return_value=mock_thread):
        dictate.handle_command({"action": "stop"})

    transcribing_calls = [c for c in notify_calls if "Transcribing" in c[0][1]]
    check(
        "stop fires 'Transcribing…' notification",
        len(transcribing_calls) == 1,
        f"all notify calls: {notify_calls}",
    )

    # ── Test 4: _transcribe_worker done → "✅ Dictation / Done — N words" ─────
    reset_state()
    try:
        import numpy as np
        dictate._last_audio = np.zeros(16000, dtype="float32")
        dictate._config = {}
        notify_calls = []
        with patch.object(dictate, "notify", side_effect=lambda *a, **kw: notify_calls.append((a, kw))), \
             patch.object(dictate, "transcribe_audio", return_value="hello world"), \
             patch.object(dictate, "inject_text"):
            dictate._transcribe_worker()

        done_calls = [c for c in notify_calls if "Done" in c[0][1]]
        check(
            "_transcribe_worker fires 'Done — N words' notification",
            len(done_calls) == 1,
            f"all notify calls: {notify_calls}",
        )
        if done_calls:
            args, _ = done_calls[0]
            check(
                "done notification reports correct word count",
                "2 words" in args[1],
                f"body={args[1]!r}",
            )
    except ImportError:
        print("  SKIP  numpy not available; skipping transcription-done notification test")

    # ── Test 5: _transcribe_worker error → "⚠ Dictation / <error>" ───────────
    reset_state()
    try:
        import numpy as np
        dictate._last_audio = np.zeros(16000, dtype="float32")
        dictate._config = {}
        notify_calls = []
        with patch.object(dictate, "notify", side_effect=lambda *a, **kw: notify_calls.append((a, kw))), \
             patch.object(dictate, "transcribe_audio", side_effect=RuntimeError("model crash")):
            dictate._transcribe_worker()

        error_calls = [c for c in notify_calls if "model crash" in c[0][1]]
        check(
            "_transcribe_worker fires error notification on transcription failure",
            len(error_calls) == 1,
            f"all notify calls: {notify_calls}",
        )
    except ImportError:
        print("  SKIP  numpy not available; skipping transcription-error notification test")

    print()
    print(f"Results: {len(PASS)} passed, {len(FAIL)} failed")
    return len(FAIL)


if __name__ == "__main__":
    rc = main()
    sys.exit(rc)
