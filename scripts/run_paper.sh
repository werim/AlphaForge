#!/usr/bin/env bash
set -euo pipefail

export ALPHAFORGE_MODE="PAPER"

if [[ "${1:-}" == "--safe-scanner" ]]; then
  export ALPHAFORGE_RUNTIME_SAFE_SCANNER="1"
fi

python -m alphaforge.runtime
