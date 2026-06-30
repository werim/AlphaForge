from __future__ import annotations

import subprocess

import pytest


def test_backtest_form_allows_dynamic_top_volume_universe() -> None:
    from alphaforge.dashboard.backtest_control import parse_backtest_form

    request, errors = parse_backtest_form({"last_days": "30", "symbols": "  ", "timeframe": "15m", "initial_balance": "10000", "max_symbols": "20"})

    assert errors == {}
    assert request is not None
    assert request.symbols == []
    assert request.max_symbols == 20


def test_backtest_form_rejects_dynamic_universe_without_positive_max_symbols() -> None:
    from alphaforge.dashboard.backtest_control import parse_backtest_form

    missing_request, missing_errors = parse_backtest_form({"last_days": "30", "symbols": "  ", "timeframe": "15m", "initial_balance": "10000", "max_symbols": ""})
    zero_request, zero_errors = parse_backtest_form({"last_days": "30", "symbols": "  ", "timeframe": "15m", "initial_balance": "10000", "max_symbols": "0"})

    expected = "Provide at least one symbol or set MAX SYMBOLS greater than 0 for dynamic universe selection."
    assert missing_request is None
    assert missing_errors["symbols"] == expected
    assert zero_request is None
    assert zero_errors["symbols"] == expected


def test_backtest_form_allows_explicit_symbols_with_max_symbols() -> None:
    from alphaforge.dashboard.backtest_control import parse_backtest_form

    request, errors = parse_backtest_form({"last_days": "30", "symbols": "BTCUSDT, ETHUSDT", "timeframe": "15m", "initial_balance": "10000", "max_symbols": "20"})

    assert errors == {}
    assert request is not None
    assert request.symbols == ["BTCUSDT", "ETHUSDT"]
    assert request.max_symbols == 20


def test_dynamic_universe_command_omits_empty_symbols_and_passes_max_symbols(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import alphaforge.dashboard.backtest_control as backtest_control
    from alphaforge.dashboard.backtest_control import DashboardBacktestRequest, run_dashboard_backtest

    monkeypatch.setenv("ALPHAFORGE_BACKTEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(backtest_control.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "NO_HISTORICAL_DATA"))

    result = run_dashboard_backtest(DashboardBacktestRequest(last_days=1, symbols=[], timeframe="15m", initial_balance=10000.0, max_symbols=20))

    assert "--symbols" not in result.command
    assert "--max-symbols" in result.command
    assert result.command[result.command.index("--max-symbols") + 1] == "20"
    assert result.command[result.command.index("--mode") + 1] == "BACKTEST"
    assert "--paper" not in result.command
    assert "--live" not in result.command
