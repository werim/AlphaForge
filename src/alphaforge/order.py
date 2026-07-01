from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping

from sqlalchemy.orm import Session

from alphaforge.ai_brain import AIBrain
from alphaforge.execution import build_execution_context, neutral_execution_context, build_execution_cost_model, classify_execution_evidence, EXECUTION_EVIDENCE_INVALID_FAKE_ZERO, EXECUTION_EVIDENCE_UNAVAILABLE_BLOCKING
from alphaforge.effective_rr import calculate_effective_rr
from alphaforge.config_registry import decision_filter_config
from alphaforge.persistence import (
    fetch_expectancy_stat,
    save_ai_decision_features,
    save_closed_trade_review,
    save_order_decision,
    save_signal,
    save_trade_lifecycle_event,
    upsert_expectancy_stats,
)

logger = logging.getLogger(__name__)
MIN_RR_THRESHOLD = float(decision_filter_config("PAPER")["MIN_EFFECTIVE_RR"])
MIN_SCORE_BASE = 7.5
MIN_RR_BASE = 1.3


def normalize_execution_ctx(ctx: Mapping[str, Any] | None) -> dict[str, Any]:
    base = dict(ctx or {})
    return {
        "expected_slippage_pct": _nullable_float(base.get("expected_slippage_pct")),
        "spread_pct": _nullable_float(base.get("spread_pct")),
        "latency_ms": _nullable_float(base.get("latency_ms")),
        "liquidity_score": _nullable_float(base.get("liquidity_score")),
        "volatility_regime": base.get("volatility_regime"),
        "orderbook_imbalance": _nullable_float(base.get("orderbook_imbalance")),
        "funding_rate_pct": _nullable_float(base.get("funding_rate_pct")),
        "spread_status": str(base.get("spread_status", "") or ""),
        "slippage_status": str(base.get("slippage_status", "") or ""),
        "latency_status": str(base.get("latency_status", base.get("market_data_latency_status", "")) or ""),
        "market_data_latency_status": str(base.get("market_data_latency_status", base.get("latency_status", "")) or ""),
        "liquidity_status": str(base.get("liquidity_status", "") or ""),
        "funding_status": str(base.get("funding_status", "") or ""),
        "orderbook_status": str(base.get("orderbook_status", "") or ""),
        "volatility_status": str(base.get("volatility_status", "") or ""),
        "evidence_status": str(base.get("evidence_status", "") or ""),
        "spoof_risk": float(base.get("spoof_risk", 0.0) or 0.0),
        "absorption_score": float(base.get("absorption_score", 0.0) or 0.0),
    }


class TradingMode(str, Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


class LifecycleState(str, Enum):
    SIGNAL_CREATED = "SIGNAL_CREATED"
    SIGNAL_REJECTED = "SIGNAL_REJECTED"
    WAITING_ENTRY_ZONE = "WAITING_ENTRY_ZONE"
    ENTRY_TRIGGERED = "ENTRY_TRIGGERED"
    ORDER_PLACED = "ORDER_PLACED"
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_CLOSED = "POSITION_CLOSED"
    ENTRY_TIMEOUT = "ENTRY_TIMEOUT"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"


@dataclass
class OrderExecutionContext:
    mode: TradingMode
    timestamp: int
    symbol: str
    balance: float
    risk_pct: float
    allow_telegram: bool = False
    allow_live_orders: bool = False
    market_ctx: Mapping[str, Any] = field(default_factory=dict)
    storage: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderCandidate:
    symbol: str
    side: str
    setup_type: str
    setup_reason: str
    regime: str
    score: float
    rr: float
    expectancy: float | None
    entry: float
    sl: float
    tp: float
    order_type: str = "MARKET"


@dataclass
class OrderRejection:
    symbol: str
    reject_reason: str
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass
class TradeQualityDecision:
    accepted: bool
    reject_reason: str = ""
    quality_score: float = 0.0
    diagnostics: dict[str, Any] = field(default_factory=dict)


def _nullable_float(value: Any) -> float | None:
    if value in (None, "", "UNKNOWN", "UNAVAILABLE", "UNAVAILABLE_BACKTEST"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_execution_payload(payload: Mapping[str, Any] | None, order: Mapping[str, Any] | None = None, ctx: Mapping[str, Any] | None = None) -> dict[str, Any]:
    base = dict(payload or {})
    order = dict(order or {})
    base_rr = (
        order.get("risk_reward")
        or order.get("rr")
        or base.get("risk_reward")
        or base.get("effective_rr")
        or 0.0
    )
    base.setdefault("execution_flags", [])
    base["effective_rr"] = round(float(base.get("effective_rr", base_rr) or base_rr), 10)
    base.setdefault("execution_metrics", {})
    base.setdefault("execution_ctx_missing", bool((ctx or {}).get("execution_ctx_missing", False)))
    base.setdefault("adjusted_risk_reward", base["effective_rr"])
    base.setdefault("effective_rr_breakdown", {})
    base.setdefault("block_reason", "")
    base.setdefault("reject_reason", "")
    return base


def build_order_candidate(symbol: str, market_ctx: Mapping[str, Any], config: Mapping[str, Any]) -> OrderCandidate | OrderRejection:
    entry = float(market_ctx.get("entry", 0.0) or 0.0)
    sl = float(market_ctx.get("sl", 0.0) or 0.0)
    tp = float(market_ctx.get("tp", 0.0) or 0.0)
    if entry <= 0 or sl <= 0 or tp <= 0:
        return OrderRejection(symbol=symbol, reject_reason="INVALID_LEVELS")
    side = str(market_ctx.get("side", "LONG"))
    score = float(market_ctx.get("score", 0.0) or 0.0)
    rr = float(market_ctx.get("rr", 0.0) or 0.0)
    expectancy = market_ctx.get("expectancy")
    return OrderCandidate(
        symbol=symbol,
        side=side,
        setup_type=str(market_ctx.get("setup_type", "GENERIC")),
        setup_reason=str(market_ctx.get("setup_reason", "NONE")),
        regime=str(market_ctx.get("regime", "UNKNOWN")),
        score=score,
        rr=rr,
        expectancy=float(expectancy) if expectancy is not None else None,
        entry=entry,
        sl=sl,
        tp=tp,
        order_type=str(market_ctx.get("order_type", "MARKET")),
    )


def evaluate_trade_quality(candidate: OrderCandidate, market_ctx: Mapping[str, Any], recent_stats: Mapping[str, Any], config: Mapping[str, Any]) -> TradeQualityDecision:
    supplied_config = dict(config or {})
    mode = str(supplied_config.get("MODE", supplied_config.get("mode", "PAPER"))).upper()
    adaptive = compute_adaptive_thresholds(recent_stats, config=supplied_config)
    cfg = decision_filter_config(mode)
    cfg["MIN_TRADE_SCORE"] = adaptive["min_score"]
    cfg["MIN_RR"] = adaptive["min_rr"]
    cfg.update(supplied_config)
    if mode == "BACKTEST" and "RUNTIME_LIMITS_ACTIVE" not in supplied_config:
        cfg["RUNTIME_LIMITS_ACTIVE"] = False
    reject_reason = ""
    failed_filter = ""
    symbol = getattr(candidate, "symbol", "")
    side = getattr(candidate, "side", "")
    setup_type = str(getattr(candidate, "setup_type", "") or "").upper()
    setup_reason = str(getattr(candidate, "setup_reason", "") or "")
    regime = str(getattr(candidate, "regime", "") or market_ctx.get("regime", "UNKNOWN")).upper()
    volatility_regime = str(market_ctx.get("volatility_regime", "unknown") or "unknown").lower()
    spread_pct = float(market_ctx.get("spread_pct", 0.0) or 0.0)
    expected_slippage_pct = float(market_ctx.get("expected_slippage_pct", 0.0) or 0.0)
    atr_pct = market_ctx.get("atr_pct", recent_stats.get("atr_pct"))
    atr_pct = float(atr_pct) if atr_pct not in (None, "") else None
    sl_pct = abs(float(candidate.entry) - float(candidate.sl)) / float(candidate.entry) * 100 if getattr(candidate, "entry", 0) else 0.0
    expectancy = candidate.expectancy
    if expectancy in (None, "UNKNOWN", ""):
        bucket = market_ctx.get("expectancy_bucket", getattr(candidate, "expectancy_bucket", None))
        for e in (bucket, market_ctx.get("expectancy"), recent_stats.get("expectancy")):
            try:
                expectancy = float(e)
                break
            except (TypeError, ValueError):
                continue
    try:
        expectancy_val = float(expectancy) if expectancy not in (None, "UNKNOWN", "") else None
    except (TypeError, ValueError):
        expectancy_val = None
    score = float(getattr(candidate, "score", 0.0) or 0.0)
    rr = float(getattr(candidate, "rr", 0.0) or 0.0)
    effective_rr = _nullable_float(market_ctx.get("effective_rr"))
    if effective_rr is None:
        effective_rr = rr
    min_trade_score = float(cfg["MIN_TRADE_SCORE"])
    score_eval = score if min_trade_score <= 1.0 else (score * 10.0 if 0.0 <= score < 1.0 else score)
    pattern_flags = [str(f).upper() for f in (market_ctx.get("pattern_flags", []) or [])]
    all_failed_gates: list[str] = []
    def _check(cond: bool, gate: str) -> bool:
        if not cond:
            all_failed_gates.append(gate)
            return False
        return True

    _check(score_eval >= min_trade_score, "score")
    _check(rr >= float(cfg["MIN_RR"]) and effective_rr >= float(cfg["MIN_EFFECTIVE_RR"]), "rr")
    _check((not cfg["BLOCK_UNKNOWN_EXPECTANCY"]) or expectancy_val is not None, "expectancy_present")
    _check(expectancy_val is None or expectancy_val >= float(cfg["MIN_EXPECTANCY"]), "expectancy_non_negative")
    _check((not cfg["BLOCK_CHOP_MARKET"]) or (not any("CHOP" in f for f in pattern_flags)), "pattern_flags")
    regime_ok = True
    if "TREND_CONTINUATION" in setup_type or "PULLBACK_" in setup_type:
        regime_ok = regime == "TREND"
    elif "BREAKOUT_UP" in setup_type or "BREAKOUT_DOWN" in setup_type:
        # BREAKOUT setup/regime alignment must not be blocked because a data
        # source labels volatility as BREAKOUT instead of normal/high.
        regime_ok = regime in {"TREND", "BREAKOUT"} and volatility_regime in {"normal", "high", "breakout"}
    elif "RANGE_MEAN_REVERSION" in setup_type:
        regime_ok = regime == "RANGE"
    _check((not cfg["REQUIRE_REGIME_ALIGNMENT"]) or regime_ok, "regime")
    _check(sl_pct >= float(cfg["MIN_SL_PCT"]), "min_sl")
    _check(sl_pct <= float(cfg["MAX_SL_PCT"]), "max_sl")
    _check(spread_pct <= float(cfg["MAX_SPREAD_PCT"]), "spread")
    _check(expected_slippage_pct <= float(cfg["MAX_EXPECTED_SLIPPAGE_PCT"]), "slippage")
    _check(atr_pct is None or atr_pct >= float(cfg["MIN_ATR_PCT"]), "min_atr")
    _check(atr_pct is None or atr_pct <= float(cfg["MAX_ATR_PCT"]), "max_atr")

    # compute quality score first
    score_comp = max(0.0, min(1.0, score_eval / min_trade_score)) * 25
    exp_comp = 0.0 if expectancy_val is None else max(0.0, min(1.0, (expectancy_val - float(cfg["MIN_EXPECTANCY"])) / 0.5)) * 25
    rr_comp = max(0.0, min(1.0, rr / float(cfg["MIN_RR"]))) * 10
    regime_comp = (20.0 if regime_ok else 0.0)
    micro_ok = spread_pct <= float(cfg["MAX_SPREAD_PCT"]) and expected_slippage_pct <= float(cfg["MAX_EXPECTED_SLIPPAGE_PCT"])
    vol_ok = atr_pct is None or (float(cfg["MIN_ATR_PCT"]) <= atr_pct <= float(cfg["MAX_ATR_PCT"]))
    vol_comp = (10.0 if (micro_ok and vol_ok) else 0.0)
    hygiene_comp = 10.0
    quality_score = round(score_comp + exp_comp + rr_comp + regime_comp + vol_comp + hygiene_comp, 2)
    backtest_disabled = set()
    if str(cfg.get("MODE", cfg.get("mode", ""))).upper() == "BACKTEST":
        backtest_disabled = {str(r).upper() for r in cfg.get("DISABLED_BACKTEST_FILTERS", [])}
    bypassed_reject_reasons: list[str] = []

    def _gate_enabled(reason: str) -> bool:
        return str(reason).upper() not in backtest_disabled

    def _bypass(reason: str) -> bool:
        if _gate_enabled(reason):
            return False
        bypassed_reject_reasons.append(str(reason).upper())
        return True

    if not candidate or not getattr(candidate, "symbol", None):
        reject_reason, failed_filter = "INVALID_CANDIDATE", "candidate"
    elif score_eval < min_trade_score and not _bypass("LOW_SCORE"):
        reject_reason, failed_filter = "LOW_SCORE", "score"
    elif (rr < float(cfg["MIN_RR"]) or effective_rr < float(cfg["MIN_EFFECTIVE_RR"])) and not _bypass("RR_TOO_LOW"):
        reject_reason, failed_filter = "RR_TOO_LOW", "rr"
    elif cfg["BLOCK_UNKNOWN_EXPECTANCY"] and expectancy_val is None:
        reject_reason, failed_filter = "EXPECTANCY_MISSING", "expectancy"
    elif expectancy_val is not None and expectancy_val < float(cfg["MIN_EXPECTANCY"]):
        reject_reason, failed_filter = "NEGATIVE_EXPECTANCY", "expectancy"
    elif cfg["BLOCK_CHOP_MARKET"] and any("CHOP" in f for f in pattern_flags):
        reject_reason, failed_filter = "CHOP_MARKET_BLOCK", "pattern_flags"
    elif cfg["REQUIRE_REGIME_ALIGNMENT"] and not regime_ok and not _bypass("REGIME_MISMATCH"):
        reject_reason, failed_filter = "REGIME_MISMATCH", "regime"
    elif sl_pct < float(cfg["MIN_SL_PCT"]):
        reject_reason, failed_filter = "STOP_TOO_TIGHT", "sl_pct"
    elif sl_pct > float(cfg["MAX_SL_PCT"]):
        effective_rr_for_stop = _nullable_float(market_ctx.get("effective_rr"))
        if effective_rr_for_stop is None:
            effective_rr_for_stop = rr
        stop_limit = float(cfg["MAX_SL_PCT"])
        is_extreme_stop = sl_pct > stop_limit * float(cfg["STOP_TOO_WIDE_EXTREME_MULT"])
        hard_reject_enabled = bool(cfg["STOP_TOO_WIDE_HARD_REJECT"])
        soft_eligible = (
            bool(cfg["STOP_TOO_WIDE_SOFTEN_FOR_HIGH_SCORE"])
            and score_eval >= float(cfg["STOP_TOO_WIDE_SOFT_SCORE_MIN"])
            and effective_rr_for_stop >= float(cfg["STOP_TOO_WIDE_SOFT_EFFECTIVE_RR_MIN"])
        )
        if hard_reject_enabled and (is_extreme_stop or not soft_eligible) and not _bypass("STOP_TOO_WIDE"):
            reject_reason, failed_filter = "STOP_TOO_WIDE", "sl_pct"
        else:
            failed_filter = ""
    elif spread_pct > float(cfg["MAX_SPREAD_PCT"]):
        reject_reason, failed_filter = "SPREAD_TOO_HIGH", "spread_pct"
    elif expected_slippage_pct > float(cfg["MAX_EXPECTED_SLIPPAGE_PCT"]):
        reject_reason, failed_filter = "SLIPPAGE_TOO_HIGH", "expected_slippage_pct"
    elif atr_pct is not None and atr_pct < float(cfg["MIN_ATR_PCT"]):
        reject_reason, failed_filter = "VOLATILITY_TOO_LOW", "atr_pct"
    elif atr_pct is not None and atr_pct > float(cfg["MAX_ATR_PCT"]):
        reject_reason, failed_filter = "VOLATILITY_TOO_HIGH", "atr_pct"
    else:
        now_ts = int(market_ctx.get("timestamp", 0) or 0)
        last_ts = int((recent_stats.get("last_trade_ts_by_symbol", {}) or {}).get(symbol, 0) or 0)
        if bool(cfg.get("RUNTIME_LIMITS_ACTIVE", True)) and last_ts and now_ts and (now_ts - last_ts) < int(cfg["SYMBOL_COOLDOWN_MINUTES"]) * 60_000:
            reject_reason, failed_filter = "SYMBOL_COOLDOWN_ACTIVE", "cooldown"
        elif (bool(cfg.get("RUNTIME_LIMITS_ACTIVE", True)) or mode == "BACKTEST") and int((recent_stats.get("trades_today_by_symbol", {}) or {}).get(symbol, 0) or 0) >= int(cfg["MAX_TRADES_PER_SYMBOL_PER_DAY"]) and not _bypass("DAILY_SYMBOL_TRADE_LIMIT"):
            reject_reason, failed_filter = "DAILY_SYMBOL_TRADE_LIMIT", "daily_symbol"
        elif bool(cfg.get("RUNTIME_LIMITS_ACTIVE", True)) and int(recent_stats.get("global_trades_today", 0) or 0) >= int(cfg["MAX_TRADES_GLOBAL_PER_DAY"]):
            reject_reason, failed_filter = "DAILY_GLOBAL_TRADE_LIMIT", "daily_global"
        elif bool(cfg.get("RUNTIME_LIMITS_ACTIVE", True)) and int((recent_stats.get("symbol_loss_block_until", {}) or {}).get(symbol, 0) or 0) > now_ts:
            reject_reason, failed_filter = "SYMBOL_LOSS_STREAK_BLOCK", "symbol_block"
        elif bool(cfg.get("RUNTIME_LIMITS_ACTIVE", True)) and int(recent_stats.get("global_loss_block_until", 0) or 0) > now_ts:
            reject_reason, failed_filter = "GLOBAL_LOSS_STREAK_BLOCK", "global_block"
    if reject_reason == "":
        if spread_pct > float(cfg["MAX_SPREAD_PCT"]):
            reject_reason, failed_filter = "SPREAD_TOO_HIGH", "spread_pct"
        elif expected_slippage_pct > float(cfg["MAX_EXPECTED_SLIPPAGE_PCT"]):
            reject_reason, failed_filter = "SLIPPAGE_TOO_HIGH", "expected_slippage_pct"
        elif atr_pct is not None and atr_pct < float(cfg["MIN_ATR_PCT"]):
            reject_reason, failed_filter = "VOLATILITY_TOO_LOW", "atr_pct"
        elif atr_pct is not None and atr_pct > float(cfg["MAX_ATR_PCT"]):
            reject_reason, failed_filter = "VOLATILITY_TOO_HIGH", "atr_pct"
        else:
            now_ts = int(market_ctx.get("timestamp", 0) or 0)
            last_ts = int((recent_stats.get("last_trade_ts_by_symbol", {}) or {}).get(symbol, 0) or 0)
            if bool(cfg.get("RUNTIME_LIMITS_ACTIVE", True)) and last_ts and now_ts and (now_ts - last_ts) < int(cfg["SYMBOL_COOLDOWN_MINUTES"]) * 60_000:
                reject_reason, failed_filter = "SYMBOL_COOLDOWN_ACTIVE", "cooldown"
            elif (bool(cfg.get("RUNTIME_LIMITS_ACTIVE", True)) or mode == "BACKTEST") and int((recent_stats.get("trades_today_by_symbol", {}) or {}).get(symbol, 0) or 0) >= int(cfg["MAX_TRADES_PER_SYMBOL_PER_DAY"]) and not _bypass("DAILY_SYMBOL_TRADE_LIMIT"):
                reject_reason, failed_filter = "DAILY_SYMBOL_TRADE_LIMIT", "daily_symbol"
            elif bool(cfg.get("RUNTIME_LIMITS_ACTIVE", True)) and int(recent_stats.get("global_trades_today", 0) or 0) >= int(cfg["MAX_TRADES_GLOBAL_PER_DAY"]):
                reject_reason, failed_filter = "DAILY_GLOBAL_TRADE_LIMIT", "daily_global"
            elif bool(cfg.get("RUNTIME_LIMITS_ACTIVE", True)) and int((recent_stats.get("symbol_loss_block_until", {}) or {}).get(symbol, 0) or 0) > now_ts:
                reject_reason, failed_filter = "SYMBOL_LOSS_STREAK_BLOCK", "symbol_block"
            elif bool(cfg.get("RUNTIME_LIMITS_ACTIVE", True)) and int(recent_stats.get("global_loss_block_until", 0) or 0) > now_ts:
                reject_reason, failed_filter = "GLOBAL_LOSS_STREAK_BLOCK", "global_block"

    stop_too_wide_softened = sl_pct > float(cfg["MAX_SL_PCT"]) and reject_reason == ""
    diagnostics = {"symbol": symbol, "side": side, "setup_type": setup_type, "setup_reason": setup_reason, "score": score_eval, "rr": rr, "effective_rr": effective_rr, "min_effective_rr": float(cfg["MIN_EFFECTIVE_RR"]), "min_raw_rr": float(cfg["MIN_RR"]), "min_score": min_trade_score, "reject_unknown_expectancy": bool(cfg["BLOCK_UNKNOWN_EXPECTANCY"]), "require_execution_context": False, "expectancy": expectancy_val, "regime": regime, "volatility_regime": volatility_regime, "sl_pct": sl_pct, "spread_pct": spread_pct, "expected_slippage_pct": expected_slippage_pct, "atr_pct": atr_pct, "reject_reason": reject_reason, "failed_filter": failed_filter, "quality_score": quality_score, "adaptive_thresholds": adaptive, "min_required_score": min_trade_score, "all_failed_gates": all_failed_gates, "bypassed_reject_reasons": bypassed_reject_reasons, "disabled_filters": sorted(backtest_disabled), "disabled_filter_bypass_count": len(bypassed_reject_reasons), "filter_switch_experiment_active": bool(backtest_disabled)}
    if stop_too_wide_softened:
        diagnostics.update({
            "stop_too_wide_softened": True,
            "original_reject_reason": "STOP_TOO_WIDE",
            "reject_reason_softened": "STOP_TOO_WIDE",
            "risk_scale": min(float(cfg["STOP_TOO_WIDE_MAX_RISK_SCALE"]), 1.0),
            "stop_too_wide_hard_reject_enabled": bool(cfg["STOP_TOO_WIDE_HARD_REJECT"]),
        })
    return TradeQualityDecision(accepted=(reject_reason == ""), reject_reason=reject_reason, quality_score=quality_score, diagnostics=diagnostics)


def compute_adaptive_thresholds(stats: Mapping[str, Any], config: Mapping[str, Any] | None = None) -> dict[str, float]:
    provided = dict(config or {})
    cfg = decision_filter_config(str(provided.get("MODE", provided.get("mode", "PAPER"))).upper())
    cfg.update(provided)
    min_score = float(provided.get("MIN_TRADE_SCORE", MIN_SCORE_BASE))
    min_rr = float(provided.get("MIN_RR", MIN_RR_BASE))
    consecutive_sl = int(stats.get("consecutive_sl_count", 0) or 0)
    consecutive_tp = int(stats.get("consecutive_tp_count", 0) or 0)

    if consecutive_sl >= 5:
        min_score = min_score + 1.5
        min_rr = min_rr + 0.5
    elif consecutive_sl >= 3:
        min_score = min_score + 1.0
        min_rr = min_rr + 0.3

    if consecutive_tp >= 5:
        min_score = min_score - 1.0
        min_rr = min_rr - 0.3
    elif consecutive_tp >= 3:
        min_score = min_score - 0.5
        min_rr = min_rr - 0.2

    if min_score <= 1.0:
        min_score = max(0.0, min(1.0, min_score))
    else:
        min_score = max(6.0, min(9.5, min_score))
    min_rr = max(1.2, min(3.0, min_rr))
    return {"min_score": min_score, "min_rr": min_rr}


def _audit(ctx: OrderExecutionContext, candidate: OrderCandidate | None, status_before: LifecycleState, status_after: LifecycleState, reject_reason: str = "") -> None:
    event = {
        "timestamp": ctx.timestamp,
        "mode": ctx.mode.value,
        "symbol": ctx.symbol,
        "side": getattr(candidate, "side", ""),
        "setup_type": getattr(candidate, "setup_type", ""),
        "setup_reason": getattr(candidate, "setup_reason", ""),
        "regime": getattr(candidate, "regime", ""),
        "score": getattr(candidate, "score", 0.0),
        "rr": getattr(candidate, "rr", 0.0),
        "expectancy": getattr(candidate, "expectancy", None),
        "entry": getattr(candidate, "entry", 0.0),
        "sl": getattr(candidate, "sl", 0.0),
        "tp": getattr(candidate, "tp", 0.0),
        "status_before": status_before.value,
        "status_after": status_after.value,
        "reject_reason": reject_reason,
        "quality_score": getattr(candidate, "score", 0.0),
        "order_type": getattr(candidate, "order_type", ""),
        "diagnostics": dict(ctx.diagnostics),
    }
    ctx.storage.setdefault("audit", []).append(event)


def execute_order_candidate(candidate: OrderCandidate, ctx: OrderExecutionContext) -> dict[str, Any]:
    if ctx.mode != TradingMode.LIVE:
        assert ctx.allow_live_orders is False
        if "allow_telegram" not in ctx.diagnostics:
            ctx.allow_telegram = False
        ctx.allow_telegram = bool(ctx.allow_telegram)
    status = LifecycleState.ORDER_PLACED
    if ctx.mode == TradingMode.BACKTEST:
        result = {"type": "virtual", "candidate": candidate}
    elif ctx.mode == TradingMode.PAPER:
        result = {"type": "paper", "candidate": candidate, "paper_balance": ctx.balance}
    else:
        bal_fn = ctx.storage.get("real_balance_fetcher")
        ord_fn: Callable[[OrderCandidate], Mapping[str, Any]] = ctx.storage["binance_place_order"]
        if callable(bal_fn):
            _ = bal_fn()
        result = dict(ord_fn(candidate))
        result["type"] = "live"
    if ctx.allow_telegram and "telegram_sender" in ctx.storage:
        ctx.storage["telegram_sender"](f"{ctx.mode.value}:{candidate.symbol}:{candidate.side}")
    _audit(ctx, candidate, LifecycleState.ENTRY_TRIGGERED, status)
    return result


def _rejected_cycle_result(reject_reason: str, candidate: OrderCandidate | None, execution: Any = None, diagnostics: Mapping[str, Any] | None = None) -> dict[str, Any]:
    normalized_reason = str(reject_reason or "")
    result: dict[str, Any] = {
        "status": "rejected",
        "accepted": False,
        "candidate": candidate,
        "reason": normalized_reason,
        "reject_reason": normalized_reason,
        "rejection_reason": normalized_reason,
        "execution": execution,
    }
    if diagnostics is not None:
        result["diagnostics"] = diagnostics
    return result


def run_order_cycle(ctx: OrderExecutionContext, config: Mapping[str, Any] | None = None, recent_stats: Mapping[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    recent_stats = recent_stats or {}
    decision = build_order_candidate(ctx.symbol, ctx.market_ctx, config)
    if isinstance(decision, OrderRejection):
        _audit(ctx, None, LifecycleState.SIGNAL_CREATED, LifecycleState.SIGNAL_REJECTED, decision.reject_reason)
        return _rejected_cycle_result(decision.reject_reason, candidate=None)
    session = ctx.storage.get("session")
    if decision.expectancy is None and isinstance(session, Session):
        setup_exp = fetch_expectancy_stat(session, "setup_expectancy_stats", "setup", decision.setup_type)
        regime_exp = fetch_expectancy_stat(session, "regime_expectancy_stats", "regime", decision.regime)
        if setup_exp is not None or regime_exp is not None:
            values = [v for v in (setup_exp, regime_exp) if v is not None]
            inferred_expectancy = sum(values) / len(values)
            decision.expectancy = inferred_expectancy
            ctx.market_ctx = {**ctx.market_ctx, "expectancy": inferred_expectancy}
    quality = evaluate_trade_quality(decision, ctx.market_ctx, recent_stats, config)
    if not quality.accepted:
        ctx.diagnostics.update(quality.diagnostics)
        reason = quality.reject_reason or "UNKNOWN"
        _audit(ctx, decision, LifecycleState.SIGNAL_CREATED, LifecycleState.SIGNAL_REJECTED, reason)
        payload = _rejected_cycle_result(reason, candidate=decision, diagnostics=quality.diagnostics)
        payload["accepted"] = False
        payload["reason"] = reason
        payload["reject_reason"] = reason
        return payload
    ctx.diagnostics.update(quality.diagnostics)
    execution = execute_order_candidate(decision, ctx)
    return {"status": "executed", "accepted": True, "candidate": decision, "reason": "", "reject_reason": "", "rejection_reason": "", "execution": execution, "diagnostics": quality.diagnostics}


def evaluate_paper_style_pre_submit(ctx: OrderExecutionContext, config: Mapping[str, Any] | None = None, recent_stats: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Run shared candidate-quality and PAPER execution-cost pre-submit checks.

    The adapter mirrors ``run_order_cycle`` through candidate construction and
    quality gates, then evaluates the PAPER effective-RR execution flags before
    any virtual/paper execution audit is emitted. It never calls Binance submit
    functions and is safe for BACKTEST/PAPER parity tests.
    """
    config = config or {}
    recent_stats = recent_stats or {}
    decision = build_order_candidate(ctx.symbol, ctx.market_ctx, config)
    if isinstance(decision, OrderRejection):
        _audit(ctx, None, LifecycleState.SIGNAL_CREATED, LifecycleState.SIGNAL_REJECTED, decision.reject_reason)
        return _rejected_cycle_result(decision.reject_reason, candidate=None)

    session = ctx.storage.get("session")
    if decision.expectancy is None and isinstance(session, Session):
        setup_exp = fetch_expectancy_stat(session, "setup_expectancy_stats", "setup", decision.setup_type)
        regime_exp = fetch_expectancy_stat(session, "regime_expectancy_stats", "regime", decision.regime)
        if setup_exp is not None or regime_exp is not None:
            values = [v for v in (setup_exp, regime_exp) if v is not None]
            inferred_expectancy = sum(values) / len(values)
            decision.expectancy = inferred_expectancy
            ctx.market_ctx = {**ctx.market_ctx, "expectancy": inferred_expectancy}

    quality = evaluate_trade_quality(decision, ctx.market_ctx, recent_stats, config)
    if not quality.accepted:
        ctx.diagnostics.update(quality.diagnostics)
        reason = quality.reject_reason or "UNKNOWN"
        _audit(ctx, decision, LifecycleState.SIGNAL_CREATED, LifecycleState.SIGNAL_REJECTED, reason)
        payload = _rejected_cycle_result(reason, candidate=decision, diagnostics=quality.diagnostics)
        payload["accepted"] = False
        payload["reason"] = reason
        payload["reject_reason"] = reason
        return payload

    execution_ctx = ctx.market_ctx.get("execution_ctx")
    if not isinstance(execution_ctx, Mapping):
        execution_ctx = build_execution_context(ctx.market_ctx)
    order_payload = {"risk_reward": decision.rr, "rr": decision.rr}
    effective_rr, execution_flags, rr_breakdown = _effective_rr(order_payload, execution_ctx, mode="paper", min_effective_rr=float(config.get("MIN_EFFECTIVE_RR", MIN_RR_THRESHOLD)))
    diagnostics = {
        **quality.diagnostics,
        "effective_rr": effective_rr,
        "execution_flags": execution_flags,
        "effective_rr_breakdown": rr_breakdown,
    }
    if execution_flags:
        reason = "LOW_EFFECTIVE_RR" if "LOW_EFFECTIVE_RR" in execution_flags else execution_flags[0]
        ctx.diagnostics.update(diagnostics)
        _audit(ctx, decision, LifecycleState.SIGNAL_CREATED, LifecycleState.SIGNAL_REJECTED, reason)
        payload = _rejected_cycle_result(reason, candidate=decision, diagnostics=diagnostics)
        payload["accepted"] = False
        payload["reason"] = reason
        payload["reject_reason"] = reason
        return payload

    ctx.diagnostics.update(diagnostics)
    execution = execute_order_candidate(decision, ctx)
    return {"status": "executed", "accepted": True, "candidate": decision, "reason": "", "reject_reason": "", "rejection_reason": "", "execution": execution, "diagnostics": diagnostics}


# Existing functions kept below

def before_virtual_order(session: Session, candidate: Mapping[str, Any], market_ctx: Mapping[str, Any], regime_ctx: Mapping[str, Any], stats_ctx: Mapping[str, Any], *, ai_enabled: bool = True) -> dict[str, Any] | None:
    if not ai_enabled:
        return dict(candidate)
    brain = AIBrain(session)
    signal = _signal_adapter(candidate)
    score, plan, explanation = brain.before_virtual_order(signal, market_ctx, regime_ctx, stats_ctx)
    signal_id = save_signal(session, **signal)
    virtual_exec_ctx = normalize_execution_ctx((market_ctx or {}).get("execution_ctx"))
    try:
        decision_id = save_order_decision(session, signal_id=signal_id, phase="virtual", decision=plan.decision, order_type=plan.order_type, confidence=score.total_score, explanation=explanation, order_payload={"limit_price": plan.limit_price, "stop_price": plan.stop_price, "execution_ctx": virtual_exec_ctx}, expected_slippage_pct=virtual_exec_ctx["expected_slippage_pct"], effective_rr=float(candidate.get("risk_reward", 1.0) or 1.0))
        if decision_id:
            save_ai_decision_features(session, decision_id=decision_id, features=score.components, penalties=score.penalties, reason_flags=score.reason_flags, execution_features=virtual_exec_ctx)
        save_trade_lifecycle_event(session, signal_id=signal_id, event_type=f"before_virtual_{plan.decision.lower()}", payload={"order_type": plan.order_type})
    except Exception as exc:
        logger.warning("Persist failed: %s", exc)
    order = dict(candidate)
    order.update({"ai_score": score.total_score, "confidence_band": _band(score.total_score), "position_size_mult": _position_mult(score.total_score), "ai_reason": explanation, "ai_flags": score.reason_flags, "ai_order_type": plan.order_type})
    if plan.decision == "REJECTED":
        order["ai_rejected"] = True
        order["reject_reason"] = explanation
    return order

# (rest unchanged omitted for brevity in this rewrite)

def before_real_order(session: Session, order: Mapping[str, Any], market_ctx: Mapping[str, Any], regime_ctx: Mapping[str, Any], stats_ctx: Mapping[str, Any], *, fail_closed_live: bool = True, mode: str = "live") -> tuple[bool, dict[str, Any]]:
    brain = AIBrain(session)
    ctx = dict(market_ctx) if isinstance(market_ctx, Mapping) else {}
    payload = normalize_execution_payload({}, order=order, ctx=ctx)
    execution_ctx_raw = ctx.get("execution_ctx") if isinstance(ctx, dict) else None
    if not execution_ctx_raw:
        payload["execution_ctx_missing"] = True
        if "EXECUTION_CTX_MISSING" not in payload["execution_flags"]:
            payload["execution_flags"].append("EXECUTION_CTX_MISSING")
    try:
        signal = _signal_adapter(order)
        execution_ctx, missing_execution_ctx = _resolve_execution_ctx(market_ctx)
        ctx["execution_ctx"] = execution_ctx
        enriched_market_ctx = {**ctx, **execution_ctx}

        score, plan, explanation = brain.before_real_order(signal, enriched_market_ctx, regime_ctx, stats_ctx)
        min_eff = float(ctx.get("MIN_EFFECTIVE_RR", market_ctx.get("MIN_EFFECTIVE_RR", MIN_RR_THRESHOLD)) if isinstance(market_ctx, Mapping) else MIN_RR_THRESHOLD)
        effective_rr, execution_flags, rr_breakdown = _effective_rr(order, execution_ctx, mode=mode, min_effective_rr=min_eff)
        if missing_execution_ctx:
            execution_flags.append("EXECUTION_CTX_MISSING")
            execution_flags.append("UNKNOWN_EXECUTION_CONTEXT")
            effective_rr = round(max(float(order.get("risk_reward", 1.0) or 1.0) - 0.6, 0.0), 6)
        blocked = _is_blocked(score, regime_ctx, stats_ctx) or effective_rr < min_eff

        qty = float(order.get("quantity", 0.0)) * _position_mult(score.total_score)
        slippage_penalty_factor = min(float(execution_ctx.get("expected_slippage_pct") or 0.0) * 10.0, 0.9)
        qty *= max(0.0, 1.0 - slippage_penalty_factor)

        payload.update(dict(order))
        payload["quantity"] = max(qty, 0.0)
        payload.update({"ai_score": score.total_score, "ai_reason": explanation, "effective_rr": round(effective_rr, 6), "effective_rr_breakdown": rr_breakdown, "expected_slippage_pct": execution_ctx["expected_slippage_pct"], "execution_flags": execution_flags, "execution_ctx": execution_ctx, "execution_ctx_missing": missing_execution_ctx, "execution_metrics": {**rr_breakdown, "execution_cost_completeness": rr_breakdown.get("execution_cost_completeness"), "missing_fields": rr_breakdown.get("missing_fields", [])}, "adjusted_risk_reward": round(effective_rr, 6), "block_reason": "QUALITY_BLOCKED" if blocked else "", "reject_reason": "QUALITY_BLOCKED" if blocked else ""})
        payload = normalize_execution_payload(payload, order=order, ctx={"execution_ctx_missing": missing_execution_ctx})
        if blocked and payload["execution_flags"]:
            payload["block_reason"] = payload["execution_flags"][0]
            payload["reject_reason"] = "|".join(payload["execution_flags"])
        signal_id = save_signal(session, **signal)
        decision_id = save_order_decision(session, signal_id=signal_id, phase="real", decision="REJECTED" if blocked else plan.decision, order_type=plan.order_type, confidence=score.total_score, explanation=explanation, order_payload=payload, expected_slippage_pct=execution_ctx["expected_slippage_pct"], effective_rr=round(effective_rr, 6))
        if decision_id:
            save_ai_decision_features(session, decision_id=decision_id, features=score.components, penalties=score.penalties, reason_flags=score.reason_flags, execution_features=execution_ctx)
        save_trade_lifecycle_event(session, signal_id=signal_id, event_type="before_real_blocked" if blocked else "before_real_allowed", payload={"reason_flags": score.reason_flags, "execution_flags": execution_flags})
        return (not blocked, normalize_execution_payload(payload, order=order, ctx={"execution_ctx_missing": missing_execution_ctx}))
    except Exception as exc:
        logger.warning("AI real-order check failed: %s", exc)
        missing_execution_ctx = _resolve_execution_ctx(market_ctx)[1]
        safe_payload = normalize_execution_payload(payload, order=order, ctx={"execution_ctx_missing": missing_execution_ctx})
        safe_payload.setdefault("execution_flags", [])
        if missing_execution_ctx and "EXECUTION_CTX_MISSING" not in safe_payload["execution_flags"]:
            safe_payload["execution_flags"].append("EXECUTION_CTX_MISSING")
        if any((market_ctx or {}).get("execution_ctx", {}).get(k) in (None, "", "UNKNOWN", "UNAVAILABLE", "UNAVAILABLE_BACKTEST") for k in ("spread_pct", "expected_slippage_pct", "latency_ms", "funding_rate_pct", "liquidity_score")):
            safe_payload["execution_flags"].append("UNKNOWN_EXECUTION_CONTEXT")
            safe_payload["effective_rr"] = round(max(float(order.get("risk_reward", 1.0) or 1.0) - 0.6, 0.0), 6)
        safe_payload["execution_flags"] = sorted(set(safe_payload["execution_flags"]))
        return (False if (mode == "live" and fail_closed_live) else True, safe_payload)


def after_position_close(session: Session, closed_trade: Mapping[str, Any], replay_ctx: Mapping[str, Any]) -> None:
    brain = AIBrain(session)
    brain.after_position_close(closed_trade, replay_ctx)
    pnl = float(closed_trade.get("pnl", 0.0))
    execution_metrics = _execution_review(closed_trade)
    save_closed_trade_review(
        session,
        trade_id=str(closed_trade.get("trade_id", "")),
        symbol=str(closed_trade.get("symbol", "unknown")),
        review_payload={"closed_trade": dict(closed_trade), "replay_ctx": dict(replay_ctx)},
        execution_metrics=execution_metrics,
    )
    upsert_expectancy_stats(session, "setup_expectancy_stats", "setup", str(closed_trade.get("setup", "unknown")), pnl)
    upsert_expectancy_stats(session, "regime_expectancy_stats", "regime", str(closed_trade.get("regime", "unknown")), pnl)
    upsert_expectancy_stats(session, "symbol_expectancy_stats", "symbol", str(closed_trade.get("symbol", "unknown")), pnl)
    save_trade_lifecycle_event(session, signal_id=None, event_type="after_position_close", payload={"trade_id": closed_trade.get("trade_id"), "execution_metrics": execution_metrics})


def _resolve_execution_ctx(market_ctx: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    raw = market_ctx.get("execution_ctx")
    if isinstance(raw, Mapping):
        if len(raw) == 0:
            return normalize_execution_ctx(neutral_execution_context()), True
        required = ("spread_pct", "expected_slippage_pct", "latency_ms", "funding_rate_pct", "liquidity_score")
        missing_raw = any(raw.get(k) in (None, "", "UNKNOWN", "UNAVAILABLE", "UNAVAILABLE_BACKTEST") for k in required)
        return normalize_execution_ctx(raw), missing_raw
    if market_ctx:
        return normalize_execution_ctx(build_execution_context(market_ctx)), False
    return normalize_execution_ctx(neutral_execution_context()), True


def _effective_rr(order: Mapping[str, Any], execution_ctx: Mapping[str, Any], *, mode: str = "live", min_effective_rr: float | None = None) -> tuple[float, list[str], dict[str, Any]]:
    rr = float(order.get("risk_reward", 1.0) or 1.0)
    require_measured = str(mode).upper() in {"LIVE", "LIVE_PRECHECK", "PAPER"}
    evidence_status = classify_execution_evidence(execution_ctx, require_measured=require_measured)
    result = calculate_effective_rr(rr, execution_ctx, include_missing_penalty=False)
    model = build_execution_cost_model(execution_ctx, include_missing_penalty=False)
    effective = result.effective_rr
    breakdown = result.as_dict()
    breakdown["execution_evidence_status"] = evidence_status

    flags: list[str] = []
    if model.missing_fields or evidence_status == EXECUTION_EVIDENCE_UNAVAILABLE_BLOCKING:
        flags.append("UNKNOWN_EXECUTION_CONTEXT")
    if evidence_status == EXECUTION_EVIDENCE_INVALID_FAKE_ZERO:
        flags.append("INVALID_FAKE_ZERO")
    if model.spread_penalty >= 0.20:
        flags.append("HIGH_SPREAD")
    if model.slippage_penalty >= 0.20:
        flags.append("HIGH_SLIPPAGE")
    if model.liquidity_penalty >= 0.35:
        flags.append("THIN_LIQUIDITY")
    if model.funding_penalty >= 0.08:
        flags.append("FUNDING_RISK")
    if model.latency_penalty >= 0.05:
        flags.append("BAD_EXECUTION")
    if str(execution_ctx.get("volatility_regime", "")).lower() == "high" and (model.spread_penalty + model.slippage_penalty) >= 0.25:
        flags.append("EXCESSIVE_VOLATILITY")
    threshold = MIN_RR_THRESHOLD if min_effective_rr is None else float(min_effective_rr)
    breakdown["min_effective_rr"] = threshold
    if effective < threshold:
        flags.append("LOW_EFFECTIVE_RR")
    return effective, sorted(set(flags)), breakdown


def _execution_review(closed_trade: Mapping[str, Any]) -> dict[str, float]:
    expected = abs(float(closed_trade.get("expected_slippage_pct", 0.0) or 0.0))
    fill_quality = 1.0

    try:
        entry = float(closed_trade.get("entry_price", 0.0) or 0.0)
        filled = float(closed_trade.get("filled_entry_price", entry) or entry)
        realized = abs(filled - entry) / entry if entry > 0 else 0.0
    except (TypeError, ValueError):
        entry = 0.0
        filled = 0.0
        realized = 0.0

    realized = abs(realized)
    slippage_delta = max(0.0, realized - expected)
    fill_quality = max(0.0, min(1.0, 1.0 - slippage_delta * 100.0))
    return {
        "entry_price": entry,
        "filled_entry_price": filled,
        "expected_slippage_pct": expected,
        "realized_slippage_pct": realized,
        "fill_quality_score": fill_quality,
    }


def _signal_adapter(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "symbol": str(payload.get("symbol", "UNKNOWN")),
        "side": str(payload.get("side", "BUY")),
        "timeframe": str(payload.get("timeframe", "NA")),
        "entry_price": float(payload.get("entry_price", payload.get("price", 0.0)) or 0.0),
        "risk_reward": float(payload.get("risk_reward", 1.0) or 1.0),
        "setup_quality": float(payload.get("setup_quality", 0.5) or 0.5),
        "setup": str(payload.get("setup", "unknown")),
        "breakout": bool(payload.get("breakout", False)),
    }


def _band(score: float) -> str:
    if score >= 0.90:
        return "AGGRESSIVE"
    if score >= 0.75:
        return "NORMAL"
    if score >= 0.60:
        return "REDUCED"
    return "REJECT"


def _position_mult(score: float) -> float:
    return {"AGGRESSIVE": 1.2, "NORMAL": 1.0, "REDUCED": 0.6, "REJECT": 0.0}[_band(score)]


def _is_blocked(score: Any, regime_ctx: Mapping[str, Any], stats_ctx: Mapping[str, Any]) -> bool:
    if _band(score.total_score) == "REJECT":
        return True
    if "negative_expectancy_risk" in score.reason_flags:
        return True
    if bool(regime_ctx.get("stale", False)):
        return True
    if float(stats_ctx.get("cooldown_remaining_sec", 0) or 0) > 0:
        return True
    return False
