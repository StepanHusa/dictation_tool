#!/usr/bin/env bash
# install.sh — set up the dictation_tool environment
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${HOME}/.local/share/dictation_tool/venv"
BIN_DIR="${HOME}/.local/bin"
CONFIG_DIR="${HOME}/.config/dictation_tool"
WRAPPER="${BIN_DIR}/dictate"

echo "==> Creating virtual environment at ${VENV_DIR}"
python3 -m venv "${VENV_DIR}"

echo "==> Installing Python dependencies"
"${VENV_DIR}/bin/pip" install --upgrade pip --quiet
"${VENV_DIR}/bin/pip" install -r "${SCRIPT_DIR}/requirements.txt"

echo "==> Creating config directory at ${CONFIG_DIR}"
mkdir -p "${CONFIG_DIR}"

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
