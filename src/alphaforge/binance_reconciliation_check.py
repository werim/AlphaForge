"""Opt-in, read-only Binance reconciliation acceptance check.

This command emits only allow-listed evidence. It never prints request URLs,
headers, API credentials, secrets, or signatures.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Sequence
from urllib.parse import urlparse

from alphaforge.binance_reconciliation_provider import (
    BinanceReadonlyReconciliationConfig,
    BinanceReadonlyReconciliationProvider,
)
from alphaforge.config import load_config_from_env


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a signed, read-only Binance reconciliation acceptance check.")
    parser.add_argument("--tracked-symbols", default="", help="Comma-separated campaign/runtime symbols to include in bounded fill scope.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cfg = load_config_from_env()
    key = os.getenv("BINANCE_API_KEY", "").strip()
    secret = os.getenv("BINANCE_API_SECRET", "").strip()
    base_url = cfg.exchange.binance.base_url
    host = urlparse(base_url).hostname or "unknown"
    environment = "DEMO" if "demo" in host.lower() or "testnet" in host.lower() else "PRODUCTION"
    if not key or not secret:
        print(json.dumps({
            "environment": environment,
            "base_host": host,
            "evidence_status": "INCOMPLETE",
            "selected_fill_symbols": [],
            "request_evidence": [],
            "errors": [{"reason": "missing_binance_credentials"}],
        }, indent=2, sort_keys=True))
        return 2

    tracked = {item.strip().upper() for item in args.tracked_symbols.split(",") if item.strip()}
    provider = BinanceReadonlyReconciliationProvider(
        config=BinanceReadonlyReconciliationConfig(
            base_url=base_url,
            api_key=key,
            api_secret=secret,
            recv_window_ms=cfg.runtime.binance_reconciliation_recv_window_ms,
            request_timeout_sec=cfg.runtime.reconciliation_timeout_sec,
            trade_lookback_ms=cfg.runtime.binance_reconciliation_trade_lookback_ms,
            position_epsilon=cfg.runtime.reconciliation_position_epsilon,
            max_fill_symbols=cfg.runtime.reconciliation_max_fill_symbols,
            recent_lifecycle_lookback_ms=cfg.runtime.reconciliation_recent_lifecycle_lookback_ms,
        ),
        tracked_symbols=lambda: tracked,
    )
    try:
        snapshot = provider.snapshot()
    finally:
        provider.close()

    scope = snapshot.get("fill_symbol_evidence") or {}
    errors = snapshot.get("errors") or []
    selected = scope.get("symbols") or next((item.get("selected_symbols", []) for item in errors if isinstance(item, dict)), [])
    output = {
        "environment": environment,
        "base_host": host,
        "evidence_status": snapshot.get("evidence_status", "INCOMPLETE"),
        "selected_fill_symbols": selected,
        "request_evidence": snapshot.get("request_evidence") or [],
        "errors": errors,
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if output["evidence_status"] == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
