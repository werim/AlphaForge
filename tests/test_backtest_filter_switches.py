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

