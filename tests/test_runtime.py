from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator, execution_mode_from_env
from alphaforge.symbol_selector import SymbolSelectionResult


@dataclass
class DummyPlan:
    decision: str
    order_type: str = "MARKET"
    reason: str = ""


class DummyBrain:
    def __init__(self, decision: str = "REJECTED") -> None:
        self.decision = decision
        self.called = 0
        self.last_market_ctx = None

    def before_real_order(self, signal, market_ctx, regime_ctx, stats_ctx):
        self.called += 1
        self.last_market_ctx = dict(market_ctx)
        return {}, DummyPlan(decision=self.decision, reason="test"), "explain"


def make_cfg(mode: ExecutionMode = ExecutionMode.PAPER) -> RuntimeConfig:
    return RuntimeConfig(mode, 0.01, 0.05, 0.01, 10, 2, True)


def make_selection(**inputs):
    return SymbolSelectionResult(
        symbol=inputs.get("symbol", "BTCUSDT"), tradable=True, symbol_score=1, regime_hint="TREND",
        liquidity_score=1, volatility_score=1, trend_score=1, spread_score=1, volume_score=1,
        diagnostics={"inputs": inputs},
    )


def test_execution_mode_from_env():
    assert execution_mode_from_env("backtest") == ExecutionMode.BACKTEST
    assert execution_mode_from_env("paper") == ExecutionMode.PAPER
    assert execution_mode_from_env("LIVE") == ExecutionMode.LIVE
    with pytest.raises(ValueError):
        execution_mode_from_env("bad")


def test_signal_created_before_rejected_and_no_execute():
    async def run():
        events = []
        brain = DummyBrain("REJECTED")

        async def scanner():
            return []

        rt = RuntimeOrchestrator(make_cfg(), brain, scanner, on_lifecycle_event=lambda p: events.append(p))

        async def should_not_execute(*args, **kwargs):
            pytest.fail("execute should not be called")

        rt._execute = should_not_execute  # type: ignore[method-assign]
        await rt._process_symbol(make_selection(entry=1.0, side="BUY", timeframe="1h"))
        assert [e["event"] for e in events] == ["SIGNAL_CREATED", "SIGNAL_REJECTED"]

    asyncio.run(run())


def test_missing_context_rejects_before_ai():
    async def run():
        brain = DummyBrain("ACCEPTED")

        async def scanner():
            return []

        rt = RuntimeOrchestrator(make_cfg(), brain, scanner)
        await rt._process_symbol(make_selection(side="BUY", timeframe="1h"))
        assert brain.called == 0
        assert rt.metrics.rejects_persisted == 1

    asyncio.run(run())


def test_paper_mode_never_calls_live_adapter():
    async def run():
        called = False

        class Adapter:
            async def submit(self, **kwargs):
                nonlocal called
                called = True
                return {}

        brain = DummyBrain("ACCEPTED")

        async def scanner():
            return []

        rt = RuntimeOrchestrator(make_cfg(ExecutionMode.PAPER), brain, scanner, real_execution_adapter=Adapter())
        await rt._process_symbol(make_selection(entry=100, side="BUY", timeframe="5m"))
        assert called is False

    asyncio.run(run())


def test_live_mode_requires_adapter():
    brain = DummyBrain()

    async def scanner():
        return []

    rt = RuntimeOrchestrator(make_cfg(ExecutionMode.LIVE), brain, scanner)
    with pytest.raises(ValueError):
        asyncio.run(rt.start())


def test_execution_enrichment_reaches_brain_without_overwrite():
    async def run():
        brain = DummyBrain("REJECTED")

        async def scanner():
            return []

        rt = RuntimeOrchestrator(make_cfg(), brain, scanner)
        await rt._process_symbol(make_selection(entry=1, side="BUY", timeframe="1h", spread_pct=9.0))
        assert brain.last_market_ctx["spread_pct"] == 9.0
        assert "expected_slippage_pct" in brain.last_market_ctx

    asyncio.run(run())


def test_scan_timeout_and_failure_continue():
    async def run():
        brain = DummyBrain()
        calls = 0

        async def scanner():
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.sleep(0.05)
                return []
            if calls == 2:
                raise RuntimeError("boom")
            return []

        rt = RuntimeOrchestrator(make_cfg(), brain, scanner)
        await rt._scan_once()
        await rt._scan_once()
        await rt._scan_once()
        assert rt.metrics.scan_timeouts == 1
        assert rt.metrics.scan_failures == 1

    asyncio.run(run())


def test_task_registry_clears_after_shutdown():
    async def run():
        brain = DummyBrain()

        async def scanner():
            return []

        rt = RuntimeOrchestrator(make_cfg(), brain, scanner)
        t = asyncio.create_task(rt.start())
        await asyncio.sleep(0.03)
        rt.shutdown()
        await t
        assert rt._tasks == set()

    asyncio.run(run())


def test_register_signals_suppresses_errors(monkeypatch):
    brain = DummyBrain()

    async def scanner():
        return []

    rt = RuntimeOrchestrator(make_cfg(), brain, scanner)

    class Loop:
        def __init__(self):
            self.calls = 0

        def add_signal_handler(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise NotImplementedError
            raise RuntimeError

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: Loop())
    rt._register_signals()


def test_reject_log_bounded():
    async def run():
        brain = DummyBrain()

        async def scanner():
            return []

        rt = RuntimeOrchestrator(make_cfg(), brain, scanner)
        await rt._persist_reject({"id": 1})
        await rt._persist_reject({"id": 2})
        await rt._persist_reject({"id": 3})
        assert len(rt._reject_log) == 2

    asyncio.run(run())


def test_build_runtime_from_env_requires_explicit_non_simulated_modes(monkeypatch):
    from alphaforge.runtime import build_runtime_from_env

    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    with pytest.raises(ValueError):
        build_runtime_from_env()

    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "LIVE")
    with pytest.raises(ValueError):
        build_runtime_from_env()


def test_build_runtime_from_env_allows_explicit_simulated(monkeypatch):
    from alphaforge.runtime import build_runtime_from_env

    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "SIMULATED")
    rt, _ = build_runtime_from_env()
    assert rt.config.execution_mode == ExecutionMode.SIMULATED
