from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import threading
import time

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError

from alphaforge.persistence import init_db
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator
from alphaforge.runtime_heartbeat import (
    ensure_runtime_heartbeat_schema,
    evaluate_runtime_heartbeat_freshness,
    fetch_latest_runtime_heartbeat,
    save_runtime_heartbeat,
)


class _NoopBrain:
    pass


def _runtime(engine, mode: ExecutionMode) -> RuntimeOrchestrator:
    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=mode),
        ai_brain=_NoopBrain(),
        market_scanner=lambda: None,
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
        persistence_engine=engine,
    )
    orchestrator.metrics.persistence_enabled = True
    return orchestrator


def test_runtime_owned_paper_heartbeat_persists_operating_evidence(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'paper_runtime.db'}")
    runtime = _runtime(engine, ExecutionMode.PAPER)
    runtime.metrics.scans = 3
    runtime.metrics.last_scan_ts = datetime.now(timezone.utc).isoformat()
    runtime._persist_runtime_heartbeat()

    latest = fetch_latest_runtime_heartbeat(engine)
    assert latest is not None
    assert latest["runtime_instance_id"] == runtime.runtime_instance_id
    assert latest["execution_mode"] == "PAPER"
    assert latest["runtime_state"] == "OPERATING"
    assert latest["evidence_status"] == "MEASURED_RUNTIME_HEARTBEAT"


def test_backtest_runtime_does_not_persist_heartbeat_evidence(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'backtest_runtime.db'}")
    runtime = _runtime(engine, ExecutionMode.BACKTEST)
    runtime._persist_runtime_heartbeat()
    assert not inspect(engine).has_table("runtime_heartbeats")


def test_payload_json_uses_allowlist_and_excludes_credentials(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'redaction.db'}")
    save_runtime_heartbeat(
        engine,
        runtime_instance_id="runtime:payload",
        execution_mode="PAPER",
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
        payload={"scans": 2, "api_key": "must-not-persist", "signature": "must-not-persist"},
    )
    with engine.connect() as conn:
        payload_json = conn.execute(text("SELECT payload_json FROM runtime_heartbeats LIMIT 1")).scalar_one()
    payload = json.loads(payload_json)
    assert payload == {"scans": 2}


def test_payload_json_preserves_mtf_observability_counters(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'mtf_counters.db'}")
    mtf_counters = {
        "mtf_contexts_built": 257,
        "mtf_alignment_pass": 0,
        "mtf_alignment_reject": 257,
        "mtf_regime_missing": 0,
        "mtf_setup_missing": 89,
        "mtf_execution_missing": 0,
        "mtf_execution_not_confirmed": 257,
        "mtf_execution_counter_regime": 168,
        "mtf_direction_mismatch": 168,
        "mtf_stale_context": 0,
        "mtf_guided_candidates_generated": 42,
        "mtf_legacy_candidates_shadowed": 17,
        "mtf_setup_continuation": 19,
        "mtf_setup_pullback": 15,
        "mtf_setup_reentry_ready": 8,
        "mtf_setup_no_setup": 21,
        "mtf_setup_overextended": 5,
        "mtf_setup_invalid": 2,
        "top_selection_advisory_reasons": {"TOO_CHOPPY": 2},
    }
    save_runtime_heartbeat(
        engine,
        runtime_instance_id="runtime:mtf-observability",
        execution_mode="PAPER",
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
        payload={**mtf_counters, "api_key": "must-not-persist"},
    )

    with engine.connect() as conn:
        payload_json = conn.execute(text(
            "SELECT payload_json FROM runtime_heartbeats LIMIT 1"
        )).scalar_one()

    assert json.loads(payload_json) == mtf_counters


def test_freshness_states_are_deterministic_and_fail_closed(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'freshness.db'}")
    now = datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc)
    assert evaluate_runtime_heartbeat_freshness(engine, now=now).state == "MISSING"

    save_runtime_heartbeat(
        engine,
        runtime_instance_id="runtime:stale",
        execution_mode="LIVE",
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
        heartbeat_ts=(now - timedelta(seconds=121)).isoformat(),
    )
    assert evaluate_runtime_heartbeat_freshness(engine, required_mode="LIVE", max_age_sec=120, now=now).state == "STALE"

    save_runtime_heartbeat(
        engine,
        runtime_instance_id="runtime:future",
        execution_mode="LIVE",
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
        heartbeat_ts=(now + timedelta(seconds=30)).isoformat(),
    )
    assert evaluate_runtime_heartbeat_freshness(engine, required_mode="LIVE", now=now).state == "FUTURE_DATED"

    save_runtime_heartbeat(
        engine,
        runtime_instance_id="runtime:invalid",
        execution_mode="LIVE",
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
        heartbeat_ts="invalid",
    )
    assert evaluate_runtime_heartbeat_freshness(engine, required_mode="LIVE", now=now).state == "INVALID"


def test_latest_heartbeat_selection_prefers_last_insert_for_equal_timestamp(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'latest.db'}")
    heartbeat_ts = "2026-05-23T12:00:00+00:00"
    save_runtime_heartbeat(engine, runtime_instance_id="runtime:a", execution_mode="PAPER", scanner_source="EXCHANGE_PUBLIC_MARKET_DATA", heartbeat_ts=heartbeat_ts)
    save_runtime_heartbeat(engine, runtime_instance_id="runtime:b", execution_mode="PAPER", scanner_source="EXCHANGE_PUBLIC_MARKET_DATA", heartbeat_ts=heartbeat_ts)
    assert fetch_latest_runtime_heartbeat(engine)["runtime_instance_id"] == "runtime:b"



def _locked_error() -> OperationalError:
    return OperationalError(
        "INSERT",
        {},
        sqlite3.OperationalError("database is locked"),
    )


def test_canonical_sqlite_runtime_contract_uses_wal_and_busy_timeout(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'wal_contract.db'}")
    with engine.connect() as conn:
        journal_mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        busy_timeout = int(conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one())
    assert str(journal_mode).lower() == "wal"
    assert busy_timeout >= 30_000


def test_runtime_heartbeat_recovers_from_real_sqlite_writer_contention(tmp_path) -> None:
    db = tmp_path / "heartbeat_contention.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{db}",
        future=True,
        connect_args={"timeout": 0.01},
    )
    ensure_runtime_heartbeat_schema(engine)
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")

    lock_acquired = threading.Event()

    def hold_writer_lock() -> None:
        raw = sqlite3.connect(db, timeout=0.01)
        try:
            raw.execute("PRAGMA journal_mode=WAL")
            raw.execute("BEGIN IMMEDIATE")
            lock_acquired.set()
            time.sleep(0.12)
            raw.commit()
        finally:
            raw.close()

    holder = threading.Thread(target=hold_writer_lock)
    holder.start()
    assert lock_acquired.wait(timeout=1.0)

    save_runtime_heartbeat(
        engine,
        runtime_instance_id="runtime:contention",
        execution_mode="PAPER",
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
        retry_attempts=6,
    )
    holder.join(timeout=1.0)
    assert not holder.is_alive()

    with engine.connect() as conn:
        rows = int(conn.execute(text(
            "SELECT COUNT(*) FROM runtime_heartbeats "
            "WHERE runtime_instance_id='runtime:contention'"
        )).scalar_one())
    engine.dispose()
    assert rows == 1


def test_runtime_heartbeat_persistent_sqlite_lock_exhausts_bounded_retry(tmp_path) -> None:
    db = tmp_path / "heartbeat_persistent_lock.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{db}",
        future=True,
        connect_args={"timeout": 0.01},
    )
    ensure_runtime_heartbeat_schema(engine)
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")

    lock_acquired = threading.Event()

    def hold_writer_lock() -> None:
        raw = sqlite3.connect(db, timeout=0.01)
        try:
            raw.execute("PRAGMA journal_mode=WAL")
            raw.execute("BEGIN IMMEDIATE")
            lock_acquired.set()
            time.sleep(0.40)
            raw.commit()
        finally:
            raw.close()

    holder = threading.Thread(target=hold_writer_lock)
    holder.start()
    assert lock_acquired.wait(timeout=1.0)

    with pytest.raises(OperationalError, match="database is locked"):
        save_runtime_heartbeat(
            engine,
            runtime_instance_id="runtime:persistent-lock",
            execution_mode="PAPER",
            scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
            retry_attempts=2,
        )

    holder.join(timeout=1.0)
    engine.dispose()
    assert not holder.is_alive()


def test_heartbeat_loop_transient_lock_recovers_without_fatal_task(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'heartbeat_loop_recovery.db'}")
    runtime = _runtime(engine, ExecutionMode.PAPER)
    runtime.config.heartbeat_interval_sec = 0.0
    runtime._persist_runtime_state_snapshot = lambda *_args, **_kwargs: None
    calls = {"count": 0}

    def persist(*_args, **_kwargs) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise _locked_error()
        if calls["count"] == 3:
            runtime.shutdown()

    runtime._persist_runtime_heartbeat = persist

    asyncio.run(runtime._heartbeat_loop())

    assert calls["count"] == 3
    assert runtime.metrics.heartbeat_persistence_failures == 1
    assert runtime.metrics.heartbeat_persistence_recoveries == 1
    assert runtime.metrics.heartbeat_persistence_degraded is False
    assert runtime._heartbeat_persistence_failure_streak == 0
    assert runtime._fatal_task_exception is None
    assert runtime._recovery_required is False


def test_heartbeat_loop_sustained_lock_enters_controlled_recovery_required(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'heartbeat_loop_sustained.db'}")
    runtime = _runtime(engine, ExecutionMode.PAPER)
    runtime.config.heartbeat_interval_sec = 0.0
    runtime._heartbeat_persistence_failure_threshold = 2
    runtime._persist_runtime_state_snapshot = lambda *_args, **_kwargs: None
    runtime._persist_runtime_heartbeat = lambda *_args, **_kwargs: (_ for _ in ()).throw(_locked_error())

    asyncio.run(runtime._heartbeat_loop())

    assert runtime.metrics.heartbeat_persistence_failures == 2
    assert runtime.metrics.heartbeat_persistence_degraded is True
    assert runtime._heartbeat_persistence_failure_streak == 2
    assert runtime._recovery_required is True
    assert runtime._fail_closed_reason == "SUSTAINED_HEARTBEAT_PERSISTENCE_FAILURE"
    assert runtime._fatal_task_exception is None
    assert runtime._stop_event.is_set()


def test_heartbeat_loop_non_lock_database_failure_remains_fatal(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'heartbeat_loop_schema_error.db'}")
    runtime = _runtime(engine, ExecutionMode.PAPER)
    runtime.config.heartbeat_interval_sec = 0.0
    runtime._persist_runtime_state_snapshot = lambda *_args, **_kwargs: None
    schema_error = OperationalError(
        "INSERT",
        {},
        sqlite3.OperationalError("no such table: required_runtime_evidence"),
    )
    runtime._persist_runtime_heartbeat = lambda *_args, **_kwargs: (_ for _ in ()).throw(schema_error)

    with pytest.raises(OperationalError, match="no such table"):
        asyncio.run(runtime._heartbeat_loop())

    assert runtime.metrics.heartbeat_persistence_failures == 0
    assert runtime._recovery_required is False


def test_degraded_heartbeat_persistence_blocks_new_execution(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'heartbeat_fail_closed.db'}")
    runtime = _runtime(engine, ExecutionMode.PAPER)
    runtime.metrics.heartbeat_persistence_degraded = True

    assert runtime._evaluate_runtime_risk("BTCUSDT", {}) == "RUNTIME_DB_UNAVAILABLE"
