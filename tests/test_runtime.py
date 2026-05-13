from __future__ import annotations

import asyncio

import pytest

from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator, build_runtime_from_env, execution_mode_from_env, main


class DummyPlan:
    def __init__(self, decision: str, order_type: str = "MARKET", reason: str = "test") -> None:
        self.decision = decision
        self.order_type = order_type
        self.reason = reason


class DummyBrain:
    def __init__(self, decision: str = "REJECTED") -> None:
        self.decision = decision

    def before_real_order(self, signal, market_ctx, regime_ctx, stats_ctx):
        return {}, DummyPlan(self.decision), "explain"


def make_cfg(mode: ExecutionMode = ExecutionMode.PAPER) -> RuntimeConfig:
    return RuntimeConfig(mode, 0.01, 0.05, 0.01, 10, 2, True)


def test_execution_mode_from_env():
    assert execution_mode_from_env("backtest") == ExecutionMode.BACKTEST
    assert execution_mode_from_env("paper") == ExecutionMode.PAPER
    assert execution_mode_from_env("LIVE") == ExecutionMode.LIVE
    with pytest.raises(ValueError):
        execution_mode_from_env("bad")


def test_build_runtime_from_env_paper(monkeypatch):
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    runtime, run_once = build_runtime_from_env()
    assert runtime.config.execution_mode == ExecutionMode.PAPER
    assert run_once is False


def test_build_runtime_live_without_adapter_raises(monkeypatch):
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "LIVE")
    with pytest.raises(ValueError):
        build_runtime_from_env()


def test_run_once_exits(monkeypatch):
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    monkeypatch.setenv("ALPHAFORGE_RUN_ONCE", "true")
    runtime, run_once = build_runtime_from_env()

    async def run():
        await runtime.start(run_once=run_once)

    asyncio.run(run())
    assert runtime.metrics.scans == 1


def test_paper_does_not_call_live_adapter():
    async def scanner():
        return [{"symbol": "BTCUSDT", "entry": 100, "side": "BUY", "timeframe": "5m", "risk_reward": 2.0, "volume_24h_usdt": 10_000_000, "spread_pct": 0.05, "liquidity_score": 0.9, "volatility_pct": 2.0, "trend_strength": 0.7, "recent_volume_change_pct": 0.0, "chop_score": 0.2}]

    called = False

    class Adapter:
        async def submit(self, **kwargs):
            nonlocal called
            called = True
            return {}

    rt = RuntimeOrchestrator(make_cfg(ExecutionMode.PAPER), DummyBrain("ACCEPTED"), scanner, real_execution_adapter=Adapter())
    asyncio.run(rt._scan_once())
    assert called is False


def test_lifecycle_order_and_reject_no_execute():
    async def scanner():
        return [{"symbol": "BTCUSDT", "entry": 100, "side": "BUY", "timeframe": "5m", "risk_reward": 2.0, "volume_24h_usdt": 10_000_000, "spread_pct": 0.05, "liquidity_score": 0.9, "volatility_pct": 2.0, "trend_strength": 0.7, "recent_volume_change_pct": 0.0, "chop_score": 0.2}]

    events: list[str] = []
    rt = RuntimeOrchestrator(make_cfg(), DummyBrain("REJECTED"), scanner, on_lifecycle_event=lambda p: events.append(p["event"]))

    async def should_not_execute(*args, **kwargs):
        pytest.fail("reject should never execute")

    rt._execute = should_not_execute  # type: ignore[method-assign]
    asyncio.run(rt._scan_once())
    assert events[:2] == ["SIGNAL_CREATED", "SIGNAL_REJECTED"]


def test_entrypoint_main_starts_in_paper_with_stubs(monkeypatch):
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    monkeypatch.setenv("ALPHAFORGE_RUN_ONCE", "true")
    asyncio.run(main())
