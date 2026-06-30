from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest


def test_dashboard_backtest_rescue_disabled_sets_scoped_env_false(monkeypatch: pytest.MonkeyPatch) -> None:
    import alphaforge.dashboard.backtest_control as backtest_control
    from alphaforge.dashboard.backtest_control import DashboardBacktestRequest, run_dashboard_backtest

    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["env"] = kwargs.get("env", {})
        return subprocess.CompletedProcess(command, 1, "", "NO_HISTORICAL_DATA")

    monkeypatch.setattr(backtest_control.subprocess, "run", fake_run)
    result = run_dashboard_backtest(DashboardBacktestRequest(1, ["BTCUSDT"], "15m", 10000.0, 1, short_breakdown_rescue_enabled=False))
    assert result.short_breakdown_rescue_enabled is False
    assert "--rescue-enabled" not in seen["command"]
    assert seen["env"]["ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED"] == "false"


def test_dashboard_backtest_rescue_enabled_sets_scoped_env_true(monkeypatch: pytest.MonkeyPatch) -> None:
    import alphaforge.dashboard.backtest_control as backtest_control
    from alphaforge.dashboard.backtest_control import DashboardBacktestRequest, run_dashboard_backtest

    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["env"] = kwargs.get("env", {})
        return subprocess.CompletedProcess(command, 1, "", "NO_HISTORICAL_DATA")

    monkeypatch.setattr(backtest_control.subprocess, "run", fake_run)
    result = run_dashboard_backtest(DashboardBacktestRequest(1, ["BTCUSDT"], "15m", 10000.0, 1, short_breakdown_rescue_enabled=True))
    assert result.short_breakdown_rescue_enabled is True
    assert "--rescue-enabled" in seen["command"]
    assert seen["env"]["ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED"] == "true"


def test_dashboard_rescue_default_off_and_paper_live_not_exposed() -> None:
    from alphaforge.dashboard.backtest_control import default_form_values, parse_backtest_form
    from alphaforge.config_registry import CONFIG_REGISTRY

    request, errors = parse_backtest_form({"last_days": "30", "symbols": "BTCUSDT", "timeframe": "15m", "initial_balance": "10000", "max_symbols": "1"})
    assert errors == {}
    assert request is not None
    assert request.short_breakdown_rescue_enabled is False
    assert default_form_values()["short_breakdown_rescue_enabled"] is False
    rescue_setting = [s for s in CONFIG_REGISTRY if s.env_name == "ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED"][0]
    assert rescue_setting.applies_to == ("BACKTEST",)


def test_backtest_filter_state_marks_short_breakdown_rescue_backtest_only(tmp_path) -> None:
    import backtest_order as bo

    state = bo.build_backtest_filter_state(disabled_filters=[], source="dashboard", timestamp="2026-06-30T00:00:00Z", symbols=["BTCUSDT"], timeframe="15m", last_days=30, short_breakdown_rescue_enabled=True)
    bo.write_backtest_filter_state_artifacts(str(tmp_path), state)
    payload = json.loads((tmp_path / "backtest_filter_state.json").read_text())
    experiment = payload["experiments"]["SHORT_BREAKDOWN_RESCUE"]
    assert experiment["enabled"] is True
    assert experiment["mode"] == "BACKTEST only"
    assert experiment["default"] is False
    rows = list(csv.DictReader((tmp_path / "backtest_filter_state.csv").open()))
    assert rows[0]["experiment_SHORT_BREAKDOWN_RESCUE_enabled"] == "True"
    assert rows[0]["experiment_SHORT_BREAKDOWN_RESCUE_mode"] == "BACKTEST only"


def test_dashboard_result_renders_rescue_state_and_breakdown(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    import alphaforge.dashboard.app as dashboard_app
    from alphaforge.dashboard.app import create_app
    from alphaforge.dashboard.backtest_control import DashboardBacktestResult

    def fake_runner(request):
        return DashboardBacktestResult("COMPLETED", "last 30 days", request.symbols, request.timeframe, request.initial_balance, request.max_symbols, short_breakdown_rescue_enabled=True, baseline_accepted_count=2, rescue_accepted_count=1, baseline_net_pnl="10.0", rescue_net_pnl="3.0", baseline_plus_rescue_net_pnl="13.0", accepted_reason_breakdown={"BASELINE": 2, "HIGH_EFFECTIVE_RR_RESCUE": 1})

    monkeypatch.setattr(dashboard_app, "run_dashboard_backtest", fake_runner)
    response = TestClient(create_app(f"sqlite+pysqlite:///{tmp_path / 'rescue-render.db'}")).post("/backtest/run", data={"last_days": "30", "symbols": "BTCUSDT", "timeframe": "15m", "initial_balance": "10000", "max_symbols": "1", "short_breakdown_rescue_enabled": "true"})
    assert response.status_code == 200
    assert "SHORT_BREAKDOWN_RESCUE experiment" in response.text
    assert "enabled" in response.text
    assert "2 / 1" in response.text
    assert "10.0 / 3.0 / 13.0" in response.text
    assert "HIGH_EFFECTIVE_RR_RESCUE" in response.text
