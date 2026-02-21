# Dictation Tool

> Offline push-to-talk speech-to-text for Linux. Press a key, speak, press
> again — transcribed text is typed wherever your cursor is.

![Platform](https://img.shields.io/badge/platform-Linux%20X11-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

No cloud. No internet. No subscription. Powered by
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) running locally
on your CPU.

---

## Features

- **Fire-and-forget** — bind a key, speak, text appears
- **Floating review window** (`dictate app`) — see and edit the transcript before inserting
- **Bluetooth headset support** — auto-switches from A2DP to HFP/HSP during recording
- **Custom vocabulary** — teach Whisper to spell your names and technical terms correctly
- **Training data collection** — save audio + transcripts for later fine-tuning

---

## Requirements

| Package | Purpose |
|---|---|
| `python3` (≥ 3.11) | Runtime |
| `xdotool` | Typing text into the focused window |
| `xclip` | Clipboard injection fallback |
| `libnotify` / `notify-send` | Desktop notifications |
| PipeWire + `pactl` | Audio device selection & BT profile switching |
| `tkinter` | Floating window mode — included with system Python |

Install system packages (Debian/Ubuntu/Mint):

```bash
sudo apt install xdotool xclip libnotify-bin pipewire-audio-client-libraries
```

---

## Installation

```bash
git clone https://github.com/StepanHusa/dictation_tool.git
cd dictation_tool
bash install.sh
```

`install.sh` will:

1. Create a virtualenv at `~/.local/share/dictation_tool/venv`
2. Install Python dependencies (`faster-whisper`, `sounddevice`, `numpy`)
3. Write the `dictate` wrapper script to `~/.local/bin/dictate`
4. Create `~/.config/dictation_tool/` and write a default `config.toml`

### Add `~/.local/bin` to your PATH

```bash
echo 'export PATH="${HOME}/.local/bin:${PATH}"' >> ~/.bashrc
source ~/.bashrc
```

Verify the install:

```bash
dictate status   # should print "not-running"
```

### Uninstall

```bash
bash uninstall.sh
```

---

## Usage

The daemon starts automatically on first use.

| Command | Effect |
|---|---|
| `dictate toggle` | Start recording (if idle) or stop and transcribe (if recording) |
| `dictate start` | Start recording |
| `dictate stop` | Stop recording and transcribe |
| `dictate cancel` | Abort recording without typing anything |
| `dictate status` | Print current state (`idle` / `recording` / `transcribing` / `not-running`) |
| `dictate app` | Open floating window for guided dictation |
| `dictate daemon --no-fork` | Run daemon in the foreground (useful for debugging) |

---

## Floating Window Mode (`dictate app`)

`dictate app` opens a small always-on-top window that stays visible during the
whole session. It captures which window was focused beforehand and restores
focus before injecting text, so the result always lands in the right place.

```
Phase 1 — Listening
┌─────────────────────────────────────┐
│  🎤 Listening…                      │
│                                     │
│  Enter = stop   Esc = cancel        │
└─────────────────────────────────────┘

Phase 2 — Transcribing (auto-advances)
┌─────────────────────────────────────┐
│  ⏳ Transcribing…                   │
└─────────────────────────────────────┘

Phase 3 — Review
┌─────────────────────────────────────┐
│  the transcribed text here          │
│                                     │
│  Enter=insert  Shift+Enter=insert+save  Space=edit  Esc=drop │
└─────────────────────────────────────┘

Phase 4 — Edit
┌─────────────────────────────────────┐
│  [editable text field]              │
│                                     │
│  Enter=confirm+save  Esc=cancel  Ctrl+W=save word │
└─────────────────────────────────────┘
```

### Key bindings

**Review phase:**

| Key | Effect |
|---|---|
| `Enter` | Insert immediately (fast path — no training data saved) |
| `Shift+Enter` | Insert and save as a verified correct training example |
| `Space` | Open edit mode to correct the text before inserting |
| `Esc` | Drop — nothing is inserted |

**Edit phase:**

| Key | Effect |
|---|---|
| `Enter` | Confirm, insert, and save as a training example |
| `Esc` | Cancel — nothing is inserted |
| `Ctrl+W` | Save the selected text to the vocabulary list |

---

## Custom Vocabulary

Whisper sometimes misspells proper nouns, technical terms, or names. You can
bias it toward correct spellings by maintaining a vocabulary list.

**File:** `~/.config/dictation_tool/vocabulary.txt` — one word or phrase per line:

```
Whisper
faster-whisper
xdotool
```

This list is passed to Whisper as an `initial_prompt` on every transcription.
It improves spelling accuracy for the listed words — it is not voice training.

### Adding words from the edit window

1. Run `dictate app` and speak something containing a misspelled word
2. Press `Space` to enter edit mode
3. Select the correct spelling with the mouse or `Shift`+arrows
4. Press `Ctrl+W` — the hint briefly confirms `Saved 'word'`

The word is appended to `vocabulary.txt`. You can also edit the file directly
in any text editor.

---

## Training Data Collection

`dictate app` can save audio + transcript pairs for later fine-tuning the
Whisper model on your voice and vocabulary.

### When data is saved

| Action | Saved? | `was_edited` |
|---|---|---|
| Review → `Enter` (fast insert) | No | — |
| Review → `Shift+Enter` (verified correct) | Yes | `false` |
| Edit → `Enter` (corrected) | Yes | `true` |

Use plain `Enter` when you just want the text and don't care about data quality.
Use `Shift+Enter` or edit+confirm when you have verified the result is correct.

### File format

Saved to `~/.local/share/dictation_tool/training/`. Each sample is a pair:

```
2026-02-21T20-54-42.wav    16-bit mono 16 kHz audio
2026-02-21T20-54-42.json   metadata
```

Example JSON:

```json
{
  "timestamp": "2026-02-21T20-54-42",
  "audio_file": "2026-02-21T20-54-42.wav",
  "audio_saved": true,
  "audio_error": null,
  "duration_s": 4.352,
  "sample_rate": 16000,
  "transcript": "what whisker originally produced",
  "edited": "what I corrected it to",
  "was_edited": true,
  "model": "base",
  "language": "en",
  "initial_prompt": "Whisper, faster-whisper"
}
```

For fine-tuning, pair the WAV with: `edited` when `was_edited` is `true`,
`transcript` otherwise.

---

## XFCE Keyboard Shortcuts

Open **Settings → Keyboard → Application Shortcuts → Add**:

| Shortcut | Command | Purpose |
|---|---|---|
| `Super+D` | `dictate toggle` | Start/stop dictation |
| `Super+Shift+D` | `dictate cancel` | Cancel without typing |
| `Super+A` | `dictate app` | Guided dictation with review window |

---

## Configuration

**File:** `~/.config/dictation_tool/config.toml`

```toml
model = "base"
language = "en"
vad_filter = true
auto_switch_bt = true
injection_method = "xdotool"
```

### Options

| Key | Type | Default | Description |
|---|---|---|---|
| `model` | string | `"base"` | Whisper model size: `tiny`, `base`, `small`, `medium`, `large-v3` |
| `language` | string | `"en"` | BCP-47 language code, e.g. `"fi"`, `"de"`. Use `"auto"` to detect (slower). |
| `vad_filter` | bool | `true` | Voice Activity Detection. Filters silence. Disable if words at sentence starts are clipped. |
| `auto_switch_bt` | bool | `true` | Auto-switch Bluetooth headset from A2DP → HFP during recording, then restore. |
| `injection_method` | string | `"xdotool"` | `"xdotool"` types directly; `"clipboard"` copies + pastes with `Ctrl+V`. |
| `device` | string | *(auto)* | Force a specific audio input. Use the source name from `pactl list sources short`. |

### Model size guide

| Model | Disk | Speed | Quality |
|---|---|---|---|
| `tiny` | ~75 MB | Very fast | Basic |
| `base` | ~145 MB | Fast | Good (default) |
| `small` | ~465 MB | Moderate | Better |
| `medium` | ~1.5 GB | Slow | Great |
| `large-v3` | ~3 GB | Slowest | Best |

All models run on CPU (`int8`) by default.

---

## Troubleshooting

### No microphone / silent recording

```bash
pactl list sources short          # list available sources
pactl set-source-mute @DEFAULT_SOURCE@ 0   # unmute default
```

Set `device` in `config.toml` to the exact source name if auto-detection fails.

### Bluetooth headset not switching to mic mode

1. Verify `pactl list cards` shows both A2DP and HFP/HSP profiles for the headset.
2. Some headsets need to be switched manually once before automatic switching works.
3. Set `auto_switch_bt = false` to disable the feature entirely.

### `xdotool` not typing in some applications

Electron apps and some browsers block synthetic key events. Use clipboard mode:

```toml
injection_method = "clipboard"
```

Requires `xclip` (`sudo apt install xclip`).

### `dictate app` shows "(empty transcript)"

The daemon is running old code. Restart it:

```bash
kill $(cat /tmp/dictation_tool.pid) && rm -f /tmp/dictation_tool.pid /tmp/dictation_tool.sock
```

The next `dictate app` call starts a fresh daemon automatically. This is only
needed after updating `dictate.py`.

### Text typed in the wrong window

In toggle mode, text is injected into whichever window has focus when
transcription finishes. Use `dictate app` instead — it captures the original
window and restores focus automatically.

### First dictation is slow

The Whisper model loads in a background thread when the daemon starts. The
first transcription waits for it; all subsequent ones are fast.

Pre-warm at login via **Settings → Session and Startup → Application Autostart**:

```
Name:    Dictation daemon
Command: dictate daemon
```

---

## File layout

```
~/.local/bin/dictate                      wrapper script
~/.local/share/dictation_tool/
  venv/                                   Python virtualenv
  training/                               WAV + JSON training samples
~/.config/dictation_tool/
  config.toml                             user configuration
  vocabulary.txt                          transcription vocabulary bias
~/.local/state/dictation_tool/
  dictation_tool.log                      rotating daemon log
/tmp/dictation_tool.sock                  Unix socket (CLI ↔ daemon)
/tmp/dictation_tool.pid                   daemon PID file
```

---

## Contributing

Contributions are welcome. Please open an issue before submitting a large pull
request so we can discuss the approach first.

```bash
git clone https://github.com/StepanHusa/dictation_tool.git
cd dictation_tool
bash install.sh

# verify no syntax errors after changes
python3 -m py_compile dictate.py
```

---

## License

MIT — see [LICENSE](LICENSE) for details.
