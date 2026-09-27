from __future__ import annotations

import inspect

import pytest
from types import SimpleNamespace

import alphaforge.runtime as runtime_module
from alphaforge.ai_brain import AIBrain
from alphaforge.order import TradeQualityDecision
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


async def _scanner():
    return []


def _runtime() -> RuntimeOrchestrator:
    return RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=object(),
        market_scanner=_scanner,
    )


def _market() -> dict:
    return {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": 100.0,
        "sl": 99.0,
        "tp": 103.0,
        "rr": 3.0,
        "candidate_rr": 3.0,
        "expected_fill": 100.05,
        "effective_rr": 2.8,
        "expectancy": 0.2,
        "setup_type": "TREND_CONTINUATION",
        "setup_reason": "fixture",
        "regime": "TRENDING",
        "spread_pct": 0.001,
        "expected_slippage_pct": 0.0005,
        "atr_pct": 1.0,
        "volatility_regime": "normal",
        "execution_ctx": {
            "spread_pct": 0.001,
            "expected_slippage_pct": 0.0005,
            "funding_rate_pct": 0.0,
        },
    }


def test_runtime_quality_adapter_delegates_to_canonical_trade_quality(monkeypatch):
    seen = {}

    def fake_quality(candidate, market_ctx, recent_stats, config):
        seen["candidate"] = candidate
        seen["market_ctx"] = dict(market_ctx)
        seen["recent_stats"] = dict(recent_stats)
        seen["config"] = dict(config)
        return TradeQualityDecision(
            accepted=False,
            reject_reason="LOW_SCORE",
            diagnostics={"sentinel": "shared-authority"},
        )

    monkeypatch.setattr(runtime_module, "evaluate_trade_quality", fake_quality)
    result = _runtime()._evaluate_authoritative_trade_quality(
        symbol="BTCUSDT",
        market_ctx=_market(),
        signal_payload={
            "side": "LONG",
            "entry_price": 100.0,
            "stop_loss": 99.0,
            "take_profit": 103.0,
            "risk_reward": 3.0,
            "setup": "TREND_CONTINUATION",
            "setup_reason": "fixture",
            "regime": "TRENDING",
            "expectancy": 0.2,
        },
        score_ctx=SimpleNamespace(total_score=0.8),
        effective_rr=2.8,
    )

    assert result["accepted"] is False
    assert result["reject_reason"] == "LOW_SCORE"
    assert result["diagnostics"]["sentinel"] == "shared-authority"
    assert result["diagnostics"]["shared_quality_authority"] == "alphaforge.order.evaluate_trade_quality"
    assert seen["config"]["MODE"] == "PAPER"
    assert seen["config"]["RUNTIME_LIMITS_ACTIVE"] is False
    assert seen["recent_stats"] == {}
    assert seen["candidate"].regime == "TRENDING"
    assert seen["market_ctx"]["effective_rr"] == 2.8


def test_runtime_process_symbol_consumes_shared_quality_without_duplicate_threshold_math():
    source = inspect.getsource(RuntimeOrchestrator._process_symbol)
    assert "_evaluate_authoritative_trade_quality(" in source

    # These historical comparisons duplicated policy already owned by
    # evaluate_trade_quality / evaluate_stop_risk_policy.
    assert "if effective_rr < self.config.min_effective_rr" not in source
    assert "float(executable_stop_pct) < float(self.config.min_sl_pct)" not in source


def test_runtime_quality_adapter_keeps_runtime_risk_out_of_shared_stats():
    rt = _runtime()
    result = rt._evaluate_authoritative_trade_quality(
        symbol="BTCUSDT",
        market_ctx=_market(),
        signal_payload={
            "side": "LONG",
            "entry_price": 100.0,
            "stop_loss": 99.0,
            "take_profit": 103.0,
            "risk_reward": 3.0,
            "setup": "TREND_CONTINUATION",
            "setup_reason": "fixture",
            "regime": "TRENDING",
            "expectancy": 0.2,
        },
        score_ctx=SimpleNamespace(total_score=0.8),
        effective_rr=2.8,
    )
    # Portfolio/daily-loss/cooldown authority remains alphaforge.portfolio_risk;
    # shared candidate quality must not invent a second runtime-state model.
    assert "shared_quality_authority" in result["diagnostics"]


def test_aibrain_exposes_raw_expectancy_used_for_scoring():
    brain = AIBrain.for_stateless_scoring()
    score = brain.score_signal(
        {
            "symbol": "BTCUSDT",
            "setup": "TREND_CONTINUATION",
            "risk_reward": 2.0,
            "setup_quality": 0.9,
        },
        {
            "momentum_confirmation": 0.9,
            "liquidity_quality": 0.9,
            "volatility_fit": 0.9,
            "spread_bps": 1.0,
            "expected_slippage_pct": 0.0002,
            "latency_ms": 20.0,
            "funding_rate_pct": 0.0,
        },
        {"regime": "TRENDING", "alignment": 0.9},
        {
            "setup": {"TREND_CONTINUATION": 0.3},
            "regime": {"TRENDING": 0.2},
            "symbol": {"BTCUSDT": 0.1},
            "sample_size": 100,
        },
    )
    assert score.probabilistic["raw_expectancy"] == pytest.approx(0.2)
    assert score.probabilistic["raw_expectancy_source"] == "setup_regime_symbol_expectancy_stats"


def test_runtime_quality_adapter_prefers_aibrain_post_cost_expectancy(monkeypatch):
    seen = {}

    def fake_quality(candidate, market_ctx, recent_stats, config):
        seen["candidate"] = candidate
        seen["market_ctx"] = dict(market_ctx)
        return TradeQualityDecision(accepted=True, diagnostics={})

    monkeypatch.setattr(runtime_module, "evaluate_trade_quality", fake_quality)
    market = _market()
    market.pop("expectancy", None)
    result = _runtime()._evaluate_authoritative_trade_quality(
        symbol="BTCUSDT",
        market_ctx=market,
        signal_payload={
            "side": "LONG",
            "entry_price": 100.0,
            "stop_loss": 99.0,
            "take_profit": 103.0,
            "risk_reward": 3.0,
            "setup": "TREND_CONTINUATION",
            "setup_reason": "fixture",
            "regime": "TRENDING",
        },
        score_ctx=SimpleNamespace(
            total_score=0.8,
            probabilistic={
                "raw_expectancy": 0.15,
                "expectancy_after_costs": 0.07,
            },
        ),
        effective_rr=2.8,
    )

    assert result["accepted"] is True
    assert seen["candidate"].expectancy == 0.07
    assert seen["market_ctx"]["expectancy"] == 0.07
    assert result["diagnostics"]["expectancy_source"] == "score.expectancy_after_costs"
