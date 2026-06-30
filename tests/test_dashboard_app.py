from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
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
    assert result.error_message is not None
    assert result.error_message.startswith(INSUFFICIENT_BINANCE_DATA_MESSAGE)
    assert "HistoricalDataError" in result.error_message


def test_dashboard_historical_failure_preserves_failed_symbol_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    import alphaforge.dashboard.backtest_control as backtest_control
    from alphaforge.dashboard.backtest_control import DashboardBacktestRequest, run_dashboard_backtest

    stderr = (
        "HistoricalDataError: Historical coverage ends before requested end boundary: "
        "symbol=ETHUSDT timeframe=1m requested_start=2026-05-25T00:00:00+00:00 "
        "requested_end=2026-06-24T00:00:00+00:00 expected_candles=43201 actual_candles=1500"
    )
    monkeypatch.setattr(backtest_control.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", stderr))
    request = DashboardBacktestRequest(last_days=30, symbols=["BTCUSDT", "ETHUSDT"], timeframe="1m", initial_balance=10000.0, max_symbols=2)
    result = run_dashboard_backtest(request)
    assert result.status == "FAILED"
    assert result.error_message is not None
    assert "symbol=ETHUSDT" in result.error_message
    assert "timeframe=1m" in result.error_message
    assert "expected_candles=43201" in result.error_message
    assert "actual_candles=1500" in result.error_message


def test_dashboard_failure_html_shows_detailed_reason(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import subprocess

    import alphaforge.dashboard.backtest_control as backtest_control

    stderr = "HistoricalDataError: No candles returned by Binance: symbol=BTCUSDT timeframe=1m requested_start=2026-05-25T00:00:00+00:00 requested_end=2026-06-24T00:00:00+00:00 expected_candles=43200 actual_candles=0"
    monkeypatch.setattr(backtest_control.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", stderr))
    client = TestClient(create_app(f"sqlite+pysqlite:///{tmp_path / 'detail.db'}"))
    html = client.post(
        "/backtest/run",
        data={"last_days": "30", "symbols": "BTCUSDT,ETHUSDT", "timeframe": "1m", "initial_balance": "10000", "max_symbols": "2"},
    ).text
    assert "Backtest failed closed" in html
    assert "symbol=BTCUSDT" in html
    assert "expected_candles=43200" in html
    assert "actual_candles=0" in html
    assert "Unavailable" in html


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


def test_dashboard_backtest_shows_top_rejection_reasons_and_diagnostics(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import subprocess
    import csv

    import alphaforge.dashboard.backtest_control as backtest_control
    from alphaforge.dashboard.backtest_control import DashboardBacktestRequest, run_dashboard_backtest

    monkeypatch.setenv("ALPHAFORGE_BACKTEST_OUTPUT_DIR", str(tmp_path))

    def fake_run(command, **kwargs):
        out = command[command.index("--output-dir") + 1]
        import os
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "order_backtest_summary.csv"), "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["total_candidates", "accepted_count", "rejected_count"])
            writer.writeheader(); writer.writerow({"total_candidates": "4", "accepted_count": "1", "rejected_count": "3"})
        with open(os.path.join(out, "order_lifecycle.csv"), "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["signal_id", "symbol", "lifecycle_state", "source_stage", "decision", "side", "score", "raw_rr", "rr", "effective_rr", "regime", "entry", "exit", "result", "net_pnl", "spread_pct", "spread_source"])
            writer.writeheader()
            writer.writerow({"signal_id": "s1", "symbol": "BTCUSDT", "lifecycle_state": "SIGNAL_CREATED", "source_stage": "SIGNAL_ENGINE", "decision": "PENDING", "spread_pct": "0.001", "spread_source": "ESTIMATED_BACKTEST"})
            writer.writerow({"signal_id": "s4", "symbol": "BTCUSDT", "lifecycle_state": "WAITING_ENTRY_ZONE", "source_stage": "SIGNAL_ENGINE", "decision": "ACCEPTED", "side": "LONG", "score": "8.8", "raw_rr": "2.0", "rr": "2.0", "effective_rr": "1.7", "regime": "TREND", "entry": "100", "exit": "103", "result": "SL_HIT", "net_pnl": "-1.0", "spread_pct": "0.001", "spread_source": "ESTIMATED_BACKTEST"})
        with open(os.path.join(out, "rejected_orders.csv"), "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["signal_id", "symbol", "lifecycle_state", "source", "source_stage", "reject_reason", "score", "raw_rr", "effective_rr", "expectancy", "expectancy_bucket", "min_required_score", "min_effective_rr", "spread_pct", "expected_slippage_pct", "volume_24h_usdt", "liquidity_ok", "volatility_ok", "volatility_score"])
            writer.writeheader()
            writer.writerow({"signal_id": "s1", "symbol": "BTCUSDT", "lifecycle_state": "SIGNAL_REJECTED", "source": "", "source_stage": "SIGNAL_ENGINE", "reject_reason": "LOW_SCORE", "score": "4", "raw_rr": "1.4", "effective_rr": "0.8", "expectancy": "0.1", "expectancy_bucket": "LOW", "min_required_score": "7.5", "min_effective_rr": "1.1", "spread_pct": "0.0008", "expected_slippage_pct": "0.0004", "volume_24h_usdt": "1000000", "liquidity_ok": "true", "volatility_ok": "true", "volatility_score": "1.2"})
            writer.writerow({"signal_id": "s2", "symbol": "ETHUSDT", "lifecycle_state": "SIGNAL_REJECTED", "source": "", "source_stage": "SIGNAL_ENGINE", "reject_reason": "LOW_SCORE", "score": "5", "raw_rr": "1.2", "effective_rr": "0.7", "expectancy": "0.1", "expectancy_bucket": "LOW", "min_required_score": "7.5", "min_effective_rr": "1.1", "spread_pct": "0.0008", "expected_slippage_pct": "0.0005", "volume_24h_usdt": "2000000", "liquidity_ok": "true", "volatility_ok": "true", "volatility_score": "1.5"})
            writer.writerow({"signal_id": "s3", "symbol": "ETHUSDT", "lifecycle_state": "SYMBOL_SELECTOR_REJECT", "source": "SYMBOL_SELECTOR", "source_stage": "SYMBOL_SELECTOR", "reject_reason": "LOW_LIQUIDITY", "score": "8", "raw_rr": "1.5", "effective_rr": "0.95", "expectancy": "0.2", "expectancy_bucket": "MEDIUM", "min_required_score": "7.5", "min_effective_rr": "1.1", "spread_pct": "", "expected_slippage_pct": "", "volume_24h_usdt": "", "liquidity_ok": "", "volatility_ok": "", "volatility_score": ""})
            writer.writerow({"signal_id": "s5", "symbol": "BTCUSDT", "lifecycle_state": "SIGNAL_REJECTED", "source": "", "source_stage": "SIGNAL_ENGINE", "reject_reason": "REGIME_MISMATCH", "score": "8.5", "raw_rr": "1.8", "effective_rr": "1.5", "expectancy": "0.3", "expectancy_bucket": "HIGH", "min_required_score": "7.5", "min_effective_rr": "1.1", "spread_pct": "0.0008", "expected_slippage_pct": "0.0004", "volume_24h_usdt": "1000000", "liquidity_ok": "true", "volatility_ok": "true", "volatility_score": "1.0"})

        with open(os.path.join(out, "rejected_shadow.csv"), "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["signal_id", "symbol", "lifecycle_state", "source_stage", "reject_reason", "score", "raw_rr", "effective_rr", "expectancy_bucket", "shadow_outcome", "cost_penalty", "liquidity_ok", "volatility_ok", "volatility_score"])
            writer.writeheader()
            writer.writerow({"signal_id": "s1", "symbol": "BTCUSDT", "lifecycle_state": "SIGNAL_REJECTED", "source_stage": "SIGNAL_ENGINE", "reject_reason": "LOW_SCORE", "score": "4", "raw_rr": "1.4", "effective_rr": "0.8", "expectancy_bucket": "LOW", "shadow_outcome": "WOULD_TP", "cost_penalty": "0.12", "liquidity_ok": "true", "volatility_ok": "true", "volatility_score": "1.2"})
            writer.writerow({"signal_id": "s2", "symbol": "ETHUSDT", "lifecycle_state": "SIGNAL_REJECTED", "source_stage": "SIGNAL_ENGINE", "reject_reason": "LOW_SCORE", "score": "5", "raw_rr": "1.2", "effective_rr": "0.7", "expectancy_bucket": "LOW", "shadow_outcome": "WOULD_SL", "cost_penalty": "0.13", "liquidity_ok": "true", "volatility_ok": "true", "volatility_score": "1.5"})
            writer.writerow({"signal_id": "s5", "symbol": "BTCUSDT", "lifecycle_state": "SIGNAL_REJECTED", "source_stage": "SIGNAL_ENGINE", "reject_reason": "REGIME_MISMATCH", "score": "8.5", "raw_rr": "1.8", "effective_rr": "1.5", "expectancy_bucket": "HIGH", "shadow_outcome": "WOULD_SL", "cost_penalty": "0.14", "liquidity_ok": "true", "volatility_ok": "true", "volatility_score": "1.0"})
            writer.writerow({"signal_id": "s6", "symbol": "SOLUSDT", "lifecycle_state": "SIGNAL_REJECTED", "source_stage": "SIGNAL_ENGINE", "reject_reason": "STOP_TOO_WIDE", "score": "8.2", "raw_rr": "1.7", "effective_rr": "1.4", "expectancy_bucket": "HIGH", "shadow_outcome": "WOULD_TP", "cost_penalty": "0.15", "liquidity_ok": "true", "volatility_ok": "false", "volatility_score": "2.0"})
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(backtest_control.subprocess, "run", fake_run)
    result = run_dashboard_backtest(DashboardBacktestRequest(10, ["BTCUSDT"], "15m", 10000, 1))
    assert result.top_rejection_reasons[0]["reason"] == "LOW_SCORE"
    assert result.signal_rows_count == 3
    assert result.symbol_selector_reject_count == 1
    assert result.pre_later_gate_pass_count == 1
    assert result.rejection_funnel["symbol_selector_rejects"] == 1
    assert result.rejection_funnel["signal_engine_signal_rejected"] == 3
    later = {row["reject_reason"]: row for row in result.later_gate_diagnostics}
    assert later["REGIME_MISMATCH"]["would_sl_count"] == 1
    assert later["STOP_TOO_WIDE"]["would_tp_count"] == 1
    assert result.low_score_shadow_comparison["would_tp_count"] == 1
    assert result.low_score_shadow_comparison["would_sl_count"] == 1
    assert result.execution_cost_summary["shadow_cost_penalty"]["count"] == 4
    assert "not mixed" in result.execution_cost_summary["cost_basis"]
    assert result.near_miss_rejected_signals[0]["shadow_outcome"] == "WOULD_SL"
    assert result.near_miss_rejected_signals[0]["cost_penalty"] == "0.14"
    assert result.accepted_trade_diagnostics[0]["signal_id"] == "s4"
    assert result.accepted_score_distribution["mean"] == 8.8
    assert result.accepted_effective_rr_distribution["mean"] == 1.7
    assert result.near_miss_score_distribution["count"] == 1
    assert result.backtest_rejection_rate == 0.75
    assert "ESTIMATED_BACKTEST_SPREAD" in result.execution_cost_summary["spread_label"]
    assert result.calibration_report_path and os.path.exists(result.calibration_report_path)
    assert result.calibration_summary_path and os.path.exists(result.calibration_summary_path)

    import alphaforge.dashboard.app as dashboard_app
    monkeypatch.setattr(dashboard_app, "run_dashboard_backtest", lambda _request: result)
    html = TestClient(create_app(f"sqlite+pysqlite:///{tmp_path / 'diag.db'}")).post(
        "/backtest/run",
        data={"last_days": "10", "symbols": "BTCUSDT", "timeframe": "15m", "initial_balance": "10000", "max_symbols": "1"},
    ).text
    assert "Backtest reject rate" in html
    assert "Accepted Trade Diagnostics" in html
    assert "Backtest Top Rejection Reasons" in html
    assert "LOW_SCORE" in html
    assert "LOW_LIQUIDITY" in html
    assert "Actionable signal rejects / Symbol-selector rejects / Order-lifecycle rejects" in html
    assert "Passed score/RR/expectancy before later gates" in html
    assert "LOW_SCORE Shadow Comparison" in html
    assert "WOULD_TP" in html
    assert "WOULD_SL" in html
    assert "Top Near-Miss Rejected Signals" in html
    assert "Accepted Trade Diagnostics" in html
    assert "s4" in html
    assert "BTCUSDT" in html
    assert "SL_HIT" in html
    assert "REGIME_MISMATCH" in html
    assert "0.14" in html
    assert "Signal Quality Diagnostics" in html
    assert "Later Gate Diagnostics" in html
    assert "Score Saturation Diagnostics" in html
    assert "DAILY_GLOBAL_TRADE_LIMIT Near-Miss Diagnostics" in html


def test_calibration_near_miss_uses_shadow_symbol_timestamp_side_match() -> None:
    from alphaforge.dashboard.backtest_control import _build_calibration_outputs

    lifecycle_rows = [
        {"signal_id": "a", "symbol": "BTCUSDT", "lifecycle_state": "SIGNAL_CREATED", "source_stage": "SIGNAL_ENGINE"},
        {"signal_id": "r1", "symbol": "BTCUSDT", "timestamp": "1000", "side": "LONG", "lifecycle_state": "SIGNAL_REJECTED", "source_stage": "SIGNAL_ENGINE", "reject_reason": "STOP_TOO_WIDE", "score": "8.4", "raw_rr": "1.9", "effective_rr": "1.5", "expectancy": "0.2", "min_required_score": "7.5", "min_effective_rr": "1.1"},
    ]
    rejected_rows = [lifecycle_rows[1]]
    shadow_rows = [
        {"symbol": "BTCUSDT", "timestamp": "1000", "side": "LONG", "lifecycle_state": "SIGNAL_REJECTED", "source_stage": "SIGNAL_ENGINE", "reject_reason": "STOP_TOO_WIDE", "score": "8.4", "raw_rr": "1.9", "effective_rr": "1.5", "shadow_outcome": "WOULD_TP", "cost_penalty": "0.22"},
        {"symbol": "ETHUSDT", "timestamp": "2000", "side": "SHORT", "lifecycle_state": "SIGNAL_REJECTED", "source_stage": "SIGNAL_ENGINE", "reject_reason": "STOP_TOO_WIDE", "score": "8.2", "raw_rr": "1.8", "effective_rr": "1.4", "shadow_outcome": "WOULD_SL", "cost_penalty": "0.20"},
    ]

    _, summary = _build_calibration_outputs(lifecycle_rows, rejected_rows, {"accepted_count": "0"}, shadow_rows)

    assert summary["near_miss_rejected_signals"][0]["shadow_outcome"] == "WOULD_TP"
    assert summary["near_miss_rejected_signals"][0]["cost_penalty"] == "0.22"
    stop = {row["reject_reason"]: row for row in summary["later_gate_diagnostics"]}["STOP_TOO_WIDE"]
    assert stop["would_tp_count"] == 1
    assert stop["would_sl_count"] == 1
    assert summary["execution_cost_summary"]["decision_cost_penalty"]["count"] == 0
    assert summary["execution_cost_summary"]["shadow_cost_penalty"]["count"] == 2


def test_accepted_trade_diagnostics_enriches_orders_and_close_ctx() -> None:
    from alphaforge.dashboard.backtest_control import _build_calibration_outputs

    lifecycle_rows = [
        {"signal_id": "s1", "symbol": "BTCUSDT", "timestamp": "1000", "side": "", "lifecycle_state": "SIGNAL_CREATED", "decision": "PENDING"},
        {"signal_id": "s1", "symbol": "BTCUSDT", "timestamp": "1000", "side": "", "lifecycle_state": "WAITING_ENTRY_ZONE", "decision": "ACCEPTED", "score": "8.7", "raw_rr": "2.0", "effective_rr": "1.7"},
        {"signal_id": "s1", "symbol": "BTCUSDT", "timestamp": "1000", "side": "", "lifecycle_state": "ENTRY_TRIGGERED", "decision": "ACCEPTED"},
        {"signal_id": "s1", "symbol": "BTCUSDT", "timestamp": "1000", "side": "", "lifecycle_state": "ORDER_PLACED", "decision": "ACCEPTED"},
        {"signal_id": "s1", "symbol": "BTCUSDT", "timestamp": "1000", "side": "", "lifecycle_state": "POSITION_OPENED", "decision": "ACCEPTED"},
        {"signal_id": "s1", "symbol": "BTCUSDT", "timestamp": "1000", "side": "", "lifecycle_state": "POSITION_CLOSED", "decision": "ACCEPTED", "execution_ctx": json.dumps({"close_reason": "TP_HIT", "volatility_regime": "TREND"})},
        {"signal_id": "s2", "symbol": "ETHUSDT", "lifecycle_state": "SIGNAL_REJECTED", "decision": "REJECTED"},
    ]
    backtest_orders = [{"signal_id": "s1", "symbol": "BTCUSDT", "timestamp": "1000", "side": "LONG", "entry": "100", "sl": "98", "tp": "104", "regime": "TREND"}]

    _, summary = _build_calibration_outputs(lifecycle_rows, [], {"accepted_count": "1"}, [], backtest_orders)
    diag = summary["accepted_trade_diagnostics"][0]

    assert diag["side"] == "LONG"
    assert diag["entry"] == "100"
    assert diag["sl"] == "98"
    assert diag["tp"] == "104"
    assert diag["regime"] == "TREND"
    assert diag["close_reason"] == "TP_HIT"
    assert diag["net_pnl_status"] == "NOT_EXPORTED"



def test_accepted_trade_diagnostics_completes_geometry_and_net_pnl_status() -> None:
    from alphaforge.dashboard.backtest_control import _build_calibration_outputs

    lifecycle_rows = [
        {"signal_id": "a1", "symbol": "BTCUSDT", "timestamp": "1000", "lifecycle_state": "WAITING_ENTRY_ZONE", "decision": "ACCEPTED", "score": "9.1", "effective_rr": "1.9", "expectancy_bucket": "HIGH", "cost_penalty": "0.11"},
        {"signal_id": "a1", "symbol": "BTCUSDT", "timestamp": "1000", "lifecycle_state": "POSITION_CLOSED", "decision": "ACCEPTED", "execution_ctx": json.dumps({"close_reason": "POSITION_CLOSED", "exit_price": "104", "net_pnl": "12.5"})},
    ]
    backtest_orders = [{"signal_id": "a1", "symbol": "BTCUSDT", "timestamp": "1000", "side": "LONG", "entry": "100", "sl": "98", "tp": "105", "regime": "TREND"}]

    _, summary = _build_calibration_outputs(lifecycle_rows, [], {"accepted_count": "1"}, [], backtest_orders)
    diag = summary["accepted_trade_diagnostics"][0]

    assert diag["side"] == "LONG"
    assert diag["entry"] == "100"
    assert diag["sl"] == "98"
    assert diag["tp"] == "105"
    assert diag["exit"] == "104"
    assert diag["close_reason"] == "POSITION_CLOSED"
    assert diag["expectancy_bucket"] == "HIGH"
    assert diag["decision_cost_penalty"] == "0.11"
    assert diag["net_pnl"] == "12.5"
    assert diag["net_pnl_status"] == "EXPORTED"


def test_accepted_trade_diagnostics_matches_backtest_orders_with_synthetic_signal_id() -> None:
    from alphaforge.dashboard.backtest_control import _build_calibration_outputs

    lifecycle_rows = [
        {"symbol": "BTCUSDT", "timestamp": "1000", "lifecycle_state": "WAITING_ENTRY_ZONE", "decision": "ACCEPTED", "score": "8.4", "effective_rr": "1.6", "expectancy_bucket": "MEDIUM", "decision_cost_penalty": "0.07"},
        {"symbol": "BTCUSDT", "timestamp": "1000", "lifecycle_state": "ORDER_PLACED", "decision": "ACCEPTED", "regime": "TREND"},
    ]
    backtest_orders = [{"signal_id": "BTCUSDT:1000", "symbol": "BTCUSDT", "timestamp": "1000", "side": "LONG", "entry": "100", "sl": "97", "tp": "106"}]

    _, summary = _build_calibration_outputs(lifecycle_rows, [], {"accepted_count": "1"}, [], backtest_orders)
    diag = summary["accepted_trade_diagnostics"][0]

    assert diag["signal_id"] == "BTCUSDT:1000"
    assert diag["side"] == "LONG"
    assert diag["entry"] == "100"
    assert diag["sl"] == "97"
    assert diag["tp"] == "106"
    assert diag["expectancy_bucket"] == "MEDIUM"
    assert diag["decision_cost_penalty"] == "0.07"
    assert diag["exit"] is None
    assert diag["exit_status"] == "NOT_EXPORTED"
    assert diag["net_pnl"] is None
    assert diag["net_pnl_status"] == "NOT_EXPORTED"


def test_stop_too_wide_rescue_diagnostics_reporting_only_keeps_counts() -> None:
    from alphaforge.dashboard.backtest_control import _build_calibration_outputs, _lifecycle_diagnostics

    lifecycle_rows = [
        {"signal_id": "a1", "lifecycle_state": "WAITING_ENTRY_ZONE", "decision": "ACCEPTED"},
        {"signal_id": "r1", "lifecycle_state": "SIGNAL_REJECTED", "source_stage": "SIGNAL_ENGINE", "reject_reason": "STOP_TOO_WIDE", "score": "10", "raw_rr": "2.2", "effective_rr": "1.8", "expectancy": "0.3", "min_required_score": "7.5", "min_effective_rr": "1.1"},
    ]
    rejected_rows = [lifecycle_rows[1]]
    shadow_rows = [
        {"signal_id": "r1", "lifecycle_state": "SIGNAL_REJECTED", "source_stage": "SIGNAL_ENGINE", "reject_reason": "STOP_TOO_WIDE", "score": "10", "raw_rr": "2.2", "effective_rr": "1.8", "shadow_outcome": "WOULD_TP", "volatility_score": "1.4"},
        {"signal_id": "r2", "lifecycle_state": "SIGNAL_REJECTED", "source_stage": "SIGNAL_ENGINE", "reject_reason": "STOP_TOO_WIDE", "score": "8", "raw_rr": "1.6", "effective_rr": "1.2", "shadow_outcome": "WOULD_SL", "alternate_stop_valid": "true"},
    ]

    before_counts = _lifecycle_diagnostics(lifecycle_rows, rejected_rows)["lifecycle_state_counts"]
    _, summary = _build_calibration_outputs(lifecycle_rows, rejected_rows, {"accepted_count": "1"}, shadow_rows)
    after_counts = _lifecycle_diagnostics(lifecycle_rows, rejected_rows)["lifecycle_state_counts"]
    rescue = summary["stop_too_wide_rescue_diagnostics"]

    assert summary["rejection_funnel"]["accepted_trades"] == 1
    assert rescue["mode"] == "REPORTING_ONLY"
    assert rescue["thresholds_changed"] is False
    assert rescue["accepted_trades_changed"] is False
    assert rescue["rescue_candidate_count"] == 2
    assert rescue["rescue_would_tp_count"] == 1
    assert rescue["rescue_would_sl_count"] == 1
    assert rescue["rescue_expected_effective_rr"] == 1.5
    assert before_counts == after_counts

def test_lifecycle_state_counts_include_full_backtest_path() -> None:
    from alphaforge.dashboard.backtest_control import _lifecycle_diagnostics

    lifecycle_rows = [
        {"signal_id": "s1", "lifecycle_state": "SIGNAL_CREATED"},
        {"signal_id": "s2", "lifecycle_state": "SIGNAL_REJECTED"},
        {"signal_id": "s3", "lifecycle_state": "WAITING_ENTRY_ZONE"},
        {"signal_id": "s3", "lifecycle_state": "ENTRY_TRIGGERED"},
        {"signal_id": "s3", "lifecycle_state": "ORDER_PLACED"},
        {"signal_id": "s3", "lifecycle_state": "POSITION_OPENED"},
        {"signal_id": "s3", "lifecycle_state": "POSITION_CLOSED"},
    ]

    diagnostics = _lifecycle_diagnostics(lifecycle_rows, [])
    counts = {row["value"]: row["count"] for row in diagnostics["lifecycle_state_counts"]}

    for state in ("SIGNAL_CREATED", "SIGNAL_REJECTED", "WAITING_ENTRY_ZONE", "ENTRY_TRIGGERED", "ORDER_PLACED", "POSITION_OPENED", "POSITION_CLOSED"):
        assert counts[state] >= 1


def test_later_gate_breakdown_uses_only_passed_before_later_gate_candidates() -> None:
    from alphaforge.dashboard.backtest_control import _build_calibration_outputs

    rejected_rows = [
        {"signal_id": "p1", "symbol": "BTCUSDT", "timestamp": "1000", "side": "LONG", "lifecycle_state": "SIGNAL_REJECTED", "source_stage": "SIGNAL_ENGINE", "reject_reason": "STOP_TOO_WIDE", "score": "9.4", "raw_rr": "2.0", "effective_rr": "1.5", "expectancy": "0.2", "min_required_score": "7.5", "min_effective_rr": "1.1"},
        {"signal_id": "p2", "symbol": "BTCUSDT", "timestamp": "2000", "side": "LONG", "lifecycle_state": "SIGNAL_REJECTED", "source_stage": "SIGNAL_ENGINE", "reject_reason": "REGIME_MISMATCH", "score": "8.4", "raw_rr": "1.8", "effective_rr": "1.4", "expectancy": "0.1", "min_required_score": "7.5", "min_effective_rr": "1.1"},
        {"signal_id": "f1", "symbol": "BTCUSDT", "timestamp": "3000", "side": "LONG", "lifecycle_state": "SIGNAL_REJECTED", "source_stage": "SIGNAL_ENGINE", "reject_reason": "STOP_TOO_WIDE", "score": "6.0", "raw_rr": "2.0", "effective_rr": "1.5", "expectancy": "0.2", "min_required_score": "7.5", "min_effective_rr": "1.1"},
    ]
    shadow_rows = [
        {"signal_id": "p1", "symbol": "BTCUSDT", "timestamp": "1000", "side": "LONG", "reject_reason": "STOP_TOO_WIDE", "score": "9.4", "effective_rr": "1.5", "shadow_outcome": "WOULD_SL"},
        {"signal_id": "p2", "symbol": "BTCUSDT", "timestamp": "2000", "side": "LONG", "reject_reason": "REGIME_MISMATCH", "score": "8.4", "effective_rr": "1.4", "shadow_outcome": "WOULD_TP"},
        {"signal_id": "f1", "symbol": "BTCUSDT", "timestamp": "3000", "side": "LONG", "reject_reason": "STOP_TOO_WIDE", "score": "6.0", "effective_rr": "1.5", "shadow_outcome": "WOULD_TP"},
    ]

    _, summary = _build_calibration_outputs([], rejected_rows, {"accepted_count": "0"}, shadow_rows, [])
    by_reason = {row["reject_reason"]: row for row in summary["later_gate_diagnostics"]}

    assert summary["rejection_funnel"]["passed_score_rr_expectancy"] == 2
    assert by_reason["STOP_TOO_WIDE"]["count"] == 1
    assert by_reason["STOP_TOO_WIDE"]["would_sl_count"] == 1
    assert by_reason["REGIME_MISMATCH"]["count"] == 1


def test_dashboard_unsupported_timeframe_failure_is_truthful(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import json
    import subprocess

    import alphaforge.dashboard.backtest_control as backtest_control
    from alphaforge.dashboard.backtest_control import DashboardBacktestRequest, run_dashboard_backtest

    stderr = "HistoricalDataError: UNSUPPORTED_TIMEFRAME: requested_interval=2d supported_intervals=1m,5m,15m,1h,4h,1d source_function=fetch_binance_klines_paginated"
    monkeypatch.setattr(backtest_control.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", stderr))
    monkeypatch.setenv("ALPHAFORGE_BACKTEST_OUTPUT_DIR", str(tmp_path))
    result = run_dashboard_backtest(DashboardBacktestRequest(90, ["BTCUSDT"], "1d", 10000.0, 1))
    assert result.status == "FAILED"
    assert result.error_message is not None
    assert "UNSUPPORTED_TIMEFRAME" in result.error_message
    assert "Not enough historical data" not in result.error_message
    md_path = next(tmp_path.glob("dashboard/*/backtest_run_metadata.json"))
    metadata = json.loads(md_path.read_text())
    assert metadata["requested_timeframe"] == "1d"
    assert metadata["effective_timeframe"] == "1d"
    assert metadata["failure_reason"] == "UNSUPPORTED_TIMEFRAME"
    assert metadata["disabled_optional_filters"] == []
    assert "LOW_SCORE" in metadata["enabled_optional_filters"]
    assert metadata["filter_state_applied_before_failure"] is True


def test_failed_backtest_html_does_not_substitute_stale_paper_diagnostics(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import subprocess

    import alphaforge.dashboard.backtest_control as backtest_control

    stderr = "HistoricalDataError: UNSUPPORTED_TIMEFRAME: requested_interval=2d supported_intervals=1m,5m,15m,1h,4h,1d source_function=fetch_binance_klines_paginated"
    monkeypatch.setattr(backtest_control.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", stderr))
    monkeypatch.setenv("ALPHAFORGE_BACKTEST_OUTPUT_DIR", str(tmp_path))
    client = TestClient(create_app(f"sqlite+pysqlite:///{tmp_path / 'failed.db'}"))
    html = client.post(
        "/backtest/run",
        data={"last_days": "90", "symbols": "BTCUSDT", "timeframe": "1d", "initial_balance": "10000", "max_symbols": "1"},
    ).text
    assert "SELECTED_BACKTEST_UNAVAILABLE_DUE_TO_FAILURE" in html
    assert "Accepted Trade Diagnostics" not in html
    assert "Backtest Top Rejection Reasons" not in html
    assert "Signal Quality Diagnostics" not in html
    assert "Later Gate Diagnostics" not in html
    assert "LOW_SCORE Shadow Comparison" not in html
    assert "Top Near-Miss Rejected Signals" not in html
    assert "Score Saturation Diagnostics" not in html
    assert "DAILY_GLOBAL_TRADE_LIMIT Near-Miss Diagnostics" not in html
