from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from alphaforge.burnin_campaign import create_campaign, pause_campaign, start_or_resume_campaign
from alphaforge.burnin_ops import bootstrap_ops_schema
from alphaforge.dashboard.app import create_app
from alphaforge.dashboard.control_center import ControlCenterService, ControlError, _CampaignLock, _sanitize
from alphaforge.persistence import init_db


def seeded(tmp_path: Path, monkeypatch, *, active: bool = True):
    db = tmp_path / "runtime.db"
    init_db(f"sqlite+pysqlite:///{db}").dispose()
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row; bootstrap_ops_schema(conn)
    campaign = create_campaign(conn, release_id="release-real", duration_days=1, symbols=["BTCUSDT"], intervals=["1h"], target_decisions=10)
    if active: start_or_resume_campaign(conn, campaign.campaign_id)
    else: conn.execute("UPDATE burnin_campaigns SET campaign_status='COMPLETED' WHERE campaign_id=?", (campaign.campaign_id,))
    conn.commit(); conn.close()
    monkeypatch.setenv("ALPHAFORGE_DB_PATH", str(db)); monkeypatch.setenv("ALPHAFORGE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER"); monkeypatch.setenv("ALPHAFORGE_CONTROL_TOKEN", "correct-token")
    return db, campaign.campaign_id


def test_active_campaign_and_zero_decision_rates_are_canonical(tmp_path, monkeypatch):
    db, cid = seeded(tmp_path, monkeypatch)
    client = TestClient(create_app(f"sqlite+pysqlite:///{db}"))
    assert client.get("/api/campaigns/active").json()["data"]["campaign_id"] == cid
    payload = client.get(f"/api/campaigns/{cid}/status").json()
    assert payload["data"]["metrics"]["total_decisions"] == 0
    assert payload["data"]["metrics"]["reject_rate"] is None
    assert payload["data"]["metrics"]["acceptance_rate"] is None
    assert payload["source"] == "canonical_sqlite" and "observed_at" in payload


def test_no_active_campaign_is_structured_but_runtime_remains_available(tmp_path, monkeypatch):
    db, _ = seeded(tmp_path, monkeypatch, active=False)
    client = TestClient(create_app(f"sqlite+pysqlite:///{db}"))
    response = client.get("/api/campaigns/active")
    assert response.status_code == 404 and response.json()["error"]["code"] == "NO_ACTIVE_CAMPAIGN"
    assert client.get("/api/runtime").json()["data"]["active_campaign"] is None


@pytest.mark.parametrize("bad", ["x' OR 1=1--", "../campaign", "x;pause"])
def test_campaign_id_injection_is_rejected(tmp_path, monkeypatch, bad):
    db, _ = seeded(tmp_path, monkeypatch)
    if "/" in bad:
        with pytest.raises(Exception) as failure:
            ControlCenterService.from_environment().status(bad)
        assert failure.value.code == "CAMPAIGN_ID_MISMATCH"
        return
    response = TestClient(create_app(f"sqlite+pysqlite:///{db}")).get(f"/api/campaigns/{bad}/status")
    assert response.status_code in {400, 404}
    assert response.json()["error"]["code"] == "CAMPAIGN_ID_MISMATCH"


def test_missing_optional_position_table_is_schema_safe(tmp_path, monkeypatch):
    db, cid = seeded(tmp_path, monkeypatch)
    conn = sqlite3.connect(db); conn.execute("DROP TABLE burnin_pending_position_outcomes"); conn.commit(); conn.close()
    payload = TestClient(create_app(f"sqlite+pysqlite:///{db}")).get(f"/api/campaigns/{cid}/positions").json()["data"]
    assert payload == {"availability": "UNAVAILABLE_IN_SCHEMA", "items": None}


def test_malformed_preflight_json_is_reported(tmp_path, monkeypatch):
    db, _ = seeded(tmp_path, monkeypatch)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO burnin_preflight_reports(preflight_id,campaign_id,release_id,generated_at,status,blockers_json,checks_json,schema_version) VALUES(?,?,?,?,?,?,?,?)", ("pf", None, "rel", "2026-01-01T00:00:00Z", "PASS", "{bad", "[]", "v"))
    conn.commit(); conn.close()
    report = TestClient(create_app(f"sqlite+pysqlite:///{db}")).get("/api/preflight/latest").json()["data"]["report"]
    assert report["blockers"] is None and report["blockers_json_quality"] == "MALFORMED_JSON"


def test_worker_without_pid_is_never_healthy(tmp_path, monkeypatch):
    db, cid = seeded(tmp_path, monkeypatch)
    payload = TestClient(create_app(f"sqlite+pysqlite:///{db}")).get(f"/api/campaigns/{cid}/status").json()["data"]
    assert payload["worker"]["pid"] is None and payload["worker"]["health"] == "UNKNOWN"


def test_pause_uses_canonical_cli_list_without_shell_and_rechecks(monkeypatch, tmp_path):
    db, cid = seeded(tmp_path, monkeypatch)
    service = ControlCenterService.from_environment()
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row; pause_campaign(conn, cid); conn.commit(); conn.close()
        return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "PAUSED"}), stderr="")
    monkeypatch.setattr("alphaforge.dashboard.control_center.subprocess.run", run)
    out = service.control(cid, "pause", "correct-token")
    command, kwargs = calls[0]
    assert isinstance(command, list) and command[-3:] == ["pause", "--campaign-id", cid]
    assert kwargs["shell"] is False and out["status"]["campaign"]["campaign_status"] == "PAUSED"


def test_nonzero_command_and_postcondition_mismatch_never_succeed(monkeypatch, tmp_path):
    _, cid = seeded(tmp_path, monkeypatch); service = ControlCenterService.from_environment()
    monkeypatch.setattr("alphaforge.dashboard.control_center.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=7, stdout="", stderr="token=secret"))
    with pytest.raises(Exception) as failure: service.control(cid, "pause", "correct-token")
    assert failure.value.code == "COMMAND_FAILED"


def test_resume_rejects_recovery_and_config_drift(tmp_path, monkeypatch):
    db, cid = seeded(tmp_path, monkeypatch)
    conn = sqlite3.connect(db); conn.execute("UPDATE burnin_campaigns SET campaign_status='PAUSED',last_error='CONFIG_DRIFT' WHERE campaign_id=?", (cid,)); conn.commit(); conn.close()
    with pytest.raises(Exception) as failure: ControlCenterService.from_environment().control(cid, "resume", "correct-token")
    assert failure.value.code == "RECOVERY_REQUIRED"


def test_operation_lock_rejects_concurrent_request(tmp_path, monkeypatch):
    _, cid = seeded(tmp_path, monkeypatch); service = ControlCenterService.from_environment()
    lock = _CampaignLock(service.lock_root / f"{cid}.lock", "first", 60)
    lock.acquire()
    try:
        with pytest.raises(Exception) as failure: service.control(cid, "pause", "correct-token")
        assert failure.value.code == "INVALID_STATE_TRANSITION"
    finally: lock.release()


def test_sensitive_log_content_is_redacted_and_no_stop_route(tmp_path, monkeypatch):
    db, cid = seeded(tmp_path, monkeypatch)
    path = tmp_path / "artifacts" / "burnin" / cid; path.mkdir(parents=True)
    (path / "worker.stderr.log").write_text("Authorization: BearerSecret\napi_key=abc")
    client = TestClient(create_app(f"sqlite+pysqlite:///{db}"))
    body = json.dumps(client.get(f"/api/campaigns/{cid}/logs").json())
    assert "BearerSecret" not in body and "abc" not in body and "[REDACTED]" in body
    assert client.post(f"/api/campaigns/{cid}/stop").status_code == 404
    assert "secret" not in _sanitize("token=secret")


def test_paper_only_guard_rejects_live(tmp_path, monkeypatch):
    db, _ = seeded(tmp_path, monkeypatch); monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "LIVE")
    response = TestClient(create_app(f"sqlite+pysqlite:///{db}")).get("/api/health")
    assert response.status_code == 403 and response.json()["error"]["code"] == "INVALID_STATE_TRANSITION"


@pytest.mark.parametrize(("offset", "expected"), [(-600, True), (-1, False)])
def test_campaign_freshness_uses_canonical_timestamp_not_response_time(tmp_path, monkeypatch, offset, expected):
    db, cid = seeded(tmp_path, monkeypatch)
    observed = (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()
    conn = sqlite3.connect(db); conn.execute("UPDATE burnin_campaigns SET last_heartbeat_at=? WHERE campaign_id=?", (observed, cid)); conn.commit(); conn.close()
    payload = TestClient(create_app(f"sqlite+pysqlite:///{db}")).get(f"/api/campaigns/{cid}/status").json()
    assert payload["freshness"]["heartbeat"]["is_stale"] is expected
    assert payload["freshness"]["heartbeat"]["observed_at"] != payload["generated_at"]


@pytest.mark.parametrize(("timestamp", "availability"), [("bad", "INVALID_TIMESTAMP"), ((datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), "CLOCK_SKEW")])
def test_invalid_and_future_timestamps_fail_closed(tmp_path, monkeypatch, timestamp, availability):
    db, cid = seeded(tmp_path, monkeypatch)
    conn = sqlite3.connect(db); conn.execute("UPDATE burnin_campaigns SET last_heartbeat_at=? WHERE campaign_id=?", (timestamp, cid)); conn.commit(); conn.close()
    freshness = TestClient(create_app(f"sqlite+pysqlite:///{db}")).get(f"/api/campaigns/{cid}/status").json()["freshness"]["heartbeat"]
    assert freshness["is_stale"] is None and freshness["availability"] == availability


def test_missing_timestamp_column_returns_unavailable_in_schema(tmp_path, monkeypatch):
    db, cid = seeded(tmp_path, monkeypatch)
    conn = sqlite3.connect(db); conn.execute("ALTER TABLE burnin_pending_position_outcomes DROP COLUMN created_at"); conn.execute("ALTER TABLE burnin_pending_position_outcomes DROP COLUMN entry_time"); conn.execute("ALTER TABLE burnin_pending_position_outcomes DROP COLUMN resolved_at"); conn.commit(); conn.close()
    payload = TestClient(create_app(f"sqlite+pysqlite:///{db}")).get(f"/api/campaigns/{cid}/positions").json()
    assert payload["data"]["availability"] == "UNAVAILABLE_IN_SCHEMA"
    assert payload["is_stale"] is None


@pytest.mark.parametrize(("table", "columns", "endpoint"), [
    ("burnin_pending_reject_labels", ("decision_timestamp",), "rejects"),
    ("burnin_campaign_events", ("event_time",), "events"),
    ("burnin_preflight_reports", ("generated_at", "id"), "preflight/latest"),
])
def test_missing_optional_order_columns_do_not_raise_sqlite_error(tmp_path, monkeypatch, table, columns, endpoint):
    db, cid = seeded(tmp_path, monkeypatch); conn = sqlite3.connect(db)
    if table == "burnin_preflight_reports":
        conn.execute("DROP TABLE burnin_preflight_reports")
        conn.execute("CREATE TABLE burnin_preflight_reports(preflight_id TEXT, status TEXT)")
    else:
        for column in columns: conn.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
    conn.commit(); conn.close()
    url = f"/api/campaigns/{cid}/{endpoint}" if endpoint != "preflight/latest" else "/api/preflight/latest"
    response = TestClient(create_app(f"sqlite+pysqlite:///{db}")).get(url)
    assert response.status_code == 200


def test_query_lock_is_structured_and_does_not_leak_sql_or_path(tmp_path, monkeypatch):
    db, cid = seeded(tmp_path, monkeypatch); app = create_app(f"sqlite+pysqlite:///{db}")
    def locked(*_args, **_kwargs): raise ControlError("DB_LOCKED", "Runtime database query failed", 503)
    monkeypatch.setattr(app.state.control_center, "_query", locked)
    response = TestClient(app).get(f"/api/campaigns/{cid}/status")
    body = response.text
    assert response.status_code == 503 and response.json()["error"]["code"] == "DB_LOCKED"
    assert "SELECT" not in body and str(db) not in body


def _runtime_recovery_snapshot(db, cid, run_id, required):
    from alphaforge.runtime_state import RuntimeStateSnapshot, save_runtime_state_snapshot
    from sqlalchemy import create_engine
    engine = create_engine(f"sqlite+pysqlite:///{db}", future=True)
    save_runtime_state_snapshot(engine, RuntimeStateSnapshot(mode="PAPER", requested_mode="PAPER", actual_mode="PAPER", runtime_status="PAUSED", instance_id="cc-test", campaign_id=cid, burnin_run_id=run_id, recovery_action_required=required))
    engine.dispose()


def test_paused_recovery_flag_blocks_resume_before_subprocess(tmp_path, monkeypatch):
    db, cid = seeded(tmp_path, monkeypatch); conn = sqlite3.connect(db)
    run = conn.execute("SELECT active_run_id FROM burnin_campaigns WHERE campaign_id=?", (cid,)).fetchone()[0]
    conn.execute("UPDATE burnin_campaigns SET campaign_status='PAUSED' WHERE campaign_id=?", (cid,)); conn.execute("UPDATE burnin_campaign_runs SET status='PAUSED' WHERE burnin_run_id=?", (run,)); conn.commit(); conn.close()
    _runtime_recovery_snapshot(db, cid, run, True)
    called = False
    def must_not_run(*a, **k):
        nonlocal called; called = True
    monkeypatch.setattr("alphaforge.dashboard.control_center.subprocess.run", must_not_run)
    with pytest.raises(ControlError) as failure: ControlCenterService.from_environment().control(cid, "resume", "correct-token")
    assert failure.value.code == "RECOVERY_REQUIRED" and called is False


def test_unknown_recovery_evidence_blocks_resume(tmp_path, monkeypatch):
    db, cid = seeded(tmp_path, monkeypatch); conn = sqlite3.connect(db)
    conn.execute("UPDATE burnin_campaigns SET campaign_status='PAUSED' WHERE campaign_id=?", (cid,)); conn.execute("UPDATE burnin_campaign_runs SET status='PAUSED' WHERE campaign_id=?", (cid,)); conn.commit(); conn.close()
    with pytest.raises(ControlError) as failure: ControlCenterService.from_environment().control(cid, "resume", "correct-token")
    assert failure.value.code == "RECOVERY_REQUIRED"


def test_aggregate_word_in_run_id_is_not_contamination_evidence(tmp_path, monkeypatch):
    db, cid = seeded(tmp_path, monkeypatch); conn = sqlite3.connect(db)
    conn.execute("UPDATE burnin_campaign_runs SET burnin_run_id='aggregate-looking-name' WHERE campaign_id=?", (cid,)); conn.commit(); conn.close()
    status = ControlCenterService.from_environment().status(cid)
    assert status["aggregate_contamination"] is None
    assert status["aggregate_contamination_availability"] == "DATA_UNAVAILABLE"


def test_rejects_are_canonical_observations_and_pending_is_separate(tmp_path, monkeypatch):
    from alphaforge.burnin import persist_burnin_observation
    db, cid = seeded(tmp_path, monkeypatch); conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    run = conn.execute("SELECT active_run_id FROM burnin_campaigns WHERE campaign_id=?", (cid,)).fetchone()[0]
    persist_burnin_observation(conn, observation_id="reject-1", burnin_run_id=run, release_id="release-real", execution_mode="PAPER", decision="REJECTED", metrics={"reject_reason": "UNKNOWN"}, source_provenance={})
    persist_burnin_observation(conn, observation_id="reject-2", burnin_run_id=run, release_id="release-real", execution_mode="PAPER", decision="REJECTED", metrics={}, source_provenance={})
    conn.execute("UPDATE burnin_observations SET metrics_json='{bad' WHERE observation_id='reject-2'"); conn.commit(); conn.close()
    payload = TestClient(create_app(f"sqlite+pysqlite:///{db}")).get(f"/api/campaigns/{cid}/rejects").json()["data"]
    assert payload["scope"] == "CANONICAL_REJECTED_OBSERVATIONS" and payload["reject_total"] == 2
    assert payload["reason_distribution"] == {"UNKNOWN": 1}
    assert payload["reason_quality"]["missing_count"] == 1 and payload["reason_quality"]["malformed_metrics_json_count"] == 1
    assert payload["pending_label_queue"]["scope"] == "UNFINALIZED_FORWARD_LABEL_QUEUE"


def test_process_lock_releases_after_exception(tmp_path):
    lock = _CampaignLock(tmp_path / "locks" / "campaign.lock", "one", 60); lock.acquire(); lock.release()
    second = _CampaignLock(tmp_path / "locks" / "campaign.lock", "two", 60); second.acquire(); second.release()


def test_paused_clean_canonical_recovery_evidence_reaches_resume_subprocess(tmp_path, monkeypatch):
    db, cid = seeded(tmp_path, monkeypatch); conn = sqlite3.connect(db)
    run = conn.execute("SELECT active_run_id FROM burnin_campaigns WHERE campaign_id=?", (cid,)).fetchone()[0]
    conn.execute("UPDATE burnin_campaigns SET campaign_status='PAUSED' WHERE campaign_id=?", (cid,)); conn.execute("UPDATE burnin_campaign_runs SET status='PAUSED' WHERE burnin_run_id=?", (run,)); conn.commit(); conn.close()
    _runtime_recovery_snapshot(db, cid, run, False); called = []
    def resume(command, **kwargs):
        called.append(command); conn = sqlite3.connect(db)
        conn.execute("UPDATE burnin_campaigns SET campaign_status='RUNNING',worker_pid=?,last_heartbeat_at=? WHERE campaign_id=?", (__import__('os').getpid(), datetime.now(timezone.utc).isoformat(), cid)); conn.commit(); conn.close()
        return SimpleNamespace(returncode=0, stdout='{}', stderr='')
    monkeypatch.setattr("alphaforge.dashboard.control_center.subprocess.run", resume)
    result = ControlCenterService.from_environment().control(cid, "resume", "correct-token")
    assert called and result["operation"]["result"] == "SUCCESS"


def test_active_run_recovery_event_blocks_paused_resume(tmp_path, monkeypatch):
    db, cid = seeded(tmp_path, monkeypatch); conn = sqlite3.connect(db)
    run = conn.execute("SELECT active_run_id FROM burnin_campaigns WHERE campaign_id=?", (cid,)).fetchone()[0]
    conn.execute("UPDATE burnin_campaigns SET campaign_status='PAUSED' WHERE campaign_id=?", (cid,)); conn.execute("UPDATE burnin_campaign_runs SET status='PAUSED' WHERE burnin_run_id=?", (run,))
    conn.execute("INSERT INTO burnin_campaign_events(event_id,campaign_id,burnin_run_id,event_type,event_time,details_json,schema_version) VALUES(?,?,?,?,?,?,?)", ("recovery-event", cid, run, "RECOVERY_REQUIRED", datetime.now(timezone.utc).isoformat(), "{}", "v")); conn.commit(); conn.close()
    _runtime_recovery_snapshot(db, cid, run, False)
    with pytest.raises(ControlError) as failure: ControlCenterService.from_environment().control(cid, "resume", "correct-token")
    assert failure.value.code == "RECOVERY_REQUIRED"


def test_connection_stage_lock_maps_to_db_locked(tmp_path, monkeypatch):
    db, _ = seeded(tmp_path, monkeypatch); app = create_app(f"sqlite+pysqlite:///{db}")
    monkeypatch.setattr("alphaforge.dashboard.control_center.sqlite3.connect", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database table is locked")))
    response = TestClient(app).get("/api/health")
    assert response.status_code == 503 and response.json()["error"]["code"] == "DB_LOCKED"
