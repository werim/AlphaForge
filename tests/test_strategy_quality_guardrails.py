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


def test_guardrail_evidence_exports_reason_breakdown_and_examples():
    import backtest_order as bo
    cfg = _cfg()
    evidence = bo.build_strategy_quality_evidence([], [
        {"reject_reason": "SCORE_SATURATION_GUARD", "symbol": "BTCUSDT", "side": "LONG", "timestamp": "1", "score": "10", "effective_rr": "1.9", "regime": "RANGE", "cost_penalty": "0.2", "shadow_outcome": "WOULD_SL"},
        {"reject_reason": "SCORE_SATURATION_GUARD", "symbol": "ETHUSDT", "side": "SHORT", "timestamp": "2", "score": "10", "effective_rr": "1.8", "regime": "RANGE", "cost_penalty": "0.3", "shadow_outcome": "WOULD_TP"},
        {"reject_reason": "LOW_SCORE", "symbol": "SOLUSDT"},
    ], {"total_net_pnl_usdt": "0", "requested_last_n_days": "30"}, cfg)
    assert evidence["rejected_by_new_guardrails"] == 2
    assert evidence["guardrail_reject_breakdown"] == {"SCORE_SATURATION_GUARD": 2}
    assert evidence["top_guardrail_reject_reasons"][0] == {"reason": "SCORE_SATURATION_GUARD", "count": 2}
    assert evidence["representative_guardrail_reject_examples"][0]["symbol"] == "BTCUSDT"


def test_default_gate_funnel_exposes_scope_when_gate_not_comparable():
    import backtest_order as bo
    rows = bo.build_default_gate_funnel([], [])
    assert rows[0]["zero_reject_warning"] is True
    assert rows[0]["funnel_scope"] == "rejected_orders_plus_executed_terminal_rows"
    assert "pre-funnel" in rows[0]["comparability_note"]


def test_high_vol_guard_diagnostics_export_candidate_quality_fields():
    rows = bo.build_high_vol_guard_diagnostics([
        {
            "reject_reason": "HIGH_VOL_GUARD", "timestamp": "1", "symbol": "BTCUSDT", "side": "LONG",
            "score": "8.4", "rr": "2.4", "raw_rr": "2.4", "effective_rr": "2.1",
            "expectancy_bucket": "POSITIVE", "source_stage": "STRATEGY_QUALITY_GUARDRAIL",
            "regime": "HIGH_VOL", "setup_type": "breakout", "cost_penalty": "0.1",
            "spread_pct": "0.01", "expected_slippage_pct": "0.02", "liquidity_score": "0.9",
            "all_failed_gates": '["HIGH_VOL_GUARD"]', "entry": "100", "sl": "98",
        }
    ], _cfg(high_vol_min_effective_rr=2.3))
    assert rows[0]["volatility_metric_name"] == "effective_rr"
    assert rows[0]["volatility_threshold"] == 2.3
    assert rows[0]["volatility_ratio_to_threshold"] > 0
    assert rows[0]["would_accept_if_high_vol_guard_disabled"] is True
    assert "filters_passed" in rows[0]


def test_high_vol_guard_not_emitted_when_volatility_acceptance_requirements_pass():
    cfg = _cfg(high_vol_min_effective_rr=2.3, high_vol_max_cost_penalty=0.18)
    stats = {"accepted_trades_by_day": {}, "accepted_trades_by_symbol_day": {}, "accepted_trades_by_symbol_regime_day": {}, "high_vol_accepted_trades_by_day": {}, "consecutive_sl_count": 0}
    assert bo._guardrail_rejection_reason("BTCUSDT", 1782777600000, "HIGH_VOL", 8.5, 2.5, {"volatility_regime": "HIGH", "cost_penalty_total": 0.1}, stats, cfg) == ""


def test_high_vol_guard_diagnostic_profile_does_not_change_default_guard_config():
    stats = {"accepted_trades_by_day": {}, "accepted_trades_by_symbol_day": {}, "accepted_trades_by_symbol_regime_day": {}, "high_vol_accepted_trades_by_day": {}, "consecutive_sl_count": 0}
    default = _cfg(high_vol_min_effective_rr=2.3)
    diagnostic = _cfg(high_vol_min_effective_rr=2.3, profile="HIGH_VOL_GUARD_OFF_DIAGNOSTIC")
    ctx = {"volatility_regime": "HIGH", "cost_penalty_total": 0.1}
    assert bo._guardrail_rejection_reason("BTCUSDT", 1782777600000, "HIGH_VOL", 8.5, 2.1, ctx, stats, default) == "HIGH_VOL_GUARD"
    assert bo._guardrail_rejection_reason("BTCUSDT", 1782777600000, "HIGH_VOL", 8.5, 2.1, ctx, stats, diagnostic) == ""
    evidence = bo.build_strategy_quality_evidence([], [], {"total_net_pnl_usdt": 1, "profit_factor": 2, "max_drawdown_pct": 0}, diagnostic)
    assert "DIAGNOSTIC_ONLY_PROFILE" in evidence["profile_quality_reasons"]
    assert "not a production strategy profile" in evidence["diagnostic_profile_warning"]


def test_acceptance_funnel_counts_reconcile_and_exposes_high_vol_counterfactual():
    rejected = [
        {"reject_reason": "LOW_SCORE", "lifecycle_state": "SIGNAL_REJECTED", "symbol": "BTCUSDT"},
        {"reject_reason": "HIGH_VOL_GUARD", "lifecycle_state": "SIGNAL_REJECTED", "symbol": "BTCUSDT", "score": "8", "rr": "2.4", "effective_rr": "2.1", "cost_penalty": "0.1", "all_failed_gates": '["HIGH_VOL_GUARD"]'},
        {"reject_reason": "TOO_CHOPPY", "lifecycle_state": "SYMBOL_REJECTED", "symbol": "BTCUSDT"},
    ]
    rows = bo.build_acceptance_funnel(rejected, 0, {"total_candidates": 3, "symbol_rejected_count": 1}, _cfg())
    by_stage = {r["stage"]: r["count"] for r in rows}
    assert by_stage["total_candidates"] == 3
    assert by_stage["symbol_level_rejected"] == 1
    assert by_stage["signal_level_rejected"] == 2
    assert by_stage["would_accept_without_high_vol_guard"] == 1


def test_high_vol_guard_corrected_effective_rr_gap_and_trigger_fields():
    rows = bo.build_high_vol_guard_diagnostics([
        {"reject_reason":"HIGH_VOL_GUARD","timestamp":"1","symbol":"BTCUSDT","side":"LONG","score":"8","rr":"2.0","raw_rr":"2.0","effective_rr":"1.5","expectancy_bucket":"POSITIVE","cost_penalty":"0.1","all_failed_gates": '["HIGH_VOL_GUARD", "STOP_TOO_WIDE"]'}
    ], _cfg(high_vol_min_effective_rr=2.3))
    row = rows[0]
    assert row["effective_rr_gap_to_threshold"] == 0.8
    assert row["counterfactual_effective_rr_gap"] == 0.8
    assert row["counterfactual_volatility_penalty"] == 0.8
    assert row["guard_metric_name"] == "effective_rr"
    assert row["guard_breach_direction"] == "BELOW_MINIMUM"
    assert row["high_vol_guard_trigger"] == "BOTH"
    assert row["counterfactual_warning"] == "HIGH_VOL_GUARD counterfactual is diagnostic only. It does not imply production acceptance."


def test_high_vol_guard_summary_far_below_threshold_is_protective():
    rows = bo.build_high_vol_guard_diagnostics([
        {"reject_reason":"HIGH_VOL_GUARD","symbol":"BTCUSDT","score":"8","rr":"2.0","effective_rr":"1.4","cost_penalty":"0.1","all_failed_gates": '["HIGH_VOL_GUARD"]'},
        {"reject_reason":"HIGH_VOL_GUARD","symbol":"BTCUSDT","score":"8","rr":"2.0","effective_rr":"1.5","cost_penalty":"0.1","all_failed_gates": '["HIGH_VOL_GUARD"]'},
    ], _cfg(high_vol_min_effective_rr=2.3))
    summary = bo.classify_high_vol_guard(rows)
    assert summary["high_vol_guard_verdict"] == "VALID_PROTECTIVE_GUARD"
    assert summary["high_vol_guard_near_threshold_count"] == 0
    assert "Do not relax" in summary["recommended_action"]


def test_low_score_diagnostics_and_summary_do_not_auto_accept_far_below():
    rows = bo.build_low_score_diagnostics([
        {"reject_reason":"LOW_SCORE","symbol":"BTCUSDT","score":"0.1","rr":"2.0","raw_rr":"2.0","effective_rr":"1.8","expectancy_bucket":"POSITIVE","all_failed_gates": '["LOW_SCORE"]'}
    ])
    assert "min_score_threshold" in rows[0]
    assert rows[0]["score_gap_to_threshold"] == max(0.0, rows[0]["min_score_threshold"] - 0.1)
    assert rows[0]["passed_rr"] is True
    assert rows[0]["would_accept_if_low_score_disabled"] is True
    summary = bo.classify_low_score(rows)
    assert summary["low_score_verdict"] == "VALID_QUALITY_FILTER"
    assert summary["far_below_threshold_count"] == 1


def test_symbol_reject_diagnostics_missing_metrics_are_not_confident():
    rows = bo.build_symbol_reject_diagnostics([
        {"reject_reason":"TOO_CHOPPY","symbol":"BTCUSDT","timestamp":"1"},
        {"reject_reason":"WEAK_TREND_AND_NO_RANGE_EDGE","symbol":"ETHUSDT","timestamp":"2"},
    ])
    assert rows[0]["source_function"] == "alphaforge.symbol_selector.select_symbol"
    assert rows[0]["interval_sensitive"] is True
    assert rows[0]["future_leakage_risk"].startswith("UNKNOWN")
    summary = bo.classify_symbol_reject(rows)
    assert summary["symbol_reject_verdict"] == "FEATURE_MISSING"


def test_zero_accepted_root_cause_summary_reconciles_distribution():
    summary = bo.build_zero_accepted_root_cause_summary(
        {"total_candidates": 3, "accepted_count": 0, "rejected_count": 3, "signal_rejected_count": 2, "symbol_rejected_count": 1, "reject_reason_distribution": {"LOW_SCORE": 2, "TOO_CHOPPY": 1}},
        {"high_vol_guard_verdict":"VALID_PROTECTIVE_GUARD"}, {"low_score_verdict":"VALID_QUALITY_FILTER", "low_score_evidence":"2 rows"}, {"symbol_reject_verdict":"FEATURE_MISSING", "symbol_reject_evidence":"1 row"}
    )
    assert summary["primary_bottleneck"] == "LOW_SCORE"
    assert summary["secondary_bottleneck"] == "TOO_CHOPPY"
    assert summary["production_threshold_change_recommended"] is False


def test_low_score_uses_row_threshold_not_backtest_config_scale():
    rows = bo.build_low_score_diagnostics([
        {
            "reject_reason": "LOW_SCORE",
            "symbol": "BTCUSDT",
            "score": "6.37",
            "min_required_score": "7.5",
            "rr": "2.0",
            "raw_rr": "2.0",
            "effective_rr": "1.8",
            "expectancy_bucket": "POSITIVE",
            "all_failed_gates": '["LOW_SCORE"]',
            "diagnostics": '{"min_score": 7.5, "adaptive_thresholds": {"min_score": 7.5}}',
        }
    ])
    row = rows[0]
    assert row["min_score_threshold"] == 7.5
    assert row["score_threshold_source"] == "row.min_required_score"
    assert row["score_gap_to_threshold"] == 1.13
    assert row["score_threshold_scale_detected"] == "0_10"


def test_low_score_near_far_counts_use_real_row_thresholds():
    rows = bo.build_low_score_diagnostics([
        {"reject_reason": "LOW_SCORE", "symbol": "BTCUSDT", "score": "7.2", "min_required_score": "7.5", "rr": "2", "raw_rr": "2", "effective_rr": "1.8", "expectancy_bucket": "POSITIVE", "all_failed_gates": '["LOW_SCORE"]'},
        {"reject_reason": "LOW_SCORE", "symbol": "ETHUSDT", "score": "6.0", "min_required_score": "7.5", "rr": "2", "raw_rr": "2", "effective_rr": "1.8", "expectancy_bucket": "POSITIVE", "all_failed_gates": '["LOW_SCORE"]'},
    ])
    summary = bo.classify_low_score(rows)
    assert summary["near_threshold_count"] == 1
    assert summary["far_below_threshold_count"] == 1
    assert summary["score_threshold_source_distribution"] == {"row.min_required_score": 2}


def test_symbol_reject_extracts_nested_selector_metrics_when_top_level_empty():
    diagnostics = {
        "selector": {
            "inputs": {"chop_score": 0.82, "trend_strength": 0.21, "candle_range_pct": 1.2, "liquidity_score": 0.74, "spread_pct": 0.01, "volume_24h_usdt": 123456},
            "metrics": {"chop_score": 0.82, "trend_strength": 0.21, "volatility_pct": 2.4, "funding_rate_pct": 0.001},
            "sub_scores": {"trend_score": 0.1},
        },
        "reject_reasons": ["TOO_CHOPPY"],
    }
    rows = bo.build_symbol_reject_diagnostics([
        {"reject_reason": "TOO_CHOPPY", "symbol": "BTCUSDT", "timestamp": "1", "diagnostics": __import__('json').dumps(diagnostics)}
    ])
    row = rows[0]
    assert row["metric_source"] == "DIAGNOSTICS_SELECTOR_INPUTS"
    assert row["selector_chop_score"] == 0.82
    assert row["selector_trend_strength"] == 0.21
    assert row["selector_volatility_pct"] == 2.4
    assert row["selector_volume_24h_usdt"] == 123456
    assert bo.classify_symbol_reject(rows)["missing_market_structure_metric_count"] == 0
    assert bo.classify_symbol_reject(rows)["symbol_reject_verdict"] == "VALID_MARKET_STRUCTURE_FILTER"


def test_symbol_feature_missing_only_when_top_level_and_diagnostics_absent():
    present = bo.build_symbol_reject_diagnostics([
        {"reject_reason": "WEAK_TREND_AND_NO_RANGE_EDGE", "symbol": "BTCUSDT", "diagnostics": '{"selector":{"inputs":{"trend_strength":0.3}}}'},
        {"reject_reason": "TOO_CHOPPY", "symbol": "ETHUSDT"},
    ])
    summary = bo.classify_symbol_reject(present)
    assert summary["missing_market_structure_metric_count"] == 1
    assert summary["symbol_reject_verdict"] == "VALID_MARKET_STRUCTURE_FILTER"
    missing = bo.classify_symbol_reject(bo.build_symbol_reject_diagnostics([{"reject_reason": "TOO_CHOPPY", "symbol": "XRPUSDT"}]))
    assert missing["symbol_reject_verdict"] == "FEATURE_MISSING"


def test_zero_accepted_evidence_quality_reasons_are_partial_when_evidence_missing():
    summary = bo.build_zero_accepted_root_cause_summary(
        {"total_candidates": 2, "accepted_count": 0, "rejected_count": 2, "reject_reason_distribution": {"LOW_SCORE": 1, "TOO_CHOPPY": 1}},
        {"high_vol_guard_verdict": "VALID_PROTECTIVE_GUARD"},
        {"low_score_count": 1, "low_score_verdict": "VALID_QUALITY_FILTER", "threshold_scale_mismatch_detected_count": 1, "threshold_scale_correction_applied_count": 0},
        {"symbol_reject_verdict": "FEATURE_MISSING", "symbol_reject_evidence": "missing"},
    )
    assert summary["evidence_quality"] == "PARTIAL"
    assert "LOW_SCORE_THRESHOLD_SCALE_MISMATCH" in summary["evidence_quality_reasons"]
    assert "SYMBOL_REJECT_METRICS_UNAVAILABLE" in summary["evidence_quality_reasons"]
