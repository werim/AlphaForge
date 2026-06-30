#!/usr/bin/env bash
set -euo pipefail

PORT="${1:-8000}"
HOST="${2:-127.0.0.1}"

python -m uvicorn alphaforge.dashboard.app:create_app --factory --host "$HOST" --port "$PORT"
