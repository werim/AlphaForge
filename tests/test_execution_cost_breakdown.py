
import json

import pytest

from alphaforge.effective_rr import calculate_effective_rr
from alphaforge.execution import build_execution_cost_breakdown, build_execution_cost_model


def _complete_cost_ctx(**overrides):
    ctx = {
        "spread_pct": 0.0,
        "expected_slippage_pct": 0.0,
        "fee_pct": 0.0,
        "funding_rate_pct": 0.0,
        "latency_ms": 0.0,
        "liquidity_score": 1.0,
        "volatility_regime": "normal",
    }
    ctx.update(overrides)
    return ctx


@pytest.mark.parametrize(("fee", "latency", "expected_fee", "expected_latency"), [
    (0.0004, 0.0, 0.004, 0.0),
    (0.0, 1000.0, 0.0, 0.2),
    (0.0004, 1000.0, 0.004, 0.2),
])
def test_cost_model_keyword_mapping_keeps_fee_and_latency_separate(
        fee, latency, expected_fee, expected_latency):
    ctx = _complete_cost_ctx(fee_pct=fee, latency_ms=latency)
    model = build_execution_cost_model(ctx)
    expected_total = expected_fee + expected_latency
    assert model.fee_penalty == pytest.approx(expected_fee)
    assert model.latency_penalty == pytest.approx(expected_latency)
    assert model.total_penalty == pytest.approx(expected_total)

    effective = calculate_effective_rr(2.0, ctx)
    assert effective.fee_penalty == pytest.approx(expected_fee)
    assert effective.latency_penalty == pytest.approx(expected_latency)
    assert effective.cost_penalty_total == pytest.approx(expected_total)
    assert effective.effective_rr == pytest.approx(2.0 - expected_total)


def test_constructor_mapping_fix_does_not_change_total_penalty_math():
    ctx = _complete_cost_ctx(
        spread_pct=0.001,
        expected_slippage_pct=0.002,
        fee_pct=0.0004,
        funding_rate_pct=0.0002,
        latency_ms=500.0,
        liquidity_score=0.75,
        volatility_regime="high",
    )
    expected = (0.001 * 25.0) + (0.002 * 30.0) + (0.0004 * 10.0) + \
        (0.0002 * 2.5) + ((500.0 / 1000.0) * 0.2) + ((1.0 - 0.75) * 0.6) + 0.12
    model = build_execution_cost_model(ctx)
    assert model.total_penalty == pytest.approx(expected)
    assert calculate_effective_rr(2.0, ctx).effective_rr == pytest.approx(2.0 - expected)


def test_missing_volatility_evidence_remains_fail_closed():
    ctx = _complete_cost_ctx(volatility_regime=None)
    model = build_execution_cost_model(ctx)
    assert model.volatility_penalty == pytest.approx(0.10)
    assert "volatility_regime" in model.missing_fields


def test_phase3_cost_breakdown_sources_and_fee_penalty_are_explicit():
    ctx = {
        "spread_pct": 0.002,
        "spread_status": "MEASURED",
        "spread_source": "BOOK_TICKER",
        "expected_slippage_pct": 0.003,
        "slippage_status": "ESTIMATED_BACKTEST",
        "slippage_source": "KLINE_RANGE_MODEL",
        "fee_pct": 0.0004,
        "fee_status": "MODELLED",
        "fee_source": "CONFIG",
        "funding_rate_pct": None,
        "funding_status": "UNAVAILABLE",
        "latency_ms": 100,
        "latency_status": "MODEL_ESTIMATE",
        "latency_source": "BACKTEST_ASSUMPTION",
        "liquidity_score": 0.2,
        "liquidity_status": "ESTIMATED_BACKTEST",
        "volatility_regime": "high",
        "volatility_status": "ESTIMATED_BACKTEST",
        "volatility_source": "CANDLE_RANGE",
        "REJECT_UNKNOWN_EXECUTION_CONTEXT": True,
    }
    b = build_execution_cost_breakdown(2.0, ctx, min_effective_rr=1.6, thresholds=ctx, include_missing_penalty=True)
    assert b.effective_rr < b.raw_rr
    assert b.fee_pct == pytest.approx(0.0004)
    diagnostics = json.loads(b.diagnostics_json)
    assert "fee_penalty" in diagnostics
    assert diagnostics["total_explicit_cost_pct"] == pytest.approx(0.0054)
    assert diagnostics["cost_penalty_rr"] == pytest.approx(b.cost_penalty_rr)
    assert b.as_dict()["total_explicit_cost_pct"] == pytest.approx(0.0054)
    assert b.as_dict()["total_cost_pct"] == pytest.approx(b.as_dict()["total_explicit_cost_pct"])
    assert b.funding_rate_pct is None
    assert "funding_rate_pct" in b.unavailable_fields
    assert b.spread_source == "MEASURED"
    assert b.slippage_source == "ESTIMATED_BACKTEST"
    assert "LOW_LIQUIDITY" in b.reject_flags
    assert "EXECUTION_CONTEXT_UNAVAILABLE" in b.reject_flags



def test_market_data_http_rtt_is_not_execution_latency_penalty():
    from alphaforge.execution import build_execution_context, build_execution_cost_model

    ctx = build_execution_context({
        "market_data_latency_ms": 1500.0,
        "market_data_latency_status": "MEASURED",
        "market_data_latency_source": "BINANCE_PUBLIC_HTTP_RTT",
        "spread_pct": 0.0001,
        "spread_status": "MEASURED",
        "expected_slippage_pct": 0.0001,
        "slippage_status": "MODEL_ESTIMATE",
        "fee_pct": 0.0004,
        "fee_status": "CONFIGURED",
        "funding_rate_pct": 0.00001,
        "funding_status": "MEASURED",
        "liquidity_score": 1.0,
        "liquidity_status": "MEASURED",
        "orderbook_imbalance": 0.1,
        "orderbook_status": "MEASURED",
        "volatility_regime": "normal",
        "volatility_status": "MEASURED",
    })

    assert ctx["market_data_latency_ms"] == pytest.approx(1500.0)
    assert ctx["market_data_latency_source"] == "BINANCE_PUBLIC_HTTP_RTT"

    # Public price-data RTT is evidence, not order execution latency.
    assert ctx["latency_ms"] is None
    assert ctx["latency_status"] == "UNAVAILABLE"
    assert ctx["latency_source"] == "UNAVAILABLE"

    model = build_execution_cost_model(ctx)
    assert model.latency_penalty == pytest.approx(0.0)
    assert "latency_ms" in model.missing_fields


def test_explicit_execution_latency_is_penalized_independently_of_market_data_rtt():
    from alphaforge.execution import build_execution_context, build_execution_cost_model

    ctx = build_execution_context({
        "market_data_latency_ms": 1500.0,
        "market_data_latency_status": "MEASURED",
        "market_data_latency_source": "BINANCE_PUBLIC_HTTP_RTT",
        "latency_ms": 250.0,
        "latency_status": "MODEL_ESTIMATE",
        "latency_source": "PAPER_EXECUTION_ASSUMPTION",
        "spread_pct": 0.0001,
        "spread_status": "MEASURED",
        "expected_slippage_pct": 0.0001,
        "slippage_status": "MODEL_ESTIMATE",
        "fee_pct": 0.0004,
        "fee_status": "CONFIGURED",
        "funding_rate_pct": 0.00001,
        "funding_status": "MEASURED",
        "liquidity_score": 1.0,
        "liquidity_status": "MEASURED",
        "orderbook_imbalance": 0.1,
        "orderbook_status": "MEASURED",
        "volatility_regime": "normal",
        "volatility_status": "MEASURED",
    })

    assert ctx["market_data_latency_ms"] == pytest.approx(1500.0)
    assert ctx["latency_ms"] == pytest.approx(250.0)
    assert ctx["latency_source"] == "PAPER_EXECUTION_ASSUMPTION"

    model = build_execution_cost_model(ctx)
    assert model.latency_penalty == pytest.approx(0.05)


def test_submit_ack_latency_overrides_modelled_execution_latency():
    from alphaforge.execution import build_execution_context, build_execution_cost_model

    ctx = build_execution_context({
        "market_data_latency_ms": 1500.0,
        "market_data_latency_status": "MEASURED",
        "market_data_latency_source": "BINANCE_PUBLIC_HTTP_RTT",
        "latency_ms": 250.0,
        "latency_status": "MODEL_ESTIMATE",
        "latency_source": "PAPER_EXECUTION_ASSUMPTION",
        "submit_ack_latency_ms": 80.0,
        "submit_ack_latency_status": "MEASURED",
        "submit_ack_latency_source": "RUNTIME_ACK",
        "spread_pct": 0.0001,
        "spread_status": "MEASURED",
        "expected_slippage_pct": 0.0001,
        "slippage_status": "MODEL_ESTIMATE",
        "fee_pct": 0.0004,
        "fee_status": "CONFIGURED",
        "funding_rate_pct": 0.00001,
        "funding_status": "MEASURED",
        "liquidity_score": 1.0,
        "liquidity_status": "MEASURED",
        "orderbook_imbalance": 0.1,
        "orderbook_status": "MEASURED",
        "volatility_regime": "normal",
        "volatility_status": "MEASURED",
    })

    assert ctx["latency_ms"] == pytest.approx(80.0)
    assert ctx["latency_status"] == "MEASURED"
    assert ctx["latency_source"] == "RUNTIME_ACK"

    model = build_execution_cost_model(ctx)
    assert model.latency_penalty == pytest.approx(0.016)


def test_normalized_execution_ctx_does_not_promote_market_data_latency_status():
    from alphaforge.order import normalize_execution_ctx

    normalized = normalize_execution_ctx({
        "latency_ms": None,
        "market_data_latency_status": "MEASURED",
    })

    assert normalized["latency_status"] == ""
    assert normalized["market_data_latency_status"] == "MEASURED"
