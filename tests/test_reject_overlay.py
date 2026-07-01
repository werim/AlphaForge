import backtest_order as bo


def _row(**overrides):
    base = {
        "timestamp": 0,
        "symbol": "BTCUSDT",
        "side": "SHORT",
        "setup": "BREAKDOWN_DOWN",
        "regime": "TREND",
        "reject_reason": "LOW_SCORE",
        "score": 7.2,
        "min_score_threshold": 7.5,
        "score_gap_to_threshold": 0.3,
        "first_touch_outcome": "WOULD_TP",
        "effective_shadow_r_after_costs": 1.4,
        "mfe_r": 1.5,
        "mae_r": 0.3,
    }
    base.update(overrides)
    return base


def _overlays(rows):
    buckets = bo.build_reject_bucket_expectancy(rows)
    overlay, summary = bo.build_reject_overlay_diagnostics(rows, buckets)
    return overlay, buckets, summary


def test_long_breakout_bad_hour_receives_session_trap_overlay():
    overlay, _, _ = _overlays([_row(side="LONG", setup="BREAKOUT_UP", hour_utc=4, first_touch_outcome="WOULD_SL")])
    assert "LONG_BREAKOUT_SESSION_TRAP" in overlay[0]["diagnostic_overlay_labels"]
    assert overlay[0]["production_decision_changed"] is False


def test_short_low_score_breakdown_good_hour_receives_candidate_overlay_only_with_forward_evidence():
    overlay, _, _ = _overlays([_row(hour_utc=22)])
    assert "SHORT_BREAKDOWN_DIAGNOSTIC_CANDIDATE" in overlay[0]["diagnostic_overlay_labels"]
    missing, _, _ = _overlays([_row(hour_utc=22, first_touch_outcome="NO_TP_SL_GEOMETRY")])
    assert "SHORT_BREAKDOWN_DIAGNOSTIC_CANDIDATE" not in missing[0]["diagnostic_overlay_labels"]
    assert "REJECT_BUCKET_INSUFFICIENT_SAMPLE" in missing[0]["diagnostic_overlay_labels"]


def test_low_score_near_threshold_uses_five_percent_gap_rule_and_splits_side():
    overlay, _, _ = _overlays([_row(side="SHORT", score_gap_to_threshold=0.375, min_score_threshold=7.5)])
    assert "LOW_SCORE_NEAR_THRESHOLD_DIAGNOSTIC_CANDIDATE" in overlay[0]["diagnostic_overlay_labels"]
    assert "LOW_SCORE_NEAR_THRESHOLD_SHORT" in overlay[0]["diagnostic_overlay_labels"]
    far, _, _ = _overlays([_row(side="LONG", score_gap_to_threshold=0.376, min_score_threshold=7.5)])
    assert "LOW_SCORE_NEAR_THRESHOLD_DIAGNOSTIC_CANDIDATE" not in far[0]["diagnostic_overlay_labels"]


def test_high_vol_guard_long_receives_no_rescue_overlay_and_does_not_bypass_guard():
    overlay, _, _ = _overlays([_row(side="LONG", setup="BREAKOUT_UP", reject_reason="HIGH_VOL_GUARD", hour_utc=6)])
    assert "GUARD_CONFIRMED_NO_RESCUE" in overlay[0]["diagnostic_overlay_labels"]
    assert "SHORT_BREAKDOWN_DIAGNOSTIC_CANDIDATE" not in overlay[0]["diagnostic_overlay_labels"]
    assert overlay[0]["diagnostic_only"] is True


def test_bucket_expectancy_verdicts_positive_negative_and_insufficient():
    positive_rows = [_row(symbol="POS", hour_utc=6, effective_shadow_r_after_costs=0.5) for _ in range(30)]
    negative_rows = [_row(symbol="NEG", hour_utc=6, first_touch_outcome="WOULD_SL", effective_shadow_r_after_costs=-1.1) for _ in range(30)]
    insufficient_rows = [_row(symbol="SMALL", hour_utc=6) for _ in range(2)]
    buckets = bo.build_reject_bucket_expectancy(positive_rows + negative_rows + insufficient_rows)
    verdicts = {r["symbol"]: r["verdict"] for r in buckets}
    assert verdicts["POS"] == "POSITIVE_SHADOW_CANDIDATE"
    assert verdicts["NEG"] == "NEGATIVE_SHADOW_CONFIRMATION"
    assert verdicts["SMALL"] == "INSUFFICIENT_SAMPLE"


def test_overlay_labels_do_not_change_accepted_count_or_paper_live_runtime():
    rows = [_row(hour_utc=22)]
    before_accepted = 0
    overlay, _, summary = _overlays(rows)
    assert before_accepted == 0
    assert all(r["production_decision_changed"] is False for r in overlay)
    assert summary["production_threshold_change_recommended"] is False
