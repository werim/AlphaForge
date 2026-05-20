from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5


@dataclass(frozen=True)
class SignalCandidate:
    signal_id: str
    symbol: str
    side: str
    setup_type: str
    setup_reason: str
    regime: str
    timestamp: int
    entry: float
    stop_loss: float
    take_profit: float
    raw_rr: float
    heuristic_score: float
    features: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProbabilityDecision:
    p_fill: float
    p_tp_before_sl: float
    p_sl_before_tp: float
    p_timeout: float
    p_ambiguous: float
    expected_hold_minutes: float
    expected_mfe_r: float
    expected_mae_r: float
    effective_rr: float
    breakeven_probability: float
    probability_edge: float
    cost_penalty_r: float
    funding_penalty_r: float
    opportunity_cost_r: float
    expected_r: float
    decision: str
    reject_reason: str


@dataclass(frozen=True)
class OrderPlan:
    signal_id: str
    lifecycle_id: str
    order_id: str
    position_id: str
    symbol: str
    side: str
    order_type: str
    entry: float
    stop_loss: float
    take_profit: float
    quantity: float
    notional: float
    risk_usdt: float
    effective_rr: float
    expected_r: float
    probability_edge: float


def evaluate_signal_to_order(candidate: SignalCandidate, market_ctx: Mapping[str, Any], regime_ctx: Mapping[str, Any], stats_ctx: Mapping[str, Any]) -> tuple[ProbabilityDecision, OrderPlan | None]:
    cfg = {
        "min_p_fill": float(stats_ctx.get("min_p_fill", 0.40) or 0.40),
        "min_probability_edge": float(stats_ctx.get("min_probability_edge", 0.0) or 0.0),
        "min_expected_r": float(stats_ctx.get("min_expected_r", 0.0) or 0.0),
        "max_p_timeout": float(stats_ctx.get("max_p_timeout", 0.45) or 0.45),
        "timeout_penalty_r": float(stats_ctx.get("timeout_penalty_r", 0.25) or 0.25),
        "max_hold_minutes_default": float(stats_ctx.get("max_hold_minutes_default", 240.0) or 240.0),
    }
    setup_holds = stats_ctx.get("setup_max_hold_minutes", {}) if isinstance(stats_ctx.get("setup_max_hold_minutes", {}), dict) else {}
    max_hold = float(setup_holds.get(candidate.setup_type, cfg["max_hold_minutes_default"]) or cfg["max_hold_minutes_default"])

    score01 = max(0.0, min(1.0, candidate.heuristic_score / 10.0 if candidate.heuristic_score > 1.0 else candidate.heuristic_score))
    liq = float(market_ctx.get("liquidity_score", 0.5) or 0.5)
    spread = abs(float(market_ctx.get("spread_pct", 0.0) or 0.0))
    slip = abs(float(market_ctx.get("expected_slippage_pct", 0.0) or 0.0))
    regime_align = float(regime_ctx.get("alignment", 0.5) or 0.5)

    p_fill = max(0.0, min(1.0, 0.35 + 0.35 * score01 + 0.20 * liq - min(0.2, spread)))
    p_tp_before_sl = max(0.0, min(1.0, 0.20 + 0.45 * score01 + 0.20 * regime_align))
    p_sl_before_tp = max(0.0, min(1.0, 0.20 + 0.25 * (1.0 - score01) + 0.20 * max(0.0, spread)))
    p_timeout = max(0.0, min(1.0, 1.0 - (p_tp_before_sl + p_sl_before_tp) * 0.85))
    p_ambiguous = max(0.0, min(1.0, float(market_ctx.get("ambiguity_probability", 0.03) or 0.03)))
    expected_hold_minutes = float(market_ctx.get("expected_hold_minutes", 20.0 + (1.0 - score01) * 80.0))
    effective_rr = max(0.0, float(candidate.raw_rr) - (spread * 2.0 + slip * 10.0))
    cost_penalty_r = max(0.0, spread * 1.2 + slip * 6.0)
    funding_penalty_r = max(0.0, abs(float(market_ctx.get("funding_rate_pct", 0.0) or 0.0)) * 0.5)
    opportunity_cost_r = max(0.0, float(stats_ctx.get("opportunity_cost_r", 0.05) or 0.05))
    breakeven_probability = 1.0 / (1.0 + max(effective_rr, 1e-9))
    probability_edge = p_tp_before_sl - breakeven_probability
    expected_r = (
        p_tp_before_sl * effective_rr
        - p_sl_before_tp * 1.0
        - p_timeout * cfg["timeout_penalty_r"]
        - cost_penalty_r
        - funding_penalty_r
        - opportunity_cost_r
    )

    reject_reason = ""
    decision = "ACCEPTED"
    if p_fill < cfg["min_p_fill"]:
        decision, reject_reason = "REJECTED", "LOW_FILL_PROBABILITY"
    elif probability_edge < cfg["min_probability_edge"]:
        decision, reject_reason = "REJECTED", "LOW_PROBABILITY_EDGE"
    elif expected_r <= cfg["min_expected_r"]:
        decision, reject_reason = "REJECTED", "NEGATIVE_EXPECTANCY"
    elif p_timeout > cfg["max_p_timeout"]:
        decision, reject_reason = "REJECTED", "HIGH_TIMEOUT_PROBABILITY"
    elif expected_hold_minutes > max_hold:
        decision, reject_reason = "REJECTED", "HOLD_TOO_LONG"

    pd = ProbabilityDecision(
        p_fill=p_fill,
        p_tp_before_sl=p_tp_before_sl,
        p_sl_before_tp=p_sl_before_tp,
        p_timeout=p_timeout,
        p_ambiguous=p_ambiguous,
        expected_hold_minutes=expected_hold_minutes,
        expected_mfe_r=max(0.0, effective_rr * p_tp_before_sl),
        expected_mae_r=max(0.0, p_sl_before_tp),
        effective_rr=effective_rr,
        breakeven_probability=breakeven_probability,
        probability_edge=probability_edge,
        cost_penalty_r=cost_penalty_r,
        funding_penalty_r=funding_penalty_r,
        opportunity_cost_r=opportunity_cost_r,
        expected_r=expected_r,
        decision=decision,
        reject_reason=reject_reason,
    )
    if decision != "ACCEPTED" or expected_r <= 0.0 or probability_edge <= 0.0:
        return pd, None

    plan = OrderPlan(
        signal_id=candidate.signal_id,
        lifecycle_id=str(uuid5(NAMESPACE_URL, f"contract:lifecycle:{candidate.signal_id}")),
        order_id=str(uuid5(NAMESPACE_URL, f"contract:order:{candidate.signal_id}:{candidate.entry}:{candidate.stop_loss}:{candidate.take_profit}")),
        position_id=str(uuid5(NAMESPACE_URL, f"contract:position:{candidate.signal_id}:{candidate.side}")),
        symbol=candidate.symbol,
        side=candidate.side,
        order_type=str(candidate.features.get("order_type", market_ctx.get("order_type", "LIMIT"))),
        entry=candidate.entry,
        stop_loss=candidate.stop_loss,
        take_profit=candidate.take_profit,
        quantity=float(candidate.features.get("quantity", 0.0) or 0.0),
        notional=float(candidate.features.get("notional", 0.0) or 0.0),
        risk_usdt=float(candidate.features.get("risk_usdt", 0.0) or 0.0),
        effective_rr=effective_rr,
        expected_r=expected_r,
        probability_edge=probability_edge,
    )
    return pd, plan
