from __future__ import annotations

import json
import sqlite3

from alphaforge.burnin import utc_now
from alphaforge.burnin_campaign import (
    create_campaign,
    get_terminal_cause,
    start_or_resume_campaign,
    terminalize_active_campaign_run,
)
from alphaforge.burnin_ops import bootstrap_ops_schema, health_payload
from alphaforge.persistence import init_db


def _seed(tmp_path):
    db = tmp_path / "issue419.db"
    init_db(f"sqlite+pysqlite:///{db}").dispose()
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    bootstrap_ops_schema(conn)
    camp = create_campaign(
        conn,
        release_id="issue419",
        duration_days=1,
        symbols=["BTCUSDT"],
        intervals=["1h"],
    )
    run = start_or_resume_campaign(conn, camp.campaign_id)["burnin_run_id"]
    conn.commit()
    return conn, camp.campaign_id, run


def test_authoritative_terminal_cause_is_immutable_and_event_linked(tmp_path):
    conn, campaign_id, run_id = _seed(tmp_path)

    terminalize_active_campaign_run(
        conn,
        campaign_id,
        run_status="FAILED",
        campaign_status="FAILED",
        reason="WORKER_UNCAUGHT_EXCEPTION",
        event_type="WORKER_UNCAUGHT_EXCEPTION",
        details={"exception_type": "RuntimeError"},
    )
    conn.commit()

    first = get_terminal_cause(conn, campaign_id, run_id)
    assert first is not None
    assert first["terminal_cause"] == "WORKER_UNCAUGHT_EXCEPTION"
    assert first["terminal_cause_source"] == "WORKER_UNCAUGHT_EXCEPTION"
    event_row = conn.execute(
        "SELECT event_type,event_time,details_json FROM burnin_campaign_events WHERE event_id=?",
        (first["terminal_event_id"],),
    ).fetchone()
    assert event_row is not None
    assert event_row["event_type"] == "WORKER_UNCAUGHT_EXCEPTION"
    assert event_row["event_time"] == first["terminal_at"]
    details = json.loads(event_row["details_json"])
    assert details["terminal_cause"] == first["terminal_cause"]
    assert details["terminal_cause_source"] == first["terminal_cause_source"]
    assert conn.execute(
        "SELECT last_error FROM burnin_campaigns WHERE campaign_id=?",
        (campaign_id,),
    ).fetchone()[0] == first["terminal_cause"]

    # A later derived symptom must not overwrite the first terminal cause.
    terminalize_active_campaign_run(
        conn,
        campaign_id,
        run_status="RECOVERY_REQUIRED",
        campaign_status="RECOVERY_REQUIRED",
        reason="RESOLVER_BACKLOG_SUSTAINED_GROWTH",
        event_type="PHASE9_WATCHDOG_RECOVERY_REQUIRED",
    )
    conn.commit()

    second = get_terminal_cause(conn, campaign_id, run_id)
    assert second["terminal_id"] == first["terminal_id"]
    assert second["terminal_cause"] == "WORKER_UNCAUGHT_EXCEPTION"
    assert conn.execute(
        "SELECT COUNT(*) FROM burnin_terminal_causes WHERE campaign_id=? AND burnin_run_id=?",
        (campaign_id, run_id),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT campaign_status,last_error FROM burnin_campaigns WHERE campaign_id=?",
        (campaign_id,),
    ).fetchone()[:] == ("FAILED", "WORKER_UNCAUGHT_EXCEPTION")

    status = health_payload(conn, campaign_id, persist_history=False)
    assert status["terminal_cause"] == "WORKER_UNCAUGHT_EXCEPTION"
    assert status["terminal_cause_source"] == "WORKER_UNCAUGHT_EXCEPTION"
    assert status["terminal_event_id"] == first["terminal_event_id"]
    assert status["terminal_at"] == first["terminal_at"]
    conn.close()


def test_legacy_terminal_campaign_is_reported_unknown_without_backfill(tmp_path):
    conn, campaign_id, run_id = _seed(tmp_path)
    terminal_at = utc_now()
    conn.execute(
        "UPDATE burnin_runs SET status='FAILED',end_time=? WHERE burnin_run_id=?",
        (terminal_at, run_id),
    )
    conn.execute(
        "UPDATE burnin_campaign_runs SET status='FAILED',ended_at=? WHERE campaign_id=? AND burnin_run_id=?",
        (terminal_at, campaign_id, run_id),
    )
    conn.execute(
        "UPDATE burnin_campaigns SET campaign_status='FAILED',last_error='LEGACY_FAILURE' WHERE campaign_id=?",
        (campaign_id,),
    )
    conn.commit()

    assert conn.execute(
        "SELECT COUNT(*) FROM burnin_terminal_causes WHERE campaign_id=?",
        (campaign_id,),
    ).fetchone()[0] == 0

    status = health_payload(conn, campaign_id, persist_history=False)
    assert status["terminal_cause"] == "UNKNOWN_LEGACY_TERMINAL_CAUSE"
    assert status["terminal_cause_source"] == "LEGACY_NO_AUTHORITATIVE_RECORD"
    assert status["terminal_event_id"] is None
    assert status["terminal_at"] == terminal_at

    # Read-only status inspection must not backfill historical evidence.
    assert conn.execute(
        "SELECT COUNT(*) FROM burnin_terminal_causes WHERE campaign_id=?",
        (campaign_id,),
    ).fetchone()[0] == 0
    conn.close()
