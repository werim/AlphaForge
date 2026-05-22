#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${REPO_DIR}/.venv"
LOG_DIR="${REPO_DIR}/logs"
PLIST_PATH="${HOME}/Library/LaunchAgents/com.alphaforge.dashboard.plist"
LABEL="com.alphaforge.dashboard"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PORT="${ALPHAFORGE_DASHBOARD_PORT:-8000}"

if [[ "${OSTYPE:-}" != darwin* ]]; then
  echo "This installer is intended for macOS (launchd)." >&2
  exit 1
fi

mkdir -p "${LOG_DIR}" "${HOME}/Library/LaunchAgents"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

"${VENV_DIR}/bin/python" -m pip install --upgrade pip
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
    <string>${VENV_DIR}/bin/uvicorn</string>
    <string>alphaforge.dashboard.app:create_app</string>
    <string>--factory</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>${PORT}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${REPO_DIR}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/dashboard.out.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/dashboard.err.log</string>
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

echo "AlphaForge Dashboard installed and started on http://127.0.0.1:${PORT}"
echo "Service: ${LABEL}"
echo "Logs: ${LOG_DIR}/dashboard.out.log and ${LOG_DIR}/dashboard.err.log"
echo "Runtime remains a separate process and was not started by this installer."
