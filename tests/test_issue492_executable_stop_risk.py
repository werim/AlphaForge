from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from alphaforge.config_registry import decision_filter_config
from alphaforge.execution import (
    EXECUTION_EVIDENCE_UNAVAILABLE_BLOCKING,
    build_stop_risk_metrics,
    evaluate_stop_risk_policy,
)
from alphaforge.order import OrderCandidate, evaluate_trade_quality
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


def _policy_config() -> dict[str, object]:
    return decision_filter_config("PAPER")


@pytest.mark.parametrize(
    ("side", "stop", "expected_fill", "expected_pct"),
    [
        ("LONG", 98.55, 100.10, abs(100.10 - 98.55) / 100.10 * 100.0),
        ("SHORT", 101.45, 99.90, abs(99.90 - 101.45) / 99.90 * 100.0),
    ],
)
def test_adverse_fill_can_cross_max_stop_symmetrically(side, stop, expected_fill, expected_pct):
    metrics = build_stop_risk_metrics(
        planned_entry=100.0,
        expected_fill=expected_fill,
        stop=stop,
    )
    assert metrics["planned_stop_distance_pct"] == pytest.approx(1.45)
    assert metrics["executable_stop_distance_pct"] == pytest.approx(expected_pct)
    assert metrics["executable_stop_distance_pct"] > 1.5
    decision = evaluate_stop_risk_policy(
        metrics,
        score=8.0,
        effective_rr=1.5,
        config=_policy_config(),
    )
    assert decision["accepted"] is False
    assert decision["reject_reason"] == "STOP_TOO_WIDE"
    assert decision["stop_distance_basis"] == "EXPECTED_FILL"


@pytest.mark.parametrize("stop", [99.85, 98.50])
def test_exact_stop_boundaries_are_not_rejected(stop):
    metrics = build_stop_risk_metrics(
        planned_entry=100.0,
        expected_fill=100.0,
        stop=stop,
    )
    decision = evaluate_stop_risk_policy(
        metrics,
        score=8.0,
        effective_rr=2.0,
        config=_policy_config(),
    )
    assert decision["accepted"] is True
    assert decision["reject_reason"] == ""


def test_missing_expected_fill_is_blocking_outside_backtest_fallback():
    metrics = build_stop_risk_metrics(
        planned_entry=100.0,
        expected_fill=None,
        stop=99.0,
    )
    assert metrics["stop_risk_evidence_status"] == EXECUTION_EVIDENCE_UNAVAILABLE_BLOCKING
    decision = evaluate_stop_risk_policy(
        metrics,
        score=9.0,
        effective_rr=2.0,
        config=_policy_config(),
    )
    assert decision["accepted"] is False
    assert decision["reject_reason"] == "UNKNOWN_EXECUTION_CONTEXT"


def test_trade_quality_uses_expected_fill_not_planned_entry_for_stop_gate():
    candidate = OrderCandidate(
        symbol="BTCUSDT",
        side="LONG",
        setup_type="TREND_CONTINUATION_LONG",
        setup_reason="test",
        regime="TREND",
        score=8.5,
        rr=1.7,
        expectancy=0.2,
        entry=100.0,
        sl=98.55,
        tp=102.465,
    )
    market = {
        "regime": "TREND",
        "volatility_regime": "normal",
        "spread_pct": 0.01,
        "expected_slippage_pct": 0.001,
        "effective_rr": 1.7,
        "atr_pct": 1.0,
        "timestamp": 1_000_000,
    }
    decision = evaluate_trade_quality(candidate, market, {}, {"MODE": "PAPER"})

    assert decision.reject_reason == "STOP_TOO_WIDE"
    assert decision.diagnostics["planned_stop_distance_pct"] == pytest.approx(1.45)
    assert decision.diagnostics["executable_stop_distance_pct"] > 1.5
    assert decision.diagnostics["sl_pct"] == pytest.approx(
        decision.diagnostics["executable_stop_distance_pct"]
    )
    assert decision.diagnostics["stop_distance_basis"] == "EXPECTED_FILL"


def test_trade_quality_missing_expected_fill_fails_closed_in_paper():
    candidate = OrderCandidate(
        symbol="BTCUSDT",
        side="LONG",
        setup_type="TREND_CONTINUATION_LONG",
        setup_reason="test",
        regime="TREND",
        score=8.5,
        rr=2.0,
        expectancy=0.2,
        entry=100.0,
        sl=99.0,
        tp=102.0,
    )
    market = {
        "regime": "TREND",
        "volatility_regime": "normal",
        "spread_pct": 0.01,
        "atr_pct": 1.0,
        "timestamp": 1_000_000,
    }
    decision = evaluate_trade_quality(candidate, market, {}, {"MODE": "PAPER"})
    assert decision.accepted is False
    assert decision.reject_reason == "UNKNOWN_EXECUTION_CONTEXT"
    assert decision.diagnostics["stop_risk_evidence_status"] == EXECUTION_EVIDENCE_UNAVAILABLE_BLOCKING


def _runtime(*, paper_slippage_bps: float = 10.0) -> RuntimeOrchestrator:
    return RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=ExecutionMode.PAPER,
            min_effective_rr=1.1,
            paper_fee_bps=0.0,
            paper_execution_latency_ms=0.0,
        ),
        ai_brain=_AlwaysAcceptBrain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        paper_slippage_bps=paper_slippage_bps,
    )


@pytest.mark.parametrize(
    ("side", "stop", "target"),
    [
        ("LONG", 98.55, 102.425),
        ("SHORT", 101.45, 97.575),
    ],
)
def test_runtime_rejects_wide_stop_that_only_crosses_limit_after_expected_fill(side, stop, target):
    rejects: list[dict] = []
    runtime = _runtime()
    runtime.on_reject_persist = lambda payload: rejects.append(dict(payload))
    market = {
        "entry": 100.0,
        "sl": stop,
        "tp": target,
        "rr": 1.67,
        "expectancy": 0.2,
        "side": side,
        "market_ts": time.time(),
        "volume_24h_usdt": 90_000_000.0,
        "spread_pct": 0.0,
        "expected_slippage_pct": 0.001,
        "latency_ms": 0.0,
        "funding_rate_pct": 0.0,
        "liquidity_score": 1.0,
        "volatility_regime": "normal",
        "volatility_status": "MODEL_ESTIMATE",
        "volatility_source": "TEST_EXECUTION_ASSUMPTION",
    }

    asyncio.run(runtime._process_symbol(SimpleNamespace(
        symbol="BTCUSDT", regime_hint="TREND", diagnostics={"inputs": market},
    )))

    assert runtime.metrics.executions == 0
    assert rejects
    reject = rejects[-1]
    assert reject["reason"] == "STOP_TOO_WIDE"
    assert reject["planned_stop_distance_pct"] == pytest.approx(1.45)
    assert reject["executable_stop_distance_pct"] > 1.5
    assert reject["stop_distance_pct"] == pytest.approx(reject["executable_stop_distance_pct"])
    assert reject["stop_distance_basis"] == "EXPECTED_FILL"


def test_canonical_reject_audit_uses_executable_stop_observed_value():
    runtime = _runtime()
    payload = runtime._canonical_reject_payload({
        "signal_id": "sig-492",
        "symbol": "BTCUSDT",
        "decision": "REJECTED",
        "reason": "LOW_SCORE",
        "score": 8.0,
        "candidate_rr": 1.7,
        "effective_rr": 1.5,
        "entry": 100.0,
        "expected_fill": 100.1,
        "sl": 98.55,
        "tp": 102.425,
        "execution_ctx": {
            "spread_pct": 0.0,
            "expected_slippage_pct": 0.001,
            "latency_ms": 0.0,
            "funding_rate_pct": 0.0,
            "liquidity_score": 1.0,
            "volatility_regime": "normal",
        },
    })

    by_gate = {row["gate"]: row for row in payload["failed_gate_evidence"]}
    assert "STOP_TOO_WIDE" in payload["all_failed_gates"]
    assert by_gate["STOP_TOO_WIDE"]["source"] == "EXECUTABLE_STOP_RISK"
    assert by_gate["STOP_TOO_WIDE"]["observed"] == pytest.approx(
        payload["executable_stop_distance_pct"]
    )
    assert payload["planned_stop_distance_pct"] == pytest.approx(1.45)
    assert payload["stop_distance_basis"] == "EXPECTED_FILL"
