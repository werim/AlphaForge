from alphaforge.portfolio_risk_engine import (
    PositionSizingDecision,
    PortfolioRiskSnapshot,
    calculate_position_size,
    evaluate_portfolio_risk,
)


def _base_open_positions():
    return [
        {"symbol": "BTCUSDT", "side": "LONG", "risk_pct": 2.0},
        {"symbol": "ETHUSDT", "side": "LONG", "risk_pct": 2.0},
        {"symbol": "SOLUSDT", "side": "LONG", "risk_pct": 1.5},
    ]


def test_correlated_btc_eth_sol_exposure_penalty():
    snap = evaluate_portfolio_risk(_base_open_positions(), {"equity": 10_000, "drawdown_pct": 1.0}, {"liquidity_score": 0.9, "volatility_ratio": 1.0})
    assert snap.correlated_exposure_pct == 5.5
    assert "CORRELATION_EXPOSURE" not in snap.warnings


def test_portfolio_heat_reduction():
    snap = evaluate_portfolio_risk(_base_open_positions(), {"equity": 10_000, "drawdown_pct": 1.0}, {"liquidity_score": 0.9, "volatility_ratio": 1.0})
    normal = calculate_position_size({"score": 9, "rr": 2.8, "base_size_pct": 1.2}, snap, {"liquidity_score": 0.9, "volatility_ratio": 1.0})

    hot_positions = _base_open_positions() + [{"symbol": "DOGEUSDT", "side": "LONG", "risk_pct": 8.0}]
    hot_snap = evaluate_portfolio_risk(hot_positions, {"equity": 10_000, "drawdown_pct": 1.0}, {"liquidity_score": 0.9, "volatility_ratio": 1.0})
    reduced = calculate_position_size({"score": 9, "rr": 2.8, "base_size_pct": 1.2}, hot_snap, {"liquidity_score": 0.9, "volatility_ratio": 1.0})

    assert normal.recommended_size_pct > reduced.recommended_size_pct


def test_drawdown_triggered_defensive_mode():
    snap = evaluate_portfolio_risk([], {"equity": 10_000, "drawdown_pct": 9.0}, {"liquidity_score": 0.9, "volatility_ratio": 1.0})
    assert snap.risk_state == "DEFENSIVE"


def test_lockdown_state_activation():
    snap = evaluate_portfolio_risk([], {"equity": 10_000, "drawdown_pct": 16.0}, {"liquidity_score": 0.9, "volatility_ratio": 1.0})
    assert snap.risk_state == "LOCKDOWN"


def test_liquidity_adjusted_size_reduction():
    snap = evaluate_portfolio_risk([], {"equity": 10_000, "drawdown_pct": 0.0}, {"liquidity_score": 0.9, "volatility_ratio": 1.0})
    good = calculate_position_size({"score": 8.5, "rr": 2.5, "base_size_pct": 1.0}, snap, {"liquidity_score": 0.95, "volatility_ratio": 1.0})
    bad = calculate_position_size({"score": 8.5, "rr": 2.5, "base_size_pct": 1.0}, snap, {"liquidity_score": 0.35, "volatility_ratio": 1.0})
    assert good.recommended_size_pct > bad.recommended_size_pct


def test_volatility_adjusted_size_reduction():
    snap = evaluate_portfolio_risk([], {"equity": 10_000, "drawdown_pct": 0.0}, {"liquidity_score": 0.95, "volatility_ratio": 1.0})
    low = calculate_position_size({"score": 8.0, "rr": 2.0, "base_size_pct": 1.0}, snap, {"liquidity_score": 0.95, "volatility_ratio": 1.0})
    high = calculate_position_size({"score": 8.0, "rr": 2.0, "base_size_pct": 1.0}, snap, {"liquidity_score": 0.95, "volatility_ratio": 1.5})
    assert low.recommended_size_pct > high.recommended_size_pct


def test_high_confidence_good_liquidity_gets_larger_size():
    snap = evaluate_portfolio_risk([], {"equity": 10_000, "drawdown_pct": 0.0}, {"liquidity_score": 1.0, "volatility_ratio": 1.0})
    high = calculate_position_size({"score": 9.8, "rr": 3.2, "base_size_pct": 1.3}, snap, {"liquidity_score": 0.98, "volatility_ratio": 1.0})
    weak = calculate_position_size({"score": 6.0, "rr": 1.3, "base_size_pct": 1.3}, snap, {"liquidity_score": 0.7, "volatility_ratio": 1.0})
    assert high.approved is True
    assert high.recommended_size_pct > weak.recommended_size_pct


def test_excessive_risk_causes_rejection():
    snap = evaluate_portfolio_risk(
        _base_open_positions(),
        {"equity": 10_000, "drawdown_pct": 18.0},
        {"liquidity_score": 0.2, "volatility_ratio": 1.8, "execution_risk": 0.9, "slippage_anomaly": True},
    )
    decision = calculate_position_size(
        {"score": 9.5, "rr": 3.0, "base_size_pct": 1.5, "requested_size_pct": 3.0},
        snap,
        {"liquidity_score": 0.2, "volatility_ratio": 1.8, "execution_risk": 0.9},
    )
    assert decision.approved is False
    assert "REGIME_UNSTABLE" in decision.reject_reasons


def test_deterministic_outputs():
    snap1 = evaluate_portfolio_risk(_base_open_positions(), {"equity": 10_000, "drawdown_pct": 3.0}, {"liquidity_score": 0.8, "volatility_ratio": 1.1})
    snap2 = evaluate_portfolio_risk(_base_open_positions(), {"equity": 10_000, "drawdown_pct": 3.0}, {"liquidity_score": 0.8, "volatility_ratio": 1.1})
    dec1 = calculate_position_size({"score": 8.0, "rr": 2.1, "base_size_pct": 1.0}, snap1, {"liquidity_score": 0.8, "volatility_ratio": 1.1})
    dec2 = calculate_position_size({"score": 8.0, "rr": 2.1, "base_size_pct": 1.0}, snap2, {"liquidity_score": 0.8, "volatility_ratio": 1.1})
    assert snap1 == snap2
    assert dec1 == dec2


def test_penalties_remain_bounded():
    snap = evaluate_portfolio_risk(_base_open_positions(), {"equity": 10_000, "drawdown_pct": 50.0}, {"liquidity_score": 0.0, "volatility_ratio": 3.0, "execution_risk": 1.0})
    decision = calculate_position_size({"score": 10.0, "rr": 5.0, "base_size_pct": 2.0}, snap, {"liquidity_score": 0.0, "volatility_ratio": 3.0, "execution_risk": 1.0, "spread_pct": 1.0, "expected_slippage_pct": 1.0})
    assert isinstance(snap, PortfolioRiskSnapshot)
    assert isinstance(decision, PositionSizingDecision)
    assert 0.0 <= decision.correlation_penalty <= 1.5
    assert 0.0 <= decision.liquidity_penalty <= 1.0
    assert 0.0 <= decision.volatility_penalty <= 1.2
    assert 0.0 <= decision.drawdown_penalty <= 1.2
    assert 0.0 <= decision.execution_penalty <= 1.2
    assert 0.0 <= decision.risk_multiplier <= 1.5
