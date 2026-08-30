from alphaforge.multi_timeframe import closed_candles, evaluate_mtf_alignment

NOW = 10_000_000

def context(tf, direction, close=NOW, **extra):
    return {"timeframe": tf, "direction": direction, "last_closed_candle_ms": close,
            "evidence_status": "COMPLETE", **extra}

def aligned(direction="LONG"):
    return (context("1h", direction), context("15m", direction),
            context("1m", direction, trigger="CONFIRMED"))

def test_perfect_long_and_short_alignment():
    for direction in ("LONG", "SHORT"):
        result = evaluate_mtf_alignment(*aligned(direction), decision_ts_ms=NOW)
        assert result["aligned"] and result["direction"] == direction

def test_regime_setup_and_execution_mismatches_are_specific():
    r, s, e = aligned("SHORT")
    r["direction"] = "LONG"
    assert evaluate_mtf_alignment(r, s, e, decision_ts_ms=NOW)["reasons"] == ["MTF_REGIME_SETUP_MISMATCH"]
    r, s, e = aligned("LONG"); e["direction"] = "SHORT"
    assert evaluate_mtf_alignment(r, s, e, decision_ts_ms=NOW)["reasons"] == ["MTF_SETUP_EXECUTION_MISMATCH"]

def test_missing_layers_no_setup_and_stale_fail_closed():
    r, s, e = aligned()
    assert "MTF_REGIME_UNAVAILABLE" in evaluate_mtf_alignment(None, s, e, decision_ts_ms=NOW)["reasons"]
    assert "MTF_SETUP_UNAVAILABLE" in evaluate_mtf_alignment(r, None, e, decision_ts_ms=NOW)["reasons"]
    assert "MTF_EXECUTION_UNAVAILABLE" in evaluate_mtf_alignment(r, s, None, decision_ts_ms=NOW)["reasons"]
    s["direction"] = "NONE"
    assert "MTF_NO_VALID_SETUP" in evaluate_mtf_alignment(r, s, e, decision_ts_ms=NOW)["reasons"]
    r, s, e = aligned(); r["last_closed_candle_ms"] = NOW - 8_000_000
    assert "MTF_CONTEXT_STALE" in evaluate_mtf_alignment(r, s, e, decision_ts_ms=NOW)["reasons"]

def test_partial_and_future_candles_cannot_contaminate_decision():
    rows = [[0,"1","2",".5","1.5","10",9999], [5000,"1.5","99","1","98","10",15000]]
    selected = closed_candles(rows, timeframe="1h", decision_ts_ms=10_000)
    assert len(selected) == 1 and selected[0]["close"] == 1.5
