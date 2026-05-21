from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from alphaforge.ai_brain import AIBrain
from alphaforge.persistence import init_db


def test_ai_decision_features_are_upserted_for_same_signal_candidate() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    brain = AIBrain(Session(engine), min_accept_score=0.62)
    signal = {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "timeframe": "5m",
        "entry_price": 67_250.0,
        "risk_reward": 2.15,
        "max_spread_bps": 12.0,
        "max_expected_slippage_pct": 0.00072,
    }
    market_ctx = {
        "side": "LONG",
        "timeframe": "5m",
        "spread_pct": 0.0009,
        "spread_bps": 9.0,
        "funding_rate_pct": 0.00005,
        "liquidity_score": 0.86,
        "liquidity_quality": 0.5,
        "volatility_fit": 0.5,
        "volatility_regime": "MODERATE",
        "momentum_confirmation": 0.7,
        "fakeout_risk": 0.22,
        "expected_slippage_pct": 0.0006,
        "latency_ms": 55,
        "orderbook_imbalance": 0.12,
        "market_ts": 1716200000.0,
    }
    regime_ctx = {"alignment": 0.8}
    stats_ctx: dict[str, object] = {}

    brain.before_real_order(signal, market_ctx, regime_ctx, stats_ctx)
    brain.before_real_order(signal, market_ctx, regime_ctx, stats_ctx)

    with engine.connect() as conn:
        feature_count = conn.execute(text("SELECT COUNT(*) FROM ai_decision_features")).scalar_one()
        decision_count = conn.execute(text("SELECT COUNT(*) FROM order_decisions")).scalar_one()
        feature_decision_ids = conn.execute(text("SELECT COUNT(DISTINCT decision_id) FROM ai_decision_features")).scalar_one()

    assert feature_count == 1
    assert decision_count == 1
    assert feature_decision_ids == 1


def test_ai_decision_features_persist_per_distinct_runtime_decision() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    brain = AIBrain(Session(engine), min_accept_score=0.62)
    signal = {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "5m", "entry_price": 67_250.0, "risk_reward": 2.15}
    market_ctx = {"spread_bps": 8.0, "liquidity_quality": 0.8, "volatility_fit": 0.7, "momentum_confirmation": 0.8}
    regime_ctx = {"alignment": 0.8}
    stats_ctx: dict[str, object] = {}

    for idx in range(3):
        brain.before_real_order(signal, {**market_ctx, "market_ts": 1716200000.0 + idx}, regime_ctx, stats_ctx)

    with engine.connect() as conn:
        feature_count = conn.execute(text("SELECT COUNT(*) FROM ai_decision_features")).scalar_one()
        decision_count = conn.execute(text("SELECT COUNT(*) FROM order_decisions")).scalar_one()
    assert feature_count == 3
    assert decision_count == 3
