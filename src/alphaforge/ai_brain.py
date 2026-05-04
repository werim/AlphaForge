from __future__ import annotations

from dataclasses import dataclass
import json
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session


DecisionType = str


@dataclass(frozen=True)
class ScoreContext:
    total_score: float
    expectancy_edge: float
    components: dict[str, float]
    penalties: dict[str, float]
    accepted: bool
    reason_flags: list[str]


@dataclass(frozen=True)
class OrderPlan:
    decision: DecisionType
    order_type: DecisionType
    limit_price: float | None
    stop_price: float | None
    confidence: float
    reason: str


class AIBrain:
    """Deterministic SQL-first decision engine for futures signals.

    This component intentionally avoids any non-deterministic LLM call path in
    live execution. All outcomes are generated from static scoring rules,
    persisted features, and expectancy statistics.
    """

    def __init__(self, session: Session, *, min_accept_score: float = 0.62) -> None:
        self.session = session
        self.min_accept_score = min_accept_score

    # ---- Scoring ---------------------------------------------------------
    def score_signal(
        self,
        signal: Mapping[str, Any],
        market_ctx: Mapping[str, Any],
        regime_ctx: Mapping[str, Any],
        stats_ctx: Mapping[str, Any],
    ) -> ScoreContext:
        setup_quality = self._clip01(_num(signal, "setup_quality", 0.5))
        regime_alignment = self._clip01(_num(regime_ctx, "alignment", 0.5))
        expectancy_edge = self._expectancy_edge(signal, regime_ctx, stats_ctx)
        momentum_confirmation = self._clip01(_num(market_ctx, "momentum_confirmation", 0.5))
        liquidity_quality = self._clip01(_num(market_ctx, "liquidity_quality", 0.5))
        volatility_fit = self._clip01(_num(market_ctx, "volatility_fit", 0.5))
        risk_reward_quality = self._risk_reward_quality(signal)

        spread_penalty = self._clip01(_num(market_ctx, "spread_bps", 0.0) / max(_num(signal, "max_spread_bps", 8.0), 1.0))
        funding_penalty = self._clip01(abs(_num(market_ctx, "funding_rate", 0.0)) / max(_num(signal, "max_funding_rate", 0.0006), 1e-8))
        fakeout_risk = self._clip01(_num(market_ctx, "fakeout_risk", 0.25))
        recent_loss_penalty = self._recent_loss_penalty(stats_ctx)
        slippage_penalty = self._clip01(_num(market_ctx, "expected_slippage_pct", 0.0) / max(_num(signal, "max_expected_slippage_pct", 0.003), 1e-8))
        latency_penalty = self._clip01(_num(market_ctx, "latency_ms", 50.0) / max(_num(signal, "max_latency_ms", 300.0), 1.0))
        funding_exec_penalty = self._clip01(abs(_num(market_ctx, "funding_rate_pct", 0.0)) / max(_num(signal, "max_funding_rate_pct", 0.05), 1e-8))

        components = {
            "setup_quality": setup_quality,
            "regime_alignment": regime_alignment,
            "expectancy_edge": expectancy_edge,
            "momentum_confirmation": momentum_confirmation,
            "liquidity_quality": liquidity_quality,
            "volatility_fit": volatility_fit,
            "risk_reward_quality": risk_reward_quality,
        }
        penalties = {
            "spread_penalty": spread_penalty,
            "funding_penalty": funding_penalty,
            "fakeout_risk": fakeout_risk,
            "recent_loss_penalty": recent_loss_penalty,
            "slippage_penalty": slippage_penalty,
            "latency_penalty": latency_penalty,
            "funding_exec_penalty": funding_exec_penalty,
        }

        weighted = (
            setup_quality * 0.18
            + regime_alignment * 0.14
            + expectancy_edge * 0.20
            + momentum_confirmation * 0.12
            + liquidity_quality * 0.10
            + volatility_fit * 0.10
            + risk_reward_quality * 0.16
            - spread_penalty * 0.08
            - funding_penalty * 0.04
            - fakeout_risk * 0.07
            - recent_loss_penalty * 0.06
            - slippage_penalty * 0.08
            - latency_penalty * 0.04
            - funding_exec_penalty * 0.04
        )
        total_score = self._clip01(weighted)

        reason_flags: list[str] = []
        if expectancy_edge < 0.5:
            reason_flags.append("negative_expectancy_risk")
        if spread_penalty > 0.8:
            reason_flags.append("spread_too_wide")
        if fakeout_risk > 0.75:
            reason_flags.append("high_fakeout_risk")

        accepted = total_score >= self.min_accept_score and expectancy_edge >= 0.5
        return ScoreContext(total_score, expectancy_edge, components, penalties, accepted, reason_flags)

    # ---- Order plan ------------------------------------------------------
    def choose_order_plan(
        self,
        signal: Mapping[str, Any],
        market_ctx: Mapping[str, Any],
        score_ctx: ScoreContext,
    ) -> OrderPlan:
        if not score_ctx.accepted:
            return OrderPlan("REJECTED", "REJECTED", None, None, score_ctx.total_score, "Score below threshold or negative expectancy.")

        entry = _num(signal, "entry_price", 0.0)
        breakout = bool(signal.get("breakout", False))
        momentum = score_ctx.components["momentum_confirmation"]
        spread_penalty = score_ctx.penalties["spread_penalty"]

        # Prefer fewer high-quality trades, avoid MARKET unless justified.
        if momentum > 0.86 and spread_penalty < 0.25 and score_ctx.total_score > 0.82:
            return OrderPlan("ACCEPTED", "MARKET", None, None, score_ctx.total_score, "High momentum and clean microstructure justify immediate entry.")
        if breakout and momentum > 0.7:
            stop_buffer = _num(market_ctx, "tick_size", 0.1) * 2
            return OrderPlan("ACCEPTED", "STOP_MARKET", None, entry + stop_buffer, score_ctx.total_score, "Breakout setup requires confirmation above trigger.")
        if entry > 0:
            return OrderPlan("ACCEPTED", "LIMIT", entry, None, score_ctx.total_score, "Quality setup accepted with controlled slippage via limit order.")

        return OrderPlan("WATCHING", "WATCHING", None, None, score_ctx.total_score, "Signal valid but waiting for executable price level.")

    def explain_decision(
        self,
        signal: Mapping[str, Any],
        score_ctx: ScoreContext,
        order_plan: OrderPlan,
    ) -> str:
        symbol = signal.get("symbol", "UNKNOWN")
        side = signal.get("side", "N/A")
        key_scores = ", ".join(f"{k}={v:.2f}" for k, v in score_ctx.components.items())
        key_penalties = ", ".join(f"{k}={v:.2f}" for k, v in score_ctx.penalties.items())
        flags = f" Flags: {', '.join(score_ctx.reason_flags)}." if score_ctx.reason_flags else ""
        return (
            f"{symbol} {side}: decision={order_plan.decision}/{order_plan.order_type}, "
            f"score={score_ctx.total_score:.2f}, expectancy={score_ctx.expectancy_edge:.2f}. "
            f"Components[{key_scores}] Penalties[{key_penalties}]. {order_plan.reason}{flags}"
        )

    # ---- Learning --------------------------------------------------------
    def learn_from_closed_trade(self, closed_trade: Mapping[str, Any], replay_ctx: Mapping[str, Any]) -> None:
        pnl = _num(closed_trade, "pnl", 0.0)
        setup = str(closed_trade.get("setup", "unknown"))
        regime = str(closed_trade.get("regime", "unknown"))
        symbol = str(closed_trade.get("symbol", "unknown"))

        self._upsert_expectancy("setup_expectancy_stats", "setup", setup, pnl)
        self._upsert_expectancy("regime_expectancy_stats", "regime", regime, pnl)
        self._upsert_expectancy("symbol_expectancy_stats", "symbol", symbol, pnl)

        execution_metrics = {
            "expected_slippage_pct": _num(closed_trade, "expected_slippage_pct", 0.0),
            "actual_slippage_pct": _num(closed_trade, "actual_slippage_pct", 0.0),
            "filled_entry_price": _num(closed_trade, "filled_entry_price", _num(closed_trade, "entry_price", 0.0)),
            "entry_price": _num(closed_trade, "entry_price", 0.0),
            "pnl": pnl,
            "spread_pct": _num(closed_trade, "spread_pct", 0.0),
            "latency_ms": int(_num(closed_trade, "latency_ms", 0)),
            "orderbook_imbalance": _num(closed_trade, "orderbook_imbalance", 0.0),
            "funding_rate_pct": _num(closed_trade, "funding_rate_pct", 0.0),
            "volatility_regime": str(closed_trade.get("volatility_regime", "unknown")),
        }

        self.session.execute(
            text(
                """
                INSERT INTO closed_trade_reviews (trade_id, symbol, review_payload, execution_metrics, created_at)
                VALUES (:trade_id, :symbol, :payload, :execution_metrics, :created_at)
                """
            ),
            {
                "trade_id": str(closed_trade.get("trade_id", "")),
                "symbol": symbol,
                "payload": _json_dumps({"closed_trade": dict(closed_trade), "replay_ctx": dict(replay_ctx)}),
                "execution_metrics": _json_dumps(execution_metrics),
                "created_at": _now(),
            },
        )
        self.session.commit()

    # ---- Hooks for order.py integration ---------------------------------
    def before_virtual_order(self, signal: Mapping[str, Any], market_ctx: Mapping[str, Any], regime_ctx: Mapping[str, Any], stats_ctx: Mapping[str, Any]) -> tuple[ScoreContext, OrderPlan, str]:
        return self._run_decision_pipeline(signal, market_ctx, regime_ctx, stats_ctx, phase="virtual")

    def before_real_order(self, signal: Mapping[str, Any], market_ctx: Mapping[str, Any], regime_ctx: Mapping[str, Any], stats_ctx: Mapping[str, Any]) -> tuple[ScoreContext, OrderPlan, str]:
        return self._run_decision_pipeline(signal, market_ctx, regime_ctx, stats_ctx, phase="real")

    def after_position_close(self, closed_trade: Mapping[str, Any], replay_ctx: Mapping[str, Any]) -> None:
        self.learn_from_closed_trade(closed_trade, replay_ctx)

    # ---- Persistence -----------------------------------------------------
    def _run_decision_pipeline(self, signal: Mapping[str, Any], market_ctx: Mapping[str, Any], regime_ctx: Mapping[str, Any], stats_ctx: Mapping[str, Any], *, phase: str) -> tuple[ScoreContext, OrderPlan, str]:
        score_ctx = self.score_signal(signal, market_ctx, regime_ctx, stats_ctx)
        order_plan = self.choose_order_plan(signal, market_ctx, score_ctx)
        explanation = self.explain_decision(signal, score_ctx, order_plan)
        self._persist_decision(signal, score_ctx, order_plan, explanation, phase, market_ctx)
        self.session.commit()
        return score_ctx, order_plan, explanation

    def _persist_decision(self, signal: Mapping[str, Any], score_ctx: ScoreContext, order_plan: OrderPlan, explanation: str, phase: str, market_ctx: Mapping[str, Any] | None = None) -> None:
        signal_id = self.session.execute(
            text(
                """
                INSERT INTO signals (symbol, side, timeframe, payload, created_at)
                VALUES (:symbol, :side, :timeframe, :payload, :created_at)
                RETURNING id
                """
            ),
            {
                "symbol": str(signal.get("symbol", "UNKNOWN")),
                "side": str(signal.get("side", "N/A")),
                "timeframe": str(signal.get("timeframe", "NA")),
                "payload": _json_dumps(dict(signal)),
                "created_at": _now(),
            },
        ).scalar_one()

        ctx = market_ctx or {}
        expected_slippage_pct = _num(ctx, "expected_slippage_pct", 0.0)
        spread_pct = _num(ctx, "spread_pct", 0.0)
        latency_ms = int(_num(ctx, "latency_ms", 0))
        orderbook_imbalance = _num(ctx, "orderbook_imbalance", 0.0)
        funding_rate_pct = _num(ctx, "funding_rate_pct", 0.0)
        volatility_regime = str(ctx.get("volatility_regime", "unknown"))
        execution_flags = ctx.get("execution_flags", [])
        if not isinstance(execution_flags, list):
            execution_flags = [str(execution_flags)]
        effective_rr = max(0.0, _num(signal, "risk_reward", 1.0) - (expected_slippage_pct * 100) - (spread_pct * 100))

        decision_id = self.session.execute(
            text(
                """
                INSERT INTO order_decisions
                (signal_id, phase, decision, order_type, confidence, explanation, order_payload, expected_slippage_pct, spread_pct, latency_ms, orderbook_imbalance, funding_rate_pct, volatility_regime, effective_rr, execution_flags, created_at)
                VALUES (:signal_id, :phase, :decision, :order_type, :confidence, :explanation, :order_payload, :expected_slippage_pct, :spread_pct, :latency_ms, :orderbook_imbalance, :funding_rate_pct, :volatility_regime, :effective_rr, :execution_flags, :created_at)
                RETURNING id
                """
            ),
            {
                "signal_id": signal_id,
                "phase": phase,
                "decision": order_plan.decision,
                "order_type": order_plan.order_type,
                "confidence": score_ctx.total_score,
                "explanation": explanation,
                "order_payload": _json_dumps({
                    "limit_price": order_plan.limit_price,
                    "stop_price": order_plan.stop_price,
                    "reason": order_plan.reason,
                }),
                "expected_slippage_pct": expected_slippage_pct,
                "spread_pct": spread_pct,
                "latency_ms": latency_ms,
                "orderbook_imbalance": orderbook_imbalance,
                "funding_rate_pct": funding_rate_pct,
                "volatility_regime": volatility_regime,
                "effective_rr": effective_rr,
                "execution_flags": _json_dumps(execution_flags),
                "created_at": _now(),
            },
        ).scalar_one()

        self.session.execute(
            text(
                """
                INSERT INTO ai_decision_features
                (decision_id, features, penalties, reason_flags, created_at)
                VALUES (:decision_id, :features, :penalties, :reason_flags, :created_at)
                """
            ),
            {
                "decision_id": decision_id,
                "features": _json_dumps(score_ctx.components),
                "penalties": _json_dumps(score_ctx.penalties),
                "reason_flags": _json_dumps(score_ctx.reason_flags),
                "created_at": _now(),
            },
        )

        self.session.execute(
            text(
                """
                INSERT INTO trade_lifecycle_events (signal_id, event_type, payload, created_at)
                VALUES (:signal_id, :event_type, :payload, :created_at)
                """
            ),
            {
                "signal_id": signal_id,
                "event_type": f"decision_{order_plan.decision.lower()}",
                "payload": _json_dumps({"phase": phase, "order_type": order_plan.order_type, "explanation": explanation}),
                "created_at": _now(),
            },
        )

    # ---- Stats helpers ---------------------------------------------------
    def _expectancy_edge(self, signal: Mapping[str, Any], regime_ctx: Mapping[str, Any], stats_ctx: Mapping[str, Any]) -> float:
        setup = str(signal.get("setup", "unknown"))
        regime = str(regime_ctx.get("regime", "unknown"))
        symbol = str(signal.get("symbol", "unknown"))
        values = [
            _num(stats_ctx.get("setup", {}), setup, 0.0),
            _num(stats_ctx.get("regime", {}), regime, 0.0),
            _num(stats_ctx.get("symbol", {}), symbol, 0.0),
        ]
        expectancy = sum(values) / len(values)
        return self._clip01((expectancy + 1.0) / 2.0)

    def _upsert_expectancy(self, table: str, key_col: str, key_val: str, pnl: float) -> None:
        self.session.execute(
            text(
                f"""
                INSERT INTO {table} ({key_col}, samples, win_count, total_pnl, expectancy, updated_at)
                VALUES (:key_val, 1, :win_count, :pnl, :pnl, :updated_at)
                ON CONFLICT ({key_col}) DO UPDATE SET
                    samples = {table}.samples + 1,
                    win_count = {table}.win_count + :win_count,
                    total_pnl = {table}.total_pnl + :pnl,
                    expectancy = ({table}.total_pnl + :pnl) / NULLIF({table}.samples + 1, 0),
                    updated_at = :updated_at
                """
            ),
            {
                "key_val": key_val,
                "win_count": 1 if pnl > 0 else 0,
                "pnl": pnl,
                "updated_at": _now(),
            },
        )

    @staticmethod
    def _risk_reward_quality(signal: Mapping[str, Any]) -> float:
        rr = _num(signal, "risk_reward", 1.0)
        return AIBrain._clip01((rr - 0.8) / 2.2)

    @staticmethod
    def _recent_loss_penalty(stats_ctx: Mapping[str, Any]) -> float:
        loss_streak = max(0.0, _num(stats_ctx, "recent_loss_streak", 0.0))
        return AIBrain._clip01(loss_streak / 5.0)

    @staticmethod
    def _clip01(value: float) -> float:
        return max(0.0, min(1.0, value))


def _num(data: Mapping[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_dumps(value: Any) -> str:
    if value is None:
        return "{}"
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str)
