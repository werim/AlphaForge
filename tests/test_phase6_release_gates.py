from __future__ import annotations

from datetime import datetime, timezone
import importlib.util

import pytest
from sqlalchemy import create_engine, event, inspect, text

from alphaforge.live_readiness import LiveReadinessEvaluator
from alphaforge.persistence import init_db
from alphaforge.release_gates import (
    build_release_snapshot,
    canary_mutation_attempt_count,
    latest_release_snapshot,
    latest_valid_operator_ack,
    persist_canary_event,
    persist_operator_ack,
    persist_release_snapshot,
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
    persist_operator_ack(engine, release_id="rel-1", phase="PHASE6", valid_until="2026-01-01T00:00:00Z")
    assert latest_valid_operator_ack(engine, release_id="rel-1", phase="PHASE6", now=datetime(2026, 7, 10, tzinfo=timezone.utc)) is None

    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO operator_acknowledgements(ack_id, release_id, phase, acknowledged_at, valid_until, operator_id, acknowledgement_text, evidence_json)
            VALUES ('ack:malformed', 'rel-1', 'PHASE6', '2026-01-01T00:00:00Z', 'not-a-timestamp', 'operator', 'ack', '{}')
        """))
    assert latest_valid_operator_ack(engine, release_id="rel-1", phase="PHASE6", now=datetime(2026, 1, 1, tzinfo=timezone.utc)) is None


def test_release_id_and_phase_must_match_for_operator_ack(tmp_path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'ack-match.db'}")
    persist_operator_ack(engine, release_id="rel-1", phase="PHASE6", valid_until="2099-01-01T00:00:00Z")
    assert latest_valid_operator_ack(engine, release_id="rel-1", phase="PHASE6") is not None
    assert latest_valid_operator_ack(engine, release_id="rel-2", phase="PHASE6") is None
    assert latest_valid_operator_ack(engine, release_id="rel-1", phase="PHASE5") is None


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
    persist_operator_ack(engine, release_id="rel-ready", phase="PHASE6", valid_until="2099-01-01T00:00:00Z")
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
