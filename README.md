# Dictation Tool

Offline push-to-talk speech-to-text for Linux (X11/XFCE). Press a key, speak,
press again — transcribed text is typed wherever your cursor is. No cloud,
no internet required.

**Stack:** `faster-whisper` · `sounddevice` · `xdotool` · PipeWire · Python 3.11+

---

## Requirements

| Package | Purpose |
|---|---|
| `python3` (≥ 3.11) | Runtime |
| `xdotool` | Typing text into the focused window |
| `xclip` | Clipboard injection fallback |
| `libnotify` / `notify-send` | Desktop notifications |
| PipeWire + `pactl` | Audio device selection & BT profile switching |

Install system packages (Debian/Ubuntu/Mint):

```bash
sudo apt install xdotool xclip libnotify-bin pipewire-audio-client-libraries
```

---

## Installation

```bash
git clone https://github.com/youruser/dictation_tool.git ~/Source/dictation_tool
cd ~/Source/dictation_tool
bash install.sh
```

`install.sh` will:
1. Create a virtualenv at `~/.local/share/dictation_tool/venv`
2. Install Python dependencies (`faster-whisper`, `sounddevice`, `numpy`)
3. Write the `dictate` wrapper script to `~/.local/bin/dictate`
4. Create `~/.config/dictation_tool/` and write a default `config.toml`

### Add `~/.local/bin` to your PATH

If it isn't there already, add this to `~/.bashrc` or `~/.zshrc`:

```bash
export PATH="${HOME}/.local/bin:${PATH}"
```

Then reload your shell:

```bash
source ~/.bashrc   # or source ~/.zshrc
```

Verify:

```bash
dictate status   # should print "not-running"
```

---

## Usage

The daemon starts automatically on first use. You don't need to start it manually.

| Command | Effect |
|---|---|
| `dictate toggle` | Start recording (if idle) **or** stop and transcribe (if recording) |
| `dictate start` | Start recording |
| `dictate stop` | Stop recording, transcribe, type result |
| `dictate cancel` | Abort recording without typing anything |
| `dictate status` | Print current state (`idle` / `recording` / `transcribing` / `not-running`) |
| `dictate daemon --no-fork` | Run daemon in the foreground (useful for debugging) |

### Typical workflow

1. Press your shortcut → "🎤 Listening…" notification appears
2. Speak your text
3. Press the shortcut again → "⏳ Transcribing…" → "✅ Done — N words"
4. Text is typed at wherever the cursor was when you pressed stop

---

## XFCE Keyboard Shortcuts

Open **Settings → Keyboard → Application Shortcuts**, click **Add**, and enter:

| Shortcut | Command | Purpose |
|---|---|---|
| `Super+D` | `dictate toggle` | Start/stop dictation |
| `Super+Shift+D` | `dictate cancel` | Cancel without typing |

The `toggle` command starts the daemon automatically on first press, so no
daemon pre-start is needed.

---

## Configuration

Config file: `~/.config/dictation_tool/config.toml`

Default contents written by `install.sh`:

```toml
model = "base"
language = "en"
vad_filter = true
auto_switch_bt = true
injection_method = "xdotool"
```

### All options

| Key | Type | Default | Description |
|---|---|---|---|
| `model` | string | `"base"` | Whisper model size. Options: `tiny`, `base`, `small`, `medium`, `large-v3`. Larger = more accurate but slower to load and transcribe. |
| `language` | string | `"en"` | BCP-47 language code for transcription, e.g. `"en"`, `"fi"`, `"de"`. Set to `"auto"` to let Whisper detect the language automatically (slower). |
| `vad_filter` | bool | `true` | Enable Voice Activity Detection. Filters out silence, improving accuracy and speed. Disable if words at the start of sentences are being clipped. |
| `auto_switch_bt` | bool | `true` | Automatically switch a Bluetooth headset from A2DP (stereo audio) to HFP/HSP (headset mic) on recording start, then restore A2DP on stop. Set to `false` to disable. |
| `injection_method` | string | `"xdotool"` | How transcribed text is typed. `"xdotool"` uses `xdotool type`; `"clipboard"` copies text to clipboard then pastes via `Ctrl+V`. |
| `device` | string | *(auto)* | Override the audio input device. Use the PipeWire source name from `pactl list sources short` (e.g. `"alsa_input.usb-..."`) or a sounddevice index. Omit to auto-detect. |

### Model size guide

| Model | Disk | VRAM* | Speed | Quality |
|---|---|---|---|---|
| `tiny` | ~75 MB | ~1 GB | Very fast | Basic |
| `base` | ~145 MB | ~1 GB | Fast | Good (default) |
| `small` | ~465 MB | ~2 GB | Moderate | Better |
| `medium` | ~1.5 GB | ~5 GB | Slow | Great |
| `large-v3` | ~3 GB | ~10 GB | Slowest | Best |

\* Run on CPU (`int8`) by default; VRAM figures are for reference only.

---

## Troubleshooting

### No microphone found / recording is silent

1. Check available sources:
   ```bash
   pactl list sources short
   ```
2. Set `device` in `config.toml` to the exact source name from that list.
3. Make sure the source is not muted:
   ```bash
   pactl set-source-mute @DEFAULT_SOURCE@ 0
   ```

### Bluetooth headset not switching to mic mode

1. Verify the headset appears in `pactl list cards` with both an A2DP and an
   HFP/HSP profile listed.
2. Check that `auto_switch_bt = true` in `config.toml`.
3. Some headsets require the HFP profile to be activated once manually via
   your desktop's Bluetooth settings before automatic switching works.
4. If you don't need auto-switching, set `auto_switch_bt = false` to silence
   the warning.

### `xdotool` not typing in some applications

Some applications (Electron apps, certain browsers, games) block synthetic
key events from `xdotool type`. Switch to the clipboard method:

```toml
injection_method = "clipboard"
```

This copies text to the X clipboard and pastes it with `Ctrl+V`. Make sure
`xclip` is installed (`sudo apt install xclip`).

### Daemon not starting / socket not created

Check for a stale PID file:

```bash
cat /tmp/dictation_tool.pid
ps aux | grep dictate
```

If the PID doesn't correspond to a running process, delete the file manually:

```bash
rm -f /tmp/dictation_tool.pid /tmp/dictation_tool.sock
```

Then try again. The daemon also prints to stdout when run with `--no-fork`:

```bash
dictate daemon --no-fork
```

### Text typed in the wrong window

The text is injected into whichever window has focus at the moment transcription
finishes (after you press stop, not before). To avoid this, keep focus on your
target window until the "✅ Done" notification appears.

### First dictation is slow

The Whisper model is loaded in a background thread when the daemon first starts.
If you trigger dictation before the model finishes loading, the transcription
step will block until it is ready. Subsequent dictations are fast because the
model stays loaded in memory.

To pre-warm the daemon at login, add this to your XFCE autostart
(**Settings → Session and Startup → Application Autostart → Add**):

```
Name:    Dictation daemon
Command: dictate daemon
```

---

## File layout

```
~/Source/dictation_tool/
  dictate.py                  main script (daemon + CLI)
  requirements.txt
  install.sh
  README.md

~/.local/bin/dictate          wrapper (activates venv, runs dictate.py)
~/.local/share/dictation_tool/venv/    virtualenv
~/.config/dictation_tool/config.toml  user config
/tmp/dictation_tool.sock      Unix socket (CLI ↔ daemon)
/tmp/dictation_tool.pid       PID file
```
