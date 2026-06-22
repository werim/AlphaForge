from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError

from alphaforge.dashboard.app import create_app
from alphaforge.persistence import init_db
from alphaforge.runtime_heartbeat import save_runtime_heartbeat


def test_dashboard_health_and_status_are_read_only_and_honest(tmp_path) -> None:
    db_path = tmp_path / "dashboard.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    app = create_app(database_url)
    client = TestClient(app)

    assert client.get("/health").json()["status"] == "ok"
    status = client.get("/api/v1/runtime/status").json()
    assert status["runtime_process_status"] == "MISSING"
    assert status["runtime_process_status_reason"] == "NO_PERSISTED_RUNTIME_HEARTBEAT"
    assert status["latest_readiness"]["status"] == "NOT_AVAILABLE"
    assert inspect(app.state.engine).get_table_names() == []
    assert db_path.exists(), "dashboard control state now persists in the runtime SQLite database"


def test_existing_runtime_sqlite_is_opened_read_only(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    seed_engine = init_db(database_url)
    seed_engine.dispose()
    app = create_app(database_url)
    with pytest.raises(OperationalError):
        with app.state.engine.begin() as conn:
            conn.execute(text("INSERT INTO order_decisions(decision_id) VALUES ('must-not-write')"))


def test_fresh_paper_heartbeat_appears_in_dashboard_runtime_status(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'fresh_paper.db'}"
    engine = init_db(database_url)
    save_runtime_heartbeat(
        engine,
        runtime_instance_id="runtime:paper-fresh",
        execution_mode="PAPER",
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
        last_scan_ts=datetime.now(timezone.utc).isoformat(),
        active_positions_count=1,
        pending_orders_count=2,
    )
    engine.dispose()

    payload = TestClient(create_app(database_url)).get("/api/v1/runtime/status").json()
    assert payload["runtime_process_status"] == "FRESH"
    assert payload["runtime_process_status_reason"] == "HEARTBEAT_WITHIN_MAX_AGE"
    assert payload["execution_mode"] == "PAPER"
    assert payload["runtime_instance_id"] == "runtime:paper-fresh"
    assert payload["latest_heartbeat_ts"]
    assert payload["active_positions_count"] == 1
    assert payload["pending_orders_count"] == 2


def test_dashboard_read_does_not_persist_or_mutate_heartbeat_evidence(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'read_only_heartbeat.db'}"
    engine = init_db(database_url)
    save_runtime_heartbeat(engine, runtime_instance_id="runtime:read-only", execution_mode="PAPER", scanner_source="EXCHANGE_PUBLIC_MARKET_DATA")
    with engine.connect() as conn:
        before = conn.execute(text("SELECT COUNT(*) FROM runtime_heartbeats")).scalar_one()
    engine.dispose()

    client = TestClient(create_app(database_url))
    assert client.get("/api/v1/runtime/status").status_code == 200
    assert client.get("/api/v1/readiness/probes").status_code == 200

    verify = init_db(database_url)
    with verify.connect() as conn:
        after = conn.execute(text("SELECT COUNT(*) FROM runtime_heartbeats")).scalar_one()
    assert before == after == 1


@pytest.mark.parametrize(
    ("heartbeat_ts", "expected_state"),
    [
        ((datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(), "STALE"),
        ((datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(), "FUTURE_DATED"),
        ("not-a-timestamp", "INVALID"),
    ],
)
def test_stale_future_or_malformed_heartbeat_fails_closed_in_dashboard(tmp_path, heartbeat_ts: str, expected_state: str) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / (expected_state.lower() + '.db')}"
    engine = init_db(database_url)
    save_runtime_heartbeat(
        engine,
        runtime_instance_id=f"runtime:{expected_state.lower()}",
        execution_mode="PAPER",
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
        heartbeat_ts=heartbeat_ts,
    )
    engine.dispose()
    payload = TestClient(create_app(database_url)).get("/api/v1/runtime/status").json()
    assert payload["runtime_process_status"] == expected_state


def test_latest_heartbeat_selection_is_deterministic_for_equal_timestamps(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'latest.db'}"
    engine = init_db(database_url)
    timestamp = datetime.now(timezone.utc).isoformat()
    save_runtime_heartbeat(engine, runtime_instance_id="runtime:first", execution_mode="PAPER", scanner_source="EXCHANGE_PUBLIC_MARKET_DATA", heartbeat_ts=timestamp)
    save_runtime_heartbeat(engine, runtime_instance_id="runtime:second", execution_mode="PAPER", scanner_source="EXCHANGE_PUBLIC_MARKET_DATA", heartbeat_ts=timestamp)
    engine.dispose()
    payload = TestClient(create_app(database_url)).get("/api/v1/runtime/status").json()
    assert payload["runtime_instance_id"] == "runtime:second"


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


def test_readiness_probe_matrix_fail_closes_missing_report_and_live_heartbeat(tmp_path) -> None:
    db_path = tmp_path / "probe_matrix.db"
    app = create_app(f"sqlite+pysqlite:///{db_path}")
    payload = TestClient(app).get("/api/v1/readiness/probes").json()

    assert payload["status"] == "INCOMPLETE"
    assert payload["expected_probe_count"] == 27
    assert payload["critical_gap_count"] == 27
    heartbeat = next(probe for probe in payload["probes"] if probe["name"] == "runtime_heartbeat")
    assert heartbeat["status"] == "MISSING"
    assert heartbeat["details"] == "NO_PERSISTED_LIVE_RUNTIME_HEARTBEAT"
    assert payload["counts"]["MISSING"] == 1
    assert payload["counts"]["NO_EVIDENCE"] == 26
    assert payload["control_boundary"]["dashboard_mutation_controls"] == "INTENTIONALLY_OMITTED"
    assert db_path.exists(), "dashboard control state is persisted even when probe evidence is missing"


def test_readiness_probe_matrix_surfaces_fresh_live_heartbeat_and_missing_report_checks(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'readiness.db'}"
    seed_engine = init_db(database_url)
    save_runtime_heartbeat(seed_engine, runtime_instance_id="runtime:live-fresh", execution_mode="LIVE", scanner_source="EXCHANGE_PUBLIC_MARKET_DATA")
    with seed_engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS live_readiness_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                generated_at TEXT NOT NULL,
                qualified INTEGER NOT NULL,
                deployment_state TEXT NOT NULL,
                acknowledgement_required INTEGER NOT NULL,
                report_payload TEXT NOT NULL
            )
        """))
        conn.execute(text("""
            INSERT INTO live_readiness_reports(generated_at, qualified, deployment_state, acknowledgement_required, report_payload)
            VALUES (:generated_at, 0, 'LIVE_BLOCKED', 1, :payload)
        """), {
            "generated_at": "2026-05-23T10:00:00Z",
            "payload": json.dumps({"checks": [{"name": "lifecycle_no_orphans", "passed": True, "details": "orphan_signals=0"}]}),
        })
    seed_engine.dispose()
    payload = TestClient(create_app(database_url)).get("/api/v1/readiness/probes").json()

    assert next(probe for probe in payload["probes"] if probe["name"] == "runtime_heartbeat")["status"] == "PASS"
    assert next(probe for probe in payload["probes"] if probe["name"] == "lifecycle_no_orphans")["status"] == "PASS"
    assert next(probe for probe in payload["probes"] if probe["name"] == "mode_parity")["status"] == "MISSING_IN_REPORT"
    assert payload["critical_gap_count"] == 25


def test_html_dashboard_pages_load_without_existing_runtime_schema(tmp_path) -> None:
    client = TestClient(create_app(f"sqlite+pysqlite:///{tmp_path / 'pages.db'}"))
    for path in ["/", "/rejects", "/lifecycle", "/readiness"]:
        response = client.get(path)
        assert response.status_code == 200
        assert "AlphaForge" in response.text


def test_dashboard_has_no_execution_or_live_mutation_routes(tmp_path) -> None:
    app = create_app(f"sqlite+pysqlite:///{tmp_path / 'routes.db'}")
    paths = {route.path for route in app.routes}
    forbidden_fragments = {"order", "execute", "live/activate", "config/update", "heartbeat/write"}
    assert not any(fragment in path.lower() for path in paths for fragment in forbidden_fragments)


def test_dashboard_exposes_backtest_form(tmp_path) -> None:
    response = TestClient(create_app(f"sqlite+pysqlite:///{tmp_path / 'form.db'}")).get("/")
    assert response.status_code == 200
    assert "Run Backtest" in response.text
    assert "Backtest only. Does not place orders." in response.text
    assert "BACKTEST ONLY" in response.text
    assert 'name="last_days"' in response.text
    assert 'name="symbols"' in response.text


def test_dashboard_rejects_invalid_last_days(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import alphaforge.dashboard.app as dashboard_app

    called = False

    def fail_if_called(_request):
        nonlocal called
        called = True

    monkeypatch.setattr(dashboard_app, "run_dashboard_backtest", fail_if_called)
    response = TestClient(create_app(f"sqlite+pysqlite:///{tmp_path / 'invalid-days.db'}")).post(
        "/backtest/run",
        data={"last_days": "0", "symbols": "BTCUSDT", "timeframe": "15m", "initial_balance": "10000", "max_symbols": "2"},
    )
    assert response.status_code == 200
    assert "last_days must be between 1 and 730" in response.text
    assert called is False


def test_dashboard_rejects_empty_symbols(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import alphaforge.dashboard.app as dashboard_app

    monkeypatch.setattr(dashboard_app, "run_dashboard_backtest", lambda _request: pytest.fail("runner must not be called"))
    response = TestClient(create_app(f"sqlite+pysqlite:///{tmp_path / 'empty-symbols.db'}")).post(
        "/backtest/run",
        data={"last_days": "30", "symbols": " , ", "timeframe": "15m", "initial_balance": "10000", "max_symbols": "2"},
    )
    assert response.status_code == 200
    assert "symbols must contain at least one non-empty symbol" in response.text


def test_backtest_endpoint_calls_runner_with_backtest_only_request(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import alphaforge.dashboard.app as dashboard_app
    from alphaforge.dashboard.backtest_control import DashboardBacktestResult

    seen = {}

    def fake_runner(request):
        seen["request"] = request
        return DashboardBacktestResult(
            status="COMPLETED",
            period="last 30 days",
            symbols=request.symbols,
            timeframe=request.timeframe,
            initial_balance=request.initial_balance,
            max_symbols=request.max_symbols,
            total_candidates=3,
            accepted_trades=1,
            rejected_signals=2,
            win_count=1,
            loss_count=0,
            open_count=0,
            net_pnl="12.5",
            total_return_pct="0.125",
            output_dir=str(tmp_path / "bt"),
        )

    monkeypatch.setattr(dashboard_app, "run_dashboard_backtest", fake_runner)
    response = TestClient(create_app(f"sqlite+pysqlite:///{tmp_path / 'success.db'}")).post(
        "/backtest/run",
        data={"last_days": "30", "symbols": " BTCUSDT, ETHUSDT ", "timeframe": "15m", "initial_balance": "10000", "max_symbols": "2"},
    )
    assert response.status_code == 200
    assert seen["request"].symbols == ["BTCUSDT", "ETHUSDT"]
    assert not hasattr(seen["request"], "mode"), "dashboard request should not expose user-selectable PAPER/LIVE mode"
    assert "COMPLETED" in response.text
    assert "Total candidates/signals" in response.text
    assert "12.5" in response.text


def test_backtest_button_cannot_trigger_live_or_paper_order_execution(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import subprocess

    import alphaforge.dashboard.backtest_control as backtest_control
    from alphaforge.dashboard.backtest_control import DashboardBacktestRequest, run_dashboard_backtest

    monkeypatch.setattr(backtest_control.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "NO_HISTORICAL_DATA"))
    request = DashboardBacktestRequest(last_days=1, symbols=["BTCUSDT"], timeframe="15m", initial_balance=10000.0, max_symbols=1)
    result = run_dashboard_backtest(request)
    assert "--mode" in result.command
    assert result.command[result.command.index("--mode") + 1] == "BACKTEST"
    command_text = " ".join(result.command).lower()
    assert " live" not in command_text
    assert " paper" not in command_text


def test_backtest_failure_is_rendered_as_safe_error(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import alphaforge.dashboard.app as dashboard_app
    from alphaforge.dashboard.backtest_control import DashboardBacktestResult

    def fake_runner(request):
        return DashboardBacktestResult("FAILED", "last 30 days", request.symbols, request.timeframe, request.initial_balance, request.max_symbols, error_message="NO_HISTORICAL_DATA")

    monkeypatch.setattr(dashboard_app, "run_dashboard_backtest", fake_runner)
    response = TestClient(create_app(f"sqlite+pysqlite:///{tmp_path / 'failed.db'}")).post(
        "/backtest/run",
        data={"last_days": "30", "symbols": "BTCUSDT", "timeframe": "15m", "initial_balance": "10000", "max_symbols": "1"},
    )
    assert response.status_code == 200
    assert "Backtest failed closed" in response.text
    assert "NO_HISTORICAL_DATA" in response.text


def test_unavailable_lifecycle_metrics_render_warning_not_fake_zero(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import alphaforge.dashboard.app as dashboard_app
    from alphaforge.dashboard.backtest_control import DashboardBacktestResult

    def fake_runner(request):
        return DashboardBacktestResult(
            "COMPLETED",
            "last 30 days",
            request.symbols,
            request.timeframe,
            request.initial_balance,
            request.max_symbols,
            lifecycle_warning="Lifecycle/reject metrics unavailable from generated backtest artifacts; values are shown as unavailable, not zero.",
            execution_context_warning="Execution context is incomplete; unknown spread/slippage/funding is unavailable, not assumed zero.",
        )

    monkeypatch.setattr(dashboard_app, "run_dashboard_backtest", fake_runner)
    response = TestClient(create_app(f"sqlite+pysqlite:///{tmp_path / 'warnings.db'}")).post(
        "/backtest/run",
        data={"last_days": "30", "symbols": "BTCUSDT", "timeframe": "15m", "initial_balance": "10000", "max_symbols": "1"},
    )
    assert response.status_code == 200
    assert "Lifecycle/reject metrics unavailable" in response.text
    assert "unknown spread/slippage/funding is unavailable" in response.text
    assert "<td>Unavailable</td>" in response.text

def test_dashboard_backtest_command_forces_fresh_historical_refresh(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import subprocess

    import alphaforge.dashboard.backtest_control as backtest_control
    from alphaforge.dashboard.backtest_control import DashboardBacktestRequest, run_dashboard_backtest

    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(command, 1, "", "NO_HISTORICAL_DATA")

    monkeypatch.setattr(backtest_control.subprocess, "run", fake_run)
    request = DashboardBacktestRequest(last_days=30, symbols=["BTCUSDT", "ETHUSDT"], timeframe="15m", initial_balance=10000.0, max_symbols=2)
    result = run_dashboard_backtest(request)
    assert result.status == "FAILED"
    assert "--force-refresh" in seen["command"]
    assert "--force-refresh" in result.command


def test_backtest_historical_data_failure_uses_clean_dashboard_message(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    import alphaforge.dashboard.backtest_control as backtest_control
    from alphaforge.dashboard.backtest_control import DashboardBacktestRequest, INSUFFICIENT_BINANCE_DATA_MESSAGE, run_dashboard_backtest

    stderr = "HistoricalDataError: Historical coverage starts after requested start"
    monkeypatch.setattr(backtest_control.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", stderr))
    request = DashboardBacktestRequest(last_days=30, symbols=["BTCUSDT"], timeframe="15m", initial_balance=10000.0, max_symbols=1)
    result = run_dashboard_backtest(request)
    assert result.status == "FAILED"
    assert result.error_message == INSUFFICIENT_BINANCE_DATA_MESSAGE


def test_dashboard_runtime_control_api_and_kill_switch(tmp_path) -> None:
    app = create_app(f"sqlite+pysqlite:///{tmp_path / 'control.db'}")
    client = TestClient(app)
    payload = client.get("/api/v1/runtime/control").json()
    assert payload["mode_requested"] == "PAPER"
    assert payload["runtime_status"] == "STOPPED"
    response = client.post("/runtime/kill-switch", data={"active": "true"}, follow_redirects=False)
    assert response.status_code == 303
    payload = client.get("/api/v1/runtime/control").json()
    assert payload["kill_switch_active"] is True
    assert payload["kill_switch_state"] == "ACTIVE"
    assert payload["last_error"] == "KILL_SWITCH_ACTIVE"


def test_dashboard_requested_mode_updates_only_when_stopped(tmp_path) -> None:
    app = create_app(f"sqlite+pysqlite:///{tmp_path / 'mode.db'}")
    client = TestClient(app)
    response = client.post("/runtime/mode", data={"mode": "PAPER"}, follow_redirects=False)
    assert response.status_code == 303
    payload = client.get("/api/v1/runtime/control").json()
    assert payload["mode_requested"] == "PAPER"
    assert payload["mode_running"] is None

def test_dashboard_renders_kill_switch_state_and_no_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BINANCE_API_KEY", "SECRET_KEY_SHOULD_NOT_RENDER")
    monkeypatch.setenv("BINANCE_API_SECRET", "SECRET_VALUE_SHOULD_NOT_RENDER")
    app = create_app(f"sqlite+pysqlite:///{tmp_path / 'html.db'}")
    html = TestClient(app).get("/").text
    assert "Kill Switch INACTIVE" in html
    assert "NOT LIVE-READY" in html
    assert "SECRET_KEY_SHOULD_NOT_RENDER" not in html
    assert "SECRET_VALUE_SHOULD_NOT_RENDER" not in html


def test_dashboard_kill_switch_survives_restart_and_audits(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'persist.db'}"
    client = TestClient(create_app(database_url))
    assert client.post("/runtime/kill-switch", data={"active": "true"}, follow_redirects=False).status_code == 303
    payload = TestClient(create_app(database_url)).get("/api/v1/runtime/control").json()
    assert payload["kill_switch_active"] is True
    engine = create_engine(database_url, future=True)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT action, success FROM runtime_control_audit_events ORDER BY id DESC LIMIT 1")).first()
    assert row == ("KILL_SWITCH_ON", 1)


def test_dashboard_paper_switch_accepted_and_live_blocked_without_readiness(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'switch.db'}"
    client = TestClient(create_app(database_url))
    assert client.post("/runtime/mode", data={"mode": "PAPER"}, follow_redirects=False).status_code == 303
    assert client.get("/api/v1/runtime/control").json()["mode_requested"] == "PAPER"
    response = client.post("/runtime/mode", data={"mode": "LIVE", "operator_acknowledged": "true"}, follow_redirects=False)
    assert response.status_code == 303
    payload = client.get("/api/v1/runtime/control").json()
    assert payload["mode_requested"] == "PAPER"
    assert "LIVE mode blocked: readiness evidence is NOT_AVAILABLE" in payload["last_error"]
    engine = create_engine(database_url, future=True)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT action, requested_mode, success, reason, operator_acknowledged FROM runtime_control_audit_events ORDER BY id DESC LIMIT 1")).first()
    assert row[0] == "MODE_SWITCH"
    assert row[1] == "LIVE"
    assert row[2] == 0
    assert "readiness evidence" in row[3]
    assert row[4] == 1
