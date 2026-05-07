from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping

from sqlalchemy.orm import Session

from alphaforge.ai_brain import AIBrain
from alphaforge.execution import build_execution_context, neutral_execution_context
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
MIN_RR_THRESHOLD = 1.1
MIN_SCORE_BASE = 0.75
MIN_RR_BASE = 1.3


def normalize_execution_ctx(ctx: Mapping[str, Any] | None) -> dict[str, Any]:
    base = dict(ctx or {})
    return {
        "expected_slippage_pct": float(base.get("expected_slippage_pct", 0.0) or 0.0),
        "spread_pct": float(base.get("spread_pct", 0.0) or 0.0),
        "latency_ms": int(base.get("latency_ms", 0) or 0),
        "orderbook_imbalance": float(base.get("orderbook_imbalance", 0.0) or 0.0),
        "funding_rate_pct": float(base.get("funding_rate_pct", 0.0) or 0.0),
        "volatility_regime": str(base.get("volatility_regime", "unknown") or "unknown"),
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
    adaptive = compute_adaptive_thresholds(recent_stats)
    cfg = {
        "MIN_TRADE_SCORE": adaptive["min_score"], "MIN_RR": adaptive["min_rr"], "MIN_EXPECTANCY": 0.0, "SYMBOL_COOLDOWN_MINUTES": 60,
        "MAX_TRADES_PER_SYMBOL_PER_DAY": 2, "MAX_TRADES_GLOBAL_PER_DAY": 10, "MIN_SL_PCT": 0.15, "MAX_SL_PCT": 1.5,
        "MAX_SPREAD_PCT": 0.05, "MAX_EXPECTED_SLIPPAGE_PCT": 0.05, "MIN_ATR_PCT": 0.25, "MAX_ATR_PCT": 3.0,
        "SYMBOL_LOSS_STREAK_LIMIT": 3, "SYMBOL_LOSS_STREAK_COOLDOWN_HOURS": 6, "GLOBAL_LOSS_STREAK_LIMIT": 5,
        "GLOBAL_LOSS_STREAK_COOLDOWN_HOURS": 3, "BLOCK_UNKNOWN_EXPECTANCY": True, "BLOCK_CHOP_MARKET": True, "REQUIRE_REGIME_ALIGNMENT": True,
    }
    cfg.update(dict(config or {}))
    reject_reason = ""
    failed_filter = ""
    symbol = getattr(candidate, "symbol", "")
    side = getattr(candidate, "side", "")
    setup_type = str(getattr(candidate, "setup_type", "") or "")
    setup_reason = str(getattr(candidate, "setup_reason", "") or "")
    regime = str(getattr(candidate, "regime", "") or market_ctx.get("regime", "UNKNOWN"))
    volatility_regime = str(market_ctx.get("volatility_regime", "unknown"))
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
    pattern_flags = [str(f).upper() for f in (market_ctx.get("pattern_flags", []) or [])]
    # compute quality score first
    score_comp = max(0.0, min(1.0, score / float(cfg["MIN_TRADE_SCORE"]))) * 25
    exp_comp = 0.0 if expectancy_val is None else max(0.0, min(1.0, (expectancy_val - float(cfg["MIN_EXPECTANCY"])) / 0.5)) * 25
    rr_comp = max(0.0, min(1.0, rr / float(cfg["MIN_RR"]))) * 10
    regime_ok = True
    if "TREND_CONTINUATION" in setup_type or "PULLBACK_" in setup_type:
        regime_ok = regime == "TREND"
    elif "BREAKOUT_UP" in setup_type or "BREAKOUT_DOWN" in setup_type:
        regime_ok = regime in {"TREND", "BREAKOUT"} and volatility_regime.lower() in {"normal", "high"}
    elif "RANGE_MEAN_REVERSION" in setup_type:
        regime_ok = regime == "RANGE"
    regime_comp = (20.0 if regime_ok else 0.0)
    micro_ok = spread_pct <= float(cfg["MAX_SPREAD_PCT"]) and expected_slippage_pct <= float(cfg["MAX_EXPECTED_SLIPPAGE_PCT"])
    vol_ok = atr_pct is None or (float(cfg["MIN_ATR_PCT"]) <= atr_pct <= float(cfg["MAX_ATR_PCT"]))
    vol_comp = (10.0 if (micro_ok and vol_ok) else 0.0)
    hygiene_comp = 10.0
    quality_score = round(score_comp + exp_comp + rr_comp + regime_comp + vol_comp + hygiene_comp, 2)
    if not candidate or not getattr(candidate, "symbol", None):
        reject_reason, failed_filter = "INVALID_CANDIDATE", "candidate"
    elif score < float(cfg["MIN_TRADE_SCORE"]):
        reject_reason, failed_filter = "LOW_SCORE", "score"
    elif rr < float(cfg["MIN_RR"]):
        reject_reason, failed_filter = "RR_TOO_LOW", "rr"
    elif cfg["BLOCK_UNKNOWN_EXPECTANCY"] and expectancy_val is None:
        reject_reason, failed_filter = "EXPECTANCY_MISSING", "expectancy"
    elif expectancy_val is not None and expectancy_val < float(cfg["MIN_EXPECTANCY"]):
        reject_reason, failed_filter = "NEGATIVE_EXPECTANCY", "expectancy"
    elif cfg["BLOCK_CHOP_MARKET"] and any("CHOP" in f for f in pattern_flags):
        reject_reason, failed_filter = "CHOP_MARKET_BLOCK", "pattern_flags"
    elif cfg["REQUIRE_REGIME_ALIGNMENT"] and not regime_ok:
        reject_reason, failed_filter = "REGIME_MISMATCH", "regime"
    elif sl_pct < float(cfg["MIN_SL_PCT"]):
        reject_reason, failed_filter = "STOP_TOO_TIGHT", "sl_pct"
    elif sl_pct > float(cfg["MAX_SL_PCT"]):
        reject_reason, failed_filter = "STOP_TOO_WIDE", "sl_pct"
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
        if last_ts and now_ts and (now_ts - last_ts) < int(cfg["SYMBOL_COOLDOWN_MINUTES"]) * 60_000:
            reject_reason, failed_filter = "SYMBOL_COOLDOWN_ACTIVE", "cooldown"
        elif int((recent_stats.get("trades_today_by_symbol", {}) or {}).get(symbol, 0) or 0) >= int(cfg["MAX_TRADES_PER_SYMBOL_PER_DAY"]):
            reject_reason, failed_filter = "DAILY_SYMBOL_TRADE_LIMIT", "daily_symbol"
        elif int(recent_stats.get("global_trades_today", 0) or 0) >= int(cfg["MAX_TRADES_GLOBAL_PER_DAY"]):
            reject_reason, failed_filter = "DAILY_GLOBAL_TRADE_LIMIT", "daily_global"
        elif int((recent_stats.get("symbol_loss_block_until", {}) or {}).get(symbol, 0) or 0) > now_ts:
            reject_reason, failed_filter = "SYMBOL_LOSS_STREAK_BLOCK", "symbol_block"
        elif int(recent_stats.get("global_loss_block_until", 0) or 0) > now_ts:
            reject_reason, failed_filter = "GLOBAL_LOSS_STREAK_BLOCK", "global_block"
    diagnostics = {"symbol": symbol, "side": side, "setup_type": setup_type, "setup_reason": setup_reason, "score": score, "rr": rr, "expectancy": expectancy_val, "regime": regime, "volatility_regime": volatility_regime, "sl_pct": sl_pct, "spread_pct": spread_pct, "expected_slippage_pct": expected_slippage_pct, "atr_pct": atr_pct, "reject_reason": reject_reason, "failed_filter": failed_filter, "quality_score": quality_score, "adaptive_thresholds": adaptive}
    return TradeQualityDecision(accepted=(reject_reason == ""), reject_reason=reject_reason, quality_score=quality_score, diagnostics=diagnostics)


def compute_adaptive_thresholds(stats: Mapping[str, Any]) -> dict[str, float]:
    min_score = MIN_SCORE_BASE
    min_rr = MIN_RR_BASE
    consecutive_sl = int(stats.get("consecutive_sl_count", 0) or 0)
    consecutive_tp = int(stats.get("consecutive_tp_count", 0) or 0)

    if consecutive_sl >= 5:
        min_score = MIN_SCORE_BASE + 1.5
        min_rr = MIN_RR_BASE + 0.5
    elif consecutive_sl >= 3:
        min_score = MIN_SCORE_BASE + 1.0
        min_rr = MIN_RR_BASE + 0.3

    if consecutive_tp >= 5:
        min_score = MIN_SCORE_BASE - 1.0
        min_rr = MIN_RR_BASE - 0.3
    elif consecutive_tp >= 3:
        min_score = MIN_SCORE_BASE - 0.5
        min_rr = MIN_RR_BASE - 0.2

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


def run_order_cycle(ctx: OrderExecutionContext, config: Mapping[str, Any] | None = None, recent_stats: Mapping[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    recent_stats = recent_stats or {}
    decision = build_order_candidate(ctx.symbol, ctx.market_ctx, config)
    if isinstance(decision, OrderRejection):
        _audit(ctx, None, LifecycleState.SIGNAL_CREATED, LifecycleState.SIGNAL_REJECTED, decision.reject_reason)
        return {"status": "rejected", "candidate": None, "reason": decision.reject_reason, "rejection_reason": decision.reject_reason, "execution": None}
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
        _audit(ctx, decision, LifecycleState.SIGNAL_CREATED, LifecycleState.SIGNAL_REJECTED, quality.reject_reason)
        return {"status": "rejected", "candidate": decision, "reason": quality.reject_reason, "rejection_reason": quality.reject_reason, "execution": None, "diagnostics": quality.diagnostics}
    execution = execute_order_candidate(decision, ctx)
    return {"status": "executed", "candidate": decision, "rejection_reason": "", "execution": execution}

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
    if plan.decision == "REJECTED":
        return None
    order = dict(candidate)
    order.update({"ai_score": score.total_score, "confidence_band": _band(score.total_score), "position_size_mult": _position_mult(score.total_score), "ai_reason": explanation, "ai_flags": score.reason_flags, "ai_order_type": plan.order_type})
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
        effective_rr, execution_flags = _effective_rr(order, execution_ctx)
        if missing_execution_ctx:
            execution_flags.append("EXECUTION_CTX_MISSING")
        blocked = _is_blocked(score, regime_ctx, stats_ctx) or effective_rr < MIN_RR_THRESHOLD

        qty = float(order.get("quantity", 0.0)) * _position_mult(score.total_score)
        slippage_penalty_factor = min(execution_ctx["expected_slippage_pct"] * 10.0, 0.9)
        qty *= max(0.0, 1.0 - slippage_penalty_factor)

        payload.update(dict(order))
        payload["quantity"] = max(qty, 0.0)
        payload.update({"ai_score": score.total_score, "ai_reason": explanation, "effective_rr": round(effective_rr, 6), "expected_slippage_pct": execution_ctx["expected_slippage_pct"], "execution_flags": execution_flags, "execution_ctx": execution_ctx, "execution_ctx_missing": missing_execution_ctx, "execution_metrics": {}, "adjusted_risk_reward": round(effective_rr, 6), "block_reason": "QUALITY_BLOCKED" if blocked else "", "reject_reason": "QUALITY_BLOCKED" if blocked else ""})
        payload = normalize_execution_payload(payload, order=order, ctx={"execution_ctx_missing": missing_execution_ctx})
        if "HIGH_SLIPPAGE" in payload["execution_flags"]:
            payload["block_reason"] = "HIGH_SLIPPAGE"
            payload["reject_reason"] = "HIGH_SLIPPAGE"
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
        return normalize_execution_ctx(raw), False
    if market_ctx:
        return normalize_execution_ctx(build_execution_context(market_ctx)), False
    return normalize_execution_ctx(neutral_execution_context()), True


def _effective_rr(order: Mapping[str, Any], execution_ctx: Mapping[str, Any]) -> tuple[float, list[str]]:
    rr = float(order.get("risk_reward", 1.0) or 1.0)
    slippage = float(execution_ctx.get("expected_slippage_pct", 0.0) or 0.0)
    effective = rr * (1 - (slippage + float(execution_ctx.get("spread_pct", 0.0) or 0.0)) * 100)
    flags = []
    if slippage >= 0.02:
        flags.append("HIGH_SLIPPAGE")
    if float(execution_ctx.get("spread_pct", 0.0) or 0.0) > 0.002:
        flags.append("LOW_LIQUIDITY")
    if float(execution_ctx.get("funding_rate_pct", 0.0) or 0.0) > 0.03:
        flags.append("FUNDING_UNFAVORABLE")
    return effective, flags


def _execution_review(closed_trade: Mapping[str, Any]) -> dict[str, float]:
    expected = float(closed_trade.get("expected_slippage_pct", 0.0) or 0.0)
    entry = float(closed_trade.get("entry_price", 0.0) or 0.0)
    filled = float(closed_trade.get("filled_entry_price", entry) or entry)
    realized = abs(filled - entry) / entry if entry > 0 else 0.0
    diff = realized - expected
    fill_quality = max(0.0, min(1.0, 1.0 - abs(diff) * 10.0))
    return {"realized_slippage_pct": realized, "entry_expected_diff_pct": diff, "fill_quality_score": fill_quality}


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
