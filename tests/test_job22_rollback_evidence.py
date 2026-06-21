from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import text

from alphaforge.persistence import init_db
from alphaforge.rollback_evidence import latest_persisted_rollback_evidence, persist_rollback_validation_evidence, run_deterministic_rollback_validation


def _complete_evidence() -> dict[str, object]:
    return {
        "kill_switch_block_verified": True,
        "no_submit_on_kill_switch_verified": True,
        "fail_closed_reconciliation_verified": True,
        "repair_actions_non_mutating_verified": True,
        "execution_mutation_attempt_count": 0,
        "blocking_reasons": [],
        "evidence_payload": {"validation_scope": "TEST"},
    }


def test_missing_evidence_fails_closed() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    loaded = latest_persisted_rollback_evidence(engine)
    assert loaded["rollback_evidence_verified"] is False
    assert loaded["rollback_blocking_reasons"] == ["ROLLBACK_EVIDENCE_MISSING"]


def test_deterministic_validator_proves_guard_without_submit() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    saved = asyncio.run(run_deterministic_rollback_validation(engine))
    loaded = latest_persisted_rollback_evidence(engine)
    assert saved["evidence_status"] == "COMPLETE"
    assert loaded["rollback_evidence_verified"] is True
    assert loaded["execution_mutation_attempt_count"] == 0
    assert loaded["evidence_payload"]["guard_reject_reason"] in {"GLOBAL_KILL_SWITCH", "KILL_SWITCH_ACTIVE"}
    assert loaded["evidence_payload"]["incident_rows_before"] == loaded["evidence_payload"]["incident_rows_after"]


def test_stale_and_future_evidence_fail_closed() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    persist_rollback_validation_evidence(engine, _complete_evidence())
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(text("UPDATE live_rollback_validation_evidence SET recorded_at=:ts"), {"ts": (now - timedelta(hours=1)).isoformat()})
    assert latest_persisted_rollback_evidence(engine, now=now)["rollback_blocking_reasons"] == ["ROLLBACK_EVIDENCE_STALE"]
    with engine.begin() as conn:
        conn.execute(text("UPDATE live_rollback_validation_evidence SET recorded_at=:ts"), {"ts": (now + timedelta(minutes=1)).isoformat()})
    assert latest_persisted_rollback_evidence(engine, now=now)["rollback_blocking_reasons"] == ["ROLLBACK_EVIDENCE_FUTURE_DATED"]


def test_failed_evidence_cannot_report_complete() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    evidence = _complete_evidence()
    evidence["no_submit_on_kill_switch_verified"] = False
    evidence["execution_mutation_attempt_count"] = 1
    saved = persist_rollback_validation_evidence(engine, evidence)
    assert saved["evidence_status"] == "INCOMPLETE"
    assert latest_persisted_rollback_evidence(engine)["rollback_evidence_verified"] is False


def test_evidence_payload_persists_only_allowlisted_fields() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    evidence = _complete_evidence()
    evidence["evidence_payload"] = {
        "validation_scope": "ALLOWLIST_TEST",
        "guard_path": "existing_guard",
        "unapproved_note": "must-not-persist",
        "raw_transport_payload": "must-not-persist",
    }
    persist_rollback_validation_evidence(engine, evidence)
    with engine.connect() as conn:
        stored = json.loads(conn.execute(text("SELECT evidence_payload FROM live_rollback_validation_evidence")).scalar_one())
    assert stored["validation_scope"] == "ALLOWLIST_TEST"
    assert stored["guard_path"] == "existing_guard"
    assert "unapproved_note" not in stored
    assert "raw_transport_payload" not in stored


def test_malformed_persisted_payload_fails_closed() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    persist_rollback_validation_evidence(engine, _complete_evidence())
    with engine.begin() as conn:
        conn.execute(text("UPDATE live_rollback_validation_evidence SET evidence_payload=:payload"), {"payload": "not-json"})
    loaded = latest_persisted_rollback_evidence(engine)
    assert loaded["rollback_evidence_verified"] is False
    assert loaded["rollback_blocking_reasons"] == ["ROLLBACK_EVIDENCE_INVALID"]
