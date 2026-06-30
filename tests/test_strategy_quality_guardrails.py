from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backtest_order as bo


def _cfg(**kw):
    base = bo.StrategyQualityGuardrailConfig(enabled=True)
    for k, v in kw.items():
        setattr(base, k, v)
    return base


def test_trade_frequency_guard_reduces_overactive_same_day_symbol_cluster():
    cfg = _cfg(max_accepted_trades_per_day=10, max_symbol_trades_per_day=1, max_symbol_regime_trades_per_day=1)
    stats = {"accepted_trades_by_day": {"2026-06-30": 1}, "accepted_trades_by_symbol_day": {"BTCUSDT:2026-06-30": 1}, "accepted_trades_by_symbol_regime_day": {}, "high_vol_accepted_trades_by_day": {}, "consecutive_sl_count": 0}
    assert bo._guardrail_rejection_reason("BTCUSDT", 1782777600000, "TREND", 8.0, 2.0, {}, stats, cfg) == "SYMBOL_CLUSTER_GUARD"


def test_loss_streak_pause_rejects_after_configured_consecutive_sls():
    cfg = _cfg(max_consecutive_sl_pause=2)
    stats = {"consecutive_sl_count": 2, "accepted_trades_by_day": {}, "accepted_trades_by_symbol_day": {}, "accepted_trades_by_symbol_regime_day": {}, "high_vol_accepted_trades_by_day": {}}
    assert bo._guardrail_rejection_reason("ETHUSDT", 1782777600000, "TREND", 8.0, 2.0, {}, stats, cfg) == "LOSS_STREAK_PAUSE"


def test_score10_sl_dominant_proxy_requires_secondary_quality():
    cfg = _cfg(saturated_min_effective_rr=2.2, saturated_max_cost_penalty=0.1)
    stats = {"accepted_trades_by_day": {}, "accepted_trades_by_symbol_day": {}, "accepted_trades_by_symbol_regime_day": {}, "high_vol_accepted_trades_by_day": {}, "consecutive_sl_count": 0}
    assert bo._guardrail_rejection_reason("SOLUSDT", 1782777600000, "RANGE", 10.0, 1.9, {"cost_penalty_total": 0.2}, stats, cfg) == "SCORE_SATURATION_GUARD"


def test_high_vol_flood_is_reduced_by_high_vol_guard():
    cfg = _cfg(high_vol_min_effective_rr=2.3, high_vol_max_trades_per_day=1)
    stats = {"accepted_trades_by_day": {}, "accepted_trades_by_symbol_day": {}, "accepted_trades_by_symbol_regime_day": {}, "high_vol_accepted_trades_by_day": {"2026-06-30": 1}, "consecutive_sl_count": 0}
    assert bo._guardrail_rejection_reason("DOGEUSDT", 1782777600000, "HIGH_VOL", 8.5, 2.5, {"volatility_regime": "HIGH"}, stats, cfg) == "HIGH_VOL_OVERTRADE"


def test_profile_quality_fails_positive_pnl_overtrade_score_saturation():
    rows = []
    for i, reason in enumerate(["SL_HIT", "SL_HIT", "SL_HIT"]):
        rows.append(bo.LifecycleRow(1782777600000+i, "BTCUSDT", "LONG", "X", "", "TREND", 10.0, 2, 1, .9, 1.2, "ORDER_PLACED", "POSITION_CLOSED", close_reason=reason, net_pnl_usdt=-1))
    rows.append(bo.LifecycleRow(1782777600010, "BTCUSDT", "LONG", "X", "", "TREND", 10.0, 2, 1, .9, 1.2, "ORDER_PLACED", "POSITION_CLOSED", close_reason="TP_HIT", net_pnl_usdt=10))
    evidence = bo.build_strategy_quality_evidence(rows, [], {"total_net_pnl_usdt": 7, "profit_factor": 3, "max_drawdown_pct": -1, "longest_loss_streak": 3, "requested_last_n_days": 1}, _cfg(max_loss_streak_for_profile_pass=2))
    assert evidence["profile_quality_status"] == "FAIL"
    assert "SCORE_SATURATION_RISK" in evidence["profile_quality_reasons"]
    assert "LOSS_STREAK_TOO_HIGH" in evidence["profile_quality_reasons"]


def test_high_vol_momentum_diagnostic_is_diagnostic_only():
    evidence = bo.build_strategy_quality_evidence([], [], {"total_net_pnl_usdt": 1, "profit_factor": 2, "max_drawdown_pct": 0, "longest_loss_streak": 0, "requested_last_n_days": 1}, _cfg(profile="HIGH_VOL_MOMENTUM_DIAGNOSTIC"))
    assert evidence["profile_quality_status"] == "FAIL"
    assert "DIAGNOSTIC_ONLY_PROFILE" in evidence["profile_quality_reasons"]


def test_env_example_contains_strategy_quality_guardrails():
    text = Path(".env.example").read_text()
    for name in ["ALPHAFORGE_BACKTEST_MAX_ACCEPTED_TRADES_PER_DAY", "ALPHAFORGE_BACKTEST_MAX_CONSECUTIVE_SL_PAUSE", "ALPHAFORGE_BACKTEST_SCORE10_SL_DOMINANCE_GUARD", "ALPHAFORGE_BACKTEST_HIGH_VOL_ACCEPTANCE_GUARD", "ALPHAFORGE_BACKTEST_MIN_PROFIT_FACTOR_FOR_PROFILE_PASS", "ALPHAFORGE_BACKTEST_MAX_LOSS_STREAK_FOR_PROFILE_PASS", "ALPHAFORGE_BACKTEST_MAX_DRAWDOWN_PCT_FOR_PROFILE_PASS"]:
        assert name in text
