
import json

import pytest

from alphaforge.execution import build_execution_cost_breakdown


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
