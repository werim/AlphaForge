from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator
from alphaforge.order import validate_live_order_authorization


class _Adapter:
    def __init__(self) -> None:
        self.calls = 0

    async def submit(self, decision, market_ctx):
        self.calls += 1
        return {"order_id": f"order-{self.calls}", "status": "filled"}


class _ControlStore:
    def __init__(self) -> None:
        self.active = False

    def is_kill_switch_active(self) -> bool:
        return self.active


def _runtime() -> tuple[RuntimeOrchestrator, _Adapter, _ControlStore]:
    adapter = _Adapter()
    control = _ControlStore()
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=ExecutionMode.LIVE,
            live_trading_enabled=True,
            allow_live_orders=True,
            operator_live_acknowledged=True,
        ),
        ai_brain=None,
        market_scanner=None,
        real_execution_adapter=adapter,
        control_store=control,  # type: ignore[arg-type]
    )
    return runtime, adapter, control


async def _submit(runtime: RuntimeOrchestrator) -> None:
    await runtime._execute("BTCUSDT", {"order_type": "LIMIT"}, {"entry": 100.0})


def test_runtime_live_authorization_is_authoritative_and_refreshed(monkeypatch):
    async def scenario():
        await _runtime_live_authorization_scenario(monkeypatch)
    asyncio.run(scenario())


async def _runtime_live_authorization_scenario(monkeypatch):
    monkeypatch.setenv("ALPHAFORGE_ALLOW_LIVE_ORDERS", "true")
    runtime, adapter, control = _runtime()

    # No qualification/reconciliation state exists yet.
    with pytest.raises(RuntimeError, match="qualification_passed"):
        await _submit(runtime)
    assert adapter.calls == 0

    runtime._qualification_report = SimpleNamespace(qualified=False, verdict="NOT_LIVE_READY")
    runtime._reconciliation_status = "CLEAN"
    with pytest.raises(RuntimeError, match="qualification_passed"):
        await _submit(runtime)
    assert adapter.calls == 0

    runtime._qualification_report = SimpleNamespace(qualified=True, verdict="LIVE_READY")
    runtime._reconciliation_status = "EXCHANGE_STATE_UNKNOWN"
    with pytest.raises(RuntimeError, match="reconciliation_passed"):
        await _submit(runtime)
    assert adapter.calls == 0

    runtime._reconciliation_status = "CLEAN"
    control.active = True
    with pytest.raises(RuntimeError, match="KILL_SWITCH_ACTIVE"):
        await _submit(runtime)
    assert adapter.calls == 0

    control.active = False
    runtime.config.live_trading_enabled = False
    with pytest.raises(RuntimeError, match="live_trading_enabled"):
        await _submit(runtime)
    assert adapter.calls == 0

    runtime.config.live_trading_enabled = True
    monkeypatch.setenv("ALPHAFORGE_ALLOW_LIVE_ORDERS", "false")
    with pytest.raises(RuntimeError, match="allow_live_orders"):
        await _submit(runtime)
    assert adapter.calls == 0

    monkeypatch.setenv("ALPHAFORGE_ALLOW_LIVE_ORDERS", "true")
    await _submit(runtime)
    assert adapter.calls == 1

    # The provider is invoked again at the final boundary, so a kill-switch
    # change after the previous readiness snapshot cannot reuse stale PASS data.
    stale_context = runtime._build_live_order_execution_context("BTCUSDT", {"entry": 100.0})
    assert stale_context.storage["live_authorization"]["kill_switch_active"] is False
    control.active = True
    with pytest.raises(RuntimeError, match="kill_switch_inactive"):
        validate_live_order_authorization(stale_context)
    assert adapter.calls == 1


def test_runtime_non_live_execution_does_not_require_live_authorization():
    async def scenario():
        await _runtime_non_live_scenario()
    asyncio.run(scenario())


async def _runtime_non_live_scenario():
    paper = RuntimeOrchestrator(config=RuntimeConfig(execution_mode=ExecutionMode.PAPER), ai_brain=None, market_scanner=None)
    await paper._execute("BTCUSDT", {"order_type": "LIMIT", "limit_price": 100.0}, {"entry": 100.0})
    assert paper.metrics.executions == 1

    backtest = RuntimeOrchestrator(config=RuntimeConfig(execution_mode=ExecutionMode.BACKTEST), ai_brain=None, market_scanner=None)
    await backtest._execute("BTCUSDT", {"order_type": "LIMIT"}, {"entry": 100.0})
    assert backtest.metrics.executions == 1
