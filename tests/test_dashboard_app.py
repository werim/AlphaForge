from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from alphaforge.dashboard.app import create_app
from alphaforge.persistence import init_db


def test_dashboard_health_and_status_are_read_only_and_honest(tmp_path) -> None:
    db_path = tmp_path / "dashboard.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    app = create_app(database_url)
    client = TestClient(app)

    assert client.get("/health").json()["status"] == "ok"
    status = client.get("/api/v1/runtime/status").json()
    assert status["runtime_process_status"] == "UNVERIFIED"
    assert status["runtime_process_status_reason"] == "PERSISTED_HEARTBEAT_NOT_IMPLEMENTED"
    assert status["latest_readiness"]["status"] == "NOT_AVAILABLE"
    assert inspect(app.state.engine).get_table_names() == []
    assert not db_path.exists(), "dashboard must not create a missing runtime SQLite database"


def test_reject_summary_surfaces_incomplete_rows(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'rejects.db'}"
    seed_engine = init_db(database_url)
    with seed_engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO order_decisions(decision_id, signal_id, symbol, mode, phase, decision, reject_reason, created_at)
            VALUES ('d1', 's1', 'BTCUSDT', 'PAPER', 'final', 'REJECTED', 'LOW_SCORE', '2026-05-22T10:00:00Z'),
                   ('d2', '', '', 'PAPER', 'final', 'REJECTED', '', '2026-05-22T10:01:00Z'),
                   ('d3', 's3', 'ETHUSDT', 'PAPER', 'final', 'ACCEPTED', NULL, '2026-05-22T10:02:00Z')
        """))
    seed_engine.dispose()
    app = create_app(database_url)
    payload = TestClient(app).get("/api/v1/rejects/summary").json()
    assert payload["total_final_decisions"] == 3
    assert payload["total_rejected"] == 2
    assert payload["incomplete_rejected_rows"]["empty_signal_id_count"] == 1
    assert payload["incomplete_rejected_rows"]["empty_symbol_count"] == 1
    assert payload["incomplete_rejected_rows"]["empty_reject_reason_count"] == 1


def test_lifecycle_timeline_is_sorted_and_flags_missing_reject_reason(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'lifecycle.db'}"
    seed_engine = init_db(database_url)
    with seed_engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO trade_lifecycle_events(event_id, signal_id, symbol, mode, lifecycle_state, reject_reason, event_ts, created_at)
            VALUES ('e2', 'sig-1', 'BTCUSDT', 'PAPER', 'SIGNAL_REJECTED', '', '2026-05-22T10:01:00Z', '2026-05-22T10:01:00Z'),
                   ('e1', 'sig-1', 'BTCUSDT', 'PAPER', 'SIGNAL_CREATED', NULL, '2026-05-22T10:00:00Z', '2026-05-22T10:00:00Z')
        """))
    seed_engine.dispose()
    app = create_app(database_url)
    payload = TestClient(app).get("/api/v1/lifecycle/sig-1").json()
    assert [row["lifecycle_state"] for row in payload["events"]] == ["SIGNAL_CREATED", "SIGNAL_REJECTED"]
    assert payload["has_signal_created"] is True
    assert payload["rejected_without_reason"] is True


def test_html_dashboard_pages_load_without_existing_runtime_schema(tmp_path) -> None:
    client = TestClient(create_app(f"sqlite+pysqlite:///{tmp_path / 'pages.db'}"))
    for path in ["/", "/rejects", "/lifecycle", "/readiness"]:
        response = client.get(path)
        assert response.status_code == 200
        assert "AlphaForge" in response.text


def test_dashboard_has_no_execution_or_live_mutation_routes(tmp_path) -> None:
    app = create_app(f"sqlite+pysqlite:///{tmp_path / 'routes.db'}")
    paths = {route.path for route in app.routes}
    forbidden_fragments = {"order", "execute", "live/activate", "kill-switch", "config/update"}
    assert not any(fragment in path.lower() for path in paths for fragment in forbidden_fragments)
