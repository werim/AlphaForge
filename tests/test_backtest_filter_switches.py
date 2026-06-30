import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alphaforge.config import load_config_from_env
from alphaforge.order import OrderCandidate, TradingMode, evaluate_trade_quality
from alphaforge.symbol_selector import select_symbol

FILTERS = {
    "ALPHAFORGE_BACKTEST_FILTER_LOW_SCORE_ENABLED": "low_score_enabled",
    "ALPHAFORGE_BACKTEST_FILTER_TOO_CHOPPY_ENABLED": "too_choppy_enabled",
    "ALPHAFORGE_BACKTEST_FILTER_WEAK_TREND_NO_RANGE_ENABLED": "weak_trend_no_range_enabled",
    "ALPHAFORGE_BACKTEST_FILTER_STOP_TOO_WIDE_ENABLED": "stop_too_wide_enabled",
    "ALPHAFORGE_BACKTEST_FILTER_RR_TOO_LOW_ENABLED": "rr_too_low_enabled",
    "ALPHAFORGE_BACKTEST_FILTER_DAILY_SYMBOL_TRADE_LIMIT_ENABLED": "daily_symbol_trade_limit_enabled",
    "ALPHAFORGE_BACKTEST_FILTER_REGIME_MISMATCH_ENABLED": "regime_mismatch_enabled",
    "ALPHAFORGE_BACKTEST_FILTER_PANIC_CONDITIONS_ENABLED": "panic_conditions_enabled",
}


def _candidate(**kwargs):
    data = dict(symbol="BTCUSDT", side="LONG", setup_type="BREAKOUT_UP", setup_reason="fixture", regime="TREND", score=9.0, rr=2.0, expectancy=0.2, entry=100, sl=99, tp=102, order_type="LIMIT")
    data.update(kwargs)
    return OrderCandidate(**data)


def test_env_example_active_backtest_filters_are_loaded(monkeypatch):
    active = []
    for line in Path(".env.example").read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("ALPHAFORGE_BACKTEST_FILTER_") and "=" in stripped:
            active.append(stripped.split("=", 1)[0])
    assert set(active) == set(FILTERS)
    for env_name, attr in FILTERS.items():
        monkeypatch.setenv(env_name, "false")
        assert getattr(load_config_from_env().backtest.filter_switches, attr) is False
        monkeypatch.delenv(env_name)


def test_backtest_low_score_switch_changes_decision_but_paper_not_loosened():
    c = _candidate(score=1.0)
    enabled = evaluate_trade_quality(c, {}, {}, {"MODE": TradingMode.BACKTEST.value})
    disabled = evaluate_trade_quality(c, {}, {}, {"MODE": "BACKTEST", "DISABLED_BACKTEST_FILTERS": ["LOW_SCORE"]})
    paper = evaluate_trade_quality(c, {}, {}, {"MODE": "PAPER", "DISABLED_BACKTEST_FILTERS": ["LOW_SCORE"]})
    assert enabled.reject_reason == "LOW_SCORE"
    assert disabled.reject_reason != "LOW_SCORE"
    assert disabled.diagnostics["bypassed_reject_reasons"] == ["LOW_SCORE"]
    assert paper.reject_reason == "LOW_SCORE"


def test_backtest_trade_quality_switches_are_real_decision_gates():
    cases = [
        ("RR_TOO_LOW", _candidate(rr=1.0), {}, {}),
        ("REGIME_MISMATCH", _candidate(setup_type="RANGE_MEAN_REVERSION", regime="TREND"), {}, {}),
        ("STOP_TOO_WIDE", _candidate(sl=95), {"volatility_regime": "normal"}, {}),
        ("DAILY_SYMBOL_TRADE_LIMIT", _candidate(), {"volatility_regime": "normal"}, {"trades_today_by_symbol": {"BTCUSDT": 2}}),
    ]
    for reason, candidate, market, stats in cases:
        enabled = evaluate_trade_quality(candidate, market, stats, {"MODE": "BACKTEST"})
        disabled = evaluate_trade_quality(candidate, market, stats, {"MODE": "BACKTEST", "DISABLED_BACKTEST_FILTERS": [reason]})
        assert enabled.reject_reason == reason
        assert disabled.reject_reason != reason
        assert reason in disabled.diagnostics["bypassed_reject_reasons"]


def test_backtest_symbol_selector_switches_are_real_decision_gates():
    market = {"volume_24h_usdt": 10_000_000, "spread_pct": 0.001, "liquidity_score": 0.9, "volatility_pct": 1.0, "trend_strength": 0.1, "recent_volume_change_pct": 50, "chop_score": 0.9, "panic_score": 0.9}
    enabled = select_symbol("BTCUSDT", market)
    disabled = select_symbol("BTCUSDT", market, {"disabled_backtest_filters": ["TOO_CHOPPY", "WEAK_TREND_AND_NO_RANGE_EDGE", "PANIC_CONDITIONS"]})
    assert {"TOO_CHOPPY", "WEAK_TREND_AND_NO_RANGE_EDGE", "PANIC_CONDITIONS"}.issubset(set(enabled.reject_reasons))
    assert not {"TOO_CHOPPY", "WEAK_TREND_AND_NO_RANGE_EDGE", "PANIC_CONDITIONS"}.intersection(disabled.reject_reasons)
    assert set(disabled.diagnostics["bypassed_reject_reasons"]) == {"TOO_CHOPPY", "WEAK_TREND_AND_NO_RANGE_EDGE", "PANIC_CONDITIONS"}



def test_rr_too_low_uses_effective_rr_and_can_only_be_bypassed_in_backtest():
    c = _candidate(rr=2.2)
    market = {"effective_rr": 1.2, "spread_pct": 0.001, "expected_slippage_pct": 0.001, "atr_pct": 1.0, "volatility_regime": "normal"}
    enabled = evaluate_trade_quality(c, market, {}, {"MODE": "BACKTEST", "MIN_EFFECTIVE_RR": 1.6})
    disabled = evaluate_trade_quality(c, market, {}, {"MODE": "BACKTEST", "MIN_EFFECTIVE_RR": 1.6, "DISABLED_BACKTEST_FILTERS": ["RR_TOO_LOW"]})
    paper = evaluate_trade_quality(c, market, {}, {"MODE": "PAPER", "MIN_EFFECTIVE_RR": 1.6, "DISABLED_BACKTEST_FILTERS": ["RR_TOO_LOW"]})
    assert enabled.reject_reason == "RR_TOO_LOW"
    assert disabled.accepted
    assert "RR_TOO_LOW" in disabled.diagnostics["bypassed_reject_reasons"]
    assert paper.reject_reason == "RR_TOO_LOW"


def test_regime_mismatch_enabled_by_default():
    c = _candidate(setup_type="RANGE_MEAN_REVERSION", regime="TREND")
    assert evaluate_trade_quality(c, {}, {}, {"MODE": "BACKTEST"}).reject_reason == "REGIME_MISMATCH"

import importlib.util

_BO_SPEC = importlib.util.spec_from_file_location("backtest_order", Path(__file__).resolve().parents[1] / "backtest_order.py")
bo = importlib.util.module_from_spec(_BO_SPEC)
assert _BO_SPEC.loader is not None
_BO_SPEC.loader.exec_module(bo)


def test_filter_state_artifact_records_all_disabled_filters(tmp_path):
    state = bo.build_backtest_filter_state(
        disabled_filters=bo.BACKTEST_FILTER_REASONS,
        source="dashboard",
        timestamp="2026-06-30T00:00:00+00:00",
        symbols=["BTCUSDT", "ETHUSDT"],
        timeframe="1h",
        last_days=30,
    )
    bo.write_backtest_filter_state_artifacts(str(tmp_path), state)
    assert state["filter_profile"] == "ALL_OFF"
    assert set(state["disabled_filters"]) == set(bo.BACKTEST_FILTER_REASONS)
    assert "diagnostic stress test" in state["all_off_warning"]
    assert (tmp_path / "backtest_filter_state.json").exists()
    assert "LOW_SCORE" in (tmp_path / "backtest_filter_state.csv").read_text()


def test_negative_expectancy_remains_hard_safety_when_all_optional_filters_disabled():
    c = _candidate(score=10.0, rr=3.0, expectancy=-0.2)
    decision = evaluate_trade_quality(c, {"effective_rr": 3.0}, {}, {"MODE": "BACKTEST", "DISABLED_BACKTEST_FILTERS": list(bo.BACKTEST_FILTER_REASONS)})
    assert decision.reject_reason == "NEGATIVE_EXPECTANCY"
    assert "NEGATIVE_EXPECTANCY" in {reason for gate in bo.HARD_SAFETY_GATES for reason in gate["affected_reject_reasons"]}


def test_filter_profile_comparison_artifact_is_backtest_only_scaffold():
    state = bo.build_backtest_filter_state(disabled_filters=[], source="default", timestamp="now", symbols=["BTCUSDT"], timeframe="1h", last_days=30)
    artifact = bo.build_filter_profile_comparison_artifact({"total_candidates": 3, "accepted_count": 1, "rejected_count": 2}, {}, state)
    assert artifact["mode"] == "BACKTEST"
    assert artifact["artifact_only"] is True
    assert artifact["profiles"]["DEFAULT"]["accepted_trades"] == 1
    assert artifact["profiles"]["ALL_OFF"]["status"] == "NOT_RUN_IN_THIS_ARTIFACT"


def test_accepted_loss_diagnostics_exports_required_buckets():
    rows = [
        {"decision": "ACCEPTED", "lifecycle_state": "POSITION_CLOSED", "close_reason": "TP_HIT", "score": 10, "effective_rr": 2.5, "net_pnl_usdt": 3, "regime": "TREND", "side": "LONG", "symbol": "BTCUSDT"},
        {"decision": "ACCEPTED", "lifecycle_state": "POSITION_CLOSED", "close_reason": "SL_HIT", "score": 10, "effective_rr": 2.6, "net_pnl_usdt": -5, "regime": "TREND", "side": "SHORT", "symbol": "ETHUSDT"},
    ]
    diag = bo.build_accepted_loss_diagnostics(rows)
    assert diag["accepted_count"] == 2
    assert diag["by"]["score_bucket"]["10"]["losses"] == 1
    assert diag["score_10_accepted_net_pnl"] == -2
    assert diag["high_effective_rr_accepted_outcome_split"] == {"TP_HIT": 1, "SL_HIT": 1}
