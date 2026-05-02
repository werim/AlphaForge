from __future__ import annotations

import logging
from typing import Any, Mapping

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
    try:
        signal = _signal_adapter(order)
        execution_ctx, missing_execution_ctx = _resolve_execution_ctx(market_ctx)
        enriched_market_ctx = {**dict(market_ctx), **execution_ctx}

        score, plan, explanation = brain.before_real_order(signal, enriched_market_ctx, regime_ctx, stats_ctx)
        effective_rr, execution_flags = _effective_rr(order, execution_ctx)
        if missing_execution_ctx:
            execution_flags.append("EXECUTION_CTX_MISSING")
        blocked = _is_blocked(score, regime_ctx, stats_ctx) or effective_rr < MIN_RR_THRESHOLD

        qty = float(order.get("quantity", 0.0)) * _position_mult(score.total_score)
        slippage_penalty_factor = min(execution_ctx["expected_slippage_pct"] * 10.0, 0.9)
        qty *= max(0.0, 1.0 - slippage_penalty_factor)

        payload = dict(order)
        payload["quantity"] = max(qty, 0.0)
        payload.update({"ai_score": score.total_score, "ai_reason": explanation, "effective_rr": effective_rr, "expected_slippage_pct": execution_ctx["expected_slippage_pct"], "execution_flags": execution_flags, "execution_ctx": execution_ctx})
        signal_id = save_signal(session, **signal)
        decision_id = save_order_decision(session, signal_id=signal_id, phase="real", decision="REJECTED" if blocked else plan.decision, order_type=plan.order_type, confidence=score.total_score, explanation=explanation, order_payload=payload, expected_slippage_pct=execution_ctx["expected_slippage_pct"], effective_rr=effective_rr)
        if decision_id:
            save_ai_decision_features(session, decision_id=decision_id, features=score.components, penalties=score.penalties, reason_flags=score.reason_flags, execution_features=execution_ctx)
        save_trade_lifecycle_event(session, signal_id=signal_id, event_type="before_real_blocked" if blocked else "before_real_allowed", payload={"reason_flags": score.reason_flags, "execution_flags": execution_flags})
        return (not blocked, payload)
    except Exception as exc:
        logger.warning("AI real-order check failed: %s", exc)
        if mode == "live" and fail_closed_live:
            return (False, dict(order))
        return (True, dict(order))


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
