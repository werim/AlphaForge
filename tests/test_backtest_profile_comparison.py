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
