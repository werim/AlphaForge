import csv
import json
from pathlib import Path

import pytest

from alphaforge.dashboard import backtest_control as bc


def _write_profile_artifacts(profile_dir: Path, disabled: list[str], accepted: int, net: float) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "total_candidates": str(accepted + 2),
        "accepted_count": str(accepted),
        "rejected_count": "2",
        "tp_hits": str(max(0, accepted - 1)),
        "sl_hits": "1" if accepted else "0",
        "open_at_end": "0",
        "total_net_pnl_usdt": str(net),
        "total_pnl_pct": str(net / 10000.0),
        "last_days": "30",
    }
    with (profile_dir / "order_backtest_summary.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(summary.keys()))
        writer.writeheader(); writer.writerow(summary)
    lifecycle_rows = []
    for i in range(accepted):
        lifecycle_rows.append({"symbol": "BTCUSDT", "side": "LONG", "regime": "TREND", "score": "10", "effective_rr": "2.0", "raw_rr": "2.2", "spread_pct": "0.001", "expected_slippage_pct": "0.001", "stop_distance_pct": "0.01", "close_reason": "TP_HIT" if i < accepted - 1 else "SL_HIT", "net_pnl_usdt": str(net / max(1, accepted)), "cost_penalty": "0.1"})
    with (profile_dir / "order_lifecycle.csv").open("w", newline="") as fh:
        fields = list(lifecycle_rows[0].keys()) if lifecycle_rows else ["symbol"]
        writer = csv.DictWriter(fh, fieldnames=fields); writer.writeheader(); writer.writerows(lifecycle_rows)
    rejected_rows = [{"reject_reason": "NEGATIVE_EXPECTANCY", "lifecycle_state": "SIGNAL_REJECTED", "effective_rr": "0.5", "score": "4"}, {"reject_reason": "LOW_SCORE", "lifecycle_state": "SIGNAL_REJECTED", "effective_rr": "1.0", "score": "5"}]
    with (profile_dir / "rejected_orders.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rejected_rows[0].keys())); writer.writeheader(); writer.writerows(rejected_rows)
    state = {"filter_profile": "ALL_OFF" if disabled else "DEFAULT", "enabled_filters": [], "disabled_filters": disabled, "hard_safety_gates": [{"filter_name": "NEGATIVE_EXPECTANCY"}]}
    (profile_dir / "backtest_filter_state.json").write_text(json.dumps(state))


def test_profile_comparison_runner_writes_real_metrics_and_leaderboard(monkeypatch, tmp_path):
    class BacktestCfg:
        output_dir = str(tmp_path)
        export_config_snapshot = False
        top_n = 1
        timeframe = "1h"
    class Cfg:
        backtest = BacktestCfg()
    monkeypatch.setattr(bc, "load_config_from_env", lambda: Cfg())
    monkeypatch.setattr(bc, "config_snapshot", lambda mode: {})
    monkeypatch.setattr(bc, "canonical_utc_timestamp", lambda: "2026-06-30T00:00:00Z")

    seen_commands = []

    def fake_run(command, cwd, text, capture_output, timeout, check, env):
        seen_commands.append(list(command))
        profile_dir = Path(command[command.index("--output-dir") + 1])
        disabled = [command[i + 1] for i, arg in enumerate(command) if arg == "--disable-backtest-filter"]
        accepted = 4 + len(disabled)
        net = 40.0 - len(disabled) * 3.0
        _write_profile_artifacts(profile_dir, disabled, accepted, net)
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(bc.subprocess, "run", fake_run)
    request = bc.DashboardBacktestRequest(30, ["BTCUSDT"], "1h", 10000.0, 1, filter_switches={"LOW_SCORE": False}, run_profile_comparison=True)
    result = bc.run_dashboard_backtest(request)

    assert result.status == "COMPLETED"
    starts = {cmd[cmd.index("--start") + 1] for cmd in seen_commands}
    ends = {cmd[cmd.index("--end") + 1] for cmd in seen_commands}
    assert len(starts) == 1 and len(ends) == 1
    by_profile = {Path(cmd[cmd.index("--output-dir") + 1]).name: cmd for cmd in seen_commands}
    assert "--disable-backtest-filter" not in by_profile["DEFAULT_FILTERS"]
    assert "--disable-backtest-filter" in by_profile["CUSTOM_CURRENT_UI"]
    assert all(cmd[cmd.index("--mode") + 1] == "BACKTEST" for cmd in seen_commands)
    comparison = json.loads(Path(result.filter_profile_comparison_path).read_text())
    assert comparison["comparison_mode"] is True
    for name in ["DEFAULT_FILTERS", "ALL_FILTERS_OFF", "STRICT_FILTERS", "CUSTOM_CURRENT_UI"]:
        assert comparison["profiles"][name]["accepted_trades"] is not None
        assert "final_objective_score" in comparison["profiles"][name]["objective_score"]
        assert comparison["profiles"][name]["bucket_diagnostics"]["symbol"]
    assert "FILTERS_OFF_STRESS_TEST" in comparison["profiles"]["ALL_FILTERS_OFF"]["warnings"]
    assert any("NEGATIVE_EXPECTANCY" in str(g) for g in comparison["profiles"]["ALL_FILTERS_OFF"]["hard_safety_gates"])
    leaderboard = json.loads(Path(result.profile_leaderboard_path).read_text())["leaderboard"]
    assert {"raw_net_pnl_rank", "objective_score_rank"}.issubset(leaderboard[0])


def test_profile_comparison_checkbox_parse_and_default_single_profile_unchanged():
    parsed, errors = bc.parse_backtest_form({"last_days": "30", "symbols": "BTCUSDT", "timeframe": "1h", "initial_balance": "10000", "max_symbols": "1", "run_profile_comparison": "on"})
    assert not errors
    assert parsed.run_profile_comparison is True
    assert bc.default_form_values()["run_profile_comparison"] is False


def test_dashboard_renders_profile_comparison_warning(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    import alphaforge.dashboard.app as dashboard_app
    from alphaforge.dashboard.app import create_app

    def fake_runner(request):
        return bc.DashboardBacktestResult(
            "COMPLETED",
            "last 30 days",
            request.symbols,
            request.timeframe,
            request.initial_balance,
            request.max_symbols,
            profile_leaderboard=[{
                "profile_name": "ALL_FILTERS_OFF",
                "raw_net_pnl_rank": 1,
                "objective_score_rank": 2,
                "raw_net_pnl": "10",
                "final_objective_score": "1",
                "accepted_trades": 9,
                "win_count": 3,
                "loss_count": 6,
                "open_count": 0,
                "avg_trades_per_day": 3,
                "score_10_tp_count": 1,
                "score_10_sl_count": 2,
                "warnings": ["FILTERS_OFF_STRESS_TEST"],
            }],
            filter_profile_comparison_path="comparison.json",
            profile_leaderboard_path="leaderboard.json",
        )

    monkeypatch.setattr(dashboard_app, "run_dashboard_backtest", fake_runner)
    response = TestClient(create_app(f"sqlite+pysqlite:///{tmp_path / 'profile-render.db'}")).post("/backtest/run", data={"last_days": "30", "symbols": "BTCUSDT", "timeframe": "1h", "initial_balance": "10000", "max_symbols": "1", "run_profile_comparison": "true"})
    assert response.status_code == 200
    assert "Backtest Profile Comparison" in response.text
    assert "Diagnostic stress test, not strategy performance" in response.text
    assert "FILTERS_OFF_STRESS_TEST" in response.text


def test_selected_default_profile_artifact_schema_20260630_loads_main_panel(tmp_path):
    run_dir = tmp_path / "20260630T164308Z"
    default_dir = run_dir / "profiles" / "DEFAULT_FILTERS"
    all_off_dir = run_dir / "profiles" / "ALL_FILTERS_OFF"
    default_dir.mkdir(parents=True)
    all_off_dir.mkdir(parents=True)
    (run_dir / "backtest_run_metadata.json").write_text(json.dumps({"requested_last_n_days": 90, "effective_start": 0, "effective_end": 7776000000}))
    leaderboard_rows = [
        {"profile_name": "DEFAULT_FILTERS", "raw_net_pnl": "3.9563009274390843", "final_objective_score": "1", "raw_net_pnl_rank": "2", "objective_score_rank": "1", "accepted_trades": "10", "win_count": "5", "loss_count": "5", "open_count": "0", "avg_trades_per_day": "10.0", "score_10_tp_count": "0", "score_10_sl_count": "0", "warnings": '["OVERTRADE_RISK"]'},
        {"profile_name": "ALL_FILTERS_OFF", "raw_net_pnl": "1", "final_objective_score": "-1", "raw_net_pnl_rank": "1", "objective_score_rank": "2", "accepted_trades": "390", "win_count": "195", "loss_count": "195", "open_count": "0", "avg_trades_per_day": "390.0", "score_10_tp_count": "0", "score_10_sl_count": "0", "warnings": '["FILTERS_OFF_STRESS_TEST", "OVERTRADE_RISK"]'},
    ]
    with (run_dir / "backtest_profile_leaderboard.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(leaderboard_rows[0].keys()))
        writer.writeheader(); writer.writerows(leaderboard_rows)
    (run_dir / "backtest_profile_leaderboard.json").write_text(json.dumps({"leaderboard": leaderboard_rows}))
    summary = {
        "total_candidates": "1076", "accepted_count": "10", "rejected_count": "1066", "total_rejected": "1066",
        "rejection_rate": "0.990706", "tp_hits": "5", "sl_hits": "5", "open_at_end": "0",
        "win_rate": "0.5", "total_net_pnl_usdt": "3.9563009274390843",
        "baseline_accepted_trades": "9", "rescue_accepted_count": "1",
        "baseline_net_pnl": "4.227490671865697", "rescue_accepted_net_pnl": "-0.2711897444266122",
        "baseline_plus_rescue_net_pnl": "3.9563009274390843", "short_breakdown_rescue_enabled": "True",
        "accepted_reason_breakdown": json.dumps({"BASELINE": 36, "SHORT_BREAKDOWN_RESCUE": 4}),
        "rejection_counts": json.dumps({"LOW_SCORE": 703, "RR_TOO_LOW": 4, "STOP_TOO_WIDE": 130, "TOO_CHOPPY": 206, "WEAK_TREND_AND_NO_RANGE_EDGE": 23}),
    }
    with (default_dir / "order_backtest_summary.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(summary.keys()))
        writer.writeheader(); writer.writerow(summary)
    orders = []
    for i in range(10):
        orders.append({"signal_id": f"s{i}", "symbol": "BTCUSDT", "side": "LONG", "score": str(8 + i / 10), "effective_rr": str(1.2 + i / 10), "raw_rr": "1.8", "accepted_reason": "BASELINE" if i < 9 else "SHORT_BREAKDOWN_RESCUE", "close_reason": "TP_HIT" if i < 5 else "SL_HIT", "net_pnl_usdt": "0.1"})
    with (default_dir / "backtest_orders.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(orders[0].keys()))
        writer.writeheader(); writer.writerows(orders)
    all_off_orders = [{**orders[i % len(orders)], "signal_id": f"all{i}", "accepted_reason": "BASELINE" if i < 36 else "SHORT_BREAKDOWN_RESCUE"} for i in range(40)]
    with (all_off_dir / "backtest_orders.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(all_off_orders[0].keys()))
        writer.writeheader(); writer.writerows(all_off_orders)
    rejected = (
        [{"reject_reason": "LOW_SCORE", "lifecycle_state": "SIGNAL_REJECTED", "score": "4", "effective_rr": "0.7"}] * 703
        + [{"reject_reason": "TOO_CHOPPY", "lifecycle_state": "SIGNAL_REJECTED", "score": "6", "effective_rr": "1.0"}] * 206
        + [{"reject_reason": "STOP_TOO_WIDE", "lifecycle_state": "SIGNAL_REJECTED", "score": "7", "effective_rr": "1.1"}] * 130
        + [{"reject_reason": "WEAK_TREND_AND_NO_RANGE_EDGE", "lifecycle_state": "SIGNAL_REJECTED", "score": "7", "effective_rr": "1.2"}] * 23
        + [{"reject_reason": "RR_TOO_LOW", "lifecycle_state": "SIGNAL_REJECTED", "score": "8", "effective_rr": "0.9"}] * 4
    )
    with (default_dir / "rejected_orders.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rejected[0].keys()))
        writer.writeheader(); writer.writerows(rejected)
    calibration = {"accepted_trade_diagnostics": [{"signal_id": "s0", "symbol": "BTCUSDT", "score": "8", "effective_rr": "1.2"}], "accepted_score_distribution": {"count": 10}, "accepted_effective_rr_distribution": {"count": 10}}
    (default_dir / "lifecycle_calibration_summary.json").write_text(json.dumps(calibration))
    (default_dir / "backtest_filter_state.json").write_text(json.dumps({"filter_profile": "DEFAULT_FILTERS", "enabled_filters": ["LOW_SCORE"], "disabled_filters": [], "hard_safety_gates": []}))
    (default_dir / "signal_quality_summary.json").write_text(json.dumps({"high_effective_rr_missed_alpha": []}))
    with (default_dir / "rejected_shadow.csv").open("w", newline="") as fh:
        shadow_rows = [
            {"signal_id": "stp1", "symbol": "BTCUSDT", "side": "LONG", "regime": "TREND", "reject_reason": "STOP_TOO_WIDE", "score": "9.6", "effective_rr": "2.1", "shadow_outcome": "WOULD_TP"},
            {"signal_id": "stp2", "symbol": "BTCUSDT", "side": "LONG", "regime": "TREND", "reject_reason": "STOP_TOO_WIDE", "score": "9.7", "effective_rr": "2.2", "shadow_outcome": "WOULD_SL"},
            {"signal_id": "stp3", "symbol": "ETHUSDT", "side": "SHORT", "regime": "CHOP", "reject_reason": "STOP_TOO_WIDE", "score": "8.0", "effective_rr": "1.8", "shadow_outcome": "WOULD_TP"},
        ]
        writer = csv.DictWriter(fh, fieldnames=list(shadow_rows[0].keys()))
        writer.writeheader(); writer.writerows(shadow_rows)
    with (default_dir / "rejected_shadow_summary.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["reject_reason", "count"])
        writer.writeheader(); writer.writerow({"reject_reason": "LOW_SCORE", "count": "703"})

    result = bc.DashboardBacktestResult("COMPLETED", "last 90 days", ["BTCUSDT"], "1d", 10000, 1, profile_leaderboard=json.loads((run_dir / "backtest_profile_leaderboard.json").read_text())["leaderboard"])
    bc._apply_backtest_artifact_model(result, run_dir, window_days=90)

    assert result.selected_profile_name == "DEFAULT_FILTERS"
    assert result.selected_profile_dir == str(default_dir)
    assert result.accepted_trades == 10
    assert result.rejected_signals == 1066
    assert result.backtest_rejection_rate == pytest.approx(0.990706, rel=1e-6)
    assert (result.win_count, result.loss_count, result.open_count) == (5, 5, 0)
    assert result.net_pnl == "3.9563009274390843"
    assert result.baseline_accepted_count == 9
    assert result.rescue_accepted_count == 1
    assert result.baseline_net_pnl == "4.227490671865697"
    assert result.rescue_net_pnl == "-0.2711897444266122"
    assert result.baseline_plus_rescue_net_pnl == "3.9563009274390843"
    assert result.accepted_reason_breakdown == {"BASELINE": 9, "SHORT_BREAKDOWN_RESCUE": 1}
    assert result.accepted_trade_diagnostics
    assert result.accepted_score_distribution["count"] == 10
    assert result.accepted_effective_rr_distribution["count"] == 10
    assert {r["reason"]: r["count"] for r in result.top_rejection_reasons} == {"LOW_SCORE": 703, "TOO_CHOPPY": 206, "STOP_TOO_WIDE": 130, "WEAK_TREND_AND_NO_RANGE_EDGE": 23, "RR_TOO_LOW": 4}
    by_profile = {row["profile_name"]: row for row in result.profile_leaderboard}
    assert by_profile["DEFAULT_FILTERS"]["avg_trades_per_day"] == pytest.approx(10 / 90)
    assert by_profile["ALL_FILTERS_OFF"]["avg_trades_per_day"] == pytest.approx(390 / 90)
    assert "OVERTRADE_RISK" not in by_profile["DEFAULT_FILTERS"]["warnings"]
    assert "FILTERS_OFF_STRESS_TEST" in by_profile["ALL_FILTERS_OFF"]["warnings"]
    assert result.selected_profile_name != "ALL_FILTERS_OFF"
    assert result.accepted_reason_breakdown != {"BASELINE": 36, "SHORT_BREAKDOWN_RESCUE": 4}
    stop_diag = result.signal_quality_diagnostics["stop_too_wide_recoverable_candidates"]
    assert stop_diag["decision_logic_changed"] is False
    highlighted = stop_diag["highlighted_candidates"]
    assert highlighted
    assert {row["shadow_outcome_bucket"] for row in highlighted} == {"would_sl", "would_tp"}
    assert all(row["symbol"] == "BTCUSDT" for row in highlighted)


def _write_csv(path, rows, fieldnames=None):
    import csv
    fieldnames = fieldnames or sorted({k for row in rows for k in row})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_profile_comparison_uses_summary_zero_not_lifecycle_event_count(tmp_path):
    from alphaforge.dashboard import backtest_control as bc
    profile_dir = tmp_path / 'DEFAULT_FILTERS'
    summary_fields = ['total_candidates','accepted_count','total_orders','triggered_orders','tp_hits','sl_hits','open_at_end','total_net_pnl_usdt','rejected_count']
    _write_csv(profile_dir / 'order_backtest_summary.csv', [{
        'total_candidates':'7180','accepted_count':'0','total_orders':'0','triggered_orders':'0','tp_hits':'0','sl_hits':'0','open_at_end':'0','total_net_pnl_usdt':'0','rejected_count':'225'
    }], summary_fields)
    lifecycle_rows = []
    for i in range(12000):
        lifecycle_rows.append({'signal_id':f's{i}','timestamp':str(i),'symbol':'BTCUSDT','side':'LONG','lifecycle_state':'SIGNAL_CREATED','decision':'CREATED','effective_rr':'2.0'})
    lifecycle_rows += [
        {'signal_id':'rej1','timestamp':'1','symbol':'ETHUSDT','side':'LONG','lifecycle_state':'SIGNAL_REJECTED','decision':'REJECTED','reject_reason':'LOW_SCORE','effective_rr':'1.8'},
        {'signal_id':'rej2','timestamp':'2','symbol':'SOLUSDT','side':'LONG','lifecycle_state':'SYMBOL_REJECTED','decision':'REJECTED','reject_reason':'SYMBOL_SELECTOR','effective_rr':'1.9'},
        {'signal_id':'rej3','timestamp':'3','symbol':'BNBUSDT','side':'LONG','lifecycle_state':'ORDER_REJECTED','decision':'REJECTED','reject_reason':'HIGH_SPREAD','effective_rr':'2.1'},
    ]
    _write_csv(profile_dir / 'order_lifecycle.csv', lifecycle_rows)
    _write_csv(profile_dir / 'rejected_orders.csv', [{'reject_reason':'LOW_SCORE','lifecycle_state':'SIGNAL_REJECTED','effective_rr':'1.8'}])

    row = bc._comparison_metrics('DEFAULT_FILTERS', profile_dir, 10000, window_days=30)

    assert row['accepted_trades'] == 0
    assert row['win_count'] == row['loss_count'] == row['open_count'] == 0
    assert row['avg_trades_per_day'] == 0
    assert row['objective_score']['raw_net_pnl'] == 0
    assert row['accepted_effective_rr_distribution']['count'] == 0
    assert 'OVERTRADE_RISK' not in row['warnings']
    assert {'NO_EXECUTED_TRADES', 'NO_ACCEPTED_TRADES'}.issubset(row['warnings'])
    assert row['lifecycle_event_count'] == 12003


def test_profile_comparison_counts_all_filters_off_executed_summary_and_orders(tmp_path):
    from alphaforge.dashboard import backtest_control as bc
    profile_dir = tmp_path / 'ALL_FILTERS_OFF'
    _write_csv(profile_dir / 'order_backtest_summary.csv', [{
        'accepted_count':'23','tp_hits':'10','sl_hits':'9','open_at_end':'4','total_net_pnl_usdt':'12.5','rejected_count':'0'
    }])
    orders = []
    for i in range(23):
        close = 'TP_HIT' if i < 10 else ('SL_HIT' if i < 19 else 'OPEN_AT_END')
        orders.append({'signal_id':f'o{i}','symbol':'BTCUSDT','side':'LONG','effective_rr':str(1.5+i/100),'score':'8','close_reason':close,'net_pnl_usdt':'1' if close == 'TP_HIT' else '-1'})
    _write_csv(profile_dir / 'backtest_orders.csv', orders)
    _write_csv(profile_dir / 'order_lifecycle.csv', [{'signal_id':'created','lifecycle_state':'SIGNAL_CREATED','decision':'CREATED','effective_rr':'9'}])
    _write_csv(profile_dir / 'rejected_orders.csv', [])

    row = bc._comparison_metrics('ALL_FILTERS_OFF', profile_dir, 10000, window_days=30)

    assert row['accepted_trades'] == 23
    assert (row['win_count'], row['loss_count'], row['open_count']) == (10, 9, 4)
    assert row['objective_score']['raw_net_pnl'] == 12.5
    assert row['accepted_effective_rr_distribution']['count'] == 23


def test_leaderboard_ranking_prefers_executed_profiles_over_no_trade_zero_pnl(tmp_path):
    from alphaforge.dashboard import backtest_control as bc
    no_trade = tmp_path / 'DEFAULT_FILTERS'
    trade = tmp_path / 'ALL_FILTERS_OFF'
    _write_csv(no_trade / 'order_backtest_summary.csv', [{'accepted_count':'0','total_net_pnl_usdt':'0','tp_hits':'0','sl_hits':'0','open_at_end':'0'}])
    _write_csv(no_trade / 'order_lifecycle.csv', [{'signal_id':'s','lifecycle_state':'SIGNAL_CREATED','decision':'CREATED'}])
    _write_csv(no_trade / 'rejected_orders.csv', [])
    _write_csv(trade / 'order_backtest_summary.csv', [{'accepted_count':'23','total_net_pnl_usdt':'-1','tp_hits':'11','sl_hits':'12','open_at_end':'0'}])
    _write_csv(trade / 'backtest_orders.csv', [{'signal_id':str(i),'effective_rr':'2','close_reason':'TP_HIT' if i < 11 else 'SL_HIT','net_pnl_usdt':'-0.05'} for i in range(23)])
    _write_csv(trade / 'order_lifecycle.csv', [])
    _write_csv(trade / 'rejected_orders.csv', [])
    rows = [bc._comparison_metrics('DEFAULT_FILTERS', no_trade, 10000, window_days=30), bc._comparison_metrics('ALL_FILTERS_OFF', trade, 10000, window_days=30)]
    ranked = sorted(rows, key=lambda r: (int(r.get('accepted_trades') or 0) > 0, r.get('objective_score', {}).get('final_objective_score', 0.0)), reverse=True)
    assert ranked[0]['profile_name'] == 'ALL_FILTERS_OFF'


def test_safe_subprocess_timeout_rejects_non_positive_values():
    assert bc._safe_subprocess_timeout(1) == 1.0
    with pytest.raises(ValueError):
        bc._safe_subprocess_timeout(0)
    with pytest.raises(ValueError):
        bc._safe_subprocess_timeout(-24745.094)


def test_profile_timeout_returns_partial_and_preserves_completed_profiles(monkeypatch, tmp_path):
    class BacktestCfg:
        output_dir = str(tmp_path)
        export_config_snapshot = False
        top_n = 1
        timeframe = "1h"
    class Cfg:
        backtest = BacktestCfg()
    monkeypatch.setattr(bc, "load_config_from_env", lambda: Cfg())
    monkeypatch.setattr(bc, "canonical_utc_timestamp", lambda: "2026-06-30T21:45:29Z")
    monkeypatch.setattr(bc, "DASHBOARD_BACKTEST_SUBPROCESS_TIMEOUT_SECONDS", 7)

    def fake_run(command, cwd, text, capture_output, timeout, check, env):
        assert timeout > 0
        profile_dir = Path(command[command.index("--output-dir") + 1])
        profile = profile_dir.name
        if profile == "ALL_FILTERS_OFF":
            raise bc.subprocess.TimeoutExpired(command, timeout)
        disabled = [command[i + 1] for i, arg in enumerate(command) if arg == "--disable-backtest-filter"]
        _write_profile_artifacts(profile_dir, disabled, accepted=3, net=12.0)
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(bc.subprocess, "run", fake_run)
    request = bc.DashboardBacktestRequest(30, ["BTCUSDT"], "1h", 10000.0, 1, run_profile_comparison=True)
    result = bc.run_dashboard_backtest(request)

    assert result.status == "PARTIAL"
    assert "Profile ALL_FILTERS_OFF timed out" in result.error_message
    assert result.selected_profile_name == "DEFAULT_FILTERS"
    assert result.accepted_trades == 3
    comparison = json.loads(Path(result.filter_profile_comparison_path).read_text())
    assert comparison["status"] == "PARTIAL"
    assert comparison["profiles"]["ALL_FILTERS_OFF"]["status"] == "TIMEOUT"
    assert comparison["profiles"]["DEFAULT_FILTERS"]["status"] == "COMPLETED"
    assert Path(tmp_path / "dashboard" / "20260630T214529Z" / "profiles" / "ALL_FILTERS_OFF" / "backtest_profile_metadata.json").exists()
    by_profile = {row["profile_name"]: row for row in result.profile_leaderboard}
    assert by_profile["ALL_FILTERS_OFF"]["status"] == "TIMEOUT"
    assert by_profile["DEFAULT_FILTERS"]["accepted_trades"] == 3


def test_dashboard_displays_partial_profile_timeout_without_500(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    import alphaforge.dashboard.app as dashboard_app
    from alphaforge.dashboard.app import create_app

    def fake_runner(request):
        return bc.DashboardBacktestResult(
            "PARTIAL",
            "last 30 days",
            request.symbols,
            request.timeframe,
            request.initial_balance,
            request.max_symbols,
            error_message="Profile ALL_FILTERS_OFF timed out. Completed profiles are still available.",
            accepted_trades=2,
            profile_leaderboard=[
                {"profile_name": "DEFAULT_FILTERS", "status": "COMPLETED", "raw_net_pnl_rank": 1, "objective_score_rank": 1, "raw_net_pnl": "10", "final_objective_score": "9", "accepted_trades": 2, "win_count": 1, "loss_count": 1, "open_count": 0, "avg_trades_per_day": 0.1, "score_10_tp_count": 0, "score_10_sl_count": 0, "warnings": []},
                {"profile_name": "ALL_FILTERS_OFF", "status": "TIMEOUT", "raw_net_pnl_rank": 2, "objective_score_rank": 2, "raw_net_pnl": 0, "final_objective_score": -1000000, "accepted_trades": None, "win_count": None, "loss_count": None, "open_count": None, "avg_trades_per_day": None, "score_10_tp_count": None, "score_10_sl_count": None, "warnings": ["PROFILE_TIMEOUT"]},
            ],
        )

    monkeypatch.setattr(dashboard_app, "run_dashboard_backtest", fake_runner)
    response = TestClient(create_app(f"sqlite+pysqlite:///{tmp_path / 'partial.db'}")).post("/backtest/run", data={"last_days": "30", "symbols": "BTCUSDT", "timeframe": "1h", "initial_balance": "10000", "max_symbols": "1", "run_profile_comparison": "true"})
    assert response.status_code == 200
    assert "Profile ALL_FILTERS_OFF timed out" in response.text
    assert "DEFAULT_FILTERS" in response.text
    assert "TIMEOUT" in response.text


def test_profile_comparison_required_zero_summary_with_12228_lifecycle_rows(tmp_path):
    profile_dir = tmp_path / "DEFAULT_FILTERS"
    _write_csv(profile_dir / "order_backtest_summary.csv", [{"accepted_count": "0", "tp_hits": "0", "sl_hits": "0", "open_at_end": "0", "total_net_pnl_usdt": "0", "rejected_count": "1058"}])
    _write_csv(profile_dir / "order_lifecycle.csv", [
        {"signal_id": f"s{i}", "lifecycle_state": "SIGNAL_CREATED", "decision": "CREATED", "score": "9.1", "effective_rr": "2.4"}
        for i in range(12228)
    ])
    _write_csv(profile_dir / "rejected_orders.csv", [{"reject_reason": "LOW_SCORE", "lifecycle_state": "SIGNAL_REJECTED", "score": "4", "effective_rr": "1.0"}])

    row = bc._comparison_metrics("DEFAULT_FILTERS", profile_dir, 10000, window_days=30)

    assert row["accepted_trades"] == 0
    assert row["avg_trades_per_day"] == 0
    assert row["lifecycle_event_count"] == 12228
    assert "OVERTRADE_RISK" not in row["warnings"]
    assert "NO_EXECUTED_TRADES" in row["warnings"]


def test_profile_comparison_required_all_filters_off_exact_executed_values(tmp_path):
    profile_dir = tmp_path / "ALL_FILTERS_OFF"
    net = "-5.977496714410623"
    _write_csv(profile_dir / "order_backtest_summary.csv", [{"accepted_count": "9", "tp_hits": "2", "sl_hits": "7", "open_at_end": "0", "total_net_pnl_usdt": net, "rejected_count": "0"}])
    orders = []
    for i in range(9):
        orders.append({"signal_id": f"o{i}", "close_reason": "TP_HIT" if i < 2 else "SL_HIT", "score": "8.5", "effective_rr": "1.7", "net_pnl_usdt": "1"})
    _write_csv(profile_dir / "backtest_orders.csv", orders)
    _write_csv(profile_dir / "order_lifecycle.csv", [{"signal_id": f"e{i}", "lifecycle_state": "SIGNAL_CREATED", "score": "10", "effective_rr": "9"} for i in range(12228)])
    _write_csv(profile_dir / "rejected_orders.csv", [])

    row = bc._comparison_metrics("ALL_FILTERS_OFF", profile_dir, 10000, window_days=30)

    assert row["accepted_trades"] == 9
    assert (row["win_count"], row["loss_count"], row["open_count"]) == (2, 7, 0)
    assert row["net_pnl"] == pytest.approx(-5.977496714410623)
    assert row["objective_score"]["raw_net_pnl"] == pytest.approx(-5.977496714410623)


def test_guardrail_attribution_and_canonical_gate_funnel_from_existing_sources(tmp_path):
    run_dir = tmp_path / "run"
    default_dir = run_dir / "profiles" / "DEFAULT_FILTERS"
    default_dir.mkdir(parents=True)
    _write_csv(default_dir / "order_backtest_summary.csv", [{"accepted_count": "0", "rejected_count": "1058", "tp_hits": "0", "sl_hits": "0", "open_at_end": "0", "total_net_pnl_usdt": "0"}])
    rejected = (
        [{"reject_reason": "LOW_SCORE", "lifecycle_state": "SIGNAL_REJECTED", "score": "4", "rr": "1.0", "expectancy": "0"} for _ in range(3)]
        + [{"reject_reason": "STOP_TOO_WIDE", "lifecycle_state": "SIGNAL_REJECTED", "score": "9", "rr": "2.0", "effective_rr": "1.8", "expectancy": "0.2", "source_stage": "SIGNAL_ENGINE", "symbol": "BTCUSDT"} for _ in range(2)]
        + [{"reject_reason": "RR_TOO_LOW", "lifecycle_state": "SIGNAL_REJECTED", "score": "8", "rr": "1.4", "effective_rr": "1.1", "expectancy": "0.1", "source_stage": "SIGNAL_ENGINE", "symbol": "ETHUSDT"}]
    )
    _write_csv(default_dir / "rejected_orders.csv", rejected)
    _write_csv(default_dir / "order_lifecycle.csv", [{"signal_id": "s", "lifecycle_state": "SIGNAL_CREATED"}])
    calibration = {"rejection_funnel": {"passed_score_rr_expectancy": 3, "rejected_by_later_gates": 3, "accepted_trades": 0}, "accepted_score_distribution": {"count": 0}, "accepted_effective_rr_distribution": {"count": 0}}
    (default_dir / "lifecycle_calibration_summary.json").write_text(json.dumps(calibration))

    result = bc.DashboardBacktestResult("COMPLETED", "last 30 days", ["BTCUSDT"], "1h", 10000, 1)
    bc._apply_backtest_artifact_model(result, run_dir, selected_profile_name="DEFAULT_FILTERS", window_days=30)

    assert result.guardrail_reject_breakdown["STOP_TOO_WIDE"] == 2
    assert result.guardrail_reject_breakdown["RR_TOO_LOW"] == 1
    assert result.top_guardrail_reject_reasons
    assert result.representative_guardrail_reject_examples
    by_gate = {row["gate"]: row for row in result.gate_funnel}
    assert by_gate["LOW_SCORE"]["rejected_by_gate"] == 3
    assert by_gate["STOP_TOO_WIDE"]["rejected_by_gate"] == 2


def test_dashboard_renders_guardrail_breakdown_when_source_data_exists(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    import alphaforge.dashboard.app as dashboard_app
    from alphaforge.dashboard.app import create_app

    def fake_runner(request):
        return bc.DashboardBacktestResult(
            "COMPLETED", "last 30 days", request.symbols, request.timeframe, request.initial_balance, request.max_symbols,
            guardrail_reject_breakdown={"STOP_TOO_WIDE": 2},
            top_guardrail_reject_reasons=[{"reason": "STOP_TOO_WIDE", "count": 2}],
            representative_guardrail_reject_examples=[{"symbol": "BTCUSDT", "reject_reason": "STOP_TOO_WIDE"}],
        )

    monkeypatch.setattr(dashboard_app, "run_dashboard_backtest", fake_runner)
    response = TestClient(create_app(f"sqlite+pysqlite:///{tmp_path / 'guardrails.db'}")).post("/backtest/run", data={"last_days": "30", "symbols": "BTCUSDT", "timeframe": "1h", "initial_balance": "10000", "max_symbols": "1"})
    assert response.status_code == 200
    guardrail_section = response.text.split("Strategy Quality Guardrails", 1)[1]
    assert "STOP_TOO_WIDE" in guardrail_section
    assert "Unavailable" not in guardrail_section.split("</table>", 1)[0]


def test_dashboard_main_and_profile_comparison_use_same_canonical_profile_artifacts(tmp_path) -> None:
    run = tmp_path / "run"
    profile = run / "profiles" / "DEFAULT_FILTERS"
    profile.mkdir(parents=True)
    (profile / "order_backtest_summary.csv").write_text(
        "total_candidates,accepted_count,rejected_count,tp_hits,sl_hits,open_at_end,total_net_pnl_usdt,total_pnl_pct,max_drawdown,last_days\n"
        "4,2,2,1,1,0,3.5,0.035,-1.2,30\n"
    )
    (profile / "backtest_orders.csv").write_text(
        "signal_id,symbol,lifecycle_state,decision,score,effective_rr,close_reason,net_pnl_usdt\n"
        "a,BTCUSDT,POSITION_CLOSED,ACCEPTED,8.4,2.0,TP_HIT,5.0\n"
        "b,ETHUSDT,POSITION_CLOSED,ACCEPTED,6.9,1.7,SL_HIT,-1.5\n"
    )
    (profile / "order_lifecycle.csv").write_text(
        "signal_id,lifecycle_state,decision,symbol,score,rr,effective_rr,reject_reason,close_reason,net_pnl_usdt\n"
        "a,SIGNAL_CREATED,PENDING,BTCUSDT,8.4,2.2,2.0,, ,\n"
        "a,POSITION_CLOSED,ACCEPTED,BTCUSDT,8.4,2.2,2.0,,TP_HIT,5.0\n"
        "b,SIGNAL_CREATED,PENDING,ETHUSDT,6.9,1.4,1.7,, ,\n"
        "b,POSITION_CLOSED,ACCEPTED,ETHUSDT,6.9,1.4,1.7,,SL_HIT,-1.5\n"
        "c,SIGNAL_CREATED,PENDING,XRPUSDT,2.1,1.1,1.1,, ,\n"
        "c,SIGNAL_REJECTED,REJECTED,XRPUSDT,2.1,1.1,1.1,LOW_SCORE, ,\n"
        "d,SIGNAL_CREATED,PENDING,ADAUSDT,8.0,1.7,1.0,, ,\n"
        "d,ORDER_REJECTED,REJECTED,ADAUSDT,8.0,1.7,1.0,EXECUTION_CONTEXT_UNAVAILABLE, ,\n"
    )
    (profile / "rejected_orders.csv").write_text(
        "signal_id,lifecycle_state,symbol,reject_reason,score,rr,effective_rr\n"
        "c,SIGNAL_REJECTED,XRPUSDT,LOW_SCORE,2.1,1.1,1.1\n"
        "d,ORDER_REJECTED,ADAUSDT,EXECUTION_CONTEXT_UNAVAILABLE,8.0,1.7,1.0\n"
    )
    (profile / "lifecycle_calibration_summary.json").write_text("{}")
    (profile / "backtest_filter_state.json").write_text('{"filter_profile":"DEFAULT_FILTERS","enabled_filters":[],"disabled_filters":[]}')

    result = bc.DashboardBacktestResult("COMPLETED", "last 30 days", ["BTCUSDT"], "1h", 10000, 1)
    bc._apply_backtest_artifact_model(result, run, selected_profile_name="DEFAULT_FILTERS", window_days=30)
    comparison = bc._comparison_metrics("DEFAULT_FILTERS", profile, 10000, window_days=30)

    assert result.selected_profile_dir == str(profile)
    assert result.summary_path == str(profile / "order_backtest_summary.csv")
    assert result.lifecycle_path == str(profile / "order_lifecycle.csv")
    assert result.rejected_path == str(profile / "rejected_orders.csv")
    assert result.accepted_trades == comparison["accepted_trades"] == 2
    assert result.rejected_signals == comparison["rejected_signals"] == 2
    assert result.backtest_rejection_rate == pytest.approx(comparison["reject_rate"])
    assert result.win_count == comparison["win_count"] == 1
    assert result.loss_count == comparison["loss_count"] == 1
    assert float(result.net_pnl) == pytest.approx(comparison["net_pnl"])
    assert {row["reason"] for row in result.top_rejection_reasons} == {"LOW_SCORE", "EXECUTION_CONTEXT_UNAVAILABLE"}


def test_dashboard_rejection_reasons_use_canonical_rejected_orders_not_raw_quality_summary(tmp_path) -> None:
    run = tmp_path / "run"
    profile = run / "profiles" / "DEFAULT_FILTERS"
    profile.mkdir(parents=True)
    (run / "backtest_profile_leaderboard.csv").write_text("profile,selected,accepted_trades,rejected_signals,net_pnl,profit_factor\nDEFAULT_FILTERS,true,0,2,0,0\n")
    (run / "backtest_run_metadata.json").write_text("{}")
    (profile / "order_backtest_summary.csv").write_text("total_candidates,accepted_count,rejected_count,tp_hits,sl_hits,open_at_end,total_net_pnl_usdt,rejection_counts\n2,0,2,0,0,0,0,{}\n")
    (profile / "order_lifecycle.csv").write_text("signal_id,lifecycle_state,decision,symbol,score,rr,effective_rr,reject_reason,expectancy_bucket\na,SIGNAL_REJECTED,REJECTED,BTCUSDT,1,1,0.5,LOW_EFFECTIVE_RR,LOW\nb,SIGNAL_REJECTED,REJECTED,BTCUSDT,1,1,0.5,UNKNOWN,LOW\n")
    (profile / "rejected_orders.csv").write_text("signal_id,lifecycle_state,symbol,reject_reason,score,rr,effective_rr\na,SIGNAL_REJECTED,BTCUSDT,LOW_SCORE,1,1,0.5\nb,SIGNAL_REJECTED,BTCUSDT,TOO_CHOPPY,1,1,0.5\n")
    (profile / "backtest_quality_summary.csv").write_text('metric,value\nraw_gate_reject_reason_distribution,"{\"LOW_EFFECTIVE_RR\": 1, \"UNKNOWN\": 1}"\n')
    (profile / "lifecycle_calibration_summary.json").write_text("{}")
    (profile / "backtest_filter_state.json").write_text("{}")

    result = bc.DashboardBacktestResult("COMPLETED", "last 30 days", ["BTCUSDT"], "1h", 10000, 1)
    bc._apply_backtest_artifact_model(result, run, selected_profile_name="DEFAULT_FILTERS", window_days=30)

    assert {row["reason"]: row["count"] for row in result.top_rejection_reasons} == {"LOW_SCORE": 1, "TOO_CHOPPY": 1}
