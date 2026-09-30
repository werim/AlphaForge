from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from alphaforge.portfolio_risk import risk_based_candidate_exposure
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


def _projection(**overrides):
    args = dict(
        equity=1_000.0,
        entry=100.0,
        stop=99.0,
        risk_pct_per_trade=0.01,
        rolling_drawdown_pct=0.0,
        max_rolling_drawdown_pct=0.08,
        expected_slippage_pct=0.0002,
        max_expected_slippage_pct=0.002,
        volatility_penalty_pct=0.02,
        max_volatility_penalty_pct=0.20,
        liquidity_depth_usdt=1_000_000.0,
        max_liquidity_participation_pct=0.01,
        max_leverage=1.0,
        min_notional=0.0,
        hard_notional_caps={
            "portfolio_remaining": 100_000.0,
            "symbol_remaining": 50_000.0,
            "correlation_remaining": 75_000.0,
        },
    )
    args.update(overrides)
    return risk_based_candidate_exposure(**args)


def test_stop_distance_derives_notional_from_risk_budget():
    tight = _projection(stop=99.5)
    wide = _projection(stop=98.0)
    assert tight["status"] == wide["status"] == "COMPLETE"
    assert tight["selected_notional"] > wide["selected_notional"]
    assert tight["risk_at_stop_usdt"] <= tight["risk_budget_usdt"] + 1e-9
    assert wide["risk_at_stop_usdt"] <= wide["risk_budget_usdt"] + 1e-9


def test_hard_caps_never_increase_risk():
    uncapped = _projection()
    capped = _projection(
        hard_notional_caps={
            "portfolio_remaining": 300.0,
            "symbol_remaining": 250.0,
            "correlation_remaining": 200.0,
        }
    )
    assert uncapped["status"] == capped["status"] == "COMPLETE"
    assert capped["selected_notional"] == pytest.approx(200.0)
    assert capped["selected_notional"] <= uncapped["selected_notional"]


def test_min_notional_rejects_instead_of_increasing_risk():
    result = _projection(
        equity=100.0,
        risk_pct_per_trade=0.001,
        stop=90.0,
        min_notional=5.0,
    )
    assert result["status"] == "REJECTED"
    assert result["reason"] == "BELOW_MIN_NOTIONAL"
    assert result["selected_notional"] < result["min_notional"]


def test_drawdown_de_risks_monotonically():
    normal = _projection(rolling_drawdown_pct=0.0)
    half_limit = _projection(rolling_drawdown_pct=0.04)
    assert normal["status"] == half_limit["status"] == "COMPLETE"
    assert half_limit["drawdown_multiplier"] == pytest.approx(0.5)
    assert half_limit["risk_budget_usdt"] < normal["risk_budget_usdt"]
    assert half_limit["selected_notional"] <= normal["selected_notional"]


def test_high_slippage_rejects_before_sizing():
    result = _projection(expected_slippage_pct=0.003, max_expected_slippage_pct=0.002)
    assert result["status"] == "REJECTED"
    assert result["reason"] == "EXCESSIVE_EXPECTED_SLIPPAGE"


def test_missing_authoritative_execution_limits_fail_closed():
    result = _projection(liquidity_depth_usdt=None)
    assert result["status"] == "UNAVAILABLE"
    assert result["reason"] == "RISK_SIZING_EXECUTION_EVIDENCE_UNAVAILABLE"


def test_risk_based_mode_is_paper_only():
    with pytest.raises(ValueError, match="PAPER-only"):
        RuntimeConfig(
            execution_mode=ExecutionMode.LIVE_PRECHECK,
            paper_position_sizing_mode="RISK_BASED",
        )


class _AlwaysAcceptBrain:
    def before_real_order(self, signal_payload, market_ctx, regime_ctx, stats_ctx):
        class _Plan:
            decision = "ACCEPTED"
            reason = ""
            confidence = 0.9
            order_type = "MARKET"
            limit_price = None
            stop_price = None
        return SimpleNamespace(total_score=9.0, components={}), _Plan(), "ok"


def _market():
    now = time.time()
    return {
        "signal_id": "issue365",
        "symbol": "BTCUSDT",
        "entry": 100.0,
        "sl": 99.0,
        "tp": 103.0,
        "rr": 3.0,
        "expectancy": 0.2,
        "side": "LONG",
        "equity": 1_000.0,
        "available_balance": 1_000.0,
        "market_ts": now,
        "volume_24h_usdt": 90_000_000.0,
        "spread_pct": 0.0002,
        "spread_status": "MEASURED",
        "spread_source": "TEST",
        "expected_slippage_pct": 0.0002,
        "slippage_status": "MEASURED",
        "slippage_source": "TEST",
        "latency_ms": 10.0,
        "latency_status": "MEASURED",
        "latency_source": "TEST",
        "funding_rate_pct": 0.00001,
        "funding_status": "MEASURED",
        "funding_source": "TEST",
        "liquidity_score": 1.0,
        "liquidity_status": "MEASURED",
        "liquidity_source": "TEST",
        "liquidity_depth_usdt": 1_000_000.0,
        "liquidity_depth_status": "MEASURED",
        "liquidity_depth_source": "TEST",
        "orderbook_imbalance": 0.0,
        "orderbook_status": "MEASURED",
        "orderbook_source": "TEST",
        "spoof_risk": 0.1,
        "spoof_status": "MEASURED",
        "spoof_source": "TEST",
        "spoof_confirmed": False,
        "absorption_score": 0.5,
        "absorption_status": "MEASURED",
        "absorption_source": "TEST",
        "absorption_execution_ok": True,
        "microstructure_observed_at": now,
        "volatility_regime": "normal",
        "volatility_status": "MEASURED",
        "volatility_source": "TEST",
        "volatility_penalty_pct": 0.02,
        "volatility_pct": 0.4,
        "trend_strength": 0.9,
        "chop_score": 0.1,
    }


def test_runtime_shadow_preserves_fixed_notional_and_records_counterfactual(monkeypatch):
    captured = []

    def capture(self, payload, **kwargs):
        captured.append(dict(payload))

    monkeypatch.setattr(RuntimeOrchestrator, "_persist_burnin_decision", capture)
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=ExecutionMode.PAPER,
            paper_position_sizing_mode="SHADOW",
            paper_candidate_notional=10.0,
            paper_initial_equity=1_000.0,
            paper_fee_bps=0.0,
            paper_execution_latency_ms=0.0,
            max_concurrent_positions=10,
            max_open_positions=10,
            max_notional_exposure=100_000.0,
            max_symbol_notional=50_000.0,
            max_correlation_group_exposure=75_000.0,
            max_correlated_positions=10,
        ),
        ai_brain=_AlwaysAcceptBrain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        paper_slippage_bps=2.0,
    )
    selection = SimpleNamespace(symbol="BTCUSDT", regime_hint="TREND", diagnostics={"inputs": _market()})
    asyncio.run(runtime._process_symbol(selection))
    accepted = [row for row in captured if row.get("decision") == "ACCEPTED"]
    assert accepted
    row = accepted[-1]
    assert row["paper_position_sizing_mode"] == "SHADOW"
    assert row["effective_notional"] == pytest.approx(10.0)
    assert row["risk_based_sizing"]["authority"] == "SHADOW_COUNTERFACTUAL"
    assert row["risk_based_sizing"]["status"] == "COMPLETE"
    assert row["risk_based_sizing"]["selected_notional"] > 10.0
