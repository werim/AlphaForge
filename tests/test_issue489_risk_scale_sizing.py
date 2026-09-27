from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from alphaforge.portfolio_risk import BacktestPortfolioState, scale_candidate_exposure
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


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


@pytest.mark.parametrize(
    ("scale", "expected"),
    [(0.0, 0.0), (0.5, 5.0), (1.0, 10.0)],
)
def test_canonical_exposure_scaling_boundaries(scale, expected):
    projection = scale_candidate_exposure(
        original_notional=10.0,
        risk_scale=scale,
        original_quantity=0.1,
        require_scale=True,
    )
    assert projection["status"] == "COMPLETE"
    assert projection["effective_notional"] == pytest.approx(expected)
    assert projection["effective_quantity"] == pytest.approx(0.1 * scale)


@pytest.mark.parametrize("bad_scale", [None, float("nan"), -0.01, 1.01, "bad"])
def test_required_risk_scale_invalid_or_missing_fails_closed(bad_scale):
    projection = scale_candidate_exposure(
        original_notional=10.0,
        risk_scale=bad_scale,
        require_scale=True,
    )
    assert projection["status"] == "INVALID"
    assert projection["effective_notional"] is None


def test_non_softened_missing_scale_remains_unscaled():
    projection = scale_candidate_exposure(
        original_notional=10.0,
        risk_scale=None,
        require_scale=False,
    )
    assert projection["status"] == "COMPLETE"
    assert projection["risk_scale"] == pytest.approx(1.0)
    assert projection["effective_notional"] == pytest.approx(10.0)


def test_backtest_notional_uses_same_scaling_authority():
    state = BacktestPortfolioState(initial_equity=1000.0)
    assert state.notional_for(entry=100.0, notional=10.0, risk_scale=0.5) == pytest.approx(5.0)
    assert state.notional_for(entry=100.0, notional=10.0, risk_scale=1.0) == pytest.approx(10.0)
    assert state.notional_for(entry=100.0, notional=10.0, risk_scale=0.0) is None


def _runtime(mode: ExecutionMode) -> RuntimeOrchestrator:
    return RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=mode,
            paper_initial_equity=1000.0,
            paper_candidate_notional=10.0,
            paper_fee_bps=0.0,
            paper_execution_latency_ms=0.0,
            max_concurrent_positions=10,
            max_open_positions=10,
            max_notional_exposure=100000.0,
            max_symbol_notional=100000.0,
            max_correlation_group_exposure=100000.0,
            max_correlated_positions=10,
        ),
        ai_brain=_AlwaysAcceptBrain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        paper_slippage_bps=2.0,
    )


def _wide_softenable_market() -> dict:
    return {
        "signal_id": "risk-scale-489",
        "entry": 100.0,
        "sl": 98.0,
        "tp": 105.0,
        "rr": 2.5,
        "expectancy": 0.2,
        "side": "LONG",
        "notional": 10.0,
        "quantity": 0.1,
        "equity": 1000.0,
        "available_balance": 1000.0,
        "market_ts": time.time(),
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
        "market_data_latency_ms": 10.0,
        "market_data_latency_status": "MEASURED",
        "market_data_latency_source": "TEST",
        "funding_rate_pct": 0.00001,
        "funding_status": "MEASURED",
        "funding_source": "TEST",
        "liquidity_score": 1.0,
        "liquidity_status": "MEASURED",
        "liquidity_source": "TEST",
        "orderbook_imbalance": 0.0,
        "orderbook_status": "MEASURED",
        "orderbook_source": "TEST",
        "volatility_regime": "normal",
        "volatility_status": "MEASURED",
        "volatility_source": "TEST",
    }


def _selection(market: dict):
    return SimpleNamespace(
        symbol="BTCUSDT",
        regime_hint="TREND",
        diagnostics={"inputs": market},
    )


def test_paper_softened_wide_stop_executes_scaled_notional_and_quantity():
    runtime = _runtime(ExecutionMode.PAPER)
    asyncio.run(runtime._process_symbol(_selection(_wide_softenable_market())))

    assert runtime.metrics.executions == 1
    assert runtime._active_positions["BTCUSDT"] == pytest.approx(5.0)


def test_live_precheck_projects_same_scaled_notional_without_submit(monkeypatch):
    captured: list[dict] = []

    def capture_decision(self, payload, **kwargs):
        captured.append(dict(payload))

    monkeypatch.setattr(RuntimeOrchestrator, "_persist_burnin_decision", capture_decision)
    runtime = _runtime(ExecutionMode.LIVE_PRECHECK)
    asyncio.run(runtime._process_symbol(_selection(_wide_softenable_market())))

    accepted = [row for row in captured if row.get("decision") == "ACCEPTED"]
    assert accepted
    row = accepted[-1]
    assert row["original_notional"] == pytest.approx(10.0)
    assert row["risk_scale"] == pytest.approx(0.5)
    assert row["effective_notional"] == pytest.approx(5.0)
    assert row["original_quantity"] == pytest.approx(0.1)
    assert row["effective_quantity"] == pytest.approx(0.05)
    assert runtime.metrics.executions == 0
