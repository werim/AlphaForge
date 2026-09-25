from __future__ import annotations

import sqlite3
import threading
import time

from sqlalchemy import text

from alphaforge.burnin_campaign import (
    BurnInCampaignRunner,
    create_campaign,
    get_campaign,
    qualify_campaign,
    start_or_resume_campaign,
)
from alphaforge.persistence import init_db
from alphaforge.runtime_heartbeat import save_runtime_heartbeat
from alphaforge.runtime_state import (
    RuntimeStateSnapshot,
    ensure_runtime_state_schema,
    persist_reconciliation_cycle,
)


def test_issue450_concurrent_campaign_writers_remain_consistent(tmp_path) -> None:
    db = tmp_path / "issue450_writer_stress.db"
    engine = init_db(f"sqlite+pysqlite:///{db}")
    with engine.begin() as conn:
        campaign = create_campaign(
            conn,
            release_id="issue450-writer-stress",
            duration_days=1,
            symbols=["BTCUSDT"],
            intervals=["1h"],
        )
        run = start_or_resume_campaign(conn, campaign.campaign_id)

    ensure_runtime_state_schema(engine)
    runner = BurnInCampaignRunner(
        engine,
        campaign.campaign_id,
        lambda *_args, **_kwargs: [],
    )
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []
    resolver_successes = {"count": 0}

    def guarded(operation):
        try:
            barrier.wait(timeout=2.0)
            operation()
        except BaseException as exc:  # surfaced after all writers join
            errors.append(exc)

    def heartbeat_writer() -> None:
        for index in range(12):
            save_runtime_heartbeat(
                engine,
                runtime_instance_id="runtime:issue450-stress",
                execution_mode="PAPER",
                scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
                heartbeat_ts=f"2026-09-25T22:30:{index:02d}Z",
                payload={"scans": index},
            )

    def reconciliation_writer() -> None:
        for index in range(8):
            snapshot = RuntimeStateSnapshot(
                mode="PAPER",
                requested_mode="PAPER",
                actual_mode="PAPER",
                runtime_status="OPERATING",
                instance_id="runtime:issue450-stress",
                startup_id="startup:issue450-stress",
                campaign_id=campaign.campaign_id,
                burnin_run_id=run["burnin_run_id"],
                release_id="issue450-writer-stress",
                unknown_exchange_state=False,
                exchange_read_only_status="AVAILABLE",
                reconciliation_status="CLEAN",
            )
            persist_reconciliation_cycle(
                engine,
                cycle_id=f"recon:issue450:stress:{index}",
                findings=[],
                snapshot=snapshot,
                diagnostics={"evidence_status": "COMPLETE", "stress_index": index},
            )

    def resolver_writer() -> None:
        for _ in range(8):
            result = runner.resolver_tick()
            if result.get("status") != "OK":
                raise AssertionError(f"unexpected resolver status: {result}")
            resolver_successes["count"] += 1

    def qualification_writer() -> None:
        for _ in range(3):
            qualify_campaign(engine, campaign.campaign_id)

    started = time.monotonic()
    threads = [
        threading.Thread(target=guarded, args=(heartbeat_writer,)),
        threading.Thread(target=guarded, args=(reconciliation_writer,)),
        threading.Thread(target=guarded, args=(resolver_writer,)),
        threading.Thread(target=guarded, args=(qualification_writer,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15.0)

    assert all(not thread.is_alive() for thread in threads)
    assert time.monotonic() - started < 15.0
    assert errors == []

    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA quick_check").scalar_one() == "ok"
        assert conn.execute(text(
            "SELECT COUNT(*) FROM runtime_heartbeats "
            "WHERE runtime_instance_id='runtime:issue450-stress'"
        )).scalar_one() == 12
        reconciliation = conn.execute(text(
            "SELECT COUNT(*), COUNT(DISTINCT cycle_id) "
            "FROM exchange_reconciliation_events "
            "WHERE cycle_id LIKE 'recon:issue450:stress:%'"
        )).one()
        assert tuple(reconciliation) == (8, 8)
        resolver_events = conn.execute(text(
            "SELECT COUNT(*), COUNT(DISTINCT event_id) FROM burnin_campaign_events "
            "WHERE campaign_id=:cid AND event_type='RESOLVER_BATCH'"
        ), {"cid": campaign.campaign_id}).one()
        # resolver_tick() intentionally deduplicates identical diagnostic events
        # that land in the same canonical second; all cycles must still complete.
        assert resolver_successes["count"] == 8
        assert 1 <= resolver_events[0] <= resolver_successes["count"]
        assert resolver_events[0] == resolver_events[1]
        assert conn.execute(text(
            "SELECT COUNT(*) FROM burnin_qualification_snapshots "
            "WHERE campaign_id=:cid"
        ), {"cid": campaign.campaign_id}).scalar_one() >= 1
        assert conn.execute(text(
            "SELECT COUNT(*) FROM burnin_campaign_events "
            "WHERE campaign_id=:cid AND event_type IN "
            "('RUNTIME_SUPERVISION_FAILED','WORKER_UNCAUGHT_EXCEPTION')"
        ), {"cid": campaign.campaign_id}).scalar_one() == 0

    check = sqlite3.connect(db)
    check.row_factory = sqlite3.Row
    try:
        assert get_campaign(check, campaign.campaign_id)["campaign_status"] == "RUNNING"
    finally:
        check.close()
        engine.dispose()
