#!/usr/bin/env bash
# install.sh — set up the dictation_tool environment
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${HOME}/.local/share/dictation_tool/venv"
BIN_DIR="${HOME}/.local/bin"
CONFIG_DIR="${HOME}/.config/dictation_tool"
WRAPPER="${BIN_DIR}/dictate"

# Prefer /usr/bin/python3 to avoid conda/conda-forge libstdc++ interference
# with system libraries (libportaudio → libjack → libstdc++)
if [ -x "/usr/bin/python3" ]; then
    PYTHON="/usr/bin/python3"
else
    PYTHON="python3"
fi
echo "==> Using Python: ${PYTHON} ($(${PYTHON} --version))"

echo "==> Creating virtual environment at ${VENV_DIR}"
"${PYTHON}" -m venv "${VENV_DIR}"

echo "==> Installing Python dependencies"
"${VENV_DIR}/bin/pip" install --upgrade pip --quiet
"${VENV_DIR}/bin/pip" install -r "${SCRIPT_DIR}/requirements.txt"

echo "==> Creating config directory at ${CONFIG_DIR}"
mkdir -p "${CONFIG_DIR}"

CONFIG_FILE="${CONFIG_DIR}/config.toml"
if [ ! -f "${CONFIG_FILE}" ]; then
    echo "==> Writing default config at ${CONFIG_FILE}"
    cat > "${CONFIG_FILE}" <<'TOML'
model = "base"
language = "en"
vad_filter = true
auto_switch_bt = true
injection_method = "xdotool"
alsa_fallback = true
# alsa_device = "plughw:0,0"  # uncomment to override; plughw:0,0 bypasses PipeWire entirely
TOML
else
    echo "==> Config already exists at ${CONFIG_FILE}, skipping"
fi

echo "==> Writing wrapper script at ${WRAPPER}"
mkdir -p "${BIN_DIR}"
cat > "${WRAPPER}" <<EOF
#!/usr/bin/env bash
exec "${VENV_DIR}/bin/python3" "${SCRIPT_DIR}/dictate.py" "\$@"
EOF
chmod +x "${WRAPPER}"

echo ""
echo "Installation complete."
echo ""
echo "Make sure ${BIN_DIR} is on your PATH, e.g. add to ~/.bashrc or ~/.zshrc:"
echo "  export PATH=\"\${HOME}/.local/bin:\${PATH}\""
echo ""
echo "Then run:  dictate daemon &   # start the daemon"
echo "           dictate toggle     # start/stop dictation"
