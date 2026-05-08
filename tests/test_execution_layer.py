from sqlalchemy import text
from sqlalchemy.orm import Session
import json
import pytest

from alphaforge.order import after_position_close, before_real_order
from alphaforge.persistence import init_db

def test_high_slippage_blocks_trade() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        ok, payload = before_real_order(
            s,
            {"symbol": "BTCUSDT", "quantity": 1, "entry_price": 100, "risk_reward": 1.2},
            {"execution_ctx": {"expected_slippage_pct": 0.02, "spread_pct": 0.01, "latency_ms": 20, "orderbook_imbalance": 0.1, "funding_rate_pct": 0.0, "volatility_regime": "high"}},
            {"alignment": 0.8},
            {},
        )
        assert not ok
        assert "HIGH_SLIPPAGE" in payload["execution_flags"]

def test_effective_rr_adjustment() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        _, payload = before_real_order(
            s,
            {"symbol": "BTCUSDT", "quantity": 1, "entry_price": 100, "risk_reward": 2.0},
            {"execution_ctx": {"expected_slippage_pct": 0.001, "spread_pct": 0.001, "latency_ms": 50, "orderbook_imbalance": 0.0, "funding_rate_pct": 0.0, "volatility_regime": "normal"}},
            {"alignment": 0.8},
            {},
        )
        assert payload["effective_rr"] == pytest.approx(1.8, rel=1e-6)

def test_execution_metrics_persisted() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        after_position_close(s, {"trade_id": "t1", "symbol": "BTCUSDT", "pnl": 1.0, "entry_price": 100, "filled_entry_price": 100.2, "expected_slippage_pct": 0.001}, {})
        row = s.execute(text("SELECT execution_metrics FROM closed_trade_reviews WHERE trade_id='t1' ORDER BY id DESC LIMIT 1")).one()
        metrics = json.loads(row.execution_metrics)
        assert "entry_price" in metrics
        assert "filled_entry_price" in metrics
        assert "expected_slippage_pct" in metrics
        assert "realized_slippage_pct" in metrics
        assert "fill_quality_score" in metrics
        assert 0.0 <= metrics["fill_quality_score"] <= 1.0
        assert metrics["fill_quality_score"] == pytest.approx(0.9, rel=1e-6)

def test_execution_metrics_worse_slippage_lowers_quality() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        after_position_close(s, {"trade_id": "t2", "symbol": "BTCUSDT", "pnl": 1.0, "entry_price": 100, "filled_entry_price": 100.05, "expected_slippage_pct": 0.001}, {})
        after_position_close(s, {"trade_id": "t3", "symbol": "BTCUSDT", "pnl": 1.0, "entry_price": 100, "filled_entry_price": 100.3, "expected_slippage_pct": 0.001}, {})
        better = json.loads(s.execute(text("SELECT execution_metrics FROM closed_trade_reviews WHERE trade_id='t2' ORDER BY id DESC LIMIT 1")).one().execution_metrics)
        worse = json.loads(s.execute(text("SELECT execution_metrics FROM closed_trade_reviews WHERE trade_id='t3' ORDER BY id DESC LIMIT 1")).one().execution_metrics)
        assert worse["fill_quality_score"] < better["fill_quality_score"]

def test_missing_execution_ctx_safe() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        ok, payload = before_real_order(s, {"symbol": "BTCUSDT", "quantity": 1, "entry_price": 100, "risk_reward": 2.0}, {}, {"alignment": 0.8}, {})
        assert isinstance(ok, bool)
        assert "EXECUTION_CTX_MISSING" in payload.get("execution_flags", [])
