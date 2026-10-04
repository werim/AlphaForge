from __future__ import annotations

import sqlite3
import threading
import time

import pytest
from sqlalchemy import text

from alphaforge.burnin_campaign import _with_fresh_lock_retry
from alphaforge.persistence import init_db
from alphaforge.sqlite_safety import SQLiteBusyExhausted, run_sqlite_write_with_retry
from alphaforge.runtime_heartbeat import save_runtime_heartbeat
from alphaforge.runtime_state import RuntimeStateSnapshot, persist_reconciliation_cycle


def test_in_process_campaign_writer_does_not_exhaust_allocation_retry_budget(tmp_path) -> None:
    path = tmp_path / "issue603-shared-writer.db"
    campaign_engine = init_db(f"sqlite+pysqlite:///{path}")
    runtime_engine = init_db(f"sqlite+pysqlite:///{path}")
    with campaign_engine.begin() as conn:
        conn.execute(text("CREATE TABLE issue603_probe (id INTEGER PRIMARY KEY, source TEXT NOT NULL)"))

    writer_started = threading.Event()
    release_writer = threading.Event()
    errors: list[BaseException] = []

    def long_campaign_writer() -> None:
        try:
            def operation(conn):
                conn.execute(text("INSERT INTO issue603_probe(id,source) VALUES (1,'campaign')"))
                writer_started.set()
                assert release_writer.wait(timeout=3.0)
            _with_fresh_lock_retry(campaign_engine, operation)
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    def allocation_writer() -> None:
        try:
            run_sqlite_write_with_retry(
                runtime_engine,
                lambda conn: conn.execute(
                    text("INSERT INTO issue603_probe(id,source) VALUES (2,'allocation')")
                ),
                operation_name="portfolio_allocation_cycle",
            )
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    first = threading.Thread(target=long_campaign_writer, name="issue603-campaign-writer")
    second = threading.Thread(target=allocation_writer, name="issue603-allocation-writer")
    first.start()
    assert writer_started.wait(timeout=2.0)
    second.start()

    # Longer than the legacy short-probe retry sleeps. The second in-process
    # writer must wait on arbitration instead of spending its SQLITE_BUSY budget.
    time.sleep(0.8)
    release_writer.set()
    first.join(timeout=3.0)
    second.join(timeout=3.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    with runtime_engine.connect() as conn:
        rows = conn.execute(text("SELECT id,source FROM issue603_probe ORDER BY id")).all()
        assert rows == [(1, "campaign"), (2, "allocation")]
        assert str(conn.exec_driver_sql("PRAGMA journal_mode").scalar_one()).lower() == "wal"
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 30000

    campaign_engine.dispose()
    runtime_engine.dispose()


def test_heartbeat_and_reconciliation_wait_for_same_in_process_writer(tmp_path) -> None:
    path = tmp_path / "issue603-runtime-writers.db"
    owner_engine = init_db(f"sqlite+pysqlite:///{path}")
    heartbeat_engine = init_db(f"sqlite+pysqlite:///{path}")
    reconciliation_engine = init_db(f"sqlite+pysqlite:///{path}")

    writer_started = threading.Event()
    release_writer = threading.Event()
    errors: list[BaseException] = []

    def long_writer() -> None:
        try:
            def operation(conn):
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS issue603_runtime_probe "
                    "(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
                ))
                conn.execute(text(
                    "INSERT INTO issue603_runtime_probe(id,value) VALUES (1,'owner')"
                ))
                writer_started.set()
                assert release_writer.wait(timeout=3.0)
            _with_fresh_lock_retry(owner_engine, operation)
        except BaseException as exc:
            errors.append(exc)

    def heartbeat_writer() -> None:
        try:
            save_runtime_heartbeat(
                heartbeat_engine,
                runtime_instance_id="runtime:issue603",
                execution_mode="PAPER",
                scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
            )
        except BaseException as exc:
            errors.append(exc)

    def reconciliation_writer() -> None:
        try:
            snapshot = RuntimeStateSnapshot(
                mode="PAPER",
                requested_mode="PAPER",
                actual_mode="PAPER",
                runtime_status="OPERATING",
                instance_id="runtime:issue603",
                startup_id="startup:issue603",
                unknown_exchange_state=False,
                exchange_read_only_status="AVAILABLE",
                reconciliation_status="CLEAN",
            )
            persist_reconciliation_cycle(
                reconciliation_engine,
                cycle_id="recon:issue603",
                findings=[],
                snapshot=snapshot,
                diagnostics={"evidence_status": "COMPLETE"},
            )
        except BaseException as exc:
            errors.append(exc)

    owner = threading.Thread(target=long_writer)
    heartbeat = threading.Thread(target=heartbeat_writer)
    reconciliation = threading.Thread(target=reconciliation_writer)
    owner.start()
    assert writer_started.wait(timeout=2.0)
    heartbeat.start()
    reconciliation.start()

    time.sleep(0.8)
    release_writer.set()
    for thread in (owner, heartbeat, reconciliation):
        thread.join(timeout=3.0)

    assert errors == []
    assert all(not thread.is_alive() for thread in (owner, heartbeat, reconciliation))
    with owner_engine.connect() as conn:
        assert conn.execute(text(
            "SELECT COUNT(*) FROM runtime_heartbeats WHERE runtime_instance_id='runtime:issue603'"
        )).scalar_one() == 1
        assert conn.execute(text(
            "SELECT COUNT(*) FROM exchange_reconciliation_events WHERE cycle_id='recon:issue603'"
        )).scalar_one() == 1

    owner_engine.dispose()
    heartbeat_engine.dispose()
    reconciliation_engine.dispose()


def test_external_sqlite_writer_still_exhausts_bounded_retry(tmp_path) -> None:
    path = tmp_path / "issue603-external-writer.db"
    engine = init_db(f"sqlite+pysqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE issue603_external (id INTEGER PRIMARY KEY)"))

    external = sqlite3.connect(path, timeout=0.01)
    external.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(SQLiteBusyExhausted, match="portfolio_allocation_cycle"):
            run_sqlite_write_with_retry(
                engine,
                lambda conn: conn.execute(
                    text("INSERT INTO issue603_external(id) VALUES (1)")
                ),
                operation_name="portfolio_allocation_cycle",
                attempts=2,
                base_seconds=0.01,
                probe_timeout_ms=10,
            )
    finally:
        external.rollback()
        external.close()
        engine.dispose()
