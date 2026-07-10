from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, text

from alphaforge.dashboard.app import create_app
from alphaforge.persistence import init_db
from alphaforge.release_gates import latest_release_snapshot, release_snapshot_by_id, canary_mutation_attempt_count


def test_latest_release_snapshot_read_path_emits_no_ddl(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'release-read.db'}", future=True)
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(str(statement).upper())

    assert latest_release_snapshot(engine) is None
    assert not any("CREATE " in stmt or "ALTER " in stmt for stmt in statements)


def test_release_read_helpers_return_missing_evidence_without_tables(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'absent.db'}", future=True)
    assert latest_release_snapshot(engine) is None
    assert release_snapshot_by_id(engine, "missing") is None
    assert canary_mutation_attempt_count(engine) is None
    assert "release_gate_snapshots" not in inspect(engine).get_table_names()


def test_release_read_helpers_work_against_read_only_sqlite_without_operational_error(tmp_path) -> None:
    db_path = tmp_path / "readonly-release.db"
    engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE unrelated(id INTEGER PRIMARY KEY)"))
    engine.dispose()
    ro = create_engine(f"sqlite+pysqlite:///file:{db_path.as_posix()}?mode=ro&uri=true", future=True)
    assert latest_release_snapshot(ro) is None


def test_dashboard_runtime_control_release_gate_no_evidence_is_read_only(tmp_path) -> None:
    db_path = tmp_path / "dashboard-release.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    seed = init_db(database_url)
    with seed.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS release_gate_snapshots"))
        conn.execute(text("DROP TABLE IF EXISTS release_operator_acks"))
        conn.execute(text("DROP TABLE IF EXISTS canary_mutation_attempts"))
    seed.dispose()

    payload = TestClient(create_app(database_url)).get("/api/v1/runtime/control").json()

    assert payload["release_gate"]["status"] == "NO_EVIDENCE"
    verify = create_engine(database_url, future=True)
    tables = set(inspect(verify).get_table_names())
    assert "release_gate_snapshots" not in tables
    assert "release_operator_acks" not in tables
    assert "canary_mutation_attempts" not in tables
