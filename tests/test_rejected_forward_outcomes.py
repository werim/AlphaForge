import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("backtest_order", Path(__file__).resolve().parents[1] / "backtest_order.py")
bo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bo)


def row(**kw):
    base = {"timestamp": 1000, "symbol": "BTCUSDT", "side": "LONG", "reject_reason": "LOW_SCORE", "lifecycle_state": "SIGNAL_REJECTED", "entry": 100, "sl": 95, "tp": 110, "rr": 2.0, "score": 7.0, "min_score_threshold": 7.5, "spread_pct": 0.1, "expected_slippage_pct": 0.1, "liquidity_score": 1.0}
    base.update(kw)
    return base


def test_rejected_forward_uses_only_candles_after_reject_timestamp():
    candles = [bo.Candle(1000, 100, 111, 99, 110, 1), bo.Candle(2000, 100, 101, 96, 100, 1)]
    out = bo.evaluate_rejected_forward_outcome(row(), candles, forward_window_bars=1, interval_minutes=60)
    assert out["first_touch_outcome"] == "WOULD_TIMEOUT"


def test_low_score_valid_geometry_would_tp_before_sl():
    candles = [bo.Candle(2000, 100, 111, 99, 110, 1), bo.Candle(3000, 100, 101, 94, 95, 1)]
    assert bo.evaluate_rejected_forward_outcome(row(), candles)["first_touch_outcome"] == "WOULD_TP"


def test_low_score_valid_geometry_would_sl_before_tp():
    candles = [bo.Candle(2000, 100, 101, 94, 95, 1), bo.Candle(3000, 100, 111, 99, 110, 1)]
    assert bo.evaluate_rejected_forward_outcome(row(), candles)["first_touch_outcome"] == "WOULD_SL"


def test_timeout_when_neither_tp_nor_sl_touched():
    candles = [bo.Candle(2000, 100, 104, 96, 101, 1), bo.Candle(3000, 101, 104, 96, 101, 1)]
    assert bo.evaluate_rejected_forward_outcome(row(), candles, forward_window_bars=2)["first_touch_outcome"] == "WOULD_TIMEOUT"


def test_ambiguous_when_tp_and_sl_same_candle():
    candles = [bo.Candle(2000, 100, 111, 94, 101, 1)]
    assert bo.evaluate_rejected_forward_outcome(row(), candles)["first_touch_outcome"] == "WOULD_AMBIGUOUS"


def test_missing_geometry_no_fake_tp_sl():
    out = bo.evaluate_rejected_forward_outcome(row(tp=""), [bo.Candle(2000, 100, 120, 80, 100, 1)])
    assert out["first_touch_outcome"] == "NO_TP_SL_GEOMETRY"
    assert out["tp"] == ""


def test_symbol_reject_without_geometry_has_symbol_no_candidate_geometry():
    out = bo.evaluate_rejected_forward_outcome(row(reject_reason="TOO_CHOPPY", lifecycle_state="SYMBOL_REJECTED", side="N/A", entry="", sl="", tp=""), [])
    assert out["first_touch_outcome"] == "SYMBOL_REJECT_NO_CANDIDATE_GEOMETRY"


def test_effective_shadow_r_subtracts_execution_cost_penalty():
    out = bo.evaluate_rejected_forward_outcome(row(spread_pct=1.0, expected_slippage_pct=1.0), [bo.Candle(2000, 100, 111, 99, 110, 1)])
    assert out["cost_penalty"] > 0
    assert out["effective_shadow_r_after_costs"] < out["gross_shadow_r"]


def test_low_score_summary_separates_near_and_far():
    rows = [
        bo.evaluate_rejected_forward_outcome(row(score=7.2, min_score_threshold=7.5), [bo.Candle(2000,100,111,99,110,1)]),
        bo.evaluate_rejected_forward_outcome(row(score=3.0, min_score_threshold=7.5), [bo.Candle(2000,100,101,94,95,1)]),
    ]
    s = bo.build_low_score_forward_summary(rows)
    assert s["near_threshold_count"] == 1
    assert s["far_below_threshold_count"] == 1


def test_symbol_reject_summary_separates_too_choppy_and_weak_trend():
    rows = [
        bo.evaluate_rejected_forward_outcome(row(reject_reason="TOO_CHOPPY"), [bo.Candle(2000,100,111,99,110,1)]),
        bo.evaluate_rejected_forward_outcome(row(reject_reason="WEAK_TREND_AND_NO_RANGE_EDGE"), [bo.Candle(2000,100,101,94,95,1)]),
    ]
    s = bo.build_symbol_reject_forward_summary(rows)
    assert s["too_choppy_count"] == 1
    assert s["weak_trend_count"] == 1


def test_zero_accepted_evidence_quality_incomplete_forward_evidence_remains_partial_or_insufficient():
    z = bo.build_zero_accepted_root_cause_summary({"total_candidates": 1, "accepted_count": 0, "rejected_count": 1, "reject_reason_distribution": {"LOW_SCORE": 1}}, {}, {"low_score_count": 1}, {})
    assert z["evidence_quality"] in {"PARTIAL", "INSUFFICIENT"}


def test_low_score_637_threshold_75_is_far_not_near():
    rows = [bo.evaluate_rejected_forward_outcome(row(score=6.37, min_score_threshold=7.5), [bo.Candle(2000,100,111,99,110,1)])]
    s = bo.build_low_score_forward_summary(rows)
    assert s["near_threshold_count"] == 0
    assert s["far_below_threshold_count"] == 1


def test_low_score_72_threshold_75_is_near_under_five_percent_rule():
    rows = [bo.evaluate_rejected_forward_outcome(row(score=7.2, min_score_threshold=7.5), [bo.Candle(2000,100,111,99,110,1)])]
    s = bo.build_low_score_forward_summary(rows)
    assert s["near_threshold_count"] == 1
    assert s["far_below_threshold_count"] == 0
    assert "min_score_threshold * 0.05" in s["near_threshold_definition"]


def test_low_score_positive_gap_113_is_far():
    rows = [bo.evaluate_rejected_forward_outcome(row(score=6.37, min_score_threshold=7.5, score_gap_to_threshold=1.13), [bo.Candle(2000,100,111,99,110,1)])]
    s = bo.build_low_score_forward_summary(rows)
    assert s["far_below_threshold_count"] == 1
    assert s["low_score_gap_source_distribution"]["row.score_gap_to_threshold"] == 1


def test_would_accept_disabled_mean_shadow_r_uses_only_counterfactual_subset():
    rows = [
        bo.evaluate_rejected_forward_outcome(row(would_accept_if_low_score_disabled=True, score=7.2, spread_pct=0.0, expected_slippage_pct=0.0), [bo.Candle(2000,100,111,99,110,1)]),
        bo.evaluate_rejected_forward_outcome(row(would_accept_if_low_score_disabled=False, score=3.0), [bo.Candle(2000,100,101,94,95,1)]),
    ]
    s = bo.build_low_score_forward_summary(rows)
    assert s["would_accept_if_low_score_disabled_forward_evaluable_count"] == 1
    assert s["would_accept_if_low_score_disabled_would_tp_count"] == 1
    assert s["would_accept_if_low_score_disabled_would_sl_count"] == 0
    assert s["would_accept_if_low_score_disabled_mean_shadow_r"] > 0


def test_rejected_forward_outcome_preserves_low_score_threshold_metadata():
    out = bo.evaluate_rejected_forward_outcome(row(score=7.2, min_score_threshold=7.5, score_gap_to_threshold=0.3, score_threshold_source="row.min_required_score", score_scale_detected="0_10", score_threshold_scale_detected="0_10", threshold_scale_mismatch_detected=False, threshold_scale_correction_applied=False, would_accept_if_low_score_disabled=True), [bo.Candle(2000,100,111,99,110,1)])
    assert out["min_score_threshold"] == 7.5
    assert out["score_gap_to_threshold"] == 0.3
    assert out["score_threshold_source"] == "row.min_required_score"
    assert out["score_scale_detected"] == "0_10"
    assert out["score_threshold_scale_detected"] == "0_10"
    assert out["threshold_scale_mismatch_detected"] is False
    assert out["threshold_scale_correction_applied"] is False
    assert out["would_accept_if_low_score_disabled"] is True


def test_symbol_forward_rows_preserve_nested_selector_metrics():
    diagnostics = {"selector": {"inputs": {"chop_score": 0.82, "trend_strength": 0.18, "volume_24h_usdt": 12345}, "metrics": {"volatility_pct": 2.5, "candle_range_pct": 1.1, "spread_pct": 0.02, "liquidity_score": 0.7}, "sub_scores": {"range_edge_score": 0.44}, "reject_reasons": ["TOO_CHOPPY"]}}
    out = bo.evaluate_rejected_forward_outcome(row(reject_reason="TOO_CHOPPY", lifecycle_state="SYMBOL_REJECTED", side="N/A", entry="", sl="", tp="", spread_pct="", liquidity_score="", diagnostics=__import__('json').dumps(diagnostics)), [])
    assert out["metric_source"] == "DIAGNOSTICS_SELECTOR_INPUTS"
    assert out["chop_score"] == 0.82
    assert out["trend_strength"] == 0.18
    assert out["selector_volatility_pct"] == 2.5
    assert out["selector_candle_range_pct"] == 1.1
    assert out["selector_spread_pct"] == 0.02
    assert out["selector_liquidity_score"] == 0.7
    assert out["selector_volume_24h_usdt"] == 12345
    assert "TOO_CHOPPY" in out["selector_reject_reasons"]


def test_symbol_forward_summary_means_use_selector_metrics():
    diagnostics = {"selector": {"inputs": {"chop_score": 0.8, "trend_strength": 0.2}, "metrics": {}, "sub_scores": {}}}
    rows = [bo.evaluate_rejected_forward_outcome(row(reject_reason="TOO_CHOPPY", diagnostics=__import__('json').dumps(diagnostics)), [bo.Candle(2000,100,111,99,110,1)])]
    s = bo.build_symbol_reject_forward_summary(rows)
    assert s["mean_chop_score"] == 0.8
    assert s["mean_trend_strength"] == 0.2


def test_forward_evidence_quality_reasons_for_missing_low_score_gaps_and_symbol_metrics():
    low_rows = [bo.evaluate_rejected_forward_outcome(row(score="", min_score_threshold=""), [bo.Candle(2000,100,111,99,110,1)])]
    sym_rows = [bo.evaluate_rejected_forward_outcome(row(reject_reason="TOO_CHOPPY", lifecycle_state="SYMBOL_REJECTED", side="N/A", entry="", sl="", tp=""), [])]
    low_summary = bo.build_low_score_forward_summary(low_rows)
    sym_summary = bo.build_symbol_reject_forward_summary(sym_rows)
    reasons = []
    if low_summary["low_score_gap_source_distribution"].get("UNAVAILABLE", 0) > low_summary["low_score_count"] / 2:
        reasons.append("LOW_SCORE_FORWARD_GAP_UNAVAILABLE")
    if sym_summary["missing_market_structure_metric_count"] > sym_summary["symbol_reject_count"] / 2:
        reasons.append("SYMBOL_FORWARD_METRICS_UNAVAILABLE")
    evidence_quality = "PARTIAL" if reasons else "COMPLETE"
    assert evidence_quality == "PARTIAL"
    assert "LOW_SCORE_FORWARD_GAP_UNAVAILABLE" in reasons
    assert "SYMBOL_FORWARD_METRICS_UNAVAILABLE" in reasons
