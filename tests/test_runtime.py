from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from sqlalchemy.orm import Session

from alphaforge.ai_brain import AIBrain
from alphaforge.persistence import init_db
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator, execution_mode_from_env


def _brain() -> AIBrain:
    engine = init_db("sqlite+pysqlite:///:memory:")
    return AIBrain(Session(engine), min_accept_score=0.62)


def test_execution_mode_from_env_parses_and_validates() -> None:
    assert execution_mode_from_env("paper") == ExecutionMode.PAPER
    assert execution_mode_from_env(None) == ExecutionMode.BACKTEST
    with pytest.raises(ValueError):
        execution_mode_from_env("sandbox")


def test_paper_execution_simulator_produces_fill() -> None:
    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=_brain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
    )
    result = orchestrator._simulate_paper_execution(
        symbol="BTCUSDT",
        decision={"order_type": "MARKET"},
        market_ctx={"entry": 100.0, "side": "LONG"},
    )
    assert result["status"] == "filled"
    assert result["fill_price"] > 100.0


def test_reject_lifecycle_persistence_increments_metrics() -> None:
    events: list[dict] = []
    rejects: list[dict] = []

    async def scanner() -> list[dict]:
        return [{"symbol": "BTCUSDT", "entry": 100.0, "sl": 99.5, "tp": 100.8, "rr": 1.0, "side": "LONG", "volume_24h_usdt": 5_000_000, "spread_pct": 0.01, "volatility_pct": 2.0, "trend_strength": 0.4, "liquidity_score": 0.8, "chop_score": 0.3}]

    def on_event(payload: dict) -> None:
        events.append(payload)

    def on_reject(payload: dict) -> None:
        rejects.append(payload)

    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.BACKTEST),
        ai_brain=_brain(),
        market_scanner=scanner,
        on_lifecycle_event=on_event,
        on_reject_persist=on_reject,
    )

    asyncio.run(orchestrator._scan_once())
    assert orchestrator.metrics.scans == 1
    assert orchestrator.metrics.decisions_generated == 1
    assert orchestrator.metrics.rejects_persisted == 1
    assert rejects and events


def test_rejected_signal_never_executes() -> None:
    async def scanner() -> list[dict]:
        return [{"symbol": "BTCUSDT", "entry": 100.0, "sl": 99.5, "tp": 100.8, "rr": 1.0, "side": "LONG", "volume_24h_usdt": 5_000_000, "spread_pct": 0.01, "volatility_pct": 2.0, "trend_strength": 0.4, "liquidity_score": 0.8, "chop_score": 0.3}]

    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.LIVE),
        ai_brain=_brain(),
        market_scanner=scanner,
        real_execution_adapter=None,
    )
    asyncio.run(orchestrator._scan_once())
    assert orchestrator.metrics.executions == 0


def test_shutdown_cancels_background_tasks() -> None:
    async def scanner() -> list[dict]:
        await asyncio.sleep(0.01)
        return []

    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.BACKTEST, scan_interval_sec=0.01, heartbeat_interval_sec=0.1),
        ai_brain=_brain(),
        market_scanner=scanner,
    )

    async def _run() -> None:
        task = asyncio.create_task(orchestrator.start())
        await asyncio.sleep(0.05)
        orchestrator.shutdown()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(_run())
    assert all(t.done() for t in orchestrator._tasks)


def test_before_real_order_not_offloaded_to_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scanner() -> list[dict]:
        return []

    async def _fail_to_thread(*args, **kwargs):
        raise AssertionError("asyncio.to_thread must not be used for before_real_order")

    monkeypatch.setattr(asyncio, "to_thread", _fail_to_thread)
    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.BACKTEST),
        ai_brain=_brain(),
        market_scanner=scanner,
    )
    asyncio.run(orchestrator._scan_once())


def test_ai_receives_execution_enriched_context() -> None:
    @dataclass(frozen=True)
    class _Score:
        total_score: float = 0.9
        expectancy_edge: float = 0.8
        components: dict = None
        penalties: dict = None
        accepted: bool = True
        reason_flags: list = None

    @dataclass(frozen=True)
    class _Plan:
        decision: str = "REJECTED"
        order_type: str = "REJECTED"
        limit_price: float | None = None
        stop_price: float | None = None
        confidence: float = 0.9
        reason: str = "test"

    class StubBrain:
        captured: dict | None = None

        def before_real_order(self, signal, market_ctx, regime_ctx, stats_ctx):
            self.captured = dict(market_ctx)
            return _Score(components={}, penalties={}, reason_flags=[]), _Plan(), "x"

    async def scanner() -> list[dict]:
        return [{"symbol": "BTCUSDT", "entry": 100.0, "sl": 99.5, "tp": 100.8, "rr": 1.0, "side": "LONG", "volume_24h_usdt": 5_000_000, "spread_pct": 0.01, "volatility_pct": 2.0, "trend_strength": 0.4, "liquidity_score": 0.8, "chop_score": 0.3}]

    brain = StubBrain()
    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.BACKTEST),
        ai_brain=brain,  # type: ignore[arg-type]
        market_scanner=scanner,
    )
    asyncio.run(orchestrator._scan_once())
    assert brain.captured is not None
    assert "expected_slippage_pct" in brain.captured
    assert "latency_ms" in brain.captured
    assert brain.captured["spread_pct"] == 0.01


def test_paper_mode_does_not_call_live_adapter() -> None:
    class LiveAdapter:
        def __init__(self) -> None:
            self.called = 0

        async def submit(self, decision, market_ctx):
            self.called += 1
            return {"status": "live"}

    async def scanner() -> list[dict]:
        return [{"symbol": "BTCUSDT", "entry": 100.0, "sl": 99.5, "tp": 100.8, "rr": 2.0, "score": 0.95, "expectancy": 0.2, "side": "LONG", "volume_24h_usdt": 5_000_000, "spread_pct": 0.01, "volatility_pct": 2.0, "trend_strength": 0.4, "liquidity_score": 0.8, "chop_score": 0.3}]

    adapter = LiveAdapter()
    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=_brain(),
        market_scanner=scanner,
        real_execution_adapter=adapter,
    )
    asyncio.run(orchestrator._scan_once())
    assert adapter.called == 0
