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
            {"execution_ctx": {"expected_slippage_pct": 0.001, "spread_pct": 0.001, "latency_ms": 50, "orderbook_imbalance": 0.0, "funding_rate_pct": 0.0, "volatility_regime": "normal", "liquidity_score": 0.9}},
            {"alignment": 0.8},
            {},
        )
        assert payload["effective_rr"] == pytest.approx(1.875, rel=1e-6)

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


def test_unknown_execution_context_not_treated_as_zero() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        ok, payload = before_real_order(
            s,
            {"symbol": "BTCUSDT", "quantity": 1, "entry_price": 100, "risk_reward": 2.0},
            {"execution_ctx": {"expected_slippage_pct": None, "spread_pct": None, "latency_ms": None, "liquidity_score": None, "funding_rate_pct": None}},
            {"alignment": 0.8},
            {},
        )
        assert not ok
        assert "UNKNOWN_EXECUTION_CONTEXT" in payload["execution_flags"]
        assert payload["effective_rr"] == pytest.approx(1.4, rel=1e-6)

from alphaforge.execution import (
    EXECUTION_EVIDENCE_INVALID_FAKE_ZERO,
    EXECUTION_EVIDENCE_PARTIAL_ESTIMATED,
    classify_execution_evidence,
)


def _measured_ctx(**overrides):
    ctx = {
        "expected_slippage_pct": 0.001,
        "slippage_status": "MEASURED",
        "spread_pct": 0.001,
        "spread_status": "MEASURED",
        "latency_ms": 50,
        "latency_status": "MEASURED",
        "market_data_latency_status": "MEASURED",
        "liquidity_score": 0.9,
        "liquidity_status": "MEASURED",
        "funding_rate_pct": 0.001,
        "funding_status": "MEASURED",
        "orderbook_imbalance": 0.1,
        "orderbook_status": "MEASURED",
        "volatility_regime": "normal",
        "volatility_status": "MEASURED",
    }
    ctx.update(overrides)
    return ctx


def test_missing_spread_slippage_funding_do_not_become_zero() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        ok, payload = before_real_order(
            s,
            {"symbol": "BTCUSDT", "quantity": 1, "entry_price": 100, "risk_reward": 2.0},
            {"execution_ctx": _measured_ctx(spread_pct=None, expected_slippage_pct=None, funding_rate_pct=None)},
            {"alignment": 0.8},
            {},
        )
        assert not ok
        assert payload["execution_ctx"]["spread_pct"] is None
        assert payload["execution_ctx"]["expected_slippage_pct"] is None
        assert payload["execution_ctx"]["funding_rate_pct"] is None
        assert "UNKNOWN_EXECUTION_CONTEXT" in payload["execution_flags"]


def test_fake_zero_context_classified_invalid_when_measured_required() -> None:
    ctx = _measured_ctx(spread_pct=0.0, expected_slippage_pct=0.0)
    assert classify_execution_evidence(ctx, require_measured=True) == EXECUTION_EVIDENCE_INVALID_FAKE_ZERO


def test_low_effective_rr_reject_when_costs_destroy_setup() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        ok, payload = before_real_order(
            s,
            {"symbol": "BTCUSDT", "quantity": 1, "entry_price": 100, "risk_reward": 1.25},
            {"execution_ctx": _measured_ctx(expected_slippage_pct=0.006, spread_pct=0.004, liquidity_score=0.5)},
            {"alignment": 0.8},
            {},
        )
        assert not ok
        assert payload["effective_rr"] < 1.1
        assert "LOW_EFFECTIVE_RR" in payload["execution_flags"]
        assert payload["reject_reason"]


def test_backtest_estimated_fields_labeled_estimated() -> None:
    ctx = _measured_ctx(
        spread_status="ESTIMATED_BACKTEST",
        slippage_status="ESTIMATED_BACKTEST",
        liquidity_status="ESTIMATED_BACKTEST",
        volatility_status="ESTIMATED_BACKTEST",
    )
    assert classify_execution_evidence(ctx, require_measured=False) == EXECUTION_EVIDENCE_PARTIAL_ESTIMATED


def test_persistence_includes_effective_rr_penalty_breakdown() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as s:
        before_real_order(
            s,
            {"symbol": "BTCUSDT", "quantity": 1, "entry_price": 100, "risk_reward": 2.0},
            {"execution_ctx": _measured_ctx()},
            {"alignment": 0.8},
            {},
        )
        row = s.execute(text("SELECT order_payload FROM order_decisions ORDER BY id DESC LIMIT 1")).one()
        payload = json.loads(row.order_payload)
        breakdown = payload["effective_rr_breakdown"]
        assert breakdown["raw_rr"] == pytest.approx(2.0)
        assert "spread_penalty" in breakdown
        assert "slippage_penalty" in breakdown
        assert "latency_penalty" in breakdown
        assert "liquidity_penalty" in breakdown
        assert "funding_penalty" in breakdown
        assert "volatility_penalty" in breakdown
        assert breakdown["effective_rr"] < breakdown["raw_rr"]


def test_effective_rr_penalty_units_treat_percent_points_and_fractions_consistently() -> None:
    from alphaforge.effective_rr import calculate_effective_rr
    from alphaforge.execution import build_execution_context

    fractional = build_execution_context({
        "spread_pct": 0.001,
        "expected_slippage_pct": 0.001,
        "market_data_latency_ms": 50,
        "liquidity_score": 0.9,
        "funding_rate_pct": 0.0,
        "orderbook_imbalance": 0.1,
        "volatility_regime": "normal",
    })
    percent_points = build_execution_context({
        "spread_pct": 0.1,
        "expected_slippage_pct": 0.1,
        "market_data_latency_ms": 50,
        "liquidity_score": 0.9,
        "funding_rate_pct": 0.0,
        "orderbook_imbalance": 0.1,
        "volatility_regime": "normal",
    })
    a = calculate_effective_rr(2.0, fractional)
    b = calculate_effective_rr(2.0, percent_points)
    assert a.effective_rr == pytest.approx(b.effective_rr, rel=1e-9)
    assert b.spread_penalty < 0.1
    assert b.slippage_penalty < 0.1


def test_effective_rr_cost_penalty_is_applied_once() -> None:
    from alphaforge.effective_rr import calculate_effective_rr
    from alphaforge.execution import build_execution_context

    ctx = build_execution_context({
        "spread_pct": 0.001,
        "expected_slippage_pct": 0.001,
        "market_data_latency_ms": 50,
        "liquidity_score": 0.9,
        "funding_rate_pct": 0.0,
        "orderbook_imbalance": 0.1,
        "volatility_regime": "normal",
    })
    result = calculate_effective_rr(2.0, ctx)

    assert result.effective_rr == pytest.approx(2.0 - result.cost_penalty_total, rel=1e-9)
    assert result.effective_rr != pytest.approx(2.0 - (2 * result.cost_penalty_total), rel=1e-9)


def test_paper_fee_evidence_is_explicit_and_missing_remains_unavailable():
    from alphaforge.execution import build_execution_context

    configured = build_execution_context({"fee_pct": 0.0004, "fee_source": "CONFIGURED_PAPER_ASSUMPTION"})
    missing = build_execution_context({})
    assert configured["fee_pct"] == pytest.approx(0.0004)
    assert configured["fee_source"] == "CONFIGURED_PAPER_ASSUMPTION"
    assert missing["fee_pct"] is None and missing["fee_source"] == "UNAVAILABLE"


def test_paper_fee_bps_is_total_round_trip_cost_applied_once():
    from alphaforge.runtime import RuntimeOrchestrator

    costs = RuntimeOrchestrator._phase7_costs_from_execution_ctx(None, {"fee_pct": 0.0004})
    assert costs["fee_cost"] == pytest.approx(0.0004)
