from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from alphaforge.ai_brain import AIBrain
from alphaforge.order import before_real_order, before_virtual_order
from alphaforge.persistence import init_db, save_signal, upsert_expectancy_stats


def test_schema_init_sqlite_memory() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    names = set(inspect(engine).get_table_names())
    assert {"signals", "order_decisions", "ai_decision_features", "trade_lifecycle_events", "closed_trade_reviews", "setup_expectancy_stats", "regime_expectancy_stats", "symbol_expectancy_stats", "cooldown_states"}.issubset(names)


def test_persistence_insert_and_upsert() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        sid = save_signal(s, symbol="BTCUSDT", side="BUY", timeframe="5m")
        assert sid is not None
        upsert_expectancy_stats(s, "setup_expectancy_stats", "setup", "pullback", 10.0)
        upsert_expectancy_stats(s, "setup_expectancy_stats", "setup", "pullback", -5.0)
        row = s.execute(text("SELECT samples,total_pnl FROM setup_expectancy_stats WHERE setup='pullback'" )).one()
        assert row.samples == 2
        assert float(row.total_pnl) == 5.0


def test_score_signal_deterministic_and_confidence_band() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        brain = AIBrain(s)
        signal = {"symbol": "BTCUSDT", "side": "BUY", "entry_price": 100, "risk_reward": 2.0, "setup_quality": 0.8}
        market = {"momentum_confirmation": 0.9, "liquidity_quality": 0.9, "volatility_fit": 0.8}
        regime = {"alignment": 0.8, "regime": "trend"}
        stats = {"setup": {"unknown": 0.3}, "regime": {"trend": 0.3}, "symbol": {"BTCUSDT": 0.3}}
        a = brain.score_signal(signal, market, regime, stats)
        b = brain.score_signal(signal, market, regime, stats)
        assert a.total_score == b.total_score
        assert a.total_score >= 0.45
        assert a.probabilistic["p_win"] > 0.0
        assert a.probabilistic["calibrated_score"] > 0.0


def test_probabilistic_score_changes_with_market_conditions() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        brain = AIBrain(s)
        signal = {"symbol": "BTCUSDT", "side": "BUY", "entry_price": 100, "risk_reward": 2.0, "setup_quality": 0.8}
        good = brain.score_signal(
            signal,
            {"momentum_confirmation": 0.9, "liquidity_quality": 0.95, "volatility_fit": 0.85, "spread_bps": 1.5, "expected_slippage_pct": 0.0006, "latency_ms": 20, "fakeout_risk": 0.15},
            {"alignment": 0.9, "regime": "trend"},
            {"setup": {"unknown": 0.3}, "regime": {"trend": 0.3}, "symbol": {"BTCUSDT": 0.3}, "sample_size": 120},
        )
        bad = brain.score_signal(
            signal,
            {"momentum_confirmation": 0.3, "liquidity_quality": 0.2, "volatility_fit": 0.25, "spread_bps": 12.0, "expected_slippage_pct": 0.02, "latency_ms": 500, "fakeout_risk": 0.9},
            {"alignment": 0.2, "regime": "chop"},
            {"setup": {"unknown": -0.2}, "regime": {"chop": -0.3}, "symbol": {"BTCUSDT": -0.2}, "sample_size": 4},
        )
        assert good.probabilistic["calibrated_score"] > bad.probabilistic["calibrated_score"]
        assert good.probabilistic["p_execution_success"] > bad.probabilistic["p_execution_success"]


def test_probabilistic_reject_reason_priority() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        brain = AIBrain(s, min_accept_score=0.99)
        signal = {"symbol": "BTCUSDT", "side": "BUY", "entry_price": 100, "risk_reward": 1.1, "setup_quality": 0.2}
        market = {"momentum_confirmation": 0.1, "liquidity_quality": 0.1, "volatility_fit": 0.2, "fakeout_risk": 0.9}
        regime = {"alignment": 0.2, "regime": "chop"}
        stats = {"setup": {"unknown": -0.4}, "regime": {"chop": -0.4}, "symbol": {"BTCUSDT": -0.4}, "sample_size": 0}
        score_ctx = brain.score_signal(signal, market, regime, stats)
        assert not score_ctx.accepted
        assert "low_p_win" in score_ctx.reason_flags


def test_cooldown_blocks_trade() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        ok, _ = before_real_order(s, {"symbol": "BTCUSDT", "quantity": 1, "entry_price": 100}, {}, {"alignment": 0.8}, {"cooldown_remaining_sec": 120})
        assert not ok


def test_market_order_requires_strong_context() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        out = before_virtual_order(s, {"symbol": "BTCUSDT", "entry_price": 100, "risk_reward": 2, "setup_quality": 0.8}, {"momentum_confirmation": 0.4, "liquidity_quality": 0.9, "volatility_fit": 0.9}, {"alignment": 0.9, "regime": "trend"}, {"setup": {"unknown": 0.6}, "regime": {"trend": 0.6}, "symbol": {"BTCUSDT": 0.6}})
        assert out is not None
        assert out["ai_order_type"] != "MARKET"


def test_order_adapter_missing_fields_safe() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        out = before_virtual_order(s, {}, {}, {}, {})
        assert out is None or "ai_reason" in out
