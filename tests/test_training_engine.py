from alphaforge.training_engine import (
    review_executed_trade,
    review_rejected_setup,
    summarize_decision_reviews,
)


def test_losing_trade_with_high_slippage_gets_warning():
    r = review_executed_trade(
        {
            "decision_id": "d1",
            "symbol": "BTCUSDT",
            "setup_type": "BREAKOUT",
            "regime": "TREND",
            "entry": 100,
            "sl": 99,
            "tp": 103,
            "gross_pnl_pct": -0.002,
            "slippage_pct": 0.005,
            "spread_cost_pct": 0.001,
            "fees_pct": 0.001,
            "expected_slippage_pct": 0.001,
        },
        {"latency_ms": 80, "regime": "TREND"},
    )
    assert "SLIPPAGE_EXCEEDED_EXPECTED" in r.warnings


def test_profitable_gross_can_become_bad_net_after_costs():
    r = review_executed_trade(
        {
            "decision_id": "d2",
            "symbol": "ETHUSDT",
            "setup_type": "MEAN_REVERT",
            "regime": "RANGE",
            "entry": 100,
            "sl": 99,
            "tp": 101,
            "gross_pnl_pct": 0.004,
            "slippage_pct": 0.003,
            "spread_cost_pct": 0.002,
            "fees_pct": 0.001,
        },
        {"latency_ms": 100},
    )
    assert r.gross_pnl_pct > 0
    assert r.net_pnl_pct < 0


def test_rejected_setup_later_dumps_is_avoided_loser():
    r = review_rejected_setup(
        {"decision_id": "r1", "symbol": "SOLUSDT", "setup_type": "BREAKOUT", "regime": "CHOP", "decision_type": "SIGNAL_REJECTED"},
        {"sl_like_move_pct": 0.02, "tp_like_move_pct": 0.0},
    )
    assert r.outcome == "AVOIDED_LOSER"
    assert r.reject_quality == "CORRECT_REJECT"


def test_rejected_setup_later_rallies_is_missed_winner():
    r = review_rejected_setup(
        {"decision_id": "r2", "symbol": "ADAUSDT", "setup_type": "PULLBACK", "regime": "TREND", "decision_type": "ORDER_REJECTED"},
        {"sl_like_move_pct": 0.0, "tp_like_move_pct": 0.015},
    )
    assert r.outcome == "MISSED_WINNER"
    assert r.reject_quality == "BAD_REJECT"


def test_aggregation_identifies_weak_regime_and_symbol():
    reviews = [
        review_executed_trade({"decision_id": "1", "symbol": "X", "setup_type": "A", "regime": "TREND", "gross_pnl_pct": -0.02, "spread_cost_pct": 0.001, "slippage_pct": 0.001}, {}),
        review_executed_trade({"decision_id": "2", "symbol": "Y", "setup_type": "A", "regime": "TREND", "gross_pnl_pct": 0.03, "spread_cost_pct": 0.001, "slippage_pct": 0.001}, {}),
        review_executed_trade({"decision_id": "3", "symbol": "X", "setup_type": "B", "regime": "CHOP", "gross_pnl_pct": -0.01, "spread_cost_pct": 0.001, "slippage_pct": 0.001}, {}),
    ]
    s = summarize_decision_reviews(reviews)
    assert "CHOP" in s["regimes_to_disable"]
    assert "X" in s["worst_symbols"]


def test_output_is_deterministic():
    reviews = [
        review_executed_trade({"decision_id": "1", "symbol": "B", "setup_type": "A", "regime": "TREND", "gross_pnl_pct": 0.01}, {}),
        review_executed_trade({"decision_id": "2", "symbol": "A", "setup_type": "A", "regime": "TREND", "gross_pnl_pct": -0.01}, {}),
    ]
    s1 = summarize_decision_reviews(reviews)
    s2 = summarize_decision_reviews(reviews)
    assert s1 == s2
