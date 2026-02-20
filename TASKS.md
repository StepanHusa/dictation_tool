# Dictation Tool - Tasks & Testing

## Research Summary

Similar tools found:
- [nerd-dictation](https://github.com/ideasman42/nerd-dictation) — hackable VOSK-based, ydotool/xdotool, well-structured
- [whisper-dictation (jacopone)](https://github.com/jacopone/whisper-dictation) — whisper.cpp, push-to-talk, ydotool, evdev
- [voice_typing (themanyone)](https://github.com/themanyone/voice_typing) — bash script, SOX silence detection, xdotool
- [AlterFlow faster-whisper guide](https://alterflow.ai/blog/offline-voice-dictation-ubuntu) — Wayland/ydotool walkthrough
- [openai/whisper Discussion #1282](https://github.com/openai/whisper/discussions/1282) — continuous dictation + xdotool pattern

**Decision:** Build custom tool on top of `faster-whisper` + `sounddevice` + `xdotool` (user is on X11/XFCE).
Daemon model: start once, keep alive. Start/stop via two key bindings (or one toggle).

---

## Architecture

```
dictate.py <command>
  daemon   — start background daemon (auto-called on first use)
  start    — begin recording (shows "Listening…" notification)
  stop     — end recording → transcribe → xdotool type result
  toggle   — start if idle, stop-and-type if recording
  status   — print current state (idle/recording/transcribing)
  cancel   — abort current recording without typing

~/.config/dictation_tool/config.toml   — user config
/tmp/dictation_tool.sock               — Unix socket (CLI ↔ daemon)
/tmp/dictation_tool.pid                — PID file
```

Audio device selection priority:
1. Config override (explicit device name)
2. Bluetooth headset in HFP/HSP profile (auto-detected via pactl)
3. Default PipeWire source

Bluetooth flow: on `start`, if a BT card is present in A2DP profile, switch it to HFP/HSP. On `stop`, switch back to A2DP.

---

## TODO

### Phase 1 — Infrastructure
- [ ] Create `requirements.txt` (`faster-whisper`, `sounddevice`, `numpy`)
- [ ] Create `install.sh` (pip install, copy script to `~/.local/bin`, create config dir)
- [ ] Create `~/.config/dictation_tool/config.toml` defaults (model, device, hotkey hints)
- [ ] Implement daemon with Unix socket listener
- [ ] Implement PID file management (start once, reuse)
- [ ] Implement `dictate.py daemon` command

### Phase 2 — Audio Recording
- [ ] List available input sources via `pactl list sources`
- [ ] Auto-select best mic (prefer BT headset when available)
- [ ] Implement `sounddevice` recording into numpy buffer
- [ ] Implement VAD-based auto-stop (silence detection via `faster-whisper` VAD filter)
- [ ] Implement manual stop via `stop`/`toggle` command

### Phase 3 — Bluetooth / Device Switching
- [ ] Detect Bluetooth cards with `pactl list cards`
- [ ] Switch card profile A2DP → HFP on recording start
- [ ] Restore A2DP profile after recording stops
- [ ] Handle edge cases: BT not connected, no mic, profile switch failure
- [ ] Config option: `auto_switch_bt = true/false`

### Phase 4 — Transcription
- [ ] Load `faster-whisper` model on daemon startup (warm-up)
- [ ] Transcribe audio buffer on `stop`
- [ ] Show "Transcribing…" notification while processing
- [ ] Config: model size (`tiny`, `base`, `small`, `medium`, `large-v3`)
- [ ] Config: language (default `en`, or `auto`)
- [ ] Config: VAD filter on/off

### Phase 5 — Text Injection
- [ ] Inject text via `xdotool type --clearmodifiers --delay 0 -- "$TEXT"`
- [ ] Clip fallback: copy to clipboard + `xdotool key ctrl+v` (for apps that block xdotool type)
- [ ] Config: injection method (`xdotool` / `clipboard`)
- [ ] Strip leading/trailing whitespace; optionally add trailing space

### Phase 6 — Notifications
- [ ] `notify-send` "🎤 Dictation" "Listening…" on start (with app icon)
- [ ] `notify-send` "⏳ Dictation" "Transcribing…" during processing
- [ ] `notify-send` "✅ Dictation" "Done — N words" on finish
- [ ] `notify-send` "❌ Dictation" "Cancelled" on cancel
- [ ] Error notification on failure

### Phase 7 — Keyboard Shortcuts (XFCE)
- [ ] Document how to add shortcut in XFCE Keyboard settings
- [ ] Recommend: `Super+D` → `dictate.py toggle`  (or `start`)
- [ ] Recommend: `Super+Shift+D` → `dictate.py cancel`
- [ ] Consider: `Super+D` start, any subsequent `Super+D` stops (toggle mode default)

### Phase 8 — Packaging & UX
- [ ] `install.sh`: create venv in `~/.local/share/dictation_tool/venv`
- [ ] `install.sh`: write wrapper `~/.local/bin/dictate` that activates venv
- [ ] `install.sh`: create XFCE autostart entry (optional daemon pre-warm)
- [ ] `README.md`: install, configure, keybind instructions
- [ ] Handle missing dependencies gracefully with helpful errors

---

## Testing Scenarios

### T1 — Basic dictation (happy path)
1. Press start shortcut
2. Speak: "Hello world this is a test"
3. Press stop shortcut
4. **Expected:** text "Hello world this is a test" typed at cursor position
5. **Expected:** notifications appeared: Listening → Transcribing → Done

### T2 — Dictation in terminal (bash)
1. Open a terminal, cursor in shell prompt
2. Run toggle shortcut, speak a sentence, stop
3. **Expected:** text appears inline in the shell prompt (not submitted)
4. Note: xdotool type works with most terminals; test xterm, gnome-terminal, xfce4-terminal

### T3 — Dictation in Vim
1. Open vim, enter Insert mode (press `i`)
2. Trigger dictation, speak, stop
3. **Expected:** text inserted at cursor
4. Edge: test from Normal mode (xdotool will type literal characters — warn user to be in Insert mode)

### T4 — Dictation in VSCode
1. Open VSCode, click in editor
2. Trigger dictation, speak, stop
3. **Expected:** text inserted at cursor; test both xdotool type and clipboard fallback

### T5 — Dictation in browser (text field)
1. Open browser, click in a text input
2. Trigger dictation, stop
3. **Expected:** text typed into field; clipboard fallback may be needed for Electron apps

### T6 — Bluetooth headset auto-switching
1. Connect BT headset (A2DP profile active — good audio, no mic)
2. Trigger dictation
3. **Expected:** profile switches to HFP/HSP (mic enabled); notification confirms source
4. Stop dictation
5. **Expected:** profile switches back to A2DP (good audio restored)

### T7 — Non-BT mic (wired or USB interface)
1. With NI Komplete Audio 2 or built-in mic
2. Trigger dictation, speak, stop
3. **Expected:** correct source auto-selected or from config; no BT switching attempted

### T8 — Daemon persistence
1. Run `dictate.py start` (first time — daemon should auto-start)
2. Kill terminal / close session
3. Check `ps aux | grep dictate` — daemon still running
4. Run `dictate.py start` again
5. **Expected:** reuses existing daemon (no duplicate launch)

### T9 — Cancel mid-dictation
1. Start recording
2. Press cancel shortcut before stopping
3. **Expected:** recording stopped, nothing typed, "Cancelled" notification shown

### T10 — Multiple sequential dictations
1. Dictate sentence 1 → stop → verify
2. Immediately dictate sentence 2 → stop → verify
3. **Expected:** both work; daemon stays warm between uses; model already loaded

### T11 — Long dictation (>30 seconds)
1. Speak continuously for ~45 seconds
2. Stop and verify
3. **Expected:** full transcription typed correctly; no buffer overflow

### T12 — Background noise / silence
1. Start recording and say nothing for 5 seconds, then speak
2. **Expected:** silence correctly handled; VAD skips silent leading period

### T13 — Wrong focus (dictate to wrong window)
1. Start dictation in window A
2. Click to window B before stopping
3. **Expected:** text typed into window B (wherever cursor is at stop time) — document this behavior

### T14 — Daemon crash recovery
1. Kill daemon process with `kill -9`
2. Trigger dictation
3. **Expected:** daemon auto-restarts; PID file cleaned up correctly

### T15 — Model warm-up time
1. First use after boot (model not loaded)
2. Press start
3. **Expected:** notification "Loading model…" before "Listening…"; acceptable delay (<10s for `base`)
