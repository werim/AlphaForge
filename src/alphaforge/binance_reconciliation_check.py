"""Operator-only, read-only Binance reconciliation evidence command."""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
from urllib.parse import urlsplit

from alphaforge.binance_reconciliation_provider import BinanceReadonlyReconciliationConfig, BinanceReadonlyReconciliationProvider, normalize_reconciliation_symbol
from alphaforge.burnin_ops import parse_symbols
from alphaforge.config import load_reconciliation_settings
from alphaforge.config_check import audit_settings, _safe_error
from alphaforge.env_contract import bootstrap_environment

SAFE_POSITION_FIELDS = ("symbol", "positionAmt", "positionSide", "entryPrice", "unRealizedProfit")


def sanitize_position_risk(source: Path, destination: Path) -> None:
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("positionRisk input must be a JSON array")
    safe = [{key: row.get(key) for key in SAFE_POSITION_FIELDS} for row in payload if isinstance(row, dict)]
    destination.write_text(json.dumps(safe, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run(*, symbols: list[str] | None = None, write_sanitized_position_risk: Path | None = None) -> dict[str, object]:
    global_audit = audit_settings()
    cfg = load_reconciliation_settings()
    requested_symbols = list(symbols or [])
    tracked_symbols = list(dict.fromkeys(normalize_reconciliation_symbol(symbol, "tracked") for symbol in requested_symbols))
    if not cfg.api_key:
        raise RuntimeError("missing_binance_api_key")
    if not cfg.api_secret:
        raise RuntimeError("missing_binance_api_secret")
    provider = BinanceReadonlyReconciliationProvider(
        config=BinanceReadonlyReconciliationConfig(
            base_url=cfg.base_url, api_key=cfg.api_key, api_secret=cfg.api_secret,
            recv_window_ms=cfg.recv_window_ms, request_timeout_sec=cfg.timeout_sec,
            trade_lookback_ms=cfg.trade_lookback_ms, position_epsilon=Decimal(cfg.position_epsilon),
            max_fill_symbols=cfg.max_fill_symbols,
        ), tracked_symbols=lambda: set(tracked_symbols)
    )
    snapshot = dict(provider.snapshot())
    positions = snapshot.get("positions", [])
    if write_sanitized_position_risk is not None:
        write_sanitized_position_risk.parent.mkdir(parents=True, exist_ok=True)
        safe_positions = [{key: row.get(key) for key in ("symbol", "qty_exact", "position_side", "entry_price", "unrealized_pnl", "symbol_valid", "exact_zero", "epsilon_filtered", "active")} for row in positions]
        write_sanitized_position_risk.write_text(json.dumps(safe_positions, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    host = urlsplit(cfg.base_url).hostname
    coverage = snapshot.get("coverage", {})
    return {
        "environment": cfg.environment, "safe_base_host": host,
        "reconciliation_config_status": "PASS", "global_config_status": global_audit["status"],
        "reconciliation_config_sources": dict(cfg.sources),
        "global_config_errors": global_audit["errors"],
        "requested_symbols": requested_symbols, "tracked_symbols": tracked_symbols,
        "tracked_scope_source": "CLI" if tracked_symbols else "NONE",
        "campaign_scope_validated": bool(tracked_symbols),
        "endpoint_statuses": {"positionRisk": "PASS" if coverage.get("positionRisk") else "FAIL",
                              "openOrders": "PASS" if coverage.get("openOrders") else "FAIL",
                              "userTrades": "PASS" if len(coverage.get("userTrades", [])) == snapshot.get("selected_count") else "FAIL"},
        "position_row_count": len(positions),
        "exact_zero_position_count": sum(1 for p in positions if p.get("qty_exact") == "0" or Decimal(str(p.get("qty_exact"))) == 0),
        "non_zero_position_count": sum(1 for p in positions if Decimal(str(p.get("qty_exact"))) != 0),
        "epsilon_filtered_position_count": sum(1 for p in positions if p.get("epsilon_filtered")),
        "active_position_count": sum(1 for p in positions if p.get("active")),
        "invalid_zero_exposure_symbol_count": sum(1 for p in positions if not p.get("symbol_valid") and p.get("exact_zero")),
        "invalid_nonzero_symbol_count": sum(1 for p in positions if not p.get("symbol_valid") and not p.get("exact_zero")),
        "invalid_symbols": [p.get("symbol") for p in positions if not p.get("symbol_valid")],
        "open_order_count": len(snapshot.get("orders", [])), "selected_fill_symbols": snapshot.get("selected_symbols", []),
        "symbol_sources": snapshot.get("symbol_sources", {}),
        "http_request_count": snapshot.get("http_request_count", 0),
        "request_attempts": snapshot.get("request_attempts", []),
        "endpoint_results": snapshot.get("endpoint_results", []), "evidence_status": snapshot.get("evidence_status"),
        "failed_endpoint": snapshot.get("failed_endpoint"), "failed_symbol": snapshot.get("failed_symbol"),
        "unknown_unreconciled_symbols": snapshot.get("unknown_unreconciled_symbols", []),
        "position_warnings": snapshot.get("position_warnings", []),
        "sanitized_errors": snapshot.get("errors", []),
    }


class _UsageError(ValueError):
    pass


class _SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


def _failure(reason: str, *, stage: str, setting: str | None = None, exc_type: str = "ValueError") -> dict[str, object]:
    error = {"type": exc_type, "reason": reason}
    if setting:
        error["setting"] = setting
    return {"evidence_status": "INCOMPLETE", "failed_stage": stage, "sanitized_errors": [error]}


def main() -> int:
    parser = _SafeParser()
    parser.add_argument("--sanitize-position-risk", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--write-sanitized-position-risk", type=Path)
    parser.add_argument("--symbols", nargs="+")
    try:
        args = parser.parse_args()
    except _UsageError:
        print(json.dumps(_failure("invalid_setting_value", stage="CLI", setting="--symbols"), sort_keys=True))
        return 4
    bootstrap_environment()
    if args.sanitize_position_risk:
        if not args.output:
            print(json.dumps(_failure("missing_required_setting", stage="CLI", setting="--output"), sort_keys=True))
            return 4
        sanitize_position_risk(args.sanitize_position_risk, args.output)
        return 0
    try:
        resolved_symbols = parse_symbols(args.symbols) if args.symbols is not None else []
        if args.symbols is not None and not resolved_symbols:
            print(json.dumps(_failure("missing_required_setting", stage="CLI", setting="--symbols"), sort_keys=True))
            return 4
        for symbol in resolved_symbols:
            normalize_reconciliation_symbol(symbol, "tracked")
        result = run(symbols=resolved_symbols, write_sanitized_position_risk=args.write_sanitized_position_risk)
    except Exception as exc:
        if "invalid_symbol" in str(exc):
            result = _failure("invalid_symbol", stage="CLI", setting="--symbols", exc_type=type(exc).__name__)
            print(json.dumps(result, sort_keys=True)); return 4
        if not isinstance(exc, RuntimeError):
            message = str(exc)
            if "BINANCE_ENVIRONMENT" in message or "Binance REST" in message or "Binance websocket" in message:
                result = _failure("environment_resolution_failed", stage="CONFIGURATION", setting="BINANCE_ENVIRONMENT", exc_type=type(exc).__name__)
                print(json.dumps(result, sort_keys=True)); return 2
            setting = next((name for name in ("ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC", "ALPHAFORGE_BINANCE_RECV_WINDOW_MS",
                                              "ALPHAFORGE_BINANCE_RECONCILIATION_TRADE_LOOKBACK_MS", "ALPHAFORGE_RECONCILIATION_POSITION_EPSILON",
                                              "ALPHAFORGE_RECONCILIATION_MAX_FILL_SYMBOLS") if name in message), "RECONCILIATION_CONFIGURATION")
            result = {"evidence_status": "INCOMPLETE", "failed_stage": "CONFIGURATION",
                      "sanitized_errors": [_safe_error(setting, exc)]}
            print(json.dumps(result, sort_keys=True)); return 2
        reason = str(exc) if str(exc) in {"missing_binance_api_key", "missing_binance_api_secret"} else "provider_initialization_failed"
        result = _failure(reason, stage="AUTHENTICATION", exc_type=type(exc).__name__)
        print(json.dumps(result, sort_keys=True)); return 3
    print(json.dumps(result, sort_keys=True))
    if result.get("evidence_status") != "COMPLETE":
        errors = str(result.get("sanitized_errors", ""))
        return 3 if "ReconciliationAuthError" in errors else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
