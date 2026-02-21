#!/usr/bin/env bash
# uninstall.sh — remove the dictation_tool installation
set -euo pipefail

VENV_DIR="${HOME}/.local/share/dictation_tool/venv"
DATA_DIR="${HOME}/.local/share/dictation_tool"
BIN_DIR="${HOME}/.local/bin"
CONFIG_DIR="${HOME}/.config/dictation_tool"
WRAPPER="${BIN_DIR}/dictate"
PID_FILE="/tmp/dictation_tool.pid"
SOCK_FILE="/tmp/dictation_tool.sock"

# Stop the running daemon if any
if [ -f "${PID_FILE}" ]; then
    PID="$(cat "${PID_FILE}")"
    if kill -0 "${PID}" 2>/dev/null; then
        echo "==> Stopping daemon (pid=${PID})"
        kill "${PID}"
        sleep 1
    fi
fi
rm -f "${PID_FILE}" "${SOCK_FILE}"

# Remove the venv / data dir
if [ -d "${DATA_DIR}" ]; then
    echo "==> Removing ${DATA_DIR}"
    rm -rf "${DATA_DIR}"
fi

# Remove the wrapper script
if [ -f "${WRAPPER}" ]; then
    echo "==> Removing ${WRAPPER}"
    rm -f "${WRAPPER}"
fi

# Remove config (ask first)
if [ -d "${CONFIG_DIR}" ]; then
    read -r -p "==> Remove config at ${CONFIG_DIR}? [y/N] " REPLY
    if [[ "${REPLY}" =~ ^[Yy]$ ]]; then
        rm -rf "${CONFIG_DIR}"
        echo "    Config removed."
    else
        echo "    Config kept."
    fi
fi

echo ""
echo "Uninstall complete."
