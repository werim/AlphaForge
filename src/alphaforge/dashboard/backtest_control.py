from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from alphaforge.config import load_config_from_env
from alphaforge.config_registry import config_snapshot
from alphaforge.contracts import canonical_utc_timestamp

SUPPORTED_TIMEFRAMES: tuple[str, ...] = ("1m", "15m", "1h", "4h", "1d")
INSUFFICIENT_BINANCE_DATA_MESSAGE = "Not enough historical data returned by Binance for the requested period. Try fewer days or a higher timeframe."


@dataclass(slots=True)
class DashboardBacktestRequest:
    last_days: int
    symbols: list[str]
    timeframe: str
    initial_balance: float
    max_symbols: int
    filter_switches: dict[str, bool] = field(default_factory=dict)
    short_breakdown_rescue_enabled: bool = False


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
    max_symbols = parse_int("max_symbols", default_form_values()["max_symbols"], 1, 200)
    symbols = [item.strip().upper() for item in str(form.get("symbols", "")).split(",") if item.strip()]
    if not symbols:
        errors["symbols"] = "symbols must contain at least one non-empty symbol."
    timeframe = str(form.get("timeframe", "")).strip()
    if timeframe not in SUPPORTED_TIMEFRAMES:
        errors["timeframe"] = f"timeframe must be one of: {', '.join(SUPPORTED_TIMEFRAMES)}."
    filter_reasons = default_form_values()["filter_reasons"]
    filter_switches = {reason: str(form.get(f"filter_{reason}", "")).lower() in {"1", "true", "on", "yes"} for reason in filter_reasons}
    short_breakdown_rescue_enabled = str(form.get("short_breakdown_rescue_enabled", "")).lower() in {"1", "true", "on", "yes"}
    if errors:
        return None, errors
    return DashboardBacktestRequest(last_days=last_days, symbols=symbols, timeframe=timeframe, initial_balance=initial_balance, max_symbols=max_symbols, filter_switches=filter_switches, short_breakdown_rescue_enabled=short_breakdown_rescue_enabled), {}


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
    symbols = request.symbols[: request.max_symbols]
    command = [
        sys.executable,
        str(script),
        "--mode",
        "BACKTEST",
        "--last-n-days",
        str(request.last_days),
        "--symbols",
        ",".join(symbols),
        "--top-n",
        str(request.max_symbols),
        "--interval",
        request.timeframe,
        "--balance",
        str(request.initial_balance),
        "--output-dir",
        str(output_dir),
        "--force-refresh",
    ]
    for reason, enabled in request.filter_switches.items():
        if not enabled:
            command.extend(["--disable-backtest-filter", reason])
    if request.short_breakdown_rescue_enabled:
        command.append("--rescue-enabled")
    period = f"last {request.last_days} days"
    result = DashboardBacktestResult("RUNNING", period, symbols, request.timeframe, request.initial_balance, request.max_symbols, output_dir=str(output_dir), command=command)
    result.disabled_filters = [reason for reason, enabled in request.filter_switches.items() if not enabled]
    result.short_breakdown_rescue_enabled = bool(request.short_breakdown_rescue_enabled)
    result.filter_switch_experiment_active = bool(result.disabled_filters)
    try:
        run_env = os.environ.copy()
        run_env["ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED"] = "true" if request.short_breakdown_rescue_enabled else "false"
        completed = subprocess.run(command, cwd=repo_root, text=True, capture_output=True, timeout=600, check=False, env=run_env)
    except Exception as exc:  # subprocess/environment failure, not strategy logic
        result.status = "FAILED"
        result.error_message = f"Backtest failed before completion: {exc}"
        return result
    if completed.returncode != 0:
        result.status = "FAILED"
        stderr = (completed.stderr or completed.stdout or "BACKTEST_PROCESS_FAILED").strip()
        if "HistoricalDataError" in stderr or "Historical coverage" in stderr or "No candles returned" in stderr or "Insufficient candles" in stderr:
            detail = stderr[-1200:]
            result.error_message = f"{INSUFFICIENT_BINANCE_DATA_MESSAGE} Details: {detail}"
        else:
            result.error_message = stderr[-1200:]
        return result

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
    result.summary_path = str(summary_path) if summary_path.exists() else None
    result.lifecycle_path = str(lifecycle_path) if lifecycle_path.exists() else None
    result.rejected_path = str(rejected_path) if rejected_path.exists() else None
    result.total_candidates = _safe_int(summary.get("total_candidates"))
    result.accepted_trades = _safe_int(summary.get("accepted_count"))
    result.rejected_signals = _safe_int(summary.get("rejected_count") or summary.get("total_rejected"))
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
