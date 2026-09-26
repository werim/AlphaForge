from __future__ import annotations

from datetime import datetime, timezone
import importlib.util

import pytest
from sqlalchemy import create_engine, event, inspect, text

from alphaforge.live_readiness import LiveReadinessEvaluator
from alphaforge.persistence import init_db
from alphaforge.rollback_evidence import persist_rollback_validation_evidence
from alphaforge.release_gates import (
    build_release_snapshot,
    canary_mutation_attempt_count,
    latest_release_snapshot,
    latest_valid_operator_ack,
    persist_canary_event,
    persist_operator_ack,
    persist_release_snapshot,
    persist_rollback_verification,
    persist_runbook_evidence,
    release_snapshot_by_id,
)


def _schema_tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def test_latest_release_snapshot_read_path_emits_no_create_or_alter(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'release-read.db'}", future=True)
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(str(statement).upper())

    assert latest_release_snapshot(engine) is None
    assert not any("CREATE " in stmt or "ALTER " in stmt for stmt in statements)


def test_release_read_helpers_return_no_evidence_without_bootstrap(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'absent.db'}", future=True)
    assert latest_release_snapshot(engine) is None
    assert release_snapshot_by_id(engine, "release-missing", phase="PHASE6") is None
    assert latest_valid_operator_ack(engine, release_id="release-missing", phase="PHASE6") is None
    assert canary_mutation_attempt_count(engine, release_id="release-missing", phase="PHASE6") is None
    assert "release_gate_snapshots" not in _schema_tables(engine)


def test_canonical_pr269_release_schema_names_are_preserved(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'canonical.db'}")
    assert {
        "release_gate_snapshots",
        "operator_acknowledgements",
        "canary_run_events",
        "rollback_verification_events",
        "runbook_evidence",
    }.issubset(_schema_tables(engine))
    assert "release_operator_acks" not in _schema_tables(engine)
    assert "canary_mutation_attempts" not in _schema_tables(engine)


def test_expired_and_malformed_operator_ack_fail_closed(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'ack.db'}")
    persist_operator_ack(
        engine,
        release_id="rel-1",
        phase="PHASE6",
        acknowledgement_text=required_operator_ack_text("rel-1"),
        ttl_minutes=60,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert latest_valid_operator_ack(engine, release_id="rel-1", phase="PHASE6", now=datetime(2026, 7, 10, tzinfo=timezone.utc)) is None

    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO operator_acknowledgements(ack_id, release_id, phase, acknowledged_at, valid_until, operator_id, acknowledgement_text, evidence_json)
            VALUES ('ack:malformed', 'rel-1', 'PHASE6', '2026-01-01T00:00:00Z', 'not-a-timestamp', 'operator', 'ack', '{}')
        """))
    assert latest_valid_operator_ack(engine, release_id="rel-1", phase="PHASE6", now=datetime(2026, 1, 1, tzinfo=timezone.utc)) is None


def test_trivial_operator_ack_text_is_persisted_but_never_valid(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'ack-trivial.db'}")
    row = persist_operator_ack(
        engine,
        release_id="rel-trivial",
        phase="PHASE6",
        acknowledgement_text="acknowledged",
        ttl_minutes=60,
    )
    assert row["valid"] is False
    assert row["blocker_reason"] == "ACK_TEXT_MISSING_RELEASE_OR_RISK_PHRASE"
    assert latest_valid_operator_ack(engine, release_id="rel-trivial", phase="PHASE6") is None


def test_operator_ack_ttl_is_bounded_to_four_hours(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'ack-ttl.db'}")
    with pytest.raises(ValueError, match="OPERATOR_ACK_TTL_OUT_OF_RANGE"):
        persist_operator_ack(
            engine,
            release_id="rel-ttl",
            phase="PHASE6",
            acknowledgement_text=required_operator_ack_text("rel-ttl"),
            ttl_minutes=241,
        )


def test_release_id_and_phase_must_match_for_operator_ack(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'ack-match.db'}")
    now=datetime(2026, 7, 10, tzinfo=timezone.utc)
    persist_operator_ack(
        engine,
        release_id="rel-1",
        phase="PHASE6",
        acknowledgement_text=required_operator_ack_text("rel-1"),
        ttl_minutes=60,
        now=now,
    )
    assert latest_valid_operator_ack(engine, release_id="rel-1", phase="PHASE6", now=now) is not None
    assert latest_valid_operator_ack(engine, release_id="rel-2", phase="PHASE6", now=now) is None
    assert latest_valid_operator_ack(engine, release_id="rel-1", phase="PHASE5", now=now) is None


@pytest.mark.skipif(importlib.util.find_spec("fastapi") is None or importlib.util.find_spec("httpx") is None, reason="fastapi/httpx unavailable")
def test_dashboard_get_read_only_sqlite_executes_no_create_or_alter(tmp_path) -> None:
    from fastapi.testclient import TestClient
    from alphaforge.dashboard.app import create_app

    db_path = tmp_path / "dashboard-release.db"
    seed = init_db(f"sqlite+pysqlite:///{db_path}")
    with seed.begin() as conn:
        for table in ["release_gate_snapshots", "operator_acknowledgements", "canary_run_events", "rollback_verification_events", "runbook_evidence"]:
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
    seed.dispose()

    app = create_app(f"sqlite+pysqlite:///{db_path}")
    statements: list[str] = []

    @event.listens_for(app.state.engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(str(statement).upper())

    payload = TestClient(app).get("/api/v1/runtime/control").json()

    assert payload["release_gate"]["status"] == "NO_EVIDENCE"
    assert not any("CREATE " in stmt or "ALTER " in stmt for stmt in statements)
    verify = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    assert "release_gate_snapshots" not in _schema_tables(verify)


def test_build_release_snapshot_all_phase6_evidence_canary_ready_not_live_ready(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'snapshot.db'}")
    persist_operator_ack(
        engine,
        release_id="rel-ready",
        phase="PHASE6",
        acknowledgement_text=required_operator_ack_text("rel-ready"),
    )
    persist_canary_event(engine, release_id="rel-ready", phase="PHASE6", mutation_attempted=False)
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO rollback_verification_events(verification_id, release_id, phase, verified_at, status, evidence_json)
            VALUES ('rollback:rel-ready', 'rel-ready', 'PHASE6', '2026-01-01T00:00:00Z', 'PASS', '{}')
        """))
        conn.execute(text("""
            INSERT INTO runbook_evidence(evidence_id, release_id, phase, recorded_at, status, evidence_json)
            VALUES ('runbook:rel-ready', 'rel-ready', 'PHASE6', '2026-01-01T00:00:00Z', 'PASS', '{}')
        """))
    snapshot = build_release_snapshot(engine, release_id="rel-ready", phase="PHASE6")
    persist_release_snapshot(engine, snapshot)

    assert snapshot.status == "CANARY_READY"
    assert snapshot.blocking_reasons == []
    assert latest_release_snapshot(engine, release_id="rel-ready", phase="PHASE6").status == "CANARY_READY"


def _valid_runbook_text() -> str:
    return """# Test Runbook
## Explicit LIVE boundary
LIVE remains blocked.
## Suspension conditions
Fail closed.
## Operator workflow
Operator verifies evidence.
## Phase 9 PAPER Burn-in Operations
Use recovery-drill before promotion and finalize only after qualification.
"""


def test_rollback_writer_derives_pass_only_from_fresh_measured_evidence(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'rollback-writer.db'}")
    failed = persist_rollback_verification(engine, release_id="rel-rollback")
    assert failed["status"] == "FAIL"
    assert "ROLLBACK_EVIDENCE_MISSING" in failed["evidence"]["blocking_reasons"]

    persist_rollback_validation_evidence(engine, {
        "validation_id": "rollback-validation:rel-rollback",
        "kill_switch_block_verified": True,
        "no_submit_on_kill_switch_verified": True,
        "fail_closed_reconciliation_verified": True,
        "repair_actions_non_mutating_verified": True,
        "execution_mutation_attempt_count": 0,
        "blocking_reasons": [],
        "evidence_payload": {"validation_scope": "RELEASE_GATE_WRITER_TEST"},
    })
    passed = persist_rollback_verification(engine, release_id="rel-rollback")
    assert passed["status"] == "PASS"
    assert passed["evidence"]["source"] == "DETERMINISTIC_VALIDATION"
    assert passed["evidence"]["execution_mutation_attempt_count"] == 0


def test_runbook_writer_hashes_content_and_fails_closed_on_missing_safety_marker(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'runbook-writer.db'}")
    runbook = tmp_path / "RUNBOOK.md"
    runbook.write_text(_valid_runbook_text(), encoding="utf-8")

    passed = persist_runbook_evidence(engine, release_id="rel-runbook", runbook_path=runbook)
    assert passed["status"] == "PASS"
    assert passed["evidence"]["sha256"]
    assert passed["evidence"]["missing_markers"] == []

    runbook.write_text("# Incomplete\n## Explicit LIVE boundary\n", encoding="utf-8")
    failed = persist_runbook_evidence(engine, release_id="rel-runbook", runbook_path=runbook)
    assert failed["status"] == "FAIL"
    assert "## Operator workflow" in failed["evidence"]["missing_markers"]


def test_release_snapshot_consumes_canonical_rollback_and_runbook_writers(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'writer-snapshot.db'}")
    release_id = "rel-writer-ready"
    runbook = tmp_path / "RUNBOOK.md"
    runbook.write_text(_valid_runbook_text(), encoding="utf-8")

    persist_operator_ack(
        engine,
        release_id=release_id,
        phase="PHASE6",
        acknowledgement_text=required_operator_ack_text(release_id),
    )
    persist_canary_event(engine, release_id=release_id, phase="PHASE6", mutation_attempted=False)
    persist_rollback_validation_evidence(engine, {
        "validation_id": "rollback-validation:rel-writer-ready",
        "kill_switch_block_verified": True,
        "no_submit_on_kill_switch_verified": True,
        "fail_closed_reconciliation_verified": True,
        "repair_actions_non_mutating_verified": True,
        "execution_mutation_attempt_count": 0,
        "blocking_reasons": [],
        "evidence_payload": {"validation_scope": "RELEASE_GATE_WRITER_TEST"},
    })
    persist_rollback_verification(engine, release_id=release_id)
    persist_runbook_evidence(engine, release_id=release_id, runbook_path=runbook)

    snapshot = build_release_snapshot(engine, release_id=release_id)
    assert snapshot.status == "CANARY_READY"
    assert snapshot.rollback_verified is True
    assert snapshot.runbook_verified is True
    assert snapshot.blocking_reasons == []
