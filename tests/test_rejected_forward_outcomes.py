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
