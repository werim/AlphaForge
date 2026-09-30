from __future__ import annotations

from alphaforge.execution import (
    build_execution_context,
    evaluate_execution_safety,
    evaluate_microstructure_authority,
)


def _measured_market(**overrides):
    market = {
        "side": "LONG",
        "market_ts": 1_000.0,
        "spread_pct": 0.0005,
        "spread_status": "MEASURED",
        "spread_source": "BOOK_TICKER",
        "expected_slippage_pct": 0.0002,
        "slippage_status": "MEASURED",
        "slippage_source": "EXECUTION_MODEL",
        "latency_ms": 25.0,
        "latency_status": "MEASURED",
        "latency_source": "RUNTIME_ACK",
        "liquidity_score": 0.90,
        "liquidity_status": "MEASURED",
        "liquidity_source": "DEPTH_NORMALIZATION",
        "funding_rate_pct": 0.00005,
        "funding_status": "MEASURED",
        "funding_source": "BINANCE_FUNDING",
        "volatility_regime": "normal",
        "volatility_status": "MEASURED",
        "volatility_source": "KLINE_RANGE",
        "fee_pct": 0.0004,
        "fee_status": "CONFIGURED",
        "fee_source": "EXCHANGE_FEE_SCHEDULE",
        "orderbook_imbalance": 0.10,
        "orderbook_status": "MEASURED",
        "orderbook_source": "BINANCE_DEPTH",
        "spoof_risk": 0.10,
        "spoof_status": "MEASURED",
        "spoof_source": "MICROSTRUCTURE_PROVIDER",
        "spoof_confirmed": False,
        "absorption_score": 0.50,
        "absorption_status": "MEASURED",
        "absorption_source": "MICROSTRUCTURE_PROVIDER",
        "absorption_execution_ok": True,
        "liquidity_depth_usdt": 250_000.0,
        "liquidity_depth_status": "MEASURED",
        "liquidity_depth_source": "BINANCE_DEPTH",
        "microstructure_observed_at": 995.0,
    }
    market.update(overrides)
    return market


def _live_thresholds():
    return {
        "REQUIRE_LIVE_MICROSTRUCTURE": True,
        "MICROSTRUCTURE_MAX_AGE_SEC": 15.0,
        "REJECT_UNKNOWN_EXECUTION_CONTEXT": True,
        "ENABLE_ORDERBOOK_FILTER": False,
    }


def _live_safety(**overrides):
    ctx = build_execution_context(_measured_market(**overrides))
    return evaluate_execution_safety(
        ctx,
        effective_rr=2.0,
        min_effective_rr=1.1,
        thresholds=_live_thresholds(),
        require_measured=True,
    )


def test_missing_spoof_and_absorption_are_not_optimistic_zeroes() -> None:
    ctx = build_execution_context(
        _measured_market(
            spoof_risk=None,
            spoof_status="UNAVAILABLE",
            spoof_source="UNAVAILABLE",
            spoof_confirmed=None,
            absorption_score=None,
            absorption_status="UNAVAILABLE",
            absorption_source="UNAVAILABLE",
            absorption_execution_ok=None,
        )
    )
    assert ctx["spoof_risk"] is None
    assert ctx["absorption_score"] is None
    assert ctx["spoof_status"] == "UNAVAILABLE"
    assert ctx["absorption_status"] == "UNAVAILABLE"


def test_live_microstructure_complete_measured_passes() -> None:
    result = _live_safety()
    assert result["accepted"] is True
    assert result["microstructure_required"] is True
    assert result["microstructure"]["evidence_status"] == "COMPLETE_MEASURED"
    assert result["microstructure"]["live_equivalent"] is True


def test_live_precheck_missing_mandatory_microstructure_blocks() -> None:
    result = _live_safety(
        spoof_risk=None,
        spoof_status="UNAVAILABLE",
        spoof_source="UNAVAILABLE",
        spoof_confirmed=None,
    )
    assert result["accepted"] is False
    assert result["primary_reject_reason"] == "MICROSTRUCTURE_CONTEXT_UNAVAILABLE"
    assert "microstructure.spoof_risk" in result["missing_fields"]


def test_stale_microstructure_blocks() -> None:
    result = _live_safety(microstructure_observed_at=980.0)
    assert result["accepted"] is False
    assert result["primary_reject_reason"] == "STALE_MICROSTRUCTURE"
    assert result["microstructure"]["age_sec"] == 20.0


def test_confirmed_extreme_spoof_risk_blocks() -> None:
    result = _live_safety(spoof_risk=0.90, spoof_confirmed=True)
    assert result["accepted"] is False
    assert result["primary_reject_reason"] == "SPOOF_RISK"


def test_orderbook_imbalance_is_directional_not_absolute() -> None:
    favorable = _live_safety(orderbook_imbalance=0.95, side="LONG")
    adverse = _live_safety(orderbook_imbalance=-0.95, side="LONG")
    assert favorable["accepted"] is True
    assert adverse["accepted"] is False
    assert adverse["primary_reject_reason"] == "ADVERSE_ORDERBOOK_IMBALANCE"


def test_upstream_absorption_permission_is_authoritative_without_new_threshold() -> None:
    result = _live_safety(absorption_score=0.50, absorption_execution_ok=False)
    assert result["accepted"] is False
    assert result["primary_reject_reason"] == "ADVERSE_ABSORPTION"
    assert (
        result["microstructure"]["absorption_permission_semantics"]
        == "UPSTREAM_MEASURED_BOOLEAN_NO_LOCAL_NUMERIC_THRESHOLD"
    )


def test_paper_optional_microstructure_is_visibly_non_live_equivalent() -> None:
    ctx = build_execution_context(
        _measured_market(
            orderbook_imbalance=None,
            orderbook_status="UNAVAILABLE",
            orderbook_source="UNAVAILABLE",
            spoof_risk=None,
            spoof_status="UNAVAILABLE",
            spoof_source="UNAVAILABLE",
            spoof_confirmed=None,
            absorption_score=None,
            absorption_status="UNAVAILABLE",
            absorption_source="UNAVAILABLE",
            absorption_execution_ok=None,
            liquidity_depth_usdt=None,
            liquidity_depth_status="UNAVAILABLE",
            liquidity_depth_source="UNAVAILABLE",
            microstructure_observed_at=None,
        )
    )
    micro = evaluate_microstructure_authority(ctx, require_measured=False)
    safety = evaluate_execution_safety(
        ctx,
        effective_rr=2.0,
        min_effective_rr=1.1,
        thresholds={
            "REQUIRE_LIVE_MICROSTRUCTURE": False,
            "REJECT_UNKNOWN_EXECUTION_CONTEXT": True,
            "ENABLE_ORDERBOOK_FILTER": False,
        },
        require_measured=False,
    )
    assert safety["accepted"] is True
    assert micro["live_equivalent"] is False
    assert micro["evidence_status"] == "UNAVAILABLE_BLOCKING"
