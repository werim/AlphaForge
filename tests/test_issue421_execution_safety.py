from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from alphaforge.execution import evaluate_execution_safety
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

        return {}, _Plan(), "ok"


def _execution_ctx(**overrides: Any) -> dict[str, Any]:
    base = {
        "spread_pct": 0.0002,
        "spread_status": "MEASURED",
        "spread_source": "BOOK_TICKER",
        "expected_slippage_pct": 0.0002,
        "slippage_status": "MODEL_ESTIMATE",
        "slippage_source": "CONFIGURED_PAPER_ASSUMPTION",
        "latency_ms": 50.0,
        "latency_status": "MODEL_ESTIMATE",
        "latency_source": "CONFIGURED_PAPER_ASSUMPTION",
        "liquidity_score": 0.90,
        "liquidity_status": "MEASURED",
        "liquidity_source": "FIXTURE",
        "funding_rate_pct": 0.00005,
        "funding_status": "MEASURED",
        "funding_source": "FIXTURE",
        "orderbook_imbalance": 0.10,
        "orderbook_status": "MEASURED",
        "orderbook_source": "FIXTURE",
        "volatility_regime": "normal",
        "volatility_status": "MEASURED",
        "volatility_source": "FIXTURE",
        "fee_pct": 0.0004,
        "fee_status": "CONFIGURED",
        "fee_source": "FIXTURE",
    }
    base.update(overrides)
    return base


def _thresholds(**overrides: Any) -> dict[str, Any]:
    base = {
        "MAX_SPREAD_PCT": 0.0025,
        "MAX_EXPECTED_SLIPPAGE_PCT": 0.0020,
        "MAX_TOTAL_COST_PCT": 0.20,
        "MIN_LIQUIDITY_SCORE": 0.30,
        "MAX_VOLATILITY_PENALTY_PCT": 0.20,
        "REJECT_UNKNOWN_EXECUTION_CONTEXT": True,
        "MAX_LATENCY_MS": 2500,
        "MAX_ABS_FUNDING_RATE_PCT": 0.0010,
        "ENABLE_ORDERBOOK_FILTER": False,
    }
    base.update(overrides)
    return base


def test_execution_safety_complete_context_passes_without_recomputing_geometry() -> None:
    result = evaluate_execution_safety(
        _execution_ctx(),
        effective_rr=1.25,
        min_effective_rr=1.10,
        thresholds=_thresholds(),
    )
    assert result["accepted"] is True
    assert result["primary_reject_reason"] is None
    assert result["effective_rr"] == pytest.approx(1.25)
    assert result["all_failed_gates"] == []


@pytest.mark.parametrize(
    ("changes", "effective_rr", "expected_gate"),
    [
        ({"spread_pct": None, "spread_status": "UNAVAILABLE"}, 9.0, "EXECUTION_CONTEXT_UNAVAILABLE"),
        ({"expected_slippage_pct": None, "slippage_status": "UNAVAILABLE"}, 9.0, "EXECUTION_CONTEXT_UNAVAILABLE"),
        ({"latency_ms": None, "latency_status": "UNAVAILABLE"}, 9.0, "EXECUTION_CONTEXT_UNAVAILABLE"),
        ({"liquidity_score": None, "liquidity_status": "UNAVAILABLE"}, 9.0, "EXECUTION_CONTEXT_UNAVAILABLE"),
        ({"funding_rate_pct": None, "funding_status": "UNAVAILABLE"}, 9.0, "EXECUTION_CONTEXT_UNAVAILABLE"),
        ({"volatility_regime": None, "volatility_status": "UNAVAILABLE"}, 9.0, "EXECUTION_CONTEXT_UNAVAILABLE"),
        ({"spread_pct": 0.0030}, 9.0, "SPREAD_TOO_HIGH"),
        ({"expected_slippage_pct": 0.0030}, 9.0, "SLIPPAGE_TOO_HIGH"),
        ({"fee_pct": 0.1997}, 9.0, "HIGH_TOTAL_COST"),
        ({"liquidity_score": 0.10}, 9.0, "THIN_LIQUIDITY"),
        ({"latency_ms": 3000.0}, 9.0, "HIGH_LATENCY"),
        ({"volatility_regime": "extreme"}, 9.0, "EXCESSIVE_VOLATILITY"),
        ({"funding_rate_pct": 0.0020}, 9.0, "FUNDING_TOO_HIGH"),
        ({}, 1.00, "LOW_EFFECTIVE_RR"),
    ],
)
def test_each_protected_execution_gate_independently_kills_acceptance(
    changes: dict[str, Any],
    effective_rr: float,
    expected_gate: str,
) -> None:
    result = evaluate_execution_safety(
        _execution_ctx(**changes),
        effective_rr=effective_rr,
        min_effective_rr=1.10,
        thresholds=_thresholds(),
    )
    assert result["accepted"] is False
    assert expected_gate in result["all_failed_gates"]
    assert any(
        row["gate"] == expected_gate and row["source"] == "EXECUTION_SAFETY_CONTRACT"
        for row in result["failed_gate_evidence"]
    )


def test_live_precheck_requires_measured_execution_evidence() -> None:
    result = evaluate_execution_safety(
        _execution_ctx(),
        effective_rr=9.0,
        min_effective_rr=1.10,
        thresholds=_thresholds(),
        require_measured=True,
    )
    assert result["accepted"] is False
    assert result["primary_reject_reason"] == "EXECUTION_CONTEXT_UNAVAILABLE"
    assert {"expected_slippage_pct", "latency_ms"} <= set(result["missing_fields"])


def _market(**overrides: Any) -> dict[str, Any]:
    base = {
        "symbol": "BTCUSDT",
        "source_exchange": "fixture",
        "timeframe": "1m",
        "entry": 100.0,
        "sl": 99.0,
        "tp": 103.0,
        "rr": 3.0,
        "side": "LONG",
        "market_ts": time.time(),
        "volume_24h_usdt": 90_000_000.0,
        "spread_pct": 0.0002,
        "spread_status": "MEASURED",
        "expected_slippage_pct": 0.0002,
        "slippage_status": "MODEL_ESTIMATE",
        "liquidity_score": 0.90,
        "liquidity_status": "MEASURED",
        "funding_rate_pct": 0.00005,
        "funding_status": "MEASURED",
        "orderbook_imbalance": 0.10,
        "orderbook_status": "MEASURED",
        "volatility_regime": "normal",
        "volatility_status": "MEASURED",
        "trend_strength": 0.90,
        "chop_score": 0.10,
    }
    base.update(overrides)
    return base


def _run_paper(
    market: dict[str, Any],
    *,
    config: RuntimeConfig | None = None,
    paper_slippage_bps: float | None = 2.0,
) -> tuple[RuntimeOrchestrator, list[dict[str, Any]]]:
    rejects: list[dict[str, Any]] = []
    orchestrator = RuntimeOrchestrator(
        config=config or RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=_AlwaysAcceptBrain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        on_reject_persist=lambda payload: rejects.append(payload),
        paper_slippage_bps=paper_slippage_bps,
    )
    selection = SimpleNamespace(
        symbol=str(market["symbol"]),
        diagnostics={"inputs": market},
    )
    asyncio.run(orchestrator._process_symbol(selection))
    return orchestrator, rejects


@pytest.mark.parametrize(
    ("market_changes", "config", "paper_slippage_bps", "expected_gate"),
    [
        (
            {"spread_pct": None, "spread_status": "UNAVAILABLE", "rr": 10.0},
            RuntimeConfig(execution_mode=ExecutionMode.PAPER),
            2.0,
            "EXECUTION_CONTEXT_UNAVAILABLE",
        ),
        (
            {"expected_slippage_pct": None, "slippage_status": "UNAVAILABLE", "rr": 10.0},
            RuntimeConfig(execution_mode=ExecutionMode.PAPER),
            None,
            "EXECUTION_CONTEXT_UNAVAILABLE",
        ),
        (
            {"rr": 10.0},
            RuntimeConfig(execution_mode=ExecutionMode.PAPER, paper_execution_latency_ms=None),
            2.0,
            "EXECUTION_CONTEXT_UNAVAILABLE",
        ),
        (
            {"liquidity_score": None, "liquidity_status": "UNAVAILABLE", "rr": 10.0},
            RuntimeConfig(execution_mode=ExecutionMode.PAPER),
            2.0,
            "EXECUTION_CONTEXT_UNAVAILABLE",
        ),
        (
            {"funding_rate_pct": None, "funding_status": "UNAVAILABLE", "rr": 10.0},
            RuntimeConfig(execution_mode=ExecutionMode.PAPER),
            2.0,
            "EXECUTION_CONTEXT_UNAVAILABLE",
        ),
        (
            {"fee_pct": 0.1997, "fee_status": "CONFIGURED"},
            RuntimeConfig(execution_mode=ExecutionMode.PAPER, paper_fee_bps=None),
            2.0,
            "HIGH_TOTAL_COST",
        ),
        (
            {"liquidity_score": 0.10},
            RuntimeConfig(execution_mode=ExecutionMode.PAPER),
            2.0,
            "THIN_LIQUIDITY",
        ),
        (
            {"volatility_regime": "extreme"},
            RuntimeConfig(execution_mode=ExecutionMode.PAPER),
            2.0,
            "EXCESSIVE_VOLATILITY",
        ),
        (
            {},
            RuntimeConfig(execution_mode=ExecutionMode.PAPER, paper_execution_latency_ms=3000.0),
            2.0,
            "HIGH_LATENCY",
        ),
    ],
)
def test_paper_process_symbol_enforces_each_execution_safety_family(
    market_changes: dict[str, Any],
    config: RuntimeConfig,
    paper_slippage_bps: float | None,
    expected_gate: str,
) -> None:
    runtime, rejects = _run_paper(
        _market(**market_changes),
        config=config,
        paper_slippage_bps=paper_slippage_bps,
    )
    assert runtime.metrics.executions == 0
    assert rejects
    reject = rejects[-1]
    assert expected_gate in reject["all_failed_gates"]
    assert reject["execution_safety"]["accepted"] is False
    assert any(
        row["gate"] == expected_gate
        and row.get("source") == "EXECUTION_SAFETY_CONTRACT"
        for row in reject["failed_gate_evidence"]
    )


def test_high_raw_rr_cannot_bypass_unknown_execution_context() -> None:
    runtime, rejects = _run_paper(
        _market(
            rr=25.0,
            liquidity_score=None,
            liquidity_status="UNAVAILABLE",
        )
    )
    assert runtime.metrics.executions == 0
    assert rejects[-1]["reason"] == "EXECUTION_CONTEXT_UNAVAILABLE"
    assert "EXECUTION_CONTEXT_UNAVAILABLE" in rejects[-1]["all_failed_gates"]


def test_multi_gate_execution_reject_persists_all_failed_threshold_evidence() -> None:
    config = RuntimeConfig(
        execution_mode=ExecutionMode.PAPER,
        paper_execution_latency_ms=3000.0,
    )
    runtime, rejects = _run_paper(
        _market(
            spread_pct=0.01,
            liquidity_score=0.10,
            funding_rate_pct=0.01,
            volatility_regime="extreme",
        ),
        config=config,
    )
    assert runtime.metrics.executions == 0
    reject = rejects[-1]
    expected = {
        "SPREAD_TOO_HIGH",
        "THIN_LIQUIDITY",
        "HIGH_LATENCY",
        "EXCESSIVE_VOLATILITY",
        "FUNDING_TOO_HIGH",
    }
    assert expected <= set(reject["all_failed_gates"])
    evidence = {
        row["gate"]: row
        for row in reject["failed_gate_evidence"]
        if row.get("source") == "EXECUTION_SAFETY_CONTRACT"
    }
    assert expected <= set(evidence)
    for gate in expected:
        assert evidence[gate]["observed"] is not None
        assert evidence[gate]["threshold"] is not None
