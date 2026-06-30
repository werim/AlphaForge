#!/usr/bin/env bash
set -euo pipefail

INTERVAL="${1:-1h}"
DAYS="${2:-30}"
SYMBOLS="${3:-BTCUSDT,ETHUSDT}"
OUTPUT_DIR="${4:-data/backtests/manual}"

python backtest_order.py \
  --interval "$INTERVAL" \
  --last-n-days "$DAYS" \
  --symbols "$SYMBOLS" \
  --output-dir "$OUTPUT_DIR"
