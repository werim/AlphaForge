from __future__ import annotations

import asyncio
import threading
import time

import pytest

from sqlalchemy.orm import Session

from alphaforge.ai_brain import AIBrain
from alphaforge.persistence import init_db
from alphaforge.readonly_snapshot_reader import ReadonlySnapshotReader
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


class BlockingProvider:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        self.entered.set()
        self.release.wait(0.5)
        return {"evidence_status": "COMPLETE", "orders": [], "positions": [], "fills": []}


def runtime(provider, mode=ExecutionMode.PAPER):
    return RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=mode, reconciliation_timeout_sec=0.03),
        ai_brain=AIBrain(Session(init_db("sqlite+pysqlite:///:memory:")), min_accept_score=0.62),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        live_reconciliation_provider=provider,
    )


@pytest.mark.parametrize("mode", [ExecutionMode.PAPER, ExecutionMode.LIVE_PRECHECK, ExecutionMode.LIVE])
def test_slow_provider_does_not_block_heartbeat_or_defeat_timeout(mode):
    provider = BlockingProvider()
    orchestrator = runtime(provider, mode)

    async def check():
        started = time.monotonic()
        task = asyncio.create_task(orchestrator._run_reconciliation_once())
        try:
            await asyncio.sleep(0.06)  # independent heartbeat can run while the read is blocked
            assert time.monotonic() - started < 0.3
            await task
            assert orchestrator._unknown_exchange_state
            assert orchestrator._exchange_read_only_status == "UNAVAILABLE"
            assert provider.calls == 1
        finally:
            provider.release.set()
            await task
            await orchestrator._shutdown_tasks()

    asyncio.run(check())


async def wait_entered(provider):
    for _ in range(100):
        if provider.entered.is_set():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("provider thread did not start")


@pytest.mark.parametrize("abandon", ["timeout", "cancel"])
def test_abandoned_complete_read_cannot_overlap_or_be_reused(abandon):
    provider = BlockingProvider()
    reader = ReadonlySnapshotReader()

    async def check():
        task = asyncio.create_task(reader.read(provider))
        try:
            await wait_entered(provider)
            if abandon == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(task, 0.01)
            with pytest.raises(RuntimeError, match="in_flight"):
                await reader.read(provider)
            assert provider.calls == 1
            provider.release.set()
            while not reader._pending.done():
                await asyncio.sleep(0.001)

            class Incomplete:
                def snapshot(self):
                    return {"evidence_status": "INCOMPLETE"}

            assert await reader.read(Incomplete()) == {"evidence_status": "INCOMPLETE"}
            assert not reader.has_unaccepted_read
        finally:
            provider.release.set()
            await reader.close(1)

    asyncio.run(check())


def test_concurrent_waiter_is_rejected_without_duplicate_provider_call():
    provider = BlockingProvider()
    reader = ReadonlySnapshotReader()

    async def check():
        first = asyncio.create_task(reader.read(provider))
        try:
            await wait_entered(provider)
            with pytest.raises(RuntimeError, match="in_flight"):
                await reader.read(provider)
            assert provider.calls == 1
        finally:
            provider.release.set()
            await first
            assert await reader.close(1)

    asyncio.run(check())


def test_provider_identity_drift_cannot_accept_old_complete_result():
    provider = BlockingProvider()
    orchestrator = runtime(provider)

    async def check():
        task = asyncio.create_task(orchestrator._reconcile_runtime_state())
        try:
            await wait_entered(provider)
            assert orchestrator._execution_reconciliation_blocked()
            orchestrator.live_reconciliation_provider = object()
            provider.release.set()
            await task
            assert orchestrator._unknown_exchange_state
            assert orchestrator._reconciliation_status != "CLEAN"
            assert orchestrator._exchange_read_only_status == "UNAVAILABLE"
        finally:
            provider.release.set()
            await orchestrator._shutdown_tasks()

    asyncio.run(check())


def test_shutdown_is_bounded_and_retains_recovery_for_unaccepted_read():
    provider = BlockingProvider()
    orchestrator = runtime(provider)

    async def check():
        task = asyncio.create_task(orchestrator._run_reconciliation_once())
        orchestrator._tasks = [task]
        try:
            await wait_entered(provider)
            started = time.monotonic()
            await orchestrator._shutdown_tasks()
            assert time.monotonic() - started < 0.3
            assert orchestrator._recovery_required
            assert orchestrator._unknown_exchange_state
            assert orchestrator._reconciliation_status != "CLEAN"
            assert orchestrator._fail_closed_reason == "RECONCILIATION_READ_UNACCEPTED_AT_SHUTDOWN"
            with pytest.raises(RuntimeError, match="closed"):
                await orchestrator._reconciliation_reader.read(provider)
        finally:
            provider.release.set()
            assert await orchestrator._reconciliation_reader.close(1)

    asyncio.run(check())


def test_provider_exception_propagates_and_does_not_poison_next_read():
    class Broken:
        def snapshot(self):
            raise OSError("transport unavailable")

    async def check():
        reader = ReadonlySnapshotReader()
        with pytest.raises(OSError, match="transport unavailable"):
            await reader.read(Broken())
        assert not reader.has_unaccepted_read
        provider = BlockingProvider()
        provider.release.set()
        assert (await reader.read(provider))["evidence_status"] == "COMPLETE"
        assert await reader.close(1)

    asyncio.run(check())


@pytest.mark.parametrize("phase", ["startup", "operating"])
def test_start_persists_recovery_before_releasing_ownership_with_pending_read(monkeypatch, phase):
    from alphaforge.runtime_state import latest_runtime_state_snapshot

    provider = BlockingProvider()
    if phase == "operating":
        provider.release.set()
    orchestrator = runtime(provider)
    orchestrator.metrics.persistence_enabled = True
    released = []
    monkeypatch.setattr(RuntimeOrchestrator, "_release_execution_ownership", lambda self: released.append(True))
    monkeypatch.setattr(RuntimeOrchestrator, "_register_signals", lambda self: None)

    async def check():
        task = asyncio.create_task(orchestrator.start())
        try:
            if phase == "startup":
                with pytest.raises(RuntimeError, match="EXCHANGE_STATE_UNKNOWN"):
                    await task
            else:
                for _ in range(100):
                    if orchestrator._tasks:
                        break
                    await asyncio.sleep(0.001)
                assert orchestrator._tasks
                provider.release.clear()
                provider.entered.clear()
                read_task = asyncio.create_task(orchestrator._reconcile_runtime_state())
                orchestrator._tasks.append(read_task)
                await wait_entered(provider)
                orchestrator.shutdown()
                await task
            snapshot = latest_runtime_state_snapshot(orchestrator._resolve_persistence_engine())
            assert snapshot["runtime_status"] == "RECOVERY_REQUIRED"
            assert "RECOVERY_REQUIRED" in snapshot["runtime_flags"]
            assert not released
        finally:
            provider.release.set()
            if not task.done():
                orchestrator.shutdown()
                await task
            assert await orchestrator._reconciliation_reader.close(1)

    asyncio.run(check())


def test_thread_start_failure_does_not_leave_a_phantom_read(monkeypatch):
    def fail_start(self):
        raise RuntimeError("cannot start thread")

    async def check():
        reader = ReadonlySnapshotReader()
        monkeypatch.setattr(threading.Thread, "start", fail_start)
        with pytest.raises(RuntimeError, match="cannot start thread"):
            await reader.read(BlockingProvider())
        assert not reader.has_unaccepted_read
        assert await reader.close(0.01)

    asyncio.run(check())


def test_failed_background_task_still_persists_failed_shutdown(monkeypatch):
    from alphaforge.runtime_state import latest_runtime_state_snapshot

    orchestrator = runtime(None, ExecutionMode.BACKTEST)
    orchestrator.metrics.persistence_enabled = True
    monkeypatch.setattr(RuntimeOrchestrator, "_register_signals", lambda self: None)

    async def fail_scan(self):
        raise ValueError("synthetic scan failure")

    monkeypatch.setattr(RuntimeOrchestrator, "_market_scan_loop", fail_scan)
    with pytest.raises(RuntimeError, match="MARKET_SCAN_LOOP_FAILED"):
        asyncio.run(orchestrator.start())
    snapshot = latest_runtime_state_snapshot(orchestrator._resolve_persistence_engine())
    assert snapshot["runtime_status"] == "FAILED"
    assert all(task.done() for task in orchestrator._tasks)
