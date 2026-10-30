#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${REPO_DIR}/.venv"
LOG_DIR="${REPO_DIR}/logs"
STATE_DIR="${ALPHAFORGE_TELEGRAM_CONTROL_STATE_DIR:-${HOME}/.alphaforge/telegram-control}"
PLIST_PATH="${HOME}/Library/LaunchAgents/com.alphaforge.telegram-control.plist"
LABEL="com.alphaforge.telegram-control"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SUMMARY_INTERVAL_SECONDS="${ALPHAFORGE_TELEGRAM_SUMMARY_INTERVAL_SECONDS:-43200}"

if [[ "${OSTYPE:-}" != darwin* ]]; then
  echo "This installer is intended for macOS (launchd)." >&2
  exit 1
fi

mkdir -p "${LOG_DIR}" "${STATE_DIR}" "${HOME}/Library/LaunchAgents"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

"${VENV_DIR}/bin/python" -m pip install -e "${REPO_DIR}[dev]"

cat > "${PLIST_PATH}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${VENV_DIR}/bin/python</string>
    <string>-m</string>
    <string>alphaforge.remote_control.telegram_service</string>
    <string>--state-dir</string>
    <string>${STATE_DIR}</string>
    <string>--summary-interval-seconds</string>
    <string>${SUMMARY_INTERVAL_SECONDS}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${REPO_DIR}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/telegram-control.out.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/telegram-control.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)" "${PLIST_PATH}" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "${PLIST_PATH}"
launchctl kickstart -k "gui/$(id -u)/${LABEL}"

echo "AlphaForge Telegram Control Center installed."
echo "Service: ${LABEL}"
echo "State: ${STATE_DIR}"
echo "Logs: ${LOG_DIR}/telegram-control.out.log and ${LOG_DIR}/telegram-control.err.log"
echo "Secrets are not written to the plist; the service loads AlphaForge's protected local environment."
echo "Runtime is not started or modified by this installer."
