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
- [x] `requirements.txt` with: faster-whisper, sounddevice, numpy (+ tomli if Python < 3.11)
- [x] `install.sh`: create venv, pip install, write `~/.local/bin/dictate` wrapper, create config dir
- [x] Default `~/.config/dictation_tool/config.toml` written by install.sh with:
      model=base, language=en, vad_filter=true, auto_switch_bt=true, injection_method=xdotool
- [x] Git repo initialised (`git init`, initial commit with scaffold files)

### Phase 2 — Daemon core
- [x] `dictate.py daemon`: start background process, write PID to `/tmp/dictation_tool.pid`,
      open Unix socket at `/tmp/dictation_tool.sock`, listen for commands
- [x] `dictate.py status`: connect to socket, print state (idle/recording/transcribing/not-running)
- [x] Daemon auto-started by `start`/`toggle` if not already running (subprocess.Popen detached)
- [x] PID file cleanup on daemon exit; stale PID detection on startup

### Phase 3 — Audio recording
- [x] List available input sources via `pactl list sources short`
- [x] Auto-select input: prefer BT headset source when available, else fall back to default
- [x] Record audio with `sounddevice` into numpy float32 buffer at 16 kHz
- [x] Config option `device` to override source (name or index)

### Phase 4 — Bluetooth profile switching
- [x] Detect BT cards: `pactl list cards` → find cards with both A2DP and HFP/HSP profiles
- [x] On `start`: if BT card in A2DP profile, switch to `headset-head-unit` (HFP/HSP)
- [x] On `stop`/`cancel`: restore previous BT profile (A2DP)
- [x] Graceful no-op if no BT device; log warning, do not crash
- [x] Config option `auto_switch_bt = false` to disable

### Phase 5 — Transcription
- [x] Load `faster-whisper` model on daemon startup; log "Model loaded"
- [x] Transcribe numpy buffer on `stop` command
- [x] Strip leading/trailing whitespace from result
- [x] Config options: `model` size, `language` (default `en`), `vad_filter`

### Phase 6 — Text injection
- [x] Inject via `xdotool type --clearmodifiers --delay 0 -- "$TEXT"`
- [x] Clipboard fallback: `xclip -selection clipboard` + `xdotool key ctrl+v`
- [x] Config option `injection_method = xdotool` or `clipboard`

### Phase 7 — Notifications
- [x] `start`: notify-send "🎤 Dictation" "Listening…" (urgency low, timeout 3000)
- [x] `stop` (transcribing): notify-send "⏳ Dictation" "Transcribing…"
- [x] `stop` (done): notify-send "✅ Dictation" "Done — N words"
- [x] `cancel`: notify-send "❌ Dictation" "Cancelled"
- [x] Errors: notify-send "⚠ Dictation" "<error message>"

### Phase 8 — README
- [x] Install instructions (clone, run install.sh, add ~/.local/bin to PATH)
- [x] XFCE keyboard shortcut setup: Settings → Keyboard → Application Shortcuts
      Recommend: `Super+D` → `dictate toggle`, `Super+Shift+D` → `dictate cancel`
- [x] Config file reference (all options documented)
- [x] Troubleshooting section (no mic found, BT not switching, xdotool not typing)

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
