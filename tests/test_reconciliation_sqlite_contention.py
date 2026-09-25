from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from alphaforge.persistence import init_db
from alphaforge.reconciliation import ReconciliationFinding
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator
from alphaforge.runtime_state import RuntimeStateSnapshot, persist_reconciliation_cycle


class _Brain:
    session = None


class _Provider:
    def snapshot(self):
        return {"evidence_status": "COMPLETE", "captured_at": "2026-09-13T00:00:00Z",
                "orders": [], "positions": [], "fills": [], "authenticated": True}


async def _scanner():
    return []


def _runtime(engine):
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER, reconciliation_interval_sec=0.05),
        ai_brain=_Brain(), market_scanner=_scanner, persistence_engine=engine,
        live_reconciliation_provider=_Provider(),
    )
    runtime.metrics.persistence_enabled = True
    return runtime


def _lock(path):
    blocker = sqlite3.connect(path, timeout=0.01, check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")
    return blocker


def _rows(engine):
    with engine.connect() as conn:
        events = conn.execute(text("SELECT status,cycle_id FROM exchange_reconciliation_events ORDER BY id")).all()
        states = conn.execute(text("SELECT reconciliation_status,runtime_status FROM runtime_state_snapshots ORDER BY id")).all()
    return events, states


def test_transient_lock_retries_atomic_cycle_and_keeps_reconciliation_task_alive(tmp_path):
    path = tmp_path / "reconciliation.db"
    engine = init_db(f"sqlite+pysqlite:///{path}")
    runtime = _runtime(engine)
    blocker = _lock(path)
    released = threading.Event()

    def release():
        blocker.rollback()
        blocker.close()
        released.set()

    timer = threading.Timer(0.11, release)
    timer.start()

    async def run():
        task = asyncio.create_task(runtime._reconciliation_loop())
        await asyncio.sleep(0.25)
        assert not task.done()
        runtime._stop_event.set()
        await task

    try:
        asyncio.run(run())
    finally:
        timer.join(timeout=2)
        if not released.is_set():
            blocker.rollback()
            blocker.close()
    events, states = _rows(engine)
    assert released.is_set()
    assert [row[0] for row in events] == ["CLEAN"]
    assert events[0][1].startswith("recon:v1:")
    assert [row[0] for row in states] == ["CLEAN"]
    assert states[0][1] == "RECONCILED"
    assert runtime._fatal_task_exception is None
    assert runtime._fail_closed_reason is None


def test_persistent_lock_exhausts_budget_and_recovers_failure_evidence(tmp_path):
    path = tmp_path / "reconciliation.db"
    engine = init_db(f"sqlite+pysqlite:///{path}")
    runtime = _runtime(engine)
    blocker = _lock(path)
    started = time.monotonic()
    try:
        asyncio.run(runtime._run_reconciliation_once())
        assert time.monotonic() - started < 1.5
        assert runtime._reconciliation_status == "PERSISTENCE_FAILED"
        assert runtime._last_error.startswith("SQLITE_BUSY reconciliation persistence")
        assert runtime._evaluate_runtime_risk("BTCUSDT", {"market_ts": time.time()}) == "RECONCILIATION_PERSISTENCE_FAILED"
        assert runtime._pending_reconciliation_persistence_failures
    finally:
        blocker.rollback()
        blocker.close()
    events, states = _rows(engine)
    assert not events and not states
    asyncio.run(runtime._run_reconciliation_once())
    events, states = _rows(engine)
    assert [row[0] for row in events] == ["PERSISTENCE_FAILED", "CLEAN"]
    assert [row[0] for row in states] == ["CLEAN"]
    assert runtime._evaluate_runtime_risk("BTCUSDT", {"market_ts": time.time()}) is None


def test_same_observed_cycle_is_idempotent(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'reconciliation.db'}")
    runtime = _runtime(engine)
    asyncio.run(runtime._run_reconciliation_once())
    asyncio.run(runtime._run_reconciliation_once())
    events, states = _rows(engine)
    assert len(events) == len(states) == 1
    assert events[0][0] == states[0][0] == "CLEAN"


def test_non_busy_write_failure_rolls_back_event_and_findings_without_retry(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'reconciliation.db'}")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE runtime_state_snapshots"))
    finding = ReconciliationFinding("STALE_ORDER", "MEDIUM", "BTCUSDT", "order:o1",
                                    "2026-09-13T00:00:00Z", {"order_id": "o1"}, "refresh", False)
    snapshot = RuntimeStateSnapshot(mode="PAPER", requested_mode="PAPER", actual_mode="PAPER",
                                    runtime_status="RECOVERY_REQUIRED", reconciliation_status="DIRTY")
    with pytest.raises(OperationalError, match="no such table: runtime_state_snapshots"):
        persist_reconciliation_cycle(engine, cycle_id="recon:v1:rollback", findings=[finding],
                                     snapshot=snapshot, diagnostics={})
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM exchange_reconciliation_events")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM reconciliation_incidents")).scalar_one() == 0



def test_clean_reconciliation_releases_recovered_heartbeat_persistence_gate(tmp_path):
    path = tmp_path / "heartbeat-recovery-gate.db"
    engine = init_db(f"sqlite+pysqlite:///{path}")
    runtime = _runtime(engine)
    runtime._heartbeat_persistence_unhealthy = False
    runtime._heartbeat_persistence_failure_streak = 0
    runtime._fail_closed_reason = "RUNTIME_HEARTBEAT_PERSISTENCE_FAILED"
    runtime._runtime_status = "RECOVERY_REQUIRED"
    runtime._last_error = "SQLITE_BUSY runtime heartbeat persistence after 4 attempts"

    asyncio.run(runtime._run_reconciliation_once())

    assert runtime._fail_closed_reason is None
    assert runtime._last_error is None
    assert runtime._reconciliation_status == "CLEAN"
    assert runtime._runtime_status == "OPERATING"
    assert runtime._evaluate_runtime_risk(
        "BTCUSDT", {"market_ts": time.time()}
    ) is None
    events, states = _rows(engine)
    assert events[-1][0] == "CLEAN"
    assert states[-1][0] == "CLEAN"
    assert states[-1][1] == "RECONCILED"
