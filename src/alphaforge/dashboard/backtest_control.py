from __future__ import annotations

import csv
import json
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from alphaforge.config import load_config_from_env
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
    backtest_rejection_rate: float | None = None


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
    if errors:
        return None, errors
    return DashboardBacktestRequest(last_days=last_days, symbols=symbols, timeframe=timeframe, initial_balance=initial_balance, max_symbols=max_symbols), {}


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




def _accepted_trade_rows(lifecycle_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    accepted_states = {"WAITING_ENTRY_ZONE", "ENTRY_TRIGGERED", "ORDER_PLACED", "PARTIAL_FILL", "FILLED", "TP_HIT", "SL_HIT", "CANCELLED", "OPEN_AT_END", "POSITION_CLOSED"}
    by_signal: dict[str, dict[str, str]] = {}
    state_rank = {state: idx for idx, state in enumerate(("WAITING_ENTRY_ZONE", "ENTRY_TRIGGERED", "ORDER_PLACED", "PARTIAL_FILL", "FILLED", "TP_HIT", "SL_HIT", "CANCELLED", "OPEN_AT_END", "POSITION_CLOSED"), start=1)}
    for row in lifecycle_rows:
        state = str(row.get("lifecycle_state") or row.get("status_after") or "").strip().upper()
        decision = str(row.get("decision") or "").strip().upper()
        if decision != "ACCEPTED" and state not in accepted_states:
            continue
        signal_id = str(row.get("signal_id") or f"{row.get('symbol','')}:{row.get('timestamp') or row.get('event_ts','')}").strip()
        current = by_signal.get(signal_id)
        if current is None or state_rank.get(state, 0) >= state_rank.get(str(current.get("lifecycle_state") or current.get("status_after") or "").upper(), 0):
            by_signal[signal_id] = row
    return list(by_signal.values())


def _accepted_trade_diagnostics(lifecycle_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    rows = []
    for row in _accepted_trade_rows(lifecycle_rows):
        rows.append({
            "signal_id": row.get("signal_id"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "score": row.get("score"),
            "raw_rr": row.get("raw_rr") if row.get("raw_rr") not in (None, "") else row.get("rr"),
            "effective_rr": row.get("effective_rr"),
            "regime": row.get("regime") or row.get("volatility_regime"),
            "entry": row.get("entry"),
            "exit": row.get("exit") or row.get("exit_price"),
            "result": row.get("result") or row.get("outcome") or row.get("lifecycle_state") or row.get("status_after"),
            "net_pnl": row.get("net_pnl") or row.get("net_pnl_usdt") or row.get("pnl") or row.get("pnl_usdt"),
        })
    return rows

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


def _identity_values(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(row.get(field) or "").strip() for field in ("symbol", "timestamp", "event_ts", "side", "entry", "stop_loss", "take_profit"))


def _shadow_lookup_key(row: Mapping[str, Any]) -> tuple[str, ...] | None:
    signal_id = str(row.get("signal_id") or "").strip()
    if signal_id:
        return ("signal_id", signal_id)
    values = _identity_values(row)
    if any(values):
        return ("composite", *values)
    return None


def _build_shadow_lookup(shadow_rows: list[dict[str, str]]) -> dict[tuple[str, ...], dict[str, str]]:
    lookup: dict[tuple[str, ...], dict[str, str]] = {}
    for row in shadow_rows:
        key = _shadow_lookup_key(row)
        if key is not None:
            lookup[key] = row
        signal_id = str(row.get("signal_id") or "").strip()
        if signal_id:
            lookup[("signal_id", signal_id)] = row
        composite = _identity_values(row)
        if any(composite):
            lookup[("composite", *composite)] = row
    return lookup


def _matching_shadow(row: Mapping[str, Any], lookup: Mapping[tuple[str, ...], dict[str, str]]) -> dict[str, str] | None:
    signal_id = str(row.get("signal_id") or "").strip()
    if signal_id and (match := lookup.get(("signal_id", signal_id))):
        return match
    composite = _identity_values(row)
    if any(composite):
        return lookup.get(("composite", *composite))
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


def _build_calibration_outputs(lifecycle_rows: list[dict[str, str]], rejected_rows: list[dict[str, str]], summary: Mapping[str, Any], shadow_rows: list[dict[str, str]] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    shadow_source = list(shadow_rows or [])
    shadow_lookup = _build_shadow_lookup(shadow_source)
    enriched_rejected = [_merge_shadow(r, _matching_shadow(r, shadow_lookup)) for r in rejected_rows]
    shadow_signal_rows = [r for r in shadow_source if _source_stage(r) == "SIGNAL_ENGINE" and str(r.get("reject_reason") or "").strip()]
    shadow_diagnostic_rows = shadow_signal_rows or [r for r in enriched_rejected if _source_stage(r) == "SIGNAL_ENGINE"]
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
    passed_later = [r for r in signal_rejected if _passes_score_rr_expectancy(r)]
    later_gate_reasons = {"DAILY_SYMBOL_TRADE_LIMIT", "REGIME_MISMATCH", "RR_TOO_LOW", "STOP_TOO_WIDE"}
    later_gate_source = [r for r in shadow_diagnostic_rows if str(r.get("reject_reason") or "").strip().upper() in later_gate_reasons]
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
    summary_out = {
        "rejection_funnel": funnel,
        "later_gate_diagnostics": later_gate,
        "low_score_shadow_comparison": low_cmp,
        "execution_cost_summary": {"cost_penalty": _numeric_distribution(shadow_source or signal_rejected, "cost_penalty"), "spread_pct": _numeric_distribution(signal_rejected, "spread_pct"), "expected_slippage_pct": _numeric_distribution(signal_rejected, "expected_slippage_pct"), "spread_label": "ESTIMATED_BACKTEST_SPREAD when spread_source/status is estimated and historical bid/ask is unavailable"},
        "near_miss_rejected_signals": [dict({k: (r.get(k) if r.get(k) not in (None, "") else ("UNAVAILABLE" if k in {"shadow_outcome", "cost_penalty"} else r.get(k))) for k in ("signal_id", "symbol", "reject_reason", "score", "raw_rr", "rr", "effective_rr", "cost_penalty", "shadow_outcome", "spread_pct", "expected_slippage_pct", "liquidity_ok", "volatility_ok")}) for r in near],
        "accepted_trade_diagnostics": _accepted_trade_diagnostics(lifecycle_rows),
        "accepted_score_distribution": _numeric_distribution(accepted_rows, "score"),
        "accepted_effective_rr_distribution": _numeric_distribution(accepted_rows, "effective_rr"),
        "near_miss_score_distribution": _numeric_distribution(near, "score"),
        "near_miss_effective_rr_distribution": _numeric_distribution(near, "effective_rr"),
    }
    return report_rows, summary_out


def _write_calibration_artifacts(output_dir: Path, lifecycle_rows: list[dict[str, str]], rejected_rows: list[dict[str, str]], summary: Mapping[str, Any], shadow_rows: list[dict[str, str]] | None = None) -> tuple[Path, Path, dict[str, Any]]:
    report_rows, summary_out = _build_calibration_outputs(lifecycle_rows, rejected_rows, summary, shadow_rows)
    report_path = output_dir / "lifecycle_calibration_report.csv"
    summary_path = output_dir / "lifecycle_calibration_summary.json"
    fieldnames = list(report_rows[0].keys()) if report_rows else ["source_stage", "lifecycle_state", "reject_reason", "symbol", "regime", "expectancy_bucket", "count"]
    with report_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(report_rows)
    summary_path.write_text(json.dumps(summary_out, indent=2, sort_keys=True))
    return report_path, summary_path, summary_out


def run_dashboard_backtest(request: DashboardBacktestRequest) -> DashboardBacktestResult:
    """Run the existing backtest_order.py pipeline with a BACKTEST-only command boundary."""
    cfg = load_config_from_env()
    timestamp = canonical_utc_timestamp().replace(":", "").replace("-", "").replace(".", "")
    output_dir = Path(cfg.backtest.output_dir) / "dashboard" / timestamp
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
    period = f"last {request.last_days} days"
    result = DashboardBacktestResult("RUNNING", period, symbols, request.timeframe, request.initial_balance, request.max_symbols, output_dir=str(output_dir), command=command)
    try:
        completed = subprocess.run(command, cwd=repo_root, text=True, capture_output=True, timeout=600, check=False)
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
    summary = _read_first_csv_row(summary_path)
    lifecycle_rows = _read_csv_rows(lifecycle_path)
    rejected_rows = _read_csv_rows(rejected_path)
    rejected_shadow_rows = _read_csv_rows(rejected_shadow_path)
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
    calibration_report_path, calibration_summary_path, calibration_summary = _write_calibration_artifacts(output_dir, lifecycle_rows, rejected_rows, summary, rejected_shadow_rows)
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
    if result.rejected_signals is not None and result.accepted_trades is not None:
        denom = result.rejected_signals + result.accepted_trades
        result.backtest_rejection_rate = (result.rejected_signals / denom) if denom else None
    if result.total_candidates is None or result.rejected_signals is None:
        result.lifecycle_warning = "Lifecycle/reject metrics unavailable from generated backtest artifacts; values are shown as unavailable, not zero."
    unavailable_markers = {"", "UNAVAILABLE_BACKTEST", "None", "null"}
    if not lifecycle_rows or any(str(row.get("spread_pct", "")).strip() in unavailable_markers for row in lifecycle_rows[:50]):
        result.execution_context_warning = "Execution context is incomplete for at least part of this backtest; unknown spread/slippage/funding is unavailable, not assumed zero."
    return result
