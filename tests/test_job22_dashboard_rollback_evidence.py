from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from alphaforge.dashboard.app import create_app
from alphaforge.persistence import init_db
from alphaforge.rollback_evidence import persist_rollback_validation_evidence


def _complete_evidence() -> dict[str, object]:
    return {
        "kill_switch_block_verified": True,
        "no_submit_on_kill_switch_verified": True,
        "fail_closed_reconciliation_verified": True,
        "repair_actions_non_mutating_verified": True,
        "execution_mutation_attempt_count": 0,
        "blocking_reasons": [],
        "evidence_payload": {"validation_scope": "DASHBOARD_TEST"},
    }


def test_dashboard_probe_matrix_blocks_when_rollback_evidence_is_missing(tmp_path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'missing_rollback.db'}"
    engine = init_db(url)
    engine.dispose()
    payload = TestClient(create_app(url)).get("/api/v1/readiness/probes").json()
    rollback = next(probe for probe in payload["probes"] if probe["name"] == "rollback_ready")
    assert rollback["status"] == "NO_EVIDENCE"
    assert "ROLLBACK_EVIDENCE_MISSING" in rollback["details"]
    assert rollback["surface"] == "live_rollback_validation_evidence"


def test_dashboard_probe_matrix_displays_persisted_no_submit_evidence_read_only(tmp_path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'complete_rollback.db'}"
    engine = init_db(url)
    persist_rollback_validation_evidence(engine, _complete_evidence())
    engine.dispose()
    client = TestClient(create_app(url))
    payload = client.get("/api/v1/readiness/probes").json()
    rollback = next(probe for probe in payload["probes"] if probe["name"] == "rollback_ready")
    assert rollback["status"] == "PASS"
    assert "no_submit=True" in rollback["details"]
    assert "mutation_attempts=0" in rollback["details"]
    html = client.get("/readiness").text
    assert "live_rollback_validation_evidence" in html
    assert "no_submit=True" in html
