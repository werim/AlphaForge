from __future__ import annotations

import json
import sqlite3
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
    with service._locks_guard: lock = service._locks.setdefault(cid, __import__("threading").Lock())
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
