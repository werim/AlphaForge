from __future__ import annotations

import asyncio
import sqlite3

import pytest
from sqlalchemy import text

from alphaforge.persistence import init_db
from alphaforge.runtime import RuntimeOrchestrator, _build_runtime_from_env


def _runtime(tmp_path, monkeypatch, name: str):
    path = tmp_path / f"{name}.db"
    engine = init_db(f"sqlite+pysqlite:///{path}")
    monkeypatch.setenv("ALPHAFORGE_PERSISTENCE_ENABLED", "1")
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    monkeypatch.setenv("EXECUTION_MODE", "PAPER")
    monkeypatch.setenv("ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION", "false")
    monkeypatch.setenv("ALPHAFORGE_RUNTIME_SAFE_SCANNER", "1")
    runtime = _build_runtime_from_env(persistence_engine=engine)
    assert runtime.on_lifecycle_event is not None
    return path, engine, runtime


def _payload(signal_id: str = "issue569:signal") -> dict[str, object]:
    return {
        "lifecycle_event_type": "SIGNAL_CREATED",
        "lifecycle_state": "SIGNAL_CREATED",
        "signal_id": signal_id,
        "symbol": "BTCUSDT",
        "timestamp": "2026-10-01T02:51:37Z",
        "mode": "PAPER",
        "previous_lifecycle_state": None,
        "details": {},
    }


def _count(engine, signal_id: str = "issue569:signal") -> int:
    with engine.connect() as conn:
        return int(conn.execute(
            text("SELECT COUNT(*) FROM trade_lifecycle_events WHERE signal_id=:signal_id"),
            {"signal_id": signal_id},
        ).scalar_one())


def test_held_writer_lifecycle_contention_is_controlled_and_replay_idempotent(
    tmp_path, monkeypatch
):
    path, engine, runtime = _runtime(tmp_path, monkeypatch, "held-writer")
    blocker = sqlite3.connect(path, timeout=0.01)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        asyncio.run(runtime.on_lifecycle_event(_payload()))
    finally:
        blocker.rollback()
        blocker.close()

    assert _count(engine) == 0
    assert runtime.metrics.lifecycle_persistence_failures == 1
    assert runtime.metrics.lifecycle_persistence_degraded is True
    assert runtime._burnin_evidence_incomplete is True
    assert runtime._recovery_required is True
    assert runtime._runtime_status == "RECOVERY_REQUIRED"
    assert runtime._fail_closed_reason == "LIFECYCLE_PERSISTENCE_FAILED"
    assert runtime._fatal_task_exception is None

    asyncio.run(runtime.on_lifecycle_event(_payload()))
    asyncio.run(runtime.on_lifecycle_event(_payload()))

    assert _count(engine) == 1
    assert runtime.metrics.lifecycle_persistence_degraded is False
    assert runtime.metrics.lifecycle_persistence_recoveries == 1
    # A later successful write proves persistence recovered, but does not
    # independently prove runtime/reconciliation recovery. Stay fail-closed.
    assert runtime._recovery_required is True
    assert runtime._fail_closed_reason == "LIFECYCLE_PERSISTENCE_FAILED"

    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 30000
        assert str(conn.exec_driver_sql("PRAGMA journal_mode").scalar_one()).lower() == "wal"
        assert conn.exec_driver_sql("PRAGMA synchronous").scalar_one() == 1
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


def test_market_scan_loop_survives_lifecycle_sqlite_busy_exhaustion(
    tmp_path, monkeypatch
):
    path, _, runtime = _runtime(tmp_path, monkeypatch, "scan-loop")
    blocker = sqlite3.connect(path, timeout=0.01)
    blocker.execute("BEGIN IMMEDIATE")

    async def scan_once(self):
        await self.on_lifecycle_event(_payload("issue569:scan"))
        self._stop_event.set()

    monkeypatch.setattr(RuntimeOrchestrator, "_scan_once", scan_once)
    try:
        asyncio.run(runtime._market_scan_loop())
    finally:
        blocker.rollback()
        blocker.close()

    assert runtime._fatal_task_exception is None
    assert runtime.metrics.lifecycle_persistence_degraded is True
    assert runtime._recovery_required is True
    assert runtime._fail_closed_reason == "LIFECYCLE_PERSISTENCE_FAILED"


def test_non_busy_lifecycle_database_failure_remains_fatal(tmp_path, monkeypatch):
    _, engine, runtime = _runtime(tmp_path, monkeypatch, "non-busy")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE trade_lifecycle_events"))

    with pytest.raises(RuntimeError, match="trade_lifecycle_event_persistence_failed"):
        asyncio.run(runtime.on_lifecycle_event(_payload("issue569:fatal")))

    assert runtime.metrics.lifecycle_persistence_degraded is False
    assert runtime._recovery_required is False
