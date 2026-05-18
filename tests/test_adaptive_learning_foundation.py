from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from alphaforge.adaptive_learning import (
    compute_shadow_thresholds,
    record_closed_trade_review,
    record_rejected_signal_review,
    update_adaptive_stats,
    update_adaptive_stats_by_scope,
)
from alphaforge.persistence import init_db


def test_closed_trade_review_persistence_with_nulls() -> None:
    engine = init_db()
    with Session(engine) as session:
        ok = record_closed_trade_review(
            session,
            trade_id="t1",
            symbol="BTCUSDT",
            setup_type="PULLBACK",
            regime="TRENDING",
            side="LONG",
            entry_price=100.0,
            exit_price=101.0,
            net_pnl_pct=0.01,
            expected_slippage_pct=None,
            actual_slippage_pct=None,
        )
        session.commit()
        assert ok is True
        row = session.execute(text("SELECT trade_id, expected_slippage_pct, actual_slippage_pct FROM closed_trade_reviews WHERE trade_id='t1'")).fetchone()
        assert row[0] == "t1"
        assert row[1] is None and row[2] is None


def test_rejected_signal_review_persistence_reason_and_bucket() -> None:
    engine = init_db()
    with Session(engine) as session:
        assert record_rejected_signal_review(session, signal_id="s1", symbol="BTCUSDT", reject_reason="LOW_SCORE", expectancy_bucket="LOW")
        assert record_rejected_signal_review(session, signal_id="s2", symbol="BTCUSDT", reject_reason="LOW_LIQUIDITY", expectancy_bucket="NEGATIVE")
        session.commit()
        reasons = {r[0] for r in session.execute(text("SELECT reject_reason FROM rejected_signal_reviews")).fetchall()}
        assert "LOW_SCORE" in reasons and "LOW_LIQUIDITY" in reasons


def test_adaptive_stats_and_shadow_thresholds() -> None:
    engine = init_db()
    with Session(engine) as session:
        for i in range(60):
            record_closed_trade_review(session, trade_id=f"t{i}", symbol="BTCUSDT", setup_type="PULLBACK", regime="TRENDING", net_pnl_pct=-0.01, effective_rr=0.9, spread_pct=0.001, expected_slippage_pct=0.0008)
        record_rejected_signal_review(session, signal_id="s1", symbol="BTCUSDT", reject_reason="LOW_SCORE", reject_correct=1)
        record_rejected_signal_review(session, signal_id="s2", symbol="BTCUSDT", reject_reason="LOW_SCORE", reject_correct=0)
        session.commit()
        assert update_adaptive_stats(session, "SYMBOL", "BTCUSDT")
        session.commit()
        stats = session.execute(text("SELECT sample_size, avg_effective_rr, expectancy, reject_accuracy FROM adaptive_stats WHERE scope_type='SYMBOL' AND scope_key='BTCUSDT'")).fetchone()
        assert stats[0] == 60
        assert stats[1] is not None
        assert stats[2] < 0
        assert stats[3] == 0.5
        static = compute_shadow_thresholds({"min_score": 0.62, "min_effective_rr": 1.1, "max_spread_pct": 0.0025, "max_expected_slippage_pct": 0.003, "min_liquidity_score": 0.2}, {"sample_size": 10}, {"ADAPTIVE_MIN_SAMPLE_SIZE": 50})
        assert static["source"] == "STATIC"
        adaptive = compute_shadow_thresholds({"min_score": 0.62, "min_effective_rr": 1.1, "max_spread_pct": 0.0025, "max_expected_slippage_pct": 0.003, "min_liquidity_score": 0.2}, {"sample_size": 60, "expectancy": -0.01, "avg_spread_pct": 0.0015, "confidence": 0.4}, {"ADAPTIVE_MIN_SAMPLE_SIZE": 50, "ADAPTIVE_MAX_SCORE_ADJUSTMENT": 0.05, "ADAPTIVE_MAX_EFFECTIVE_RR_ADJUSTMENT": 0.15, "ADAPTIVE_ALLOW_LOOSENING_GATES": False})
        assert adaptive["source"] == "SHADOW_ADAPTIVE"
        assert adaptive["min_score"] <= 0.62 and adaptive["min_effective_rr"] >= 1.1


def test_adaptive_stats_by_scope_rejection_reason() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        record_rejected_signal_review(session, signal_id="s1", symbol="BTCUSDT", reject_reason="LOW_SCORE", reject_correct=1, payload_json={"execution_quality_bucket": "HIGH"})
        record_rejected_signal_review(session, signal_id="s2", symbol="ETHUSDT", reject_reason="LOW_SCORE", reject_correct=0, payload_json={"execution_quality_bucket": "LOW"})
        assert update_adaptive_stats_by_scope(session, "REJECTION_REASON", "LOW_SCORE")
        row = session.execute(text("SELECT sample_size, reject_accuracy FROM adaptive_stats WHERE scope_type='REJECTION_REASON' AND scope_key='LOW_SCORE'")).fetchone()
        assert row.sample_size == 2
        assert float(row.reject_accuracy) == 0.5
        assert update_adaptive_stats_by_scope(session, "EXECUTION_QUALITY_BUCKET", "HIGH")
        eq_row = session.execute(text("SELECT sample_size, reject_accuracy FROM adaptive_stats WHERE scope_type='EXECUTION_QUALITY_BUCKET' AND scope_key='HIGH'")).fetchone()
        assert eq_row.sample_size == 1
        assert float(eq_row.reject_accuracy) == 1.0
