# PRD — Linux Dictation Tool

## Overview

A Linux speech-to-text dictation daemon that transcribes speech offline and types the result
wherever the cursor is. Push-to-talk style, triggered by a keyboard shortcut.

**Environment:**
- OS: Linux Mint / Ubuntu-based, X11 session, XFCE desktop
- Audio: PipeWire (pactl available), Bluetooth headset support needed
- Input injection: `xdotool` (X11)
- Python 3.13, no virtualenv yet

---

## Architecture

```
~/.local/bin/dictate          wrapper script (activates venv, runs dictate.py)
~/Source/dictation_tool/
  dictate.py                  main script (daemon + CLI)
  requirements.txt
  install.sh
  README.md
  PRD.md
  progress.txt

~/.config/dictation_tool/config.toml   user config
~/.local/share/dictation_tool/venv/    virtualenv
/tmp/dictation_tool.sock               Unix socket (CLI ↔ daemon)
/tmp/dictation_tool.pid                PID file
```

**CLI commands:** `dictate <toggle|start|stop|cancel|status|daemon>`

---

## Tasks

Mark tasks `[x]` when complete. Do NOT skip tasks; work in order unless blocked.

### Phase 1 — Project scaffold
- [ ] `requirements.txt` with: faster-whisper, sounddevice, numpy (+ tomli if Python < 3.11)
- [ ] `install.sh`: create venv, pip install, write `~/.local/bin/dictate` wrapper, create config dir
- [ ] Default `~/.config/dictation_tool/config.toml` written by install.sh with:
      model=base, language=en, vad_filter=true, auto_switch_bt=true, injection_method=xdotool
- [ ] Git repo initialised (`git init`, initial commit with scaffold files)

### Phase 2 — Daemon core
- [ ] `dictate.py daemon`: start background process, write PID to `/tmp/dictation_tool.pid`,
      open Unix socket at `/tmp/dictation_tool.sock`, listen for commands
- [ ] `dictate.py status`: connect to socket, print state (idle/recording/transcribing/not-running)
- [ ] Daemon auto-started by `start`/`toggle` if not already running (subprocess.Popen detached)
- [ ] PID file cleanup on daemon exit; stale PID detection on startup

### Phase 3 — Audio recording
- [ ] List available input sources via `pactl list sources short`
- [ ] Auto-select input: prefer BT headset source when available, else fall back to default
- [ ] Record audio with `sounddevice` into numpy float32 buffer at 16 kHz
- [ ] Config option `device` to override source (name or index)

### Phase 4 — Bluetooth profile switching
- [ ] Detect BT cards: `pactl list cards` → find cards with both A2DP and HFP/HSP profiles
- [ ] On `start`: if BT card in A2DP profile, switch to `headset-head-unit` (HFP/HSP)
- [ ] On `stop`/`cancel`: restore previous BT profile (A2DP)
- [ ] Graceful no-op if no BT device; log warning, do not crash
- [ ] Config option `auto_switch_bt = false` to disable

### Phase 5 — Transcription
- [ ] Load `faster-whisper` model on daemon startup; log "Model loaded"
- [ ] Transcribe numpy buffer on `stop` command
- [ ] Strip leading/trailing whitespace from result
- [ ] Config options: `model` size, `language` (default `en`), `vad_filter`

### Phase 6 — Text injection
- [ ] Inject via `xdotool type --clearmodifiers --delay 0 -- "$TEXT"`
- [ ] Clipboard fallback: `xclip -selection clipboard` + `xdotool key ctrl+v`
- [ ] Config option `injection_method = xdotool` or `clipboard`

### Phase 7 — Notifications
- [ ] `start`: notify-send "🎤 Dictation" "Listening…" (urgency low, timeout 3000)
- [ ] `stop` (transcribing): notify-send "⏳ Dictation" "Transcribing…"
- [ ] `stop` (done): notify-send "✅ Dictation" "Done — N words"
- [ ] `cancel`: notify-send "❌ Dictation" "Cancelled"
- [ ] Errors: notify-send "⚠ Dictation" "<error message>"

### Phase 8 — README
- [ ] Install instructions (clone, run install.sh, add ~/.local/bin to PATH)
- [ ] XFCE keyboard shortcut setup: Settings → Keyboard → Application Shortcuts
      Recommend: `Super+D` → `dictate toggle`, `Super+Shift+D` → `dictate cancel`
- [ ] Config file reference (all options documented)
- [ ] Troubleshooting section (no mic found, BT not switching, xdotool not typing)

---

## Success Criteria

- [ ] `bash install.sh` completes without errors
- [ ] `dictate toggle` starts daemon (first call), shows notification, records, transcribes, types text
- [ ] `dictate toggle` again stops recording and types result
- [ ] `dictate cancel` aborts without typing anything
- [ ] All notify-send notifications appear at the correct stages
- [ ] BT switching is a graceful no-op when no BT device is present
- [ ] Daemon survives terminal close; second call reuses running daemon
- [ ] `python3 -m py_compile dictate.py` passes with no errors
