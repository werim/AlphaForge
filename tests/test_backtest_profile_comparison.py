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

    def fake_run(command, cwd, text, capture_output, timeout, check, env):
        profile_dir = Path(command[command.index("--output-dir") + 1])
        disabled = [command[i + 1] for i, arg in enumerate(command) if arg == "--disable-backtest-filter"]
        accepted = 4 + len(disabled)
        net = 40.0 - len(disabled) * 3.0
        _write_profile_artifacts(profile_dir, disabled, accepted, net)
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(bc.subprocess, "run", fake_run)
    request = bc.DashboardBacktestRequest(30, ["BTCUSDT"], "1h", 10000.0, 1, run_profile_comparison=True)
    result = bc.run_dashboard_backtest(request)

    assert result.status == "COMPLETED"
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
