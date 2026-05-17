from __future__ import annotations

from dataclasses import dataclass, field, asdict
from statistics import mean
from typing import Any


@dataclass
class DecisionReviewResult:
    decision_id: str
    symbol: str
    setup_type: str
    regime: str
    decision_type: str  # EXECUTED, SIGNAL_REJECTED, ORDER_REJECTED
    outcome: str  # TP_HIT, SL_HIT, OPEN_AT_END, REJECTED, MISSED_WINNER, AVOIDED_LOSER
    gross_pnl_pct: float
    net_pnl_pct: float
    effective_rr: float
    execution_cost_pct: float
    slippage_damage_pct: float
    spread_damage_pct: float
    latency_penalty: float
    reject_quality: str
    expectancy_bucket: str
    lessons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


def review_executed_trade(trade: dict[str, Any], market_after: dict[str, Any], config: dict[str, Any] | None = None) -> DecisionReviewResult:
    cfg = _merged_config(config)
    gross = float(trade.get("gross_pnl_pct", trade.get("pnl_pct", 0.0)) or 0.0)
    spread = float(trade.get("spread_cost_pct", market_after.get("spread_pct", 0.0)) or 0.0)
    slippage = float(trade.get("slippage_pct", market_after.get("slippage_pct", 0.0)) or 0.0)
    fees = float(trade.get("fees_pct", cfg["default_fees_pct"]) or 0.0)
    latency = float(trade.get("latency_ms", market_after.get("latency_ms", 0.0)) or 0.0)
    latency_penalty = (latency / 1000.0) * float(cfg["latency_penalty_per_second"])

    execution_cost = spread + slippage + fees
    net = gross - execution_cost - latency_penalty

    rr = _safe_rr(trade.get("entry"), trade.get("sl"), trade.get("tp"))
    realized_move = float(market_after.get("realized_move_pct", gross) or gross)
    effective_rr = realized_move / max(abs(float(trade.get("entry_risk_pct", 0.0) or _entry_risk_pct(trade))), 1e-9)

    warnings: list[str] = []
    lessons: list[str] = []

    expected_slippage = float(trade.get("expected_slippage_pct", cfg["expected_slippage_pct"]) or 0.0)
    if slippage > expected_slippage:
        warnings.append("SLIPPAGE_EXCEEDED_EXPECTED")

    expected_regime = str(trade.get("expected_regime", trade.get("regime", "UNKNOWN")))
    observed_regime = str(market_after.get("regime", expected_regime))
    if expected_regime != "UNKNOWN" and observed_regime != expected_regime:
        warnings.append("REGIME_MISMATCH_POST_TRADE")

    if rr > float(cfg["max_realistic_rr"]):
        warnings.append("TP_UNREALISTIC_FOR_CONDITIONS")
    if _entry_risk_pct(trade) > float(cfg["max_stop_pct"]):
        warnings.append("STOP_TOO_WIDE")

    if net < 0 and execution_cost > abs(gross):
        lessons.append("Costs erased edge; tighten spread/slippage filters")
    if execution_cost > float(cfg["high_execution_cost_pct"]):
        warnings.append("EXECUTION_COST_TOO_HIGH")

    if net < 0 and (slippage > expected_slippage or spread > float(cfg["max_spread_pct"])):
        lessons.append("This trade likely should have been rejected by execution-quality gates")

    outcome = str(trade.get("outcome", "OPEN_AT_END"))
    bucket = _expectancy_bucket(net)
    return DecisionReviewResult(
        decision_id=str(trade.get("decision_id", "")),
        symbol=str(trade.get("symbol", "UNKNOWN")),
        setup_type=str(trade.get("setup_type", "UNKNOWN")),
        regime=str(trade.get("regime", "UNKNOWN")),
        decision_type="EXECUTED",
        outcome=outcome,
        gross_pnl_pct=gross,
        net_pnl_pct=net,
        effective_rr=effective_rr,
        execution_cost_pct=execution_cost,
        slippage_damage_pct=slippage,
        spread_damage_pct=spread,
        latency_penalty=latency_penalty,
        reject_quality="N/A",
        expectancy_bucket=bucket,
        lessons=lessons,
        warnings=warnings,
        diagnostics={
            "configured_rr": rr,
            "expected_slippage_pct": expected_slippage,
            "observed_regime": observed_regime,
            "fees_pct": fees,
            "raw": {"trade": trade, "market_after": market_after},
        },
    )


def review_rejected_setup(rejected: dict[str, Any], market_after: dict[str, Any], config: dict[str, Any] | None = None) -> DecisionReviewResult:
    cfg = _merged_config(config)
    tp_like = float(market_after.get("tp_like_move_pct", 0.0) or 0.0)
    sl_like = float(market_after.get("sl_like_move_pct", 0.0) or 0.0)

    if sl_like >= float(cfg["reject_move_threshold_pct"]):
        outcome = "AVOIDED_LOSER"
        quality = "CORRECT_REJECT"
        net = 0.0
    elif tp_like >= float(cfg["reject_move_threshold_pct"]):
        outcome = "MISSED_WINNER"
        quality = "BAD_REJECT"
        net = -tp_like
    elif tp_like == 0.0 and sl_like == 0.0:
        outcome = "REJECTED"
        quality = "UNKNOWN"
        net = 0.0
    else:
        outcome = "REJECTED"
        quality = "CORRECT_REJECT"
        net = 0.0

    warnings: list[str] = []
    lessons: list[str] = []
    if outcome == "MISSED_WINNER":
        warnings.append("REJECT_FILTER_TOO_STRICT")
        lessons.append("Review thresholds; setup had positive follow-through")
    if outcome == "AVOIDED_LOSER":
        lessons.append("Reject gate protected expectancy")

    return DecisionReviewResult(
        decision_id=str(rejected.get("decision_id", "")),
        symbol=str(rejected.get("symbol", "UNKNOWN")),
        setup_type=str(rejected.get("setup_type", "UNKNOWN")),
        regime=str(rejected.get("regime", "UNKNOWN")),
        decision_type=str(rejected.get("decision_type", "SIGNAL_REJECTED")),
        outcome=outcome,
        gross_pnl_pct=0.0,
        net_pnl_pct=net,
        effective_rr=0.0,
        execution_cost_pct=0.0,
        slippage_damage_pct=0.0,
        spread_damage_pct=0.0,
        latency_penalty=0.0,
        reject_quality=quality,
        expectancy_bucket=_expectancy_bucket(net),
        lessons=lessons,
        warnings=warnings,
        diagnostics={"tp_like_move_pct": tp_like, "sl_like_move_pct": sl_like},
    )


def summarize_decision_reviews(reviews: list[DecisionReviewResult]) -> dict[str, Any]:
    if not reviews:
        return {
            "expectancy_by_symbol": {},
            "expectancy_by_regime": {},
            "expectancy_by_setup_type": {},
            "reject_accuracy": 0.0,
            "missed_winner_rate": 0.0,
            "avoided_loser_rate": 0.0,
            "average_execution_cost_pct": 0.0,
            "worst_symbols": [],
            "best_symbols": [],
            "regimes_to_disable": [],
            "setup_types_to_reduce": [],
            "recommended_threshold_adjustments": [],
        }

    by_symbol = _group_expectancy(reviews, "symbol")
    by_regime = _group_expectancy(reviews, "regime")
    by_setup = _group_expectancy(reviews, "setup_type")

    rejected = [r for r in reviews if r.decision_type != "EXECUTED"]
    correct_rejects = [r for r in rejected if r.reject_quality == "CORRECT_REJECT"]
    missed = [r for r in rejected if r.outcome == "MISSED_WINNER"]
    avoided = [r for r in rejected if r.outcome == "AVOIDED_LOSER"]

    adjustments = []
    if missed and (len(missed) / max(len(rejected), 1)) > 0.35:
        adjustments.append("Loosen rejection thresholds for high-score setups")
    if mean(r.execution_cost_pct for r in reviews) > 0.01:
        adjustments.append("Tighten max spread/slippage limits")

    regimes_to_disable = sorted([k for k, v in by_regime.items() if v < 0.0])
    setup_types_to_reduce = sorted([k for k, v in by_setup.items() if v < 0.0])

    sorted_symbols = sorted(by_symbol.items(), key=lambda kv: (kv[1], kv[0]))
    return {
        "expectancy_by_symbol": by_symbol,
        "expectancy_by_regime": by_regime,
        "expectancy_by_setup_type": by_setup,
        "reject_accuracy": len(correct_rejects) / max(len(rejected), 1),
        "missed_winner_rate": len(missed) / max(len(rejected), 1),
        "avoided_loser_rate": len(avoided) / max(len(rejected), 1),
        "average_execution_cost_pct": mean(r.execution_cost_pct for r in reviews),
        "worst_symbols": [s for s, _ in sorted_symbols[:3]],
        "best_symbols": [s for s, _ in sorted_symbols[-3:]][::-1],
        "regimes_to_disable": regimes_to_disable,
        "setup_types_to_reduce": setup_types_to_reduce,
        "recommended_threshold_adjustments": adjustments,
        "sample_reviews": [asdict(r) for r in reviews[:3]],
    }


def _group_expectancy(reviews: list[DecisionReviewResult], key: str) -> dict[str, float]:
    groups: dict[str, list[float]] = {}
    for review in reviews:
        value = getattr(review, key)
        groups.setdefault(value, []).append(review.net_pnl_pct)
    return {k: mean(v) for k, v in sorted(groups.items())}


def _expectancy_bucket(net_pnl_pct: float) -> str:
    if net_pnl_pct > 0.01:
        return "STRONG_POSITIVE"
    if net_pnl_pct > 0:
        return "SLIGHT_POSITIVE"
    if net_pnl_pct == 0:
        return "NEUTRAL"
    if net_pnl_pct > -0.01:
        return "SLIGHT_NEGATIVE"
    return "STRONG_NEGATIVE"


def _safe_rr(entry: Any, sl: Any, tp: Any) -> float:
    try:
        e, s, t = float(entry), float(sl), float(tp)
    except (TypeError, ValueError):
        return 0.0
    risk = abs(e - s)
    if risk <= 0:
        return 0.0
    return abs(t - e) / risk


def _entry_risk_pct(trade: dict[str, Any]) -> float:
    entry = float(trade.get("entry", 0.0) or 0.0)
    sl = float(trade.get("sl", 0.0) or 0.0)
    if entry <= 0:
        return 0.0
    return abs(entry - sl) / entry


def _merged_config(config: dict[str, Any] | None) -> dict[str, Any]:
    base = {
        "expected_slippage_pct": 0.002,
        "max_realistic_rr": 4.0,
        "max_stop_pct": 0.03,
        "max_spread_pct": 0.003,
        "default_fees_pct": 0.0007,
        "high_execution_cost_pct": 0.006,
        "latency_penalty_per_second": 0.0005,
        "reject_move_threshold_pct": 0.01,
    }
    if config:
        base.update(config)
    return base
