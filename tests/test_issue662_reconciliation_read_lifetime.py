from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Mapping

import pytest

from sqlalchemy.orm import Session
from sqlalchemy import text

from alphaforge.ai_brain import AIBrain
from alphaforge.persistence import init_db
from alphaforge.reconciliation import ReconciliationFinding
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
            engine = orchestrator._resolve_persistence_engine()
            with engine.connect() as conn:
                diagnostics = conn.execute(text(
                    "SELECT diagnostics_json FROM exchange_reconciliation_events "
                    "WHERE status='EXCHANGE_STATE_UNKNOWN' ORDER BY id DESC LIMIT 1"
                )).scalar_one()
            details = json.loads(diagnostics)
            assert details["timeout_phase"] == "PROVIDER_READ"
            assert details["failure_class"] == "UNKNOWN"
            assert orchestrator._provider_failure_class == "UNKNOWN"
        finally:
            provider.release.set()
            await task
            await orchestrator._shutdown_tasks()

    asyncio.run(check())


def test_reconciliation_phase_advances_after_provider_read_and_persistence():
    class CompleteProvider:
        def snapshot(self):
            return {"evidence_status": "COMPLETE", "orders": [], "positions": [], "fills": []}

    orchestrator = runtime(CompleteProvider())
    phases = []

    async def check():
        try:
            await orchestrator._reconcile_runtime_state(on_phase=phases.append)
        finally:
            await orchestrator._shutdown_tasks()

    asyncio.run(check())
    assert phases == ["PROVIDER_READ", "RECONCILIATION", "PERSISTENCE"]


def test_timeout_after_provider_read_is_not_recorded_as_provider_read(monkeypatch):
    class CompleteProvider:
        def snapshot(self):
            return {"evidence_status": "COMPLETE", "orders": [], "positions": [], "fills": []}

    orchestrator = runtime(CompleteProvider())
    finding = ReconciliationFinding(
        finding_type="ORPHAN_ORDER", severity="HIGH", symbol="BTCUSDT",
        lifecycle_ref="test", timestamp="2026-11-02T00:00:00Z", evidence={},
        suggested_remediation="operator_review", fail_closed=True,
    )
    monkeypatch.setattr(orchestrator._reconciliation_engine, "reconcile", lambda **kwargs: ([finding], [], {}))

    async def delayed_event(*args, **kwargs):
        await asyncio.sleep(0.1)

    monkeypatch.setattr(RuntimeOrchestrator, "_emit_lifecycle_event", delayed_event)

    async def check():
        try:
            await orchestrator._run_reconciliation_once()
        finally:
            await orchestrator._shutdown_tasks()

    asyncio.run(check())
    with orchestrator._resolve_persistence_engine().connect() as conn:
        diagnostics = conn.execute(text(
            "SELECT diagnostics_json FROM exchange_reconciliation_events "
            "WHERE status='EXCHANGE_STATE_UNKNOWN' ORDER BY id DESC LIMIT 1"
        )).scalar_one()
    details = json.loads(diagnostics)
    assert details["timeout_phase"] == "LIFECYCLE_EVENT"
    assert details["failure_class"] == "UNKNOWN"
    assert orchestrator._provider_failure_class == "UNKNOWN"
    assert orchestrator._execution_reconciliation_blocked()


@pytest.mark.parametrize("mode", [ExecutionMode.PAPER, ExecutionMode.LIVE_PRECHECK])
def test_repeated_lifecycle_deadlines_preserve_unknown_failure_streak(monkeypatch, mode):
    class CompleteProvider:
        def snapshot(self):
            return {"evidence_status": "COMPLETE", "orders": [], "positions": [], "fills": []}

    orchestrator = runtime(CompleteProvider(), mode)
    cycles = iter(range(3))

    def findings(**kwargs):
        finding = ReconciliationFinding(
            finding_type="ORPHAN_ORDER", severity="HIGH", symbol="BTCUSDT",
            lifecycle_ref=f"cycle-{next(cycles)}", timestamp="2026-11-02T00:00:00Z",
            evidence={}, suggested_remediation="operator_review", fail_closed=True,
        )
        return [finding], [], {}

    original_emit = RuntimeOrchestrator._emit_lifecycle_event

    async def stalled_event(self, event, symbol, details=None):
        if symbol == "BTCUSDT":
            await asyncio.Event().wait()
        await original_emit(self, event, symbol, details)

    monkeypatch.setattr(orchestrator._reconciliation_engine, "reconcile", findings)
    monkeypatch.setattr(RuntimeOrchestrator, "_emit_lifecycle_event", stalled_event)

    async def check():
        try:
            for count in range(1, 4):
                await orchestrator._run_reconciliation_once()
                assert orchestrator._provider_failure_count == count
                assert orchestrator._provider_failure_class == "UNKNOWN"
                assert orchestrator._execution_reconciliation_blocked()
                assert orchestrator._stop_event.is_set() == (count == 3)
            with orchestrator._resolve_persistence_engine().connect() as conn:
                rows = conn.execute(text(
                    "SELECT diagnostics_json FROM exchange_reconciliation_events "
                    "WHERE status='EXCHANGE_STATE_UNKNOWN' ORDER BY id"
                )).scalars().all()
            timeouts = [json.loads(row) for row in rows if "timeout_phase" in json.loads(row)]
            assert len(timeouts) == 3
            assert all(row["timeout_phase"] == "LIFECYCLE_EVENT" for row in timeouts)
        finally:
            await orchestrator._shutdown_tasks()

    asyncio.run(check())


def test_interrupted_repair_delivery_retries_same_finding_before_deduplication(monkeypatch):
    class CompleteProvider:
        def snapshot(self):
            return {"evidence_status": "COMPLETE", "orders": [], "positions": [], "fills": []}

    orchestrator = runtime(CompleteProvider())
    finding = ReconciliationFinding(
        finding_type="ORPHAN_ORDER", severity="HIGH", symbol="BTCUSDT",
        lifecycle_ref="same-order", timestamp="2026-11-02T00:00:00Z",
        evidence={}, suggested_remediation="operator_review", fail_closed=True,
    )
    monkeypatch.setattr(orchestrator._reconciliation_engine, "reconcile", lambda **kwargs: ([finding], [], {}))
    attempts = []

    async def interrupted_first(payload):
        if payload["symbol"] == "BTCUSDT":
            attempts.append(payload["symbol"])
            if len(attempts) == 1:
                await asyncio.Event().wait()

    orchestrator.on_lifecycle_event = interrupted_first

    async def check():
        try:
            await orchestrator._run_reconciliation_once()
            assert not orchestrator._stop_event.is_set()
            await orchestrator._run_reconciliation_once()
            assert attempts == ["BTCUSDT", "BTCUSDT"]
            assert orchestrator._stop_event.is_set()
            assert "ORPHAN_ORDER:BTCUSDT:same-order" in orchestrator._last_repair_signature
            await orchestrator._run_reconciliation_once()
            assert attempts == ["BTCUSDT", "BTCUSDT"]
        finally:
            await orchestrator._shutdown_tasks()

    asyncio.run(check())


def test_completed_reconciliation_clears_prior_unknown_failure_streak():
    class CompleteProvider:
        def snapshot(self):
            return {"evidence_status": "COMPLETE", "orders": [], "positions": [], "fills": []}

    orchestrator = runtime(CompleteProvider())
    orchestrator._provider_failure_count = 2
    orchestrator._provider_failure_class = "UNKNOWN"
    orchestrator._unknown_exchange_state = True
    orchestrator._fail_closed_reason = "EXCHANGE_STATE_UNKNOWN"

    async def check():
        try:
            await orchestrator._run_reconciliation_once()
            assert orchestrator._provider_failure_count == 0
            assert orchestrator._provider_failure_class is None
            assert not orchestrator._execution_reconciliation_blocked()
            assert not orchestrator._stop_event.is_set()
        finally:
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

    class FirstCompleteThenBlocking(BlockingProvider):
        def snapshot(self):
            if self.calls == 0:
                self.calls += 1
                return {"evidence_status": "COMPLETE", "orders": [], "positions": [], "fills": []}
            return super().snapshot()

    provider = FirstCompleteThenBlocking() if phase == "operating" else BlockingProvider()
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
                # The real background loop makes its second read after the clean
                # startup read. Do not race it with an extra test-created read.
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


@pytest.mark.parametrize("mode", [ExecutionMode.PAPER, ExecutionMode.LIVE_PRECHECK])
def test_local_mapping_conversion_timeout_has_no_transport_evidence(mode):
    class BlockingMapping(Mapping):
        def __init__(self):
            self.entered = threading.Event()
            self.release = threading.Event()

        def __iter__(self):
            self.entered.set()
            self.release.wait(1)
            return iter(())

        def __len__(self):
            return 0

        def __getitem__(self, key):
            raise KeyError(key)

    mapping = BlockingMapping()

    class LocalProvider:
        def snapshot(self):
            return mapping

    orchestrator = runtime(LocalProvider(), mode)

    async def check():
        try:
            await orchestrator._run_reconciliation_once()
            assert mapping.entered.is_set()
            assert orchestrator._provider_failure_class == "UNKNOWN"
            assert orchestrator._transient_provider_outage_started_monotonic is None
            assert orchestrator._execution_reconciliation_blocked()
            with orchestrator._resolve_persistence_engine().connect() as conn:
                details = json.loads(conn.execute(text(
                    "SELECT diagnostics_json FROM exchange_reconciliation_events ORDER BY id DESC LIMIT 1"
                )).scalar_one())
            assert details["timeout_phase"] == "PROVIDER_READ"
            assert details["failure_class"] == "UNKNOWN"
        finally:
            mapping.release.set()
            await orchestrator._reconciliation_reader.close(1)

    asyncio.run(check())


@pytest.mark.parametrize("mode", [ExecutionMode.PAPER, ExecutionMode.LIVE_PRECHECK])
def test_repeated_provider_deadlines_pause_after_committed_third_unknown(monkeypatch, mode):
    provider = BlockingProvider()
    orchestrator = runtime(provider, mode)
    pauses = []

    def pause(self, reason):
        with self._resolve_persistence_engine().connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM exchange_reconciliation_events")).scalar_one()
        pauses.append((reason, count))
        self.shutdown()

    monkeypatch.setattr(RuntimeOrchestrator, "_pause_for_provider_failure", pause)

    async def check():
        try:
            for count in range(1, 4):
                provider.release.clear()
                await orchestrator._run_reconciliation_once()
                assert orchestrator._provider_failure_count == count
                assert orchestrator._provider_failure_class == "UNKNOWN"
                assert orchestrator._execution_reconciliation_blocked()
                assert orchestrator._stop_event.is_set() == (count == 3)
                assert pauses == ([] if count < 3 else [("PROVIDER_UNKNOWN_FAILURE_THRESHOLD", 3)])
                provider.release.set()
                while not orchestrator._reconciliation_reader._pending.done():
                    await asyncio.sleep(0.001)
            assert provider.calls == 3
        finally:
            provider.release.set()
            await orchestrator._reconciliation_reader.close(1)

    asyncio.run(check())


@pytest.mark.parametrize("notification_failure", ["stall", "error", "timeout_error"])
def test_timeout_incident_failure_cannot_block_committed_pause(monkeypatch, notification_failure):
    orchestrator = runtime(None)
    orchestrator._provider_failure_count = 2
    committed_at_pause = []

    async def stalled(self, *, on_phase):
        on_phase("LIFECYCLE_EVENT")
        await asyncio.Event().wait()

    async def notify(payload):
        assert payload["symbol"] == "GLOBAL"
        if notification_failure == "error":
            raise RuntimeError("synthetic notification failure")
        if notification_failure == "timeout_error":
            raise TimeoutError("synthetic callback timeout")
        await asyncio.Event().wait()

    def pause(self, reason):
        with self._resolve_persistence_engine().connect() as conn:
            row = conn.execute(text(
                "SELECT status, diagnostics_json FROM exchange_reconciliation_events"
            )).one()
        committed_at_pause.append((reason, row[0], json.loads(row[1])))
        self.shutdown()

    monkeypatch.setattr(RuntimeOrchestrator, "_reconcile_runtime_state", stalled)
    monkeypatch.setattr(RuntimeOrchestrator, "_pause_for_provider_failure", pause)
    orchestrator.on_lifecycle_event = notify

    async def check():
        await asyncio.wait_for(orchestrator._run_reconciliation_once(), timeout=2)

    asyncio.run(check())
    assert orchestrator._execution_reconciliation_blocked()
    assert orchestrator._provider_failure_class == "UNKNOWN"
    assert orchestrator._provider_failure_count == 3
    assert orchestrator._stop_event.is_set()
    assert len(committed_at_pause) == 1
    reason, status, details = committed_at_pause[0]
    assert reason == "PROVIDER_UNKNOWN_FAILURE_THRESHOLD"
    assert status == "EXCHANGE_STATE_UNKNOWN"
    assert details["timeout_phase"] == "LIFECYCLE_EVENT"
    assert details["failure_class"] == "UNKNOWN"
    assert details["timeout_incident_delivery"] == {
        "status": "TIMED_OUT" if notification_failure == "stall" else "FAILED",
        "error_type": "RuntimeError" if notification_failure == "error" else "TimeoutError",
    }
    assert details["errors"] == ["reconciliation_timeout", "timeout_incident_delivery_failed"]


def test_external_cancellation_during_timeout_incident_propagates(monkeypatch):
    orchestrator = runtime(None)

    async def stalled(self, *, on_phase):
        on_phase("LIFECYCLE_EVENT")
        await asyncio.Event().wait()

    monkeypatch.setattr(RuntimeOrchestrator, "_reconcile_runtime_state", stalled)

    async def check():
        entered = asyncio.Event()

        async def notify(payload):
            entered.set()
            await asyncio.Event().wait()

        orchestrator.on_lifecycle_event = notify
        task = asyncio.create_task(orchestrator._run_reconciliation_once())
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert orchestrator._execution_reconciliation_blocked()
            assert not orchestrator._stop_event.is_set()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())


@pytest.mark.parametrize("notification_fails", [False, True])
def test_timeout_persistence_failure_retains_containment_before_pause(monkeypatch, notification_fails):
    from alphaforge.runtime_state import ReconciliationPersistenceFailure

    orchestrator = runtime(None)
    orchestrator._provider_failure_count = 2

    async def stalled(self, *, on_phase):
        on_phase("PERSISTENCE")
        await asyncio.Event().wait()

    def fail_persistence(*args, **kwargs):
        raise ReconciliationPersistenceFailure("synthetic persistence failure")

    if notification_fails:
        async def notify(payload):
            raise RuntimeError("synthetic notification failure")
        orchestrator.on_lifecycle_event = notify

    monkeypatch.setattr(RuntimeOrchestrator, "_reconcile_runtime_state", stalled)
    monkeypatch.setattr("alphaforge.runtime.persist_reconciliation_cycle", fail_persistence)
    asyncio.run(orchestrator._run_reconciliation_once())
    assert orchestrator._provider_failure_class == "UNKNOWN"
    assert orchestrator._provider_failure_count == 3
    assert orchestrator._reconciliation_persistence_unhealthy
    assert orchestrator._pending_reconciliation_persistence_failures
    assert orchestrator._execution_reconciliation_blocked()
    assert not orchestrator._stop_event.is_set()


@pytest.mark.parametrize("mode, exception", [
    (ExecutionMode.PAPER, TimeoutError("explicit transport timeout")),
    (ExecutionMode.LIVE_PRECHECK, TimeoutError("explicit transport timeout")),
    (ExecutionMode.LIVE, TimeoutError("explicit transport timeout")),
    (ExecutionMode.PAPER, ConnectionResetError("reset")),
    (ExecutionMode.LIVE_PRECHECK, ConnectionResetError("reset")),
])
def test_completed_transport_exception_preserves_grace_policy(mode, exception):
    class FailedProvider:
        def snapshot(self):
            raise exception

    orchestrator = runtime(FailedProvider(), mode)

    async def check():
        try:
            for _ in range(3):
                await orchestrator._run_reconciliation_once()
                assert orchestrator._provider_failure_class == "TRANSIENT_TRANSPORT"
                assert not orchestrator._stop_event.is_set()
                assert orchestrator._execution_reconciliation_blocked()
            orchestrator._transient_provider_outage_started_monotonic -= (
                orchestrator.config.provider_transient_outage_grace_seconds + 1
            )
            await orchestrator._run_reconciliation_once()
            assert orchestrator._stop_event.is_set()
        finally:
            await orchestrator._reconciliation_reader.close(1)

    asyncio.run(check())


@pytest.mark.parametrize("phase, expired", [("PROVIDER_READ", True), ("LIFECYCLE_EVENT", False)])
def test_timeout_error_without_completed_provider_transport_stays_unknown(monkeypatch, phase, expired):
    orchestrator = runtime(None)

    async def raise_timeout(self, *, on_phase):
        on_phase(phase)
        if expired:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise TimeoutError("local cleanup after deadline") from None
        raise TimeoutError("local lifecycle timeout")

    monkeypatch.setattr(RuntimeOrchestrator, "_reconcile_runtime_state", raise_timeout)
    asyncio.run(orchestrator._run_reconciliation_once())
    assert orchestrator._provider_failure_class == "UNKNOWN"
    assert orchestrator._transient_provider_outage_started_monotonic is None
    assert orchestrator._execution_reconciliation_blocked()
    with orchestrator._resolve_persistence_engine().connect() as conn:
        details = json.loads(conn.execute(text(
            "SELECT diagnostics_json FROM exchange_reconciliation_events ORDER BY id DESC LIMIT 1"
        )).scalar_one())
    assert details["timeout_phase"] == phase
    assert details["failure_class"] == "UNKNOWN"
