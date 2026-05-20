from alphaforge.signal_contract import SignalCandidate, evaluate_signal_to_order


def _cand(score=8.0, rr=2.0):
    return SignalCandidate(
        signal_id="s1", symbol="BTCUSDT", side="LONG", setup_type="BREAKOUT_UP", setup_reason="X", regime="TREND", timestamp=1,
        entry=100.0, stop_loss=99.0, take_profit=102.0, raw_rr=rr, heuristic_score=score,
        features={"heuristic_score": score, "order_type": "LIMIT", "quantity": 1.0, "notional": 100.0, "risk_usdt": 10.0},
    )


def test_positive_edge_and_expected_r_creates_plan():
    d, p = evaluate_signal_to_order(_cand(), {"liquidity_score": 0.9, "spread_pct": 0.01}, {"alignment": 0.9}, {})
    assert d.probability_edge > 0
    assert d.expected_r > 0
    assert p is not None


def test_negative_expected_r_rejects():
    d, p = evaluate_signal_to_order(_cand(score=1.0, rr=1.0), {"liquidity_score": 0.1, "spread_pct": 0.2, "expected_slippage_pct": 0.1}, {"alignment": 0.1}, {"min_expected_r": 0.0})
    assert d.decision == "REJECTED"
    assert d.reject_reason in {"NEGATIVE_EXPECTANCY", "LOW_PROBABILITY_EDGE", "LOW_FILL_PROBABILITY"}
    assert p is None


def test_high_timeout_rejects_despite_rr():
    d, p = evaluate_signal_to_order(_cand(rr=3.0), {"expected_hold_minutes": 50}, {"alignment": 0.6}, {"max_p_timeout": 0.1})
    assert d.decision == "REJECTED"
    assert d.reject_reason == "HIGH_TIMEOUT_PROBABILITY"
    assert p is None


def test_low_p_fill_rejects():
    d, p = evaluate_signal_to_order(_cand(score=1.0), {"liquidity_score": 0.0, "spread_pct": 0.25}, {"alignment": 0.5}, {"min_p_fill": 0.8})
    assert d.decision == "REJECTED"
    assert d.reject_reason == "LOW_FILL_PROBABILITY"
    assert p is None


def test_heuristic_score_in_features_not_final_decision():
    c = _cand(score=9.0)
    d, _ = evaluate_signal_to_order(c, {"liquidity_score": 0.0, "spread_pct": 0.3}, {"alignment": 0.1}, {"min_p_fill": 0.9})
    assert "heuristic_score" in c.features
    assert d.decision == "REJECTED"


def test_backtest_and_runtime_can_call_shared_contract():
    c = _cand()
    d1, _ = evaluate_signal_to_order(c, {"liquidity_score": 0.8}, {"alignment": 0.8}, {})
    d2, _ = evaluate_signal_to_order(c, {"liquidity_score": 0.8}, {"alignment": 0.8}, {})
    assert d1.decision in {"ACCEPTED", "REJECTED"}
    assert d2.decision in {"ACCEPTED", "REJECTED"}
