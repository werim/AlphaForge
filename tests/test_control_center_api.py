from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from alphaforge.burnin_campaign import create_campaign, pause_campaign, start_or_resume_campaign
from alphaforge.burnin_ops import bootstrap_ops_schema
from alphaforge.dashboard.app import create_app
from alphaforge.dashboard.control_center import ControlCenterService, _sanitize
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


def add_rejects(db: Path, cid: str, count: int, *, duplicate_first: bool = False):
    conn = sqlite3.connect(db)
    run = conn.execute("SELECT active_run_id FROM burnin_campaigns WHERE campaign_id=?", (cid,)).fetchone()[0]
    for index in range(count):
        reason = None if index == 0 else ("UNKNOWN" if index == 1 else "LOW_CONFIDENCE")
        metrics = "{broken" if index == 2 else json.dumps({} if reason is None else {"reject_reason": reason})
        conn.execute("INSERT INTO burnin_observations(observation_id,burnin_run_id,release_id,observed_at,execution_mode,decision,metrics_json,missing_fields_json,source_provenance_json,schema_version,evidence_complete) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (f"obs-{index}", run, "release-real", f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}Z", "PAPER", "REJECTED", metrics, "[]", "{}", "v", 1))
    if duplicate_first:
        # Rebuild the canonical table shape without its current UNIQUE constraint
        # to represent an affected historical schema containing duplicate IDs.
        conn.execute("ALTER TABLE burnin_observations RENAME TO burnin_observations_current")
        conn.execute("CREATE TABLE burnin_observations AS SELECT * FROM burnin_observations_current")
        conn.execute("INSERT INTO burnin_observations SELECT * FROM burnin_observations_current WHERE observation_id='obs-0'")
        conn.execute("DROP TABLE burnin_observations_current")
    conn.commit(); conn.close()


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


def test_frontend_aliases_return_same_canonical_data(tmp_path, monkeypatch):
    monkeypatch.setattr("alphaforge.dashboard.control_center._age", lambda _value: 1.0)
    db, cid = seeded(tmp_path, monkeypatch); client = TestClient(create_app(f"sqlite+pysqlite:///{db}"))
    assert client.get("/api/runtime/status").json()["data"] == client.get("/api/runtime").json()["data"]
    assert client.get("/api/campaigns/current").json()["data"] == client.get("/api/campaigns/active").json()["data"]
    assert client.get(f"/api/campaigns/{cid}").json()["data"] == client.get(f"/api/campaigns/{cid}/status").json()["data"]


def test_configured_cors_origin_is_allowed_and_unknown_preflight_is_rejected(tmp_path, monkeypatch):
    db, _ = seeded(tmp_path, monkeypatch); monkeypatch.setenv("ALPHAFORGE_CONTROL_CORS_ORIGINS", "https://control.example")
    client = TestClient(create_app(f"sqlite+pysqlite:///{db}"))
    allowed = client.options("/api/health", headers={"Origin": "https://control.example", "Access-Control-Request-Method": "GET"})
    assert allowed.status_code == 200 and allowed.headers["access-control-allow-origin"] == "https://control.example"
    denied = client.options("/api/health", headers={"Origin": "https://unknown.example", "Access-Control-Request-Method": "GET"})
    assert denied.status_code == 400 and "access-control-allow-origin" not in denied.headers


def test_executable_module_help_works():
    result = subprocess.run([sys.executable, "-m", "alphaforge.control_center", "--help"], capture_output=True, text=True, shell=False, check=False)
    assert result.returncode == 0 and "--cors-origin" in result.stdout and "--project-root" in result.stdout


def test_hosted_frontend_browser_policy_is_documented():
    text = (Path(__file__).parents[1] / "docs" / "CONTROL_CENTER_RUNTIME_MAPPING.md").read_text(encoding="utf-8")
    assert "mixed-content" in text and "Private Network Access" in text


def test_health_separates_database_runtime_worker_and_exposes_diagnostics(tmp_path, monkeypatch):
    db, _ = seeded(tmp_path, monkeypatch); data = TestClient(create_app(f"sqlite+pysqlite:///{db}")).get("/api/health").json()["data"]
    assert data["backend_status"] == data["database_status"] == "AVAILABLE"
    assert data["runtime_status"] == "RUNTIME_NOT_RUNNING" and data["worker_status"] == "WORKER_UNHEALTHY"
    assert data["active_campaign_status"] == "RUNNING" and data["control_actions_status"] == "CONTROL_AVAILABLE"
    assert data["diagnostics"]["database_identity"]["filename"] == db.name
    service = ControlCenterService(tmp_path / "missing.db", tmp_path, Path(sys.executable), "token")
    assert service.health()["database_status"] == "DATABASE_UNAVAILABLE"


def test_runtime_execution_mode_comes_from_validated_config(tmp_path, monkeypatch):
    db, _ = seeded(tmp_path, monkeypatch); monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "LIVE")
    client = TestClient(create_app(f"sqlite+pysqlite:///{db}"))
    assert client.get("/api/runtime/status").json()["data"]["execution_mode"] == "LIVE"
    assert client.get("/api/health").json()["data"]["control_actions_status"] == "READ_ONLY"


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


def test_reject_total_and_campaign_distribution_are_limit_independent(tmp_path, monkeypatch):
    db, cid = seeded(tmp_path, monkeypatch); add_rejects(db, cid, 550)
    data = TestClient(create_app(f"sqlite+pysqlite:///{db}")).get(f"/api/campaigns/{cid}/rejects?limit=200").json()["data"]
    assert data["reject_total"] == 550 and data["returned_count"] == data["limit"] == 200
    assert data["pagination"]["has_more"] is True
    assert data["reason_distribution_scope"] == "campaign_distribution"
    assert sum(row["count"] for row in data["reason_distribution"]) == 550
    missing = next(row for row in data["reason_distribution"] if row["reason_quality"] == "MISSING")
    explicit_unknown = next(row for row in data["reason_distribution"] if row["reason"] == "UNKNOWN")
    assert missing["reason"] is None and explicit_unknown["reason_quality"] == "EXPLICIT"


def test_reject_deduplication_precedes_limit_and_page_is_full(tmp_path, monkeypatch):
    db, cid = seeded(tmp_path, monkeypatch); add_rejects(db, cid, 250, duplicate_first=True)
    data = ControlCenterService.from_environment().rejects(cid, 200)
    assert data["reject_total"] == 250 and data["returned_count"] == 200
    assert data["deduplication"] == {"applied": True, "key": "observation_id", "semantics": "DISTINCT_CANONICAL_OBSERVATIONS"}
    assert len({row["observation_id"] for row in data["items"]}) == 200


def test_reject_legacy_schema_without_identity_reports_raw_semantics_and_zero_rates(tmp_path, monkeypatch):
    db, cid = seeded(tmp_path, monkeypatch)
    conn = sqlite3.connect(db); run = conn.execute("SELECT active_run_id FROM burnin_campaigns WHERE campaign_id=?", (cid,)).fetchone()[0]
    conn.execute("DROP TABLE burnin_observations")
    conn.execute("CREATE TABLE burnin_observations(burnin_run_id TEXT,decision TEXT,metrics_json TEXT)")
    conn.execute("INSERT INTO burnin_observations VALUES(?,?,?)", (run, "REJECTED", json.dumps({"reject_reason": "UNKNOWN"}))); conn.commit(); conn.close()
    data = ControlCenterService.from_environment().rejects(cid, 200)
    assert data["reject_total"] == 1 and data["returned_count"] == 1
    assert data["deduplication"] == {"applied": False, "key": None, "semantics": "RAW_OBSERVATION_ROWS_NO_RELIABLE_UNIQUE_KEY"}


def test_pending_labels_are_not_counted_as_reject_history(tmp_path, monkeypatch):
    db, cid = seeded(tmp_path, monkeypatch)
    conn = sqlite3.connect(db); run = conn.execute("SELECT active_run_id FROM burnin_campaigns WHERE campaign_id=?", (cid,)).fetchone()[0]
    conn.execute("INSERT INTO burnin_pending_reject_labels(pending_label_id,campaign_id,burnin_run_id,reject_decision_id,symbol,side,decision_timestamp,execution_cost_assumptions_json,source_provenance_json,due_at,status,created_at,schema_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("pending", cid, run, "decision", "BTCUSDT", "LONG", "2026-01-01", "{}", "{}", "2026-01-02", "PENDING", "2026-01-01", "v"))
    conn.commit(); conn.close()
    assert ControlCenterService.from_environment().rejects(cid)["reject_total"] == 0


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


def test_pause_active_worker_is_partial_failure_and_audit_has_separate_postconditions(monkeypatch, tmp_path):
    db, cid = seeded(tmp_path, monkeypatch)
    conn = sqlite3.connect(db); conn.execute("UPDATE burnin_campaigns SET worker_pid=4242 WHERE campaign_id=?", (cid,)); conn.commit(); conn.close()
    monkeypatch.setattr("alphaforge.dashboard.control_center._pid_alive", lambda pid: True)
    service = ControlCenterService.from_environment(); service.pause_worker_timeout = 0
    def run(*_a, **_k):
        conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row; pause_campaign(conn, cid); conn.commit(); conn.close()
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr("alphaforge.dashboard.control_center.subprocess.run", run)
    with pytest.raises(Exception) as failure: service.control(cid, "pause", "correct-token")
    assert failure.value.code == "PARTIAL_FAILURE"
    audit = json.loads(service.audit_path.read_text().splitlines()[-1])
    assert audit["previous_campaign_status"] == "RUNNING" and audit["verified_campaign_status"] == "PAUSED"
    assert audit["previous_worker_status"] == audit["verified_worker_status"] == "ACTIVE"


def test_pause_worker_closes_during_bounded_polling(monkeypatch, tmp_path):
    db, cid = seeded(tmp_path, monkeypatch)
    conn = sqlite3.connect(db); conn.execute("UPDATE burnin_campaigns SET worker_pid=4242 WHERE campaign_id=?", (cid,)); conn.commit(); conn.close()
    alive = iter([True, True, False])
    monkeypatch.setattr("alphaforge.dashboard.control_center._pid_alive", lambda pid: next(alive, False))
    service = ControlCenterService.from_environment(); service.pause_worker_timeout = .2; service.pause_worker_poll_interval = .01
    def run(*_a, **_k):
        conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row; pause_campaign(conn, cid); conn.commit(); conn.close()
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr("alphaforge.dashboard.control_center.subprocess.run", run)
    assert service.control(cid, "pause", "correct-token")["operation"]["result"] == "SUCCESS"


def test_pause_campaign_postcondition_missing_is_command_failed(monkeypatch, tmp_path):
    _, cid = seeded(tmp_path, monkeypatch); service = ControlCenterService.from_environment(); service.pause_worker_timeout = 0
    monkeypatch.setattr("alphaforge.dashboard.control_center.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    with pytest.raises(Exception) as failure: service.control(cid, "pause", "correct-token")
    assert failure.value.code == "COMMAND_FAILED"


def test_pause_worker_verification_exception_is_sanitized_partial_failure(monkeypatch, tmp_path):
    _, cid = seeded(tmp_path, monkeypatch); service = ControlCenterService.from_environment()
    monkeypatch.setattr("alphaforge.dashboard.control_center.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(service, "_poll_pause_postcondition", lambda *_: (_ for _ in ()).throw(RuntimeError("token=private")))
    with pytest.raises(Exception) as failure: service.control(cid, "pause", "correct-token")
    assert failure.value.code == "PARTIAL_FAILURE"
    assert "private" not in failure.value.metadata["verification_error"]


def test_pid_absent_with_running_continuation_is_not_stopped(tmp_path, monkeypatch):
    _, cid = seeded(tmp_path, monkeypatch)
    status = ControlCenterService.from_environment().status(cid)
    assert ControlCenterService._worker_verification(status)["status"] == "UNKNOWN"


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
    with service._locks_guard: lock = service._locks.setdefault(cid, __import__("threading").Lock())
    lock.acquire()
    try:
        with pytest.raises(Exception) as failure: service.control(cid, "pause", "correct-token")
        assert failure.value.code == "INVALID_STATE_TRANSITION"
    finally: lock.release()


def test_lease_owner_can_release_but_old_owner_cannot_delete_replacement(tmp_path, monkeypatch):
    _, cid = seeded(tmp_path, monkeypatch); service = ControlCenterService.from_environment()
    path = service._acquire_lease(cid, "owner-one")
    assert service._release_lease(path, "owner-one") is True and not path.exists()
    path = service._acquire_lease(cid, "owner-new")
    assert service._release_lease(path, "owner-old") is False
    assert service._lease_metadata(path)["owner_token"] == "owner-new"


def test_stale_takeover_makes_old_release_harmless(tmp_path, monkeypatch):
    _, cid = seeded(tmp_path, monkeypatch); service = ControlCenterService.from_environment(); service.lease_stale_seconds = 1
    path = service._acquire_lease(cid, "old")
    (path / "owner.json").write_text(json.dumps({"owner_token": "old", "started_at": "2020-01-01T00:00:00+00:00"}))
    assert service._acquire_lease(cid, "new") == path
    assert service._release_lease(path, "old") is False
    assert service._lease_metadata(path)["owner_token"] == "new"


def test_malformed_lease_metadata_fails_closed(tmp_path, monkeypatch):
    _, cid = seeded(tmp_path, monkeypatch); service = ControlCenterService.from_environment()
    path = service._lease_path(cid); path.mkdir(parents=True); (path / "owner.json").write_text("{broken")
    with pytest.raises(Exception) as failure: service._acquire_lease(cid, "new")
    assert failure.value.code == "INVALID_STATE_TRANSITION" and path.exists()


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
    response = TestClient(create_app(f"sqlite+pysqlite:///{db}")).post("/api/campaigns/not-the-active-campaign/pause", headers={"X-AlphaForge-Control-Token": "correct-token"})
    assert response.status_code == 403 and response.json()["error"]["code"] == "INVALID_STATE_TRANSITION"
