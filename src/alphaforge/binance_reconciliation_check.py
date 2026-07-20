"""Operator-only, read-only Binance reconciliation evidence command."""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from alphaforge.binance_reconciliation_provider import BinanceReadonlyReconciliationConfig, BinanceReadonlyReconciliationProvider
from alphaforge.config import load_config_from_env

SAFE_POSITION_FIELDS = ("symbol", "positionAmt", "positionSide", "entryPrice", "unRealizedProfit")


def sanitize_position_risk(source: Path, destination: Path) -> None:
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("positionRisk input must be a JSON array")
    safe = [{key: row.get(key) for key in SAFE_POSITION_FIELDS} for row in payload if isinstance(row, dict)]
    destination.write_text(json.dumps(safe, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run() -> dict[str, object]:
    cfg = load_config_from_env()
    provider = BinanceReadonlyReconciliationProvider(
        config=BinanceReadonlyReconciliationConfig(
            base_url=cfg.binance.base_url,
            api_key=os.getenv("BINANCE_API_KEY", "").strip(), api_secret=os.getenv("BINANCE_API_SECRET", "").strip(),
            recv_window_ms=cfg.binance.recv_window_ms, request_timeout_sec=cfg.runtime.reconciliation_timeout_sec,
            trade_lookback_ms=cfg.runtime.binance_reconciliation_trade_lookback_ms,
            position_epsilon=Decimal(cfg.runtime.reconciliation_position_epsilon),
            max_fill_symbols=cfg.runtime.reconciliation_max_fill_symbols,
        )
    )
    snapshot = dict(provider.snapshot())
    positions = snapshot.get("positions", [])
    host = urlsplit(cfg.binance.base_url).hostname
    coverage = snapshot.get("coverage", {})
    return {
        "environment": cfg.binance.environment, "safe_base_host": host,
        "endpoint_statuses": {"positionRisk": "PASS" if coverage.get("positionRisk") else "FAIL",
                              "openOrders": "PASS" if coverage.get("openOrders") else "FAIL",
                              "userTrades": "PASS" if len(coverage.get("userTrades", [])) == snapshot.get("selected_count") else "FAIL"},
        "position_row_count": len(positions),
        "exact_zero_position_count": sum(1 for p in positions if p.get("qty_exact") == "0" or Decimal(str(p.get("qty_exact"))) == 0),
        "non_zero_position_count": sum(1 for p in positions if Decimal(str(p.get("qty_exact"))) != 0),
        "epsilon_filtered_count": sum(1 for p in positions if p.get("epsilon_filtered")),
        "active_position_count": sum(1 for p in positions if p.get("active")),
        "open_order_count": len(snapshot.get("orders", [])), "selected_fill_symbols": snapshot.get("selected_symbols", []),
        "symbol_sources": snapshot.get("symbol_sources", {}), "request_count": snapshot.get("request_count", 0),
        "request_evidence": snapshot.get("request_evidence", []), "evidence_status": snapshot.get("evidence_status"),
        "sanitized_errors": snapshot.get("errors", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sanitize-position-risk", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.sanitize_position_risk:
        if not args.output: parser.error("--output is required with --sanitize-position-risk")
        sanitize_position_risk(args.sanitize_position_risk, args.output)
        return 0
    try:
        result = run()
    except Exception as exc:
        result = {"evidence_status": "INCOMPLETE", "sanitized_errors": [f"{type(exc).__name__}:configuration_or_authentication_failed"]}
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("evidence_status") == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
