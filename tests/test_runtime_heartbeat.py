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
    RuntimeHeartbeatPersistenceFailure,
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



def _sqlite_write_lock(path):
    blocker = sqlite3.connect(path, timeout=0.01, check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")
    return blocker


def test_runtime_heartbeat_retries_transient_sqlite_writer_lock(tmp_path) -> None:
    path = tmp_path / "heartbeat-lock.db"
    engine = init_db(f"sqlite+pysqlite:///{path}")
    ensure_runtime_heartbeat_schema(engine)
    blocker = _sqlite_write_lock(path)
    released = threading.Event()

    def release() -> None:
        blocker.rollback()
        blocker.close()
        released.set()

    timer = threading.Timer(0.12, release)
    timer.start()
    try:
        save_runtime_heartbeat(
            engine,
            runtime_instance_id="runtime:transient-lock",
            execution_mode="PAPER",
            scanner_source="TEST",
        )
    finally:
        timer.join(timeout=2)
        if not released.is_set():
            blocker.rollback()
            blocker.close()

    assert released.is_set()
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT runtime_instance_id FROM runtime_heartbeats "
            "WHERE runtime_instance_id='runtime:transient-lock'"
        )).scalars().all()
    assert rows == ["runtime:transient-lock"]


def test_runtime_heartbeat_persistent_lock_raises_typed_bounded_failure(tmp_path) -> None:
    path = tmp_path / "heartbeat-persistent-lock.db"
    engine = init_db(f"sqlite+pysqlite:///{path}")
    ensure_runtime_heartbeat_schema(engine)
    blocker = _sqlite_write_lock(path)
    started = time.monotonic()
    try:
        with pytest.raises(
            RuntimeHeartbeatPersistenceFailure,
            match="SQLITE_BUSY runtime heartbeat persistence",
        ):
            save_runtime_heartbeat(
                engine,
                runtime_instance_id="runtime:persistent-lock",
                execution_mode="PAPER",
                scanner_source="TEST",
            )
        assert time.monotonic() - started < 1.5
    finally:
        blocker.rollback()
        blocker.close()

    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT COUNT(*) FROM runtime_heartbeats "
            "WHERE runtime_instance_id='runtime:persistent-lock'"
        )).scalar_one() == 0


def test_heartbeat_loop_survives_transient_lock_and_stays_fail_closed_until_reconciliation(tmp_path) -> None:
    path = tmp_path / "heartbeat-loop-lock.db"
    engine = init_db(f"sqlite+pysqlite:///{path}")
    ensure_runtime_heartbeat_schema(engine)
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=ExecutionMode.PAPER,
            heartbeat_interval_sec=0.02,
        ),
        ai_brain=_NoopBrain(),
        market_scanner=lambda: None,
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
        persistence_engine=engine,
    )
    runtime.metrics.persistence_enabled = True
    blocker = _sqlite_write_lock(path)
    released = threading.Event()

    def release() -> None:
        blocker.rollback()
        blocker.close()
        released.set()

    timer = threading.Timer(0.55, release)
    timer.start()

    async def run() -> None:
        task = asyncio.create_task(runtime._heartbeat_loop(), name="metrics_heartbeat")
        await asyncio.sleep(0.95)
        assert not task.done()
        assert released.is_set()
        assert runtime._heartbeat_persistence_unhealthy is False
        assert runtime._heartbeat_persistence_failure_streak == 0
        assert runtime._fail_closed_reason == "RUNTIME_HEARTBEAT_PERSISTENCE_FAILED"
        assert runtime._runtime_status == "RECOVERY_REQUIRED"
        assert runtime._evaluate_runtime_risk(
            "BTCUSDT", {"market_ts": time.time()}
        ) == "RUNTIME_HEARTBEAT_PERSISTENCE_FAILED"
        runtime._stop_event.set()
        await task

    try:
        asyncio.run(run())
    finally:
        timer.join(timeout=2)
        if not released.is_set():
            blocker.rollback()
            blocker.close()

    with engine.connect() as conn:
        states = conn.execute(text(
            "SELECT runtime_state FROM runtime_heartbeats "
            "WHERE runtime_instance_id=:rid ORDER BY id"
        ), {"rid": runtime.runtime_instance_id}).scalars().all()
    assert "RECOVERY_REQUIRED" in states


def test_non_busy_heartbeat_database_error_is_not_reclassified_as_transient(tmp_path) -> None:
    path = tmp_path / "heartbeat-malformed-schema.db"
    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE runtime_heartbeats (id INTEGER PRIMARY KEY AUTOINCREMENT)"
        ))

    with pytest.raises(OperationalError, match="runtime_instance_id"):
        save_runtime_heartbeat(
            engine,
            runtime_instance_id="runtime:malformed",
            execution_mode="PAPER",
            scanner_source="TEST",
        )
