import argparse
import csv
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, asdict, field
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Mapping, Iterable
# Allow running this script directly from the repo root without requiring
# prior editable install.
ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
from alphaforge.execution import build_execution_context, build_execution_cost_model, normalize_pct_input
from alphaforge.config import load_config_from_env
from alphaforge.config_registry import decision_filter_config
from alphaforge.lifecycle_contract import normalize_lifecycle_event
from alphaforge.persistence import init_db, save_order_decision, save_signal, save_trade_lifecycle_event
from alphaforge.symbol_selector import select_symbol
from alphaforge.symbols import SymbolListError, normalize_symbol_list
from alphaforge.historical_market_data import (
    HistoricalCandle,
    HistoricalDataError,
    fetch_binance_klines_paginated,
    load_or_fetch_candles as load_or_fetch_historical_candles,
)
from sqlalchemy import text
from sqlalchemy.orm import Session
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import uuid5, NAMESPACE_URL


def resolve_csv_fieldnames(rows: List[Mapping[str, Any]], preferred_fieldnames: List[str]) -> List[str]:
    seen: set[str] = set()
    ordered: List[str] = []
    for name in preferred_fieldnames:
        if name not in seen:
            ordered.append(name)
            seen.add(name)

    extra_keys = sorted({key for row in rows for key in row.keys() if key not in seen})
    ordered.extend(extra_keys)
    return ordered


@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
@dataclass
class CandidateOrder:
    timestamp: int
    symbol: str
    side: str
    entry: float
    sl: float
    tp: float
    rr: float
    setup_type: str
    setup_reason: str
    regime: str
    score: float
    order_type: str
    expectancy_bucket: str = "UNKNOWN"
    accepted_reason: str = "BASELINE"
    original_reject_reason: str = ""
    rescue_size_multiplier: float = 1.0
    rescue_effective_rr: float = 0.0
    rescue_decision_context: str = ""
    bypassed_reject_reasons: str = ""
    disabled_filters: str = ""
    disabled_filter_bypass_count: int = 0
    filter_switch_experiment_active: bool = False
@dataclass
class RescueConfig:
    enabled: bool = False
    modes: tuple[str, ...] = ("BACKTEST",)
    effective_rr_min: float = 1.90
    score_min: float = 9.0
    size_multiplier: float = 0.25
    max_trades_per_day: int = 1
    allowed_reasons: tuple[str, ...] = ("STOP_TOO_WIDE", "DAILY_SYMBOL_TRADE_LIMIT")
    allow_regime_mismatch: bool = False
    max_spread_pct: float = 0.0025
    max_slippage_pct: float = 0.0020
    allow_cooldown_bypass: bool = False
    max_concurrent_positions: int = 3

@dataclass
class RescueStats:
    candidate_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    reject_reasons: Dict[str, int] = field(default_factory=dict)

QUALITY_GATE_NAME = "SHORT_BREAKDOWN_BREAKOUT_NORMAL_STOP_GATE"
SHORT_BREAKDOWN_RESCUE_REASON = "SHORT_BREAKDOWN_RESCUE"

@dataclass
class ShortBreakdownRescueConfig:
    enabled: bool = False
    modes: tuple[str, ...] = ("BACKTEST",)
    size_multiplier: float = 0.25
    max_trades_per_day: int = 1
    min_effective_rr: float = 1.10
    min_shadow_expectancy: float = 0.0
    allowed_reasons: tuple[str, ...] = ("LOW_SCORE", "STOP_TOO_WIDE", "DAILY_SYMBOL_TRADE_LIMIT")
    max_spread_pct: float = 0.0025
    max_slippage_pct: float = 0.0020

@dataclass
class QualityGateConfig:
    enabled: bool = False
    modes: tuple[str, ...] = ("BACKTEST",)
    size_multiplier: float = 0.25
    max_trades_per_day: int = 1
    min_effective_rr: float = 1.10
    min_score: Optional[float] = None
    allowed_gate_name: str = QUALITY_GATE_NAME
    max_spread_pct: float = 0.0025
    max_slippage_pct: float = 0.0020
    allowed_reasons: tuple[str, ...] = ("LOW_SCORE", "DAILY_SYMBOL_TRADE_LIMIT")


@dataclass
class StrategyQualityGuardrailConfig:
    enabled: bool = True
    profile: str = "DEFAULT_FILTERS"
    max_accepted_trades_per_day: int = 6
    max_symbol_trades_per_day: int = 2
    max_symbol_regime_trades_per_day: int = 1
    max_consecutive_sl_pause: int = 4
    score10_sl_dominance_guard: bool = True
    high_vol_acceptance_guard: bool = True
    saturated_score_threshold: float = 9.8
    saturated_min_effective_rr: float = 2.20
    saturated_max_cost_penalty: float = 0.20
    high_vol_min_effective_rr: float = 2.30
    high_vol_max_cost_penalty: float = 0.18
    high_vol_max_trades_per_day: int = 2
    min_profit_factor_for_profile_pass: float = 1.20
    max_loss_streak_for_profile_pass: int = 6
    max_drawdown_pct_for_profile_pass: float = 12.0


HIGH_VOL_GUARD_DIAGNOSTIC_WARNING = (
    "HIGH_VOL_GUARD_OFF_DIAGNOSTIC is not a production strategy profile. "
    "It measures guardrail impact only."
)

DIAGNOSTIC_PROFILE_NAME = "SHORT_LOW_SCORE_BREAKDOWN_DIAGNOSTIC"
DIAGNOSTIC_PROFILE_DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT")
DIAGNOSTIC_PROFILE_GOOD_HOUR_GROUP = "SHORT_LOW_SCORE_GOOD_UTC_HOURS"
DIAGNOSTIC_PROFILE_REASON = (
    "DIAGNOSTIC ONLY: BACKTEST-only shadow validation for SHORT BREAKDOWN_DOWN rows rejected by LOW_SCORE "
    "in historically favorable UTC hours. Production thresholds unchanged; PAPER/LIVE unchanged; "
    "HIGH_VOL_GUARD, STOP_TOO_WIDE, execution-cost, effective-RR, liquidity, and geometry sanity remain active."
)


def diagnostic_short_low_score_symbols_from_env() -> tuple[str, ...]:
    raw = os.getenv("ALPHAFORGE_BACKTEST_SHORT_LOW_SCORE_BREAKDOWN_DIAGNOSTIC_SYMBOLS", "")
    symbols = tuple(s.strip().upper() for s in raw.replace(",", " ").split() if s.strip())
    return symbols or DIAGNOSTIC_PROFILE_DEFAULT_SYMBOLS


def _env_bool(name: str, default: bool) -> bool:
    return str(os.getenv(name, str(default))).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def strategy_guardrail_config_from_env(profile: str = "DEFAULT_FILTERS") -> StrategyQualityGuardrailConfig:
    return StrategyQualityGuardrailConfig(
        enabled=_env_bool("ALPHAFORGE_BACKTEST_STRATEGY_GUARDRAILS_ENABLED", True),
        profile=str(os.getenv("ALPHAFORGE_BACKTEST_STRATEGY_PROFILE", profile) or profile).upper(),
        max_accepted_trades_per_day=_env_int("ALPHAFORGE_BACKTEST_MAX_ACCEPTED_TRADES_PER_DAY", 6),
        max_consecutive_sl_pause=_env_int("ALPHAFORGE_BACKTEST_MAX_CONSECUTIVE_SL_PAUSE", 4),
        score10_sl_dominance_guard=_env_bool("ALPHAFORGE_BACKTEST_SCORE10_SL_DOMINANCE_GUARD", True),
        high_vol_acceptance_guard=_env_bool("ALPHAFORGE_BACKTEST_HIGH_VOL_ACCEPTANCE_GUARD", True),
        min_profit_factor_for_profile_pass=_env_float("ALPHAFORGE_BACKTEST_MIN_PROFIT_FACTOR_FOR_PROFILE_PASS", 1.20),
        max_loss_streak_for_profile_pass=_env_int("ALPHAFORGE_BACKTEST_MAX_LOSS_STREAK_FOR_PROFILE_PASS", 6),
        max_drawdown_pct_for_profile_pass=_env_float("ALPHAFORGE_BACKTEST_MAX_DRAWDOWN_PCT_FOR_PROFILE_PASS", 12.0),
    )

BACKTEST_FILTER_REASONS = (
    "LOW_SCORE",
    "TOO_CHOPPY",
    "WEAK_TREND_AND_NO_RANGE_EDGE",
    "STOP_TOO_WIDE",
    "RR_TOO_LOW",
    "DAILY_SYMBOL_TRADE_LIMIT",
    "REGIME_MISMATCH",
    "PANIC_CONDITIONS",
)


FILTER_SWITCH_SPECS: tuple[dict[str, Any], ...] = (
    {"filter_name": "LOW_SCORE", "env_var": "ALPHAFORGE_BACKTEST_FILTER_LOW_SCORE_ENABLED", "dashboard_field": "filter_LOW_SCORE", "internal_flag": "DISABLED_BACKTEST_FILTERS / disabled_backtest_filters", "affected_reject_reasons": ["LOW_SCORE"], "application": "src/alphaforge/order.py:evaluate_trade_quality", "optional_or_hard_safety": "optional", "mode": "BACKTEST only"},
    {"filter_name": "TOO_CHOPPY", "env_var": "ALPHAFORGE_BACKTEST_FILTER_TOO_CHOPPY_ENABLED", "dashboard_field": "filter_TOO_CHOPPY", "internal_flag": "disabled_backtest_filters", "affected_reject_reasons": ["TOO_CHOPPY"], "application": "src/alphaforge/symbol_selector.py:select_symbol", "optional_or_hard_safety": "optional", "mode": "BACKTEST only"},
    {"filter_name": "WEAK_TREND_AND_NO_RANGE_EDGE", "env_var": "ALPHAFORGE_BACKTEST_FILTER_WEAK_TREND_NO_RANGE_ENABLED", "dashboard_field": "filter_WEAK_TREND_AND_NO_RANGE_EDGE", "internal_flag": "disabled_backtest_filters", "affected_reject_reasons": ["WEAK_TREND_AND_NO_RANGE_EDGE"], "application": "src/alphaforge/symbol_selector.py:select_symbol", "optional_or_hard_safety": "optional", "mode": "BACKTEST only"},
    {"filter_name": "STOP_TOO_WIDE", "env_var": "ALPHAFORGE_BACKTEST_FILTER_STOP_TOO_WIDE_ENABLED", "dashboard_field": "filter_STOP_TOO_WIDE", "internal_flag": "DISABLED_BACKTEST_FILTERS / disabled_backtest_filters", "affected_reject_reasons": ["STOP_TOO_WIDE"], "application": "src/alphaforge/order.py:evaluate_trade_quality", "optional_or_hard_safety": "optional", "mode": "BACKTEST only"},
    {"filter_name": "RR_TOO_LOW", "env_var": "ALPHAFORGE_BACKTEST_FILTER_RR_TOO_LOW_ENABLED", "dashboard_field": "filter_RR_TOO_LOW", "internal_flag": "DISABLED_BACKTEST_FILTERS / disabled_backtest_filters", "affected_reject_reasons": ["RR_TOO_LOW"], "application": "src/alphaforge/order.py:evaluate_trade_quality", "optional_or_hard_safety": "optional", "mode": "BACKTEST only"},
    {"filter_name": "DAILY_SYMBOL_TRADE_LIMIT", "env_var": "ALPHAFORGE_BACKTEST_FILTER_DAILY_SYMBOL_TRADE_LIMIT_ENABLED", "dashboard_field": "filter_DAILY_SYMBOL_TRADE_LIMIT", "internal_flag": "DISABLED_BACKTEST_FILTERS / disabled_backtest_filters", "affected_reject_reasons": ["DAILY_SYMBOL_TRADE_LIMIT"], "application": "src/alphaforge/order.py:evaluate_trade_quality", "optional_or_hard_safety": "optional", "mode": "BACKTEST only", "naming_note": "DAILY_GLOBAL_TRADE_LIMIT is an always-on runtime gate when runtime limits are active and is not controlled by this switch."},
    {"filter_name": "REGIME_MISMATCH", "env_var": "ALPHAFORGE_BACKTEST_FILTER_REGIME_MISMATCH_ENABLED", "dashboard_field": "filter_REGIME_MISMATCH", "internal_flag": "DISABLED_BACKTEST_FILTERS / disabled_backtest_filters", "affected_reject_reasons": ["REGIME_MISMATCH"], "application": "src/alphaforge/order.py:evaluate_trade_quality", "optional_or_hard_safety": "optional", "mode": "BACKTEST only"},
    {"filter_name": "PANIC_CONDITIONS", "env_var": "ALPHAFORGE_BACKTEST_FILTER_PANIC_CONDITIONS_ENABLED", "dashboard_field": "filter_PANIC_CONDITIONS", "internal_flag": "disabled_backtest_filters", "affected_reject_reasons": ["PANIC_CONDITIONS"], "application": "src/alphaforge/symbol_selector.py:select_symbol", "optional_or_hard_safety": "optional", "mode": "BACKTEST only"},
)

HARD_SAFETY_GATES: tuple[dict[str, Any], ...] = (
    {"filter_name": "NEGATIVE_EXPECTANCY", "affected_reject_reasons": ["NEGATIVE_EXPECTANCY"], "optional_or_hard_safety": "hard_safety", "mode": "BACKTEST/PAPER/LIVE", "application": "src/alphaforge/order.py:evaluate_trade_quality"},
    {"filter_name": "EXPECTANCY_MISSING", "affected_reject_reasons": ["EXPECTANCY_MISSING"], "optional_or_hard_safety": "hard_safety", "mode": "BACKTEST/PAPER/LIVE", "application": "src/alphaforge/order.py:evaluate_trade_quality"},
    {"filter_name": "INVALID_CANDIDATE", "affected_reject_reasons": ["INVALID_CANDIDATE", "REJECT_REASON_MISSING"], "optional_or_hard_safety": "hard_safety", "mode": "BACKTEST/PAPER/LIVE", "application": "src/alphaforge/order.py:evaluate_trade_quality / backtest_order.py:process_backtest_result"},
    {"filter_name": "EXECUTION_COST_SANITY", "affected_reject_reasons": ["LOW_EFFECTIVE_RR", "HIGH_SPREAD", "HIGH_SLIPPAGE", "EXECUTION_RISK", "SPREAD_TOO_HIGH", "SLIPPAGE_TOO_HIGH", "VOLATILITY_TOO_HIGH", "VOLATILITY_TOO_LOW"], "optional_or_hard_safety": "hard_safety", "mode": "BACKTEST/PAPER/LIVE where applicable", "application": "backtest_order.py:_execution_reject_flags and src/alphaforge/order.py:evaluate_trade_quality"},
    {"filter_name": "ORDER_GEOMETRY", "affected_reject_reasons": ["STOP_TOO_TIGHT", "INVALID_ENTRY_SL_TP", "IMPOSSIBLE_RR"], "optional_or_hard_safety": "hard_safety", "mode": "BACKTEST/PAPER/LIVE", "application": "candidate construction and trade-quality validation"},
    {"filter_name": "DAILY_GLOBAL_TRADE_LIMIT", "affected_reject_reasons": ["DAILY_GLOBAL_TRADE_LIMIT"], "optional_or_hard_safety": "runtime_gate", "mode": "PAPER/LIVE and BACKTEST when runtime limits are active", "application": "src/alphaforge/order.py:evaluate_trade_quality"},
)


CONCRETE_UNKNOWN_REJECT_PLACEHOLDERS = {"", "UNKNOWN", "REJECT_REASON_MISSING"}
REJECT_REASON_UNAVAILABLE = "REJECT_REASON_UNAVAILABLE"


def _primary_reject_reason_from_context(
    *,
    current_reason: str = "",
    diagnostics: Mapping[str, Any] | None = None,
    market_ctx: Mapping[str, Any] | None = None,
    execution_ctx_missing: bool = False,
) -> str:
    """Return the first concrete reject reason available without loosening filters."""
    diagnostics = diagnostics if isinstance(diagnostics, Mapping) else {}
    market_ctx = market_ctx if isinstance(market_ctx, Mapping) else {}

    def _norm(value: Any) -> str:
        return str(value or "").strip().upper()

    for value in (current_reason, diagnostics.get("reject_reason"), diagnostics.get("primary_reject_reason")):
        reason = _norm(value)
        if reason not in CONCRETE_UNKNOWN_REJECT_PLACEHOLDERS:
            if reason == "RR_TOO_LOW":
                eff = _safe_float(diagnostics.get("effective_rr", market_ctx.get("effective_rr", 0.0)), 0.0)
                min_eff = _safe_float(diagnostics.get("min_effective_rr", market_ctx.get("MIN_EFFECTIVE_RR", market_ctx.get("min_effective_rr", 1.60))), 1.60)
                raw_rr = _safe_float(diagnostics.get("rr", market_ctx.get("rr", 0.0)), 0.0)
                min_raw = _safe_float(diagnostics.get("min_raw_rr", diagnostics.get("min_rr", market_ctx.get("MIN_RR", 1.30))), 1.30)
                if eff < min_eff and raw_rr >= min_raw:
                    return "LOW_EFFECTIVE_RR"
            return reason

    failed = [_norm(x) for x in (diagnostics.get("all_failed_gates") or [])] if isinstance(diagnostics.get("all_failed_gates"), list) else []
    eff = _safe_float(diagnostics.get("effective_rr", market_ctx.get("effective_rr", market_ctx.get("rr", 0.0))), 0.0)
    min_eff = _safe_float(diagnostics.get("min_effective_rr", market_ctx.get("MIN_EFFECTIVE_RR", market_ctx.get("min_effective_rr", 1.60))), 1.60)
    raw_rr = _safe_float(diagnostics.get("rr", market_ctx.get("rr", 0.0)), 0.0)
    min_raw = _safe_float(diagnostics.get("min_raw_rr", diagnostics.get("min_rr", market_ctx.get("MIN_RR", 1.30))), 1.30)
    score = _safe_float(diagnostics.get("score", market_ctx.get("score", 0.0)), 0.0)
    min_score = _safe_float(diagnostics.get("min_required_score", diagnostics.get("min_score", market_ctx.get("MIN_TRADE_SCORE", 7.5))), 7.5)
    expectancy = diagnostics.get("expectancy", market_ctx.get("expectancy"))
    try:
        expectancy_val = None if expectancy in (None, "", "UNKNOWN") else float(expectancy)
    except (TypeError, ValueError):
        expectancy_val = None
    reject_unknown_expectancy = bool(diagnostics.get("reject_unknown_expectancy", diagnostics.get("block_unknown_expectancy", False)))

    if eff < min_eff or "RR" in failed:
        return "LOW_EFFECTIVE_RR"
    if raw_rr < min_raw:
        return "RR_TOO_LOW"
    if expectancy_val is not None and expectancy_val < 0.0:
        return "NEGATIVE_EXPECTANCY"
    if expectancy_val is None and (reject_unknown_expectancy or "EXPECTANCY_PRESENT" in failed):
        return "EXPECTANCY_MISSING"
    if execution_ctx_missing:
        return "EXECUTION_CONTEXT_UNAVAILABLE"
    if score < min_score or "SCORE" in failed:
        return "LOW_SCORE"
    if "REGIME" in failed:
        return "REGIME_MISMATCH"
    return REJECT_REASON_UNAVAILABLE

def filter_profile_name(disabled_filters: Iterable[str]) -> str:
    disabled = {str(r).upper() for r in disabled_filters}
    all_filters = {spec["filter_name"] for spec in FILTER_SWITCH_SPECS}
    if disabled == all_filters:
        return "ALL_OFF"
    if not disabled:
        return "DEFAULT"
    return "CUSTOM"

def build_backtest_filter_state(*, disabled_filters: Iterable[str], source: str, timestamp: str, symbols: Iterable[str], timeframe: str, last_days: int, short_breakdown_rescue_enabled: bool = False) -> dict[str, Any]:
    disabled = {str(r).upper() for r in disabled_filters}
    filters = []
    for spec in FILTER_SWITCH_SPECS:
        name = str(spec["filter_name"])
        filters.append({**spec, "enabled": name not in disabled, "source": source})
    return {
        "timestamp": timestamp,
        "symbols": list(symbols),
        "timeframe": timeframe,
        "last_days": last_days,
        "mode": "BACKTEST",
        "filter_profile": filter_profile_name(disabled),
        "enabled_filters": [f["filter_name"] for f in filters if f["enabled"]],
        "disabled_filters": [f["filter_name"] for f in filters if not f["enabled"]],
        "filters": filters,
        "hard_safety_gates": list(HARD_SAFETY_GATES),
        "experiments": {"SHORT_BREAKDOWN_RESCUE": {"env_var": "ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED", "dashboard_field": "short_breakdown_rescue_enabled", "enabled": bool(short_breakdown_rescue_enabled), "default": False, "mode": "BACKTEST only", "paper_live_effect": "does not affect PAPER/LIVE", "description": "SHORT_BREAKDOWN_RESCUE experiment"}},
        "all_off_warning": "This is a diagnostic stress test. It can increase accepted trades and destroy expectancy. Do not treat as strategy performance." if filter_profile_name(disabled) == "ALL_OFF" else "",
        "backtest_only_experiments": [{"name": SHORT_BREAKDOWN_RESCUE_REASON, "enabled": bool(short_breakdown_rescue_enabled), "mode": "BACKTEST only", "default_behavior": "unchanged when disabled", "accepted_reason": SHORT_BREAKDOWN_RESCUE_REASON}],
    }

def write_backtest_filter_state_artifacts(output_dir: str, state: Mapping[str, Any]) -> None:
    path = Path(output_dir)
    (path / "backtest_filter_state.json").write_text(json.dumps(state, indent=2, sort_keys=True))
    rows = []
    for row in state.get("filters", []):
        rows.append({
            "timestamp": state.get("timestamp"),
            "symbols": ",".join(str(x) for x in state.get("symbols", [])),
            "timeframe": state.get("timeframe"),
            "last_days": state.get("last_days"),
            "mode": state.get("mode"),
            "filter_profile": state.get("filter_profile"),
            "filter_name": row.get("filter_name"),
            "enabled": row.get("enabled"),
            "source": row.get("source"),
            "affected_reject_reasons": json.dumps(row.get("affected_reject_reasons", []), sort_keys=True),
            "optional_or_hard_safety": row.get("optional_or_hard_safety"),
            "application": row.get("application"),
            "env_var": row.get("env_var"),
            "dashboard_field": row.get("dashboard_field"),
            "internal_flag": row.get("internal_flag"),
            "experiment_SHORT_BREAKDOWN_RESCUE_enabled": state.get("experiments", {}).get("SHORT_BREAKDOWN_RESCUE", {}).get("enabled"),
            "experiment_SHORT_BREAKDOWN_RESCUE_mode": state.get("experiments", {}).get("SHORT_BREAKDOWN_RESCUE", {}).get("mode"),
        })
    with (path / "backtest_filter_state.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["filter_name"])
        writer.writeheader(); writer.writerows(rows)

def _disabled_backtest_filters(args: Any | None = None) -> tuple[str, ...]:
    cfg = load_config_from_env().backtest.filter_switches
    disabled = set(cfg.disabled_filters())
    if args is not None:
        for reason in BACKTEST_FILTER_REASONS:
            attr = "backtest_filter_" + reason.lower().replace("weak_trend_and_no_range_edge", "weak_trend_no_range").replace("daily_symbol_trade_limit", "daily_symbol_trade_limit").replace("panic_conditions", "panic_conditions") + "_enabled"
            if hasattr(args, attr) and not bool(getattr(args, attr)):
                disabled.add(reason)
    return tuple(sorted(disabled))

def _filter_switch_metadata(disabled_filters: Iterable[str]) -> dict[str, Any]:
    disabled = tuple(sorted({str(r).upper() for r in disabled_filters}))
    return {
        "disabled_filters": json.dumps(disabled),
        "filter_switch_experiment_active": bool(disabled),
    }

@dataclass
class RejectedShadowEvaluation:
    symbol: str
    timestamp: int
    side: str
    entry: float
    stop_loss: float
    take_profit: float
    raw_rr: float
    effective_rr: float
    reject_reasons: str
    score: float
    regime: str
    spread_pct: Any
    liquidity_score: Any
    volatility_score: Any
    shadow_outcome: str
    effective_tp_hit: bool
    cost_penalty: float
    liquidity_ok: bool
    volatility_ok: bool
    low_score_gate_score: float = 0.0
    rescue_attempted: bool = False
    rescue_passed: bool = False
    rescued_stop_loss: float = 0.0
    rescued_effective_rr: float = 0.0
    rescued_size_multiplier: float = 0.0
    rescue_reject_reason: str = ""
    setup_type: str = "UNAVAILABLE"
    timeframe: str = "UNAVAILABLE"
    expected_slippage_pct: Any = "UNAVAILABLE"
    stop_distance_pct: Any = "UNAVAILABLE"
@dataclass
class ForwardWindowEvaluation:
    signal_id: str
    symbol: str
    decision: str
    lifecycle_state: str
    reject_reason: str
    setup_type: str = "UNKNOWN"
    score: float = 0.0
    rr: float = 0.0
    effective_rr: float = 0.0
    predicted_quality: float = 0.0
    forward_window_minutes: int = 0
    would_have_hit_tp: bool = False
    would_have_hit_sl: bool = False
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    max_forward_return: float = 0.0
    max_adverse_return: float = 0.0
    reject_correct: Optional[bool] = None
    reject_missed_winner: bool = False
    reject_saved_from_loss: bool = False
    forward_window_regime: str = "UNKNOWN"
    execution_quality_bucket: str = "UNKNOWN"


TERMINAL_FORWARD_CLOSE_REASONS = {"TP_HIT", "SL_HIT", "TIMEOUT", "EXPIRED", "CANCELED"}
@dataclass
class LifecycleRow:
    timestamp: int
    symbol: str
    side: str
    setup_type: str
    setup_reason: str
    regime: str
    score: float
    rr: Optional[float]
    entry: float
    sl: float
    tp: float
    status_before: str
    status_after: str
    trigger_price: float = 0.0
    close_price: float = 0.0
    close_reason: str = ""
    net_pnl_pct: float = 0.0
    net_pnl_usdt: float = 0.0
    hold_minutes: float = 0.0
    reject_reason: str = ""
    cancel_reason: str = ""
    order_type: str = "LIMIT"
    expectancy_bucket: str = "UNKNOWN"
    event_flags: str = ""
    volume_24h_usdt: Any = "UNAVAILABLE_BACKTEST"
    spread_pct: Any = "UNAVAILABLE_BACKTEST"
    funding_rate_pct: Any = "UNAVAILABLE_BACKTEST"
    expected_slippage_pct: Any = "UNAVAILABLE_BACKTEST"
    volatility_regime: str = "UNAVAILABLE_BACKTEST"
    liquidity_score: Any = "UNAVAILABLE_BACKTEST"
    effective_rr: Optional[float] = None
    mfe: float = 0.0
    mae: float = 0.0
    would_tp_hit: bool = False
    would_sl_hit: bool = False
    would_trigger: bool = False
    signal_id: str = ""
    lifecycle_id: str = ""
    order_id: str = ""
    position_id: str = ""
    lifecycle_seq: int = 0
    shadow_outcome: str = ""
    cost_penalty: float = 0.0
    volatility_score: Any = "UNAVAILABLE_BACKTEST"
    liquidity_ok: Optional[bool] = None
    volatility_ok: Optional[bool] = None
    stop_too_wide_softened: bool = False
    original_reject_reason: str = ""
    reject_reason_softened: str = ""
    risk_scale: float = 1.0
    accepted_reason: str = "BASELINE"
    rescue_size_multiplier: float = 1.0
    rescue_effective_rr: float = 0.0
    rescue_decision_context: str = ""
    bypassed_reject_reasons: str = ""
    disabled_filters: str = ""
    disabled_filter_bypass_count: int = 0
    filter_switch_experiment_active: bool = False
    source_stage: str = ""
    rr_available: bool = True
    effective_rr_available: bool = True
    expectancy_available: bool = True


def _is_pre_signal_symbol_reject(row: LifecycleRow, lifecycle_state: str | None = None) -> bool:
    state = (lifecycle_state or row.status_after or "").strip().upper()
    return state == "SYMBOL_REJECTED" and row.status_before.strip().upper() in {"", "NONE"} and row.event_flags == "SYMBOL_SELECTOR"


def _lifecycle_signal_id(row: LifecycleRow, lifecycle_state: str | None = None) -> str:
    if row.signal_id:
        return row.signal_id
    if _is_pre_signal_symbol_reject(row, lifecycle_state):
        return f"SYMBOL_SELECTOR:{row.symbol}:{row.timestamp}"
    return f"{row.symbol}:{row.timestamp}"

def _bucket_expectancy(expectancy: Optional[float]) -> str:
    if expectancy in (None, "", "UNKNOWN", "UNAVAILABLE", "UNAVAILABLE_BACKTEST"):
        return "BACKTEST_EXPECTANCY_UNAVAILABLE"
    if expectancy < 0.0:
        return "NEGATIVE"
    if expectancy < 0.05:
        return "LOW"
    if expectancy < 0.2:
        return "MEDIUM"
    return "HIGH"
def _execution_reject_flags(rr: float, market_ctx: Mapping[str, Any]) -> tuple[float, list[str], dict[str, float]]:
    model = build_execution_cost_model(market_ctx, include_missing_penalty=False)
    effective = round(max(float(rr) - model.total_penalty, 0.0), 6)
    liquidity_score = min(1.0, max(0.0, float(market_ctx.get("liquidity_score", 1.0) or 1.0)))
    flags: list[str] = []
    if model.slippage_penalty >= 0.20:
        flags.append("HIGH_SLIPPAGE")
    if model.spread_penalty >= 0.20:
        flags.append("HIGH_SPREAD")
    if liquidity_score < 0.3:
        flags.append("LOW_LIQUIDITY")
    min_effective_rr = float(market_ctx.get("MIN_EFFECTIVE_RR", market_ctx.get("min_effective_rr", 1.60)) or 1.60)
    if effective < min_effective_rr:
        flags.append("LOW_EFFECTIVE_RR")
    breakdown = {
        "cost_penalty_total": model.total_penalty,
        "spread_penalty": model.spread_penalty,
        "slippage_penalty": model.slippage_penalty,
        "latency_penalty": model.latency_penalty,
        "liquidity_penalty": model.liquidity_penalty,
        "funding_penalty": model.funding_penalty,
    }
    return effective, flags, breakdown


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off", ""}


def _rescue_config_from_args(args: Any, runtime_cfg: Any) -> RescueConfig:
    return RescueConfig(
        enabled=bool(getattr(args, "rescue_enabled", False)) or _env_bool("ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED", False),
        modes=tuple(str(getattr(args, "rescue_modes", "BACKTEST") or "BACKTEST").upper().replace(",", " ").split()),
        effective_rr_min=float(getattr(args, "rescue_effective_rr_min", 1.90)),
        score_min=float(getattr(args, "rescue_score_min", 9.0)),
        size_multiplier=min(1.0, max(0.0, float(getattr(args, "rescue_size_multiplier", 0.25)))),
        max_trades_per_day=int(getattr(args, "max_rescue_trades_per_day", 1)),
        allowed_reasons=tuple(r.strip().upper() for r in str(getattr(args, "rescue_allowed_reasons", "STOP_TOO_WIDE,DAILY_SYMBOL_TRADE_LIMIT") or "").replace(",", " ").split() if r.strip()),
        allow_regime_mismatch=bool(getattr(args, "rescue_allow_regime_mismatch", False)),
        max_spread_pct=float(getattr(args, "rescue_max_spread_pct", 0.0025)),
        max_slippage_pct=float(getattr(args, "rescue_max_slippage_pct", 0.0020)),
        allow_cooldown_bypass=bool(getattr(args, "rescue_allow_cooldown_bypass", False)),
        max_concurrent_positions=int(getattr(getattr(runtime_cfg, "runtime", runtime_cfg), "max_concurrent_positions", 3)),
    )

def _short_breakdown_rescue_config_from_args(args: Any) -> ShortBreakdownRescueConfig:
    return ShortBreakdownRescueConfig(
        enabled=bool(getattr(args, "short_breakdown_rescue_enabled", False)),
        modes=tuple(str(getattr(args, "short_breakdown_rescue_modes", "BACKTEST") or "BACKTEST").upper().replace(",", " ").split()),
        size_multiplier=min(1.0, max(0.0, float(getattr(args, "short_breakdown_rescue_size_multiplier", 0.25)))),
        max_trades_per_day=int(getattr(args, "short_breakdown_rescue_max_per_day", 1)),
        min_effective_rr=float(getattr(args, "short_breakdown_rescue_min_effective_rr", 1.10)),
        min_shadow_expectancy=float(getattr(args, "short_breakdown_rescue_min_shadow_expectancy", 0.0)),
        allowed_reasons=tuple(r.strip().upper() for r in str(getattr(args, "short_breakdown_rescue_allowed_reasons", "LOW_SCORE,STOP_TOO_WIDE,DAILY_SYMBOL_TRADE_LIMIT") or "").replace(",", " ").split() if r.strip()),
        max_spread_pct=float(getattr(args, "short_breakdown_rescue_max_spread_pct", 0.0025)),
        max_slippage_pct=float(getattr(args, "short_breakdown_rescue_max_slippage_pct", 0.0020)),
    )

def _quality_gate_config_from_args(args: Any) -> QualityGateConfig:
    min_score_raw = getattr(args, "quality_gate_min_score", None)
    min_score = None if min_score_raw in (None, "") else float(min_score_raw)
    return QualityGateConfig(
        enabled=bool(getattr(args, "quality_gate_enabled", False)),
        modes=tuple(str(getattr(args, "quality_gate_modes", "BACKTEST") or "BACKTEST").upper().replace(",", " ").split()),
        size_multiplier=min(1.0, max(0.0, float(getattr(args, "quality_gate_size_multiplier", 0.25)))),
        max_trades_per_day=int(getattr(args, "max_quality_gate_trades_per_day", 1)),
        min_effective_rr=float(getattr(args, "quality_gate_min_effective_rr", 1.10)),
        min_score=min_score,
        allowed_gate_name=str(getattr(args, "quality_gate_name", QUALITY_GATE_NAME) or QUALITY_GATE_NAME),
        max_spread_pct=float(getattr(args, "quality_gate_max_spread_pct", 0.0025)),
        max_slippage_pct=float(getattr(args, "quality_gate_max_slippage_pct", 0.0020)),
        allowed_reasons=tuple(r.strip().upper() for r in str(getattr(args, "quality_gate_allowed_reasons", "LOW_SCORE,DAILY_SYMBOL_TRADE_LIMIT") or "").replace(",", " ").split() if r.strip()),
    )

def _rescue_reject(stats: RescueStats, reason: str) -> tuple[bool, str]:
    stats.rejected_count += 1
    stats.reject_reasons[reason] = stats.reject_reasons.get(reason, 0) + 1
    return False, reason

def _rescue_acceptance_allowed(
    *, mode: str, reason: str, score: float, effective_rr: float, regime: str, mctx: Mapping[str, Any],
    cfg: RescueConfig, stats: RescueStats, recent_stats: Mapping[str, Any], open_rows: List[LifecycleRow], symbol: str,
) -> tuple[bool, str]:
    if not cfg.enabled:
        return False, "RESCUE_DISABLED"
    if str(mode).upper() not in cfg.modes or str(mode).upper() != "BACKTEST":
        return _rescue_reject(stats, "MODE_NOT_BACKTEST")
    reason = str(reason or "").upper()
    if reason not in set(cfg.allowed_reasons):
        return _rescue_reject(stats, "REASON_NOT_ALLOWED")
    stats.candidate_count += 1
    if score < cfg.score_min:
        return _rescue_reject(stats, "SCORE_TOO_LOW")
    if mctx.get("liquidity_ok") is False:
        return _rescue_reject(stats, "LIQUIDITY_NOT_OK")
    if mctx.get("volatility_ok") is False:
        return _rescue_reject(stats, "VOLATILITY_NOT_OK")
    spread = _safe_float(mctx.get("spread_pct"), -1.0)
    slip = _safe_float(mctx.get("expected_slippage_pct"), -1.0)
    if spread < 0.0 or spread > cfg.max_spread_pct:
        return _rescue_reject(stats, "SPREAD_TOO_HIGH")
    if slip < 0.0 or slip > cfg.max_slippage_pct:
        return _rescue_reject(stats, "SLIPPAGE_TOO_HIGH")
    if effective_rr < cfg.effective_rr_min:
        return _rescue_reject(stats, "EFFECTIVE_RR_TOO_LOW")
    if str(regime or mctx.get("regime", "")).upper() in {"PANIC", "NEWS_DRIVEN"}:
        return _rescue_reject(stats, "REGIME_BLOCKED")
    failed = {str(x).upper() for x in (mctx.get("all_failed_gates") or [])} if isinstance(mctx.get("all_failed_gates"), list) else set()
    if not cfg.allow_regime_mismatch and (reason == "REGIME_MISMATCH" or "REGIME_MISMATCH" in failed):
        return _rescue_reject(stats, "REGIME_MISMATCH")
    if stats.accepted_count >= cfg.max_trades_per_day:
        return _rescue_reject(stats, "DAILY_RESCUE_LIMIT")
    if len(open_rows) >= cfg.max_concurrent_positions:
        return _rescue_reject(stats, "MAX_CONCURRENT_POSITIONS")
    if not cfg.allow_cooldown_bypass and reason == "SYMBOL_COOLDOWN_ACTIVE":
        return _rescue_reject(stats, "COOLDOWN_ACTIVE")
    return True, "HIGH_EFFECTIVE_RR_RESCUE"
def _estimate_backtest_spread_pct(liquidity_score: float, volatility_pct: float) -> float:
    base_spread_pct = 0.0008 + (1.0 - liquidity_score) * 0.0012
    volatility_widening = min(0.0004, max(0.0, (volatility_pct - 2.0) * 0.00002))
    return max(0.0005, min(0.0024, base_spread_pct + volatility_widening))
def _build_market_ctx(
    now: Candle,
    prev: Candle,
    symbol_meta: Mapping[str, Any],
    recent: Optional[List[Candle]] = None,
) -> Dict[str, Any]:
    bullish_breakout = now.close >= prev.close
    side = "LONG" if bullish_breakout else "SHORT"
    entry = now.close
    sl = min(now.low, prev.low) if side == "LONG" else max(now.high, prev.high)
    risk = max(entry - sl, 1e-9)
    if side == "SHORT":
        risk = max(sl - entry, 1e-9)
    body = abs(now.close - now.open)
    breakout_strength = max(0.0, (now.close - prev.high) / max(prev.high, 1e-9)) if side == "LONG" else max(0.0, (prev.low - now.close) / max(prev.low, 1e-9))
    range_pct = ((now.high - now.low) / max(now.close, 1e-9)) * 100.0
    rr = max(1.1, min(3.5, 1.2 + breakout_strength * 25.0 + body / max(now.open, 1e-9) * 8.0))
    tp = entry + rr * risk if side == "LONG" else entry - rr * risk
    score = max(0.0, min(10.0, 3.0 + breakout_strength * 500.0 + range_pct))
    expectancy = ((score / 10.0) - 0.5) * (rr - 1.0)
    quote_volume = symbol_meta.get("quoteVolume")
    if quote_volume in (None, "", 0, 0.0):
        quote_volume = now.volume * now.close * 1440.0
    candle_range_pct = ((now.high - now.low) / max(now.close, 1e-9)) * 100.0
    liq = min(1.0, max(0.05, float(quote_volume) / 100000000.0))
    spread_source = "ACTUAL" if symbol_meta.get("actual_spread_pct") not in (None, "") else "ESTIMATED_BACKTEST"
    raw_spread = symbol_meta.get("actual_spread_pct") or symbol_meta.get("estimated_spread_pct") or _estimate_backtest_spread_pct(liq, candle_range_pct)
    spread_pct, spread_unit_assumed = normalize_pct_input(raw_spread, field="spread_pct")
    base = {
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "rr": rr,
        "score": score,
        "setup_type": "BREAKOUT_UP" if side == "LONG" else "BREAKDOWN_DOWN",
        "setup_reason": "CLOSE_ABOVE_PREV_HIGH" if side == "LONG" else "CLOSE_BELOW_PREV_LOW",
        "regime": "BREAKOUT" if breakout_strength > 0.002 else "TREND",
        "expectancy": expectancy,
        "expectancy_bucket": _bucket_expectancy(expectancy),
        "side": side,
        "volume_24h_usdt": float(quote_volume),
        "spread_pct": spread_pct,
        "spread_unit_assumed": spread_unit_assumed,
        "spread_source": spread_source,
        "candle_range_pct": candle_range_pct,
        "volatility_pct": candle_range_pct,
        "funding_rate_pct": float(symbol_meta.get("fundingRate", 0.0) or 0.0),
    }
    klines = [{"high": c.high, "low": c.low, "close": c.close} for c in (recent or [])[-20:] if c]
    exec_ctx = build_execution_context(
        {
            **base,
            "raw_spread_pct_input": raw_spread,
            "recent_klines": klines,
            "liquidity_score": liq,
        }
    )
    base.update(exec_ctx)
    base["spread_unit_assumed"] = spread_unit_assumed
    return base
def _build_symbol_market_data(symbol_meta: Mapping[str, Any], candles: List[Candle], idx: int) -> Dict[str, Any]:
    now = candles[idx]
    prev = candles[idx - 1] if idx > 0 else now
    recent = candles[max(0, idx - 20):idx + 1]
    diagnostics: Dict[str, Any] = {}
    quote_volume = symbol_meta.get("quoteVolume")
    if quote_volume in (None, "", 0, 0.0):
        close = max(now.close, 1e-9)
        quote_volume = now.volume * close * 1440.0
        diagnostics["volume_24h_usdt"] = "derived_from_candle_volume"
    candle_range_pct = ((now.high - now.low) / max(now.close, 1e-9)) * 100.0
    volatility_pct = candle_range_pct
    lookback = recent[-10:] if recent else [now]
    up_bars = sum(1 for c in lookback if c.close > c.open)
    trend_strength = up_bars / max(1, len(lookback))
    liquidity_score = min(1.0, max(0.05, float(quote_volume) / 100000000.0))
    actual_spread_pct = symbol_meta.get("actual_spread_pct")
    spread_source = "ACTUAL" if actual_spread_pct not in (None, "") else "ESTIMATED_BACKTEST"
    spread_unit_assumed = "fraction"
    if actual_spread_pct not in (None, ""):
        spread_pct, spread_unit_assumed = normalize_pct_input(actual_spread_pct, field="spread_pct")
    else:
        # Conservative offline estimate: wider for lower liquidity and high volatility.
        spread_pct = _estimate_backtest_spread_pct(liquidity_score, volatility_pct)
    recent_vol = [c.volume for c in recent[-6:]]
    prev_vol = [c.volume for c in recent[-12:-6]]
    if recent_vol and prev_vol:
        recent_avg = sum(recent_vol) / len(recent_vol)
        prev_avg = sum(prev_vol) / len(prev_vol)
        recent_volume_change_pct = ((recent_avg - prev_avg) / max(prev_avg, 1e-9)) * 100.0
    else:
        recent_volume_change_pct = 0.0
        diagnostics["recent_volume_change_pct"] = "defaulted_insufficient_history"
    closes = [c.close for c in lookback]
    close_min = min(closes)
    close_max = max(closes)
    chop_score = min(1.0, max(0.0, 1.0 - abs((closes[-1] - closes[0]) / max(close_max - close_min, 1e-9))))
    panic_score = 0.0
    drop_pct = ((prev.close - now.close) / max(prev.close, 1e-9)) * 100.0
    if drop_pct > 3.0 and volatility_pct > 2.0:
        panic_score = min(1.0, (drop_pct / 10.0) + (volatility_pct / 20.0))
    return {
        "volume_24h_usdt": float(quote_volume),
        "spread_pct": spread_pct,
        "spread_source": spread_source,
        "spread_unit_assumed": spread_unit_assumed,
        "actual_spread_pct": spread_pct if actual_spread_pct not in (None, "") else None,
        "estimated_spread_pct": spread_pct if spread_source == "ESTIMATED_BACKTEST" else None,
        "candle_range_pct": candle_range_pct,
        "volatility_pct": volatility_pct,
        "trend_strength": trend_strength,
        "liquidity_score": liquidity_score,
        "recent_volume_change_pct": recent_volume_change_pct,
        "chop_score": chop_score,
        "panic_score": panic_score,
        "selector_diagnostics": diagnostics,
    }
def parse_ts(value: str) -> int:
    if value.isdigit():
        return int(value)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)
def fetch_json(url: str) -> Any:
    with urlopen(url) as resp:  # nosec - public market data
        return json.loads(resp.read().decode("utf-8"))
def select_symbol_universe(top_n: int, quote: str = "USDT", symbols: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    if symbols:
        return [{"symbol": sym.strip().upper(), "quoteVolume": 0.0} for sym in symbols if sym.strip()]
    info = fetch_json("https://fapi.binance.com/fapi/v1/exchangeInfo")
    tickers = fetch_json("https://fapi.binance.com/fapi/v1/ticker/24hr")
    ticker_map = {t["symbol"]: t for t in tickers}
    selected = []
    for s in info.get("symbols", []):
        sym = s.get("symbol", "")
        if s.get("status") != "TRADING" or s.get("contractType") != "PERPETUAL":
            continue
        if s.get("quoteAsset") != quote or not sym.endswith(quote):
            continue
        t = ticker_map.get(sym)
        if not t:
            continue
        qv = float(t.get("quoteVolume", 0.0) or 0.0)
        if qv <= 0:
            continue
        if not s.get("filters"):
            continue
        selected.append({"symbol": sym, "quoteVolume": qv})
    selected.sort(key=lambda x: x["quoteVolume"], reverse=True)
    return selected[:top_n]
def save_symbol_universe(path: str, universe: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "quoteVolume"])
        w.writeheader()
        w.writerows(universe)
def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> List[Candle]:
    rows = fetch_binance_klines_paginated(symbol=symbol, interval=interval, start_ms=start_ms, end_ms=end_ms)
    return [Candle(timestamp=r.timestamp, open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume) for r in rows]

def _fetch_klines_legacy(symbol: str, interval: str, start_ms: int, end_ms: int) -> List[Candle]:
    params = urlencode(
        {
            "symbol": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1500,
        }
    )
    rows = fetch_json(f"https://fapi.binance.com/fapi/v1/klines?{params}")
    return [
        Candle(
            timestamp=int(r[0]),
            open=float(r[1]),
            high=float(r[2]),
            low=float(r[3]),
            close=float(r[4]),
            volume=float(r[5]),
        )
        for r in rows
    ]

def _prune_stale_candle_artifacts(output_dir: str, symbols: Iterable[str], interval: str) -> None:
    """Keep run-local candle artifacts aligned with the current symbol universe.

    Candle JSON files live inside the run artifact directory, not a shared cache.
    Remove symbol/interval files not used by this run so exported zips cannot imply
    stale candles were run inputs.
    """
    candles_dir = Path(output_dir) / "candles"
    if not candles_dir.exists():
        return
    expected = {f"{str(symbol).upper()}_{interval}.json" for symbol in symbols}
    for path in candles_dir.glob("*.json"):
        if path.name not in expected:
            path.unlink()

def load_or_fetch_candles(symbol: str, interval: str, start_ms: int, end_ms: int, output_dir: str, force_refresh: bool = False) -> List[Candle]:
    rows = load_or_fetch_historical_candles(
        symbol=symbol,
        interval=interval,
        start_ms=start_ms,
        end_ms=end_ms,
        output_dir=output_dir,
        force_refresh=force_refresh,
    )
    return [Candle(timestamp=r.timestamp, open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume) for r in rows]
def load_candles(path: str, start_ms: int, end_ms: int) -> List[Candle]:
    out = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            ts = parse_ts(str(row.get("timestamp") or row.get("open_time") or row.get("time") or row.get("date")))
            if start_ms <= ts <= end_ms:
                out.append(
                    Candle(
                        ts,
                        float(row["open"]),
                        float(row["high"]),
                        float(row["low"]),
                        float(row["close"]),
                        float(row.get("volume", 0.0)),
                    )
                )
    out.sort(key=lambda x: x.timestamp)
    return out
def scan_symbol_backtest(
    symbol: str,
    candles: List[Candle],
    idx: int,
    context: Dict[str, Any],
) -> Optional[CandidateOrder]:
    OrderExecutionContext, TradingMode, run_order_cycle = _order_runtime()
    if idx < 2:
        return None
    now = candles[idx]
    prev = candles[idx - 1]
    mctx = _build_market_ctx(now, prev, context.get("symbol_meta", {}), candles[max(0, idx - 20):idx + 1])
    if "min_effective_rr" in context:
        mctx["MIN_EFFECTIVE_RR"] = context["min_effective_rr"]
    ctx = OrderExecutionContext(
        mode=TradingMode.BACKTEST,
        timestamp=now.timestamp,
        symbol=symbol,
        balance=float(context.get("balance", 1000)),
        risk_pct=float(context.get("risk_pct", 1.0)),
        market_ctx=mctx,
    )
    disabled_filters = tuple(context.get("disabled_backtest_filters", ()))
    try:
        result = run_order_cycle(ctx, config={"MODE": "BACKTEST", "DISABLED_BACKTEST_FILTERS": disabled_filters}, recent_stats=context.get("recent_stats", {}))
    except TypeError:
        # Test doubles and older call sites may not accept the newer config kwarg;
        # production runtime still receives the real BACKTEST filter switches above.
        result = run_order_cycle(ctx, recent_stats=context.get("recent_stats", {}))
    context["last_result"] = result
    context["market_ctx"] = mctx
    if result.get("status") != "executed":
        return None
    c = result["candidate"]
    return CandidateOrder(
        now.timestamp,
        symbol,
        c.side,
        c.entry,
        c.sl,
        c.tp,
        c.rr,
        c.setup_type,
        c.setup_reason,
        c.regime,
        c.score,
        c.order_type,
        expectancy_bucket=mctx.get("expectancy_bucket", "UNKNOWN"),
    )
def simulate_candidate(
    candidate: CandidateOrder,
    candles: List[Candle],
    idx: int,
    balance: float,
    risk_pct: float,
    market_ctx: Optional[Mapping[str, Any]] = None,
) -> List[LifecycleRow]:
    market_ctx = market_ctx or {}
    signal_id = f"{candidate.symbol}:{candidate.timestamp}"
    lifecycle_id = str(uuid5(NAMESPACE_URL, f"backtest:lifecycle:{signal_id}"))
    order_id = str(uuid5(NAMESPACE_URL, f"backtest:order:{signal_id}:{candidate.entry}:{candidate.sl}:{candidate.tp}"))
    position_id = str(uuid5(NAMESPACE_URL, f"backtest:position:{signal_id}:{candidate.side}"))
    def _finalize_rows(out_rows: List[LifecycleRow]) -> List[LifecycleRow]:
        for seq, row in enumerate(out_rows, start=1):
            row.lifecycle_seq = seq
            row.accepted_reason = str(market_ctx.get("accepted_reason", row.accepted_reason) or row.accepted_reason)
            row.original_reject_reason = str(market_ctx.get("original_reject_reason", row.original_reject_reason) or row.original_reject_reason)
            row.rescue_size_multiplier = _safe_float(market_ctx.get("rescue_size_multiplier"), row.rescue_size_multiplier)
            row.rescue_effective_rr = _safe_float(market_ctx.get("rescue_effective_rr"), row.rescue_effective_rr)
            row.rescue_decision_context = str(market_ctx.get("rescue_decision_context", row.rescue_decision_context) or row.rescue_decision_context)
            if row.signal_id == "":
                row.signal_id = signal_id
            if row.lifecycle_id == "":
                row.lifecycle_id = lifecycle_id
            if row.status_after in {"ORDER_PLACED", "POSITION_OPENED", "POSITION_CLOSED"} and row.order_id == "":
                row.order_id = order_id
            if row.status_after in {"POSITION_OPENED", "POSITION_CLOSED"} and row.position_id == "":
                row.position_id = position_id
        return out_rows
    rows: List[LifecycleRow] = [
        LifecycleRow(
            candidate.timestamp,
            candidate.symbol,
            candidate.side,
            candidate.setup_type,
            candidate.setup_reason,
            candidate.regime,
            candidate.score,
            candidate.rr,
            candidate.entry,
            candidate.sl,
            candidate.tp,
            "NONE",
            "SIGNAL_CREATED",
            order_type=candidate.order_type,
            expectancy_bucket=candidate.expectancy_bucket,
            volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
            spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
            funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
            expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
            volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
            liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
            signal_id=signal_id,
            lifecycle_id=lifecycle_id,
        )
    ]
    rows.append(
        LifecycleRow(
            candidate.timestamp, candidate.symbol, candidate.side, candidate.setup_type, candidate.setup_reason, candidate.regime,
            candidate.score, candidate.rr, candidate.entry, candidate.sl, candidate.tp, "SIGNAL_CREATED", "SIGNAL_ACCEPTED",
            order_type=candidate.order_type, expectancy_bucket=candidate.expectancy_bucket,
            volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
            spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
            funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
            expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
            volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
            liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
            signal_id=signal_id, lifecycle_id=lifecycle_id,
        )
    )
    rows.append(
        LifecycleRow(
            candidate.timestamp,
            candidate.symbol,
            candidate.side,
            candidate.setup_type,
            candidate.setup_reason,
            candidate.regime,
            candidate.score,
            candidate.rr,
            candidate.entry,
            candidate.sl,
            candidate.tp,
            "SIGNAL_ACCEPTED",
            "WAITING_ENTRY_ZONE",
            order_type=candidate.order_type,
            expectancy_bucket=candidate.expectancy_bucket,
            volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
            spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
            funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
            expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
            volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
            liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
            signal_id=signal_id, lifecycle_id=lifecycle_id,
        )
    )
    triggered_ts = None
    trigger_price = 0.0
    if candidate.order_type in {"MARKET", "BREAKOUT", "IMMEDIATE"}:
        triggered_ts = candles[idx].timestamp
        trigger_price = candidate.entry
        start_idx = idx
        rows.append(
            LifecycleRow(
                candidate.timestamp,
                candidate.symbol,
                candidate.side,
                candidate.setup_type,
                candidate.setup_reason,
                candidate.regime,
                candidate.score,
                candidate.rr,
                candidate.entry,
                candidate.sl,
                candidate.tp,
                "WAITING_ENTRY_ZONE",
                "ENTRY_TRIGGERED",
                trigger_price=trigger_price,
                order_type=candidate.order_type,
                expectancy_bucket=candidate.expectancy_bucket,
                volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
                spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
                expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
                liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                signal_id=signal_id, lifecycle_id=lifecycle_id,
            )
        )
        rows.append(
            LifecycleRow(
                candidate.timestamp,
                candidate.symbol,
                candidate.side,
                candidate.setup_type,
                candidate.setup_reason,
                candidate.regime,
                candidate.score,
                candidate.rr,
                candidate.entry,
                candidate.sl,
                candidate.tp,
                "ENTRY_TRIGGERED",
                "ORDER_PLACED",
                trigger_price=trigger_price,
                order_type=candidate.order_type,
                expectancy_bucket=candidate.expectancy_bucket,
                volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
                spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
                expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
                liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                signal_id=signal_id, lifecycle_id=lifecycle_id, order_id=order_id,
            )
        )
        rows.append(
            LifecycleRow(
                candidate.timestamp, candidate.symbol, candidate.side, candidate.setup_type, candidate.setup_reason, candidate.regime,
                candidate.score, candidate.rr, candidate.entry, candidate.sl, candidate.tp, "ORDER_PLACED", "POSITION_OPENED",
                trigger_price=trigger_price, order_type=candidate.order_type, expectancy_bucket=candidate.expectancy_bucket,
                volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
                spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
                expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
                liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                signal_id=signal_id, lifecycle_id=lifecycle_id, order_id=order_id, position_id=position_id,
            )
        )
    else:
        start_idx = idx
        for j in range(idx, len(candles)):
            c = candles[j]
            if c.low <= candidate.entry <= c.high:
                triggered_ts = c.timestamp
                trigger_price = candidate.entry
                start_idx = j
                rows.append(
                    LifecycleRow(
                        candidate.timestamp,
                        candidate.symbol,
                        candidate.side,
                        candidate.setup_type,
                        candidate.setup_reason,
                        candidate.regime,
                        candidate.score,
                        candidate.rr,
                        candidate.entry,
                        candidate.sl,
                        candidate.tp,
                        "WAITING_ENTRY_ZONE",
                        "ENTRY_TRIGGERED",
                        trigger_price=trigger_price,
                        order_type=candidate.order_type,
                        expectancy_bucket=candidate.expectancy_bucket,
                        volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
                        spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                        funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
                        expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                        volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
                        liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                        signal_id=signal_id, lifecycle_id=lifecycle_id,
                    )
                )
                rows.append(
                    LifecycleRow(
                        candidate.timestamp,
                        candidate.symbol,
                        candidate.side,
                        candidate.setup_type,
                        candidate.setup_reason,
                        candidate.regime,
                        candidate.score,
                        candidate.rr,
                        candidate.entry,
                        candidate.sl,
                        candidate.tp,
                        "ENTRY_TRIGGERED",
                        "ORDER_PLACED",
                        trigger_price=trigger_price,
                        order_type=candidate.order_type,
                        expectancy_bucket=candidate.expectancy_bucket,
                        volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
                        spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                        funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
                        expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                        volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
                        liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                        signal_id=signal_id, lifecycle_id=lifecycle_id, order_id=order_id,
                    )
                )
                rows.append(
                    LifecycleRow(
                        candidate.timestamp, candidate.symbol, candidate.side, candidate.setup_type, candidate.setup_reason, candidate.regime,
                        candidate.score, candidate.rr, candidate.entry, candidate.sl, candidate.tp, "ORDER_PLACED", "POSITION_OPENED",
                        trigger_price=trigger_price, order_type=candidate.order_type, expectancy_bucket=candidate.expectancy_bucket,
                        volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
                        spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                        funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
                        expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                        volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
                        liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                        signal_id=signal_id, lifecycle_id=lifecycle_id, order_id=order_id, position_id=position_id,
                    )
                )
                break
        if triggered_ts is None:
            rows.append(
                LifecycleRow(
                    candidate.timestamp,
                    candidate.symbol,
                    candidate.side,
                    candidate.setup_type,
                    candidate.setup_reason,
                    candidate.regime,
                    candidate.score,
                    candidate.rr,
                    candidate.entry,
                    candidate.sl,
                    candidate.tp,
                    "WAITING_ENTRY_ZONE",
                    "ENTRY_TIMEOUT",
                    cancel_reason="TIMEOUT",
                    order_type=candidate.order_type,
                    expectancy_bucket=candidate.expectancy_bucket,
                    volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
                    spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                    funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
                    expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                    volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
                    liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                )
            )
            return _finalize_rows(rows)
    mfe = 0.0
    mae = 0.0
    long_side = str(candidate.side).upper() == "LONG"
    tp_distance = max(abs(candidate.tp - candidate.entry), 1e-9)
    sl_distance = max(abs(candidate.entry - candidate.sl), 1e-9)
    for j in range(start_idx, len(candles)):
        c = candles[j]
        if long_side:
            mfe = max(mfe, c.high - candidate.entry)
            mae = max(mae, candidate.entry - c.low)
            hit_tp = c.high >= candidate.tp
            hit_sl = c.low <= candidate.sl
        else:
            mfe = max(mfe, candidate.entry - c.low)
            mae = max(mae, c.high - candidate.entry)
            hit_tp = c.low <= candidate.tp
            hit_sl = c.high >= candidate.sl
        # Conservative same-candle rule:
        # if both TP and SL touch inside the same candle, count SL first.
        if hit_sl and hit_tp:
            hit_tp = False
        if hit_sl:
            pnl_pct = ((candidate.sl - candidate.entry) / candidate.entry) * 100 if long_side else ((candidate.entry - candidate.sl) / candidate.entry) * 100
            rows.append(
                finalize(
                    candidate,
                    "ORDER_PLACED",
                    "POSITION_CLOSED",
                    trigger_price,
                    candidate.sl,
                    "SL_HIT",
                    pnl_pct,
                    balance,
                    risk_pct,
                    triggered_ts,
                    c.timestamp,
                    market_ctx,
                    mfe / tp_distance,
                    mae / sl_distance,
                )
            )
            return _finalize_rows(rows)
        if hit_tp:
            pnl_pct = ((candidate.tp - candidate.entry) / candidate.entry) * 100 if long_side else ((candidate.entry - candidate.tp) / candidate.entry) * 100
            rows.append(
                finalize(
                    candidate,
                    "ORDER_PLACED",
                    "POSITION_CLOSED",
                    trigger_price,
                    candidate.tp,
                    "TP_HIT",
                    pnl_pct,
                    balance,
                    risk_pct,
                    triggered_ts,
                    c.timestamp,
                    market_ctx,
                    mfe / tp_distance,
                    mae / sl_distance,
                )
            )
            return _finalize_rows(rows)
    c = candles[-1]
    pnl_pct = ((c.close - candidate.entry) / candidate.entry) * 100 if long_side else ((candidate.entry - c.close) / candidate.entry) * 100
    rows.append(
        finalize(
            candidate,
            "ORDER_PLACED",
            "POSITION_CLOSED",
            trigger_price,
            c.close,
            "TIMEOUT",
            pnl_pct,
            balance,
            risk_pct,
            triggered_ts,
            c.timestamp,
            market_ctx,
            mfe / tp_distance,
            mae / sl_distance,
        )
    )
    return _finalize_rows(rows)
def finalize(
    candidate,
    before,
    after,
    trigger_price,
    close_price,
    close_reason,
    pnl_pct,
    balance,
    risk_pct,
    triggered_ts,
    closed_ts,
    market_ctx: Optional[Mapping[str, Any]] = None,
    mfe: float = 0.0,
    mae: float = 0.0,
):
    market_ctx = market_ctx or {}
    risk_usdt = balance * (risk_pct / 100)
    net_pnl_usdt = risk_usdt * (pnl_pct / 100)
    hold = (closed_ts - triggered_ts) / 60000 if triggered_ts else 0
    return LifecycleRow(
        candidate.timestamp,
        candidate.symbol,
        candidate.side,
        candidate.setup_type,
        candidate.setup_reason,
        candidate.regime,
        candidate.score,
        candidate.rr,
        candidate.entry,
        candidate.sl,
        candidate.tp,
        before,
        after,
        trigger_price=trigger_price,
        close_price=close_price,
        close_reason=close_reason,
        net_pnl_pct=pnl_pct,
        net_pnl_usdt=net_pnl_usdt,
        hold_minutes=hold,
        order_type=candidate.order_type,
        expectancy_bucket=candidate.expectancy_bucket,
        volume_24h_usdt=market_ctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
        spread_pct=market_ctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
        funding_rate_pct=market_ctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
        expected_slippage_pct=market_ctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
        volatility_regime=str(market_ctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
        liquidity_score=market_ctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
        effective_rr=_execution_reject_flags(candidate.rr, market_ctx)[0],
        cost_penalty=_execution_reject_flags(candidate.rr, market_ctx)[2]["cost_penalty_total"],
        stop_too_wide_softened=bool(market_ctx.get("stop_too_wide_softened", False)),
        original_reject_reason=str(market_ctx.get("original_reject_reason", "") or ""),
        reject_reason_softened=str(market_ctx.get("reject_reason_softened", "") or ""),
        risk_scale=_safe_float(market_ctx.get("risk_scale"), 1.0),
        accepted_reason=str(market_ctx.get("accepted_reason", "BASELINE") or "BASELINE"),
        rescue_size_multiplier=_safe_float(market_ctx.get("rescue_size_multiplier"), 1.0),
        rescue_effective_rr=_safe_float(market_ctx.get("rescue_effective_rr"), 0.0),
        rescue_decision_context=str(market_ctx.get("rescue_decision_context", "") or ""),
        bypassed_reject_reasons=json.dumps(market_ctx.get("bypassed_reject_reasons", []), sort_keys=True) if not isinstance(market_ctx.get("bypassed_reject_reasons", ""), str) else str(market_ctx.get("bypassed_reject_reasons", "")),
        disabled_filters=json.dumps(market_ctx.get("disabled_filters", []), sort_keys=True) if not isinstance(market_ctx.get("disabled_filters", ""), str) else str(market_ctx.get("disabled_filters", "")),
        disabled_filter_bypass_count=int(market_ctx.get("disabled_filter_bypass_count", 0) or 0),
        filter_switch_experiment_active=bool(market_ctx.get("filter_switch_experiment_active", False)),
        mfe=mfe,
        mae=mae,
    )
def simulate_rejected_counterfactual(
    candidate: CandidateOrder,
    candles: List[Candle],
    idx: int,
    timeout_bars: int = 240,
) -> dict[str, Any]:
    if idx >= len(candles):
        return {
            "outcome": "UNKNOWN",
            "would_trigger": False,
            "would_tp_hit": False,
            "would_sl_hit": False,
            "max_favorable_excursion": 0.0,
            "max_adverse_excursion": 0.0,
        }
    would_trigger = False
    would_tp = False
    would_sl = False
    mfe = 0.0
    mae = 0.0
    scan = candles[idx:idx + timeout_bars]
    side = str(candidate.side).upper()
    for c in scan:
        if c.low <= candidate.entry <= c.high:
            would_trigger = True
        if would_trigger:
            if side == "SHORT":
                mfe = max(mfe, candidate.entry - c.low)
                mae = max(mae, c.high - candidate.entry)
                hit_tp = c.low <= candidate.tp
                hit_sl = c.high >= candidate.sl
            else:
                mfe = max(mfe, c.high - candidate.entry)
                mae = max(mae, candidate.entry - c.low)
                hit_tp = c.high >= candidate.tp
                hit_sl = c.low <= candidate.sl
            # Conservative same-candle tie-breaker for both LONG and SHORT.
            # We cannot infer intrabar path from OHLC, so if both TP and SL touch,
            # count it as a stop loss to avoid optimistic bias.
            if hit_sl and hit_tp:
                hit_tp = False
            if hit_sl:
                would_sl = True
                break
            if hit_tp:
                would_tp = True
                break
    if not would_trigger:
        outcome = "WOULD_NOT_TRIGGER"
    elif would_tp:
        outcome = "WOULD_TP"
    elif would_sl:
        outcome = "WOULD_SL"
    elif len(scan) < timeout_bars and idx + timeout_bars > len(candles):
        outcome = "UNKNOWN"
    else:
        outcome = "WOULD_TIMEOUT"
    return {
        "outcome": outcome,
        "would_trigger": would_trigger,
        "would_tp_hit": would_tp,
        "would_sl_hit": would_sl,
        "max_favorable_excursion": mfe,
        "max_adverse_excursion": mae,
    }
def _update_recent_stats_after_close(recent_stats: Dict[str, Any], symbol: str, close_reason: str) -> None:
    if close_reason == "SL_HIT":
        recent_stats["consecutive_sl_count"] = int(recent_stats.get("consecutive_sl_count", 0) or 0) + 1
        recent_stats["consecutive_tp_count"] = 0
    elif close_reason == "TP_HIT":
        recent_stats["consecutive_tp_count"] = int(recent_stats.get("consecutive_tp_count", 0) or 0) + 1
        recent_stats["consecutive_sl_count"] = 0
    outcomes = recent_stats.setdefault("outcomes", [])
    if close_reason in {"TP_HIT", "SL_HIT"}:
        outcomes.append(1 if close_reason == "TP_HIT" else 0)
    window = int(recent_stats.get("rolling_window", 20) or 20)
    recent = outcomes[-window:]
    recent_stats["rolling_winrate"] = (sum(recent) / len(recent)) if recent else 0.0
def _offline_fixture(start_ms: int) -> tuple[list[dict[str, float]], dict[str, list[Candle]]]:
    universe = [{"symbol": "BTCUSDT", "quoteVolume": 100000000.0}]
    candles: list[Candle] = []
    base = 100.0
    for i in range(30):
        ts = start_ms + i * 60_000
        o = base + i * 0.2
        h = o + 0.6
        l = o - 0.4
        c = o + 0.3
        candles.append(Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=1000.0 + i))
    return universe, {"BTCUSDT": candles}
def process_backtest_result(
    symbol: str,
    candle: Candle,
    idx: int,
    candles: List[Candle],
    result: Dict[str, Any],
    mctx: Mapping[str, Any],
    balance: float,
    risk_pct: float,
    lifecycle: List[LifecycleRow],
    rejected: List[Dict[str, Any]],
    rejection_counts: Dict[str, int],
    open_rows: List[LifecycleRow],
    recent_stats: Dict[str, Any],
    rescue_config: Optional[RescueConfig] = None,
    rescue_stats: Optional[RescueStats] = None,
    short_breakdown_rescue_config: Optional[ShortBreakdownRescueConfig] = None,
    mode: str = "BACKTEST",
    disabled_backtest_filters: Iterable[str] = (),
    strategy_guardrail_config: Optional[StrategyQualityGuardrailConfig] = None,
) -> Optional[CandidateOrder]:
    strategy_guardrail_config = strategy_guardrail_config or strategy_guardrail_config_from_env()
    rescue_config = rescue_config or RescueConfig()
    short_breakdown_rescue_config = short_breakdown_rescue_config or ShortBreakdownRescueConfig()
    rescue_stats = rescue_stats or RescueStats()
    diagnostics = result.get("diagnostics", {})
    disabled_backtest_filters = tuple(sorted({str(r).upper() for r in disabled_backtest_filters}))
    side = diagnostics.get("side") or "LONG"
    setup_type = diagnostics.get("setup_type", mctx.get("setup_type", ""))
    setup_reason = diagnostics.get("setup_reason", mctx.get("setup_reason", ""))
    regime = diagnostics.get("regime", mctx.get("regime", ""))
    score = float(diagnostics.get("score", mctx.get("score", 0.0)) or 0.0)
    rr = float(diagnostics.get("rr", mctx.get("rr", 0.0)) or 0.0)
    entry = float(diagnostics.get("entry", mctx.get("entry", 0.0)) or 0.0)
    sl = float(diagnostics.get("sl", mctx.get("sl", 0.0)) or 0.0)
    tp = float(diagnostics.get("tp", mctx.get("tp", 0.0)) or 0.0)
    order_type = diagnostics.get("order_type", "LIMIT")
    expectancy = diagnostics.get("expectancy", mctx.get("expectancy"))
    expectancy_bucket = _bucket_expectancy(expectancy)
    signal_id = f"{symbol}:{candle.timestamp}"
    base_effective_rr, _base_execution_flags, _base_penalty_breakdown = _execution_reject_flags(rr, mctx)
    execution_ctx_missing = any(
        value == "UNAVAILABLE_BACKTEST"
        for value in (
            mctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
            mctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
            mctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
            mctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
            mctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
        )
    )
    def _try_rescue(reject_reason: str, effective_rr_value: float) -> Optional[CandidateOrder]:
        sbr = short_breakdown_rescue_config
        reason_norm = str(reject_reason or "").upper()
        regime_norm = str(regime or mctx.get("regime", "")).upper()
        first_gate = str((diagnostics.get("failed_filter") if isinstance(diagnostics, dict) else "") or reason_norm).upper()
        liquidity_ok = mctx.get("liquidity_ok") is not False
        volatility_raw = mctx.get("volatility_ok", "UNAVAILABLE_BACKTEST")
        volatility_ok = volatility_raw is not False or str(volatility_raw).upper() == "UNAVAILABLE_BACKTEST"
        spread = _safe_float(mctx.get("spread_pct"), -1.0)
        slip = _safe_float(mctx.get("expected_slippage_pct"), -1.0)
        sbr_mode_ok = str(mode).upper() == "BACKTEST" and "BACKTEST" in {m.upper() for m in sbr.modes}
        sbr_candidate = (
            sbr.enabled and sbr_mode_ok
            and str(side).upper() == "SHORT" and str(setup_type).upper() == "BREAKDOWN_DOWN"
            and (reason_norm in set(sbr.allowed_reasons) or first_gate in set(sbr.allowed_reasons))
        )
        if sbr_candidate:
            rescue_stats.candidate_count += 1
        short_breakdown_ok = (
            sbr_candidate
            and ("BREAKOUT" in regime_norm or regime_norm == "NORMAL" or str(mctx.get("volatility_regime", "")).upper() == "NORMAL")
            and liquidity_ok and volatility_ok
            and effective_rr_value >= sbr.min_effective_rr
            and (spread < 0.0 or spread <= sbr.max_spread_pct) and (slip < 0.0 or slip <= sbr.max_slippage_pct)
            and rescue_stats.accepted_count < sbr.max_trades_per_day
        )
        if sbr_candidate and not short_breakdown_ok:
            rescue_stats.rejected_count += 1
        if short_breakdown_ok:
            rescue_reason = SHORT_BREAKDOWN_RESCUE_REASON
            rescue_size_multiplier = sbr.size_multiplier
        else:
            ok, rescue_reason = _rescue_acceptance_allowed(
            mode=mode, reason=reject_reason, score=score, effective_rr=effective_rr_value, regime=regime,
            mctx=mctx, cfg=rescue_config, stats=rescue_stats, recent_stats=recent_stats, open_rows=open_rows, symbol=symbol,
        )
        if not short_breakdown_ok:
            if not ok:
                return None
            rescue_size_multiplier = rescue_config.size_multiplier
        rescue_stats.accepted_count += 1
        context = {
            "accepted_reason": rescue_reason,
            "original_reject_reason": reject_reason,
            "rescue_size_multiplier": rescue_size_multiplier,
            "rescue_effective_rr": effective_rr_value,
            "rescue_score": score,
            "rescue_mode": mode,
            "rescue_min_shadow_expectancy": getattr(sbr, "min_shadow_expectancy", 0.0) if rescue_reason == SHORT_BREAKDOWN_RESCUE_REASON else None,
        }
        rescued = CandidateOrder(
            candle.timestamp, symbol, side, entry, sl, tp, rr, setup_type, setup_reason, regime, score, order_type,
            expectancy_bucket=expectancy_bucket, accepted_reason=rescue_reason, original_reject_reason=reject_reason,
            rescue_size_multiplier=rescue_size_multiplier, rescue_effective_rr=effective_rr_value,
            rescue_decision_context=json.dumps(context, sort_keys=True),
        )
        sim_ctx = {**dict(mctx), **context, "risk_scale": rescue_size_multiplier, "rescue_decision_context": json.dumps(context, sort_keys=True)}
        sim_rows = simulate_candidate(rescued, candles, idx, balance, risk_pct * rescue_size_multiplier, market_ctx=sim_ctx)
        lifecycle.extend(sim_rows)
        recent_stats["last_trade_ts_by_symbol"][symbol] = candle.timestamp
        recent_stats["trades_today_by_symbol"][symbol] = int(recent_stats["trades_today_by_symbol"].get(symbol, 0)) + 1
        recent_stats["global_trades_today"] += 1
        for sim_row in sim_rows:
            if sim_row.close_reason == "TIMEOUT":
                open_rows.append(sim_row)
            if sim_row.status_after == "POSITION_CLOSED":
                _update_recent_stats_after_close(recent_stats, symbol, sim_row.close_reason)
        return rescued
    lifecycle.append(
        LifecycleRow(
            timestamp=candle.timestamp,
            symbol=symbol,
            side=side,
            setup_type=setup_type,
            setup_reason=setup_reason,
            regime=regime,
            score=score,
            rr=rr,
            entry=entry,
            sl=sl,
            tp=tp,
            status_before="NONE",
            status_after="SIGNAL_CREATED",
            order_type=order_type,
            expectancy_bucket=expectancy_bucket,
            volume_24h_usdt=mctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
            spread_pct=mctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
            funding_rate_pct=mctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
            expected_slippage_pct=mctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
            volatility_regime=str(mctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
            liquidity_score=mctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
            effective_rr=base_effective_rr,
            cost_penalty=_safe_float(_base_penalty_breakdown.get("cost_penalty_total"), 0.0),
            stop_too_wide_softened=bool(diagnostics.get("stop_too_wide_softened", False)) if isinstance(diagnostics, dict) else False,
            original_reject_reason=str(diagnostics.get("original_reject_reason", "") or "") if isinstance(diagnostics, dict) else "",
            reject_reason_softened=str(diagnostics.get("reject_reason_softened", "") or "") if isinstance(diagnostics, dict) else "",
            risk_scale=_safe_float(diagnostics.get("risk_scale"), 1.0) if isinstance(diagnostics, dict) else 1.0,
            signal_id=signal_id,
            lifecycle_id=signal_id,
        )
    )
    if result.get("status") == "rejected":
        raw_reason = str(result.get("reason") or result.get("reject_reason") or "").strip().upper()
        reason = _primary_reject_reason_from_context(
            current_reason=raw_reason,
            diagnostics=diagnostics if isinstance(diagnostics, Mapping) else {},
            market_ctx=mctx,
            execution_ctx_missing=execution_ctx_missing,
        )
        if isinstance(diagnostics, dict):
            secondary = [str(x).upper() for x in diagnostics.get("all_failed_gates", [])] if isinstance(diagnostics.get("all_failed_gates"), list) else []
            diagnostics.setdefault("primary_reject_reason", reason)
            diagnostics.setdefault("secondary_reject_reasons", secondary)
        rescued = _try_rescue(reason, base_effective_rr)
        if rescued is not None:
            return rescued
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        lifecycle.append(
            LifecycleRow(
                timestamp=candle.timestamp,
                symbol=symbol,
                side=side,
                setup_type=setup_type,
                setup_reason=setup_reason,
                regime=regime,
                score=score,
                rr=rr,
                entry=entry,
                sl=sl,
                tp=tp,
                status_before="SIGNAL_CREATED",
                status_after="SIGNAL_REJECTED",
                reject_reason=reason,
                order_type=order_type,
                expectancy_bucket=expectancy_bucket,
                volume_24h_usdt=mctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
                spread_pct=mctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                funding_rate_pct=mctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
                expected_slippage_pct=mctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                volatility_regime=str(mctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
                liquidity_score=mctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                effective_rr=base_effective_rr,
                cost_penalty=_safe_float(_base_penalty_breakdown.get("cost_penalty_total"), 0.0),
                stop_too_wide_softened=bool(diagnostics.get("stop_too_wide_softened", False)) if isinstance(diagnostics, dict) else False,
                original_reject_reason=str(diagnostics.get("original_reject_reason", "") or "") if isinstance(diagnostics, dict) else "",
                reject_reason_softened=str(diagnostics.get("reject_reason_softened", "") or "") if isinstance(diagnostics, dict) else "",
                risk_scale=_safe_float(diagnostics.get("risk_scale"), 1.0) if isinstance(diagnostics, dict) else 1.0,
                signal_id=signal_id,
                lifecycle_id=signal_id,
            )
        )
        rejected.append(
            {
                "signal_id": signal_id,
                "lifecycle_state": "SIGNAL_REJECTED",
                "execution_ctx_missing": execution_ctx_missing,
                "expectancy_bucket": expectancy_bucket,
                "timestamp": candle.timestamp,
                "symbol": symbol,
                "side": side,
                "setup_type": setup_type,
                "setup_reason": setup_reason,
                "regime": regime,
                "score": score,
                "gate_score": _safe_float(diagnostics.get("score"), score),
                "rr": rr,
                "expectancy": expectancy,
                "quality_score": diagnostics.get("quality_score", 0.0),
                "reject_reason": reason,
                "diagnostics": json.dumps(diagnostics, sort_keys=True),
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "spread_pct": mctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                "liquidity_score": mctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                "volatility_score": mctx.get("volatility_pct", mctx.get("spread_pct", "UNAVAILABLE_BACKTEST")),
                "expected_slippage_pct": mctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                "raw_rr": rr,
                "effective_rr": diagnostics.get("effective_rr", base_effective_rr),
                **_base_penalty_breakdown,
                "cost_penalty": _safe_float(_base_penalty_breakdown.get("cost_penalty_total"), 0.0),
                "stop_too_wide_softened": bool(diagnostics.get("stop_too_wide_softened", False)) if isinstance(diagnostics, dict) else False,
                "original_reject_reason": diagnostics.get("original_reject_reason", "") if isinstance(diagnostics, dict) else "",
                "reject_reason_softened": diagnostics.get("reject_reason_softened", "") if isinstance(diagnostics, dict) else "",
                "risk_scale": _safe_float(diagnostics.get("risk_scale"), 1.0) if isinstance(diagnostics, dict) else 1.0,
                "min_required_score": ((diagnostics.get("adaptive_thresholds") or {}).get("min_score") if isinstance(diagnostics, dict) else None),
                "trend_strength": mctx.get("trend_strength", "UNAVAILABLE_BACKTEST"),
                "volatility_pct": mctx.get("volatility_pct", "UNAVAILABLE_BACKTEST"),
                "range_position": mctx.get("range_position", "UNAVAILABLE_BACKTEST"),
                "spread_pct": mctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                "slippage_pct": mctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                "liquidity_score": mctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                "first_blocking_gate": diagnostics.get("failed_filter", ""),
                "all_failed_gates": json.dumps(diagnostics.get("all_failed_gates", []), sort_keys=True) if isinstance(diagnostics, dict) else "[]",
                "secondary_reject_reasons": json.dumps(diagnostics.get("secondary_reject_reasons", []), sort_keys=True) if isinstance(diagnostics, dict) else "[]",
                **_low_score_rescue_watch_fields(reason, diagnostics if isinstance(diagnostics, dict) else {}),
            }
        )
        return None
    if result.get("status") != "executed":
        return None
    c = result["candidate"]
    cand = CandidateOrder(
        candle.timestamp,
        symbol,
        c.side,
        c.entry,
        c.sl,
        c.tp,
        c.rr,
        c.setup_type,
        c.setup_reason,
        c.regime,
        c.score,
        c.order_type,
        expectancy_bucket=expectancy_bucket,
    )
    effective_rr, execution_flags, penalty_breakdown = _execution_reject_flags(cand.rr, mctx)
    if execution_flags:
        reason = execution_flags[0]
        rescued = _try_rescue(reason, effective_rr)
        if rescued is not None:
            return rescued
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        lifecycle.append(
            LifecycleRow(
                timestamp=candle.timestamp,
                symbol=symbol,
                side=cand.side,
                setup_type=cand.setup_type,
                setup_reason=cand.setup_reason,
                regime=cand.regime,
                score=cand.score,
                rr=cand.rr,
                entry=cand.entry,
                sl=cand.sl,
                tp=cand.tp,
                status_before="SIGNAL_CREATED",
                status_after="ORDER_REJECTED",
                reject_reason=reason,
                order_type=cand.order_type,
                expectancy_bucket=cand.expectancy_bucket,
                volume_24h_usdt=mctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
                spread_pct=mctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                funding_rate_pct=mctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
                expected_slippage_pct=mctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                volatility_regime=str(mctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")),
                liquidity_score=mctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                effective_rr=effective_rr,
                cost_penalty=_safe_float(penalty_breakdown.get("cost_penalty_total"), 0.0),
                signal_id=signal_id,
                lifecycle_id=signal_id,
            )
        )
        rejected.append(
            {
                "signal_id": signal_id,
                "lifecycle_state": "ORDER_REJECTED",
                "execution_ctx_missing": execution_ctx_missing,
                "expectancy_bucket": cand.expectancy_bucket,
                "timestamp": candle.timestamp,
                "symbol": symbol,
                "side": cand.side,
                "setup_type": cand.setup_type,
                "setup_reason": cand.setup_reason,
                "regime": cand.regime,
                "score": cand.score,
                "gate_score": _safe_float(diagnostics.get("score"), cand.score),
                "rr": cand.rr,
                "expectancy": expectancy,
                "quality_score": diagnostics.get("quality_score", ""),
                "reject_reason": reason,
                "diagnostics": json.dumps(
                    {"effective_rr": effective_rr, "execution_flags": execution_flags, **penalty_breakdown},
                    sort_keys=True,
                ),
                "entry": cand.entry,
                "sl": cand.sl,
                "tp": cand.tp,
                "spread_pct": mctx.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                "liquidity_score": mctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                "volatility_score": mctx.get("volatility_pct", mctx.get("spread_pct", "UNAVAILABLE_BACKTEST")),
                "expected_slippage_pct": mctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                "raw_rr": cand.rr,
                "effective_rr": effective_rr,
                **penalty_breakdown,
                "min_required_score": ((diagnostics.get("adaptive_thresholds") or {}).get("min_score") if isinstance(diagnostics, dict) else None),
                "trend_strength": mctx.get("trend_strength", "UNAVAILABLE_BACKTEST"),
                "volatility_pct": mctx.get("volatility_pct", "UNAVAILABLE_BACKTEST"),
                "range_position": mctx.get("range_position", "UNAVAILABLE_BACKTEST"),
                "slippage_pct": mctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
                "first_blocking_gate": "execution",
                "all_failed_gates": json.dumps(execution_flags, sort_keys=True),
                **_low_score_rescue_watch_fields(reason, {}),
            }
        )
        return None
    guard_reason = _guardrail_rejection_reason(symbol, candle.timestamp, cand.regime, cand.score, effective_rr, {**dict(mctx), **penalty_breakdown, **({"stop_too_wide_softened": diagnostics.get("stop_too_wide_softened", False)} if isinstance(diagnostics, dict) else {})}, recent_stats, strategy_guardrail_config)
    if guard_reason:
        _append_guardrail_reject(guard_reason, symbol, candle, cand, mctx, effective_rr, diagnostics if isinstance(diagnostics, dict) else {}, lifecycle, rejected, rejection_counts, execution_ctx_missing, _safe_float(penalty_breakdown.get("cost_penalty_total"), 0.0))
        return None

    risk_scale = min(1.0, max(0.0, _safe_float(diagnostics.get("risk_scale"), 1.0))) if isinstance(diagnostics, dict) else 1.0
    sim_ctx = {**dict(mctx), "risk_scale": risk_scale}
    if isinstance(diagnostics, dict):
        for key in ("bypassed_reject_reasons", "disabled_filters", "disabled_filter_bypass_count", "filter_switch_experiment_active"):
            if key in diagnostics:
                sim_ctx[key] = diagnostics[key]
    if isinstance(diagnostics, dict):
        for key in ("stop_too_wide_softened", "original_reject_reason", "reject_reason_softened"):
            if key in diagnostics:
                sim_ctx[key] = diagnostics[key]
    sim_rows = simulate_candidate(cand, candles, idx, balance, risk_pct * risk_scale, market_ctx=sim_ctx)
    lifecycle.extend(sim_rows)
    recent_stats["last_trade_ts_by_symbol"][symbol] = candle.timestamp
    recent_stats["trades_today_by_symbol"][symbol] = int(recent_stats["trades_today_by_symbol"].get(symbol, 0)) + 1
    recent_stats["global_trades_today"] += 1
    _record_guardrail_acceptance(recent_stats, symbol, candle.timestamp, cand.regime, sim_ctx)
    for sim_row in sim_rows:
        if sim_row.close_reason == "TIMEOUT":
            open_rows.append(sim_row)
        if sim_row.status_after == "POSITION_CLOSED":
            _update_recent_stats_after_close(recent_stats, symbol, sim_row.close_reason)
    return cand


def _day_key(ts: int) -> str:
    try:
        return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return "UNKNOWN"


def _is_high_vol_context(regime: str, mctx: Mapping[str, Any]) -> bool:
    text = " ".join([str(regime or ""), str(mctx.get("regime", "")), str(mctx.get("volatility_regime", ""))]).upper()
    return "HIGH" in text or "VOL" in text or "BREAKOUT" in text


def _guardrail_rejection_reason(symbol: str, timestamp: int, regime: str, score: float, effective_rr: float, mctx: Mapping[str, Any], recent_stats: Mapping[str, Any], cfg: StrategyQualityGuardrailConfig) -> str:
    if not cfg.enabled or cfg.profile in {"HIGH_VOL_MOMENTUM_DIAGNOSTIC", "HIGH_VOL_GUARD_OFF_DIAGNOSTIC", "VOL_GUARD_RELAXED_DIAGNOSTIC"}:
        return ""
    day = _day_key(timestamp)
    daily = recent_stats.get("accepted_trades_by_day", {}) or {}
    sym_daily = recent_stats.get("accepted_trades_by_symbol_day", {}) or {}
    sym_regime_daily = recent_stats.get("accepted_trades_by_symbol_regime_day", {}) or {}
    if int(recent_stats.get("consecutive_sl_count", 0) or 0) >= cfg.max_consecutive_sl_pause:
        return "LOSS_STREAK_PAUSE"
    if int(daily.get(day, 0) or 0) >= cfg.max_accepted_trades_per_day:
        return "DAILY_TRADE_FREQUENCY_GUARD"
    if int(sym_daily.get(f"{symbol}:{day}", 0) or 0) >= cfg.max_symbol_trades_per_day:
        return "SYMBOL_CLUSTER_GUARD"
    if int(sym_regime_daily.get(f"{symbol}:{str(regime).upper()}:{day}", 0) or 0) >= cfg.max_symbol_regime_trades_per_day:
        return "SYMBOL_CLUSTER_GUARD"
    cost = _safe_float(mctx.get("cost_penalty_total", mctx.get("cost_penalty")), 0.0)
    high_vol = _is_high_vol_context(regime, mctx)
    if high_vol and cfg.high_vol_acceptance_guard:
        hv_daily = recent_stats.get("high_vol_accepted_trades_by_day", {}) or {}
        if int(hv_daily.get(day, 0) or 0) >= cfg.high_vol_max_trades_per_day:
            return "HIGH_VOL_OVERTRADE"
        if cost > cfg.high_vol_max_cost_penalty:
            return "HIGH_VOL_EXECUTION_COST"
        if effective_rr < cfg.high_vol_min_effective_rr or bool(mctx.get("stop_too_wide_softened", False)):
            return "HIGH_VOL_GUARD"
    if cfg.score10_sl_dominance_guard and score >= cfg.saturated_score_threshold:
        regime_ok = str(regime or "").upper() in {"TREND", "BREAKOUT"}
        if effective_rr < cfg.saturated_min_effective_rr or not regime_ok or high_vol or cost > cfg.saturated_max_cost_penalty:
            return "SCORE_SATURATION_GUARD"
    return ""


def _append_guardrail_reject(reason: str, symbol: str, candle: Candle, cand: CandidateOrder, mctx: Mapping[str, Any], effective_rr: float, diagnostics: Mapping[str, Any], lifecycle: List[LifecycleRow], rejected: List[Dict[str, Any]], rejection_counts: Dict[str, int], execution_ctx_missing: bool, cost_penalty: float) -> None:
    rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
    signal_id = f"{symbol}:{candle.timestamp}"
    lifecycle.append(LifecycleRow(timestamp=candle.timestamp, symbol=symbol, side=cand.side, setup_type=cand.setup_type, setup_reason=cand.setup_reason, regime=cand.regime, score=cand.score, rr=cand.rr, entry=cand.entry, sl=cand.sl, tp=cand.tp, status_before="SIGNAL_CREATED", status_after="SIGNAL_REJECTED", reject_reason=reason, order_type=cand.order_type, expectancy_bucket=cand.expectancy_bucket, volume_24h_usdt=mctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"), spread_pct=mctx.get("spread_pct", "UNAVAILABLE_BACKTEST"), funding_rate_pct=mctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"), expected_slippage_pct=mctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"), volatility_regime=str(mctx.get("volatility_regime", "UNAVAILABLE_BACKTEST")), liquidity_score=mctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"), effective_rr=effective_rr, cost_penalty=cost_penalty, signal_id=signal_id, lifecycle_id=signal_id))
    rejected.append({"signal_id": signal_id, "lifecycle_state": "SIGNAL_REJECTED", "source_stage": "STRATEGY_QUALITY_GUARDRAIL", "execution_ctx_missing": execution_ctx_missing, "expectancy_bucket": cand.expectancy_bucket, "timestamp": candle.timestamp, "symbol": symbol, "side": cand.side, "setup_type": cand.setup_type, "setup_reason": cand.setup_reason, "regime": cand.regime, "score": cand.score, "gate_score": cand.score, "rr": cand.rr, "expectancy": mctx.get("expectancy"), "quality_score": diagnostics.get("quality_score", ""), "reject_reason": reason, "diagnostics": json.dumps({**dict(diagnostics), "strategy_quality_guardrail": reason, "guard_source_function": "backtest_order._guardrail_rejection_reason"}, sort_keys=True), "entry": cand.entry, "sl": cand.sl, "tp": cand.tp, "spread_pct": mctx.get("spread_pct", "UNAVAILABLE_BACKTEST"), "liquidity_score": mctx.get("liquidity_score", "UNAVAILABLE_BACKTEST"), "volatility_score": mctx.get("volatility_pct", mctx.get("spread_pct", "UNAVAILABLE_BACKTEST")), "atr_pct": mctx.get("atr_pct", diagnostics.get("atr_pct", "")), "candle_range_pct": mctx.get("candle_range_pct", diagnostics.get("candle_range_pct", "")), "realized_volatility_pct": mctx.get("realized_volatility_pct", diagnostics.get("realized_volatility_pct", "")), "volatility_regime": mctx.get("volatility_regime", "UNAVAILABLE_BACKTEST"), "volume_24h_usdt": mctx.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"), "funding_rate_pct": mctx.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"), "expected_slippage_pct": mctx.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"), "raw_rr": cand.rr, "effective_rr": effective_rr, "cost_penalty": cost_penalty, "first_blocking_gate": reason, "all_failed_gates": json.dumps([reason]), **_low_score_rescue_watch_fields(reason, {})})


def _record_guardrail_acceptance(recent_stats: Dict[str, Any], symbol: str, timestamp: int, regime: str, mctx: Mapping[str, Any]) -> None:
    day = _day_key(timestamp)
    recent_stats.setdefault("accepted_trades_by_day", {})[day] = int(recent_stats.setdefault("accepted_trades_by_day", {}).get(day, 0) or 0) + 1
    recent_stats.setdefault("accepted_trades_by_symbol_day", {})[f"{symbol}:{day}"] = int(recent_stats.setdefault("accepted_trades_by_symbol_day", {}).get(f"{symbol}:{day}", 0) or 0) + 1
    recent_stats.setdefault("accepted_trades_by_symbol_regime_day", {})[f"{symbol}:{str(regime).upper()}:{day}"] = int(recent_stats.setdefault("accepted_trades_by_symbol_regime_day", {}).get(f"{symbol}:{str(regime).upper()}:{day}", 0) or 0) + 1
    if _is_high_vol_context(regime, mctx):
        recent_stats.setdefault("high_vol_accepted_trades_by_day", {})[day] = int(recent_stats.setdefault("high_vol_accepted_trades_by_day", {}).get(day, 0) or 0) + 1

def _lifecycle_event_id(row: LifecycleRow, index: int, lifecycle_state: str | None = None) -> str:
    status_after = lifecycle_state or row.status_after
    return (
        f"{row.timestamp}:{row.symbol}:{row.status_before}:{status_after}:"
        f"{row.entry}:{row.sl}:{row.tp}:{index}"
    )
def _persist_lifecycle_rows(rows: List[LifecycleRow]) -> List[dict[str, Any]]:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        for idx, row in enumerate(rows):
            execution_ctx_missing = any(
                row_value == "UNAVAILABLE_BACKTEST"
                for row_value in (row.volume_24h_usdt, row.spread_pct, row.funding_rate_pct, row.expected_slippage_pct, row.liquidity_score)
            )
            lifecycle_state = normalize_lifecycle_event(row.status_after)
            pre_signal_symbol_reject = _is_pre_signal_symbol_reject(row, lifecycle_state)
            if lifecycle_state == "SYMBOL_REJECTED" and not pre_signal_symbol_reject:
                lifecycle_state = "SIGNAL_REJECTED"
            source_stage = "SYMBOL_SELECTOR" if pre_signal_symbol_reject else "SIGNAL_ENGINE"
            if lifecycle_state in {"SIGNAL_REJECTED", "ORDER_REJECTED", "SYMBOL_REJECTED"}:
                decision = "REJECTED"
                reject_reason = str(row.reject_reason or "").strip().upper() or REJECT_REASON_UNAVAILABLE
            elif lifecycle_state == "SIGNAL_CREATED":
                decision = "PENDING"
                reject_reason = ""
            else:
                decision = "ACCEPTED"
                reject_reason = row.reject_reason
            signal_id = _lifecycle_signal_id(row, lifecycle_state)
            execution_ctx = {
                "volume_24h_usdt": row.volume_24h_usdt,
                "spread_pct": row.spread_pct,
                "funding_rate_pct": row.funding_rate_pct,
                "expected_slippage_pct": row.expected_slippage_pct,
                "volatility_regime": row.volatility_regime,
                "liquidity_score": row.liquidity_score,
                "volatility_score": row.volatility_score,
                "liquidity_ok": row.liquidity_ok,
                "volatility_ok": row.volatility_ok,
                "shadow_outcome": row.shadow_outcome or None,
                "cost_penalty": row.cost_penalty,
                "close_reason": row.close_reason,
                "side": row.side,
                "entry": row.entry,
                "entry_price": row.entry,
                "sl": row.sl,
                "stop_loss": row.sl,
                "tp": row.tp,
                "take_profit": row.tp,
                "exit_price": row.close_price,
                "close_price": row.close_price,
                "gross_pnl": row.net_pnl_usdt,
                "net_pnl": row.net_pnl_usdt,
                "net_pnl_usdt": row.net_pnl_usdt,
                "fees": row.cost_penalty,
                "accepted_reason": row.accepted_reason,
                "original_reject_reason": row.original_reject_reason,
                "rescue_size_multiplier": row.rescue_size_multiplier,
                "rescue_effective_rr": row.rescue_effective_rr,
                "rescue_decision_context": row.rescue_decision_context,
                "bypassed_reject_reasons": row.bypassed_reject_reasons,
                "disabled_filters": row.disabled_filters,
                "disabled_filter_bypass_count": row.disabled_filter_bypass_count,
                "filter_switch_experiment_active": row.filter_switch_experiment_active,
                "source_stage": row.source_stage or source_stage,
                "rr_available": row.rr_available,
                "effective_rr_available": row.effective_rr_available,
                "expectancy_available": row.expectancy_available,
            }
            effective_rr = row.effective_rr if row.effective_rr is not None else row.rr
            save_signal(
                session,
                signal_id=signal_id,
                symbol=row.symbol,
                side=row.side,
                timeframe=None,
                mode="BACKTEST",
                score=row.score,
                rr=row.rr,
                effective_rr=effective_rr,
                expectancy_bucket=row.expectancy_bucket,
            )
            if decision == "REJECTED" or lifecycle_state == "ORDER_PLACED":
                save_order_decision(
                    session,
                    decision_id=f"{signal_id}:{lifecycle_state}:{row.lifecycle_seq or idx + 1}",
                    signal_id=signal_id,
                    order_id=row.order_id or None,
                    symbol=row.symbol,
                    mode="BACKTEST",
                    phase="final",
                    decision=decision,
                    reject_reason=reject_reason,
                    score=row.score,
                    rr=row.rr,
                    effective_rr=effective_rr,
                    expectancy_bucket=row.expectancy_bucket,
                    order_payload={"lifecycle_state": lifecycle_state, "reject_reason": reject_reason},
                    execution_ctx=execution_ctx,
                    execution_ctx_missing=execution_ctx_missing,
                    expected_slippage_pct=None if row.expected_slippage_pct == "UNAVAILABLE_BACKTEST" else row.expected_slippage_pct,
                    spread_pct=None if row.spread_pct == "UNAVAILABLE_BACKTEST" else row.spread_pct,
                    funding_rate_pct=None if row.funding_rate_pct == "UNAVAILABLE_BACKTEST" else row.funding_rate_pct,
                    volatility_regime=None if row.volatility_regime == "UNAVAILABLE_BACKTEST" else row.volatility_regime,
                )
            save_trade_lifecycle_event(
                session,
                event_id=_lifecycle_event_id(row, idx, lifecycle_state),
                signal_id=signal_id,
                order_id=row.order_id or None,
                symbol=row.symbol,
                mode="BACKTEST",
                lifecycle_state=lifecycle_state,
                decision=decision,
                reject_reason=reject_reason,
                score=row.score,
                rr=row.rr,
                effective_rr=effective_rr,
                expectancy_bucket=row.expectancy_bucket,
                execution_ctx=execution_ctx,
                execution_ctx_missing=execution_ctx_missing,
                event_ts=str(row.timestamp),
                lifecycle_seq=row.lifecycle_seq or (idx + 1),
                lifecycle_id=row.lifecycle_id or f"{row.symbol}:{row.timestamp}",
                payload={"source_stage": source_stage, "event_flags": row.event_flags},
            )
        decision_counts = {
            row["signal_id"]: {"sql_order_decision_count": row["total_count"], "sql_rejected_decision_count": row["rejected_count"]}
            for row in session.execute(
                text(
                    """
                    SELECT signal_id,
                           COUNT(*) AS total_count,
                           SUM(CASE WHEN UPPER(COALESCE(decision,''))='REJECTED' THEN 1 ELSE 0 END) AS rejected_count
                    FROM order_decisions
                    WHERE mode = 'BACKTEST'
                    GROUP BY signal_id
                    """
                )
            ).mappings().all()
        }
        persisted = session.execute(
            text(
                """
                SELECT event_id, signal_id, order_id, symbol, mode, lifecycle_state, decision, reject_reason,
                       score, rr, effective_rr, expectancy_bucket, execution_ctx, execution_ctx_missing,
                       json_extract(execution_ctx, '$.liquidity_score') AS liquidity_score,
                       json_extract(execution_ctx, '$.volatility_score') AS volatility_score,
                       json_extract(execution_ctx, '$.liquidity_ok') AS liquidity_ok,
                       json_extract(execution_ctx, '$.volatility_ok') AS volatility_ok,
                       json_extract(execution_ctx, '$.shadow_outcome') AS shadow_outcome,
                       json_extract(execution_ctx, '$.cost_penalty') AS cost_penalty,
                       json_extract(execution_ctx, '$.spread_pct') AS spread_pct,
                       json_extract(execution_ctx, '$.expected_slippage_pct') AS expected_slippage_pct,
                       json_extract(execution_ctx, '$.funding_rate_pct') AS funding_rate_pct,
                       json_extract(execution_ctx, '$.volume_24h_usdt') AS volume_24h_usdt,
                       json_extract(execution_ctx, '$.accepted_reason') AS accepted_reason,
                       json_extract(execution_ctx, '$.side') AS side,
                       json_extract(execution_ctx, '$.entry') AS entry,
                       json_extract(execution_ctx, '$.sl') AS sl,
                       json_extract(execution_ctx, '$.tp') AS tp,
                       json_extract(execution_ctx, '$.exit_price') AS exit_price,
                       json_extract(execution_ctx, '$.close_price') AS close_price,
                       json_extract(execution_ctx, '$.close_reason') AS close_reason,
                       json_extract(execution_ctx, '$.gross_pnl') AS gross_pnl,
                       json_extract(execution_ctx, '$.net_pnl') AS net_pnl,
                       json_extract(execution_ctx, '$.net_pnl_usdt') AS net_pnl_usdt,
                       json_extract(execution_ctx, '$.fees') AS fees,
                       json_extract(execution_ctx, '$.original_reject_reason') AS original_reject_reason,
                       json_extract(execution_ctx, '$.rescue_size_multiplier') AS rescue_size_multiplier,
                       json_extract(execution_ctx, '$.rescue_effective_rr') AS rescue_effective_rr,
                       json_extract(execution_ctx, '$.rescue_decision_context') AS rescue_decision_context,
                       json_extract(execution_ctx, '$.source_stage') AS source_stage,
                       json_extract(execution_ctx, '$.rr_available') AS rr_available,
                       json_extract(execution_ctx, '$.effective_rr_available') AS effective_rr_available,
                       json_extract(execution_ctx, '$.expectancy_available') AS expectancy_available,
                       event_ts, created_at, lifecycle_seq, lifecycle_id
                FROM trade_lifecycle_events
                WHERE mode = 'BACKTEST'
                ORDER BY event_ts, symbol, signal_id, COALESCE(lifecycle_seq, 0), lifecycle_state, event_id
                """
            )
        ).mappings().all()
    out = []
    for row in persisted:
        data = dict(row)
        data.update(decision_counts.get(data.get("signal_id"), {"sql_order_decision_count": 0, "sql_rejected_decision_count": 0}))
        out.append(data)
    return out


def _attach_rejected_shadow_to_lifecycle(rows: List[LifecycleRow], shadows: List[RejectedShadowEvaluation]) -> None:
    """Annotate rejected lifecycle decisions with shadow labels without accepting them."""
    shadow_by_key = {(s.symbol, int(s.timestamp)): s for s in shadows}
    for row in rows:
        if row.status_after not in {"SIGNAL_REJECTED", "ORDER_REJECTED"}:
            continue
        shadow = shadow_by_key.get((row.symbol, int(row.timestamp)))
        if shadow is None:
            continue
        row.effective_rr = shadow.effective_rr
        row.spread_pct = shadow.spread_pct
        row.liquidity_score = shadow.liquidity_score
        row.volatility_score = shadow.volatility_score
        row.shadow_outcome = shadow.shadow_outcome
        row.cost_penalty = shadow.cost_penalty
        row.liquidity_ok = shadow.liquidity_ok
        row.volatility_ok = shadow.volatility_ok


def _derive_backtest_counts(lifecycle: List[LifecycleRow]) -> Dict[str, int]:
    signal_ids = {
        f"{row.symbol}:{row.timestamp}"
        for row in lifecycle
        if row.status_after in {"SIGNAL_CREATED", "SYMBOL_REJECTED"}
    }
    total_candidates = len(signal_ids)
    rejected_count = sum(1 for row in lifecycle if row.status_after in {"SYMBOL_REJECTED", "SIGNAL_REJECTED", "ORDER_REJECTED"})
    accepted_count = total_candidates - rejected_count
    waiting_keys = {
        (row.order_id or f"{row.symbol}:{row.timestamp}", row.signal_id or f"{row.symbol}:{row.timestamp}")
        for row in lifecycle
        if row.status_after == "WAITING_ENTRY_ZONE"
    }
    triggered_keys = {
        (row.order_id or f"{row.symbol}:{row.timestamp}", row.signal_id or f"{row.symbol}:{row.timestamp}")
        for row in lifecycle
        if row.status_after == "ENTRY_TRIGGERED"
    }
    placed_keys = {
        (row.order_id or f"{row.symbol}:{row.timestamp}", row.signal_id or f"{row.symbol}:{row.timestamp}")
        for row in lifecycle
        if row.status_after == "ORDER_PLACED"
    }
    total_orders = len(placed_keys)
    triggered_orders = len(triggered_keys)
    not_triggered_orders = len(waiting_keys - (triggered_keys | placed_keys))
    open_at_end_orders = sum(1 for row in lifecycle if row.status_after == "POSITION_CLOSED" and row.close_reason == "TIMEOUT")
    tp_hits = sum(1 for row in lifecycle if row.status_after == "POSITION_CLOSED" and row.close_reason == "TP_HIT")
    sl_hits = sum(1 for row in lifecycle if row.status_after == "POSITION_CLOSED" and row.close_reason == "SL_HIT")
    return {
        "total_candidates": total_candidates,
        "accepted_count": accepted_count,
        "rejected_count": rejected_count,
        "total_orders": total_orders,
        "triggered_orders": triggered_orders,
        "not_triggered_orders": not_triggered_orders,
        "open_at_end_orders": open_at_end_orders,
        "tp_hits": tp_hits,
        "sl_hits": sl_hits,
    }


def _distribution(values: List[Any]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for value in values:
        key = str(value)
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda item: item[0]))




def _percentiles(values: List[float], points: List[int]) -> Dict[str, float]:
    if not values:
        return {f"p{pt}": 0.0 for pt in points}
    vals = sorted(float(v) for v in values)
    n = len(vals)
    out: Dict[str, float] = {}
    for pt in points:
        if n == 1:
            out[f"p{pt}"] = round(vals[0], 6)
            continue
        rank = (pt / 100.0) * (n - 1)
        lo = int(rank)
        hi = min(lo + 1, n - 1)
        w = rank - lo
        out[f"p{pt}"] = round(vals[lo] * (1.0 - w) + vals[hi] * w, 6)
    return out

def _value_unavailable(value: Any) -> bool:
    return value is None or value == "" or value == "UNAVAILABLE_BACKTEST"


def _low_score_rescue_watch_fields(reject_reason: str, row: Mapping[str, Any]) -> Dict[str, Any]:
    is_low_score = str(reject_reason or "").upper() == "LOW_SCORE"
    return {
        "rescue_watch_eligible": bool(is_low_score),
        "rescue_watch_reason": ("LOW_SCORE_DIAGNOSTIC_ONLY" if is_low_score else ""),
        "rescued_size_multiplier": (_safe_float(row.get("rescued_size_multiplier"), 0.0) if is_low_score else 0.0),
        "rescue_effective_rr": (_safe_float(row.get("rescued_effective_rr"), 0.0) if is_low_score else 0.0),
        "rescue_reject_reason": (str(row.get("rescue_reject_reason", "")) if is_low_score else ""),
    }


def build_backtest_quality_summary(rows: List[Mapping[str, Any]], canonical_rejected_rows: Optional[List[Mapping[str, Any]]] = None) -> Dict[str, Any]:
    def _normalized_decision(row: Mapping[str, Any]) -> str:
        decision = str(row.get("decision", "") or "").strip().upper()
        if decision:
            return decision
        for key in ("status", "status_after", "status_before", "lifecycle_state"):
            value = str(row.get(key, "") or "").strip().upper()
            if value:
                return value
        return ""

    def _is_metadata_row(row: Mapping[str, Any]) -> bool:
        # Skip only explicit aggregate rows. Plain decision dictionaries are candidates.
        if row.get("metric") is not None and row.get("value") is not None:
            return True
        marker = str(row.get("row_type", "") or "").strip().lower()
        return marker in {"summary", "metadata"}

    candidate_rows = [r for r in rows if not _is_metadata_row(r)]
    signal_created_rows = [r for r in candidate_rows if str(r.get("lifecycle_state", "")).strip().upper() == "SIGNAL_CREATED"]
    if signal_created_rows:
        candidate_rows_for_counts = signal_created_rows
    else:
        candidate_rows_for_counts = candidate_rows
    total = len(candidate_rows_for_counts)

    rejected_tokens = {"REJECTED", "SIGNAL_REJECTED", "ORDER_REJECTED", "SYMBOL_REJECTED"}
    accepted_tokens = {"ACCEPTED", "EXECUTED", "ENTRY_TRIGGERED", "ORDER_PLACED", "PARTIAL_FILL", "FILLED", "TP_HIT", "SL_HIT", "OPEN_AT_END"}

    candidate_signal_ids = {str(r.get("signal_id", "")).strip() for r in candidate_rows_for_counts if str(r.get("signal_id", "")).strip()}
    if signal_created_rows:
        rejected_signal_ids = {
            str(r.get("signal_id", "")).strip()
            for r in candidate_rows
            if (_normalized_decision(r) in rejected_tokens or str(r.get("reject_reason", "") or "").strip() != "")
            and str(r.get("signal_id", "")).strip()
        }
        rejected_rows = []
        reject_reason_by_signal_id = {
            str(r.get("signal_id", "")).strip(): str(r.get("reject_reason", "") or "").strip()
            for r in candidate_rows
            if str(r.get("signal_id", "")).strip() and str(r.get("reject_reason", "") or "").strip()
        }
        for r in signal_created_rows:
            sid = str(r.get("signal_id", "")).strip()
            if str(r.get("reject_reason", "") or "").strip() != "" or sid in rejected_signal_ids or _normalized_decision(r) in rejected_tokens:
                if not str(r.get("reject_reason", "") or "").strip() and sid in reject_reason_by_signal_id:
                    r = {**r, "reject_reason": reject_reason_by_signal_id[sid]}
                rejected_rows.append(r)
        accepted_rows = [
            r for r in signal_created_rows
            if str(r.get("reject_reason", "") or "").strip() == ""
            and str(r.get("signal_id", "")).strip() not in rejected_signal_ids
            and _normalized_decision(r) not in rejected_tokens
        ]
    else:
        rejected_rows = [
            r for r in candidate_rows
            if (_normalized_decision(r) in rejected_tokens or str(r.get("reject_reason", "") or "").strip() != "")
            and (not candidate_signal_ids or str(r.get("signal_id", "")).strip() in candidate_signal_ids)
        ]
        accepted_rows = [
            r for r in candidate_rows
            if _normalized_decision(r) in accepted_tokens
            and (not candidate_signal_ids or str(r.get("signal_id", "")).strip() in candidate_signal_ids)
        ]
    execution_ctx_missing_true = sum(1 for r in candidate_rows if bool(r.get("execution_ctx_missing")))
    effective_rr_diff_count = sum(
        1
        for r in candidate_rows
        if abs(_safe_float(r.get("effective_rr"), 0.0) - _safe_float(r.get("rr"), 0.0)) > 1e-12
    )

    unavailable_counts = {
        "volume_24h_usdt": 0,
        "spread_pct": 0,
        "funding_rate_pct": 0,
        "slippage_pct": 0,
        "latency_ms": 0,
    }
    for row in candidate_rows:
        ctx = row.get("execution_ctx")
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except Exception:
                ctx = {}
        if not isinstance(ctx, dict):
            ctx = {}
        if _value_unavailable(ctx.get("volume_24h_usdt")):
            unavailable_counts["volume_24h_usdt"] += 1
        if _value_unavailable(ctx.get("spread_pct")):
            unavailable_counts["spread_pct"] += 1
        if _value_unavailable(ctx.get("funding_rate_pct")):
            unavailable_counts["funding_rate_pct"] += 1
        if "expected_slippage_pct" in ctx and _value_unavailable(ctx.get("expected_slippage_pct")):
            unavailable_counts["slippage_pct"] += 1
        if "latency_ms" in ctx and _value_unavailable(ctx.get("latency_ms")):
            unavailable_counts["latency_ms"] += 1

    score_vals = [_safe_float(r.get("score"), 0.0) for r in candidate_rows]
    raw_rr_vals = [_safe_float(r.get("rr"), 0.0) for r in candidate_rows]
    effective_rr_vals = [_safe_float(r.get("effective_rr"), 0.0) for r in candidate_rows]
    near_threshold = [
        r for r in rejected_rows
        if str(r.get("reject_reason", "")).upper() == "LOW_SCORE" and abs(_safe_float(r.get("score"), 0.0) - 7.5) <= 0.5
    ]
    cfg = decision_filter_config("BACKTEST")
    threshold_values = {
        "min_score": float(cfg.get("MIN_TRADE_SCORE", 7.5)),
        "min_raw_rr": float(cfg.get("MIN_RR", 1.3)),
        "min_effective_rr": float(cfg.get("MIN_EFFECTIVE_RR", 1.6)),
        "reject_unknown_expectancy": bool(cfg.get("BLOCK_UNKNOWN_EXPECTANCY", False)),
        "require_execution_context": False,
    }
    rescue_rows = [r for r in candidate_rows if str(r.get("accepted_reason", "")).upper() == "HIGH_EFFECTIVE_RR_RESCUE"]
    rescue_closed = [r for r in rescue_rows if str(r.get("lifecycle_state", "")).upper() == "POSITION_CLOSED"]
    baseline_closed = [r for r in candidate_rows if str(r.get("lifecycle_state", "")).upper() == "POSITION_CLOSED" and str(r.get("accepted_reason", "")).upper() != "HIGH_EFFECTIVE_RR_RESCUE"]

    raw_gate_reject_reason_distribution = _distribution([
        _primary_reject_reason_from_context(
            current_reason=str(r.get("reject_reason", "") or ""),
            diagnostics=r,
            market_ctx=r,
            execution_ctx_missing=bool(r.get("execution_ctx_missing")),
        )
        for r in rejected_rows
    ])
    if canonical_rejected_rows is None:
        canonical_rejected_rows_for_counts = rejected_rows
        canonical_reject_reason_distribution = raw_gate_reject_reason_distribution
    else:
        canonical_rejected_rows_for_counts = list(canonical_rejected_rows)
        canonical_reject_reason_distribution = _distribution([
            str(r.get("reject_reason") or r.get("reason") or "UNKNOWN").strip() or "UNKNOWN"
            for r in canonical_rejected_rows_for_counts
        ])
    canonical_rejected_count = sum(canonical_reject_reason_distribution.values())
    total_for_summary = max(total, len(accepted_rows) + canonical_rejected_count) if canonical_rejected_rows is not None else total
    signal_rejected_count = len(rejected_rows)
    symbol_rejected_count = sum(
        1
        for r in canonical_rejected_rows_for_counts
        if str(r.get("lifecycle_state", "") or "").strip().upper() in {"SYMBOL_REJECTED", "SYMBOL_SELECTOR_REJECT"}
        or str(r.get("source", "") or "").strip().upper() == "SYMBOL_SELECTOR"
        or str(r.get("source_stage", "") or "").strip().upper() == "SYMBOL_SELECTOR"
        or str(r.get("event_flags", "") or "").strip().upper() == "SYMBOL_SELECTOR"
    )

    return {
        "total_candidates": total_for_summary,
        "accepted_count": len(accepted_rows),
        "baseline_accepted_trades": max(len(accepted_rows) - len(rescue_rows), 0),
        "rescue_candidate_count": len(rescue_rows),
        "rescue_accepted_count": len(rescue_rows),
        "rescue_rejected_count": 0,
        "rescue_accepted_would_tp_count": sum(1 for r in rescue_closed if str(r.get("close_reason", "")).upper() == "TP_HIT"),
        "rescue_accepted_would_sl_count": sum(1 for r in rescue_closed if str(r.get("close_reason", "")).upper() == "SL_HIT"),
        "rescue_accepted_net_pnl": sum(_safe_float(r.get("net_pnl_usdt"), 0.0) for r in rescue_closed),
        "baseline_net_pnl": sum(_safe_float(r.get("net_pnl_usdt"), 0.0) for r in baseline_closed),
        "baseline_plus_rescue_net_pnl": sum(_safe_float(r.get("net_pnl_usdt"), 0.0) for r in baseline_closed + rescue_closed),
        "rescue_avg_effective_rr": (sum(_safe_float(r.get("rescue_effective_rr"), 0.0) for r in rescue_rows) / len(rescue_rows)) if rescue_rows else 0.0,
        "rescue_avg_score": (sum(_safe_float(r.get("score"), 0.0) for r in rescue_rows) / len(rescue_rows)) if rescue_rows else 0.0,
        "rescue_reject_reasons": {},
        "accepted_reason_breakdown": _distribution([r.get("accepted_reason", "BASELINE") for r in accepted_rows]),
        "rejected_count": canonical_rejected_count,
        "signal_rejected_count": signal_rejected_count,
        "symbol_rejected_count": symbol_rejected_count,
        "canonical_rejected_count": canonical_rejected_count,
        "reject_rate": (canonical_rejected_count / total_for_summary) if total_for_summary else 0.0,
        "reject_reason_distribution": canonical_reject_reason_distribution,
        "canonical_reject_reason_distribution": canonical_reject_reason_distribution,
        "raw_gate_reject_reason_distribution": raw_gate_reject_reason_distribution,
        "thresholds_used": threshold_values,
        "score_distribution": _distribution([r.get("score") for r in candidate_rows]),
        "rr_distribution": _distribution([r.get("rr") for r in candidate_rows]),
        "effective_rr_distribution": _distribution([r.get("effective_rr") for r in candidate_rows]),
        "effective_rr_differs_from_rr_count": effective_rr_diff_count,
        "expectancy_bucket_distribution": _distribution([r.get("expectancy_bucket", "UNKNOWN") for r in candidate_rows]),
        "execution_ctx_missing_distribution": {
            "true": execution_ctx_missing_true,
            "false": total_for_summary - execution_ctx_missing_true,
        },
        "unavailable_execution_context_field_counts": unavailable_counts,
        "score_percentiles": _percentiles(score_vals, [10, 25, 50, 75, 90]),
        "raw_rr_percentiles": _percentiles(raw_rr_vals, [10, 25, 50, 75, 90]),
        "effective_rr_percentiles": _percentiles(effective_rr_vals, [10, 25, 50, 75, 90]),
        "rejection_reason_by_setup_type": _distribution([f"{r.get('setup_type','UNKNOWN') or 'UNKNOWN'}::{_primary_reject_reason_from_context(current_reason=str(r.get('reject_reason', '') or ''), diagnostics=r, market_ctx=r, execution_ctx_missing=bool(r.get('execution_ctx_missing')))}" for r in rejected_rows]),
        "rejection_reason_by_regime": _distribution([f"{r.get('regime','UNKNOWN') or 'UNKNOWN'}::{_primary_reject_reason_from_context(current_reason=str(r.get('reject_reason', '') or ''), diagnostics=r, market_ctx=r, execution_ctx_missing=bool(r.get('execution_ctx_missing')))}" for r in rejected_rows]),
        "acceptance_candidates_near_threshold_count": len(near_threshold),
        "accepted_trade_quality_diagnostics": _accepted_quality_diagnostics(accepted_rows, candidate_rows),
        "score_calibration_diagnostics": _score_calibration_diagnostics(candidate_rows),
        "disabled_filter_acceptance_evidence": {"disabled_filters": _distribution([r.get("disabled_filters", "") for r in candidate_rows if str(r.get("disabled_filters", "")) not in {"", "[]"}]), "accepted_because_filter_disabled_count": sum(int(_safe_float(r.get("disabled_filter_bypass_count"), 0.0)) for r in accepted_rows), "estimated_pnl_impact_usdt": sum(_safe_float(r.get("net_pnl_usdt", r.get("net_pnl", 0.0)), 0.0) for r in accepted_rows if int(_safe_float(r.get("disabled_filter_bypass_count"), 0.0)) > 0)},
    }



def _accepted_quality_diagnostics(accepted_rows: List[Mapping[str, Any]], candidate_rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    closed = [r for r in candidate_rows if str(r.get("lifecycle_state", r.get("status_after", ""))).upper() == "POSITION_CLOSED"]
    accepted_ids = {str(r.get("signal_id", "")).strip() for r in accepted_rows if str(r.get("signal_id", "")).strip()}
    if accepted_ids:
        closed = [r for r in closed if str(r.get("signal_id", "")).strip() in accepted_ids]
    result_rows = closed or accepted_rows

    def outcome(row: Mapping[str, Any]) -> str:
        return str(row.get("close_reason") or row.get("result") or row.get("lifecycle_state") or row.get("status_after") or "UNKNOWN").upper()

    def bucket_eff(row: Mapping[str, Any]) -> str:
        eff = _safe_float(row.get("effective_rr"), -1.0)
        if eff < 0:
            return "UNAVAILABLE"
        if eff < 1.6:
            return "<1.6"
        if eff < 1.9:
            return "1.6-1.9"
        if eff < 2.3:
            return "1.9-2.3"
        return ">=2.3"

    def score_bucket(row: Mapping[str, Any]) -> str:
        score = _safe_float(row.get("score"), -1.0)
        if score < 0:
            return "UNAVAILABLE"
        return "10" if score >= 10.0 else f"{int(score)}-{int(score)+1}"

    def hour_bucket(row: Mapping[str, Any]) -> str:
        raw = row.get("timestamp") or row.get("event_ts") or row.get("created_at")
        try:
            value = int(float(raw))
            if value > 10_000_000_000:
                value //= 1000
            return f"{datetime.fromtimestamp(value, tz=timezone.utc).hour:02d}:00Z"
        except Exception:
            return "UNAVAILABLE"

    def group(field: str, rows: List[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            key = str(r.get(field) or "UNKNOWN")
            b = out.setdefault(key, {"count": 0, "tp": 0, "sl": 0, "open": 0, "net_pnl": 0.0})
            b["count"] += 1
            o = outcome(r)
            b["tp"] += int(o == "TP_HIT")
            b["sl"] += int(o == "SL_HIT")
            b["open"] += int(o in {"OPEN_AT_END", "OPEN", "CANCELLED", "UNKNOWN"})
            b["net_pnl"] += _safe_float(r.get("net_pnl_usdt", r.get("net_pnl", 0.0)), 0.0)
        for b in out.values():
            n = max(1, int(b["count"]))
            b["tp_rate"] = b["tp"] / n
            b["sl_rate"] = b["sl"] / n
            b["expectancy"] = b["net_pnl"] / n
        return out

    enriched = []
    for r in result_rows:
        enriched.append({**dict(r), "score_bucket": score_bucket(r), "effective_rr_bucket": bucket_eff(r), "hour_session": hour_bucket(r)})
    total = len(enriched)
    tp = sum(1 for r in enriched if outcome(r) == "TP_HIT")
    sl = sum(1 for r in enriched if outcome(r) == "SL_HIT")
    return {
        "accepted_tp_rate": tp / total if total else 0.0,
        "accepted_sl_rate": sl / total if total else 0.0,
        "by_score_bucket": group("score_bucket", enriched),
        "by_regime": group("regime", enriched),
        "by_effective_rr_bucket": group("effective_rr_bucket", enriched),
        "by_side": group("side", enriched),
        "by_symbol": group("symbol", enriched),
        "by_hour_session": group("hour_session", enriched),
        "expectancy_bucket_distribution": _distribution([r.get("expectancy_bucket", "UNKNOWN") for r in enriched]),
        "negative_expectancy_accepted_count": sum(1 for r in accepted_rows if str(r.get("expectancy_bucket", "")).upper() == "NEGATIVE"),
    }

def _score_calibration_diagnostics(candidate_rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    rows = [r for r in candidate_rows if str(r.get("score", "")) not in {"", "None", "null"}]
    def bucket(r: Mapping[str, Any]) -> str:
        score = _safe_float(r.get("score"), -1.0)
        if score < 0: return "UNAVAILABLE"
        return "10" if score >= 10.0 else f"{int(score)}-{int(score)+1}"
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        b = out.setdefault(bucket(r), {"count": 0, "tp": 0, "sl": 0, "timeout": 0, "net_pnl": 0.0, "effective_rr_sum": 0.0, "effective_rr_count": 0, "expectancy_buckets": {}})
        b["count"] += 1
        outcome = str(r.get("close_reason") or r.get("shadow_outcome") or r.get("lifecycle_state") or r.get("status_after") or "UNKNOWN").upper()
        b["tp"] += int(outcome in {"TP_HIT", "WOULD_TP"})
        b["sl"] += int(outcome in {"SL_HIT", "WOULD_SL"})
        b["timeout"] += int(outcome in {"WOULD_TIMEOUT", "OPEN_AT_END"})
        b["net_pnl"] += _safe_float(r.get("net_pnl_usdt", r.get("net_pnl", 0.0)), 0.0)
        eff = _safe_float(r.get("effective_rr"), -1.0)
        if eff >= 0:
            b["effective_rr_sum"] += eff; b["effective_rr_count"] += 1
        eb = str(r.get("expectancy_bucket") or "UNKNOWN")
        b["expectancy_buckets"][eb] = b["expectancy_buckets"].get(eb, 0) + 1
    for b in out.values():
        n = max(1, int(b["count"]))
        b["tp_rate"] = b["tp"] / n; b["sl_rate"] = b["sl"] / n; b["net_pnl_per_signal"] = b["net_pnl"] / n
        b["mean_effective_rr"] = b["effective_rr_sum"] / b["effective_rr_count"] if b["effective_rr_count"] else None
    return {"score_10_saturation_count": sum(1 for r in rows if _safe_float(r.get("score"), -1.0) >= 10.0), "by_score_bucket": out}


def build_accepted_loss_diagnostics(rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    accepted = [r for r in rows if str(r.get("decision", "")).upper() == "ACCEPTED" and str(r.get("lifecycle_state", "")).upper() in {"POSITION_CLOSED", "OPEN_AT_END", "TP_HIT", "SL_HIT"}]
    if not accepted:
        accepted = [r for r in rows if str(r.get("reject_reason", "") or "") == "" and str(r.get("lifecycle_state", "")).upper() in {"POSITION_CLOSED", "OPEN_AT_END"}]

    def outcome(row: Mapping[str, Any]) -> str:
        return str(row.get("close_reason") or row.get("lifecycle_state") or "UNKNOWN").upper()

    def score_bucket(row: Mapping[str, Any]) -> str:
        score = _safe_float(row.get("score"), -1.0)
        if score < 0: return "UNAVAILABLE"
        return "10" if score >= 10.0 else f"{int(score)}-{int(score)+1}"

    def eff_bucket(row: Mapping[str, Any]) -> str:
        eff = _safe_float(row.get("effective_rr"), -1.0)
        if eff < 0: return "UNAVAILABLE"
        if eff < 1.6: return "<1.6"
        if eff < 1.9: return "1.6-1.9"
        if eff < 2.3: return "1.9-2.3"
        return ">=2.3"

    enriched = []
    for r in accepted:
        enriched.append({**dict(r), "score_bucket": score_bucket(r), "effective_rr_bucket": eff_bucket(r)})

    flat_rows: List[Dict[str, Any]] = []
    def add_group(grouping: str, key_func) -> Dict[str, Dict[str, Any]]:
        grouped: Dict[str, Dict[str, Any]] = {}
        for r in enriched:
            key = str(key_func(r) or "UNKNOWN")
            b = grouped.setdefault(key, {"count": 0, "wins": 0, "losses": 0, "open": 0, "net_pnl": 0.0, "tp_gain_sum": 0.0, "tp_count": 0, "sl_loss_sum": 0.0, "sl_count": 0})
            pnl = _safe_float(r.get("net_pnl_usdt", r.get("net_pnl", 0.0)), 0.0)
            o = outcome(r)
            b["count"] += 1; b["net_pnl"] += pnl
            b["wins"] += int(o == "TP_HIT"); b["losses"] += int(o == "SL_HIT"); b["open"] += int(o not in {"TP_HIT", "SL_HIT"})
            if o == "TP_HIT": b["tp_gain_sum"] += pnl; b["tp_count"] += 1
            if o == "SL_HIT": b["sl_loss_sum"] += pnl; b["sl_count"] += 1
        for key, b in grouped.items():
            n = max(1, int(b["count"]))
            b["win_rate"] = b["wins"] / n; b["loss_rate"] = b["losses"] / n
            b["avg_tp_gain"] = b["tp_gain_sum"] / b["tp_count"] if b["tp_count"] else 0.0
            b["avg_sl_loss"] = b["sl_loss_sum"] / b["sl_count"] if b["sl_count"] else 0.0
            flat_rows.append({"grouping": grouping, "bucket": key, **b})
        return grouped

    by = {
        "score_bucket": add_group("score_bucket", lambda r: r.get("score_bucket")),
        "regime": add_group("regime", lambda r: r.get("regime")),
        "side": add_group("side", lambda r: r.get("side")),
        "symbol": add_group("symbol", lambda r: r.get("symbol")),
        "effective_rr_bucket": add_group("effective_rr_bucket", lambda r: r.get("effective_rr_bucket")),
    }
    high_rr = [r for r in enriched if _safe_float(r.get("effective_rr"), 0.0) >= 2.3]
    score10 = [r for r in enriched if _safe_float(r.get("score"), -1.0) >= 10.0]
    return {
        "accepted_count": len(enriched),
        "by": by,
        "rows": flat_rows,
        "high_effective_rr_accepted_outcome_split": _distribution([outcome(r) for r in high_rr]),
        "score_10_accepted_net_pnl": sum(_safe_float(r.get("net_pnl_usdt", r.get("net_pnl", 0.0)), 0.0) for r in score10),
    }

def build_filter_profile_comparison_artifact(summary: Mapping[str, Any], quality_summary: Mapping[str, Any], filter_state: Mapping[str, Any]) -> Dict[str, Any]:
    current = {
        "profile": filter_state.get("filter_profile", "CUSTOM"),
        "candidates": _safe_float(summary.get("total_candidates"), 0.0),
        "accepted_trades": _safe_float(summary.get("accepted_count"), 0.0),
        "rejected_signals": _safe_float(summary.get("rejected_count", summary.get("total_rejected")), 0.0),
        "reject_rate": _safe_float(summary.get("rejection_rate"), 0.0),
        "win": _safe_float(summary.get("tp_hits"), 0.0),
        "loss": _safe_float(summary.get("sl_hits"), 0.0),
        "open": _safe_float(summary.get("open_at_end"), 0.0),
        "net_pnl": _safe_float(summary.get("total_net_pnl_usdt"), 0.0),
        "return": _safe_float(summary.get("total_pnl_pct"), 0.0),
        "accepted_effective_rr_distribution": quality_summary.get("accepted_trade_quality_diagnostics", {}).get("by_effective_rr_bucket", {}),
        "score_calibration": quality_summary.get("score_calibration_diagnostics", {}),
        "top_reject_reasons": quality_summary.get("reject_reason_distribution", {}),
        "expectancy_bucket_split": quality_summary.get("expectancy_bucket_distribution", {}),
    }
    empty = {"status": "NOT_RUN_IN_THIS_ARTIFACT", "reason": "Run DEFAULT, ALL_OFF, and CUSTOM profiles separately; this artifact is BACKTEST-only and does not mutate PAPER/LIVE."}
    profiles = {"DEFAULT": dict(empty), "ALL_OFF": dict(empty), "CUSTOM": dict(empty)}
    profiles[str(current["profile"])] = current
    return {"mode": "BACKTEST", "artifact_only": True, "profiles": profiles}


def _accepted_terminal_rows_for_risk(lifecycle_rows: List[LifecycleRow]) -> List[LifecycleRow]:
    rows = [
        r for r in lifecycle_rows
        if not r.reject_reason and r.status_after in {"POSITION_CLOSED", "OPEN_AT_END", "TP_HIT", "SL_HIT"}
    ]
    return sorted(rows, key=lambda r: (r.timestamp, r.symbol, r.side))


def build_equity_curve_metrics(lifecycle_rows: List[LifecycleRow], initial_balance: float) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    equity = float(initial_balance or 0.0)
    peak = equity
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    longest_loss = longest_win = cur_loss = cur_win = 0
    gross_profit = gross_loss = 0.0
    curve: List[Dict[str, Any]] = []
    for idx, row in enumerate(_accepted_terminal_rows_for_risk(lifecycle_rows), start=1):
        pnl = float(row.net_pnl_usdt or 0.0)
        equity += pnl
        peak = max(peak, equity)
        drawdown = equity - peak
        drawdown_pct = (drawdown / peak * 100.0) if peak else 0.0
        max_drawdown = min(max_drawdown, drawdown)
        max_drawdown_pct = min(max_drawdown_pct, drawdown_pct)
        if pnl > 0:
            gross_profit += pnl; cur_win += 1; cur_loss = 0
        elif pnl < 0:
            gross_loss += abs(pnl); cur_loss += 1; cur_win = 0
        else:
            cur_loss = cur_win = 0
        longest_loss = max(longest_loss, cur_loss)
        longest_win = max(longest_win, cur_win)
        curve.append({
            "trade_index": idx, "timestamp": row.timestamp, "symbol": row.symbol, "side": row.side,
            "close_reason": row.close_reason or row.status_after, "net_pnl_usdt": pnl,
            "equity": equity, "peak_equity": peak, "drawdown": drawdown, "drawdown_pct": drawdown_pct,
        })
    metrics = {
        "max_drawdown": max_drawdown,
        "max_drawdown_pct": max_drawdown_pct,
        "longest_loss_streak": longest_loss,
        "longest_win_streak": longest_win,
        "profit_factor": (gross_profit / gross_loss if gross_loss else (float("inf") if gross_profit else 0.0)),
    }
    return curve, metrics




def build_strategy_quality_evidence(lifecycle_rows: List[LifecycleRow], rejected_rows: List[Mapping[str, Any]], summary: Mapping[str, Any], cfg: StrategyQualityGuardrailConfig) -> Dict[str, Any]:
    guard_reasons = {"DAILY_TRADE_FREQUENCY_GUARD", "LOSS_STREAK_PAUSE", "SYMBOL_CLUSTER_GUARD", "SCORE_SATURATION_GUARD", "HIGH_VOL_GUARD", "HIGH_VOL_OVERTRADE", "HIGH_VOL_EXECUTION_COST"}
    accepted_after = _accepted_terminal_rows_for_risk(lifecycle_rows)
    guard_rejects = [r for r in rejected_rows if str(r.get("reject_reason", "")).upper() in guard_reasons]
    guardrail_reject_breakdown = dict(Counter(str(r.get("reject_reason", "UNKNOWN")).upper() or "UNKNOWN" for r in guard_rejects))
    representative_examples = []
    for r in guard_rejects[:25]:
        representative_examples.append({
            "symbol": r.get("symbol"), "side": r.get("side"), "timestamp": r.get("timestamp") or r.get("event_ts"),
            "reject_reason": str(r.get("reject_reason", "UNKNOWN")).upper() or "UNKNOWN",
            "score": r.get("score"), "effective_rr": r.get("effective_rr"), "regime": r.get("regime"),
            "cost_penalty": r.get("cost_penalty"), "shadow_outcome": r.get("shadow_outcome"),
        })
    before_count = len(accepted_after) + len(guard_rejects)
    score10_after = [r for r in accepted_after if float(r.score or 0.0) >= cfg.saturated_score_threshold]
    high_vol_after = [r for r in accepted_after if _is_high_vol_context(r.regime, {"volatility_regime": r.volatility_regime})]
    reasons = []
    net = _safe_float(summary.get("total_net_pnl_usdt"), 0.0)
    pf = _safe_float(summary.get("profit_factor"), 0.0)
    dd = abs(_safe_float(summary.get("max_drawdown_pct"), 0.0))
    loss = int(_safe_float(summary.get("longest_loss_streak"), 0.0))
    avg_day = before_count / max(1, int(_safe_float(summary.get("requested_last_n_days"), 1.0)))
    score10_tp = sum(1 for r in score10_after if r.close_reason == "TP_HIT")
    score10_sl = sum(1 for r in score10_after if r.close_reason == "SL_HIT")
    if net <= 0: reasons.append("NET_PNL_NOT_POSITIVE")
    if pf < cfg.min_profit_factor_for_profile_pass: reasons.append("PROFIT_FACTOR_BELOW_MIN")
    if dd > cfg.max_drawdown_pct_for_profile_pass: reasons.append("MAX_DRAWDOWN_TOO_HIGH")
    if loss > cfg.max_loss_streak_for_profile_pass: reasons.append("LOSS_STREAK_TOO_HIGH")
    if avg_day > cfg.max_accepted_trades_per_day: reasons.append("OVERTRADE_RISK")
    if score10_sl > score10_tp: reasons.append("SCORE_SATURATION_RISK")
    if cfg.profile in {"HIGH_VOL_MOMENTUM_DIAGNOSTIC", "HIGH_VOL_GUARD_OFF_DIAGNOSTIC", "VOL_GUARD_RELAXED_DIAGNOSTIC"}:
        reasons.append("DIAGNOSTIC_ONLY_PROFILE")
    status = "PASS" if not reasons else "FAIL"
    return {
        "profile_quality_status": status, "profile_quality_reasons": reasons,
        "thresholds_used": asdict(cfg),
        "accepted_before_guardrails": before_count, "accepted_after_guardrails": len(accepted_after),
        "rejected_by_new_guardrails": len(guard_rejects),
        "guardrail_reject_breakdown": guardrail_reject_breakdown,
        "top_guardrail_reject_reasons": [{"reason": reason, "count": count} for reason, count in Counter(guardrail_reject_breakdown).most_common()],
        "representative_guardrail_reject_examples": representative_examples,
        "pnl_before_guardrails": None, "pnl_after_guardrails": net,
        "trade_count_before_after": {"before": before_count, "after": len(accepted_after)},
        "loss_streak_before_after": {"before": None, "after": loss},
        "profit_factor_before_after": {"before": None, "after": pf},
        "max_drawdown_before_after": {"before": None, "after": _safe_float(summary.get("max_drawdown_pct"), 0.0)},
        "score10_tp_sl_before_after": {"before": None, "after": {"tp": score10_tp, "sl": score10_sl}},
        "high_vol_trade_count_before_after": {"before": None, "after": len(high_vol_after)},
        "diagnostic_profile_warning": HIGH_VOL_GUARD_DIAGNOSTIC_WARNING if cfg.profile == "HIGH_VOL_GUARD_OFF_DIAGNOSTIC" else "",
    }

def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []

def _high_vol_metric(row: Mapping[str, Any], cfg: StrategyQualityGuardrailConfig) -> tuple[str, float, float]:
    if _safe_float(row.get("cost_penalty"), 0.0) > cfg.high_vol_max_cost_penalty:
        return ("cost_penalty_total", _safe_float(row.get("cost_penalty"), 0.0), cfg.high_vol_max_cost_penalty)
    return ("effective_rr", _safe_float(row.get("effective_rr"), 0.0), cfg.high_vol_min_effective_rr)

def _metric_or_unavailable(row: Mapping[str, Any], diag: Mapping[str, Any], name: str) -> Any:
    value = row.get(name, diag.get(name, ""))
    return "UNAVAILABLE_BACKTEST" if value in (None, "") else value

def build_high_vol_guard_diagnostics(rejected_rows: List[Mapping[str, Any]], cfg: StrategyQualityGuardrailConfig) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for row in rejected_rows:
        if str(row.get("reject_reason", "")).upper() != "HIGH_VOL_GUARD":
            continue
        diag = {}
        if str(row.get("diagnostics", "")).strip().startswith("{"):
            try:
                diag = json.loads(str(row.get("diagnostics", "{}")))
            except Exception:
                diag = {}
        metric_name, metric_value, threshold = _high_vol_metric(row, cfg)
        effective_rr = _safe_float(row.get("effective_rr"), 0.0)
        effective_rr_gap = round(max(0.0, float(cfg.high_vol_min_effective_rr) - effective_rr), 10)
        cost_penalty_total = _safe_float(row.get("cost_penalty"), _safe_float(diag.get("cost_penalty"), 0.0))
        cost_penalty_gap = round(max(0.0, cost_penalty_total - float(cfg.high_vol_max_cost_penalty)), 10)
        effective_rr_below = effective_rr_gap > 0.0
        stop_too_wide_softened = "STOP_TOO_WIDE" in {str(x).upper() for x in _json_list(row.get("all_failed_gates")) + _json_list(diag.get("all_failed_gates"))}
        if effective_rr_below and stop_too_wide_softened:
            trigger = "BOTH"
        elif effective_rr_below:
            trigger = "EFFECTIVE_RR_BELOW_THRESHOLD"
        elif stop_too_wide_softened:
            trigger = "STOP_TOO_WIDE_SOFTENED"
        else:
            trigger = "UNKNOWN"
        guard_gap = effective_rr_gap if metric_name == "effective_rr" else cost_penalty_gap
        guard_breach_direction = "BELOW_MINIMUM" if metric_name == "effective_rr" and guard_gap > 0 else ("ABOVE_MAXIMUM" if metric_name == "cost_penalty_total" and guard_gap > 0 else "NONE")
        failed = sorted(set([str(x) for x in _json_list(row.get("all_failed_gates")) + _json_list(diag.get("all_failed_gates")) if str(x)]))
        dcfg = decision_filter_config("BACKTEST")
        pass_map = {
            "score": _safe_float(row.get("score"), 0.0) >= float(dcfg.get("MIN_TRADE_SCORE", 7.5)),
            "raw_rr": _safe_float(row.get("raw_rr", row.get("rr")), 0.0) >= float(dcfg.get("MIN_RR", 1.3)),
            "effective_rr": effective_rr >= float(dcfg.get("MIN_EFFECTIVE_RR", 1.6)),
            "high_vol_effective_rr": effective_rr >= float(cfg.high_vol_min_effective_rr),
            "expectancy": str(row.get("expectancy_bucket", "")).upper() not in {"NEGATIVE", "MISSING"},
            "spread": "HIGH_SPREAD" not in failed,
            "slippage": "HIGH_SLIPPAGE" not in failed,
            "liquidity": "THIN_LIQUIDITY" not in failed,
            "stop_geometry": "INVALID_STOP_GEOMETRY" not in failed,
            "stop_width": "STOP_TOO_WIDE" not in failed,
            "cost_penalty": cost_penalty_total <= cfg.high_vol_max_cost_penalty,
        }
        passed = [name for name, ok in pass_map.items() if ok]
        all_non_vol_passed = not [x for x in failed if x != "HIGH_VOL_GUARD"]
        entry = _safe_float(row.get("entry"), 0.0)
        sl = _safe_float(row.get("sl"), 0.0)
        counterfactual_scope = "WOULD_PASS_BASELINE_NOT_HIGH_VOL_SAFE" if all_non_vol_passed and effective_rr_below else ("BASELINE_NON_HIGH_VOL_FILTERS_PASS" if all_non_vol_passed else "BLOCKED_BY_SECONDARY_FILTERS")
        rows.append({
            "timestamp": row.get("timestamp"), "symbol": row.get("symbol"), "side": row.get("side"),
            "score": row.get("score"), "rr": row.get("rr"), "raw_rr": row.get("raw_rr", row.get("rr")),
            "effective_rr": row.get("effective_rr"), "expectancy_bucket": row.get("expectancy_bucket"),
            "candidate_stage": row.get("source_stage", "STRATEGY_QUALITY_GUARDRAIL"),
            "reject_reason": row.get("reject_reason"),
            "guard_metric_name": metric_name,
            "guard_metric_value": metric_value,
            "guard_threshold": threshold,
            "guard_gap_to_threshold": guard_gap,
            "guard_breach_direction": guard_breach_direction,
            "volatility_metric_name": metric_name,
            "volatility_metric_value": metric_value,
            "volatility_threshold": threshold,
            "volatility_ratio_to_threshold": (metric_value / threshold if threshold else ""),
            "effective_rr_gap_to_threshold": effective_rr_gap,
            "counterfactual_effective_rr_gap": effective_rr_gap,
            "high_vol_context_detected": True,
            "high_vol_guard_trigger": trigger,
            "stop_too_wide_softened": stop_too_wide_softened,
            "effective_rr_below_high_vol_threshold": effective_rr_below,
            "cost_penalty_total": cost_penalty_total,
            "high_vol_cost_penalty_threshold": cfg.high_vol_max_cost_penalty,
            "high_vol_context_source": row.get("high_vol_context_source") or diag.get("high_vol_context_source") or "volatility_regime_or_guardrail_context",
            "volatility_regime": row.get("volatility_regime", diag.get("volatility_regime", "")),
            "atr_pct": _metric_or_unavailable(row, diag, "atr_pct"),
            "realized_volatility_pct": _metric_or_unavailable(row, diag, "realized_volatility_pct"),
            "candle_range_pct": _metric_or_unavailable(row, diag, "candle_range_pct"),
            "volume_24h_usdt": row.get("volume_24h_usdt"),
            "spread_pct": row.get("spread_pct"),
            "expected_slippage_pct": row.get("expected_slippage_pct"),
            "funding_rate_pct": row.get("funding_rate_pct"),
            "liquidity_score": row.get("liquidity_score"),
            "regime": row.get("regime"), "setup": row.get("setup_type") or row.get("setup"),
            "passed_all_non_volatility_filters": all_non_vol_passed,
            "passed_score": pass_map["score"],
            "passed_raw_rr": pass_map["raw_rr"],
            "passed_min_effective_rr": pass_map["effective_rr"],
            "passed_high_vol_effective_rr": pass_map["high_vol_effective_rr"],
            "passed_expectancy": pass_map["expectancy"],
            "passed_spread": pass_map["spread"],
            "passed_slippage": pass_map["slippage"],
            "passed_liquidity": pass_map["liquidity"],
            "passed_stop_geometry": pass_map["stop_geometry"],
            "passed_stop_width": pass_map["stop_width"],
            "passed_all_non_high_vol_filters": all_non_vol_passed,
            "filters_passed": json.dumps(passed, sort_keys=True),
            "filters_failed": json.dumps(failed or ["HIGH_VOL_GUARD"], sort_keys=True),
            "would_accept_if_high_vol_guard_disabled": all_non_vol_passed,
            "counterfactual_reject_reason_if_high_vol_guard_ignored": "" if all_non_vol_passed else ",".join(x for x in failed if x != "HIGH_VOL_GUARD"),
            "counterfactual_acceptance_scope": counterfactual_scope,
            "counterfactual_warning": "HIGH_VOL_GUARD counterfactual is diagnostic only. It does not imply production acceptance.",
            "counterfactual_effective_rr": row.get("effective_rr"),
            "counterfactual_expected_slippage_penalty": row.get("expected_slippage_pct"),
            "counterfactual_volatility_penalty": guard_gap if threshold else "",
            "counterfactual_trade_quality_score": row.get("quality_score") or row.get("score"),
            "estimated_stop_distance": (abs(entry - sl) / entry if entry else ""),
            "estimated_liquidation_slippage_danger": "UNAVAILABLE_BACKTEST",
            "source_function": "backtest_order._guardrail_rejection_reason",
            "guard_name": "HIGH_VOL_GUARD",
        })
    return rows

def build_acceptance_funnel(rejected_rows: List[Mapping[str, Any]], accepted_count: int, summary: Mapping[str, Any], cfg: StrategyQualityGuardrailConfig) -> List[Dict[str, Any]]:
    counts = Counter(str(r.get("reject_reason") or "UNKNOWN").upper() for r in rejected_rows)
    total = int(_safe_float(summary.get("total_candidates"), accepted_count + len(rejected_rows)))
    symbol_rej = int(_safe_float(summary.get("symbol_rejected_count"), sum(1 for r in rejected_rows if str(r.get("lifecycle_state", "")).upper() == "SYMBOL_REJECTED")))
    signal_rej = len(rejected_rows) - symbol_rej
    hv_without = sum(1 for r in build_high_vol_guard_diagnostics(rejected_rows, cfg) if r["would_accept_if_high_vol_guard_disabled"])
    stages = [
        ("symbol_universe", len({r.get("symbol") for r in rejected_rows if r.get("symbol")}) or 0, "unique symbols in canonical rejected/accepted artifacts"),
        ("total_candidates", total, "canonical accepted + rejected candidates"),
        ("symbol_level_rejected", symbol_rej, "pre-signal symbol-selector rejects"),
        ("signal_created", max(total - symbol_rej, 0), "candidates reaching signal engine"),
        ("signal_level_rejected", signal_rej, "signal/guardrail rejects"),
    ]
    for reason, count in sorted(counts.items()):
        stages.append((f"{reason.lower()}_rejected", count, "canonical reject_reason count"))
    before = accepted_count + sum(counts[r] for r in ("HIGH_VOL_GUARD", "HIGH_VOL_OVERTRADE", "HIGH_VOL_EXECUTION_COST"))
    stages.extend([
        ("would_accept_without_high_vol_guard", hv_without, "shadow diagnostic only; default result unchanged"),
        ("accepted_before_guardrails", before, "accepted terminal rows plus strategy guardrail rejects"),
        ("accepted_after_guardrails", accepted_count, "default conservative BACKTEST acceptance"),
        ("position_opened", accepted_count, "accepted terminal row proxy"),
        ("position_closed", accepted_count, "accepted terminal row proxy"),
    ])
    prev = None
    out = []
    for stage, count, notes in stages:
        out.append({"stage": stage, "count": count, "delta_from_previous": "" if prev is None else count - prev, "notes": notes})
        prev = count
    return out

def classify_high_vol_guard(rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {"high_vol_guard_verdict": "VALID_PROTECTIVE_GUARD", "high_vol_guard_evidence": "No HIGH_VOL_GUARD rows emitted.", "recommended_action": "Keep HIGH_VOL_GUARD enabled. Do not relax threshold based on current evidence. HIGH_VOL_GUARD is not the primary zero-accepted bottleneck. Continue audit on LOW_SCORE and symbol-level market-structure rejects."}
    would = sum(1 for r in rows if r.get("would_accept_if_high_vol_guard_disabled") in {True, "True", "true", "1"})
    gaps = [_safe_float(r.get("effective_rr_gap_to_threshold"), 0.0) for r in rows]
    eff = [_safe_float(r.get("effective_rr"), 0.0) for r in rows]
    threshold = _safe_float(rows[0].get("guard_threshold"), 0.0)
    marginal = sum(1 for r in rows if abs(_safe_float(r.get("effective_rr"), 0.0) - threshold) <= threshold * 0.05) if threshold else 0
    triggers = _distribution([r.get("high_vol_guard_trigger", "UNKNOWN") for r in rows])
    secondary = sum(1 for r in rows if not (r.get("passed_all_non_high_vol_filters") in {True, "True", "true", "1"}))
    verdict = "OVERSTRICT_THRESHOLD" if would and marginal >= max(1, len(rows) // 2) else "VALID_PROTECTIVE_GUARD"
    return {
        "high_vol_guard_count": len(rows),
        "high_vol_guard_would_accept_without_guard": would,
        "high_vol_guard_trigger_distribution": triggers,
        "effective_rr_min": min(eff) if eff else 0.0,
        "effective_rr_max": max(eff) if eff else 0.0,
        "effective_rr_mean": sum(eff) / len(eff) if eff else 0.0,
        "effective_rr_threshold": threshold,
        "effective_rr_gap_min": min(gaps) if gaps else 0.0,
        "effective_rr_gap_max": max(gaps) if gaps else 0.0,
        "effective_rr_gap_mean": sum(gaps) / len(gaps) if gaps else 0.0,
        "high_vol_guard_near_threshold_count": marginal,
        "high_vol_guard_secondary_failure_count": secondary,
        "high_vol_guard_verdict": verdict,
        "high_vol_guard_evidence": f"{len(rows)} HIGH_VOL_GUARD rows; {would} would pass baseline non-high-vol filters if disabled; {marginal} are within +/-5% of effective RR threshold; {secondary} have secondary failures.",
        "recommended_action": "Audit counterfactual drawdown before any production threshold change." if verdict != "VALID_PROTECTIVE_GUARD" else "Keep HIGH_VOL_GUARD enabled. Do not relax threshold based on current evidence. HIGH_VOL_GUARD is not the primary zero-accepted bottleneck. Continue audit on LOW_SCORE and symbol-level market-structure rejects.",
    }

def _json_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _first_numeric_with_source(candidates: list[tuple[str, Any]]) -> tuple[float | None, str]:
    for source, value in candidates:
        if value in ("", None):
            continue
        try:
            return float(value), source
        except (TypeError, ValueError):
            continue
    return None, "UNAVAILABLE"


def _detect_score_scale(value: Any) -> str:
    val = _safe_float(value, -1.0)
    if val < 0.0:
        return "UNKNOWN"
    if val <= 1.0:
        return "0_1"
    if val <= 10.0:
        return "0_10"
    return "GT_10"


def _low_score_threshold_for_row(row: Mapping[str, Any], diagnostics: Mapping[str, Any]) -> Dict[str, Any]:
    adaptive = diagnostics.get("adaptive_thresholds") if isinstance(diagnostics.get("adaptive_thresholds"), dict) else {}
    cfg_threshold = decision_filter_config("BACKTEST").get("MIN_TRADE_SCORE")
    threshold, source = _first_numeric_with_source([
        ("row.min_required_score", row.get("min_required_score")),
        ("diagnostics.min_required_score", diagnostics.get("min_required_score")),
        ("diagnostics.min_score", diagnostics.get("min_score")),
        ("diagnostics.adaptive_thresholds.min_score", adaptive.get("min_score")),
    ])
    fallback_threshold, _ = _first_numeric_with_source([("decision_filter_config.BACKTEST.MIN_TRADE_SCORE", cfg_threshold)])
    score = _safe_float(row.get("score"), _safe_float(diagnostics.get("score"), 0.0))
    score_scale = _detect_score_scale(score)
    if threshold is None:
        threshold = fallback_threshold if fallback_threshold is not None else 7.5
        source = "decision_filter_config.BACKTEST.MIN_TRADE_SCORE"
    threshold_scale = _detect_score_scale(threshold)
    mismatch = score_scale == "0_10" and threshold_scale == "0_1"
    correction = False
    if source == "decision_filter_config.BACKTEST.MIN_TRADE_SCORE" and mismatch:
        threshold *= 10.0
        threshold_scale = "0_10"
        correction = True
    return {
        "threshold": threshold,
        "source": source,
        "score_scale_detected": score_scale,
        "score_threshold_scale_detected": threshold_scale,
        "threshold_scale_mismatch_detected": mismatch,
        "threshold_scale_correction_applied": correction,
    }


def build_low_score_diagnostics(rejected_rows: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    cfg = decision_filter_config("BACKTEST"); min_rr = float(cfg.get("MIN_RR", 1.3)); min_eff = float(cfg.get("MIN_EFFECTIVE_RR", 1.6))
    out: List[Dict[str, Any]] = []
    for row in rejected_rows:
        if str(row.get("reject_reason", "")).upper() != "LOW_SCORE": continue
        diagnostics = _json_dict(row.get("diagnostics"))
        threshold_info = _low_score_threshold_for_row(row, diagnostics)
        min_score = float(threshold_info["threshold"])
        score = _safe_float(row.get("score"), _safe_float(diagnostics.get("score"), 0.0)); eff = _safe_float(row.get("effective_rr"), 0.0); raw = _safe_float(row.get("raw_rr", row.get("rr")), 0.0)
        failed = sorted(set([str(x) for x in _json_list(row.get("all_failed_gates")) if str(x)]))
        passed_rr = raw >= min_rr; passed_eff = eff >= min_eff
        passed_exp = str(row.get("expectancy_bucket", "")).upper() not in {"NEGATIVE", "MISSING"}
        passed_costs = "HIGH_SPREAD" not in failed and "HIGH_SLIPPAGE" not in failed and "THIN_LIQUIDITY" not in failed
        would = passed_rr and passed_eff and passed_exp and passed_costs
        out.append({"timestamp": row.get("timestamp"), "symbol": row.get("symbol"), "side": row.get("side"), "setup": row.get("setup_type") or row.get("setup"), "regime": row.get("regime"), "score": row.get("score"), "min_score_threshold": min_score, "score_threshold_source": threshold_info["source"], "score_scale_detected": threshold_info["score_scale_detected"], "score_threshold_scale_detected": threshold_info["score_threshold_scale_detected"], "threshold_scale_mismatch_detected": threshold_info["threshold_scale_mismatch_detected"], "threshold_scale_correction_applied": threshold_info["threshold_scale_correction_applied"], "score_gap_to_threshold": round(max(0.0, min_score - score), 10), "rr": row.get("rr"), "raw_rr": row.get("raw_rr", row.get("rr")), "effective_rr": row.get("effective_rr"), "expectancy_bucket": row.get("expectancy_bucket"), "spread_pct": row.get("spread_pct"), "expected_slippage_pct": row.get("expected_slippage_pct"), "liquidity_score": row.get("liquidity_score"), "funding_rate_pct": row.get("funding_rate_pct"), "atr_pct": row.get("atr_pct", "UNAVAILABLE_BACKTEST"), "candle_range_pct": row.get("candle_range_pct", "UNAVAILABLE_BACKTEST"), "realized_volatility_pct": row.get("realized_volatility_pct", "UNAVAILABLE_BACKTEST"), "trend_strength": row.get("trend_strength", ""), "chop_score": row.get("chop_score", ""), "range_edge_score": row.get("range_edge_score", ""), "momentum_score": row.get("momentum_score", ""), "volume_24h_usdt": row.get("volume_24h_usdt"), "first_blocking_gate": failed[0] if failed else "LOW_SCORE", "all_failed_gates": json.dumps(failed or ["LOW_SCORE"], sort_keys=True), "passed_rr": passed_rr, "passed_effective_rr": passed_eff, "passed_expectancy": passed_exp, "passed_execution_costs": passed_costs, "would_accept_if_low_score_disabled": would, "counterfactual_reject_reason_if_low_score_ignored": "" if would else ",".join(x for x in failed if x != "LOW_SCORE"), "shadow_outcome": row.get("shadow_outcome", ""), "shadow_r": row.get("shadow_r", ""), "source_function": "backtest_order.evaluate_trade_quality"})
    return out


def classify_low_score(rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    scores = [_safe_float(r.get("score"), 0.0) for r in rows]; thresholds = [_safe_float(r.get("min_score_threshold"), 0.0) for r in rows]
    threshold = thresholds[0] if thresholds else 7.5
    near = sum(1 for s, t in zip(scores, thresholds) if t and 0 <= t - s <= t * 0.05); far = sum(1 for s, t in zip(scores, thresholds) if t and t - s > t * 0.05)
    would = sum(1 for r in rows if r.get("would_accept_if_low_score_disabled") in {True, "True", "true", "1"})
    mismatch = sum(1 for r in rows if r.get("threshold_scale_mismatch_detected") in {True, "True", "true", "1"})
    corrected = sum(1 for r in rows if r.get("threshold_scale_correction_applied") in {True, "True", "true", "1"})
    sources = _distribution([r.get("score_threshold_source", "UNAVAILABLE") for r in rows])
    verdict = "OVERSTRICT_THRESHOLD" if rows and near >= max(1, len(rows)//2) and would >= max(1, len(rows)//2) else "VALID_QUALITY_FILTER"
    return {"low_score_count": len(rows), "score_min": min(scores) if scores else 0.0, "score_max": max(scores) if scores else 0.0, "score_mean": sum(scores)/len(scores) if scores else 0.0, "min_score_threshold": threshold, "score_threshold_source_distribution": sources, "threshold_scale_mismatch_detected_count": mismatch, "threshold_scale_correction_applied_count": corrected, "near_threshold_count": near, "far_below_threshold_count": far, "would_accept_if_low_score_disabled_count": would, "low_score_shadow_tp_count": sum(1 for r in rows if str(r.get("shadow_outcome","")).upper()=="WOULD_TP"), "low_score_shadow_sl_count": sum(1 for r in rows if str(r.get("shadow_outcome","")).upper()=="WOULD_SL"), "low_score_shadow_timeout_count": sum(1 for r in rows if str(r.get("shadow_outcome","")).upper()=="WOULD_TIMEOUT"), "low_score_verdict": verdict, "low_score_evidence": f"{len(rows)} LOW_SCORE rows; {near} near threshold; {far} far below threshold; {would} pass non-score counterfactual gates; threshold sources={sources}.", "recommended_action": "Do not lower score threshold without stronger shadow-outcome, effective-RR, execution-cost, liquidity, and drawdown evidence."}


def _selector_payload(row: Mapping[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Any]:
    diagnostics = _json_dict(row.get("diagnostics"))
    selector = diagnostics.get("selector") if isinstance(diagnostics.get("selector"), dict) else {}
    inputs = selector.get("inputs") if isinstance(selector.get("inputs"), dict) else {}
    metrics = selector.get("metrics") if isinstance(selector.get("metrics"), dict) else {}
    sub_scores = selector.get("sub_scores") if isinstance(selector.get("sub_scores"), dict) else {}
    reject_reasons = diagnostics.get("reject_reasons", selector.get("reject_reasons", row.get("reject_reasons", "")))
    return inputs, metrics, sub_scores, reject_reasons


def _selector_metric(row: Mapping[str, Any], inputs: Mapping[str, Any], metrics: Mapping[str, Any], top_key: str, input_key: str | None = None, metric_key: str | None = None) -> tuple[Any, str]:
    input_key = input_key or top_key; metric_key = metric_key or top_key
    if row.get(top_key) not in ("", None):
        return row.get(top_key), "TOP_LEVEL"
    if inputs.get(input_key) not in ("", None):
        return inputs.get(input_key), "DIAGNOSTICS_SELECTOR_INPUTS"
    if metrics.get(metric_key) not in ("", None):
        return metrics.get(metric_key), "DIAGNOSTICS_SELECTOR_METRICS"
    return "", "MISSING"


def build_symbol_reject_diagnostics(rejected_rows: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rejected_rows:
        reason = str(row.get("reject_reason", "")).upper()
        if reason not in {"TOO_CHOPPY", "WEAK_TREND_AND_NO_RANGE_EDGE"}: continue
        inputs, metrics, sub_scores, reject_reasons = _selector_payload(row)
        chop, chop_src = _selector_metric(row, inputs, metrics, "chop_score")
        trend, trend_src = _selector_metric(row, inputs, metrics, "trend_strength")
        vol, vol_src = _selector_metric(row, inputs, metrics, "realized_volatility_pct", metric_key="volatility_pct")
        candle, candle_src = _selector_metric(row, inputs, metrics, "candle_range_pct")
        spread, spread_src = _selector_metric(row, inputs, metrics, "spread_pct")
        liq, liq_src = _selector_metric(row, inputs, metrics, "liquidity_score")
        volume, volume_src = _selector_metric(row, inputs, metrics, "volume_24h_usdt")
        sources = [chop_src, trend_src, vol_src, candle_src, spread_src, liq_src, volume_src]
        metric_source = next((s for s in sources if s != "MISSING"), "MISSING")
        threshold = row.get("threshold") or row.get("chop_threshold") or row.get("trend_threshold") or ""
        metric = chop if reason == "TOO_CHOPPY" else trend
        gap = (_safe_float(metric) - _safe_float(threshold)) if threshold not in ("", None) and metric not in ("", None) else ""
        out.append({"timestamp": row.get("timestamp"), "symbol": row.get("symbol"), "reject_reason": reason, "regime": row.get("regime"), "metric_source": metric_source, "trend_strength": trend, "chop_score": chop, "range_edge_score": row.get("range_edge_score", ""), "volatility_regime": row.get("volatility_regime", ""), "candle_range_pct": candle or "UNAVAILABLE_BACKTEST", "atr_pct": row.get("atr_pct", "UNAVAILABLE_BACKTEST"), "realized_volatility_pct": vol or "UNAVAILABLE_BACKTEST", "volume_24h_usdt": volume, "spread_pct": spread, "funding_rate_pct": row.get("funding_rate_pct", metrics.get("funding_rate_pct", "")), "liquidity_score": liq, "selector_chop_score": chop, "selector_trend_strength": trend, "selector_volatility_pct": vol, "selector_candle_range_pct": candle, "selector_spread_pct": spread, "selector_liquidity_score": liq, "selector_volume_24h_usdt": volume, "selector_reject_reasons": json.dumps(reject_reasons, sort_keys=True) if isinstance(reject_reasons, (list, dict)) else reject_reasons, "selector_sub_scores": json.dumps(sub_scores, sort_keys=True), "threshold_fields": json.dumps({"threshold": threshold}, sort_keys=True), "ratio_to_threshold": (_safe_float(metric)/_safe_float(threshold) if _safe_float(threshold, 0.0) else ""), "gap_to_threshold": gap, "source_function": "alphaforge.symbol_selector.select_symbol", "historical_safe_data_only": True, "future_leakage_risk": "UNKNOWN: exported diagnostics cannot independently prove feature lookback safety", "interval_sensitive": True, "recommended_action": "Audit market-structure feature scaling by interval before threshold changes."})
    return out


def classify_symbol_reject(rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    missing = sum(1 for r in rows if r.get("metric_source") == "MISSING" and r.get("trend_strength","") in ("", None) and r.get("chop_score","") in ("", None) and r.get("range_edge_score","") in ("", None))
    verdict = "FEATURE_MISSING" if rows and missing == len(rows) else "VALID_MARKET_STRUCTURE_FILTER"
    return {"symbol_reject_count": len(rows), "symbol_reject_distribution": _distribution([r.get("reject_reason") for r in rows]), "missing_market_structure_metric_count": missing, "symbol_reject_verdict": verdict, "symbol_reject_evidence": f"{len(rows)} choppy/weak-trend symbol-level rejects; {missing} lack exported market-structure metrics after diagnostics JSON extraction.", "recommended_action": "Continue symbol-level market-structure audit; do not relax thresholds from count evidence alone."}


def build_zero_accepted_root_cause_summary(summary: Mapping[str, Any], hv: Mapping[str, Any], low: Mapping[str, Any], sym: Mapping[str, Any], manual_command: str = "") -> Dict[str, Any]:
    dist = summary.get("reject_reason_distribution") or summary.get("canonical_reject_reason_distribution") or {}
    if isinstance(dist, str):
        try: dist = json.loads(dist)
        except Exception: dist = {}
    sorted_reasons = sorted(((str(k), int(v)) for k, v in dict(dist).items()), key=lambda x: x[1], reverse=True)
    reasons: list[str] = []
    total = _safe_float(summary.get("total_candidates"), 0.0); accepted = _safe_float(summary.get("accepted_count"), 0.0); rejected = _safe_float(summary.get("rejected_count"), 0.0)
    if total and abs((accepted + rejected) - total) > 0.000001:
        reasons.append("COUNT_RECONCILIATION_FAILED")
    if int(low.get("threshold_scale_mismatch_detected_count", 0) or 0) > int(low.get("threshold_scale_correction_applied_count", 0) or 0):
        reasons.append("LOW_SCORE_THRESHOLD_SCALE_MISMATCH")
    if sym.get("symbol_reject_verdict") == "FEATURE_MISSING":
        reasons.append("SYMBOL_REJECT_METRICS_UNAVAILABLE")
    shadow_total = sum(int(low.get(k, 0) or 0) for k in ["low_score_shadow_tp_count", "low_score_shadow_sl_count", "low_score_shadow_timeout_count"])
    if int(low.get("low_score_count", 0) or 0) and shadow_total == 0:
        reasons.append("SHADOW_OUTCOME_EVIDENCE_MISSING")
    if hv.get("high_vol_guard_count") not in (None, 0) and hv.get("effective_rr_gap_mean") in (None, ""):
        reasons.append("HIGH_VOL_DIAGNOSTICS_INCOMPLETE")
    evidence_quality = "COMPLETE" if sorted_reasons and not reasons else "PARTIAL"
    return {"total_candidates": summary.get("total_candidates"), "accepted_count": summary.get("accepted_count"), "rejected_count": summary.get("rejected_count"), "symbol_rejected_count": summary.get("symbol_rejected_count"), "signal_rejected_count": summary.get("signal_rejected_count"), "reject_distribution": dist, "primary_bottleneck": sorted_reasons[0][0] if sorted_reasons else "UNKNOWN", "secondary_bottleneck": " / ".join([r for r, _ in sorted_reasons[1:3]]) if len(sorted_reasons) > 1 else "UNKNOWN", "high_vol_guard_verdict": hv.get("high_vol_guard_verdict"), "low_score_verdict": low.get("low_score_verdict"), "symbol_reject_verdict": sym.get("symbol_reject_verdict"), "high_vol_guard_conclusion": "HIGH_VOL_GUARD is protective and not the primary zero-accepted bottleneck.", "low_score_conclusion": low.get("low_score_evidence"), "symbol_reject_conclusion": sym.get("symbol_reject_evidence"), "recommended_next_action": "Continue audit on LOW_SCORE and symbol-level market-structure rejects; keep HIGH_VOL_GUARD enabled.", "production_threshold_change_recommended": False, "reason_no_threshold_change": "Counterfactual diagnostics are audit-only and do not show safe expectancy after execution costs.", "evidence_quality": evidence_quality, "evidence_quality_reasons": reasons, "manual_backtest_command_used": manual_command}

def build_default_gate_funnel(rejected: List[Dict[str, Any]], accepted_rows: List[LifecycleRow]) -> List[Dict[str, Any]]:
    gate_order = ["LOW_SCORE", "TOO_CHOPPY", "WEAK_TREND_AND_NO_RANGE_EDGE", "STOP_TOO_WIDE", "RR_TOO_LOW", "DAILY_SYMBOL_TRADE_LIMIT", "REGIME_MISMATCH", "PANIC_CONDITIONS"]
    accepted_count = len(_accepted_terminal_rows_for_risk(accepted_rows))
    total = accepted_count + len(rejected)
    remaining = total
    rows: List[Dict[str, Any]] = []
    for gate in gate_order:
        gate_rows = [r for r in rejected if str(r.get("reject_reason") or "").upper() == gate]
        split = {
            "would_tp_count": sum(1 for r in gate_rows if str(r.get("shadow_outcome") or "").upper() == "WOULD_TP"),
            "would_sl_count": sum(1 for r in gate_rows if str(r.get("shadow_outcome") or "").upper() == "WOULD_SL"),
            "would_timeout_count": sum(1 for r in gate_rows if str(r.get("shadow_outcome") or "").upper() == "WOULD_TIMEOUT"),
        }
        unknown = len(gate_rows) - sum(split.values())
        exp = sum((float(r.get("effective_rr") or r.get("rr") or 0.0) if str(r.get("shadow_outcome") or "").upper() == "WOULD_TP" else (-1.0 if str(r.get("shadow_outcome") or "").upper() == "WOULD_SL" else 0.0)) for r in gate_rows)
        rows.append({
            "gate": gate, "candidates_entering_gate": remaining, "rejected_by_gate": len(gate_rows),
            "accepted_after_gate": max(accepted_count, remaining - len(gate_rows)),
            **split, "unknown_count": unknown,
            "expected_effective_expectancy": (exp / len(gate_rows) if gate_rows else 0.0),
            "gate_visible": True, "zero_reject_warning": len(gate_rows) == 0,
            "funnel_scope": "rejected_orders_plus_executed_terminal_rows",
            "comparability_note": "Matches rejection_counts when rejected_orders.csv contains canonical reject_reason values; zero rows indicate no exported reject rows for this gate or pre-funnel/profile-disabled filtering, not accepted trades.",
        })
        remaining = max(accepted_count, remaining - len(gate_rows))
    return rows


def build_symbol_regime_acceptance_diagnostics(lifecycle_rows: List[LifecycleRow]) -> List[Dict[str, Any]]:
    groups: Dict[tuple[str, str], List[LifecycleRow]] = {}
    for row in _accepted_terminal_rows_for_risk(lifecycle_rows):
        groups.setdefault((row.symbol or "UNKNOWN", row.regime or row.volatility_regime or "UNKNOWN"), []).append(row)
    out: List[Dict[str, Any]] = []
    for (symbol, regime), rows in sorted(groups.items()):
        score10 = [r for r in rows if float(r.score or 0.0) >= 10.0]
        high = [r for r in rows if str(r.regime or r.volatility_regime).upper() in {"HIGH", "PANIC", "BREAKOUT", "NEWS_DRIVEN"}]
        normal = [r for r in rows if str(r.regime or r.volatility_regime).upper() in {"NORMAL", "TREND", "RANGE"}]
        def cnt(rs, reason): return sum(1 for r in rs if (r.close_reason or r.status_after) == reason)
        n = len(rows)
        out.append({
            "symbol": symbol, "regime": regime, "accepted_count": n,
            "tp_count": cnt(rows, "TP_HIT"), "sl_count": cnt(rows, "SL_HIT"),
            "tp_rate": cnt(rows, "TP_HIT") / n if n else 0.0, "sl_rate": cnt(rows, "SL_HIT") / n if n else 0.0,
            "mean_effective_rr": sum(_safe_float(r.effective_rr if r.effective_rr is not None else r.rr, 0.0) for r in rows) / n if n else 0.0,
            "mean_net_pnl": sum(float(r.net_pnl_usdt or 0.0) for r in rows) / n if n else 0.0,
            "score_10_tp_count": cnt(score10, "TP_HIT"), "score_10_sl_count": cnt(score10, "SL_HIT"),
            "high_regime_tp_count": cnt(high, "TP_HIT"), "high_regime_sl_count": cnt(high, "SL_HIT"),
            "normal_regime_tp_count": cnt(normal, "TP_HIT"), "normal_regime_sl_count": cnt(normal, "SL_HIT"),
        })
    return out

def write_backtest_quality_summary(path: str, summary: Mapping[str, Any]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["metric", "value"])
        w.writeheader()
        for key, value in summary.items():
            serialized = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value
            w.writerow({"metric": key, "value": serialized})


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


def _is_fake_missing_zero(value: Any) -> bool:
    if value in (None, "", "UNAVAILABLE", "UNAVAILABLE_BACKTEST"):
        return False
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def verify_export_integrity(
    persisted_lifecycle_rows: List[Mapping[str, Any]],
    rejected_rows: List[Mapping[str, Any]],
    lifecycle_csv_rows: List[Mapping[str, Any]],
    rejected_csv_rows: List[Mapping[str, Any]],
) -> list[str]:
    errors: list[str] = []
    rejected_states = {"SIGNAL_REJECTED", "ORDER_REJECTED", "SYMBOL_REJECTED"}
    if len(persisted_lifecycle_rows) != len(lifecycle_csv_rows):
        errors.append("lifecycle row count mismatch between SQLite lifecycle and order_lifecycle.csv")
    if len(rejected_rows) != len(rejected_csv_rows):
        errors.append("rejected row count mismatch between rejected records and rejected_orders.csv")
    lifecycle_reject_count = sum(
        1
        for row in persisted_lifecycle_rows
        if str(row.get("lifecycle_state", "") or "").strip().upper() in rejected_states
    )
    if lifecycle_reject_count != len(rejected_csv_rows):
        errors.append("rejected_orders.csv count mismatch with rejected lifecycle SQL rows")
    signal_terminals: dict[str, set[str]] = {}
    score_by_signal: dict[str, Any] = {}
    rr_by_signal: dict[str, Any] = {}
    for idx, row in enumerate(persisted_lifecycle_rows):
        signal_id = str(row.get("signal_id", "") or f"idx:{idx}")
        lifecycle_state = str(row.get("lifecycle_state", "") or "").strip().upper()
        status_after = str(row.get("status_after", "") or lifecycle_state).strip().upper()
        if lifecycle_state == "" or status_after == "":
            errors.append(f"lifecycle row index={idx} missing lifecycle_state/status_after")
        if lifecycle_state == "CREATED" or status_after == "CREATED":
            errors.append(f"lifecycle row index={idx} uses legacy CREATED state")

        if lifecycle_state == "SIGNAL_REJECTED" and "SYMBOL_REJECTED" in str(row.get("event_id", "") or "").upper():
            errors.append(f"lifecycle row index={idx} mislabels selector reject as SIGNAL_REJECTED")
        if lifecycle_state == "SYMBOL_REJECTED" and signal_id in score_by_signal:
            errors.append(f"signal_id={signal_id} has SYMBOL_REJECTED after signal creation")
        if lifecycle_state == "ORDER_REJECTED" and status_after == "SIGNAL_CREATED":
            errors.append(f"lifecycle row index={idx} has impossible SIGNAL_CREATED -> ORDER_REJECTED status")
        decision = str(row.get("decision", "")).upper()
        reject_reason = str(row.get("reject_reason", "") or "").strip().upper()
        if (decision == "REJECTED" or lifecycle_state in rejected_states) and reject_reason in {"", "UNKNOWN"}:
            errors.append(f"rejected lifecycle row index={idx} missing reject_reason")
        expectancy_bucket = str(row.get("expectancy_bucket", "") or "").strip().upper()
        if expectancy_bucket == "":
            errors.append(f"lifecycle row index={idx} missing expectancy_bucket")
        signal_terminals.setdefault(signal_id, set()).add(lifecycle_state)
        if lifecycle_state == "SIGNAL_CREATED":
            score_by_signal[signal_id] = row.get("score")
            rr_by_signal[signal_id] = row.get("rr")
        ctx = _decode_execution_ctx(row.get("execution_ctx"))
        execution_ctx_missing = str(row.get("execution_ctx_missing", "") or "").strip().lower() in {"1", "true", "t", "yes"}
        if execution_ctx_missing:
            for field in ("volume_24h_usdt", "spread_pct", "funding_rate_pct", "expected_slippage_pct", "liquidity_score"):
                if _is_fake_missing_zero(ctx.get(field)):
                    errors.append(f"lifecycle row index={idx} has fake zero for missing execution field {field}")
    for signal_id, states in signal_terminals.items():
        if states == {"SIGNAL_CREATED"}:
            errors.append(f"signal_id={signal_id} has CREATED-only lifecycle export")
    if len(score_by_signal) >= 3 and len({str(v) for v in score_by_signal.values()}) == 1:
        errors.append("score distribution suspiciously constant across lifecycle candidates")
    if len(rr_by_signal) >= 3 and len({str(v) for v in rr_by_signal.values()}) == 1:
        errors.append("rr distribution suspiciously constant across lifecycle candidates")
    return errors

def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
def _bucket_execution_quality(spread_pct: Any, slippage_pct: Any, liquidity_score: Any) -> str:
    spread = _safe_float(spread_pct, -1.0)
    slippage = _safe_float(slippage_pct, -1.0)
    liquidity = _safe_float(liquidity_score, -1.0)
    if spread < 0.0 or slippage < 0.0 or liquidity < 0.0:
        return "UNAVAILABLE"
    if spread <= 0.03 and slippage <= 0.02 and liquidity >= 0.7:
        return "HIGH"
    if spread <= 0.08 and slippage <= 0.05 and liquidity >= 0.4:
        return "MEDIUM"
    return "LOW"


def evaluate_forward_window(
    candidate_row: Mapping[str, Any],
    candles: List[Candle],
    idx: int,
    forward_window_minutes: int = 240,
) -> ForwardWindowEvaluation:
    outcome = simulate_rejected_counterfactual(
        CandidateOrder(
            timestamp=int(candidate_row.get("timestamp", 0)),
            symbol=str(candidate_row.get("symbol", "")),
            side=str(candidate_row.get("side", "LONG")),
            entry=_safe_float(candidate_row.get("entry"), 0.0),
            sl=_safe_float(candidate_row.get("sl"), 0.0),
            tp=_safe_float(candidate_row.get("tp"), 0.0),
            rr=_safe_float(candidate_row.get("rr"), 0.0),
            setup_type=str(candidate_row.get("setup_type", "")),
            setup_reason=str(candidate_row.get("setup_reason", "")),
            regime=str(candidate_row.get("regime", "")),
            score=_safe_float(candidate_row.get("score"), 0.0),
            order_type=str(candidate_row.get("order_type", "LIMIT")),
        ),
        candles,
        idx,
        timeout_bars=forward_window_minutes,
    )
    entry = max(_safe_float(candidate_row.get("entry"), 0.0), 1e-9)
    mfe_pct = (float(outcome["max_favorable_excursion"]) / entry) * 100.0
    mae_pct = (float(outcome["max_adverse_excursion"]) / entry) * 100.0
    decision = str(candidate_row.get("decision", "") or "").upper()
    lifecycle_state = str(candidate_row.get("status_after", candidate_row.get("lifecycle_state", "")) or "").upper()
    reject_reason = str(candidate_row.get("reject_reason", "") or "")
    is_rejected = decision == "REJECTED" or lifecycle_state in {"SIGNAL_REJECTED", "ORDER_REJECTED", "SYMBOL_REJECTED"} or reject_reason != ""
    reject_correct = None
    reject_missed_winner = False
    reject_saved_from_loss = False
    if is_rejected:
        reject_missed_winner = bool(outcome["would_tp_hit"]) and not bool(outcome["would_sl_hit"])
        reject_saved_from_loss = bool(outcome["would_sl_hit"]) and not bool(outcome["would_tp_hit"])
        reject_correct = reject_saved_from_loss
    return ForwardWindowEvaluation(
        signal_id=str(candidate_row.get("signal_id", f"{candidate_row.get('symbol','')}:{candidate_row.get('timestamp','')}")),
        symbol=str(candidate_row.get("symbol", "")),
        decision=("REJECTED" if is_rejected else ("ACCEPTED" if decision == "ACCEPTED" else decision)),
        lifecycle_state=lifecycle_state,
        reject_reason=reject_reason,
        setup_type=str(candidate_row.get("setup_type", "UNKNOWN")),
        score=_safe_float(candidate_row.get("score"), 0.0),
        rr=_safe_float(candidate_row.get("rr"), 0.0),
        effective_rr=_safe_float(candidate_row.get("effective_rr"), _safe_float(candidate_row.get("rr"), 0.0)),
        predicted_quality=1.0 if _bucket_execution_quality(
            candidate_row.get("spread_pct"),
            candidate_row.get("slippage_pct", candidate_row.get("expected_slippage_pct")),
            candidate_row.get("liquidity_score"),
        ) == "HIGH" else 0.0,
        forward_window_minutes=forward_window_minutes,
        would_have_hit_tp=bool(outcome["would_tp_hit"]),
        would_have_hit_sl=bool(outcome["would_sl_hit"]),
        mfe_pct=round(mfe_pct, 8),
        mae_pct=round(mae_pct, 8),
        max_forward_return=round(mfe_pct, 8),
        max_adverse_return=round(-mae_pct, 8),
        reject_correct=reject_correct,
        reject_missed_winner=reject_missed_winner,
        reject_saved_from_loss=reject_saved_from_loss,
        forward_window_regime=str(candidate_row.get("regime", "UNKNOWN")),
        execution_quality_bucket=_bucket_execution_quality(
            candidate_row.get("spread_pct"),
            candidate_row.get("slippage_pct", candidate_row.get("expected_slippage_pct")),
            candidate_row.get("liquidity_score"),
        ),
    )
def _realized_outcome_from_row(row: Mapping[str, Any]) -> str:
    state = str(row.get("lifecycle_state", row.get("status_after", "")) or "").upper()
    if state in {"SIGNAL_REJECTED", "ORDER_REJECTED", "SYMBOL_REJECTED"}:
        return "REJECTED"
    if state in {"EXPIRED", "CANCELED", "CANCELLED"}:
        return "CANCELED" if state != "EXPIRED" else "EXPIRED"
    close_reason = str(row.get("close_reason", "") or "").upper()
    if close_reason in {"TP_HIT", "SL_HIT"}:
        return close_reason
    if close_reason in {"TIMEOUT", "OPEN_AT_END"}:
        return "OPEN_AT_END"
    return "NON_TERMINAL"


def build_forward_evaluation_rows(
    lifecycle_rows: List[Mapping[str, Any]],
    candles_by_symbol: Mapping[str, List[Candle]],
    forward_window_minutes: int = 240,
) -> List[dict[str, Any]]:
    out: List[dict[str, Any]] = []
    terminal = {"TP_HIT", "SL_HIT", "EXPIRED", "CANCELED", "OPEN_AT_END", "REJECTED"}
    for row in lifecycle_rows:
        realized = _realized_outcome_from_row(row)
        if realized not in terminal:
            continue
        symbol = str(row.get("symbol", ""))
        ts = int(_safe_float(row.get("timestamp", row.get("event_ts")), 0.0))
        candles = candles_by_symbol.get(symbol, [])
        idx = next((i for i, c in enumerate(candles) if c.timestamp >= ts), len(candles))
        evaluation = evaluate_forward_window(row, candles, idx, forward_window_minutes=forward_window_minutes)
        out.append({**asdict(evaluation), "realized_outcome": realized})
    return out


def _bucket_numeric(value: float, bounds: list[float], labels: list[str]) -> str:
    for idx, bound in enumerate(bounds):
        if value <= bound:
            return labels[idx]
    return labels[-1]


def build_forward_evaluations_from_lifecycle(
    lifecycle: List[LifecycleRow],
    candles_by_symbol: Mapping[str, List[Candle]],
    forward_window_minutes: int = 240,
) -> List[ForwardWindowEvaluation]:
    out: List[ForwardWindowEvaluation] = []
    for row in lifecycle:
        if row.status_after != "POSITION_CLOSED":
            continue
        if row.close_reason not in TERMINAL_FORWARD_CLOSE_REASONS:
            continue
        candles = candles_by_symbol.get(row.symbol, [])
        idx = next((i for i, c in enumerate(candles) if c.timestamp >= row.timestamp), len(candles))
        out.append(
            evaluate_forward_window(
                {
                    "signal_id": f"{row.symbol}:{row.timestamp}",
                    "timestamp": row.timestamp,
                    "symbol": row.symbol,
                    "side": row.side,
                    "entry": row.entry,
                    "sl": row.sl,
                    "tp": row.tp,
                    "rr": row.rr,
                    "score": row.score,
                    "regime": row.regime,
                    "setup_type": row.setup_type,
                    "reject_reason": row.reject_reason,
                    "decision": "ACCEPTED",
                    "lifecycle_state": row.status_after,
                    "spread_pct": row.spread_pct,
                    "expected_slippage_pct": row.expected_slippage_pct,
                    "liquidity_score": row.liquidity_score,
                },
                candles,
                idx,
                forward_window_minutes=forward_window_minutes,
            )
        )
    return out


def persist_calibration_snapshots(evals: List[ForwardWindowEvaluation], lifecycle_index: Mapping[str, LifecycleRow]) -> List[dict[str, Any]]:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        for ev in evals:
            src = lifecycle_index.get(ev.signal_id)
            session.execute(text("""
                INSERT INTO calibration_snapshots (
                    signal_id,predicted_quality,realized_outcome,score,rr,effective_rr,regime,setup_type,rejection_reason,
                    forward_window_minutes,mfe_pct,mae_pct,would_have_hit_tp,would_have_hit_sl,reject_correct,created_at
                ) VALUES (
                    :signal_id,:predicted_quality,:realized_outcome,:score,:rr,:effective_rr,:regime,:setup_type,:rejection_reason,
                    :forward_window_minutes,:mfe_pct,:mae_pct,:would_have_hit_tp,:would_have_hit_sl,:reject_correct,:created_at
                )
                ON CONFLICT(signal_id, forward_window_minutes) DO NOTHING
            """), {
                "signal_id": ev.signal_id,
                "predicted_quality": (src.score if src is not None else None),
                "realized_outcome": ("WOULD_TP" if ev.would_have_hit_tp else ("WOULD_SL" if ev.would_have_hit_sl else "NEUTRAL")),
                "score": (src.score if src is not None else None),
                "rr": (src.rr if src is not None else None),
                "effective_rr": (src.effective_rr if src is not None else (src.rr if src is not None else None)),
                "regime": ev.forward_window_regime,
                "setup_type": (src.setup_type if src is not None else None),
                "rejection_reason": ev.reject_reason,
                "forward_window_minutes": ev.forward_window_minutes,
                "mfe_pct": ev.mfe_pct,
                "mae_pct": ev.mae_pct,
                "would_have_hit_tp": int(ev.would_have_hit_tp),
                "would_have_hit_sl": int(ev.would_have_hit_sl),
                "reject_correct": (None if ev.reject_correct is None else int(ev.reject_correct)),
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
        rows = session.execute(text("SELECT * FROM calibration_snapshots ORDER BY signal_id, forward_window_minutes")).mappings().all()
    return [dict(r) for r in rows]
    entry = max(_safe_float(candidate_row.get("entry"), 0.0), 1e-9)
    mfe_pct = (float(outcome["max_favorable_excursion"]) / entry) * 100.0
    mae_pct = (float(outcome["max_adverse_excursion"]) / entry) * 100.0
    decision = str(candidate_row.get("decision", "") or "").upper()
    lifecycle_state = str(candidate_row.get("status_after", candidate_row.get("lifecycle_state", "")) or "").upper()
    reject_reason = str(candidate_row.get("reject_reason", "") or "")
    is_rejected = decision == "REJECTED" or lifecycle_state in {"SIGNAL_REJECTED", "ORDER_REJECTED", "SYMBOL_REJECTED"} or reject_reason != ""
    reject_correct = None
    reject_missed_winner = False
    reject_saved_from_loss = False
    if is_rejected:
        reject_missed_winner = bool(outcome["would_tp_hit"]) and not bool(outcome["would_sl_hit"])
        reject_saved_from_loss = bool(outcome["would_sl_hit"]) and not bool(outcome["would_tp_hit"])
        reject_correct = reject_saved_from_loss
    return ForwardWindowEvaluation(
        signal_id=str(candidate_row.get("signal_id", f"{candidate_row.get('symbol','')}:{candidate_row.get('timestamp','')}")),
        symbol=str(candidate_row.get("symbol", "")),
        decision=("REJECTED" if is_rejected else ("ACCEPTED" if decision == "ACCEPTED" else decision)),
        lifecycle_state=lifecycle_state,
        reject_reason=reject_reason,
        forward_window_minutes=forward_window_minutes,
        would_have_hit_tp=bool(outcome["would_tp_hit"]),
        would_have_hit_sl=bool(outcome["would_sl_hit"]),
        mfe_pct=round(mfe_pct, 8),
        mae_pct=round(mae_pct, 8),
        max_forward_return=round(mfe_pct, 8),
        max_adverse_return=round(-mae_pct, 8),
        reject_correct=reject_correct,
        reject_missed_winner=reject_missed_winner,
        reject_saved_from_loss=reject_saved_from_loss,
        forward_window_regime=str(candidate_row.get("regime", "UNKNOWN")),
        execution_quality_bucket=_bucket_execution_quality(
            candidate_row.get("spread_pct"),
            candidate_row.get("slippage_pct", candidate_row.get("expected_slippage_pct")),
            candidate_row.get("liquidity_score"),
        ),
    )

FORWARD_OUTCOME_VALUES = ["WOULD_TP", "WOULD_SL", "WOULD_TIMEOUT", "WOULD_AMBIGUOUS", "INSUFFICIENT_FORWARD_BARS", "NO_TP_SL_GEOMETRY", "SYMBOL_REJECT_NO_CANDIDATE_GEOMETRY"]


def _interval_to_minutes(interval: str) -> int:
    text = str(interval or "").strip().lower()
    try:
        if text.endswith("m"):
            return max(1, int(text[:-1]))
        if text.endswith("h"):
            return max(1, int(text[:-1]) * 60)
        if text.endswith("d"):
            return max(1, int(text[:-1]) * 1440)
    except ValueError:
        return 60
    return 60


def _has_tp_sl_geometry(row: Mapping[str, Any]) -> bool:
    if str(row.get("side", "") or "").upper() not in {"LONG", "SHORT"}:
        return False
    return all(_safe_float(row.get(k), 0.0) > 0.0 for k in ("entry", "sl", "tp"))


def _cost_penalty_from_row(row: Mapping[str, Any], raw_rr: float) -> tuple[float, float]:
    spread_pct, _ = normalize_pct_input(row.get("spread_pct"), field="spread_pct")
    slippage = _safe_float(row.get("expected_slippage_pct", row.get("slippage_pct", 0.0)), 0.0)
    liquidity = _safe_float(row.get("liquidity_score"), 1.0)
    effective_rr, _, breakdown = _execution_reject_flags(raw_rr, {"spread_pct": spread_pct, "expected_slippage_pct": slippage, "liquidity_score": liquidity})
    return effective_rr, _safe_float(breakdown.get("cost_penalty_total"), max(raw_rr - effective_rr, 0.0))



def _numeric_or_none(value: Any) -> float | None:
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def _low_score_forward_metadata(row: Mapping[str, Any]) -> Dict[str, Any]:
    diagnostics = _json_dict(row.get("diagnostics"))
    threshold_info = _low_score_threshold_for_row(row, diagnostics)
    score_value = row.get("score", diagnostics.get("score"))
    score_num = _numeric_or_none(score_value)
    score = _safe_float(score_value, 0.0)
    threshold = _numeric_or_none(row.get("min_score_threshold"))
    threshold_source = row.get("score_threshold_source", "")
    if threshold is None:
        threshold = _numeric_or_none(row.get("min_required_score"))
    if threshold is None:
        threshold = _safe_float(threshold_info.get("threshold"), 7.5)
        threshold_source = threshold_info.get("source", "decision_filter_config.BACKTEST.MIN_TRADE_SCORE")
    if not threshold_source:
        threshold_source = threshold_info.get("source", "UNAVAILABLE")
    raw_gap = _numeric_or_none(row.get("score_gap_to_threshold"))
    if raw_gap is not None:
        gap = raw_gap
        gap_source = "row.score_gap_to_threshold"
    elif score_num is not None and threshold is not None:
        gap = threshold - score_num
        gap_source = "computed:min_score_threshold-score"
    else:
        gap = ""
        gap_source = "UNAVAILABLE"
    failed = sorted(set([str(x) for x in _json_list(row.get("all_failed_gates")) if str(x)]))
    cfg = decision_filter_config("BACKTEST")
    raw = _safe_float(row.get("raw_rr", row.get("rr")), 0.0)
    eff = _safe_float(row.get("effective_rr"), raw)
    passed_rr = raw >= float(cfg.get("MIN_RR", 1.3))
    passed_eff = eff >= float(cfg.get("MIN_EFFECTIVE_RR", 1.6))
    passed_exp = str(row.get("expectancy_bucket", "")).upper() not in {"NEGATIVE", "MISSING"}
    passed_costs = "HIGH_SPREAD" not in failed and "HIGH_SLIPPAGE" not in failed and "THIN_LIQUIDITY" not in failed
    would = row.get("would_accept_if_low_score_disabled")
    if would in ("", None):
        would = passed_rr and passed_eff and passed_exp and passed_costs
    return {
        "min_score_threshold": threshold,
        "score_gap_to_threshold": gap,
        "score_gap_source": gap_source,
        "score_threshold_source": threshold_source,
        "score_scale_detected": row.get("score_scale_detected", threshold_info.get("score_scale_detected")),
        "score_threshold_scale_detected": row.get("score_threshold_scale_detected", threshold_info.get("score_threshold_scale_detected")),
        "threshold_scale_mismatch_detected": row.get("threshold_scale_mismatch_detected", threshold_info.get("threshold_scale_mismatch_detected")),
        "threshold_scale_correction_applied": row.get("threshold_scale_correction_applied", threshold_info.get("threshold_scale_correction_applied")),
        "would_accept_if_low_score_disabled": bool(_truthy(would)),
    }


def _symbol_forward_metadata(row: Mapping[str, Any]) -> Dict[str, Any]:
    inputs, metrics, sub_scores, reject_reasons = _selector_payload(row)
    chop, chop_src = _selector_metric(row, inputs, metrics, "chop_score")
    trend, trend_src = _selector_metric(row, inputs, metrics, "trend_strength")
    vol, _ = _selector_metric(row, inputs, metrics, "realized_volatility_pct", metric_key="volatility_pct")
    candle, _ = _selector_metric(row, inputs, metrics, "candle_range_pct")
    spread, _ = _selector_metric(row, inputs, metrics, "spread_pct")
    liq, _ = _selector_metric(row, inputs, metrics, "liquidity_score")
    volume, _ = _selector_metric(row, inputs, metrics, "volume_24h_usdt")
    metric_source = next((src for src in [chop_src, trend_src] if src != "MISSING"), row.get("metric_source", "MISSING"))
    return {
        "metric_source": metric_source,
        "chop_score": chop,
        "trend_strength": trend,
        "range_edge_score": row.get("range_edge_score", metrics.get("range_edge_score", inputs.get("range_edge_score", ""))),
        "selector_chop_score": chop,
        "selector_trend_strength": trend,
        "selector_volatility_pct": vol,
        "selector_candle_range_pct": candle,
        "selector_spread_pct": spread,
        "selector_liquidity_score": liq,
        "selector_volume_24h_usdt": volume,
        "selector_reject_reasons": json.dumps(reject_reasons, sort_keys=True) if isinstance(reject_reasons, (list, dict)) else reject_reasons,
        "selector_sub_scores": json.dumps(sub_scores, sort_keys=True) if isinstance(sub_scores, (list, dict)) else sub_scores,
    }

def evaluate_rejected_forward_outcome(row: Mapping[str, Any], candles: List[Candle], *, forward_window_bars: int = 240, interval_minutes: int = 60) -> Dict[str, Any]:
    """Diagnostic-only first-touch evaluation using candles strictly after reject timestamp."""
    ts = int(_safe_float(row.get("timestamp"), 0.0))
    reason = str(row.get("reject_reason", "UNKNOWN") or "UNKNOWN").upper()
    lifecycle = str(row.get("lifecycle_state", row.get("status_after", "")) or "").upper()
    raw_rr = _safe_float(row.get("raw_rr", row.get("rr")), 0.0)
    effective_rr, cost_penalty = _cost_penalty_from_row(row, raw_rr)
    low_meta = _low_score_forward_metadata(row) if reason == "LOW_SCORE" else {}
    symbol_meta = _symbol_forward_metadata(row) if reason in {"TOO_CHOPPY", "WEAK_TREND_AND_NO_RANGE_EDGE"} else {}
    base = {
        "timestamp": row.get("timestamp"), "symbol": row.get("symbol"), "side": row.get("side"),
        "reject_reason": reason, "lifecycle_state": lifecycle, "candidate_stage": row.get("source_stage", row.get("setup_reason", "UNAVAILABLE")),
        "setup": row.get("setup_type", row.get("setup", "UNAVAILABLE")), "regime": row.get("regime", "UNAVAILABLE"),
        "score": row.get("score"), "min_score_threshold": low_meta.get("min_score_threshold", row.get("min_score_threshold", row.get("min_required_score", ""))),
        "score_gap_to_threshold": low_meta.get("score_gap_to_threshold", row.get("score_gap_to_threshold", "")),
        "score_gap_source": low_meta.get("score_gap_source", ""),
        "score_threshold_source": low_meta.get("score_threshold_source", row.get("score_threshold_source", "")),
        "score_scale_detected": low_meta.get("score_scale_detected", row.get("score_scale_detected", "")),
        "score_threshold_scale_detected": low_meta.get("score_threshold_scale_detected", row.get("score_threshold_scale_detected", "")),
        "threshold_scale_mismatch_detected": low_meta.get("threshold_scale_mismatch_detected", row.get("threshold_scale_mismatch_detected", "")),
        "threshold_scale_correction_applied": low_meta.get("threshold_scale_correction_applied", row.get("threshold_scale_correction_applied", "")),
        "would_accept_if_low_score_disabled": low_meta.get("would_accept_if_low_score_disabled", row.get("would_accept_if_low_score_disabled", "")),
        "raw_rr": raw_rr, "effective_rr": effective_rr,
        "entry": row.get("entry"), "sl": row.get("sl"), "tp": row.get("tp"),
        "stop_distance_pct": row.get("stop_distance_pct", ""), "target_distance_pct": row.get("target_distance_pct", ""),
        "expectancy_bucket": row.get("expectancy_bucket", ""), "spread_pct": row.get("spread_pct", ""),
        "expected_slippage_pct": row.get("expected_slippage_pct", row.get("slippage_pct", "")), "funding_rate_pct": row.get("funding_rate_pct", ""),
        "liquidity_score": row.get("liquidity_score", ""), "volume_24h_usdt": row.get("volume_24h_usdt", ""),
        "volatility_regime": row.get("volatility_regime", ""), "candle_range_pct": row.get("candle_range_pct", ""),
        "atr_pct": row.get("atr_pct", ""), "realized_volatility_pct": row.get("realized_volatility_pct", row.get("volatility_pct", "")),
        "forward_window_bars": forward_window_bars, "forward_window_minutes": forward_window_bars * interval_minutes,
        "first_touch_timestamp": "", "bars_to_first_touch": "", "minutes_to_first_touch": "", "mfe_pct": 0.0, "mae_pct": 0.0,
        "mfe_r": 0.0, "mae_r": 0.0, "max_favorable_before_adverse": 0.0, "max_adverse_before_favorable": 0.0,
        "gross_shadow_r": 0.0, "effective_shadow_r_after_costs": 0.0, "cost_penalty": cost_penalty, "shadow_net_expectancy_r": 0.0,
        "shadow_outcome_confidence": "UNAVAILABLE", "shadow_unavailable_reason": "", "historical_safe_data_only": True,
        "future_leakage_risk": "PASS", "source_function": "backtest_order.evaluate_rejected_forward_outcome",
        **symbol_meta,
    }
    if not _has_tp_sl_geometry(row):
        outcome = "SYMBOL_REJECT_NO_CANDIDATE_GEOMETRY" if lifecycle == "SYMBOL_REJECTED" or reason in {"TOO_CHOPPY", "WEAK_TREND_AND_NO_RANGE_EDGE"} else "NO_TP_SL_GEOMETRY"
        return {**base, "first_touch_outcome": outcome, "shadow_unavailable_reason": outcome}
    entry = _safe_float(row.get("entry")); sl = _safe_float(row.get("sl")); tp = _safe_float(row.get("tp")); side = str(row.get("side", "LONG")).upper()
    risk = abs(entry - sl)
    if risk <= 0.0:
        return {**base, "first_touch_outcome": "NO_TP_SL_GEOMETRY", "shadow_unavailable_reason": "NO_TP_SL_GEOMETRY"}
    scan = [c for c in candles if c.timestamp > ts][:forward_window_bars]
    if not scan:
        return {**base, "first_touch_outcome": "INSUFFICIENT_FORWARD_BARS", "shadow_unavailable_reason": "INSUFFICIENT_FORWARD_BARS"}
    mfe = mae = 0.0; outcome = "WOULD_TIMEOUT"; touch_ts = ""; bars = ""
    for i, c in enumerate(scan, start=1):
        if side == "SHORT":
            favorable = max(0.0, entry - c.low); adverse = max(0.0, c.high - entry); hit_tp = c.low <= tp; hit_sl = c.high >= sl
        else:
            favorable = max(0.0, c.high - entry); adverse = max(0.0, entry - c.low); hit_tp = c.high >= tp; hit_sl = c.low <= sl
        mfe = max(mfe, favorable); mae = max(mae, adverse)
        if hit_tp and hit_sl:
            outcome = "WOULD_AMBIGUOUS"; touch_ts = c.timestamp; bars = i; break
        if hit_tp or hit_sl:
            outcome = "WOULD_TP" if hit_tp else "WOULD_SL"; touch_ts = c.timestamp; bars = i; break
    if outcome == "WOULD_TIMEOUT" and len(scan) < forward_window_bars:
        outcome = "INSUFFICIENT_FORWARD_BARS"
    gross_r = raw_rr if outcome == "WOULD_TP" else (-1.0 if outcome == "WOULD_SL" else 0.0)
    effective_shadow_r = effective_rr if outcome == "WOULD_TP" else (-1.0 - cost_penalty if outcome == "WOULD_SL" else 0.0)
    return {**base, "first_touch_outcome": outcome, "first_touch_timestamp": touch_ts, "bars_to_first_touch": bars, "minutes_to_first_touch": (bars * interval_minutes if isinstance(bars, int) else ""), "mfe_pct": (mfe / entry) * 100.0, "mae_pct": (mae / entry) * 100.0, "mfe_r": mfe / risk, "mae_r": mae / risk, "max_favorable_before_adverse": mfe / risk, "max_adverse_before_favorable": mae / risk, "gross_shadow_r": gross_r, "effective_shadow_r_after_costs": effective_shadow_r, "shadow_net_expectancy_r": effective_shadow_r, "shadow_outcome_confidence": ("HIGH" if outcome in {"WOULD_TP", "WOULD_SL"} else "MEDIUM"), "shadow_unavailable_reason": (outcome if outcome in {"INSUFFICIENT_FORWARD_BARS"} else "")}


def build_rejected_forward_outcomes(rejected_rows: List[Mapping[str, Any]], candles_by_symbol: Mapping[str, List[Candle]], *, forward_window_bars: int = 240, interval_minutes: int = 60) -> List[Dict[str, Any]]:
    return [evaluate_rejected_forward_outcome(r, candles_by_symbol.get(str(r.get("symbol", "")), []), forward_window_bars=forward_window_bars, interval_minutes=interval_minutes) for r in rejected_rows]


def _mean(vals: List[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _median(vals: List[float]) -> float:
    vals = sorted(vals)
    if not vals: return 0.0
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _p95(vals: List[float]) -> float:
    vals = sorted(vals)
    if not vals: return 0.0
    return vals[min(len(vals) - 1, int(len(vals) * 0.95))]


def _is_forward_evaluable(r: Mapping[str, Any]) -> bool:
    return str(r.get("first_touch_outcome", "")).upper() in {"WOULD_TP", "WOULD_SL", "WOULD_TIMEOUT", "WOULD_AMBIGUOUS"}


def _low_score_gap_bucket(row: Mapping[str, Any]) -> str:
    gap = _numeric_or_none(row.get("score_gap_to_threshold"))
    threshold = _numeric_or_none(row.get("min_score_threshold"))
    if gap is None or threshold is None or threshold <= 0.0:
        return "above_threshold_or_unknown"
    if 0.0 <= gap <= threshold * 0.05:
        return "near"
    if gap > threshold * 0.05:
        return "far"
    return "above_threshold_or_unknown"


def _utc_hour_from_row(row: Mapping[str, Any]) -> int | None:
    direct = _numeric_or_none(row.get("hour_utc"))
    if direct is not None:
        return int(direct) % 24
    ts = _numeric_or_none(row.get("timestamp"))
    if ts is None:
        return None
    # Backtest timestamps are epoch milliseconds in exported rows.
    if ts > 10_000_000_000:
        ts = ts / 1000.0
    return datetime.fromtimestamp(ts, tz=timezone.utc).hour


def _hour_group(hour: int | None) -> str:
    if hour in {6, 7, 18, 22, 23}:
        return "SHORT_LOW_SCORE_GOOD_UTC_HOURS"
    if hour in {0, 4, 8, 15, 17}:
        return "LONG_BREAKOUT_BAD_UTC_HOURS"
    if hour is None:
        return "HOUR_UNAVAILABLE"
    return "OTHER_UTC_HOURS"


def _score_gap_band(row: Mapping[str, Any]) -> str:
    bucket = _low_score_gap_bucket(row)
    if bucket == "near":
        return "NEAR_THRESHOLD_5PCT"
    if bucket == "far":
        return "FAR_BELOW_THRESHOLD"
    return "ABOVE_THRESHOLD_OR_UNKNOWN"


def _confidence_lower_bound(values: List[float]) -> float:
    if not values:
        return 0.0
    mean = _mean(values)
    if len(values) < 2:
        return mean
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return mean - 1.96 * ((variance ** 0.5) / (len(values) ** 0.5))


def build_reject_bucket_expectancy(rows: List[Mapping[str, Any]], *, min_n: int = 30, exploratory_min_n: int = 10) -> List[Dict[str, Any]]:
    buckets: Dict[tuple[str, str, str, str, str, str, str], List[Mapping[str, Any]]] = {}
    for row in rows:
        hour = _utc_hour_from_row(row)
        key = (
            str(row.get("symbol") or "UNKNOWN").upper(),
            str(row.get("side") or "UNKNOWN").upper(),
            str(row.get("setup") or row.get("setup_type") or "UNAVAILABLE").upper(),
            str(row.get("regime") or "UNAVAILABLE").upper(),
            _hour_group(hour),
            str(row.get("reject_reason") or "UNKNOWN").upper(),
            _score_gap_band(row),
        )
        buckets.setdefault(key, []).append(row)

    out: List[Dict[str, Any]] = []
    for key, sub in sorted(buckets.items()):
        ev = [r for r in sub if _is_forward_evaluable(r)]
        vals = [_safe_float(r.get("effective_shadow_r_after_costs")) for r in ev]
        n = len(ev)
        mean_eff = _mean(vals)
        p95_adv = _p95([_safe_float(r.get("mae_r")) for r in ev])
        exploratory = len(sub) >= exploratory_min_n and len(sub) < min_n
        if n < min_n:
            verdict = "INSUFFICIENT_SAMPLE"
        elif mean_eff > 0.05 and _confidence_lower_bound(vals) > 0.0:
            verdict = "POSITIVE_SHADOW_CANDIDATE"
        elif mean_eff < -0.05:
            verdict = "NEGATIVE_SHADOW_CONFIRMATION"
        elif mean_eff > 0.0 and p95_adv > 2.0:
            verdict = "EXECUTION_COSTS_INVALIDATE"
        else:
            verdict = "MIXED_UNCERTAIN"
        out.append({
            "symbol": key[0], "side": key[1], "setup": key[2], "regime": key[3], "hour_group": key[4],
            "reject_reason": key[5], "score_gap_band": key[6],
            "sample_count": len(sub), "forward_evaluable_count": n,
            "would_tp_count": sum(1 for r in sub if r.get("first_touch_outcome") == "WOULD_TP"),
            "would_sl_count": sum(1 for r in sub if r.get("first_touch_outcome") == "WOULD_SL"),
            "ambiguous_count": sum(1 for r in sub if r.get("first_touch_outcome") == "WOULD_AMBIGUOUS"),
            "timeout_count": sum(1 for r in sub if r.get("first_touch_outcome") == "WOULD_TIMEOUT"),
            "tp_rate": (sum(1 for r in ev if r.get("first_touch_outcome") == "WOULD_TP") / n if n else 0.0),
            "mean_effective_shadow_r": mean_eff,
            "median_effective_shadow_r": _median(vals),
            "mean_mfe_r": _mean([_safe_float(r.get("mfe_r")) for r in ev]),
            "mean_mae_r": _mean([_safe_float(r.get("mae_r")) for r in ev]),
            "p95_adverse_r": p95_adv,
            "confidence_lower_bound_effective_r": _confidence_lower_bound(vals),
            "min_sample_size": min_n,
            "exploratory_micro_bucket": exploratory,
            "micro_bucket_note": ("Exploratory only; never use for production threshold changes." if exploratory else ""),
            "verdict": verdict,
        })
    return out


def build_reject_overlay_diagnostics(rows: List[Mapping[str, Any]], bucket_rows: List[Mapping[str, Any]]) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    bucket_verdicts = {
        (r["symbol"], r["side"], r["setup"], r["regime"], r["hour_group"], r["reject_reason"], r["score_gap_band"]): r
        for r in bucket_rows
    }
    out: List[Dict[str, Any]] = []
    for row in rows:
        reason = str(row.get("reject_reason") or "").upper()
        side = str(row.get("side") or "").upper()
        setup = str(row.get("setup") or row.get("setup_type") or "").upper()
        hour = _utc_hour_from_row(row)
        labels: list[str] = []
        if side == "LONG" and setup == "BREAKOUT_UP" and hour in {0, 4, 8, 15, 17}:
            labels.append("LONG_BREAKOUT_SESSION_TRAP")
        if reason == "LOW_SCORE" and side == "SHORT" and setup == "BREAKDOWN_DOWN" and hour in {6, 7, 18, 22, 23} and _is_forward_evaluable(row):
            labels.append("SHORT_BREAKDOWN_DIAGNOSTIC_CANDIDATE")
        if reason == "LOW_SCORE" and _low_score_gap_bucket(row) == "near":
            labels.append(f"LOW_SCORE_NEAR_THRESHOLD_{side or 'UNKNOWN'}")
            labels.append("LOW_SCORE_NEAR_THRESHOLD_DIAGNOSTIC_CANDIDATE")
        if reason in {"HIGH_VOL_GUARD", "STOP_TOO_WIDE"} and side == "LONG":
            labels.append("GUARD_CONFIRMED_NO_RESCUE")
        key = (
            str(row.get("symbol") or "UNKNOWN").upper(), side or "UNKNOWN", setup or "UNAVAILABLE",
            str(row.get("regime") or "UNAVAILABLE").upper(), _hour_group(hour), reason or "UNKNOWN", _score_gap_band(row),
        )
        verdict = str(bucket_verdicts.get(key, {}).get("verdict", "REJECT_BUCKET_INSUFFICIENT_SAMPLE"))
        if verdict == "POSITIVE_SHADOW_CANDIDATE":
            labels.append("REJECT_BUCKET_POSITIVE_SHADOW_CANDIDATE")
        elif verdict == "NEGATIVE_SHADOW_CONFIRMATION":
            labels.append("REJECT_BUCKET_NEGATIVE_SHADOW_CONFIRMATION")
        elif verdict == "INSUFFICIENT_SAMPLE":
            labels.append("REJECT_BUCKET_INSUFFICIENT_SAMPLE")
        out.append({**dict(row), "hour_utc": hour if hour is not None else "", "diagnostic_overlay_labels": "|".join(dict.fromkeys(labels)), "bucket_expectancy_verdict": verdict, "diagnostic_only": True, "production_decision_changed": False})
    positives = [r for r in bucket_rows if r.get("verdict") == "POSITIVE_SHADOW_CANDIDATE"]
    negatives = [r for r in bucket_rows if r.get("verdict") == "NEGATIVE_SHADOW_CONFIRMATION"]
    sort_key = lambda r: _safe_float(r.get("mean_effective_shadow_r"))
    summary = {
        "diagnostic_overlay_count": len(out),
        "overlay_label_distribution": _distribution(label for r in out for label in str(r.get("diagnostic_overlay_labels", "")).split("|") if label),
        "strongest_positive_diagnostic_buckets": sorted(positives, key=sort_key, reverse=True)[:10],
        "strongest_negative_confirmation_buckets": sorted(negatives, key=sort_key)[:10],
        "production_threshold_change_recommended": False,
        "recommended_next_action": "Keep production thresholds unchanged. Create diagnostic-only profile for SHORT LOW_SCORE BREAKDOWN candidates in validated good hours. Add safe pre-reject candidate geometry capture for symbol-level rejects. Do not relax HIGH_VOL_GUARD or STOP_TOO_WIDE.",
    }
    return out, summary


def build_low_score_forward_summary(rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    low = [r for r in rows if str(r.get("reject_reason", "")).upper() == "LOW_SCORE"]
    ev = [r for r in low if _is_forward_evaluable(r)]; un = [r for r in low if not _is_forward_evaluable(r)]
    near = [r for r in low if _low_score_gap_bucket(r) == "near"]
    far = [r for r in low if _low_score_gap_bucket(r) == "far"]
    above_unknown = [r for r in low if _low_score_gap_bucket(r) == "above_threshold_or_unknown"]
    def c(sub, out): return sum(1 for r in sub if r.get("first_touch_outcome") == out)
    mean_eff = _mean([_safe_float(r.get("effective_shadow_r_after_costs")) for r in ev])
    near_ev = [r for r in near if _is_forward_evaluable(r)]; far_ev = [r for r in far if _is_forward_evaluable(r)]
    would_subset = [r for r in low if _truthy(r.get("would_accept_if_low_score_disabled"))]
    would_subset_ev = [r for r in would_subset if _is_forward_evaluable(r)]
    verdict = "SHADOW_EVIDENCE_INSUFFICIENT" if len(ev) < max(10, len(low) * 0.1) else ("POSSIBLE_OVERSTRICT_NEAR_THRESHOLD" if near_ev and _mean([_safe_float(r.get("effective_shadow_r_after_costs")) for r in near_ev]) > 0 and c(near_ev,"WOULD_TP") > c(near_ev,"WOULD_SL") else "VALID_QUALITY_FILTER")
    if verdict == "POSSIBLE_OVERSTRICT_NEAR_THRESHOLD": action = "Add diagnostic-only near-threshold LOW_SCORE shadow profile; do not relax production thresholds."
    else: action = "Keep LOW_SCORE threshold; rejected forward diagnostics do not justify production relaxation."
    return {"low_score_count": len(low), "forward_evaluable_count": len(ev), "forward_unavailable_count": len(un), "unavailable_reason_distribution": _distribution([r.get("shadow_unavailable_reason") or r.get("first_touch_outcome") for r in un]), "would_tp_count": c(low,"WOULD_TP"), "would_sl_count": c(low,"WOULD_SL"), "would_timeout_count": c(low,"WOULD_TIMEOUT"), "would_ambiguous_count": c(low,"WOULD_AMBIGUOUS"), "insufficient_forward_bars_count": c(low,"INSUFFICIENT_FORWARD_BARS"), "no_geometry_count": c(low,"NO_TP_SL_GEOMETRY"), "would_tp_rate": c(ev,"WOULD_TP")/len(ev) if ev else 0.0, "would_sl_rate": c(ev,"WOULD_SL")/len(ev) if ev else 0.0, "timeout_rate": c(ev,"WOULD_TIMEOUT")/len(ev) if ev else 0.0, "mean_effective_shadow_r": mean_eff, "median_effective_shadow_r": _median([_safe_float(r.get("effective_shadow_r_after_costs")) for r in ev]), "mean_mfe_r": _mean([_safe_float(r.get("mfe_r")) for r in ev]), "mean_mae_r": _mean([_safe_float(r.get("mae_r")) for r in ev]), "p95_adverse_r": _p95([_safe_float(r.get("mae_r")) for r in ev]), "near_threshold_definition": "0 <= (min_score_threshold - score or score_gap_to_threshold) <= min_score_threshold * 0.05", "near_threshold_count": len(near), "near_threshold_forward_evaluable_count": len(near_ev), "near_threshold_would_tp_count": c(near,"WOULD_TP"), "near_threshold_would_sl_count": c(near,"WOULD_SL"), "near_threshold_mean_effective_shadow_r": _mean([_safe_float(r.get("effective_shadow_r_after_costs")) for r in near_ev]), "far_below_threshold_count": len(far), "far_below_would_tp_count": c(far,"WOULD_TP"), "far_below_would_sl_count": c(far,"WOULD_SL"), "far_below_mean_effective_shadow_r": _mean([_safe_float(r.get("effective_shadow_r_after_costs")) for r in far_ev]), "above_threshold_or_unknown_count": len(above_unknown), "low_score_gap_source_distribution": _distribution([r.get("score_gap_source") or "UNAVAILABLE" for r in low]), "would_accept_if_low_score_disabled_count": len(would_subset), "would_accept_if_low_score_disabled_forward_evaluable_count": len(would_subset_ev), "would_accept_if_low_score_disabled_would_tp_count": c(would_subset,"WOULD_TP"), "would_accept_if_low_score_disabled_would_sl_count": c(would_subset,"WOULD_SL"), "would_accept_if_low_score_disabled_mean_shadow_r": _mean([_safe_float(r.get("effective_shadow_r_after_costs")) for r in would_subset_ev]), "low_score_forward_verdict": verdict, "low_score_forward_evidence": f"{len(ev)} of {len(low)} LOW_SCORE rejects have diagnostic forward outcomes after costs.", "recommended_action": action}


def build_symbol_reject_forward_summary(rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    sym = [r for r in rows if str(r.get("reject_reason", "")).upper() in {"TOO_CHOPPY", "WEAK_TREND_AND_NO_RANGE_EDGE"}]
    ev = [r for r in sym if _is_forward_evaluable(r)]; un = [r for r in sym if not _is_forward_evaluable(r)]
    ch = [r for r in sym if r.get("reject_reason") == "TOO_CHOPPY"]; wk = [r for r in sym if r.get("reject_reason") == "WEAK_TREND_AND_NO_RANGE_EDGE"]
    def c(sub, out): return sum(1 for r in sub if r.get("first_touch_outcome") == out)
    def mean_eff(sub): return _mean([_safe_float(r.get("effective_shadow_r_after_costs")) for r in sub if _is_forward_evaluable(r)])
    missing_metrics = sum(1 for r in sym if (r.get("metric_source") in ("", None, "MISSING") and r.get("chop_score") in ("", None) and r.get("trend_strength") in ("", None) and r.get("range_edge_score") in ("", None)))
    verdict = "SYMBOL_REJECT_NO_GEOMETRY" if sym and not ev and any(r.get("first_touch_outcome") == "SYMBOL_REJECT_NO_CANDIDATE_GEOMETRY" for r in sym) else ("SHADOW_EVIDENCE_INSUFFICIENT" if sym and len(ev) < max(10, len(sym)*0.1) else "VALID_MARKET_STRUCTURE_FILTER")
    return {"symbol_reject_count": len(sym), "forward_evaluable_count": len(ev), "forward_unavailable_count": len(un), "missing_market_structure_metric_count": missing_metrics, "unavailable_reason_distribution": _distribution([r.get("shadow_unavailable_reason") or r.get("first_touch_outcome") for r in un]), "too_choppy_count": len(ch), "too_choppy_forward_evaluable_count": sum(1 for r in ch if _is_forward_evaluable(r)), "too_choppy_would_tp_count": c(ch,"WOULD_TP"), "too_choppy_would_sl_count": c(ch,"WOULD_SL"), "too_choppy_mean_effective_shadow_r": mean_eff(ch), "weak_trend_count": len(wk), "weak_trend_forward_evaluable_count": sum(1 for r in wk if _is_forward_evaluable(r)), "weak_trend_would_tp_count": c(wk,"WOULD_TP"), "weak_trend_would_sl_count": c(wk,"WOULD_SL"), "weak_trend_mean_effective_shadow_r": mean_eff(wk), "mean_chop_score": _mean([_safe_float(r.get("chop_score")) for r in sym if r.get("chop_score") not in (None,"")]), "mean_trend_strength": _mean([_safe_float(r.get("trend_strength")) for r in sym if r.get("trend_strength") not in (None,"")]), "mean_range_edge_score": _mean([_safe_float(r.get("range_edge_score")) for r in sym if r.get("range_edge_score") not in (None,"")]), "mean_mfe_r": _mean([_safe_float(r.get("mfe_r")) for r in ev]), "mean_mae_r": _mean([_safe_float(r.get("mae_r")) for r in ev]), "p95_adverse_r": _p95([_safe_float(r.get("mae_r")) for r in ev]), "symbol_reject_forward_verdict": verdict, "symbol_reject_forward_evidence": f"{len(ev)} of {len(sym)} symbol-level rejects have diagnostic forward outcomes.", "recommended_action": "Keep symbol-level filters; if geometry is unavailable, add safe pre-reject candidate geometry capture before threshold changes."}



def _json_or_pipe_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).upper() for v in value]
    text_value = str(value).strip()
    if not text_value:
        return []
    if text_value.startswith("["):
        try:
            parsed = json.loads(text_value)
            if isinstance(parsed, list):
                return [str(v).upper() for v in parsed]
        except json.JSONDecodeError:
            pass
    return [part.strip().upper() for part in text_value.replace("|", ",").split(",") if part.strip()]


def _distribution_stats(rows: List[Mapping[str, Any]], field: str) -> Dict[str, Any]:
    vals = [_safe_float(r.get(field)) for r in rows if str(r.get(field, "")).strip() != ""]
    return {"count": len(vals), "min": min(vals) if vals else 0.0, "mean": _mean(vals), "median": _median(vals), "p95": _p95(vals), "max": max(vals) if vals else 0.0}


def _strict_numeric_or_none(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        text_value = value.strip()
        if not text_value or "UNAVAILABLE" in text_value.upper() or text_value.upper() in {"UNKNOWN", "NONE", "NULL", "NAN"}:
            return None
        value = text_value
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return None
    return parsed


def _diagnostic_short_low_score_breakdown_row_allowed(row: Mapping[str, Any], symbols: tuple[str, ...]) -> tuple[bool, str]:
    symbol = str(row.get("symbol") or "").upper()
    reason = str(row.get("reject_reason") or "").upper()
    side = str(row.get("side") or "").upper()
    setup = str(row.get("setup") or row.get("setup_type") or "").upper()
    hour_group = _hour_group(_utc_hour_from_row(row))
    if symbol not in symbols:
        return False, "SYMBOL_OUT_OF_SCOPE"
    if side != "SHORT" or setup != "BREAKDOWN_DOWN" or reason != "LOW_SCORE":
        return False, "NOT_SHORT_BREAKDOWN_LOW_SCORE"
    if hour_group != DIAGNOSTIC_PROFILE_GOOD_HOUR_GROUP:
        return False, "UTC_HOUR_OUT_OF_SCOPE"
    failed = set(_json_or_pipe_list(row.get("all_failed_gates")))
    labels = set(_json_or_pipe_list(row.get("diagnostic_overlay_labels")))
    if "HIGH_VOL_GUARD" in failed or "HIGH_VOL_GUARD" in reason or "GUARD_CONFIRMED_NO_RESCUE" in labels:
        return False, "HIGH_VOL_GUARD_ACTIVE"
    if "STOP_TOO_WIDE" in failed or reason == "STOP_TOO_WIDE":
        return False, "STOP_TOO_WIDE_ACTIVE"
    if not _has_tp_sl_geometry(row):
        return False, "INVALID_GEOMETRY"
    required_values = {
        "effective_rr": _strict_numeric_or_none(row.get("effective_rr")),
        "min_effective_rr": _strict_numeric_or_none(row.get("min_effective_rr", row.get("MIN_EFFECTIVE_RR"))),
        "cost_penalty": _strict_numeric_or_none(row.get("cost_penalty")),
        "liquidity_score": _strict_numeric_or_none(row.get("liquidity_score")),
        "spread_pct": _strict_numeric_or_none(row.get("spread_pct")),
        "expected_slippage_pct": _strict_numeric_or_none(row.get("expected_slippage_pct")),
    }
    if any(value is None for value in required_values.values()):
        return False, "EXECUTION_CONTEXT_UNAVAILABLE"
    effective_rr = required_values["effective_rr"]
    min_effective_rr = required_values["min_effective_rr"]
    cost_penalty = required_values["cost_penalty"]
    liquidity_score = required_values["liquidity_score"]
    spread_pct = required_values["spread_pct"]
    expected_slippage_pct = required_values["expected_slippage_pct"]
    if effective_rr <= 0.0 or effective_rr < min_effective_rr:
        return False, "LOW_EFFECTIVE_RR"
    if cost_penalty >= effective_rr:
        return False, "EXECUTION_COST_SANITY"
    if liquidity_score < 0.3:
        return False, "THIN_LIQUIDITY"
    if spread_pct >= 0.0025:
        return False, "HIGH_SPREAD"
    if expected_slippage_pct >= 0.0020:
        return False, "HIGH_SLIPPAGE"
    return True, "DIAGNOSTIC_CANDIDATE"


def build_short_low_score_breakdown_diagnostic_profile(rows: List[Mapping[str, Any]], *, symbols: tuple[str, ...] | None = None) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    scoped_symbols = symbols or diagnostic_short_low_score_symbols_from_env()
    candidates: List[Dict[str, Any]] = []
    blocked: Counter[str] = Counter()
    for row in rows:
        ok, block_reason = _diagnostic_short_low_score_breakdown_row_allowed(row, scoped_symbols)
        if not ok:
            blocked[block_reason] += 1
            continue
        enriched = dict(row)
        enriched.update({
            "diagnostic_profile": DIAGNOSTIC_PROFILE_NAME,
            "diagnostic_only": True,
            "production_decision_changed": False,
            "production_thresholds_unchanged": True,
            "paper_live_effect": "NONE",
            "hour_group": _hour_group(_utc_hour_from_row(row)),
        })
        candidates.append(enriched)
    ev = [r for r in candidates if _is_forward_evaluable(r)]
    values = [_safe_float(r.get("effective_shadow_r_after_costs")) for r in ev]
    outcome = Counter(str(r.get("first_touch_outcome") or "UNKNOWN").upper() for r in candidates)
    summary = {
        "profile": DIAGNOSTIC_PROFILE_NAME,
        "mode": "BACKTEST",
        "diagnostic_only": True,
        "production_thresholds_unchanged": True,
        "paper_live_effect": "NONE",
        "symbol_scope": list(scoped_symbols),
        "hour_group_scope": DIAGNOSTIC_PROFILE_GOOD_HOUR_GROUP,
        "candidate_count": len(candidates),
        "would_tp_count": outcome.get("WOULD_TP", 0),
        "would_sl_count": outcome.get("WOULD_SL", 0),
        "would_timeout_count": outcome.get("WOULD_TIMEOUT", 0),
        "unknown_count": len(candidates) - outcome.get("WOULD_TP", 0) - outcome.get("WOULD_SL", 0) - outcome.get("WOULD_TIMEOUT", 0),
        "mean_effective_shadow_r": _mean(values),
        "median_effective_shadow_r": _median(values),
        "confidence_lower_bound_effective_r": _confidence_lower_bound(values),
        "cost_penalty_distribution": _distribution_stats(candidates, "cost_penalty"),
        "spread_distribution": _distribution_stats(candidates, "spread_pct"),
        "slippage_distribution": _distribution_stats(candidates, "expected_slippage_pct"),
        "liquidity_distribution": _distribution_stats(candidates, "liquidity_score"),
        "symbol_breakdown": _distribution([r.get("symbol") for r in candidates]),
        "hour_breakdown": _distribution([_utc_hour_from_row(r) for r in candidates]),
        "blocked_reason_distribution": dict(blocked),
        "reason_diagnostic_only": DIAGNOSTIC_PROFILE_REASON,
    }
    return candidates, summary

def build_rejected_forward_confirmation_summary(rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for reason, prefix in [("HIGH_VOL_GUARD", "high_vol_guard"), ("STOP_TOO_WIDE", "stop_too_wide")]:
        sub = [r for r in rows if str(r.get("reject_reason","")).upper() == reason]
        ev = [r for r in sub if _is_forward_evaluable(r)]
        out[f"{prefix}_forward_evaluable_count"] = len(ev)
        out[f"{prefix}_would_tp_count"] = sum(1 for r in sub if r.get("first_touch_outcome") == "WOULD_TP")
        out[f"{prefix}_would_sl_count"] = sum(1 for r in sub if r.get("first_touch_outcome") == "WOULD_SL")
        out[f"{prefix}_mean_effective_shadow_r"] = _mean([_safe_float(r.get("effective_shadow_r_after_costs")) for r in ev])
    out["high_vol_forward_confirmation"] = "VALID_PROTECTIVE_GUARD" if out.get("high_vol_guard_would_sl_count",0) >= out.get("high_vol_guard_would_tp_count",0) else "DIAGNOSTIC_REVIEW_REQUIRED"
    out["stop_too_wide_forward_confirmation"] = "KEEP_GUARD_DIAGNOSTIC_ONLY" if out.get("stop_too_wide_would_sl_count",0) >= out.get("stop_too_wide_would_tp_count",0) else "DIAGNOSTIC_REVIEW_REQUIRED"
    return out

def _is_actionable_rejected_order(row: Mapping[str, Any]) -> bool:
    if row.get("setup_reason") == "SYMBOL_SELECTOR":
        return False
    if row.get("event_flags") == "SYMBOL_SELECTOR":
        return False
    if row.get("side") in {"N/A", "", None}:
        return False
    for key in ("entry", "sl", "tp"):
        if key not in row:
            return False
        if _safe_float(row.get(key), 0.0) <= 0.0:
            return False
    return True
def evaluate_rejected_shadow(
    candidate_row: Mapping[str, Any],
    candles: List[Candle],
    idx: int,
) -> RejectedShadowEvaluation:
    rr = _safe_float(candidate_row.get("rr"), 0.0)
    spread_pct, _ = normalize_pct_input(candidate_row.get("spread_pct"), field="spread_pct")
    liquidity_score = _safe_float(candidate_row.get("liquidity_score"), 1.0)
    volatility_score = _safe_float(candidate_row.get("volatility_score"), spread_pct)
    effective_rr, _, penalty_breakdown = _execution_reject_flags(
        rr,
        {
            "spread_pct": spread_pct,
            "expected_slippage_pct": _safe_float(candidate_row.get("expected_slippage_pct"), 0.0),
            "liquidity_score": liquidity_score,
        },
    )
    counterfactual = simulate_rejected_counterfactual(
        CandidateOrder(
            timestamp=int(candidate_row.get("timestamp", 0)),
            symbol=str(candidate_row.get("symbol", "")),
            side=str(candidate_row.get("side", "LONG")),
            entry=_safe_float(candidate_row.get("entry"), 0.0),
            sl=_safe_float(candidate_row.get("sl"), 0.0),
            tp=_safe_float(candidate_row.get("tp"), 0.0),
            rr=rr,
            setup_type=str(candidate_row.get("setup_type", "")),
            setup_reason=str(candidate_row.get("setup_reason", "")),
            regime=str(candidate_row.get("regime", "")),
            score=_safe_float(candidate_row.get("score"), 0.0),
            order_type=str(candidate_row.get("order_type", "LIMIT")),
        ),
        candles,
        idx,
    )
    liquidity_ok = liquidity_score >= 0.3
    volatility_ok = volatility_score <= 5.0
    cost_penalty = _safe_float(penalty_breakdown.get("cost_penalty_total"), max(rr - effective_rr, 0.0))
    effective_tp_hit = (
        counterfactual["outcome"] == "WOULD_TP"
        and effective_rr >= 1.1
        and liquidity_ok
        and volatility_ok
    )
    low_score_gate_score = _safe_float(candidate_row.get("gate_score"), _safe_float(candidate_row.get("score"), 0.0))
    min_required_score = _safe_float(candidate_row.get("min_required_score"), 7.5)
    rescue_attempted = False
    rescue_passed = False
    rescued_stop_loss = _safe_float(candidate_row.get("sl"), 0.0)
    rescued_effective_rr = 0.0
    rescued_size_multiplier = 0.0
    rescue_reject_reason = ""
    if str(candidate_row.get("reject_reason", "")).upper() == "STOP_TOO_WIDE":
        setup_type = str(candidate_row.get("setup_type", "")).upper()
        regime = str(candidate_row.get("regime", "")).upper()
        score_val = _safe_float(candidate_row.get("score"), 0.0)
        spread_ok = spread_pct <= 0.05
        if ("BREAKOUT" in setup_type or regime == "BREAKOUT") and score_val >= 8.0 and effective_rr >= 1.2 and liquidity_ok and spread_ok:
            rescue_attempted = True
            entry = _safe_float(candidate_row.get("entry"), 0.0)
            sl = _safe_float(candidate_row.get("sl"), 0.0)
            risk = abs(entry - sl)
            max_risk = entry * 0.015
            reduced_risk = min(risk, max_risk)
            if risk > 0.0 and reduced_risk > 0.0:
                rescued_stop_loss = (entry - reduced_risk) if str(candidate_row.get("side", "LONG")).upper() == "LONG" else (entry + reduced_risk)
                rescued_rr = abs(_safe_float(candidate_row.get("tp"), entry) - entry) / reduced_risk
                rescued_effective_rr, _, _ = _execution_reject_flags(rescued_rr, {"spread_pct": spread_pct, "expected_slippage_pct": _safe_float(candidate_row.get("expected_slippage_pct"), 0.0), "liquidity_score": liquidity_score})
                stop_width_pct = (reduced_risk / max(entry, 1e-9)) * 100.0
                rescued_size_multiplier = min(0.5, max_risk / max(risk, 1e-9))
                if rescued_effective_rr >= 1.2 and stop_width_pct <= 1.5 and rescued_size_multiplier < 1.0:
                    rescue_passed = True
                else:
                    rescue_reject_reason = "LOW_RESCUED_EFFECTIVE_RR" if rescued_effective_rr < 1.2 else "RESCUED_STOP_STILL_WIDE"
            else:
                rescue_reject_reason = "INVALID_RESCUE_GEOMETRY"

    return RejectedShadowEvaluation(
        symbol=str(candidate_row.get("symbol", "")),
        timestamp=int(candidate_row.get("timestamp", 0)),
        side=str(candidate_row.get("side", "LONG")),
        entry=_safe_float(candidate_row.get("entry"), 0.0),
        stop_loss=_safe_float(candidate_row.get("sl"), 0.0),
        take_profit=_safe_float(candidate_row.get("tp"), 0.0),
        raw_rr=rr,
        effective_rr=effective_rr,
        reject_reasons=str(candidate_row.get("reject_reason", "UNKNOWN")),
        score=_safe_float(candidate_row.get("score"), 0.0),
        regime=str(candidate_row.get("regime", "UNKNOWN")),
        spread_pct=spread_pct,
        liquidity_score=liquidity_score,
        volatility_score=volatility_score,
        shadow_outcome=counterfactual["outcome"],
        effective_tp_hit=effective_tp_hit,
        cost_penalty=cost_penalty,
        liquidity_ok=liquidity_ok,
        volatility_ok=volatility_ok,
        low_score_gate_score=low_score_gate_score,
        rescue_attempted=rescue_attempted,
        rescue_passed=rescue_passed,
        rescued_stop_loss=rescued_stop_loss,
        rescued_effective_rr=rescued_effective_rr,
        rescued_size_multiplier=rescued_size_multiplier,
        rescue_reject_reason=rescue_reject_reason,
        setup_type=str(candidate_row.get("setup_type") or "UNAVAILABLE"),
        expected_slippage_pct=(candidate_row.get("expected_slippage_pct") if candidate_row.get("expected_slippage_pct") not in (None, "") else "UNAVAILABLE"),
        stop_distance_pct=(abs(_safe_float(candidate_row.get("entry"), 0.0) - _safe_float(candidate_row.get("sl"), 0.0)) / max(_safe_float(candidate_row.get("entry"), 0.0), 1e-9) * 100.0 if _safe_float(candidate_row.get("entry"), 0.0) > 0.0 and _safe_float(candidate_row.get("sl"), 0.0) > 0.0 else "UNAVAILABLE"),
    )

def _quality_float(value: Any) -> Optional[float]:
    if value in (None, "", "None", "null", "UNAVAILABLE", "UNAVAILABLE_BACKTEST"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _decile(value: Any) -> str:
    numeric = _quality_float(value)
    if numeric is None:
        return "UNAVAILABLE"
    return f"D{min(10, max(1, int(numeric * 10) + 1))}" if 0.0 <= numeric <= 1.0 else f"D{min(10, max(1, int(numeric) + 1))}"

def _bucket_quality(value: Any, cuts: Iterable[float], labels: List[str]) -> str:
    numeric = _quality_float(value)
    if numeric is None:
        return "UNAVAILABLE"
    for cut, label in zip(cuts, labels):
        if numeric <= cut:
            return label
    return labels[-1]

def _outcome_from_lifecycle(row: LifecycleRow) -> str:
    reason = str(row.close_reason or row.status_after or "").upper()
    if reason == "TP_HIT" or row.would_tp_hit:
        return "WOULD_TP"
    if reason == "SL_HIT" or row.would_sl_hit:
        return "WOULD_SL"
    if reason in {"OPEN_AT_END", "TIMEOUT"}:
        return "WOULD_TIMEOUT"
    return "UNKNOWN"

def _quality_record_from_lifecycle(row: LifecycleRow, timeframe: str) -> Dict[str, Any]:
    stop_distance = "UNAVAILABLE"
    if row.entry > 0 and row.sl > 0:
        stop_distance = abs(row.entry - row.sl) / row.entry * 100.0
    return {"source": "ACCEPTED", "reject_reason": "ACCEPTED", "symbol": row.symbol or "UNKNOWN", "side": row.side or "UNKNOWN", "regime": row.regime or "UNKNOWN", "setup_type": row.setup_type or "UNAVAILABLE", "timeframe": timeframe, "score": row.score, "raw_rr": row.rr, "effective_rr": row.effective_rr if row.effective_rr is not None else row.rr, "decision_cost_penalty": row.cost_penalty, "shadow_cost_penalty": "UNAVAILABLE", "spread_pct": row.spread_pct, "expected_slippage_pct": row.expected_slippage_pct, "volatility_score": row.volatility_score, "liquidity_score": row.liquidity_score, "stop_distance_pct": stop_distance, "outcome": _outcome_from_lifecycle(row)}

def _quality_record_from_shadow(s: RejectedShadowEvaluation, timeframe: str) -> Dict[str, Any]:
    return {"source": "REJECTED_SHADOW", "timestamp": s.timestamp, "reject_reason": s.reject_reasons or "UNKNOWN", "symbol": s.symbol or "UNKNOWN", "side": s.side or "UNKNOWN", "regime": s.regime or "UNKNOWN", "setup_type": s.setup_type or "UNAVAILABLE", "timeframe": timeframe, "score": s.score, "raw_rr": s.raw_rr, "effective_rr": s.effective_rr, "decision_cost_penalty": "UNAVAILABLE", "shadow_cost_penalty": s.cost_penalty, "spread_pct": s.spread_pct, "expected_slippage_pct": s.expected_slippage_pct, "volatility_score": s.volatility_score, "liquidity_score": s.liquidity_score, "liquidity_ok": s.liquidity_ok, "volatility_ok": s.volatility_ok, "stop_distance_pct": s.stop_distance_pct, "effective_tp_hit": s.effective_tp_hit, "outcome": s.shadow_outcome or "UNKNOWN"}



def _rank_values(values: List[float]) -> List[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j + 2) / 2.0
        for k in range(i, j + 1):
            ranks[indexed[k][0]] = avg_rank
        i = j + 1
    return ranks

def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mx = sum(xs) / len(xs); my = sum(ys) / len(ys)
    dx = [x - mx for x in xs]; dy = [y - my for y in ys]
    denx = sum(x * x for x in dx) ** 0.5; deny = sum(y * y for y in dy) ** 0.5
    if denx == 0.0 or deny == 0.0:
        return None
    return sum(x * y for x, y in zip(dx, dy)) / (denx * deny)

def _spearman(xs: List[float], ys: List[float]) -> Optional[float]:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    return _pearson(_rank_values(xs), _rank_values(ys))

def _score_bucket_label(score: Any) -> str:
    value = _quality_float(score)
    if value is None:
        return "UNAVAILABLE"
    return "10" if value >= 10.0 else f"{max(0, min(9, int(value)))}-{max(1, min(10, int(value) + 1))}"

def _calibrated_score_components(row: Mapping[str, Any]) -> Dict[str, float]:
    score = _quality_float(row.get("score")) or 0.0
    raw_rr = _quality_float(row.get("raw_rr")) or _quality_float(row.get("rr")) or 0.0
    effective_rr = _quality_float(row.get("effective_rr")) or 0.0
    cost_penalty = _quality_float(row.get("shadow_cost_penalty"))
    if cost_penalty is None:
        cost_penalty = _quality_float(row.get("decision_cost_penalty")) or _quality_float(row.get("cost_penalty")) or max(raw_rr - effective_rr, 0.0)
    stop_distance = _quality_float(row.get("stop_distance_pct")) or 0.0
    volatility = _quality_float(row.get("volatility_score")) or _quality_float(row.get("volatility_pct")) or 0.0
    spread = _quality_float(row.get("spread_pct")) or 0.0
    slippage = _quality_float(row.get("expected_slippage_pct")) or 0.0
    rr_quality_bonus = max(-1.0, min(1.0, (effective_rr - 1.7) * 0.35))
    cost_component = min(2.0, cost_penalty * 2.0)
    stop_component = max(0.0, stop_distance - 1.5) * 0.45
    volatility_component = max(0.0, volatility - 2.5) * 0.25
    overextension_component = max(0.0, stop_distance - 2.0) * max(0.0, volatility - 2.0) * 0.10
    late_breakout_component = 0.35 if str(row.get("setup_type", "")).upper().find("BREAKOUT") >= 0 and volatility > 3.0 and stop_distance > 1.5 else 0.0
    execution_component = min(1.0, spread * 10.0 + slippage * 8.0)
    total_penalty = cost_component + stop_component + volatility_component + overextension_component + late_breakout_component + execution_component
    calibrated = max(0.0, min(10.0, score + rr_quality_bonus - total_penalty))
    return {
        "effective_rr_quality_component": round(rr_quality_bonus, 6),
        "execution_cost_penalty_component": round(cost_component, 6),
        "stop_distance_quality_penalty": round(stop_component, 6),
        "volatility_exhaustion_penalty": round(volatility_component, 6),
        "overextension_penalty": round(overextension_component, 6),
        "late_breakout_entry_penalty": round(late_breakout_component, 6),
        "spread_slippage_penalty": round(execution_component, 6),
        "total_calibration_penalty": round(total_penalty, 6),
        "calibrated_score": round(calibrated, 6),
        "calibrated_score_delta": round(calibrated - score, 6),
    }

def build_score_calibration_artifacts(records: List[Mapping[str, Any]]) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows = [dict(r) for r in records if _quality_float(r.get("score")) is not None]
    for r in rows:
        comps = _calibrated_score_components(r)
        r.update({"score_bucket": _score_bucket_label(r.get("score")), **comps, "calibrated_score_components": json.dumps(comps, sort_keys=True)})
        r["score_calibration_verdict"] = "PENALIZED_EXECUTION_RISK" if comps["calibrated_score_delta"] < -0.5 else "UNCHANGED_OR_LOW_RISK"
    def metric_rows(group_field: str) -> List[Dict[str, Any]]:
        out=[]
        for key in sorted({str(r.get(group_field) or "UNAVAILABLE") for r in rows}):
            bucket=[r for r in rows if str(r.get(group_field) or "UNAVAILABLE") == key]
            n=len(bucket)
            def avg(field):
                vals=[_quality_float(x.get(field)) for x in bucket]; vals=[v for v in vals if v is not None]
                return round(sum(vals)/len(vals), 8) if vals else None
            tp=sum(1 for x in bucket if x.get("outcome") == "WOULD_TP"); sl=sum(1 for x in bucket if x.get("outcome") == "WOULD_SL"); eff=sum(1 for x in bucket if bool(x.get("effective_tp_hit")) or str(x.get("effective_tp_hit")).lower()=="true")
            out.append({"breakdown": group_field, group_field: key, "count": n, "would_tp_count": tp, "would_tp_rate": tp/n if n else 0.0, "would_sl_count": sl, "would_sl_rate": sl/n if n else 0.0, "effective_tp_hit_count": eff, "effective_tp_hit_rate": eff/n if n else 0.0, "avg_raw_rr": avg("raw_rr"), "avg_effective_rr": avg("effective_rr"), "avg_cost_penalty": avg("shadow_cost_penalty"), "avg_stop_distance_pct": avg("stop_distance_pct"), "avg_volatility_score": avg("volatility_score"), "avg_spread_pct": avg("spread_pct"), "avg_expected_slippage_pct": avg("expected_slippage_pct"), "avg_calibrated_score": avg("calibrated_score")})
        return out
    diagnostics = metric_rows("score_bucket") + metric_rows("reject_reason") + metric_rows("regime") + metric_rows("setup_type")
    xs=[_quality_float(r.get("score")) for r in rows]
    raw_y=[1.0 if r.get("outcome") == "WOULD_TP" else 0.0 for r in rows]
    eff_y=[1.0 if (bool(r.get("effective_tp_hit")) or str(r.get("effective_tp_hit")).lower()=="true") else 0.0 for r in rows]
    xs=[x for x in xs if x is not None]
    bucket_rows=[r for r in diagnostics if r.get("breakdown") == "score_bucket" and r.get("score_bucket") != "UNAVAILABLE"]
    bucket_rates=[_quality_float(r.get("would_tp_rate")) or 0.0 for r in bucket_rows]
    monotonic_violations=sum(1 for a,b in zip(bucket_rates, bucket_rates[1:]) if b + 1e-9 < a)
    flags=[]
    high_score=[r for r in rows if (_quality_float(r.get("score")) or 0.0) >= 8.0]
    if high_score and sum(1 for r in high_score if r.get("outcome")=="WOULD_TP") / len(high_score) < 0.45: flags.append("HIGH_SCORE_LOW_TP_RATE")
    for reason in ["HIGH_VOL_GUARD", "STOP_TOO_WIDE"]:
        cluster=[r for r in high_score if str(r.get("reject_reason","")).upper()==reason]
        if cluster and sum(1 for r in cluster if r.get("outcome")=="WOULD_SL") > sum(1 for r in cluster if r.get("outcome")=="WOULD_TP"):
            flags.append("HIGH_VOL_HIGH_SCORE_SL_CLUSTER" if reason=="HIGH_VOL_GUARD" else "STOP_TOO_WIDE_HIGH_SCORE_SL_CLUSTER")
    if monotonic_violations: flags.append("SCORE_NOT_MONOTONIC")
    pear_raw=_pearson(xs, raw_y) if len(xs)==len(raw_y) else None; pear_eff=_pearson(xs, eff_y) if len(xs)==len(eff_y) else None
    if pear_raw is not None and pear_eff is not None and pear_eff > pear_raw + 0.1: flags.append("SCORE_PREDICTS_EFFECTIVE_TP_BETTER_THAN_RAW_TP")
    if pear_raw is not None and pear_raw < 0: flags.append("SCORE_INVERSION")
    if any((_quality_float(r.get("score")) or 0) >= 8 and (_quality_float(r.get("stop_distance_pct")) or 0) > 2 and (_quality_float(r.get("volatility_score")) or 0) > 3 and r.get("outcome")=="WOULD_SL" for r in rows): flags.append("OVEREXTENSION_NOT_PENALIZED")
    summary={"total_rows": len(rows), "score_source_interpretation": "BACKTEST score mixes breakout/range strength, raw RR expectancy, and execution context; PAPER/LIVE AIBrain score is probabilistic quality after costs. Diagnostics test both raw WOULD_TP and effective_tp_hit.", "pearson_score_would_tp": pear_raw, "spearman_score_would_tp": _spearman(xs, raw_y) if len(xs)==len(raw_y) else None, "pearson_score_effective_tp_hit": pear_eff, "spearman_score_effective_tp_hit": _spearman(xs, eff_y) if len(xs)==len(eff_y) else None, "monotonicity": {"score_bucket_count": len(bucket_rows), "violations": monotonic_violations, "generally_improves_with_score": monotonic_violations == 0}, "miscalibration_flags": sorted(set(flags)), "thresholds_changed": False, "acceptance_logic_changed": False, "calibrated_score_scope": "BACKTEST_DIAGNOSTIC_ONLY", "calibrated_score_future_leakage": "NO_FORWARD_OUTCOME_FIELDS_USED; row-local pre-decision score/RR/execution/volatility/stop-distance fields only"}
    return diagnostics, summary

def _quality_gate_metrics(records: List[Dict[str, Any]], cfg: QualityGateConfig, baseline_net_pnl: float = 0.0) -> Dict[str, Any]:
    rejected = [r for r in records if r.get("source") == "REJECTED_SHADOW"]
    mode_ok = "BACKTEST" in {m.upper() for m in cfg.modes}
    candidates: List[Dict[str, Any]] = []
    rejected_reasons: Dict[str, int] = {}
    daily_counts: Dict[str, int] = {}
    for r in rejected:
        if "stop_distance_pct_bucket" not in r:
            r["stop_distance_pct_bucket"] = _bucket_quality(r.get("stop_distance_pct"), [0.75, 1.5], ["TIGHT", "NORMAL", "WIDE"])
        reason = str(r.get("reject_reason", "")).upper()
        eff = _quality_float(r.get("effective_rr"))
        score = _quality_float(r.get("score"))
        spread = _quality_float(r.get("spread_pct"))
        slip = _quality_float(r.get("expected_slippage_pct"))
        stop_bucket = str(r.get("stop_distance_pct_bucket") or "").upper()
        blocked = (
            not cfg.enabled or not mode_ok or cfg.allowed_gate_name != QUALITY_GATE_NAME
            or str(r.get("side", "")).upper() != "SHORT"
            or str(r.get("setup_type", "")).upper() != "BREAKDOWN_DOWN"
            or str(r.get("regime", "")).upper() != "BREAKOUT"
            or stop_bucket != "NORMAL"
            or reason not in set(cfg.allowed_reasons)
            or reason == "REGIME_MISMATCH"
            or str(r.get("regime", "")).upper() in {"PANIC", "NEWS_DRIVEN"}
            or r.get("liquidity_ok") is False
            or r.get("volatility_ok") is False
            or str(r.get("outcome") or "").upper() != "WOULD_TP"
            or eff is None or eff < cfg.min_effective_rr
            or (cfg.min_score is not None and (score is None or score < cfg.min_score))
            or spread is None or spread > cfg.max_spread_pct
            or slip is None or slip > cfg.max_slippage_pct
        )
        if blocked:
            if cfg.enabled:
                rejected_reasons[reason or "NOT_ELIGIBLE"] = rejected_reasons.get(reason or "NOT_ELIGIBLE", 0) + 1
            continue
        day_key = str(r.get("timestamp") or "BACKTEST_DAY")
        if daily_counts.get(day_key, 0) >= cfg.max_trades_per_day:
            rejected_reasons["DAILY_QUALITY_GATE_LIMIT"] = rejected_reasons.get("DAILY_QUALITY_GATE_LIMIT", 0) + 1
            continue
        daily_counts[day_key] = daily_counts.get(day_key, 0) + 1
        candidates.append(r)
    tp = sum(1 for r in candidates if r.get("outcome") == "WOULD_TP")
    sl = sum(1 for r in candidates if r.get("outcome") == "WOULD_SL")
    unknown = sum(1 for r in candidates if r.get("outcome") not in {"WOULD_TP", "WOULD_SL"})
    eff_vals = [_quality_float(r.get("effective_rr")) for r in candidates]
    eff_vals = [v for v in eff_vals if v is not None]
    expected = (sum((( _quality_float(r.get("effective_rr")) or 0.0) if r.get("outcome") == "WOULD_TP" else (-1.0 if r.get("outcome") == "WOULD_SL" else 0.0)) for r in candidates) / len(candidates)) if candidates else 0.0
    sized_expected = expected * cfg.size_multiplier * len(candidates)
    return {
        "quality_gate_enabled": cfg.enabled,
        "quality_gate_mode": ",".join(cfg.modes),
        "quality_gate_candidate_count": len(candidates),
        "quality_gate_accepted_count": len(candidates),
        "quality_gate_rejected_count": sum(rejected_reasons.values()),
        "quality_gate_would_tp_count": tp,
        "quality_gate_would_sl_count": sl,
        "quality_gate_unknown_count": unknown,
        "quality_gate_tp_rate": tp / (tp + sl) if (tp + sl) else 0.0,
        "quality_gate_mean_effective_rr": (sum(eff_vals) / len(eff_vals)) if eff_vals else 0.0,
        "quality_gate_expected_effective_expectancy": expected,
        "baseline_plus_quality_gate_net_pnl": baseline_net_pnl + sized_expected,
        "quality_gate_size_multiplier": cfg.size_multiplier,
        "quality_gate_reason_breakdown": _distribution([r.get("reject_reason") for r in candidates]),
        "quality_gate_symbol_breakdown": _distribution([r.get("symbol") for r in candidates]),
        "quality_gate_daily_trade_count_distribution": _distribution(daily_counts.values()),
        "quality_gate_rejected_reason_breakdown": rejected_reasons,
    }

def build_signal_quality_diagnostics(accepted_rows: List[LifecycleRow], shadows: List[RejectedShadowEvaluation], timeframe: str, thresholds: List[float] | None = None, quality_gate_config: Optional[QualityGateConfig] = None, baseline_net_pnl: float = 0.0) -> tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    thresholds = thresholds or [1.7, 1.9, 2.1, 2.3]
    records = [_quality_record_from_lifecycle(r, timeframe) for r in accepted_rows] + [_quality_record_from_shadow(s, timeframe) for s in shadows]
    for r in records:
        r["score_decile"] = _decile(r.get("score"))
        r["effective_rr_decile"] = _decile(r.get("effective_rr"))
        r["volatility_score_bucket"] = _bucket_quality(r.get("volatility_score"), [0.3, 0.7], ["LOW", "MEDIUM", "HIGH"])
        r["liquidity_score_bucket"] = _bucket_quality(r.get("liquidity_score"), [0.3, 0.7], ["THIN", "NORMAL", "DEEP"])
        r["spread_pct_bucket"] = _bucket_quality(r.get("spread_pct"), [0.03, 0.08], ["TIGHT", "NORMAL", "WIDE"])
        r["expected_slippage_pct_bucket"] = _bucket_quality(r.get("expected_slippage_pct"), [0.01, 0.05], ["LOW", "MEDIUM", "HIGH"])
        r["stop_distance_pct_bucket"] = _bucket_quality(r.get("stop_distance_pct"), [0.75, 1.5], ["TIGHT", "NORMAL", "WIDE"])
    dims = ["reject_reason", "symbol", "side", "regime", "setup_type", "timeframe", "score_decile", "effective_rr_decile", "volatility_score_bucket", "liquidity_score_bucket", "spread_pct_bucket", "expected_slippage_pct_bucket", "stop_distance_pct_bucket"]
    group_rows=[]
    for dim in dims:
        keys=sorted({str(r.get(dim) or "UNAVAILABLE") for r in records})
        for key in keys:
            rows=[r for r in records if str(r.get(dim) or "UNAVAILABLE")==key]
            n=len(rows); outcomes= {o: sum(1 for r in rows if r.get("outcome")==o) for o in ["WOULD_TP","WOULD_SL","WOULD_TIMEOUT","UNKNOWN"]}
            def mean(f):
                vals=[_quality_float(r.get(f)) for r in rows]; vals=[v for v in vals if v is not None]
                return (sum(vals)/len(vals)) if vals else None
            group_rows.append({"group_field":dim,"group_value":key,"count":n, **{f"{o.lower()}_count":c for o,c in outcomes.items()}, **{f"{o.lower()}_rate":(c/n if n else 0.0) for o,c in outcomes.items()}, "mean_score":mean("score"), "mean_raw_rr":mean("raw_rr"), "mean_effective_rr":mean("effective_rr"), "mean_decision_cost_penalty":mean("decision_cost_penalty"), "mean_shadow_cost_penalty":mean("shadow_cost_penalty"), "mean_spread_pct":mean("spread_pct"), "mean_expected_slippage_pct":mean("expected_slippage_pct")})
    def split(rows): return {"count":len(rows), "would_tp_count":sum(1 for r in rows if r.get("outcome")=="WOULD_TP"), "would_sl_count":sum(1 for r in rows if r.get("outcome")=="WOULD_SL"), "would_timeout_count":sum(1 for r in rows if r.get("outcome")=="WOULD_TIMEOUT"), "unknown_count":sum(1 for r in rows if r.get("outcome") not in {"WOULD_TP","WOULD_SL","WOULD_TIMEOUT"})}
    def mean(rows, f):
        vals=[_quality_float(r.get(f)) for r in rows]; vals=[v for v in vals if v is not None]
        return (sum(vals)/len(vals)) if vals else None
    def outcome_metrics(rows):
        n=len(rows); base=split(rows)
        return {**base, "would_tp_rate":base["would_tp_count"]/n if n else 0.0, "would_sl_rate":base["would_sl_count"]/n if n else 0.0, "would_timeout_rate":base["would_timeout_count"]/n if n else 0.0, "unknown_rate":base["unknown_count"]/n if n else 0.0}
    combo_rows=[]
    combo_dims=[("side","regime"),("side","setup_type"),("regime","setup_type"),("side","regime","setup_type"),("side","regime","setup_type","stop_distance_pct_bucket"),("side","regime","setup_type","effective_rr_threshold_bucket")]
    for r in records:
        eff=_quality_float(r.get("effective_rr"))
        r["effective_rr_threshold_bucket"] = "UNAVAILABLE" if eff is None else (">=2.3" if eff>=2.3 else ">=2.1" if eff>=2.1 else ">=1.9" if eff>=1.9 else ">=1.7" if eff>=1.7 else "<1.7")
    for dims_tuple in combo_dims:
        keys=sorted({tuple(str(r.get(d) or "UNAVAILABLE") for d in dims_tuple) for r in records})
        for key in keys:
            rows=[r for r in records if tuple(str(r.get(d) or "UNAVAILABLE") for d in dims_tuple)==key]
            metrics=outcome_metrics(rows)
            combo_rows.append({"grouping":"+".join(dims_tuple), **{d:v for d,v in zip(dims_tuple,key)}, "count":len(rows), **metrics, "mean_score":mean(rows,"score"), "mean_raw_rr":mean(rows,"raw_rr"), "mean_effective_rr":mean(rows,"effective_rr"), "mean_shadow_cost_penalty":mean(rows,"shadow_cost_penalty"), "mean_spread_pct":mean(rows,"spread_pct"), "mean_expected_slippage_pct":mean(rows,"expected_slippage_pct"), "mean_stop_distance_pct":mean(rows,"stop_distance_pct")})
    rejected=[r for r in records if r.get("source")=="REJECTED_SHADOW"]
    gate_defs={
        QUALITY_GATE_NAME: lambda r: r.get("side")=="SHORT" and r.get("setup_type")=="BREAKDOWN_DOWN" and r.get("regime")=="BREAKOUT" and r.get("stop_distance_pct_bucket")=="NORMAL",
        "SHORT_BREAKDOWN_BREAKOUT_GATE": lambda r: r.get("side")=="SHORT" and r.get("setup_type")=="BREAKDOWN_DOWN" and r.get("regime")=="BREAKOUT",
        "LONG_BREAKOUT_STRICT_GATE": lambda r: r.get("side")=="LONG" and r.get("setup_type")=="BREAKOUT_UP" and (_quality_float(r.get("effective_rr")) or 0)>=1.9 and (_quality_float(r.get("stop_distance_pct")) or 999)<=1.5,
        "HIGH_EFFECTIVE_RR_SHORT_GATE": lambda r: r.get("side")=="SHORT" and (_quality_float(r.get("effective_rr")) or 0)>=1.9,
        "STOP_TOO_WIDE_RECOVERABLE_GATE": lambda r: str(r.get("reject_reason")).upper()=="STOP_TOO_WIDE" and (_quality_float(r.get("effective_rr")) or 0)>=1.7 and (_quality_float(r.get("stop_distance_pct")) or 999)<=3.0,
    }
    gate_allowed={QUALITY_GATE_NAME:["LOW_SCORE","STOP_TOO_WIDE","DAILY_SYMBOL_TRADE_LIMIT"],"SHORT_BREAKDOWN_BREAKOUT_GATE":["LOW_SCORE","STOP_TOO_WIDE","DAILY_SYMBOL_TRADE_LIMIT","REGIME_MISMATCH"],"LONG_BREAKOUT_STRICT_GATE":["LOW_SCORE","STOP_TOO_WIDE"],"HIGH_EFFECTIVE_RR_SHORT_GATE":["LOW_SCORE","STOP_TOO_WIDE","DAILY_SYMBOL_TRADE_LIMIT"],"STOP_TOO_WIDE_RECOVERABLE_GATE":["STOP_TOO_WIDE"]}
    gate_rows=[]
    for name, pred in gate_defs.items():
        rows=[r for r in rejected if pred(r)]
        metrics=outcome_metrics(rows)
        gate_rows.append({"gate_name":name,"reporting_only":True,"candidate_count":len(rows),**{k:v for k,v in metrics.items() if k!="count"},"mean_effective_rr":mean(rows,"effective_rr"),"expected_effective_expectancy":(sum(((_quality_float(r.get("effective_rr")) or 0.0) if r.get("outcome")=="WOULD_TP" else (-1.0 if r.get("outcome")=="WOULD_SL" else 0.0)) for r in rows)/len(rows) if rows else 0.0),"allowed_reject_reasons":"|".join(gate_allowed[name]),"rejected_by_reason":json.dumps(_distribution([r.get("reject_reason") for r in rows]), sort_keys=True)})
    calibration_rows=[]
    for dims_tuple in [("score_decile","side"),("score_decile","regime"),("score_decile","setup_type")]:
        keys=sorted({tuple(str(r.get(d) or "UNAVAILABLE") for d in dims_tuple) for r in records})
        for key in keys:
            rows=[r for r in records if tuple(str(r.get(d) or "UNAVAILABLE") for d in dims_tuple)==key]
            calibration_rows.append({"diagnostic":"by_"+dims_tuple[1], **{d:v for d,v in zip(dims_tuple,key)}, **outcome_metrics(rows)})
    d10=[r for r in records if r.get("score_decile")=="D10"]
    for dim in ["reject_reason","stop_distance_pct_bucket"]:
        for key in sorted({str(r.get(dim) or "UNAVAILABLE") for r in d10}):
            rows=[r for r in d10 if str(r.get(dim) or "UNAVAILABLE")==key]
            calibration_rows.append({"diagnostic":"d10_by_"+dim, dim:key, **outcome_metrics(rows)})
    score10=[r for r in records if _quality_float(r.get("score")) == 10.0]
    stop=[r for r in records if str(r.get("reject_reason")).upper()=="STOP_TOO_WIDE"]
    missed=[]
    for t in thresholds:
        rows=[r for r in rejected if (_quality_float(r.get("effective_rr")) or -1) >= t]
        missed.append({"effective_rr_threshold":t, **split(rows)})
    raw_tp_candidates = sorted([r for r in rejected if r.get("outcome")=="WOULD_TP"], key=lambda r:((_quality_float(r.get("effective_rr")) or 0), (_quality_float(r.get("score")) or 0)), reverse=True)
    positive_candidates = []
    for r in raw_tp_candidates:
        peer = [x for x in rejected if x.get("reject_reason") == r.get("reject_reason")]
        if peer and outcome_metrics(peer)["would_sl_rate"] > outcome_metrics(peer)["would_tp_rate"]:
            continue
        positive_candidates.append(r)
    score_calibration_detail_rows, score_calibration_summary = build_score_calibration_artifacts(records)
    calibration_rows.extend(score_calibration_detail_rows)
    accepted_score10=[r for r in score10 if r.get("source")=="ACCEPTED"]
    accepted_score10_split=split(accepted_score10)
    summary={"total_records":len(records), "accepted_records":sum(1 for r in records if r.get("source")=="ACCEPTED"), "rejected_shadow_records":len(shadows), "score_saturation":{"score_10_count":len(score10), "score_10_rate":len(score10)/len(records) if records else 0.0, "score_10_would_tp_count":sum(1 for r in score10 if r.get("outcome")=="WOULD_TP"), "score_10_would_sl_count":sum(1 for r in score10 if r.get("outcome")=="WOULD_SL"), "accepted_score_10_count":len(accepted_score10), "accepted_score_10_would_tp_count":accepted_score10_split["would_tp_count"], "accepted_score_10_would_sl_count":accepted_score10_split["would_sl_count"], "accepted_score_10_tp_rate":accepted_score10_split["would_tp_count"]/len(accepted_score10) if accepted_score10 else 0.0, "accepted_score_10_sl_rate":accepted_score10_split["would_sl_count"]/len(accepted_score10) if accepted_score10 else 0.0, "warning":"SCORE_NO_LONGER_SEPARATES_WINNERS" if accepted_score10_split["would_sl_count"] > accepted_score10_split["would_tp_count"] else "", "score_10_by_reject_reason":{k:split([r for r in score10 if str(r.get("reject_reason"))==k]) for k in sorted({str(r.get("reject_reason")) for r in score10})}, "score_10_by_regime":{k:split([r for r in score10 if str(r.get("regime"))==k]) for k in sorted({str(r.get("regime")) for r in score10})}}, "stop_too_wide_split":{"would_tp":split([r for r in stop if r.get("outcome")=="WOULD_TP"]), "would_sl":split([r for r in stop if r.get("outcome")=="WOULD_SL"]), "metrics":[g for g in group_rows if g["group_field"] in {"symbol","side","regime","effective_rr_decile","score_decile","volatility_score_bucket","liquidity_score_bucket","spread_pct_bucket","expected_slippage_pct_bucket","stop_distance_pct_bucket"} and any(str(r.get(g["group_field"]) or "UNAVAILABLE")==g["group_value"] and str(r.get("reject_reason")).upper()=="STOP_TOO_WIDE" for r in records)]}, "high_effective_rr_missed_alpha":missed, "top_quality_improvement_candidates": positive_candidates[:20], "top_quality_improvement_candidate_note": "" if positive_candidates else "No positive-expectancy improvement candidates found: near-miss groups are dominated by WOULD_SL or negative expected expectancy.", "combo_group_count": len(combo_rows), "candidate_quality_gates": gate_rows, "score_calibration_diagnostics_count": len(calibration_rows), "thresholds_changed": False, "acceptance_logic_changed": False, "score_calibration_summary": score_calibration_summary}
    summary.update(_quality_gate_metrics(records, quality_gate_config or QualityGateConfig(), baseline_net_pnl))
    return summary, group_rows, missed, combo_rows, gate_rows, calibration_rows

def build_rejected_shadow_summary(shadows: List[RejectedShadowEvaluation]) -> Dict[str, Any]:
    total = len(shadows)
    counts = {
        k: sum(1 for s in shadows if s.shadow_outcome == k)
        for k in ["WOULD_TP", "WOULD_SL", "WOULD_NOT_TRIGGER", "WOULD_TIMEOUT", "UNKNOWN"]
    }
    profitable = sum(1 for s in shadows if s.effective_tp_hit)
    unprofitable = counts["WOULD_SL"]
    avoidable_loss = sum(1 for s in shadows if s.shadow_outcome == "WOULD_SL")
    missed_profit = profitable
    false_positive_rate = (profitable / total) if total else 0.0
    reject_precision = ((total - profitable) / total) if total else 0.0
    expectancy = (
        0.0
        if total == 0
        else sum(
            (
                s.effective_rr
                if s.effective_tp_hit
                else (-1.0 if s.shadow_outcome == "WOULD_SL" else 0.0)
            )
            for s in shadows
        )
        / total
    )
    def _group(attr: str) -> Dict[str, Dict[str, float]]:
        out: Dict[str, Dict[str, float]] = {}
        for s in shadows:
            key = str(getattr(s, attr))
            bucket = out.setdefault(key, {"count": 0, "would_tp": 0, "would_sl": 0, "effective_tp": 0})
            bucket["count"] += 1
            bucket["would_tp"] += int(s.shadow_outcome == "WOULD_TP")
            bucket["would_sl"] += int(s.shadow_outcome == "WOULD_SL")
            bucket["effective_tp"] += int(s.effective_tp_hit)
        return out
    grouped: Dict[str, Dict[str, Any]] = {}
    for s in shadows:
        reason = str(s.reject_reasons or "UNKNOWN")
        bucket = grouped.setdefault(reason, {"rows": 0, "would_tp": 0, "effective_tp": 0, "score_sum": 0.0, "raw_rr_sum": 0.0, "effective_rr_sum": 0.0, "cost_penalty_sum": 0.0, "symbols": {}, "regimes": {}})
        bucket["rows"] += 1
        bucket["would_tp"] += int(s.shadow_outcome == "WOULD_TP")
        bucket["effective_tp"] += int(bool(s.effective_tp_hit))
        bucket["score_sum"] += s.score
        bucket["raw_rr_sum"] += s.raw_rr
        bucket["effective_rr_sum"] += s.effective_rr
        bucket["cost_penalty_sum"] += s.cost_penalty
        bucket["symbols"][s.symbol] = bucket["symbols"].get(s.symbol, 0) + 1
        bucket["regimes"][s.regime] = bucket["regimes"].get(s.regime, 0) + 1
    reason_diagnostics = {}
    for reason, b in grouped.items():
        rows = max(1, b["rows"])
        top_symbols = sorted(b["symbols"].items(), key=lambda x: (-x[1], x[0]))[:3]
        top_regimes = sorted(b["regimes"].items(), key=lambda x: (-x[1], x[0]))[:3]
        reason_diagnostics[reason] = {
            "rows": b["rows"],
            "would_tp_count": b["would_tp"],
            "would_tp_rate": b["would_tp"] / rows,
            "effective_tp_hit_count": b["effective_tp"],
            "effective_tp_hit_rate": b["effective_tp"] / rows,
            "avg_score": b["score_sum"] / rows,
            "avg_raw_rr": b["raw_rr_sum"] / rows,
            "avg_effective_rr": b["effective_rr_sum"] / rows,
            "avg_cost_penalty": b["cost_penalty_sum"] / rows,
            "top_symbols": top_symbols,
            "top_regimes": top_regimes,
        }
    return {
        "total_rejected": total,
        "would_tp": counts["WOULD_TP"],
        "would_sl": counts["WOULD_SL"],
        "would_not_trigger": counts["WOULD_NOT_TRIGGER"],
        "would_timeout": counts["WOULD_TIMEOUT"],
        "rejected_raw_win_rate": (counts["WOULD_TP"] / total if total else 0.0),
        "rejected_effective_expectancy": expectancy,
        "performance_by_reject_reason": json.dumps(_group("reject_reasons"), sort_keys=True),
        "performance_by_regime": json.dumps(_group("regime"), sort_keys=True),
        "performance_by_symbol": json.dumps(_group("symbol"), sort_keys=True),
        "profitable_reject_count": profitable,
        "unprofitable_reject_count": unprofitable,
        "avoidable_loss_count": avoidable_loss,
        "missed_profit_count": missed_profit,
        "reject_precision": reject_precision,
        "reject_false_positive_rate": false_positive_rate,
        "reject_reason_diagnostics": json.dumps(reason_diagnostics, sort_keys=True),
    }
def main():
    cfg = load_config_from_env()
    p = argparse.ArgumentParser()
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--last-n-days", type=int, default=7)
    p.add_argument("--top-n", "--max-symbols", dest="top_n", type=int, default=cfg.backtest.top_n)
    p.add_argument("--quote", default="USDT")
    p.add_argument("--interval", default=cfg.backtest.timeframe)
    p.add_argument("--output-dir", default=cfg.backtest.output_dir)
    p.add_argument("--mode", default="BACKTEST")
    p.add_argument("--balance", type=float, default=cfg.backtest.initial_balance)
    p.add_argument("--risk-pct", type=float, default=cfg.backtest.risk_pct)
    p.add_argument("--telegram", action="store_true")
    p.add_argument("--offline", action="store_true", help="Run without network APIs using deterministic fixture data")
    p.add_argument("--ci", action="store_true", help="CI-safe mode; implies --offline")
    p.add_argument("--symbols", nargs="*", default=[], help="Comma- or whitespace-separated fixed symbol list for deterministic historical universe")
    p.add_argument("--force-refresh", action="store_true", help="Fetch the full requested Binance historical range before running")
    p.add_argument("--rescue-enabled", action="store_true", help="Enable BACKTEST-only high effective-RR rescue acceptance lane")
    p.add_argument("--rescue-modes", default="BACKTEST")
    p.add_argument("--rescue-effective-rr-min", type=float, default=1.90)
    p.add_argument("--rescue-score-min", type=float, default=9.0)
    p.add_argument("--rescue-size-multiplier", type=float, default=0.25)
    p.add_argument("--max-rescue-trades-per-day", type=int, default=1)
    p.add_argument("--rescue-allowed-reasons", default="STOP_TOO_WIDE,DAILY_SYMBOL_TRADE_LIMIT")
    p.add_argument("--rescue-allow-regime-mismatch", action="store_true")
    p.add_argument("--rescue-max-spread-pct", type=float, default=0.0025)
    p.add_argument("--rescue-max-slippage-pct", type=float, default=0.0020)
    p.add_argument("--rescue-allow-cooldown-bypass", action="store_true")
    p.add_argument("--short-breakdown-rescue-enabled", action="store_true", default=str(os.getenv("ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED", "false")).lower() in {"1","true","yes","on"}, help="Enable BACKTEST-only SHORT breakdown rescue activation; disabled is reporting-only")
    p.add_argument("--short-breakdown-rescue-modes", default="BACKTEST")
    p.add_argument("--short-breakdown-rescue-size-multiplier", type=float, default=float(os.getenv("ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_SIZE_MULTIPLIER", "0.25")))
    p.add_argument("--short-breakdown-rescue-max-per-day", type=int, default=int(os.getenv("ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_MAX_PER_DAY", "1")))
    p.add_argument("--short-breakdown-rescue-allowed-reasons", default=os.getenv("ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ALLOWED_REASONS", "LOW_SCORE,STOP_TOO_WIDE,DAILY_SYMBOL_TRADE_LIMIT"))
    p.add_argument("--short-breakdown-rescue-min-effective-rr", type=float, default=float(os.getenv("ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_MIN_EFFECTIVE_RR", "1.10")))
    p.add_argument("--short-breakdown-rescue-min-shadow-expectancy", type=float, default=float(os.getenv("ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_MIN_SHADOW_EXPECTANCY", "0.0")))
    p.add_argument("--short-breakdown-rescue-max-spread-pct", type=float, default=0.0025)
    p.add_argument("--short-breakdown-rescue-max-slippage-pct", type=float, default=0.0020)
    p.add_argument("--quality-gate-enabled", action="store_true", help="Enable BACKTEST-only reporting comparison for SHORT breakdown/breakout NORMAL-stop gate")
    p.add_argument("--quality-gate-modes", default="BACKTEST")
    p.add_argument("--quality-gate-size-multiplier", type=float, default=0.25)
    p.add_argument("--max-quality-gate-trades-per-day", type=int, default=1)
    p.add_argument("--quality-gate-min-effective-rr", type=float, default=1.10)
    p.add_argument("--quality-gate-min-score", default=None)
    p.add_argument("--quality-gate-name", default=QUALITY_GATE_NAME)
    p.add_argument("--quality-gate-max-spread-pct", type=float, default=0.0025)
    p.add_argument("--quality-gate-max-slippage-pct", type=float, default=0.0020)
    p.add_argument("--quality-gate-allowed-reasons", default="LOW_SCORE,DAILY_SYMBOL_TRADE_LIMIT")

    p.add_argument("--disable-backtest-filter", action="append", default=[], choices=list(BACKTEST_FILTER_REASONS), help="Disable one real BACKTEST rejection gate; may be repeated")
    p.add_argument("--compare-filter-profiles", action="store_true", help="Write BACKTEST-only filter profile comparison artifact scaffold for DEFAULT/ALL_OFF/CUSTOM; does not change decisions")
    args = p.parse_args()
    if args.ci:
        args.offline = True
    disabled_filters = tuple(sorted(set(_disabled_backtest_filters(args)) | {str(x).upper() for x in getattr(args, "disable_backtest_filter", [])}))
    rescue_config = _rescue_config_from_args(args, cfg)
    quality_gate_config = _quality_gate_config_from_args(args)
    short_breakdown_rescue_config = _short_breakdown_rescue_config_from_args(args)
    strategy_guardrail_config = strategy_guardrail_config_from_env()
    rescue_stats = RescueStats()
    now = datetime.now(timezone.utc)
    default_end = int(now.timestamp() * 1000)
    default_start = int((now.timestamp() - args.last_n_days * 86400) * 1000)
    start_ms = parse_ts(args.start) if args.start else default_start
    end_ms = parse_ts(args.end) if args.end else default_end
    os.makedirs(args.output_dir, exist_ok=True)
    try:
        fixed_symbols_for_state = normalize_symbol_list(args.symbols)
    except SymbolListError as exc:
        p.error(str(exc))
    filter_state = build_backtest_filter_state(
        disabled_filters=disabled_filters,
        source=("dashboard/env/default" if disabled_filters else "default"),
        timestamp=datetime.now(timezone.utc).isoformat(),
        symbols=fixed_symbols_for_state,
        timeframe=args.interval,
        last_days=args.last_n_days,
        short_breakdown_rescue_enabled=rescue_config.enabled and str(args.mode).upper() == "BACKTEST",
    )
    write_backtest_filter_state_artifacts(args.output_dir, filter_state)
    if args.offline:
        universe, candles_by_symbol = _offline_fixture(start_ms)
    else:
        fixed_symbols = fixed_symbols_for_state
        universe = select_symbol_universe(args.top_n, args.quote, symbols=fixed_symbols)
        _prune_stale_candle_artifacts(args.output_dir, [row["symbol"] for row in universe], args.interval)
        candles_by_symbol = {}
        for row in universe:
            c = load_or_fetch_candles(row["symbol"], args.interval, start_ms, end_ms, args.output_dir, force_refresh=args.force_refresh)
            if c:
                candles_by_symbol[row["symbol"]] = c
    save_symbol_universe(os.path.join(args.output_dir, "symbol_universe.csv"), universe)
    symbol_meta_by_symbol = {row["symbol"]: row for row in universe}
    lifecycle = []
    candidates = []
    rejected = []
    open_rows = []
    recent_stats: Dict[str, Any] = {
        "last_trade_ts_by_symbol": {},
        "trades_today_by_symbol": {},
        "global_trades_today": 0,
        "symbol_loss_streak": {},
        "global_loss_streak": 0,
        "symbol_loss_block_until": {},
        "global_loss_block_until": 0,
        "consecutive_sl_count": 0,
        "consecutive_tp_count": 0,
        "rolling_winrate": 0.0,
        "outcomes": [],
        "accepted_trades_by_day": {},
        "accepted_trades_by_symbol_day": {},
        "accepted_trades_by_symbol_regime_day": {},
        "high_vol_accepted_trades_by_day": {},
    }
    rejection_counts: Dict[str, int] = {}
    if not args.offline:
        for symbol, candles in candles_by_symbol.items():
            for i in range(len(candles)):
                symbol_meta = symbol_meta_by_symbol.get(symbol, {})
                if i < 2:
                    continue
                selector_market = _build_symbol_market_data(symbol_meta, candles, i)
                selector_result = select_symbol(symbol, selector_market, {"disabled_backtest_filters": disabled_filters})
                if not selector_result.tradable:
                    reason = selector_result.reject_reasons[0] if selector_result.reject_reasons else "SYMBOL_FILTER_REJECTED"
                    rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                    lifecycle.append(
                        LifecycleRow(
                            timestamp=candles[i].timestamp,
                            symbol=symbol,
                            side="N/A",
                            setup_type="",
                            setup_reason="",
                            regime=selector_result.regime_hint,
                            score=selector_result.symbol_score,
                            rr=None,
                            entry=0.0,
                            sl=0.0,
                            tp=0.0,
                            status_before="NONE",
                            status_after="SYMBOL_REJECTED",
                            reject_reason=reason,
                            event_flags="SYMBOL_SELECTOR",
                            volume_24h_usdt=selector_market.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
                            spread_pct=selector_market.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                            liquidity_score=selector_market.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                            effective_rr=None,
                            expectancy_bucket="NOT_APPLICABLE_SYMBOL_FILTER",
                            source_stage="SYMBOL_SELECTOR",
                            rr_available=False,
                            effective_rr_available=False,
                            expectancy_available=False,
                        )
                    )
                    rejected.append(
                        {
                            "signal_id": f"SYMBOL_SELECTOR:{symbol}:{candles[i].timestamp}",
                            "lifecycle_state": "SYMBOL_REJECTED",
                            "source_stage": "SYMBOL_SELECTOR",
                            "timestamp": candles[i].timestamp,
                            "symbol": symbol,
                            "side": "N/A",
                            "setup_type": "",
                            "setup_reason": "SYMBOL_SELECTOR",
                            "regime": selector_result.regime_hint,
                            "score": selector_result.symbol_score,
                            "rr": "",
                            "rr_available": False,
                            "expectancy": "",
                            "expectancy_available": False,
                            "quality_score": "",
                            "reject_reason": reason,
                            "diagnostics": json.dumps(
                                {
                                    "selector": selector_result.diagnostics,
                                    "warnings": selector_result.warnings,
                                    "reject_reasons": selector_result.reject_reasons,
                                    "derived": selector_market.get("selector_diagnostics", {}),
                                },
                                sort_keys=True,
                            ),
                            "event_flags": "SYMBOL_SELECTOR",
                            "volume_24h_usdt": selector_market.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
                            "spread_pct": selector_market.get("spread_pct", "UNAVAILABLE_BACKTEST"),
                            "liquidity_score": selector_market.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
                            "expectancy_bucket": "NOT_APPLICABLE_SYMBOL_FILTER",
                            "raw_rr": "",
                            "effective_rr": "",
                            "effective_rr_available": False,
                            **_low_score_rescue_watch_fields(reason, {}),
                        }
                    )
                    continue
                scan_ctx = {
                    "mode": args.mode,
                    "balance": args.balance,
                    "risk_pct": args.risk_pct,
                    "recent_stats": recent_stats,
                    "symbol_meta": symbol_meta,
                    "disabled_backtest_filters": disabled_filters,
                    "min_effective_rr": getattr(getattr(cfg, "runtime", cfg), "min_effective_rr", 1.60),
                }
                _ = scan_symbol_backtest(symbol, candles, i, scan_ctx)
                result = scan_ctx.get("last_result", {})
                mctx = scan_ctx.get("market_ctx", {})
                if isinstance(mctx, dict):
                    mctx.setdefault("symbol_score", selector_result.symbol_score)
                    mctx.setdefault("regime_hint", selector_result.regime_hint)
                    mctx.setdefault("symbol_selector_warnings", selector_result.warnings)
                cand = process_backtest_result(
                    symbol,
                    candles[i],
                    i,
                    candles,
                    result,
                    mctx,
                    args.balance,
                    args.risk_pct,
                    lifecycle,
                    rejected,
                    rejection_counts,
                    open_rows,
                    recent_stats,
                    rescue_config=rescue_config,
                    rescue_stats=rescue_stats,
                    short_breakdown_rescue_config=short_breakdown_rescue_config,
                    mode=args.mode,
                    disabled_backtest_filters=disabled_filters,
                    strategy_guardrail_config=strategy_guardrail_config,
                )
                if cand:
                    candidates.append(cand)
    if args.offline and not candidates and candles_by_symbol:
        symbol = next(iter(candles_by_symbol.keys()))
        fixture_candles = candles_by_symbol[symbol]
        c0 = fixture_candles[5]
        mctx = _build_market_ctx(
            fixture_candles[6],
            fixture_candles[5],
            {"quoteVolume": 100000000.0},
            fixture_candles[:7],
        )
        mctx["MIN_EFFECTIVE_RR"] = getattr(getattr(cfg, "runtime", cfg), "min_effective_rr", 1.60)
        synthetic = CandidateOrder(
            c0.timestamp,
            symbol,
            "LONG",
            c0.close,
            c0.close - 0.5,
            c0.close + (c0.close - (c0.close - 0.5)) * mctx["rr"],
            mctx["rr"],
            "BREAKOUT_UP",
            "OFFLINE_FIXTURE",
            "TREND",
            mctx["score"],
            "LIMIT",
            expectancy_bucket=mctx.get("expectancy_bucket", "UNKNOWN"),
        )
        candidates.append(synthetic)
        lifecycle.extend(
            simulate_candidate(
                synthetic,
                fixture_candles,
                5,
                args.balance,
                args.risk_pct,
                market_ctx={
                    "volume_24h_usdt": 100000000.0,
                    "spread_pct": 0.2,
                    "liquidity_score": 0.8,
                    "expected_slippage_pct": 0.001,
                    "volatility_regime": "NORMAL",
                },
            )
        )
        rejected.append(
            {
                "timestamp": fixture_candles[8].timestamp,
                "symbol": symbol,
                "side": "LONG",
                "setup_type": "BREAKOUT_UP",
                "setup_reason": "OFFLINE_FIXTURE",
                "regime": "TREND",
                "score": 4.0,
                "rr": 0.9,
                "expectancy": -0.1,
                "quality_score": 0.1,
                "reject_reason": "LOW_EFFECTIVE_RR",
                "diagnostics": json.dumps({"offline": True}, sort_keys=True),
                "entry": fixture_candles[8].close,
                "sl": fixture_candles[8].close - 0.5,
                "tp": fixture_candles[8].close + 0.3,
                "spread_pct": 0.2,
                "liquidity_score": 0.8,
                "volatility_score": 0.2,
                "expected_slippage_pct": 0.001,
                **_low_score_rescue_watch_fields("LOW_EFFECTIVE_RR", {}),
            }
        )
        lifecycle.append(
            LifecycleRow(
                timestamp=fixture_candles[8].timestamp,
                symbol=symbol,
                side="LONG",
                setup_type="BREAKOUT_UP",
                setup_reason="OFFLINE_FIXTURE",
                regime="TREND",
                score=4.0,
                rr=0.9,
                entry=fixture_candles[8].close,
                sl=fixture_candles[8].close - 0.5,
                tp=fixture_candles[8].close + 0.3,
                status_before="SIGNAL_CREATED",
                status_after="SIGNAL_REJECTED",
                reject_reason="LOW_EFFECTIVE_RR",
                order_type="N/A",
                volume_24h_usdt=100000000.0,
                spread_pct=0.2,
                liquidity_score=0.8,
                expected_slippage_pct=0.001,
            )
        )
    candidate_rows = [{**asdict(x), "quality_score": "", "accepted": True, "reject_reason": "", "raw_rr": x.rr, "effective_rr": x.rr, "min_required_score": "", "trend_strength": "", "volatility_pct": "", "range_position": "", "spread_pct": "", "slippage_pct": "", "liquidity_score": "", "first_blocking_gate": "", "all_failed_gates": "[]"} for x in candidates]
    rejected_shadow: List[RejectedShadowEvaluation] = []
    for row in rejected:
        if not _is_actionable_rejected_order(row):
            continue
        symbol = row.get("symbol")
        ts = int(row.get("timestamp", 0) or 0)
        candles = candles_by_symbol.get(symbol, [])
        idx = next((i for i, c in enumerate(candles) if c.timestamp > ts), len(candles))
        rejected_shadow.append(evaluate_rejected_shadow(row, candles, idx))
    rejected_forward_outcomes = build_rejected_forward_outcomes(
        rejected, candles_by_symbol, forward_window_bars=240, interval_minutes=_interval_to_minutes(args.interval)
    )
    low_score_forward_summary = build_low_score_forward_summary(rejected_forward_outcomes)
    symbol_reject_forward_summary = build_symbol_reject_forward_summary(rejected_forward_outcomes)
    rejected_forward_confirmation = build_rejected_forward_confirmation_summary(rejected_forward_outcomes)
    reject_bucket_expectancy = build_reject_bucket_expectancy(rejected_forward_outcomes)
    reject_overlay_diagnostics, reject_overlay_summary = build_reject_overlay_diagnostics(rejected_forward_outcomes, reject_bucket_expectancy)
    diagnostic_short_candidates, diagnostic_short_summary = build_short_low_score_breakdown_diagnostic_profile(rejected_forward_outcomes)
    forward_evaluations = build_forward_evaluations_from_lifecycle(lifecycle, candles_by_symbol)
    lifecycle_index = {f"{row.symbol}:{row.timestamp}": row for row in lifecycle}
    calibration_snapshots = persist_calibration_snapshots(forward_evaluations, lifecycle_index)
    adaptive_scope_stats: List[dict[str, Any]] = []
    for ev in forward_evaluations:
        scope_payload = {
            "signal_id": ev.signal_id,
            "regime": ev.forward_window_regime,
            "setup_type": lifecycle_index.get(ev.signal_id).setup_type if lifecycle_index.get(ev.signal_id) else "UNKNOWN",
            "timeframe": args.interval,
            "session": "BACKTEST",
            "volatility_bucket": _bucket_numeric(abs(ev.max_forward_return), [0.2, 0.5], ["LOW", "MEDIUM", "HIGH"]),
            "spread_bucket": _bucket_numeric(_safe_float(lifecycle_index.get(ev.signal_id).spread_pct if lifecycle_index.get(ev.signal_id) else 0.0), [0.03, 0.08], ["TIGHT", "NORMAL", "WIDE"]),
            "liquidity_bucket": _bucket_numeric(_safe_float(lifecycle_index.get(ev.signal_id).liquidity_score if lifecycle_index.get(ev.signal_id) else 0.0), [0.3, 0.7], ["THIN", "NORMAL", "DEEP"]),
            "trend_strength_bucket": _bucket_numeric(_safe_float(lifecycle_index.get(ev.signal_id).score if lifecycle_index.get(ev.signal_id) else 0.0), [3.0, 7.0], ["WEAK", "MID", "STRONG"]),
            "rejection_reason": ev.reject_reason,
            "execution_quality_bucket": ev.execution_quality_bucket,
            "reject_correct": ev.reject_correct,
        }
        adaptive_scope_stats.append(scope_payload)
    _attach_rejected_shadow_to_lifecycle(lifecycle, rejected_shadow)
    persisted_lifecycle_rows = _persist_lifecycle_rows(lifecycle)
    forward_eval_rows = build_forward_evaluation_rows(
        [{**row, "timestamp": _safe_float(row.get("event_ts"), 0.0)} for row in persisted_lifecycle_rows],
        candles_by_symbol,
        forward_window_minutes=240,
    )
    for name, rows in [
        ("order_lifecycle.csv", persisted_lifecycle_rows),
        ("order_candidates.csv", candidate_rows),
        ("backtest_orders.csv", candidate_rows),
        ("rejected_orders.csv", rejected),
        ("rejected_signals.csv", rejected),
        ("rejected_shadow.csv", [asdict(x) for x in rejected_shadow]),
        ("rejected_forward_outcomes.csv", rejected_forward_outcomes),
        ("open_at_end.csv", [asdict(x) for x in open_rows]),
        ("forward_evaluations.csv", [asdict(x) for x in forward_evaluations]),
        ("adaptive_scope_stats.csv", adaptive_scope_stats),
        ("calibration_snapshots.csv", calibration_snapshots),
    ]:
        with open(os.path.join(args.output_dir, name), "w", newline="") as f:
            if not rows:
                f.write("")
                continue
            preferred_fieldnames = list(rows[0].keys())
            fieldnames = resolve_csv_fieldnames(rows, preferred_fieldnames)
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
    counts = _derive_backtest_counts(lifecycle)
    softened_lifecycle_rows = [r for r in lifecycle if bool(getattr(r, "stop_too_wide_softened", False))]
    softened_scales = [_safe_float(getattr(r, "risk_scale", 1.0), 1.0) for r in softened_lifecycle_rows]
    stop_shadow_counts = {
        "WOULD_TP": sum(1 for s in rejected_shadow if s.reject_reasons == "STOP_TOO_WIDE" and s.shadow_outcome == "WOULD_TP"),
        "WOULD_SL": sum(1 for s in rejected_shadow if s.reject_reasons == "STOP_TOO_WIDE" and s.shadow_outcome == "WOULD_SL"),
        "WOULD_TIMEOUT": sum(1 for s in rejected_shadow if s.reject_reasons == "STOP_TOO_WIDE" and s.shadow_outcome == "WOULD_TIMEOUT"),
    }
    rescue_reason_names = {"HIGH_EFFECTIVE_RR_RESCUE", SHORT_BREAKDOWN_RESCUE_REASON}
    rescue_closed = [r for r in lifecycle if r.status_after == "POSITION_CLOSED" and r.accepted_reason in rescue_reason_names]
    baseline_closed = [r for r in lifecycle if r.status_after == "POSITION_CLOSED" and r.accepted_reason not in rescue_reason_names]
    rescue_accept_rows = [r for r in lifecycle if r.status_after == "SIGNAL_CREATED" and r.accepted_reason in rescue_reason_names]
    accepted_reason_breakdown = _distribution([
        r.accepted_reason
        for r in lifecycle
        if r.status_after in {"SIGNAL_ACCEPTED", "ORDER_PLACED", "POSITION_OPENED", "POSITION_CLOSED"}
    ])
    quality_gate_metrics = _quality_gate_metrics(
        [_quality_record_from_shadow(s, args.interval) for s in rejected_shadow],
        quality_gate_config,
        baseline_net_pnl=sum(r.net_pnl_usdt for r in baseline_closed),
    )
    equity_curve_rows, drawdown_metrics = build_equity_curve_metrics(lifecycle, args.balance)
    gate_funnel_rows = build_default_gate_funnel([asdict(x) for x in rejected_shadow] or rejected, lifecycle)
    symbol_regime_rows = build_symbol_regime_acceptance_diagnostics(lifecycle)
    summary = {
        "selected_symbols": len(universe),
        "requested_timeframe": args.interval,
        "effective_timeframe": args.interval,
        "requested_last_n_days": args.last_n_days,
        "effective_start": start_ms,
        "effective_end": end_ms,
        "symbols": json.dumps([row.get("symbol") for row in universe], sort_keys=True),
        "failure_reason": "",
        "total_candidates": counts["total_candidates"],
        "accepted_count": counts["accepted_count"],
        "baseline_accepted_trades": counts["accepted_count"] - rescue_stats.accepted_count,
        "rescue_candidate_count": rescue_stats.candidate_count,
        "rescue_accepted_count": rescue_stats.accepted_count,
        "rescue_rejected_count": rescue_stats.rejected_count,
        "rescue_accepted_would_tp_count": sum(1 for r in rescue_closed if r.close_reason == "TP_HIT"),
        "rescue_accepted_would_sl_count": sum(1 for r in rescue_closed if r.close_reason == "SL_HIT"),
        "rescue_accepted_net_pnl": sum(r.net_pnl_usdt for r in rescue_closed),
        "baseline_net_pnl": sum(r.net_pnl_usdt for r in baseline_closed),
        "baseline_plus_rescue_net_pnl": sum(r.net_pnl_usdt for r in lifecycle if r.status_after == "POSITION_CLOSED"),
        "rescue_avg_effective_rr": (sum(r.rescue_effective_rr for r in rescue_accept_rows) / len(rescue_accept_rows)) if rescue_accept_rows else 0.0,
        "rescue_avg_score": (sum(r.score for r in rescue_accept_rows) / len(rescue_accept_rows)) if rescue_accept_rows else 0.0,
        "rescue_reject_reasons": json.dumps(rescue_stats.reject_reasons, sort_keys=True),
        "short_breakdown_rescue_enabled": bool(rescue_config.enabled and str(args.mode).upper() == "BACKTEST"),
        "short_breakdown_rescue_scope": "BACKTEST-only",
        "accepted_reason_breakdown": json.dumps(accepted_reason_breakdown, sort_keys=True),
        "rejected_count": counts["rejected_count"],
        "total_rejected": counts["rejected_count"],
        "stop_too_wide_softened_count": len({r.signal_id or f"{r.symbol}:{r.timestamp}" for r in softened_lifecycle_rows}),
        "stop_too_wide_hard_reject_count": sum(1 for row in rejected if str(row.get("reject_reason", "")).upper() == "STOP_TOO_WIDE"),
        "stop_too_wide_shadow_would_tp": stop_shadow_counts["WOULD_TP"],
        "stop_too_wide_shadow_would_sl": stop_shadow_counts["WOULD_SL"],
        "stop_too_wide_shadow_timeout": stop_shadow_counts["WOULD_TIMEOUT"],
        "avg_risk_scale_for_softened_stop_too_wide": (sum(softened_scales) / len(softened_scales)) if softened_scales else 0.0,
        "rejection_rate": (
            0.0
            if counts["total_candidates"] == 0
            else counts["rejected_count"] / counts["total_candidates"]
        ),
        "total_orders": counts["total_orders"],
        "triggered_orders": counts["triggered_orders"],
        "not_triggered_orders": counts["not_triggered_orders"],
        "tp_hits": counts["tp_hits"],
        "sl_hits": counts["sl_hits"],
        "open_at_end": counts["open_at_end_orders"],
        "win_rate": (
            0.0
            if not lifecycle
            else sum(1 for r in lifecycle if r.close_reason == "TP_HIT")
            / max(1, sum(1 for r in lifecycle if r.status_after == "POSITION_CLOSED"))
        ),
        "avg_rr": 0.0 if not lifecycle else sum(_safe_float(r.rr, 0.0) for r in lifecycle) / len(lifecycle),
        "avg_pnl_pct": 0.0 if not lifecycle else sum(r.net_pnl_pct for r in lifecycle) / len(lifecycle),
        "total_pnl_pct": sum(r.net_pnl_pct for r in lifecycle),
        "return_unit": "pct_of_position_risk_sum",
        "net_pnl_unit": "USDT",
        **drawdown_metrics,
        "total_net_pnl_usdt": sum(r.net_pnl_usdt for r in lifecycle),
        "avg_hold_minutes": 0.0 if not lifecycle else sum(r.hold_minutes for r in lifecycle) / len(lifecycle),
        "performance_by_symbol": {},
        "performance_by_regime": {},
        "performance_by_setup_type": {},
        "rejection_counts": json.dumps(rejection_counts, sort_keys=True),
        "disabled_filters": json.dumps(disabled_filters),
        "filter_switch_experiment_active": bool(disabled_filters),
        "filter_profile": filter_state.get("filter_profile"),
        "hard_safety_gates_active": json.dumps([g.get("filter_name") for g in HARD_SAFETY_GATES], sort_keys=True),
        "filter_thresholds_used": json.dumps({"min_score": decision_filter_config("BACKTEST").get("MIN_TRADE_SCORE"), "min_raw_rr": decision_filter_config("BACKTEST").get("MIN_RR"), "min_effective_rr": decision_filter_config("BACKTEST").get("MIN_EFFECTIVE_RR"), "reject_unknown_expectancy": decision_filter_config("BACKTEST").get("BLOCK_UNKNOWN_EXPECTANCY"), "require_execution_context": False}, sort_keys=True),
        "disabled_filter_bypass_count": sum(int((json.loads(str(r.get("diagnostics", "{}"))) if str(r.get("diagnostics", "")).startswith("{") else {}).get("disabled_filter_bypass_count", 0) or 0) for r in rejected),
        "cancel_counts": {},
        "event_flags":{},
    }
    summary.update({
        **quality_gate_metrics,
        "quality_gate_reason_breakdown": json.dumps(quality_gate_metrics["quality_gate_reason_breakdown"], sort_keys=True),
        "quality_gate_symbol_breakdown": json.dumps(quality_gate_metrics["quality_gate_symbol_breakdown"], sort_keys=True),
        "quality_gate_daily_trade_count_distribution": json.dumps(quality_gate_metrics["quality_gate_daily_trade_count_distribution"], sort_keys=True),
    })
    strategy_quality_evidence = build_strategy_quality_evidence(lifecycle, rejected, summary, strategy_guardrail_config)
    high_vol_guard_diagnostics = build_high_vol_guard_diagnostics(rejected, strategy_guardrail_config)
    high_vol_guard_summary = classify_high_vol_guard(high_vol_guard_diagnostics)
    low_score_diagnostics = build_low_score_diagnostics(rejected)
    low_score_summary = classify_low_score(low_score_diagnostics)
    symbol_reject_diagnostics = build_symbol_reject_diagnostics(rejected)
    symbol_reject_summary = classify_symbol_reject(symbol_reject_diagnostics)
    acceptance_funnel_rows = build_acceptance_funnel(rejected, counts["accepted_count"], {**summary, "symbol_rejected_count": sum(1 for r in rejected if str(r.get("lifecycle_state", "")).upper() == "SYMBOL_REJECTED")}, strategy_guardrail_config)
    summary.update({
        "profile_quality_status": strategy_quality_evidence["profile_quality_status"],
        "profile_quality_reasons": json.dumps(strategy_quality_evidence["profile_quality_reasons"], sort_keys=True),
        "profile_quality_thresholds_used": json.dumps(strategy_quality_evidence["thresholds_used"], sort_keys=True),
        "accepted_before_guardrails": strategy_quality_evidence["accepted_before_guardrails"],
        "accepted_after_guardrails": strategy_quality_evidence["accepted_after_guardrails"],
        "rejected_by_new_guardrails": strategy_quality_evidence["rejected_by_new_guardrails"],
        **high_vol_guard_summary,
        "low_score_verdict": low_score_summary["low_score_verdict"],
        "symbol_reject_verdict": symbol_reject_summary["symbol_reject_verdict"],
        "diagnostic_short_low_score_breakdown_candidate_count": diagnostic_short_summary["candidate_count"],
        "diagnostic_short_low_score_breakdown_profile": DIAGNOSTIC_PROFILE_NAME,
        "diagnostic_short_low_score_breakdown_note": "DIAGNOSTIC ONLY; production thresholds unchanged; PAPER/LIVE unchanged",
    })
    with open(os.path.join(args.output_dir, "strategy_quality_guardrails.json"), "w") as f:
        json.dump(strategy_quality_evidence, f, indent=2, sort_keys=True)
    with open(os.path.join(args.output_dir, "strategy_quality_guardrails.csv"), "w", newline="") as f:
        flat = {k: (json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v) for k, v in strategy_quality_evidence.items()}
        w = csv.DictWriter(f, fieldnames=list(flat.keys())); w.writeheader(); w.writerow(flat)
    with open(os.path.join(args.output_dir, "high_vol_guard_diagnostics.csv"), "w", newline="") as f:
        fields = resolve_csv_fieldnames(high_vol_guard_diagnostics, list(high_vol_guard_diagnostics[0].keys()) if high_vol_guard_diagnostics else ["timestamp", "symbol", "side", "reject_reason", "volatility_metric_name", "volatility_metric_value", "volatility_threshold", "volatility_ratio_to_threshold", "would_accept_if_high_vol_guard_disabled"])
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(high_vol_guard_diagnostics)
    with open(os.path.join(args.output_dir, "high_vol_guard_summary.json"), "w") as f:
        json.dump(high_vol_guard_summary, f, indent=2, sort_keys=True)
    for name, rows, fallback in [
        ("low_score_diagnostics.csv", low_score_diagnostics, ["timestamp", "symbol", "score", "min_score_threshold", "score_gap_to_threshold", "would_accept_if_low_score_disabled"]),
        ("symbol_reject_diagnostics.csv", symbol_reject_diagnostics, ["timestamp", "symbol", "reject_reason", "threshold_fields", "gap_to_threshold", "future_leakage_risk"]),
    ]:
        with open(os.path.join(args.output_dir, name), "w", newline="") as f:
            fields = resolve_csv_fieldnames(rows, list(rows[0].keys()) if rows else fallback)
            w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)
    with open(os.path.join(args.output_dir, "low_score_summary.json"), "w") as f:
        json.dump(low_score_summary, f, indent=2, sort_keys=True)
    with open(os.path.join(args.output_dir, "symbol_reject_summary.json"), "w") as f:
        json.dump(symbol_reject_summary, f, indent=2, sort_keys=True)
    with open(os.path.join(args.output_dir, "acceptance_funnel.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["stage", "count", "delta_from_previous", "notes"]); w.writeheader(); w.writerows(acceptance_funnel_rows)
    with open(os.path.join(args.output_dir, "acceptance_funnel.json"), "w") as f:
        json.dump(acceptance_funnel_rows, f, indent=2, sort_keys=True)

    with open(os.path.join(args.output_dir, "order_backtest_summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary.keys()))
        w.writeheader()
        w.writerow(summary)
    for name, rows, fallback in [
        ("equity_curve.csv", equity_curve_rows, ["trade_index", "timestamp", "symbol", "side", "net_pnl_usdt", "equity", "drawdown", "drawdown_pct"]),
        ("default_gate_funnel.csv", gate_funnel_rows, ["gate", "candidates_entering_gate", "rejected_by_gate", "accepted_after_gate", "would_tp_count", "would_sl_count", "would_timeout_count", "unknown_count", "expected_effective_expectancy", "gate_visible", "zero_reject_warning", "funnel_scope", "comparability_note"]),
        ("symbol_regime_acceptance_diagnostics.csv", symbol_regime_rows, ["symbol", "regime", "accepted_count", "tp_count", "sl_count"]),
    ]:
        with open(os.path.join(args.output_dir, name), "w", newline="") as f:
            fieldnames = resolve_csv_fieldnames(rows, list(rows[0].keys()) if rows else fallback)
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
    quality_summary = build_backtest_quality_summary(persisted_lifecycle_rows, canonical_rejected_rows=rejected)
    quality_summary.update({
        **high_vol_guard_summary,
        **low_score_summary,
        **symbol_reject_summary,
    })
    forward_counts = {
        "rejected_forward_evaluable_count": sum(1 for r in rejected_forward_outcomes if _is_forward_evaluable(r)),
        "rejected_forward_unavailable_count": sum(1 for r in rejected_forward_outcomes if not _is_forward_evaluable(r)),
        "forward_unavailable_reason_distribution": _distribution([r.get("shadow_unavailable_reason") or r.get("first_touch_outcome") for r in rejected_forward_outcomes if not _is_forward_evaluable(r)]),
        "low_score_forward_verdict": low_score_forward_summary.get("low_score_forward_verdict"),
        "symbol_reject_forward_verdict": symbol_reject_forward_summary.get("symbol_reject_forward_verdict"),
        "rejected_shadow_expectancy_by_reason": {reason: _mean([_safe_float(x.get("effective_shadow_r_after_costs")) for x in rejected_forward_outcomes if str(x.get("reject_reason")).upper() == reason and _is_forward_evaluable(x)]) for reason in sorted({str(x.get("reject_reason")).upper() for x in rejected_forward_outcomes})},
        "rejected_shadow_outcome_distribution_by_reason": {reason: _distribution([x.get("first_touch_outcome") for x in rejected_forward_outcomes if str(x.get("reject_reason")).upper() == reason]) for reason in sorted({str(x.get("reject_reason")).upper() for x in rejected_forward_outcomes})},
        **rejected_forward_confirmation,
    }
    zero_summary = build_zero_accepted_root_cause_summary(quality_summary, high_vol_guard_summary, {**low_score_summary, **low_score_forward_summary}, {**symbol_reject_summary, **symbol_reject_forward_summary})
    zero_summary.update(forward_counts)
    if forward_counts["rejected_forward_evaluable_count"] == 0 and forward_counts["rejected_forward_unavailable_count"]:
        zero_summary["evidence_quality"] = "INSUFFICIENT"
    elif forward_counts["rejected_forward_unavailable_count"]:
        zero_summary["evidence_quality"] = "PARTIAL"
    extra_reasons = ["FORWARD_EVIDENCE_INCOMPLETE"] if forward_counts["rejected_forward_unavailable_count"] else []
    gap_dist = low_score_forward_summary.get("low_score_gap_source_distribution") or {}
    if low_score_forward_summary.get("low_score_count", 0) and int(gap_dist.get("UNAVAILABLE", 0) or 0) > int(low_score_forward_summary.get("low_score_count", 0) or 0) / 2:
        extra_reasons.append("LOW_SCORE_FORWARD_GAP_UNAVAILABLE")
    if symbol_reject_forward_summary.get("symbol_reject_count", 0) and int(symbol_reject_forward_summary.get("missing_market_structure_metric_count", 0) or 0) > int(symbol_reject_forward_summary.get("symbol_reject_count", 0) or 0) / 2:
        extra_reasons.append("SYMBOL_FORWARD_METRICS_UNAVAILABLE")
    if extra_reasons:
        zero_summary["evidence_quality"] = "PARTIAL" if forward_counts["rejected_forward_evaluable_count"] else "INSUFFICIENT"
    zero_summary["evidence_quality_reasons"] = list(dict.fromkeys(list(zero_summary.get("evidence_quality_reasons") or []) + extra_reasons))
    zero_summary["strongest_positive_diagnostic_buckets"] = reject_overlay_summary.get("strongest_positive_diagnostic_buckets", [])
    zero_summary["strongest_negative_confirmation_buckets"] = reject_overlay_summary.get("strongest_negative_confirmation_buckets", [])
    zero_summary["recommended_next_action"] = reject_overlay_summary.get("recommended_next_action")
    zero_summary["production_threshold_change_recommended"] = False
    with open(os.path.join(args.output_dir, "zero_accepted_root_cause_summary.json"), "w") as f:
        json.dump(zero_summary, f, indent=2, sort_keys=True)
    with open(os.path.join(args.output_dir, "zero_accepted_root_cause_summary.csv"), "w", newline="") as f:
        flat = {k: (json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v) for k, v in zero_summary.items()}
        w = csv.DictWriter(f, fieldnames=list(flat.keys())); w.writeheader(); w.writerow(flat)
    with open(os.path.join(args.output_dir, "diagnostic_short_low_score_breakdown_candidates.csv"), "w", newline="") as f:
        fields = resolve_csv_fieldnames(diagnostic_short_candidates, list(diagnostic_short_candidates[0].keys()) if diagnostic_short_candidates else ["timestamp", "symbol", "side", "setup", "reject_reason", "hour_group", "effective_rr", "first_touch_outcome", "effective_shadow_r_after_costs", "diagnostic_only", "production_thresholds_unchanged"])
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(diagnostic_short_candidates)
    with open(os.path.join(args.output_dir, "diagnostic_short_low_score_breakdown_summary.json"), "w") as f:
        json.dump(diagnostic_short_summary, f, indent=2, sort_keys=True)
    for artifact_name, payload in [("rejected_forward_outcomes.json", rejected_forward_outcomes), ("low_score_forward_summary.json", low_score_forward_summary), ("symbol_reject_forward_summary.json", symbol_reject_forward_summary), ("reject_overlay_summary.json", reject_overlay_summary), ("reject_bucket_expectancy.json", reject_bucket_expectancy), ("diagnostic_short_low_score_breakdown_summary.json", diagnostic_short_summary)]:
        with open(os.path.join(args.output_dir, artifact_name), "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
    for artifact_name, rows, fallback in [
        ("reject_overlay_diagnostics.csv", reject_overlay_diagnostics, ["timestamp", "symbol", "side", "setup", "reject_reason", "diagnostic_overlay_labels", "diagnostic_only", "production_decision_changed"]),
        ("reject_bucket_expectancy.csv", reject_bucket_expectancy, ["symbol", "side", "setup", "regime", "hour_group", "reject_reason", "score_gap_band", "sample_count", "verdict"]),
    ]:
        with open(os.path.join(args.output_dir, artifact_name), "w", newline="") as f:
            fields = resolve_csv_fieldnames(rows, list(rows[0].keys()) if rows else fallback)
            w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)
    for artifact_name, payload in [("low_score_forward_summary.csv", low_score_forward_summary), ("symbol_reject_forward_summary.csv", symbol_reject_forward_summary)]:
        with open(os.path.join(args.output_dir, artifact_name), "w", newline="") as f:
            flat = {k: (json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v) for k, v in payload.items()}
            w = csv.DictWriter(f, fieldnames=list(flat.keys())); w.writeheader(); w.writerow(flat)
    write_backtest_quality_summary(
        os.path.join(args.output_dir, "backtest_quality_summary.csv"),
        quality_summary,
    )
    accepted_loss_diagnostics = build_accepted_loss_diagnostics(persisted_lifecycle_rows)
    with open(os.path.join(args.output_dir, "accepted_trade_loss_diagnostics.json"), "w") as f:
        json.dump(accepted_loss_diagnostics, f, indent=2, sort_keys=True)
    with open(os.path.join(args.output_dir, "accepted_trade_loss_diagnostics.csv"), "w", newline="") as f:
        loss_rows = accepted_loss_diagnostics.get("rows", [])
        fields = resolve_csv_fieldnames(loss_rows, list(loss_rows[0].keys()) if loss_rows else ["grouping", "bucket", "count"])
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(loss_rows)
    comparison = build_filter_profile_comparison_artifact(summary, quality_summary, filter_state)
    with open(os.path.join(args.output_dir, "backtest_filter_profile_comparison.json"), "w") as f:
        json.dump(comparison, f, indent=2, sort_keys=True)

    rejected_shadow_summary = build_rejected_shadow_summary(rejected_shadow)
    with open(os.path.join(args.output_dir, "rejected_shadow_summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rejected_shadow_summary.keys()))
        w.writeheader()
        w.writerow(rejected_shadow_summary)

    accepted_quality_rows = [
        r for r in lifecycle
        if not r.reject_reason and r.status_after in {"POSITION_CLOSED", "OPEN_AT_END", "TP_HIT", "SL_HIT"}
    ]
    if not accepted_quality_rows:
        accepted_quality_rows = [r for r in lifecycle if not r.reject_reason and r.status_after == "SIGNAL_CREATED"]
    (
        signal_quality_summary,
        signal_quality_group_rows,
        high_effective_rr_rows,
        signal_quality_combo_rows,
        candidate_quality_gate_rows,
        score_calibration_rows,
    ) = build_signal_quality_diagnostics(
        accepted_quality_rows,
        rejected_shadow,
        args.interval,
        quality_gate_config=quality_gate_config,
        baseline_net_pnl=sum(r.net_pnl_usdt for r in baseline_closed),
    )
    with open(os.path.join(args.output_dir, "signal_quality_summary.json"), "w") as f:
        json.dump(signal_quality_summary, f, indent=2, sort_keys=True)
    with open(os.path.join(args.output_dir, "score_calibration_summary.json"), "w") as f:
        json.dump(signal_quality_summary.get("score_calibration_summary", {}), f, indent=2, sort_keys=True)
    for name, rows, fallback_fields in [
        ("signal_quality_by_group.csv", signal_quality_group_rows, ["group_field", "group_value", "count"]),
        ("high_effective_rr_missed_alpha.csv", high_effective_rr_rows, ["effective_rr_threshold", "count", "would_tp_count", "would_sl_count"]),
        ("signal_quality_combo_groups.csv", signal_quality_combo_rows, ["grouping", "count"]),
        ("candidate_quality_gates.csv", candidate_quality_gate_rows, ["gate_name", "reporting_only", "candidate_count"]),
        ("score_calibration_diagnostics.csv", score_calibration_rows, ["diagnostic", "count"]),
    ]:
        with open(os.path.join(args.output_dir, name), "w", newline="") as f:
            fieldnames = resolve_csv_fieldnames(rows, list(rows[0].keys()) if rows else fallback_fields)
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
    try:
        import importlib.util

        calibration_module_path = SRC_DIR / "alphaforge" / "dashboard" / "backtest_control.py"
        spec = importlib.util.spec_from_file_location("alphaforge_dashboard_backtest_control", calibration_module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"unable to load {calibration_module_path}")
        calibration_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = calibration_module
        spec.loader.exec_module(calibration_module)
        calibration_module._write_calibration_artifacts(
            Path(args.output_dir),
            persisted_lifecycle_rows,
            rejected,
            summary,
            [asdict(x) for x in rejected_shadow],
        )
    except Exception as exc:
        raise ValueError(f"Lifecycle calibration artifact generation failed: {exc}") from exc
    with Session(init_db("sqlite+pysqlite:///:memory:")) as session:
        for row in forward_eval_rows:
            session.execute(
                text(
                    """
                    INSERT INTO calibration_snapshots (
                        signal_id, predicted_quality, realized_outcome, score, rr, effective_rr, regime, setup_type,
                        rejection_reason, forward_window_minutes, mfe_pct, mae_pct, would_have_hit_tp, would_have_hit_sl,
                        reject_correct, created_at
                    ) VALUES (
                        :signal_id, :predicted_quality, :realized_outcome, :score, :rr, :effective_rr, :regime, :setup_type,
                        :rejection_reason, :forward_window_minutes, :mfe_pct, :mae_pct, :would_have_hit_tp, :would_have_hit_sl,
                        :reject_correct, :created_at
                    )
                    ON CONFLICT(signal_id, forward_window_minutes) DO NOTHING
                    """
                ),
                {
                    "signal_id": row.get("signal_id"),
                    "predicted_quality": _safe_float(row.get("execution_quality_bucket") == "HIGH", 0.0),
                    "realized_outcome": row.get("realized_outcome"),
                    "score": _safe_float(row.get("score"), 0.0),
                    "rr": _safe_float(row.get("rr"), 0.0),
                    "effective_rr": _safe_float(row.get("max_forward_return"), 0.0),
                    "regime": row.get("forward_window_regime"),
                    "setup_type": row.get("setup_type"),
                    "rejection_reason": row.get("reject_reason"),
                    "forward_window_minutes": int(row.get("forward_window_minutes", 240)),
                    "mfe_pct": _safe_float(row.get("mfe_pct"), 0.0),
                    "mae_pct": _safe_float(row.get("mae_pct"), 0.0),
                    "would_have_hit_tp": int(bool(row.get("would_have_hit_tp"))),
                    "would_have_hit_sl": int(bool(row.get("would_have_hit_sl"))),
                    "reject_correct": None if row.get("reject_correct") is None else int(bool(row.get("reject_correct"))),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        snapshot_rows = session.execute(text("SELECT * FROM calibration_snapshots ORDER BY id")).mappings().all()
    with open(os.path.join(args.output_dir, "calibration_snapshots.csv"), "w", newline="") as f:
        if snapshot_rows:
            fieldnames = resolve_csv_fieldnames([dict(r) for r in snapshot_rows], list(dict(snapshot_rows[0]).keys()))
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows([dict(r) for r in snapshot_rows])
    with open(os.path.join(args.output_dir, "order_lifecycle.csv"), newline="") as f:
        lifecycle_csv_rows = list(csv.DictReader(f))
    with open(os.path.join(args.output_dir, "rejected_orders.csv"), newline="") as f:
        rejected_csv_rows = list(csv.DictReader(f))
    export_errors = verify_export_integrity(
        persisted_lifecycle_rows=persisted_lifecycle_rows,
        rejected_rows=rejected,
        lifecycle_csv_rows=lifecycle_csv_rows,
        rejected_csv_rows=rejected_csv_rows,
    )
    if export_errors:
        raise ValueError(f"Export integrity check failed: {'; '.join(export_errors)}")
def _order_runtime():
    from alphaforge.order import OrderExecutionContext, TradingMode, run_order_cycle
    return OrderExecutionContext, TradingMode, run_order_cycle
if __name__ == "__main__":
    main()
