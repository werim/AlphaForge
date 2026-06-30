import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
    (profile_dir / "lifecycle_calibration_summary.json").write_text(json.dumps({
        "rejection_funnel": {"accepted_trades": accepted, "signal_engine_signal_rejected": 2},
        "accepted_trade_diagnostics": [{"symbol": "BTCUSDT", "score": "10", "effective_rr": "2.0"}] if accepted else [],
        "accepted_score_distribution": {"count": accepted},
        "accepted_effective_rr_distribution": {"count": accepted},
        "execution_cost_summary": {"spread_pct": {"count": 2}},
    }))


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


def test_profile_comparison_main_result_uses_default_profile_artifacts(monkeypatch, tmp_path):
    class BacktestCfg:
        output_dir = str(tmp_path)
        export_config_snapshot = False
        top_n = 1
        timeframe = "1h"
    class Cfg:
        backtest = BacktestCfg()
    monkeypatch.setattr(bc, "load_config_from_env", lambda: Cfg())
    monkeypatch.setattr(bc, "config_snapshot", lambda mode: {})
    monkeypatch.setattr(bc, "canonical_utc_timestamp", lambda: "2026-06-30T14:27:38Z")

    def fake_run(command, cwd, text, capture_output, timeout, check, env):
        profile_dir = Path(command[command.index("--output-dir") + 1])
        accepted = 4 if profile_dir.name == "DEFAULT_FILTERS" else 9
        net = 1.0938092385903218 if profile_dir.name == "DEFAULT_FILTERS" else -99.0
        _write_profile_artifacts(profile_dir, [], accepted, net)
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(bc.subprocess, "run", fake_run)
    result = bc.run_dashboard_backtest(bc.DashboardBacktestRequest(90, ["BTCUSDT"], "1h", 10000.0, 1, run_profile_comparison=True))

    assert result.status == "COMPLETED"
    assert result.filter_profile == "DEFAULT_FILTERS"
    assert result.accepted_trades == 4
    assert result.rejected_signals == 2
    assert result.win_count == 3
    assert result.loss_count == 1
    assert result.open_count == 0
    assert result.net_pnl == "1.0938092385903218"
    assert result.top_rejection_reasons[0]["reason"] == "NEGATIVE_EXPECTANCY"
    assert result.rejection_funnel["accepted_trades"] == 4
    assert result.accepted_trade_diagnostics
    assert result.score_distribution["count"] == 2
    assert result.accepted_effective_rr_distribution["count"] == 4
    assert result.execution_cost_summary
    assert str(Path(result.summary_path).parent).endswith("profiles/DEFAULT_FILTERS")


def test_profile_avg_trades_per_day_uses_requested_window(tmp_path):
    profile_dir = tmp_path / "DEFAULT_FILTERS"
    _write_profile_artifacts(profile_dir, [], 4, 1.0)
    # Simulate canonical summary using requested_last_n_days rather than legacy last_days.
    row = bc._read_first_csv_row(profile_dir / "order_backtest_summary.csv")
    row["requested_last_n_days"] = "90"
    row.pop("last_days", None)
    with (profile_dir / "order_backtest_summary.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(row.keys()))
        writer.writeheader(); writer.writerow(row)

    metrics = bc._comparison_metrics("DEFAULT_FILTERS", profile_dir, 10000.0)
    assert metrics["avg_trades_per_day"] == pytest.approx(4 / 90)


def test_backtest_quality_summary_counts_unique_accepted_lifecycle_signals():
    import backtest_order as bo
    rows = [
        {"signal_id": "a", "lifecycle_state": "SIGNAL_CREATED", "accepted_reason": "BASELINE"},
        {"signal_id": "a", "lifecycle_state": "ORDER_PLACED", "accepted_reason": "BASELINE"},
        {"signal_id": "a", "lifecycle_state": "POSITION_CLOSED", "accepted_reason": "BASELINE", "net_pnl_usdt": "1"},
        {"signal_id": "b", "lifecycle_state": "SIGNAL_CREATED", "accepted_reason": "BASELINE", "reject_reason": "LOW_SCORE"},
        {"signal_id": "b", "lifecycle_state": "SIGNAL_REJECTED", "reject_reason": "LOW_SCORE"},
    ]
    summary = bo.build_backtest_quality_summary(rows)
    assert summary["total_candidates"] == 2
    assert summary["accepted_count"] == 1
    assert summary["rejected_count"] == 1
    assert summary["accepted_reason_breakdown"] == {"BASELINE": 1}
