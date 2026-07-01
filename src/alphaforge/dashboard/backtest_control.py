from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from alphaforge.config import load_config_from_env
from alphaforge.config_registry import config_snapshot
from alphaforge.contracts import canonical_utc_timestamp
from alphaforge.historical_market_data import supported_intervals
from alphaforge.symbols import SymbolListError, normalize_symbol_list

SUPPORTED_TIMEFRAMES: tuple[str, ...] = tuple(tf for tf in ("1m", "15m", "1h", "4h", "1d") if tf in supported_intervals())
INSUFFICIENT_BINANCE_DATA_MESSAGE = "Not enough historical data returned by Binance for the requested period. Try fewer days or a higher timeframe."
DASHBOARD_BACKTEST_SUBPROCESS_TIMEOUT_SECONDS = 600.0


def _safe_subprocess_timeout(seconds: float | int | None) -> float:
    """Return a strictly positive subprocess timeout or fail before subprocess.run."""
    if seconds is None:
        raise ValueError("subprocess timeout is required")
    timeout = float(seconds)
    if timeout <= 0:
        raise ValueError(f"subprocess timeout must be positive; got {timeout:g}")
    return timeout


def _timeout_profile_metrics(profile: str, profile_dir: Path, command: list[str], timeout_seconds: float, warnings: list[str] | None = None) -> dict[str, Any]:
    warn = sorted(set(list(warnings or []) + ["PROFILE_TIMEOUT"]))
    return {
        "profile_name": profile,
        "filter_profile": profile,
        "status": "TIMEOUT",
        "enabled_filters": [],
        "disabled_filters": [],
        "hard_safety_gates": [],
        "candidates": None,
        "accepted_trades": None,
        "accepted_trades_source": "UNAVAILABLE_TIMEOUT",
        "lifecycle_event_count": 0,
        "rejected_row_count": 0,
        "rejected_signals": None,
        "reject_rate": None,
        "win_count": None,
        "loss_count": None,
        "open_count": None,
        "timeout_count": None,
        "gross_pnl": None,
        "net_pnl": None,
        "return_pct": None,
        "return": None,
        "max_drawdown": None,
        "max_drawdown_status": "UNAVAILABLE_TIMEOUT",
        "max_consecutive_losses": None,
        "profit_factor": None,
        "avg_win": None,
        "avg_loss": None,
        "expectancy_per_trade": None,
        "avg_trades_per_day": None,
        "accepted_effective_rr_distribution": {},
        "rejected_effective_rr_distribution": {},
        "score_10_count": None,
        "score_10_tp_count": None,
        "score_10_sl_count": None,
        "score_10_timeout_count": None,
        "score_10_net_pnl": None,
        "top_reject_reasons": [],
        "objective_score": {"raw_net_pnl": 0.0, "final_objective_score": -1000000.0, "timeout_penalty": 1000000.0},
        "warnings": warn,
        "bucket_diagnostics": {},
        "artifact_paths": {"directory": str(profile_dir)},
        "failure_reason": "PROFILE_TIMEOUT",
        "timeout_seconds": timeout_seconds,
        "command": command,
    }


def _write_profile_timeout_metadata(profile_dir: Path, profile: str, command: list[str], timeout_seconds: float) -> None:
    _write_backtest_run_metadata(
        profile_dir / "backtest_profile_metadata.json",
        {
            "mode": "BACKTEST",
            "profile_name": profile,
            "status": "TIMEOUT",
            "failure_reason": "PROFILE_TIMEOUT",
            "timeout_seconds": timeout_seconds,
            "command": command,
        },
    )


def _write_backtest_run_metadata(path: Path, metadata: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")


def _classify_backtest_failure(stderr: str) -> tuple[str, str]:
    if "UNSUPPORTED_TIMEFRAME" in stderr:
        return "UNSUPPORTED_TIMEFRAME", stderr[-1200:]
    historical_tokens = ("HistoricalDataError", "Historical coverage", "No candles returned", "Insufficient candles")
    if any(token in stderr for token in historical_tokens):
        return "NOT_ENOUGH_HISTORICAL_DATA", f"{INSUFFICIENT_BINANCE_DATA_MESSAGE} Details: {stderr[-1200:]}"
    return "BACKTEST_PROCESS_FAILED", stderr[-1200:]


@dataclass(slots=True)
class DashboardBacktestRequest:
    last_days: int
    symbols: list[str]
    timeframe: str
    initial_balance: float
    max_symbols: int
    filter_switches: dict[str, bool] = field(default_factory=dict)
    short_breakdown_rescue_enabled: bool = False
    run_profile_comparison: bool = False


@dataclass(slots=True)
class DashboardBacktestResult:
    status: str
    period: str
    symbols: list[str]
    timeframe: str
    initial_balance: float
    max_symbols: int
    output_dir: str | None = None
    summary_path: str | None = None
    lifecycle_path: str | None = None
    rejected_path: str | None = None
    calibration_report_path: str | None = None
    calibration_summary_path: str | None = None
    total_candidates: Any = None
    accepted_trades: Any = None
    rejected_signals: Any = None
    win_count: Any = None
    loss_count: Any = None
    open_count: Any = None
    net_pnl: Any = None
    total_return_pct: Any = None
    max_drawdown: Any = None
    lifecycle_warning: str | None = None
    execution_context_warning: str | None = None
    error_message: str | None = None
    command: list[str] = field(default_factory=list)
    top_rejection_reasons: list[dict[str, Any]] = field(default_factory=list)
    signal_rows_count: int | None = None
    symbol_selector_reject_count: int | None = None
    score_distribution: dict[str, Any] = field(default_factory=dict)
    rr_distribution: dict[str, Any] = field(default_factory=dict)
    effective_rr_distribution: dict[str, Any] = field(default_factory=dict)
    pre_later_gate_pass_count: int | None = None
    lifecycle_state_counts: list[dict[str, Any]] = field(default_factory=list)
    lifecycle_path_counts: list[dict[str, Any]] = field(default_factory=list)
    final_reject_reason_counts: list[dict[str, Any]] = field(default_factory=list)
    order_reject_reason_counts: list[dict[str, Any]] = field(default_factory=list)
    symbol_selector_reject_counts: list[dict[str, Any]] = field(default_factory=list)
    rejection_funnel: dict[str, Any] = field(default_factory=dict)
    later_gate_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    low_score_shadow_comparison: dict[str, Any] = field(default_factory=dict)
    execution_cost_summary: dict[str, Any] = field(default_factory=dict)
    near_miss_rejected_signals: list[dict[str, Any]] = field(default_factory=list)
    accepted_trade_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    accepted_score_distribution: dict[str, Any] = field(default_factory=dict)
    accepted_effective_rr_distribution: dict[str, Any] = field(default_factory=dict)
    near_miss_score_distribution: dict[str, Any] = field(default_factory=dict)
    near_miss_effective_rr_distribution: dict[str, Any] = field(default_factory=dict)
    stop_too_wide_rescue_diagnostics: dict[str, Any] = field(default_factory=dict)
    signal_quality_diagnostics: dict[str, Any] = field(default_factory=dict)
    high_effective_rr_missed_alpha: list[dict[str, Any]] = field(default_factory=list)
    stop_too_wide_quality_split: dict[str, Any] = field(default_factory=dict)
    top_quality_improvement_candidates: list[dict[str, Any]] = field(default_factory=list)
    backtest_rejection_rate: float | None = None
    disabled_filters: list[str] = field(default_factory=list)
    filter_switch_experiment_active: bool = False
    score_saturation_diagnostics: dict[str, Any] = field(default_factory=dict)
    daily_global_trade_limit_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    dynamic_trade_limit_proposal: dict[str, Any] = field(default_factory=dict)
    filter_profile: str = "DEFAULT"
    enabled_filters: list[str] = field(default_factory=list)
    hard_safety_gates: list[str] = field(default_factory=list)
    filter_state_path: str | None = None
    filter_warning: str = ""
    filter_profile_comparison_path: str | None = None
    accepted_loss_diagnostics_path: str | None = None
    short_breakdown_rescue_enabled: bool = False
    short_breakdown_rescue_scope: str = "BACKTEST-only"
    baseline_accepted_count: Any = None
    rescue_accepted_count: Any = None
    baseline_net_pnl: Any = None
    rescue_net_pnl: Any = None
    baseline_plus_rescue_net_pnl: Any = None
    accepted_reason_breakdown: dict[str, Any] = field(default_factory=dict)
    profile_comparison: dict[str, Any] = field(default_factory=dict)
    profile_leaderboard: list[dict[str, Any]] = field(default_factory=list)
    profile_leaderboard_path: str | None = None
    selected_profile_name: str | None = None
    selected_profile_dir: str | None = None
    artifact_warnings: list[str] = field(default_factory=list)
    blocking_warnings: list[str] = field(default_factory=list)
    strategy_quality_guardrails: dict[str, Any] = field(default_factory=dict)
    guardrail_reject_breakdown: dict[str, Any] = field(default_factory=dict)
    top_guardrail_reject_reasons: list[dict[str, Any]] = field(default_factory=list)
    representative_guardrail_reject_examples: list[dict[str, Any]] = field(default_factory=list)
    high_vol_guard_summary: dict[str, Any] = field(default_factory=dict)
    high_vol_guard_diagnostics_path: str | None = None
    low_score_summary: dict[str, Any] = field(default_factory=dict)
    low_score_diagnostics_path: str | None = None
    symbol_reject_summary: dict[str, Any] = field(default_factory=dict)
    symbol_reject_diagnostics_path: str | None = None
    zero_accepted_root_cause_summary: dict[str, Any] = field(default_factory=dict)
    zero_accepted_root_cause_summary_path: str | None = None
    acceptance_funnel_path: str | None = None
    top_quality_improvement_note: str = ""
    gate_funnel: list[dict[str, Any]] = field(default_factory=list)
    risk_metrics: dict[str, Any] = field(default_factory=dict)


def default_form_values() -> dict[str, Any]:
    cfg = load_config_from_env()
    default_timeframe = cfg.backtest.timeframe if cfg.backtest.timeframe in SUPPORTED_TIMEFRAMES else "15m"
    return {
        "last_days": 30,
        "symbols": "BTCUSDT,ETHUSDT",
        "timeframe": default_timeframe,
        "initial_balance": 10000,
        "max_symbols": max(1, min(int(cfg.backtest.top_n), 200)),
        "timeframes": SUPPORTED_TIMEFRAMES,
        "filter_reasons": ["LOW_SCORE", "TOO_CHOPPY", "WEAK_TREND_AND_NO_RANGE_EDGE", "STOP_TOO_WIDE", "RR_TOO_LOW", "DAILY_SYMBOL_TRADE_LIMIT", "REGIME_MISMATCH", "PANIC_CONDITIONS"],
        "filter_switches": {reason: True for reason in ["LOW_SCORE", "TOO_CHOPPY", "WEAK_TREND_AND_NO_RANGE_EDGE", "STOP_TOO_WIDE", "RR_TOO_LOW", "DAILY_SYMBOL_TRADE_LIMIT", "REGIME_MISMATCH", "PANIC_CONDITIONS"]},
        "short_breakdown_rescue_enabled": False,
        "run_profile_comparison": False,
    }


def parse_backtest_form(form: Mapping[str, Any]) -> tuple[DashboardBacktestRequest | None, dict[str, str]]:
    errors: dict[str, str] = {}

    def parse_int(name: str, default: int, min_value: int, max_value: int) -> int:
        raw = form.get(name, default)
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            errors[name] = f"{name} must be an integer."
            return default
        if value < min_value or value > max_value:
            errors[name] = f"{name} must be between {min_value} and {max_value}."
        return value

    def parse_optional_int(name: str, default: int, min_value: int, max_value: int) -> tuple[int, bool]:
        raw = form.get(name, "")
        text = str(raw).strip() if raw is not None else ""
        if text == "":
            return default, False
        try:
            value = int(text)
        except (TypeError, ValueError):
            errors[name] = f"{name} must be an integer."
            return default, True
        if value < min_value or value > max_value:
            errors[name] = f"{name} must be between {min_value} and {max_value}."
        return value, True

    def parse_float(name: str, default: float, min_value: float, max_value: float) -> float:
        raw = form.get(name, default)
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            errors[name] = f"{name} must be numeric."
            return default
        if value < min_value or value > max_value:
            errors[name] = f"{name} must be between {min_value:g} and {max_value:g}."
        return value

    last_days = parse_int("last_days", 30, 1, 730)
    initial_balance = parse_float("initial_balance", 10000.0, 100.0, 10_000_000.0)
    max_symbols, max_symbols_provided = parse_optional_int("max_symbols", default_form_values()["max_symbols"], 1, 200)
    try:
        symbols = normalize_symbol_list(form.get("symbols", ""))
    except SymbolListError as exc:
        symbols = []
        errors["symbols"] = str(exc)
    dynamic_universe_requested = not symbols and max_symbols_provided and max_symbols > 0 and "max_symbols" not in errors
    if not symbols and not dynamic_universe_requested:
        errors["symbols"] = "Provide at least one symbol or set MAX SYMBOLS greater than 0 for dynamic universe selection."
    timeframe = str(form.get("timeframe", "")).strip()
    if timeframe not in SUPPORTED_TIMEFRAMES:
        errors["timeframe"] = f"timeframe must be one of: {', '.join(SUPPORTED_TIMEFRAMES)}."
    filter_reasons = default_form_values()["filter_reasons"]
    filter_switches = {reason: str(form.get(f"filter_{reason}", "")).lower() in {"1", "true", "on", "yes"} for reason in filter_reasons}
    short_breakdown_rescue_enabled = str(form.get("short_breakdown_rescue_enabled", "")).lower() in {"1", "true", "on", "yes"}
    run_profile_comparison = str(form.get("run_profile_comparison", "")).lower() in {"1", "true", "on", "yes"}
    if errors:
        return None, errors
    return DashboardBacktestRequest(last_days=last_days, symbols=symbols, timeframe=timeframe, initial_balance=initial_balance, max_symbols=max_symbols, filter_switches=filter_switches, short_breakdown_rescue_enabled=short_breakdown_rescue_enabled, run_profile_comparison=run_profile_comparison), {}



def _selected_symbols_from_summary(summary: Mapping[str, Any]) -> list[str]:
    raw = summary.get("symbols")
    if raw in (None, "", "None", "null"):
        return []
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = str(raw).split(",")
    if not isinstance(parsed, list):
        return []
    return [str(symbol).strip().upper() for symbol in parsed if str(symbol).strip()]

def _read_first_csv_row(path: Path) -> dict[str, str]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    with path.open(newline="") as fh:
        return next(csv.DictReader(fh), {}) or {}


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def _safe_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _safe_float_or_none(value: Any) -> float | None:
    try:
        if value in (None, "", "None", "null", "UNAVAILABLE_BACKTEST"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    parsed = _safe_float_or_none(value)
    return default if parsed is None else parsed


def parse_dashboard_window_start_ms(end_iso: str, last_days: int) -> int:
    """Return a stable BACKTEST window start for all comparison sub-runs."""
    end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    start_dt = end_dt - timedelta(days=int(last_days))
    return int(start_dt.timestamp() * 1000)


def _numeric_distribution(rows: list[dict[str, str]], field: str) -> dict[str, Any]:
    values = [_safe_float_or_none(row.get(field)) for row in rows]
    values = [v for v in values if v is not None]
    if not values:
        return {"count": 0, "min": None, "mean": None, "p50": None, "p90": None, "max": None}
    ordered = sorted(values)
    def pct(q: float) -> float:
        idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
        return round(ordered[idx], 6)
    return {"count": len(values), "min": round(ordered[0], 6), "mean": round(sum(values) / len(values), 6), "p50": pct(0.5), "p90": pct(0.9), "max": round(ordered[-1], 6)}


def _decode_execution_ctx(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value in (None, ""):
        return {}
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _first_available(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", "None", "null"):
            return value
    return None


def _first_exported_available(*values: Any) -> Any:
    unavailable = {None, "", "None", "null", "NOT_EXPORTED", "UNAVAILABLE", "UNAVAILABLE_BACKTEST"}
    for value in values:
        if value not in unavailable:
            return value
    return None


def _lookup_by_signal_or_composite(rows: list[dict[str, str]]) -> dict[tuple[str, ...], dict[str, str]]:
    lookup: dict[tuple[str, ...], dict[str, str]] = {}
    for row in rows:
        signal_id = str(row.get("signal_id") or "").strip()
        if signal_id:
            lookup.setdefault(("signal_id", signal_id), row)
        symbol = str(row.get("symbol") or "").strip()
        timestamp = str(row.get("timestamp") or row.get("event_ts") or "").strip()
        side = str(row.get("side") or "").strip().upper()
        if symbol and timestamp and side:
            lookup.setdefault(("symbol_ts_side", symbol, timestamp, side), row)
    return lookup


def _match_by_signal_or_composite(row: Mapping[str, Any], lookup: Mapping[tuple[str, ...], dict[str, str]]) -> dict[str, str] | None:
    signal_id = str(row.get("signal_id") or "").strip()
    if not signal_id:
        symbol_for_id = str(row.get("symbol") or "").strip()
        timestamp_for_id = str(row.get("timestamp") or row.get("event_ts") or "").strip()
        if symbol_for_id and timestamp_for_id:
            signal_id = f"{symbol_for_id}:{timestamp_for_id}"
    if signal_id and (match := lookup.get(("signal_id", signal_id))):
        return match
    symbol = str(row.get("symbol") or "").strip()
    timestamp = str(row.get("timestamp") or row.get("event_ts") or "").strip()
    side = str(row.get("side") or "").strip().upper()
    if symbol and timestamp and side:
        return lookup.get(("symbol_ts_side", symbol, timestamp, side))
    return None



def _accepted_trade_rows(lifecycle_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    accepted_states = {
        "WAITING_ENTRY_ZONE", "ENTRY_TRIGGERED", "ORDER_PLACED", "PARTIAL_FILL", "FILLED",
        "POSITION_OPENED", "TP_HIT", "SL_HIT", "CANCELLED", "OPEN_AT_END", "POSITION_CLOSED",
    }
    state_rank = {state: idx for idx, state in enumerate((
        "WAITING_ENTRY_ZONE", "ENTRY_TRIGGERED", "ORDER_PLACED", "PARTIAL_FILL", "FILLED",
        "POSITION_OPENED", "TP_HIT", "SL_HIT", "CANCELLED", "OPEN_AT_END", "POSITION_CLOSED",
    ), start=1)}
    by_signal: dict[str, dict[str, str]] = {}
    for row in lifecycle_rows:
        state = str(row.get("lifecycle_state") or row.get("status_after") or "").strip().upper()
        decision = str(row.get("decision") or "").strip().upper()
        if decision != "ACCEPTED" and state not in accepted_states:
            continue
        signal_id = str(row.get("signal_id") or f"{row.get('symbol','')}:{row.get('timestamp') or row.get('event_ts','')}").strip()
        current = by_signal.setdefault(signal_id, {})
        if signal_id and current.get("signal_id") in (None, "", "None", "null"):
            current["signal_id"] = signal_id
        current_state = str(current.get("lifecycle_state") or current.get("status_after") or "").upper()
        next_is_later = state_rank.get(state, 0) >= state_rank.get(current_state, 0)
        # Preserve the most complete accepted trade record across lifecycle rows: early rows
        # often carry score/geometry while POSITION_CLOSED carries close_reason/PnL in execution_ctx.
        for key, value in row.items():
            if key in {"lifecycle_state", "status_after"}:
                continue
            if value not in (None, "", "None", "null"):
                if key == "execution_ctx" and current.get("execution_ctx") not in (None, "", "None", "null"):
                    merged_ctx = {**_decode_execution_ctx(current.get("execution_ctx")), **_decode_execution_ctx(value)}
                    current[key] = json.dumps(merged_ctx, sort_keys=True)
                else:
                    current[key] = value
        if next_is_later:
            current["lifecycle_state"] = state
            if row.get("status_after") not in (None, ""):
                current["status_after"] = row["status_after"]
    return list(by_signal.values())


def _accepted_trade_diagnostics(lifecycle_rows: list[dict[str, str]], backtest_order_rows: list[dict[str, str]] | None = None) -> list[dict[str, Any]]:
    order_lookup = _lookup_by_signal_or_composite(backtest_order_rows or [])
    rows = []
    for row in _accepted_trade_rows(lifecycle_rows):
        ctx = _decode_execution_ctx(row.get("execution_ctx"))
        order_row = _match_by_signal_or_composite(row, order_lookup) or {}
        net_pnl = _first_exported_available(row.get("net_pnl"), row.get("net_pnl_usdt"), row.get("pnl"), row.get("pnl_usdt"), ctx.get("net_pnl"), ctx.get("net_pnl_usdt"), ctx.get("pnl"), ctx.get("pnl_usdt"), order_row.get("net_pnl"), order_row.get("net_pnl_usdt"), order_row.get("pnl"), order_row.get("pnl_usdt"))
        gross_pnl = _first_exported_available(row.get("gross_pnl"), row.get("gross_pnl_usdt"), ctx.get("gross_pnl"), ctx.get("gross_pnl_usdt"), order_row.get("gross_pnl"), order_row.get("gross_pnl_usdt"))
        fees = _first_exported_available(row.get("fees"), row.get("fee"), row.get("cost_penalty"), ctx.get("fees"), ctx.get("fee"), ctx.get("cost_penalty"), order_row.get("fees"), order_row.get("fee"), order_row.get("cost_penalty"))
        exit_price = _first_exported_available(row.get("exit"), row.get("exit_price"), row.get("close_price"), ctx.get("exit"), ctx.get("exit_price"), ctx.get("close_price"), order_row.get("exit"), order_row.get("exit_price"), order_row.get("close_price"))
        rows.append({
            "signal_id": row.get("signal_id"),
            "symbol": row.get("symbol"),
            "side": _first_available(row.get("side"), ctx.get("side"), order_row.get("side")),
            "score": _first_available(row.get("score"), order_row.get("score")),
            "raw_rr": _first_available(row.get("raw_rr"), row.get("rr"), order_row.get("raw_rr"), order_row.get("rr")),
            "effective_rr": _first_available(row.get("effective_rr"), order_row.get("effective_rr")),
            "regime": _first_available(row.get("regime"), row.get("volatility_regime"), ctx.get("regime"), ctx.get("volatility_regime"), order_row.get("regime"), order_row.get("volatility_regime")),
            "expectancy_bucket": _first_available(row.get("expectancy_bucket"), ctx.get("expectancy_bucket"), order_row.get("expectancy_bucket")),
            "decision_cost_penalty": _first_available(row.get("decision_cost_penalty"), row.get("cost_penalty"), ctx.get("decision_cost_penalty"), ctx.get("cost_penalty"), order_row.get("decision_cost_penalty"), order_row.get("cost_penalty")),
            "entry": _first_available(row.get("entry"), row.get("entry_price"), ctx.get("entry"), ctx.get("entry_price"), order_row.get("entry"), order_row.get("entry_price")),
            "sl": _first_available(row.get("sl"), row.get("stop_loss"), ctx.get("sl"), ctx.get("stop_loss"), order_row.get("sl"), order_row.get("stop_loss")),
            "stop_loss": _first_available(row.get("stop_loss"), row.get("sl"), ctx.get("stop_loss"), ctx.get("sl"), order_row.get("stop_loss"), order_row.get("sl")),
            "tp": _first_available(row.get("tp"), row.get("take_profit"), ctx.get("tp"), ctx.get("take_profit"), order_row.get("tp"), order_row.get("take_profit")),
            "take_profit": _first_available(row.get("take_profit"), row.get("tp"), ctx.get("take_profit"), ctx.get("tp"), order_row.get("take_profit"), order_row.get("tp")),
            "exit": exit_price,
            "exit_price": exit_price,
            "exit_status": "EXPORTED" if exit_price is not None else "NOT_EXPORTED",
            "close_reason": _first_available(row.get("close_reason"), ctx.get("close_reason"), order_row.get("close_reason")),
            "result": _first_available(row.get("result"), row.get("outcome"), row.get("lifecycle_state"), row.get("status_after")),
            "gross_pnl": gross_pnl,
            "fees": fees,
            "cost_penalty": fees,
            "net_pnl": net_pnl,
            "net_pnl_status": "EXPORTED" if net_pnl is not None else "NOT_EXPORTED",
        })
    return rows


def _stop_too_wide_rescue_diagnostics(rows: list[dict[str, str]]) -> dict[str, Any]:
    stop_rows = [r for r in rows if str(r.get("reject_reason") or "").strip().upper() == "STOP_TOO_WIDE"]
    def reduced_size(row: Mapping[str, Any]) -> bool:
        return _safe_float_or_none(row.get("effective_rr")) is not None and _safe_float_or_none(row.get("risk_scale")) != 0.0
    def vol_norm(row: Mapping[str, Any]) -> bool:
        return _first_available(row.get("volatility_score"), row.get("atr_pct"), row.get("atr"), row.get("volatility_regime")) is not None and _safe_float_or_none(row.get("effective_rr")) is not None
    def alt_stop(row: Mapping[str, Any]) -> bool:
        valid = str(_first_available(row.get("alternate_stop_valid"), row.get("structural_stop_valid"), row.get("alt_stop_structurally_valid")) or "").lower()
        return valid in {"1", "true", "yes"} or _safe_float_or_none(_first_available(row.get("alternate_effective_rr"), row.get("alt_effective_rr"))) is not None
    rescue_rows = [r for r in stop_rows if reduced_size(r) or vol_norm(r) or alt_stop(r)]
    shadows = Counter(_shadow(r) for r in rescue_rows)
    return {
        "mode": "REPORTING_ONLY",
        "thresholds_changed": False,
        "accepted_trades_changed": False,
        "stop_too_wide_reject_count": len(stop_rows),
        "rescue_candidate_count": len(rescue_rows),
        "reduced_position_size_candidate_count": sum(1 for r in stop_rows if reduced_size(r)),
        "volatility_normalized_stop_candidate_count": sum(1 for r in stop_rows if vol_norm(r)),
        "tighter_alternate_stop_candidate_count": sum(1 for r in stop_rows if alt_stop(r)),
        "rescue_would_tp_count": shadows["WOULD_TP"],
        "rescue_would_sl_count": shadows["WOULD_SL"],
        "rescue_expected_effective_rr": _numeric_distribution(rescue_rows, "effective_rr")["mean"],
    }

def _counter_rows(counter: Counter) -> list[dict[str, Any]]:
    return [{"value": key, "count": count} for key, count in counter.most_common()]

def _lifecycle_diagnostics(lifecycle_rows: list[dict[str, str]], rejected_rows: list[dict[str, str]]) -> dict[str, Any]:
    state_counts = Counter((row.get("lifecycle_state") or "UNKNOWN").strip() or "UNKNOWN" for row in lifecycle_rows)
    by_signal: dict[str, list[str]] = {}
    for row in lifecycle_rows:
        signal_id = (row.get("signal_id") or f"{row.get('symbol','')}:{row.get('event_ts','')}").strip()
        by_signal.setdefault(signal_id, []).append((row.get("lifecycle_state") or "UNKNOWN").strip() or "UNKNOWN")
    path_counts = Counter(" -> ".join(states) for states in by_signal.values())
    final_reasons: Counter = Counter()
    order_reasons: Counter = Counter()
    selector_reasons: Counter = Counter()
    for row in rejected_rows:
        reason = (row.get("reject_reason") or row.get("reason") or "UNKNOWN").strip() or "UNKNOWN"
        state = (row.get("lifecycle_state") or "").strip()
        source = (row.get("source") or row.get("source_stage") or row.get("event_flags") or "").strip()
        final_reasons[reason] += 1
        if state == "ORDER_REJECTED":
            order_reasons[reason] += 1
        if state == "SYMBOL_REJECTED" or source == "SYMBOL_SELECTOR":
            selector_reasons[reason] += 1
    return {
        "lifecycle_state_counts": _counter_rows(state_counts),
        "lifecycle_path_counts": _counter_rows(path_counts),
        "final_reject_reason_counts": _counter_rows(final_reasons),
        "order_reject_reason_counts": _counter_rows(order_reasons),
        "symbol_selector_reject_counts": _counter_rows(selector_reasons),
    }

def _rejection_diagnostics(rows: list[dict[str, str]]) -> dict[str, Any]:
    reasons = Counter((row.get("reject_reason") or row.get("reason") or "UNKNOWN").strip() or "UNKNOWN" for row in rows)
    total = len(rows)
    signal_rows = sum(1 for row in rows if str(row.get("lifecycle_state", "")).strip() == "SIGNAL_REJECTED")
    selector_rows = sum(1 for row in rows if str(row.get("lifecycle_state", "")).strip() == "SYMBOL_REJECTED" or str(row.get("lifecycle_state", "")).strip() == "SYMBOL_SELECTOR_REJECT" or str(row.get("source", "")).strip() == "SYMBOL_SELECTOR" or str(row.get("event_flags", "")).strip() == "SYMBOL_SELECTOR")
    def passes(row: dict[str, str]) -> bool:
        score = _safe_float_or_none(row.get("score"))
        rr = _safe_float_or_none(row.get("raw_rr") if row.get("raw_rr") not in (None, "") else row.get("rr"))
        expectancy = _safe_float_or_none(row.get("expectancy") if row.get("expectancy") not in (None, "") else row.get("expectancy_bucket"))
        min_score = _safe_float_or_none(row.get("min_required_score")) or 7.5
        return _source_stage(row) == "SIGNAL_ENGINE" and score is not None and rr is not None and expectancy is not None and score >= min_score and rr >= 1.3 and expectancy >= 0.0
    return {
        "top_rejection_reasons": [{"reason": reason, "count": count, "ratio": (count / total if total else None)} for reason, count in reasons.most_common(8)],
        "signal_rows_count": signal_rows,
        "symbol_selector_reject_count": selector_rows,
        "score_distribution": _numeric_distribution(rows, "score"),
        "rr_distribution": _numeric_distribution(rows, "raw_rr") if any("raw_rr" in r for r in rows) else _numeric_distribution(rows, "rr"),
        "effective_rr_distribution": _numeric_distribution(rows, "effective_rr"),
        "pre_later_gate_pass_count": sum(1 for row in rows if passes(row)),
    }


def _source_stage(row: Mapping[str, Any]) -> str:
    state = str(row.get("lifecycle_state", "") or "").strip().upper()
    source = str(row.get("source_stage") or row.get("source") or row.get("event_flags") or "").strip().upper()
    if state in {"SYMBOL_REJECTED", "SYMBOL_SELECTOR_REJECT"} or source == "SYMBOL_SELECTOR":
        return "SYMBOL_SELECTOR"
    return "SIGNAL_ENGINE"


def _shadow(row: Mapping[str, Any]) -> str:
    value = str(row.get("shadow_outcome") or "").strip().upper()
    return value if value in {"WOULD_TP", "WOULD_SL", "WOULD_TIMEOUT"} else "UNKNOWN"


def _rate(rows: list[dict[str, str]], field: str) -> float | None:
    vals = [str(row.get(field, "")).strip().lower() for row in rows if row.get(field) not in (None, "")]
    if not vals:
        return None
    return round(sum(1 for v in vals if v in {"1", "true", "yes"}) / len(vals), 6)


def _score_bucket(row: Mapping[str, Any]) -> str:
    score = _safe_float_or_none(row.get("score"))
    if score is None:
        return "UNAVAILABLE"
    return "10" if score >= 10.0 else f"{int(score)}-{int(score) + 1}"


def _outcome_split(rows: list[dict[str, str]]) -> dict[str, Any]:
    count = len(rows)
    shadows = Counter(_shadow(row) for row in rows)
    return {
        "count": count,
        "would_tp_count": shadows["WOULD_TP"],
        "would_sl_count": shadows["WOULD_SL"],
        "would_timeout_count": shadows["WOULD_TIMEOUT"],
        "unknown_count": shadows["UNKNOWN"],
        "would_tp_rate": round(shadows["WOULD_TP"] / count, 6) if count else 0.0,
        "would_sl_rate": round(shadows["WOULD_SL"] / count, 6) if count else 0.0,
    }


def _score_saturation_diagnostics(accepted_rows: list[dict[str, str]], shadow_rows: list[dict[str, str]]) -> dict[str, Any]:
    accepted = [dict(row, score_bucket=_score_bucket(row)) for row in accepted_rows]
    rejected = [dict(row, score_bucket=_score_bucket(row)) for row in shadow_rows if _source_stage(row) == "SIGNAL_ENGINE"]
    all_rows = accepted + rejected
    buckets = []
    for bucket in sorted({str(row.get("score_bucket")) for row in all_rows}, key=lambda v: (v == "UNAVAILABLE", v)):
        rows = [row for row in all_rows if row.get("score_bucket") == bucket]
        buckets.append({"score_bucket": bucket, **_outcome_split(rows)})
    score10 = [row for row in all_rows if _safe_float_or_none(row.get("score")) == 10.0]
    return {
        "mode": "DIAGNOSTIC_ONLY",
        "thresholds_changed": False,
        "acceptance_logic_changed": False,
        "score_bucket_outcome_split": buckets,
        "score_10": _outcome_split(score10),
        "score_10_tp_rate": _outcome_split(score10)["would_tp_rate"],
        "score_10_sl_rate": _outcome_split(score10)["would_sl_rate"],
        "accepted_score_bucket_outcome_split": [{"score_bucket": b, **_outcome_split([r for r in accepted if r.get("score_bucket") == b])} for b in sorted({str(r.get("score_bucket")) for r in accepted})],
        "rejected_score_bucket_shadow_split": [{"score_bucket": b, **_outcome_split([r for r in rejected if r.get("score_bucket") == b])} for b in sorted({str(r.get("score_bucket")) for r in rejected})],
        "guardrail_proposal": {
            "enabled_by_default": False,
            "possible_reject_reasons": ["SCORE_SATURATION_RISK", "POOR_SCORE_BUCKET_CALIBRATION"],
            "rule": "If explicitly enabled later, down-calibrate or reject score buckets whose shadow WOULD_SL rate materially exceeds WOULD_TP rate after execution costs.",
        },
    }


def _score_saturation_from_quality_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    sat = summary.get("score_saturation") if isinstance(summary, Mapping) else {}
    if not isinstance(sat, Mapping):
        return {}
    rows: list[dict[str, Any]] = []
    for section_name in ("score_10_by_regime", "score_10_by_reject_reason"):
        section = sat.get(section_name, {})
        if not isinstance(section, Mapping):
            continue
        label = "regime" if section_name.endswith("regime") else "reject_reason"
        for key, split in section.items():
            if isinstance(split, Mapping):
                rows.append({"score_bucket": "10", "group_type": label, "group_value": key, **dict(split)})
    return {
        "mode": "DIAGNOSTIC_ONLY",
        "thresholds_changed": False,
        "acceptance_logic_changed": False,
        "score_bucket_outcome_split": rows,
        "score_10": {
            "count": sat.get("score_10_count", 0),
            "would_tp_count": sat.get("score_10_would_tp_count", 0),
            "would_sl_count": sat.get("score_10_would_sl_count", 0),
        },
        "score_10_tp_rate": (float(sat.get("score_10_would_tp_count", 0) or 0) / float(sat.get("score_10_count", 1) or 1)),
        "score_10_sl_rate": (float(sat.get("score_10_would_sl_count", 0) or 0) / float(sat.get("score_10_count", 1) or 1)),
        "warning": sat.get("warning", ""),
    }


def _daily_global_trade_limit_diagnostics(rows: list[dict[str, str]], accepted_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    accepted_by_day: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in accepted_rows:
        day = str(row.get("timestamp") or row.get("event_ts") or "")[:10]
        accepted_by_day[day].append(row)
    out = []
    for row in rows:
        if str(row.get("reject_reason") or "").upper() != "DAILY_GLOBAL_TRADE_LIMIT":
            continue
        day = str(row.get("timestamp") or row.get("event_ts") or "")[:10]
        same_day = accepted_by_day.get(day, [])
        shadow = _shadow(row)
        out.append({
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "timestamp": row.get("timestamp") or row.get("event_ts"),
            "effective_rr": row.get("effective_rr"),
            "score": row.get("score"),
            "shadow_outcome": shadow,
            "net_outcome_if_accepted": "IMPROVED" if shadow == "WOULD_TP" else ("WORSENED" if shadow == "WOULD_SL" else "UNKNOWN"),
            "same_day_accepted_trade_count": len(same_day),
            "same_day_accepted_trade_outcomes": dict(Counter(str(r.get("close_reason") or r.get("lifecycle_state") or "UNKNOWN") for r in same_day)),
        })
    return out[:50]


def _summary_value(rows: list[dict[str, str]], field: str, metric: str) -> float | None:
    dist = _numeric_distribution(rows, field)
    return dist.get(metric)


def _passes_score_rr_expectancy(row: Mapping[str, Any]) -> bool:
    score = _safe_float_or_none(row.get("score"))
    rr = _safe_float_or_none(row.get("raw_rr") if row.get("raw_rr") not in (None, "") else row.get("rr"))
    effective_rr = _safe_float_or_none(row.get("effective_rr"))
    expectancy_raw = row.get("expectancy") if row.get("expectancy") not in (None, "") else row.get("expectancy_value")
    expectancy = _safe_float_or_none(expectancy_raw)
    bucket = str(row.get("expectancy_bucket") or "").upper()
    min_score = _safe_float_or_none(row.get("min_required_score")) or 7.5
    min_effective_rr = _safe_float_or_none(row.get("min_effective_rr")) or 1.1
    expectancy_ok = expectancy is None and bucket in {"LOW", "MEDIUM", "HIGH"} or (expectancy is not None and expectancy >= 0.0)
    return score is not None and score >= min_score and rr is not None and rr >= 1.3 and effective_rr is not None and effective_rr >= min_effective_rr and expectancy_ok


def _passes_aggregate_shadow_later_gate(row: Mapping[str, Any]) -> bool:
    score = _safe_float_or_none(row.get("score"))
    rr = _safe_float_or_none(row.get("raw_rr") if row.get("raw_rr") not in (None, "") else row.get("rr"))
    effective_rr = _safe_float_or_none(row.get("effective_rr"))
    min_score = _safe_float_or_none(row.get("min_required_score")) or 7.5
    min_effective_rr = _safe_float_or_none(row.get("min_effective_rr")) or 1.1
    return score is not None and score >= min_score and rr is not None and rr >= 1.3 and effective_rr is not None and effective_rr >= min_effective_rr


def _identity_values(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(row.get(field) or "").strip() for field in ("symbol", "timestamp", "event_ts", "side"))


def _strict_shadow_lookup_key(row: Mapping[str, Any]) -> tuple[str, ...] | None:
    signal_id = str(row.get("signal_id") or "").strip()
    if signal_id:
        return ("signal_id", signal_id)
    symbol = str(row.get("symbol") or "").strip()
    timestamp = str(row.get("timestamp") or row.get("event_ts") or "").strip()
    side = str(row.get("side") or "").strip().upper()
    if symbol and timestamp and side:
        return ("symbol_ts_side", symbol, timestamp, side)
    return None


def _build_strict_shadow_lookup(shadow_rows: list[dict[str, str]]) -> dict[tuple[str, ...], dict[str, str]]:
    """Build the per-row enrichment lookup for rejected shadow outcomes.

    This lookup is intentionally strict because it attaches counterfactual
    shadow fields to a specific rejected row. Aggregate diagnostics must use
    the raw shadow rows instead so shadow-only rows are not filtered out.
    """
    lookup: dict[tuple[str, ...], dict[str, str]] = {}
    for row in shadow_rows:
        key = _strict_shadow_lookup_key(row)
        if key is not None:
            lookup.setdefault(key, row)
    return lookup


def _strict_matching_shadow(row: Mapping[str, Any], lookup: Mapping[tuple[str, ...], dict[str, str]]) -> dict[str, str] | None:
    signal_id = str(row.get("signal_id") or "").strip()
    if signal_id and (match := lookup.get(("signal_id", signal_id))):
        return match
    symbol = str(row.get("symbol") or "").strip()
    timestamp = str(row.get("timestamp") or row.get("event_ts") or "").strip()
    side = str(row.get("side") or "").strip().upper()
    if symbol and timestamp and side:
        return lookup.get(("symbol_ts_side", symbol, timestamp, side))
    return None


def _merge_shadow(row: dict[str, str], shadow: Mapping[str, str] | None) -> dict[str, str]:
    if not shadow:
        return dict(row)
    merged = dict(row)
    for field in ("shadow_outcome", "cost_penalty", "effective_tp_hit", "effective_sl_hit", "liquidity_ok", "volatility_ok", "volatility_score", "effective_rr", "raw_rr", "rr", "score", "reject_reason"):
        value = shadow.get(field)
        if value not in (None, ""):
            merged[field] = value
    return merged


def _build_calibration_outputs(lifecycle_rows: list[dict[str, str]], rejected_rows: list[dict[str, str]], summary: Mapping[str, Any], shadow_rows: list[dict[str, str]] | None = None, backtest_order_rows: list[dict[str, str]] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    shadow_source = list(shadow_rows or [])
    strict_shadow_lookup = _build_strict_shadow_lookup(shadow_source)
    enriched_rejected = [_merge_shadow(r, _strict_matching_shadow(r, strict_shadow_lookup)) for r in rejected_rows]
    aggregate_shadow_rows = [r for r in shadow_source if _source_stage(r) == "SIGNAL_ENGINE" and str(r.get("reject_reason") or "").strip()]
    shadow_diagnostic_rows = aggregate_shadow_rows or [r for r in enriched_rejected if _source_stage(r) == "SIGNAL_ENGINE"]
    combined = list(lifecycle_rows) + [r for r in enriched_rejected if r not in lifecycle_rows]
    groups: dict[tuple[str, str, str, str, str, str], list[dict[str, str]]] = {}
    for row in combined:
        key = (
            _source_stage(row),
            str(row.get("lifecycle_state") or "UNKNOWN").strip() or "UNKNOWN",
            str(row.get("reject_reason") or row.get("reason") or "").strip() or "NONE",
            str(row.get("symbol") or "UNKNOWN").strip() or "UNKNOWN",
            str(row.get("regime") or row.get("volatility_regime") or "UNKNOWN").strip() or "UNKNOWN",
            str(row.get("expectancy_bucket") or "UNKNOWN").strip() or "UNKNOWN",
        )
        groups.setdefault(key, []).append(row)
    report_rows: list[dict[str, Any]] = []
    for key, rows in sorted(groups.items()):
        shadows = Counter(_shadow(row) for row in rows)
        count = len(rows)
        report_rows.append({
            "source_stage": key[0], "lifecycle_state": key[1], "reject_reason": key[2], "symbol": key[3],
            "regime": key[4], "expectancy_bucket": key[5], "count": count,
            "mean_score": _summary_value(rows, "score", "mean"), "p50_score": _summary_value(rows, "score", "p50"), "p90_score": _summary_value(rows, "score", "p90"),
            "mean_rr": _summary_value(rows, "raw_rr", "mean") if any("raw_rr" in r for r in rows) else _summary_value(rows, "rr", "mean"),
            "p50_rr": _summary_value(rows, "raw_rr", "p50") if any("raw_rr" in r for r in rows) else _summary_value(rows, "rr", "p50"),
            "p90_rr": _summary_value(rows, "raw_rr", "p90") if any("raw_rr" in r for r in rows) else _summary_value(rows, "rr", "p90"),
            "mean_effective_rr": _summary_value(rows, "effective_rr", "mean"), "p50_effective_rr": _summary_value(rows, "effective_rr", "p50"), "p90_effective_rr": _summary_value(rows, "effective_rr", "p90"),
            "mean_cost_penalty": _summary_value(rows, "cost_penalty", "mean"), "p50_cost_penalty": _summary_value(rows, "cost_penalty", "p50"), "p90_cost_penalty": _summary_value(rows, "cost_penalty", "p90"),
            "mean_spread_pct": _summary_value(rows, "spread_pct", "mean"), "mean_expected_slippage_pct": _summary_value(rows, "expected_slippage_pct", "mean"), "mean_volume_24h_usdt": _summary_value(rows, "volume_24h_usdt", "mean"),
            "liquidity_ok_rate": _rate(rows, "liquidity_ok"), "volatility_ok_rate": _rate(rows, "volatility_ok"),
            "would_tp_count": shadows["WOULD_TP"], "would_tp_rate": round(shadows["WOULD_TP"] / count, 6),
            "would_sl_count": shadows["WOULD_SL"], "would_sl_rate": round(shadows["WOULD_SL"] / count, 6),
            "would_timeout_count": shadows["WOULD_TIMEOUT"], "would_timeout_rate": round(shadows["WOULD_TIMEOUT"] / count, 6),
            "unknown_shadow_count": shadows["UNKNOWN"], "unknown_shadow_rate": round(shadows["UNKNOWN"] / count, 6),
        })
    signal_rejected = [r for r in enriched_rejected if _source_stage(r) == "SIGNAL_ENGINE" and str(r.get("lifecycle_state", "")).upper() == "SIGNAL_REJECTED"]
    raw_signal_rejected = [r for r in rejected_rows if _source_stage(r) == "SIGNAL_ENGINE" and str(r.get("lifecycle_state", "")).upper() == "SIGNAL_REJECTED"]
    passed_later = [r for r in signal_rejected if _passes_score_rr_expectancy(r)]
    later_gate_reasons = {"DAILY_SYMBOL_TRADE_LIMIT", "REGIME_MISMATCH", "RR_TOO_LOW", "STOP_TOO_WIDE"}
    later_gate_source = [r for r in shadow_diagnostic_rows if str(r.get("reject_reason") or "").strip().upper() in later_gate_reasons and _passes_aggregate_shadow_later_gate(r)]
    if not later_gate_source:
        later_gate_source = [r for r in passed_later if str(r.get("reject_reason") or "").strip().upper() in later_gate_reasons]
    later_gate_groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in later_gate_source:
        reason = str(row.get("reject_reason") or "UNKNOWN").strip() or "UNKNOWN"
        later_gate_groups[(reason, _source_stage(row))].append(row)
    later_gate = []
    for (reason, stage), rows in sorted(later_gate_groups.items()):
        shadows = Counter(_shadow(row) for row in rows)
        count = len(rows)
        later_gate.append({
            "reject_reason": reason,
            "source_stage": stage,
            "count": count,
            "avg_score": _summary_value(rows, "score", "mean"),
            "mean_score": _summary_value(rows, "score", "mean"),
            "avg_effective_rr": _summary_value(rows, "effective_rr", "mean"),
            "mean_effective_rr": _summary_value(rows, "effective_rr", "mean"),
            "would_tp_count": shadows["WOULD_TP"],
            "would_tp_rate": round(shadows["WOULD_TP"] / count, 6) if count else 0.0,
            "would_sl_count": shadows["WOULD_SL"],
            "would_sl_rate": round(shadows["WOULD_SL"] / count, 6) if count else 0.0,
            "would_timeout_count": shadows["WOULD_TIMEOUT"],
            "avg_cost_penalty": _summary_value(rows, "cost_penalty", "mean"),
            "mean_cost_penalty": _summary_value(rows, "cost_penalty", "mean"),
            "liquidity_ok_rate": _rate(rows, "liquidity_ok"),
            "volatility_ok_rate": _rate(rows, "volatility_ok"),
        })
    low = [r for r in shadow_diagnostic_rows if str(r.get("reject_reason") or "").upper() == "LOW_SCORE"]
    low_tp = [r for r in low if _shadow(r) == "WOULD_TP"]
    low_sl = [r for r in low if _shadow(r) == "WOULD_SL"]
    low_cmp = {
        "would_tp_count": len(low_tp), "would_sl_count": len(low_sl),
        "mean_score_would_tp": _summary_value(low_tp, "score", "mean"), "mean_score_would_sl": _summary_value(low_sl, "score", "mean"),
        "mean_effective_rr_would_tp": _summary_value(low_tp, "effective_rr", "mean"), "mean_effective_rr_would_sl": _summary_value(low_sl, "effective_rr", "mean"),
        "mean_volatility_score_would_tp": _summary_value(low_tp, "volatility_score", "mean"), "mean_volatility_score_would_sl": _summary_value(low_sl, "volatility_score", "mean"),
        "mean_cost_penalty_would_tp": _summary_value(low_tp, "cost_penalty", "mean"), "mean_cost_penalty_would_sl": _summary_value(low_sl, "cost_penalty", "mean"),
        "symbol_breakdown": dict(Counter(str(r.get("symbol") or "UNKNOWN") for r in low)),
        "expectancy_bucket_breakdown": dict(Counter(str(r.get("expectancy_bucket") or "UNKNOWN") for r in low)),
        "diagnostic_answer": "LOW_SCORE calibration requires review if WOULD_TP has materially higher score/effective_rr than WOULD_SL; threshold is unchanged by this diagnostic.",
    }
    accepted_ids = {str(r.get("signal_id")) for r in lifecycle_rows if str(r.get("decision", "")).upper() == "ACCEPTED" or str(r.get("lifecycle_state", "")).upper() in {"WAITING_ENTRY_ZONE", "ENTRY_TRIGGERED", "ORDER_PLACED"}}
    outcome_states = Counter(str(r.get("lifecycle_state") or "UNKNOWN").upper() for r in lifecycle_rows if str(r.get("lifecycle_state") or "").upper() in {"TP_HIT", "SL_HIT", "OPEN_AT_END", "POSITION_CLOSED"})
    accepted_summary = _safe_int(summary.get("accepted_count"))
    funnel = {
        "symbol_selector_rejects": sum(1 for r in rejected_rows if _source_stage(r) == "SYMBOL_SELECTOR"),
        "signal_engine_signal_created": sum(1 for r in lifecycle_rows if _source_stage(r) == "SIGNAL_ENGINE" and str(r.get("lifecycle_state", "")).upper() == "SIGNAL_CREATED"),
        "signal_engine_signal_rejected": len(signal_rejected),
        "passed_score_rr_expectancy": len(passed_later),
        "rejected_by_later_gates": len(passed_later),
        "accepted_trades": accepted_summary if accepted_summary is not None else len(accepted_ids),
        "executed_trade_outcomes": dict(outcome_states),
        "note": "SYMBOL_SELECTOR rejects are pre-signal diagnostics; WOULD_TP/WOULD_SL rejected shadows are counterfactual labels and never acceptance approvals.",
    }
    near = sorted(passed_later, key=lambda r: ((_safe_float_or_none(r.get("score")) or 0.0), (_safe_float_or_none(r.get("effective_rr")) or 0.0), 1 if _shadow(r) == "WOULD_TP" else 0), reverse=True)[:20]
    accepted_rows = _accepted_trade_rows(lifecycle_rows)
    score_saturation = _score_saturation_diagnostics(accepted_rows, shadow_diagnostic_rows)
    daily_global_limit = _daily_global_trade_limit_diagnostics(shadow_diagnostic_rows, accepted_rows)
    summary_out = {
        "rejection_funnel": funnel,
        "later_gate_diagnostics": later_gate,
        "low_score_shadow_comparison": low_cmp,
        "execution_cost_summary": {
            "decision_cost_penalty": _numeric_distribution(raw_signal_rejected, "cost_penalty"),
            "shadow_cost_penalty": _numeric_distribution(shadow_source, "cost_penalty"),
            "cost_basis": "decision_cost_penalty comes from rejected_orders/order-decision context; shadow_cost_penalty comes from rejected_shadow.csv forward counterfactual evaluation. These are intentionally not mixed into one cost_penalty metric.",
            "spread_pct": _numeric_distribution(signal_rejected, "spread_pct"),
            "expected_slippage_pct": _numeric_distribution(signal_rejected, "expected_slippage_pct"),
            "spread_label": "ESTIMATED_BACKTEST_SPREAD when spread_source/status is estimated and historical bid/ask is unavailable",
        },
        "near_miss_rejected_signals": [dict({k: (r.get(k) if r.get(k) not in (None, "") else ("UNAVAILABLE" if k in {"shadow_outcome", "cost_penalty"} else r.get(k))) for k in ("signal_id", "symbol", "reject_reason", "score", "raw_rr", "rr", "effective_rr", "cost_penalty", "shadow_outcome", "spread_pct", "expected_slippage_pct", "liquidity_ok", "volatility_ok")}) for r in near],
        "accepted_trade_diagnostics": _accepted_trade_diagnostics(lifecycle_rows, backtest_order_rows),
        "score_saturation_diagnostics": score_saturation,
        "daily_global_trade_limit_diagnostics": daily_global_limit,
        "dynamic_trade_limit_proposal": {
            "enabled": False,
            "default_behavior_changed": False,
            "mode": "PROPOSAL_ONLY",
            "requirements": [
                "effective_rr above a configurable high threshold",
                "score bucket has historically positive shadow TP-vs-SL calibration after costs",
                "same-day accepted trades are not degraded",
                "symbol/session correlation exposure is acceptable",
                "shadow diagnostics show WOULD_TP advantage over WOULD_SL in that bucket",
            ],
            "note": "DAILY_GLOBAL_TRADE_LIMIT is not disabled or relaxed by this proposal.",
        },
        "accepted_score_distribution": _numeric_distribution(accepted_rows, "score"),
        "accepted_effective_rr_distribution": _numeric_distribution(accepted_rows, "effective_rr"),
        "near_miss_score_distribution": _numeric_distribution(near, "score"),
        "near_miss_effective_rr_distribution": _numeric_distribution(near, "effective_rr"),
        "stop_too_wide_rescue_diagnostics": _stop_too_wide_rescue_diagnostics(later_gate_source),
        "stop_too_wide_recoverable_candidates": _stop_too_wide_recoverable_candidate_table(shadow_diagnostic_rows),
        "disabled_filters": summary.get("disabled_filters", "[]"),
        "filter_switch_experiment_active": str(summary.get("filter_switch_experiment_active", "False")).lower() in {"1", "true", "yes", "on"},
        "disabled_filter_bypass_count": _safe_int(summary.get("disabled_filter_bypass_count")) or 0,
    }
    return report_rows, summary_out


def _write_calibration_artifacts(output_dir: Path, lifecycle_rows: list[dict[str, str]], rejected_rows: list[dict[str, str]], summary: Mapping[str, Any], shadow_rows: list[dict[str, str]] | None = None, backtest_order_rows: list[dict[str, str]] | None = None) -> tuple[Path, Path, dict[str, Any]]:
    report_rows, summary_out = _build_calibration_outputs(lifecycle_rows, rejected_rows, summary, shadow_rows, backtest_order_rows)
    report_path = output_dir / "lifecycle_calibration_report.csv"
    summary_path = output_dir / "lifecycle_calibration_summary.json"
    fieldnames = list(report_rows[0].keys()) if report_rows else ["source_stage", "lifecycle_state", "reject_reason", "symbol", "regime", "expectancy_bucket", "count"]
    with report_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(report_rows)
    summary_path.write_text(json.dumps(summary_out, indent=2, sort_keys=True))
    later_gate_path = output_dir / "later_gate_breakdown.csv"
    later_gate_rows = list(summary_out.get("later_gate_diagnostics", [])) if isinstance(summary_out, Mapping) else []
    later_gate_fields = list(later_gate_rows[0].keys()) if later_gate_rows else ["reject_reason", "source_stage", "count", "avg_score", "avg_effective_rr", "would_tp_rate", "would_sl_rate"]
    with later_gate_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=later_gate_fields)
        writer.writeheader()
        writer.writerows(later_gate_rows)
    return report_path, summary_path, summary_out

PROFILE_FILTERS: dict[str, list[str]] = {
    "DEFAULT_FILTERS": [],
    "ALL_FILTERS_OFF": list(default_form_values()["filter_reasons"]),
    "STRICT_FILTERS": [],
    "SCORE_SATURATION_GUARD_DIAGNOSTIC": [],
    "STOP_WIDTH_GUARD_DIAGNOSTIC": [],
    "TRADE_FREQUENCY_GUARD_DIAGNOSTIC": [],
}


def _profit_factor(rows: list[Mapping[str, Any]]) -> float | None:
    wins = sum(max(_safe_float(r.get("net_pnl_usdt", r.get("net_pnl")), 0.0), 0.0) for r in rows)
    losses = abs(sum(min(_safe_float(r.get("net_pnl_usdt", r.get("net_pnl")), 0.0), 0.0) for r in rows))
    if losses == 0:
        return None if wins == 0 else float("inf")
    return wins / losses


def _avg_net(rows: list[Mapping[str, Any]]) -> float | None:
    if not rows:
        return None
    return sum(_safe_float(r.get("net_pnl_usdt", r.get("net_pnl")), 0.0) for r in rows) / len(rows)


def _artifact_missing_message(path: Path, fallbacks: list[Path] | None = None) -> str:
    checked = [str(path), *[str(p) for p in (fallbacks or [])]]
    return f"Missing artifact. Expected path: {path}. Fallbacks checked: {', '.join(checked)}"


def _window_days_from_metadata(run_dir: Path, fallback: int | float | None = None) -> float | None:
    metadata_path = run_dir / "backtest_run_metadata.json"
    if metadata_path.exists() and metadata_path.stat().st_size:
        try:
            metadata = json.loads(metadata_path.read_text())
        except json.JSONDecodeError:
            metadata = {}
        requested = _safe_float_or_none(metadata.get("requested_last_n_days"))
        if requested:
            return requested
        start = metadata.get("effective_start")
        end = metadata.get("effective_end")
        try:
            start_dt = datetime.fromtimestamp(float(start) / 1000.0) if str(start).isdigit() else datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            end_dt = datetime.fromtimestamp(float(end) / 1000.0) if str(end).isdigit() else datetime.fromisoformat(str(end).replace("Z", "+00:00"))
            days = abs((end_dt - start_dt).total_seconds()) / 86400.0
            if days > 0:
                return days
        except (TypeError, ValueError):
            pass
    return float(fallback) if fallback else None


def _selected_profile_dir(run_dir: Path, selected_profile_name: str | None = None) -> tuple[str, Path]:
    profile_name = selected_profile_name or "DEFAULT_FILTERS"
    profiles_root = run_dir / "profiles"
    if (profiles_root / profile_name).exists():
        return profile_name, profiles_root / profile_name
    return profile_name, profiles_root / profile_name


def _diagnostics_from_backtest_orders(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append({
            "signal_id": row.get("signal_id"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "score": row.get("score"),
            "raw_rr": row.get("raw_rr") or row.get("rr"),
            "effective_rr": row.get("effective_rr"),
            "regime": row.get("regime") or row.get("volatility_regime"),
            "entry": row.get("entry") or row.get("entry_price"),
            "sl": row.get("sl") or row.get("stop_loss"),
            "tp": row.get("tp") or row.get("take_profit"),
            "exit": row.get("exit") or row.get("exit_price") or row.get("close_price"),
            "close_reason": row.get("close_reason"),
            "result": row.get("result") or row.get("close_reason"),
            "net_pnl": row.get("net_pnl") or row.get("net_pnl_usdt"),
            "net_pnl_status": "EXPORTED" if _first_exported_available(row.get("net_pnl"), row.get("net_pnl_usdt")) is not None else "NOT_EXPORTED",
        })
    return out



def _accepted_reason_breakdown_from_orders(rows: list[dict[str, str]]) -> dict[str, int]:
    reasons = Counter()
    for row in rows:
        reason = str(_first_available(row.get("accepted_reason"), row.get("acceptance_reason"), row.get("source"), row.get("strategy_source"), "UNKNOWN")).strip() or "UNKNOWN"
        reasons[reason] += 1
    return dict(reasons)


def _selected_scoped_summary_reason_breakdown(summary: Mapping[str, Any], accepted_count: int | None) -> dict[str, Any]:
    raw = summary.get("accepted_reason_breakdown")
    if not raw:
        return {}
    try:
        decoded = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    numeric_total = 0
    normalized: dict[str, Any] = {}
    for key, value in decoded.items():
        count = _safe_int(value)
        if count is None:
            return {}
        normalized[str(key)] = count
        numeric_total += count
    if accepted_count is not None and numeric_total != accepted_count:
        return {}
    return normalized


def _effective_expectancy_estimate(rows: list[dict[str, str]]) -> float | None:
    if not rows:
        return None
    split = _outcome_split(rows)
    mean_rr = _summary_value(rows, "effective_rr", "mean") or 0.0
    return round((split["would_tp_rate"] * mean_rr) - split["would_sl_rate"], 6)


def _stop_too_wide_recoverable_candidate_table(rows: list[dict[str, str]]) -> dict[str, Any]:
    stop_rows = [r for r in rows if str(r.get("reject_reason") or "").strip().upper() == "STOP_TOO_WIDE"]
    groups: dict[tuple[str, str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in stop_rows:
        outcome = _shadow(row).lower().replace("would_", "would_")
        if outcome == "would_timeout":
            outcome = "timeout"
        elif outcome not in {"would_tp", "would_sl"}:
            outcome = "unknown"
        key = (
            str(row.get("symbol") or "UNKNOWN"),
            str(row.get("side") or "UNKNOWN").upper() or "UNKNOWN",
            str(row.get("regime") or row.get("volatility_regime") or "UNKNOWN"),
            _bucket(row.get("effective_rr"), 0.5),
            outcome,
        )
        groups[key].append(row)
    table = []
    for (symbol, side, regime, rr_bucket, outcome), bucket_rows in sorted(groups.items()):
        count = len(bucket_rows)
        split = _outcome_split(bucket_rows)
        highlighted = any(
            (_safe_float_or_none(r.get("score")) or 0.0) >= 9.5
            and (_safe_float_or_none(r.get("effective_rr")) or 0.0) >= 1.9
            and _shadow(r) in {"WOULD_TP", "WOULD_SL"}
            for r in bucket_rows
        )
        table.append({
            "symbol": symbol,
            "side": side,
            "regime": regime,
            "effective_rr_bucket": rr_bucket,
            "shadow_outcome_bucket": outcome,
            "count": count,
            "would_tp_rate": split["would_tp_rate"],
            "would_sl_rate": split["would_sl_rate"],
            "mean_effective_rr": _summary_value(bucket_rows, "effective_rr", "mean"),
            "expected_effective_expectancy": _effective_expectancy_estimate(bucket_rows),
            "highlighted_candidate": highlighted,
        })
    return {
        "mode": "DIAGNOSTIC_ONLY",
        "decision_logic_changed": False,
        "stop_too_wide_gate_loosened": False,
        "candidate_table": table,
        "highlighted_candidates": [row for row in table if row["highlighted_candidate"]],
    }

def _apply_backtest_artifact_model(result: DashboardBacktestResult, artifact_dir: Path, *, selected_profile_name: str | None = None, window_days: float | None = None) -> None:
    """Populate the overview Backtest Result panel from a real backtest artifact directory."""
    run_dir = artifact_dir
    if (artifact_dir / "profiles").exists():
        profile_name, profile_dir = _selected_profile_dir(artifact_dir, selected_profile_name)
        result.selected_profile_name = profile_name
        result.selected_profile_dir = str(profile_dir)
        artifact_dir = profile_dir
    result.output_dir = str(run_dir)
    summary_path = artifact_dir / "order_backtest_summary.csv"
    lifecycle_path = artifact_dir / "order_lifecycle.csv"
    rejected_path = artifact_dir / "rejected_orders.csv"
    rejected_shadow_path = artifact_dir / "rejected_shadow.csv"
    rejected_shadow_summary_path = artifact_dir / "rejected_shadow_summary.csv"
    high_rr_path = artifact_dir / "high_effective_rr_missed_alpha.csv"
    backtest_orders_path = artifact_dir / "backtest_orders.csv"
    calibration_summary_path = artifact_dir / "lifecycle_calibration_summary.json"
    filter_state_path = artifact_dir / "backtest_filter_state.json"
    signal_quality_summary_path = artifact_dir / "signal_quality_summary.json"
    candidate_quality_gates_path = artifact_dir / "candidate_quality_gates.csv"
    later_gate_path = artifact_dir / "later_gate_breakdown.csv"
    gate_funnel_path = artifact_dir / "default_gate_funnel.csv"
    acceptance_funnel_path = artifact_dir / "acceptance_funnel.csv"
    high_vol_guard_diagnostics_path = artifact_dir / "high_vol_guard_diagnostics.csv"
    high_vol_guard_summary_path = artifact_dir / "high_vol_guard_summary.json"
    low_score_diagnostics_path = artifact_dir / "low_score_diagnostics.csv"
    low_score_summary_path = artifact_dir / "low_score_summary.json"
    symbol_reject_diagnostics_path = artifact_dir / "symbol_reject_diagnostics.csv"
    symbol_reject_summary_path = artifact_dir / "symbol_reject_summary.json"
    zero_accepted_root_cause_summary_path = artifact_dir / "zero_accepted_root_cause_summary.json"
    equity_curve_path = artifact_dir / "equity_curve.csv"
    strategy_quality_path = artifact_dir / "strategy_quality_guardrails.json"

    summary = _read_first_csv_row(summary_path)
    lifecycle_rows = _read_csv_rows(lifecycle_path)
    rejected_rows = _read_csv_rows(rejected_path)
    rejected_shadow_rows = _read_csv_rows(rejected_shadow_path)
    backtest_order_rows = _read_csv_rows(backtest_orders_path)
    calibration_summary = json.loads(calibration_summary_path.read_text()) if calibration_summary_path.exists() and calibration_summary_path.stat().st_size else {}
    signal_quality_summary = json.loads(signal_quality_summary_path.read_text()) if signal_quality_summary_path.exists() and signal_quality_summary_path.stat().st_size else {}
    filter_state = json.loads(filter_state_path.read_text()) if filter_state_path.exists() and filter_state_path.stat().st_size else {}
    strategy_quality = json.loads(strategy_quality_path.read_text()) if strategy_quality_path.exists() and strategy_quality_path.stat().st_size else {}
    high_vol_guard_summary = json.loads(high_vol_guard_summary_path.read_text()) if high_vol_guard_summary_path.exists() and high_vol_guard_summary_path.stat().st_size else {}
    low_score_summary = json.loads(low_score_summary_path.read_text()) if low_score_summary_path.exists() and low_score_summary_path.stat().st_size else {}
    symbol_reject_summary = json.loads(symbol_reject_summary_path.read_text()) if symbol_reject_summary_path.exists() and symbol_reject_summary_path.stat().st_size else {}
    zero_accepted_root_cause_summary = json.loads(zero_accepted_root_cause_summary_path.read_text()) if zero_accepted_root_cause_summary_path.exists() and zero_accepted_root_cause_summary_path.stat().st_size else {}

    for path, fallbacks in (
        (summary_path, []),
        (backtest_orders_path, [artifact_dir / "accepted_orders.csv", calibration_summary_path]),
        (rejected_path, []),
        (calibration_summary_path, [backtest_orders_path]),
    ):
        if not path.exists():
            result.artifact_warnings.append(_artifact_missing_message(path, fallbacks))

    exported_symbols = _selected_symbols_from_summary(summary)
    if exported_symbols:
        result.symbols = exported_symbols
    result.summary_path = str(summary_path) if summary_path.exists() else None
    result.lifecycle_path = str(lifecycle_path) if lifecycle_path.exists() else None
    result.rejected_path = str(rejected_path) if rejected_path.exists() else None
    result.calibration_summary_path = str(calibration_summary_path) if calibration_summary_path.exists() else None
    result.total_candidates = _safe_int(summary.get("total_candidates"))
    accepted_summary_count = _safe_int(summary.get("accepted_count"))
    result.accepted_trades = accepted_summary_count if accepted_summary_count is not None else (len(backtest_order_rows) or None)
    rejected_summary_count = _safe_int(summary.get("rejected_count") or summary.get("total_rejected"))
    canonical_rejected_count = len(rejected_rows) if rejected_rows else rejected_summary_count
    result.rejected_signals = canonical_rejected_count if canonical_rejected_count is not None else None
    result.win_count = _safe_int(summary.get("tp_hits"))
    result.loss_count = _safe_int(summary.get("sl_hits"))
    result.open_count = _safe_int(summary.get("open_at_end"))
    result.net_pnl = summary.get("total_net_pnl_usdt")
    result.total_return_pct = summary.get("total_pnl_pct")
    result.max_drawdown = summary.get("max_drawdown")
    result.strategy_quality_guardrails = strategy_quality if isinstance(strategy_quality, dict) else {}
    result.high_vol_guard_summary = high_vol_guard_summary if isinstance(high_vol_guard_summary, dict) else {}
    result.high_vol_guard_diagnostics_path = str(high_vol_guard_diagnostics_path) if high_vol_guard_diagnostics_path.exists() else None
    result.low_score_summary = low_score_summary if isinstance(low_score_summary, dict) else {}
    result.low_score_diagnostics_path = str(low_score_diagnostics_path) if low_score_diagnostics_path.exists() else None
    result.symbol_reject_summary = symbol_reject_summary if isinstance(symbol_reject_summary, dict) else {}
    result.symbol_reject_diagnostics_path = str(symbol_reject_diagnostics_path) if symbol_reject_diagnostics_path.exists() else None
    result.zero_accepted_root_cause_summary = zero_accepted_root_cause_summary if isinstance(zero_accepted_root_cause_summary, dict) else {}
    result.zero_accepted_root_cause_summary_path = str(zero_accepted_root_cause_summary_path) if zero_accepted_root_cause_summary_path.exists() else None
    result.acceptance_funnel_path = str(acceptance_funnel_path) if acceptance_funnel_path.exists() else None
    result.risk_metrics = {
        "return_unit": summary.get("return_unit", "pct"),
        "net_pnl_unit": summary.get("net_pnl_unit", "USDT"),
        "max_drawdown": summary.get("max_drawdown"),
        "max_drawdown_pct": summary.get("max_drawdown_pct"),
        "longest_loss_streak": summary.get("longest_loss_streak"),
        "longest_win_streak": summary.get("longest_win_streak"),
        "profit_factor": summary.get("profit_factor"),
        "equity_curve_path": str(equity_curve_path) if equity_curve_path.exists() else None,
    }
    result.baseline_accepted_count = _safe_int(summary.get("baseline_accepted_trades"))
    result.rescue_accepted_count = _safe_int(summary.get("rescue_accepted_count"))
    result.baseline_net_pnl = summary.get("baseline_net_pnl")
    result.rescue_net_pnl = summary.get("rescue_accepted_net_pnl")
    result.baseline_plus_rescue_net_pnl = summary.get("baseline_plus_rescue_net_pnl")
    result.accepted_reason_breakdown = _accepted_reason_breakdown_from_orders(backtest_order_rows) if backtest_order_rows else _selected_scoped_summary_reason_breakdown(summary, result.accepted_trades)

    diagnostics = _rejection_diagnostics(rejected_rows)
    result.top_rejection_reasons = diagnostics["top_rejection_reasons"]
    result.signal_rows_count = diagnostics["signal_rows_count"]
    result.symbol_selector_reject_count = diagnostics["symbol_selector_reject_count"]
    result.score_distribution = diagnostics["score_distribution"]
    result.rr_distribution = diagnostics["rr_distribution"]
    result.effective_rr_distribution = diagnostics["effective_rr_distribution"]
    result.pre_later_gate_pass_count = diagnostics["pre_later_gate_pass_count"]
    result.accepted_trade_diagnostics = list(calibration_summary.get("accepted_trade_diagnostics") or []) or _accepted_trade_diagnostics(lifecycle_rows, backtest_order_rows) or _diagnostics_from_backtest_orders(backtest_order_rows)
    result.accepted_score_distribution = calibration_summary.get("accepted_score_distribution") or _numeric_distribution(backtest_order_rows, "score")
    result.accepted_effective_rr_distribution = calibration_summary.get("accepted_effective_rr_distribution") or _numeric_distribution(backtest_order_rows, "effective_rr")
    result.near_miss_score_distribution = calibration_summary.get("near_miss_score_distribution", {})
    result.near_miss_effective_rr_distribution = calibration_summary.get("near_miss_effective_rr_distribution", {})
    result.rejection_funnel = calibration_summary.get("rejection_funnel", {})
    result.later_gate_diagnostics = calibration_summary.get("later_gate_diagnostics") or _read_csv_rows(later_gate_path)
    guardrail_attribution = _guardrail_attribution_from_sources(rejected_rows, result.later_gate_diagnostics, calibration_summary)
    result.guardrail_reject_breakdown = result.strategy_quality_guardrails.get("guardrail_reject_breakdown") or guardrail_attribution["guardrail_reject_breakdown"]
    result.top_guardrail_reject_reasons = result.strategy_quality_guardrails.get("top_guardrail_reject_reasons") or guardrail_attribution["top_guardrail_reject_reasons"]
    result.representative_guardrail_reject_examples = result.strategy_quality_guardrails.get("representative_guardrail_reject_examples") or guardrail_attribution["representative_guardrail_reject_examples"]
    result.low_score_shadow_comparison = calibration_summary.get("low_score_shadow_comparison", {})
    result.execution_cost_summary = calibration_summary.get("execution_cost_summary", {})
    result.near_miss_rejected_signals = calibration_summary.get("near_miss_rejected_signals") or rejected_shadow_rows[:20]
    result.high_effective_rr_missed_alpha = signal_quality_summary.get("high_effective_rr_missed_alpha", []) if isinstance(signal_quality_summary, dict) else _read_csv_rows(high_rr_path)
    result.signal_quality_diagnostics = signal_quality_summary if isinstance(signal_quality_summary, dict) else {}
    result.top_quality_improvement_note = str(result.signal_quality_diagnostics.get("top_quality_improvement_candidate_note") or "")
    result.stop_too_wide_rescue_diagnostics = calibration_summary.get("stop_too_wide_rescue_diagnostics", {}) if isinstance(calibration_summary, dict) else {}
    recoverable_rows = rejected_shadow_rows or rejected_rows
    result.signal_quality_diagnostics.setdefault("stop_too_wide_recoverable_candidates", _stop_too_wide_recoverable_candidate_table(recoverable_rows))
    if candidate_quality_gates_path.exists():
        result.signal_quality_diagnostics.setdefault("candidate_quality_gates", _read_csv_rows(candidate_quality_gates_path))
    exported_gate_funnel = _read_csv_rows(acceptance_funnel_path) or _read_csv_rows(gate_funnel_path)
    result.gate_funnel = exported_gate_funnel or _canonical_gate_funnel_from_rejections(rejected_rows, result.accepted_trades)
    if rejected_shadow_summary_path.exists():
        result.signal_quality_diagnostics.setdefault("rejected_shadow_summary", _read_csv_rows(rejected_shadow_summary_path))
    if isinstance(filter_state, dict) and filter_state:
        result.filter_state_path = str(filter_state_path)
        result.filter_profile = str(filter_state.get("filter_profile", result.selected_profile_name or result.filter_profile))
        result.enabled_filters = list(filter_state.get("enabled_filters", []))
        result.disabled_filters = list(filter_state.get("disabled_filters", []))
        result.hard_safety_gates = [str(g.get("filter_name", g)) for g in filter_state.get("hard_safety_gates", [])]
    if result.rejected_signals is not None and result.accepted_trades is not None:
        denom = result.rejected_signals + result.accepted_trades
        result.backtest_rejection_rate = (result.rejected_signals / denom) if denom else None
    result.score_saturation_diagnostics = _score_saturation_from_quality_summary(result.signal_quality_diagnostics) or _score_saturation_diagnostics(result.accepted_trade_diagnostics, rejected_shadow_rows or rejected_rows)
    avg_per_day = (float(result.accepted_trades or 0.0) / max(1.0, float(window_days or _window_days_from_metadata(run_dir) or _safe_float_or_none(summary.get("last_days")) or 1.0)))
    score10 = result.score_saturation_diagnostics.get("score_10", {}) if isinstance(result.score_saturation_diagnostics, dict) else {}
    score10_sl = _safe_float_or_none(score10.get("would_sl_count")) or 0.0
    score10_tp = _safe_float_or_none(score10.get("would_tp_count")) or 0.0
    if avg_per_day > float(os.getenv("ALPHAFORGE_BACKTEST_SAFE_TRADES_PER_DAY", "3")):
        result.blocking_warnings.append("OVERTRADE_RISK")
    if score10_sl > score10_tp:
        result.blocking_warnings.append("SCORE_SATURATION_RISK")
    if (_safe_float_or_none(result.net_pnl) or 0.0) < 0 and "OVERTRADE_RISK" in result.blocking_warnings and "SCORE_SATURATION_RISK" in result.blocking_warnings:
        result.blocking_warnings.append("DEFAULT PROFILE NOT STRATEGY-QUALITY: overtrade/score saturation risk")
    effective_days = window_days or _window_days_from_metadata(run_dir) or _safe_float_or_none(summary.get("last_days"))
    if result.profile_leaderboard and effective_days:
        for row in result.profile_leaderboard:
            row["avg_trades_per_day"] = (float(row.get("accepted_trades") or 0.0) / effective_days)
            raw_warnings = row.get("warnings", [])
            if isinstance(raw_warnings, str):
                try:
                    decoded_warnings = json.loads(raw_warnings)
                    raw_warnings = decoded_warnings if isinstance(decoded_warnings, list) else [raw_warnings]
                except json.JSONDecodeError:
                    raw_warnings = [w.strip() for w in raw_warnings.split(",") if w.strip()]
            warnings = [str(w) for w in raw_warnings if str(w) != "OVERTRADE_RISK"]
            if row["avg_trades_per_day"] > 3:
                warnings.append("OVERTRADE_RISK")
            row["warnings"] = sorted(set(warnings))



def _guardrail_attribution_from_sources(rejected_rows: list[dict[str, str]], later_gate_rows: list[dict[str, Any]], calibration_summary: Mapping[str, Any]) -> dict[str, Any]:
    """Build reporting-only guardrail attribution from exported later-gate evidence.

    This intentionally does not infer accepted trades. It only names concrete later
    gates/reasons when the rejection funnel says candidates passed score/RR/expectancy
    and were rejected downstream.
    """
    guard_names = {
        "DAILY_SYMBOL_TRADE_LIMIT", "DAILY_TRADE_FREQUENCY_GUARD", "LOSS_STREAK_PAUSE",
        "SYMBOL_CLUSTER_GUARD", "SCORE_SATURATION_GUARD", "HIGH_VOL_GUARD",
        "HIGH_VOL_OVERTRADE", "HIGH_VOL_EXECUTION_COST", "REGIME_MISMATCH",
        "RR_TOO_LOW", "STOP_TOO_WIDE", "PANIC_CONDITIONS",
    }
    breakdown: Counter = Counter()
    examples: list[dict[str, Any]] = []
    for row in later_gate_rows:
        reason = str(row.get("reject_reason") or row.get("reason") or row.get("gate") or "UNKNOWN").strip().upper() or "UNKNOWN"
        count = _safe_int(row.get("count")) or _safe_int(row.get("rejected_by_gate")) or 0
        if count > 0:
            breakdown[reason] += count
            examples.append({k: row.get(k) for k in ("reject_reason", "gate", "source_stage", "count", "avg_score", "mean_score", "avg_effective_rr", "mean_effective_rr") if row.get(k) not in (None, "")})
    if not breakdown:
        for row in rejected_rows:
            reason = str(row.get("reject_reason") or row.get("reason") or "UNKNOWN").strip().upper() or "UNKNOWN"
            if reason not in guard_names:
                continue
            if not (_passes_score_rr_expectancy(row) or _safe_float_or_none(row.get("effective_rr")) is not None):
                continue
            breakdown[reason] += 1
            if len(examples) < 5:
                examples.append({k: row.get(k) for k in ("signal_id", "symbol", "side", "timestamp", "reject_reason", "score", "raw_rr", "rr", "effective_rr", "regime", "source_stage") if row.get(k) not in (None, "")})
    funnel = calibration_summary.get("rejection_funnel", {}) if isinstance(calibration_summary, Mapping) else {}
    later_count = _safe_int(funnel.get("rejected_by_later_gates")) or 0
    if later_count > 0 and not breakdown:
        breakdown["UNATTRIBUTED_LATER_GATE"] = later_count
        examples.append({"reject_reason": "UNATTRIBUTED_LATER_GATE", "count": later_count})
    return {
        "guardrail_reject_breakdown": dict(breakdown),
        "top_guardrail_reject_reasons": [{"reason": reason, "count": count} for reason, count in breakdown.most_common()],
        "representative_guardrail_reject_examples": examples[:5],
    }


def _canonical_gate_funnel_from_rejections(rejected_rows: list[dict[str, str]], accepted_count: int | None) -> list[dict[str, Any]]:
    gate_order = ["LOW_SCORE", "TOO_CHOPPY", "WEAK_TREND_AND_NO_RANGE_EDGE", "STOP_TOO_WIDE", "RR_TOO_LOW", "DAILY_SYMBOL_TRADE_LIMIT", "REGIME_MISMATCH", "PANIC_CONDITIONS"]
    counts = Counter(str(r.get("reject_reason") or r.get("reason") or "UNKNOWN").strip().upper() or "UNKNOWN" for r in rejected_rows)
    remaining = int(accepted_count or 0) + sum(counts.values())
    rows: list[dict[str, Any]] = []
    for gate in gate_order:
        rejected = counts.get(gate, 0)
        rows.append({
            "gate": gate,
            "candidates_entering_gate": remaining,
            "rejected_by_gate": rejected,
            "accepted_after_gate": max(int(accepted_count or 0), remaining - rejected),
            "funnel_scope": "canonical_rejected_orders_plus_executed_trades",
            "comparability_note": "Canonical dashboard funnel from rejected_orders.csv reject_reason counts and canonical accepted/executed trade count.",
            "zero_reject_warning": rejected == 0,
        })
        remaining = max(int(accepted_count or 0), remaining - rejected)
    return rows

def _bucket(value: Any, step: float = 1.0) -> str:
    val = _safe_float_or_none(value)
    if val is None:
        return "UNAVAILABLE"
    lo = int(val // step) * step
    hi = lo + step
    return f"{lo:g}-{hi:g}"


def _bucket_diagnostics(rows: list[dict[str, str]]) -> dict[str, list[dict[str, Any]]]:
    specs = {
        "symbol": lambda r: r.get("symbol") or "UNKNOWN",
        "side": lambda r: r.get("side") or "UNKNOWN",
        "regime": lambda r: r.get("regime") or "UNKNOWN",
        "score_bucket": lambda r: _bucket(r.get("score"), 1.0),
        "effective_rr_bucket": lambda r: _bucket(r.get("effective_rr"), 0.5),
        "raw_rr_bucket": lambda r: _bucket(r.get("raw_rr") or r.get("rr"), 0.5),
        "volatility_bucket": lambda r: _bucket(r.get("volatility_score") or r.get("volatility_pct"), 0.25),
        "liquidity_bucket": lambda r: _bucket(r.get("liquidity_score") or r.get("volume_24h_usdt"), 1.0),
        "spread_bucket": lambda r: _bucket(r.get("spread_pct"), 0.001),
        "expected_slippage_bucket": lambda r: _bucket(r.get("expected_slippage_pct"), 0.001),
        "stop_distance_pct_bucket": lambda r: _bucket(r.get("stop_distance_pct"), 0.005),
        "hour_session": lambda r: str(r.get("entry_time") or r.get("timestamp") or r.get("event_ts") or "UNAVAILABLE")[:13],
    }
    out: dict[str, list[dict[str, Any]]] = {}
    for name, keyfn in specs.items():
        groups: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in rows:
            groups[str(keyfn(row))].append(row)
        bucket_rows = []
        for bucket_name, bucket_rows_source in sorted(groups.items()):
            count = len(bucket_rows_source)
            win_count = sum(1 for r in bucket_rows_source if str(r.get("close_reason") or r.get("result")).upper() in {"TP_HIT", "WIN"})
            loss_count = sum(1 for r in bucket_rows_source if str(r.get("close_reason") or r.get("result")).upper() in {"SL_HIT", "LOSS"})
            timeout_count = sum(1 for r in bucket_rows_source if "TIMEOUT" in str(r.get("close_reason") or r.get("result")).upper())
            net = sum(_safe_float(r.get("net_pnl_usdt", r.get("net_pnl")), 0.0) for r in bucket_rows_source)
            recommendation = "INSUFFICIENT_SAMPLE" if count < 3 else ("KEEP" if net > 0 else ("PENALIZE" if win_count else "REJECT"))
            bucket_rows.append({"bucket": bucket_name, "count": count, "win_count": win_count, "loss_count": loss_count, "timeout_count": timeout_count, "net_pnl": net, "avg_net_pnl": net / count if count else 0.0, "profit_factor": _profit_factor(bucket_rows_source), "avg_effective_rr": _summary_value(bucket_rows_source, "effective_rr", "mean"), "avg_score": _summary_value(bucket_rows_source, "score", "mean"), "recommendation": recommendation})
        out[name] = bucket_rows
    return out


def _canonical_profile_accepted_rows(summary: Mapping[str, Any], lifecycle: list[dict[str, str]], backtest_orders: list[dict[str, str]]) -> tuple[int, list[dict[str, str]], str]:
    for field in ("accepted_count", "total_orders", "triggered_orders"):
        value = _safe_int(summary.get(field))
        if value is not None:
            if value <= 0:
                return 0, [], f"order_backtest_summary.csv:{field}"
            lifecycle_exec_rows = _accepted_trade_rows(lifecycle)
            if not lifecycle_exec_rows:
                lifecycle_exec_rows = [r for r in lifecycle if _safe_float_or_none(r.get("net_pnl_usdt", r.get("net_pnl"))) is not None and str(r.get("reject_reason") or "").strip() == ""]
            rows = backtest_orders[:value] if backtest_orders else lifecycle_exec_rows[:value]
            return value, rows, f"order_backtest_summary.csv:{field}"
    executed_states = {"POSITION_CLOSED", "POSITION_OPENED", "ORDER_PLACED", "ENTRY_TRIGGERED"}
    disallowed_states = {"SIGNAL_CREATED", "SIGNAL_REJECTED", "SYMBOL_REJECTED", "ORDER_REJECTED"}
    rows = []
    for row in _accepted_trade_rows(lifecycle):
        state = str(row.get("lifecycle_state") or row.get("status_after") or "").strip().upper()
        decision = str(row.get("decision") or row.get("status") or "").strip().upper()
        rejected = bool(str(row.get("reject_reason") or row.get("cancel_reason") or "").strip())
        if state in executed_states and state not in disallowed_states and decision in {"ACCEPTED", "EXECUTED"} and not rejected:
            rows.append(row)
    return len(rows), rows, "order_lifecycle.csv:accepted_executed_states"


def _comparison_metrics(profile: str, profile_dir: Path, initial_balance: float, warnings: list[str] | None = None, window_days: float | None = None) -> dict[str, Any]:
    summary = _read_first_csv_row(profile_dir / "order_backtest_summary.csv")
    lifecycle = _read_csv_rows(profile_dir / "order_lifecycle.csv")
    rejected = _read_csv_rows(profile_dir / "rejected_orders.csv")
    backtest_orders = _read_csv_rows(profile_dir / "backtest_orders.csv")
    filter_state_path = profile_dir / "backtest_filter_state.json"
    filter_state = json.loads(filter_state_path.read_text()) if filter_state_path.exists() else {}
    accepted_count, accepted, accepted_source = _canonical_profile_accepted_rows(summary, lifecycle, backtest_orders)
    no_trades = accepted_count == 0
    net = 0.0 if no_trades else _safe_float(summary.get("total_net_pnl_usdt"), 0.0)
    loss_count = 0 if no_trades else (_safe_int(summary.get("sl_hits")) or 0)
    win_count = 0 if no_trades else (_safe_int(summary.get("tp_hits")) or 0)
    rejected_count = _safe_int(summary.get("rejected_count") or summary.get("total_rejected")) or len(rejected)
    max_dd = _safe_float_or_none(summary.get("max_drawdown"))
    max_losses = _max_consecutive_losses(accepted)
    avg_per_day = accepted_count / max(1.0, float(window_days or _safe_int(summary.get("last_days")) or 1))
    drawdown_penalty = abs(max_dd) if max_dd is not None else 0.0
    components = {
        "raw_net_pnl": net,
        "max_drawdown_penalty": drawdown_penalty,
        "loss_streak_penalty": max(0, max_losses - 2) * max(abs(net) * 0.05, 1.0),
        "overtrade_penalty": max(0.0, avg_per_day - 3.0) * max(abs(net) * 0.02, 1.0),
        "execution_cost_penalty": abs(_summary_value(accepted, "cost_penalty", "mean") or 0.0) * accepted_count,
        "low_sample_penalty": max(0, 5 - accepted_count) * 2.0,
        "no_executed_trade_penalty": 1000000.0 if no_trades else 0.0,
    }
    components["final_objective_score"] = components["raw_net_pnl"] - sum(v for k, v in components.items() if k != "raw_net_pnl")
    score10 = [r for r in accepted if (_safe_float_or_none(r.get("score")) or 0) >= 10]
    warn = list(warnings or [])
    if profile == "ALL_FILTERS_OFF":
        warn.append("FILTERS_OFF_STRESS_TEST")
    if max_dd is None:
        warn.append("DRAWDOWN_UNAVAILABLE")
    if no_trades:
        warn.extend(["NO_EXECUTED_TRADES", "NO_ACCEPTED_TRADES"])
    elif accepted_count < 5:
        warn.append("LOW_SAMPLE_RISK")
    if accepted_count > 0 and avg_per_day > 3:
        warn.append("OVERTRADE_RISK")
    if max_losses >= 3:
        warn.append("HIGH_LOSS_STREAK_RISK")
    if len(score10) >= max(3, accepted_count * 0.5):
        warn.append("SCORE_SATURATION_RISK")
    diagnostics = _rejection_diagnostics(rejected)
    return {
        "profile_name": profile, "filter_profile": filter_state.get("filter_profile", profile), "status": "COMPLETED",
        "enabled_filters": filter_state.get("enabled_filters", []), "disabled_filters": filter_state.get("disabled_filters", []),
        "hard_safety_gates": filter_state.get("hard_safety_gates", []), "candidates": _safe_int(summary.get("total_candidates")),
        "accepted_trades": accepted_count, "accepted_trades_source": accepted_source, "lifecycle_event_count": len(lifecycle), "rejected_row_count": len(rejected),
        "rejected_signals": rejected_count, "reject_rate": rejected_count / max(1, accepted_count + rejected_count),
        "win_count": win_count, "loss_count": loss_count, "open_count": 0 if no_trades else (_safe_int(summary.get("open_at_end")) or 0), "timeout_count": 0 if no_trades else _safe_int(summary.get("timeout_count")),
        "gross_pnl": _safe_float_or_none(summary.get("total_gross_pnl_usdt")), "net_pnl": net, "return_pct": _safe_float_or_none(summary.get("total_pnl_pct")), "return": _safe_float_or_none(summary.get("total_pnl_pct")),
        "max_drawdown": max_dd, "max_drawdown_status": "AVAILABLE" if max_dd is not None else "UNAVAILABLE", "max_consecutive_losses": max_losses,
        "profit_factor": _profit_factor(accepted), "avg_win": _avg_net([r for r in accepted if _safe_float(r.get("net_pnl_usdt", r.get("net_pnl")), 0.0) > 0]),
        "avg_loss": _avg_net([r for r in accepted if _safe_float(r.get("net_pnl_usdt", r.get("net_pnl")), 0.0) < 0]),
        "expectancy_per_trade": net / accepted_count if accepted_count else 0.0, "avg_trades_per_day": avg_per_day,
        "accepted_effective_rr_distribution": _numeric_distribution(accepted, "effective_rr"), "rejected_effective_rr_distribution": _numeric_distribution(rejected, "effective_rr"),
        "score_10_count": len(score10), "score_10_tp_count": sum(1 for r in score10 if str(r.get("close_reason")).upper() == "TP_HIT"), "score_10_sl_count": sum(1 for r in score10 if str(r.get("close_reason")).upper() == "SL_HIT"), "score_10_timeout_count": sum(1 for r in score10 if "TIMEOUT" in str(r.get("close_reason")).upper()), "score_10_net_pnl": sum(_safe_float(r.get("net_pnl_usdt", r.get("net_pnl")), 0.0) for r in score10),
        "top_reject_reasons": diagnostics["top_rejection_reasons"], "objective_score": components, "warnings": sorted(set(warn)),
        "bucket_diagnostics": _bucket_diagnostics(accepted), "artifact_paths": {"directory": str(profile_dir), "summary": str(profile_dir / "order_backtest_summary.csv"), "lifecycle": str(profile_dir / "order_lifecycle.csv"), "rejected": str(profile_dir / "rejected_orders.csv")},
    }


def _max_consecutive_losses(rows: list[Mapping[str, Any]]) -> int:
    best = cur = 0
    for row in rows:
        is_loss = str(row.get("close_reason") or row.get("result")).upper() in {"SL_HIT", "LOSS"}
        cur = cur + 1 if is_loss else 0
        best = max(best, cur)
    return best


def run_dashboard_backtest(request: DashboardBacktestRequest) -> DashboardBacktestResult:
    """Run the existing backtest_order.py pipeline with a BACKTEST-only command boundary."""
    cfg = load_config_from_env()
    timestamp = canonical_utc_timestamp().replace(":", "").replace("-", "").replace(".", "")
    output_dir = Path(cfg.backtest.output_dir) / "dashboard" / timestamp
    if getattr(cfg.backtest, "export_config_snapshot", True):
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "config_snapshot.json").write_text(json.dumps({"mode": "BACKTEST", "config_snapshot": config_snapshot(mode="BACKTEST")}, indent=2, sort_keys=True))
    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "backtest_order.py"
    explicit_symbols = [symbol for symbol in request.symbols if str(symbol).strip()]
    symbols = explicit_symbols[: request.max_symbols]
    fixed_end = canonical_utc_timestamp()
    fixed_start_ms = parse_dashboard_window_start_ms(fixed_end, request.last_days)
    base_command = [
        sys.executable,
        str(script),
        "--mode",
        "BACKTEST",
        "--last-n-days",
        str(request.last_days),
        "--start",
        str(fixed_start_ms),
        "--end",
        fixed_end,
        "--max-symbols",
        str(request.max_symbols),
        "--interval",
        request.timeframe,
        "--balance",
        str(request.initial_balance),
        "--output-dir",
        str(output_dir),
        "--force-refresh",
    ]
    effective_filter_switches = request.filter_switches or dict(default_form_values()["filter_switches"])
    command = list(base_command)
    if symbols:
        command.extend(["--symbols", ",".join(symbols)])
    for reason, enabled in effective_filter_switches.items():
        if not enabled:
            command.extend(["--disable-backtest-filter", reason])
    if request.short_breakdown_rescue_enabled:
        command.append("--rescue-enabled")
    period = f"last {request.last_days} days"
    result = DashboardBacktestResult("RUNNING", period, symbols, request.timeframe, request.initial_balance, request.max_symbols, output_dir=str(output_dir), command=command)
    result.disabled_filters = [reason for reason, enabled in effective_filter_switches.items() if not enabled]
    result.enabled_filters = [reason for reason, enabled in effective_filter_switches.items() if enabled]
    result.short_breakdown_rescue_enabled = bool(request.short_breakdown_rescue_enabled)
    result.filter_switch_experiment_active = bool(result.disabled_filters)
    run_metadata_path = output_dir / "backtest_run_metadata.json"
    run_metadata = {
        "mode": "BACKTEST",
        "status": "RUNNING",
        "requested_timeframe": request.timeframe,
        "effective_timeframe": request.timeframe,
        "requested_last_n_days": request.last_days,
        "effective_start": fixed_start_ms,
        "effective_end": fixed_end,
        "symbols": symbols,
        "symbol_universe_mode": "EXPLICIT" if symbols else "DYNAMIC_TOP_VOLUME",
        "requested_max_symbols": request.max_symbols,
        "requested_profile": "CUSTOM" if result.disabled_filters else "DEFAULT",
        "enabled_optional_filters": result.enabled_filters,
        "disabled_optional_filters": result.disabled_filters,
        "filter_state_applied_before_failure": True,
        "failure_reason": None,
    }
    _write_backtest_run_metadata(run_metadata_path, run_metadata)
    if request.run_profile_comparison:
        profiles_root = output_dir / "profiles"
        comparison_profiles: dict[str, Any] = {}
        profile_sequence = ["DEFAULT_FILTERS", "ALL_FILTERS_OFF", "STRICT_FILTERS", "CUSTOM_CURRENT_UI", "SCORE_SATURATION_GUARD_DIAGNOSTIC", "STOP_WIDTH_GUARD_DIAGNOSTIC", "TRADE_FREQUENCY_GUARD_DIAGNOSTIC"]
        base_env = os.environ.copy()
        base_env["ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED"] = "true" if request.short_breakdown_rescue_enabled else "false"
        for profile in profile_sequence:
            profile_dir = profiles_root / profile
            profile_dir.mkdir(parents=True, exist_ok=True)
            disabled = PROFILE_FILTERS.get(profile, [])
            warnings: list[str] = []
            if profile == "CUSTOM_CURRENT_UI":
                disabled = [reason for reason, enabled in effective_filter_switches.items() if not enabled]
            if profile == "ALL_FILTERS_OFF":
                warnings.append("FILTERS_OFF_STRESS_TEST")
            if profile == "TRADE_FREQUENCY_GUARD_DIAGNOSTIC":
                warnings.extend(["DIAGNOSTIC_MAX_1_TRADE_PER_DAY_NOT_ENFORCED", "DIAGNOSTIC_MAX_2_TRADES_PER_DAY_NOT_ENFORCED", "DIAGNOSTIC_MAX_3_TRADES_PER_DAY_NOT_ENFORCED", "DIAGNOSTIC_PAUSE_AFTER_2_CONSECUTIVE_SL_NOT_ENFORCED"])
            profile_command = [arg if arg != str(output_dir) else str(profile_dir) for arg in base_command]
            for reason in disabled:
                profile_command.extend(["--disable-backtest-filter", reason])
            if request.short_breakdown_rescue_enabled:
                profile_command.append("--rescue-enabled")
            profile_timeout = _safe_subprocess_timeout(DASHBOARD_BACKTEST_SUBPROCESS_TIMEOUT_SECONDS)
            try:
                completed = subprocess.run(profile_command, cwd=repo_root, text=True, capture_output=True, timeout=profile_timeout, check=False, env=base_env)
            except subprocess.TimeoutExpired:
                _write_profile_timeout_metadata(profile_dir, profile, profile_command, profile_timeout)
                comparison_profiles[profile] = _timeout_profile_metrics(profile, profile_dir, profile_command, profile_timeout, warnings)
                result.status = "PARTIAL"
                result.error_message = f"Profile {profile} timed out. Completed profiles are still available."
                continue
            if completed.returncode != 0:
                result.status = "FAILED"
                result.error_message = (completed.stderr or completed.stdout or f"{profile} failed")[-1200:]
                return result
            comparison_profiles[profile] = _comparison_metrics(profile, profile_dir, request.initial_balance, warnings, window_days=request.last_days)
        raw_sorted = sorted(comparison_profiles.values(), key=lambda r: (int(r.get("accepted_trades") or 0) > 0, r.get("objective_score", {}).get("raw_net_pnl", 0.0) or 0.0), reverse=True)
        obj_sorted = sorted(comparison_profiles.values(), key=lambda r: (int(r.get("accepted_trades") or 0) > 0, r.get("objective_score", {}).get("final_objective_score", 0.0) or 0.0), reverse=True)
        raw_rank = {r["profile_name"]: i + 1 for i, r in enumerate(raw_sorted)}
        obj_rank = {r["profile_name"]: i + 1 for i, r in enumerate(obj_sorted)}
        leaderboard = []
        for row in comparison_profiles.values():
            leaderboard.append({
                "profile_name": row["profile_name"], "raw_net_pnl": row["objective_score"]["raw_net_pnl"], "final_objective_score": row["objective_score"]["final_objective_score"],
                "raw_net_pnl_rank": raw_rank[row["profile_name"]], "objective_score_rank": obj_rank[row["profile_name"]],
                "accepted_trades": row["accepted_trades"], "win_count": row["win_count"], "loss_count": row["loss_count"], "open_count": row["open_count"], "status": row.get("status", "COMPLETED"),
                "avg_trades_per_day": row["avg_trades_per_day"], "score_10_tp_count": row["score_10_tp_count"], "score_10_sl_count": row["score_10_sl_count"], "warnings": row["warnings"],
            })
        comparison_status = "PARTIAL" if any(row.get("status") == "TIMEOUT" for row in comparison_profiles.values()) else "COMPLETED"
        comparison = {"mode": "BACKTEST", "comparison_mode": True, "status": comparison_status, "windows": {"30": "RUN" if request.last_days == 30 else "NOT_RUN", "90": "RUN" if request.last_days == 90 else "NOT_RUN", "180": "RUN" if request.last_days == 180 else "NOT_RUN", "365": "RUN" if request.last_days == 365 else "NOT_RUN"}, "profiles": comparison_profiles}
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "backtest_filter_profile_comparison.json").write_text(json.dumps(comparison, indent=2, sort_keys=True))
        (output_dir / "backtest_profile_leaderboard.json").write_text(json.dumps({"mode": "BACKTEST", "leaderboard": leaderboard}, indent=2, sort_keys=True))
        with (output_dir / "backtest_profile_leaderboard.csv").open("w", newline="") as fh:
            fields = ["profile_name", "status", "raw_net_pnl", "final_objective_score", "raw_net_pnl_rank", "objective_score_rank", "accepted_trades", "win_count", "loss_count", "open_count", "avg_trades_per_day", "score_10_tp_count", "score_10_sl_count", "warnings"]
            writer = csv.DictWriter(fh, fieldnames=fields); writer.writeheader()
            for row in leaderboard:
                writer.writerow({**row, "warnings": json.dumps(row["warnings"], sort_keys=True)})
        result.status = comparison_status
        run_metadata.update({"status": comparison_status, "failure_reason": "PROFILE_TIMEOUT" if comparison_status == "PARTIAL" else None})
        _write_backtest_run_metadata(run_metadata_path, run_metadata)
        result.profile_comparison = comparison
        result.profile_leaderboard = sorted(leaderboard, key=lambda r: r["objective_score_rank"])
        result.filter_profile_comparison_path = str(output_dir / "backtest_filter_profile_comparison.json")
        result.profile_leaderboard_path = str(output_dir / "backtest_profile_leaderboard.json")
        _apply_backtest_artifact_model(result, output_dir, selected_profile_name="DEFAULT_FILTERS", window_days=request.last_days)
        return result
    try:
        run_env = os.environ.copy()
        run_env["ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED"] = "true" if request.short_breakdown_rescue_enabled else "false"
        completed = subprocess.run(command, cwd=repo_root, text=True, capture_output=True, timeout=_safe_subprocess_timeout(DASHBOARD_BACKTEST_SUBPROCESS_TIMEOUT_SECONDS), check=False, env=run_env)
    except Exception as exc:  # subprocess/environment failure, not strategy logic
        result.status = "FAILED"
        result.error_message = f"Backtest failed before completion: {exc}"
        return result
    if completed.returncode != 0:
        result.status = "FAILED"
        stderr = (completed.stderr or completed.stdout or "BACKTEST_PROCESS_FAILED").strip()
        failure_reason, message = _classify_backtest_failure(stderr)
        result.error_message = message
        run_metadata.update({"status": "FAILED", "failure_reason": failure_reason, "failure_detail": stderr[-1200:]})
        _write_backtest_run_metadata(run_metadata_path, run_metadata)
        if failure_reason == "UNSUPPORTED_TIMEFRAME":
            result.lifecycle_warning = "SELECTED_BACKTEST_UNAVAILABLE_DUE_TO_FAILURE"
            result.execution_context_warning = "SELECTED_BACKTEST_UNAVAILABLE_DUE_TO_FAILURE"
        return result

    _apply_backtest_artifact_model(result, output_dir, window_days=request.last_days)
    summary_path = output_dir / "order_backtest_summary.csv"
    lifecycle_path = output_dir / "order_lifecycle.csv"
    rejected_path = output_dir / "rejected_orders.csv"
    rejected_shadow_path = output_dir / "rejected_shadow.csv"
    backtest_orders_path = output_dir / "backtest_orders.csv"
    signal_quality_summary_path = output_dir / "signal_quality_summary.json"
    filter_state_path = output_dir / "backtest_filter_state.json"
    filter_comparison_path = output_dir / "backtest_filter_profile_comparison.json"
    accepted_loss_path = output_dir / "accepted_trade_loss_diagnostics.json"
    summary = _read_first_csv_row(summary_path)
    lifecycle_rows = _read_csv_rows(lifecycle_path)
    rejected_rows = _read_csv_rows(rejected_path)
    rejected_shadow_rows = _read_csv_rows(rejected_shadow_path)
    backtest_order_rows = _read_csv_rows(backtest_orders_path)
    signal_quality_summary = json.loads(signal_quality_summary_path.read_text()) if signal_quality_summary_path.exists() and signal_quality_summary_path.stat().st_size else {}
    filter_state = json.loads(filter_state_path.read_text()) if filter_state_path.exists() and filter_state_path.stat().st_size else {}
    result.status = "COMPLETED"
    run_metadata.update({"status": "COMPLETED", "failure_reason": None})
    _write_backtest_run_metadata(run_metadata_path, run_metadata)
    exported_symbols = _selected_symbols_from_summary(summary)
    if exported_symbols:
        result.symbols = exported_symbols
    result.summary_path = str(summary_path) if summary_path.exists() else None
    result.lifecycle_path = str(lifecycle_path) if lifecycle_path.exists() else None
    result.rejected_path = str(rejected_path) if rejected_path.exists() else None
    result.total_candidates = _safe_int(summary.get("total_candidates"))
    result.accepted_trades = _safe_int(summary.get("accepted_count"))
    rejected_summary_count = _safe_int(summary.get("rejected_count") or summary.get("total_rejected"))
    result.rejected_signals = len(rejected_rows) if rejected_rows else rejected_summary_count
    result.win_count = _safe_int(summary.get("tp_hits"))
    result.loss_count = _safe_int(summary.get("sl_hits"))
    result.open_count = _safe_int(summary.get("open_at_end"))
    result.net_pnl = summary.get("total_net_pnl_usdt")
    result.baseline_accepted_count = _safe_int(summary.get("baseline_accepted_trades"))
    result.rescue_accepted_count = _safe_int(summary.get("rescue_accepted_count"))
    result.baseline_net_pnl = summary.get("baseline_net_pnl")
    result.rescue_net_pnl = summary.get("rescue_accepted_net_pnl")
    result.baseline_plus_rescue_net_pnl = summary.get("baseline_plus_rescue_net_pnl")
    try:
        result.accepted_reason_breakdown = json.loads(summary.get("accepted_reason_breakdown", "{}") or "{}")
    except Exception:
        result.accepted_reason_breakdown = {}
    result.short_breakdown_rescue_enabled = str(summary.get("short_breakdown_rescue_enabled", result.short_breakdown_rescue_enabled)).lower() in {"1", "true", "yes", "on"}
    result.total_return_pct = summary.get("total_pnl_pct")
    result.max_drawdown = None
    diagnostics = _rejection_diagnostics(rejected_rows)
    result.top_rejection_reasons = diagnostics["top_rejection_reasons"]
    result.signal_rows_count = diagnostics["signal_rows_count"]
    result.symbol_selector_reject_count = diagnostics["symbol_selector_reject_count"]
    result.score_distribution = diagnostics["score_distribution"]
    result.rr_distribution = diagnostics["rr_distribution"]
    result.effective_rr_distribution = diagnostics["effective_rr_distribution"]
    result.pre_later_gate_pass_count = diagnostics["pre_later_gate_pass_count"]
    lifecycle_diagnostics = _lifecycle_diagnostics(lifecycle_rows, rejected_rows)
    result.lifecycle_state_counts = lifecycle_diagnostics["lifecycle_state_counts"]
    result.lifecycle_path_counts = lifecycle_diagnostics["lifecycle_path_counts"]
    result.final_reject_reason_counts = lifecycle_diagnostics["final_reject_reason_counts"]
    result.order_reject_reason_counts = lifecycle_diagnostics["order_reject_reason_counts"]
    result.symbol_selector_reject_counts = lifecycle_diagnostics["symbol_selector_reject_counts"]
    calibration_report_path, calibration_summary_path, calibration_summary = _write_calibration_artifacts(output_dir, lifecycle_rows, rejected_rows, summary, rejected_shadow_rows, backtest_order_rows)
    result.calibration_report_path = str(calibration_report_path)
    result.calibration_summary_path = str(calibration_summary_path)
    result.rejection_funnel = calibration_summary["rejection_funnel"]
    result.later_gate_diagnostics = calibration_summary["later_gate_diagnostics"]
    result.low_score_shadow_comparison = calibration_summary["low_score_shadow_comparison"]
    result.execution_cost_summary = calibration_summary["execution_cost_summary"]
    result.near_miss_rejected_signals = calibration_summary["near_miss_rejected_signals"]
    result.accepted_trade_diagnostics = calibration_summary["accepted_trade_diagnostics"]
    result.accepted_score_distribution = calibration_summary["accepted_score_distribution"]
    result.accepted_effective_rr_distribution = calibration_summary["accepted_effective_rr_distribution"]
    result.near_miss_score_distribution = calibration_summary["near_miss_score_distribution"]
    result.near_miss_effective_rr_distribution = calibration_summary["near_miss_effective_rr_distribution"]
    result.stop_too_wide_rescue_diagnostics = calibration_summary["stop_too_wide_rescue_diagnostics"]
    result.signal_quality_diagnostics.setdefault("stop_too_wide_recoverable_candidates", calibration_summary.get("stop_too_wide_recoverable_candidates", {}))
    result.score_saturation_diagnostics = calibration_summary["score_saturation_diagnostics"]
    result.daily_global_trade_limit_diagnostics = calibration_summary["daily_global_trade_limit_diagnostics"]
    result.dynamic_trade_limit_proposal = calibration_summary["dynamic_trade_limit_proposal"]
    result.signal_quality_diagnostics = signal_quality_summary
    result.high_effective_rr_missed_alpha = signal_quality_summary.get("high_effective_rr_missed_alpha", []) if isinstance(signal_quality_summary, dict) else []
    result.stop_too_wide_quality_split = signal_quality_summary.get("stop_too_wide_split", {}) if isinstance(signal_quality_summary, dict) else {}
    result.top_quality_improvement_candidates = signal_quality_summary.get("top_quality_improvement_candidates", []) if isinstance(signal_quality_summary, dict) else []
    if isinstance(filter_state, dict) and filter_state:
        result.filter_state_path = str(filter_state_path)
        result.filter_profile = str(filter_state.get("filter_profile", result.filter_profile))
        result.enabled_filters = list(filter_state.get("enabled_filters", []))
        result.disabled_filters = list(filter_state.get("disabled_filters", result.disabled_filters))
        result.hard_safety_gates = [str(g.get("filter_name")) for g in filter_state.get("hard_safety_gates", []) if isinstance(g, dict)]
        result.filter_warning = str(filter_state.get("all_off_warning", "") or "")
        experiment_state = filter_state.get("experiments", {}).get("SHORT_BREAKDOWN_RESCUE", {}) if isinstance(filter_state.get("experiments", {}), dict) else {}
        if experiment_state:
            result.short_breakdown_rescue_enabled = str(experiment_state.get("enabled", result.short_breakdown_rescue_enabled)).lower() in {"1", "true", "yes", "on"}
    result.filter_profile_comparison_path = str(filter_comparison_path) if filter_comparison_path.exists() else None
    result.accepted_loss_diagnostics_path = str(accepted_loss_path) if accepted_loss_path.exists() else None
    if summary.get("disabled_filters"):
        try:
            result.disabled_filters = list(json.loads(summary.get("disabled_filters", "[]")))
        except Exception:
            pass
    result.filter_switch_experiment_active = str(summary.get("filter_switch_experiment_active", result.filter_switch_experiment_active)).lower() in {"1", "true", "yes", "on"}
    result.filter_profile = str(summary.get("filter_profile", result.filter_profile) or result.filter_profile)
    if result.rejected_signals is not None and result.accepted_trades is not None:
        denom = result.rejected_signals + result.accepted_trades
        result.backtest_rejection_rate = (result.rejected_signals / denom) if denom else None
    if result.total_candidates is None or result.rejected_signals is None:
        result.lifecycle_warning = "Lifecycle/reject metrics unavailable from generated backtest artifacts; values are shown as unavailable, not zero."
    unavailable_markers = {"", "UNAVAILABLE_BACKTEST", "None", "null"}
    if not lifecycle_rows or any(str(row.get("spread_pct", "")).strip() in unavailable_markers for row in lifecycle_rows[:50]):
        result.execution_context_warning = "Execution context is incomplete for at least part of this backtest; unknown spread/slippage/funding is unavailable, not assumed zero."
    return result
