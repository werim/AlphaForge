from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping

from sqlalchemy.orm import Session

from alphaforge.ai_brain import AIBrain
from alphaforge.execution import build_execution_context, neutral_execution_context
from alphaforge.persistence import (
    save_ai_decision_features,
    save_closed_trade_review,
    save_order_decision,
    save_signal,
    save_trade_lifecycle_event,
    upsert_expectancy_stats,
)

logger = logging.getLogger(__name__)
MIN_RR_THRESHOLD = 1.1


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
    reason: str = ""
    quality_score: float = 0.0


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
    min_score = float(config.get("MIN_TRADE_SCORE", 0.6) or 0.6)
    min_rr = float(config.get("MIN_RR", 1.1) or 1.1)
    max_spread = float(config.get("MAX_SPREAD_PCT", 0.002) or 0.002)
    min_sl_pct = float(config.get("MIN_SL_PCT", 0.001) or 0.001)
    max_sl_pct = float(config.get("MAX_SL_PCT", 0.05) or 0.05)
    if candidate.score < min_score:
        return TradeQualityDecision(False, "SCORE_TOO_LOW", candidate.score)
    if candidate.expectancy is None:
        return TradeQualityDecision(False, "EXPECTANCY_MISSING", candidate.score)
    if candidate.expectancy <= 0:
        return TradeQualityDecision(False, "EXPECTANCY_NON_POSITIVE", candidate.score)
    if str(market_ctx.get("expected_regime", candidate.regime)) != candidate.regime:
        return TradeQualityDecision(False, "REGIME_MISMATCH", candidate.score)
    if candidate.rr < min_rr:
        return TradeQualityDecision(False, "RR_TOO_LOW", candidate.score)
    if float(market_ctx.get("spread_pct", 0.0) or 0.0) > max_spread:
        return TradeQualityDecision(False, "SPREAD_TOO_HIGH", candidate.score)
    sl_pct = abs(candidate.entry - candidate.sl) / candidate.entry
    if sl_pct < min_sl_pct:
        return TradeQualityDecision(False, "SL_TOO_TIGHT", candidate.score)
    if sl_pct > max_sl_pct:
        return TradeQualityDecision(False, "SL_TOO_WIDE", candidate.score)
    if bool(recent_stats.get("cooldown_active", False)):
        return TradeQualityDecision(False, "SYMBOL_COOLDOWN_ACTIVE", candidate.score)
    if bool(recent_stats.get("daily_trade_limit_hit", False)):
        return TradeQualityDecision(False, "DAILY_TRADE_LIMIT_HIT", candidate.score)
    if bool(recent_stats.get("loss_streak_circuit_breaker", False)):
        return TradeQualityDecision(False, "LOSS_STREAK_CIRCUIT_BREAKER", candidate.score)
    return TradeQualityDecision(True, quality_score=candidate.score)


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
    status = LifecycleState.ORDER_PLACED
    if ctx.mode == TradingMode.BACKTEST:
        result = {"type": "virtual", "candidate": candidate}
    elif ctx.mode == TradingMode.PAPER:
        result = {"type": "paper", "candidate": candidate, "paper_balance": ctx.balance}
    else:
        bal_fn: Callable[[], float] = ctx.storage["real_balance_fetcher"]
        ord_fn: Callable[[OrderCandidate], Mapping[str, Any]] = ctx.storage["binance_place_order"]
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
        return {"status": "rejected", "reason": decision.reject_reason}
    quality = evaluate_trade_quality(decision, ctx.market_ctx, recent_stats, config)
    if not quality.accepted:
        _audit(ctx, decision, LifecycleState.SIGNAL_CREATED, LifecycleState.SIGNAL_REJECTED, quality.reason)
        return {"status": "rejected", "reason": quality.reason}
    execution = execute_order_candidate(decision, ctx)
    return {"status": "executed", "execution": execution, "candidate": decision}

# Existing functions kept below

def before_virtual_order(session: Session, candidate: Mapping[str, Any], market_ctx: Mapping[str, Any], regime_ctx: Mapping[str, Any], stats_ctx: Mapping[str, Any], *, ai_enabled: bool = True) -> dict[str, Any] | None:
    if not ai_enabled:
        return dict(candidate)
    brain = AIBrain(session)
    signal = _signal_adapter(candidate)
    score, plan, explanation = brain.before_virtual_order(signal, market_ctx, regime_ctx, stats_ctx)
    signal_id = save_signal(session, **signal)
    decision_id = save_order_decision(session, signal_id=signal_id, phase="virtual", decision=plan.decision, order_type=plan.order_type, confidence=score.total_score, explanation=explanation, order_payload={"limit_price": plan.limit_price, "stop_price": plan.stop_price})
    if decision_id:
        save_ai_decision_features(session, decision_id=decision_id, features=score.components, penalties=score.penalties, reason_flags=score.reason_flags, execution_features={})
    save_trade_lifecycle_event(session, signal_id=signal_id, event_type=f"before_virtual_{plan.decision.lower()}", payload={"order_type": plan.order_type})
    if plan.decision == "REJECTED":
        return None
    order = dict(candidate)
    order.update({"ai_score": score.total_score, "confidence_band": _band(score.total_score), "position_size_mult": _position_mult(score.total_score), "ai_reason": explanation, "ai_flags": score.reason_flags, "ai_order_type": plan.order_type})
    return order

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
        payload.update({"ai_score": score.total_score, "ai_reason": explanation, "effective_rr": round(effective_rr, 10), "expected_slippage_pct": execution_ctx["expected_slippage_pct"], "execution_flags": execution_flags, "execution_ctx": execution_ctx, "execution_ctx_missing": missing_execution_ctx, "execution_metrics": {}, "adjusted_risk_reward": round(effective_rr, 10), "block_reason": "QUALITY_BLOCKED" if blocked else "", "reject_reason": "QUALITY_BLOCKED" if blocked else ""})
        payload = normalize_execution_payload(payload, order=order, ctx={"execution_ctx_missing": missing_execution_ctx})
        if "HIGH_SLIPPAGE" in payload["execution_flags"]:
            payload["block_reason"] = "HIGH_SLIPPAGE"
            payload["reject_reason"] = "HIGH_SLIPPAGE"
        signal_id = save_signal(session, **signal)
        decision_id = save_order_decision(session, signal_id=signal_id, phase="real", decision="REJECTED" if blocked else plan.decision, order_type=plan.order_type, confidence=score.total_score, explanation=explanation, order_payload=payload, expected_slippage_pct=execution_ctx["expected_slippage_pct"], effective_rr=effective_rr)
        if decision_id:
            save_ai_decision_features(session, decision_id=decision_id, features=score.components, penalties=score.penalties, reason_flags=score.reason_flags, execution_features=execution_ctx)
        save_trade_lifecycle_event(session, signal_id=signal_id, event_type="before_real_blocked" if blocked else "before_real_allowed", payload={"reason_flags": score.reason_flags, "execution_flags": execution_flags})
        return (not blocked, normalize_execution_payload(payload, order=order, ctx={"execution_ctx_missing": missing_execution_ctx}))
    except Exception as exc:
        logger.warning("AI real-order check failed: %s", exc)
        safe_payload = normalize_execution_payload(payload, order=order, ctx={"execution_ctx_missing": _resolve_execution_ctx(market_ctx)[1]})
        if safe_payload["execution_ctx_missing"] and "EXECUTION_CTX_MISSING" not in safe_payload["execution_flags"]:
            safe_payload["execution_flags"].append("EXECUTION_CTX_MISSING")
        if mode == "live" and fail_closed_live:
            return (False, normalize_execution_payload(safe_payload, order=order, ctx=ctx))
        return (True, normalize_execution_payload(safe_payload, order=order, ctx=ctx))


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
            return neutral_execution_context(), True
        return dict(raw), False
    if market_ctx:
        return build_execution_context(market_ctx), False
    return neutral_execution_context(), True


def _effective_rr(order: Mapping[str, Any], execution_ctx: Mapping[str, Any]) -> tuple[float, list[str]]:
    rr = float(order.get("risk_reward", 1.0) or 1.0)
    slippage_cost = float(execution_ctx.get("expected_slippage_pct", 0.0) or 0.0) * 100
    spread_cost = float(execution_ctx.get("spread_pct", 0.0) or 0.0) * 100
    effective = rr - slippage_cost - spread_cost
    flags = []
    if slippage_cost > 0.2:
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
