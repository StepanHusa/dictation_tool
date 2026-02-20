"Implement Linux Dictation Tool"

**Requirements and tasks:** See TASKS.md — implement everything listed there top to bottom.

**Key technical constraints:**
- Python daemon + CLI (`dictate.py <toggle|start|stop|cancel|status|daemon>`)
- `faster-whisper` for offline transcription, `sounddevice` + numpy for recording
- `xdotool type` for text injection (user is on X11/XFCE)
- `notify-send` for all status notifications
- `pactl` for Bluetooth profile switching (A2DP ↔ HFP/HSP)
- Daemon stays alive between uses; CLI talks to it via Unix socket
- `install.sh` + venv; wrapper at `~/.local/bin/dictate`

**Success criteria:** All items in TASKS.md completed and checked off; `python3 -m py_compile dictate.py` passes.

Output <promise>COMPLETE</promise> when done --max-iterations 40 --completion-promise "COMPLETE"
