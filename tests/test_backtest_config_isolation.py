from alphaforge.order import OrderCandidate, evaluate_trade_quality


def candidate():
    return OrderCandidate('BTCUSDT','LONG','GENERIC','unit','TREND',8.0,2.0,0.2,100.0,99.0,103.0)


def market():
    return {'timestamp': 10_000_000, 'spread_pct': 0.001, 'expected_slippage_pct': 0.001, 'atr_pct': 1.0}


def test_max_trades_global_does_not_affect_backtest_by_default():
    stats = {'global_trades_today': 50}
    low = evaluate_trade_quality(candidate(), market(), stats, {'MODE': 'BACKTEST', 'MAX_TRADES_GLOBAL_PER_DAY': 3})
    high = evaluate_trade_quality(candidate(), market(), stats, {'MODE': 'BACKTEST', 'MAX_TRADES_GLOBAL_PER_DAY': 100})
    assert (low.accepted, low.reject_reason) == (high.accepted, high.reject_reason)
    assert low.accepted


def test_max_trades_global_affects_paper_runtime_gate():
    stats = {'global_trades_today': 3}
    blocked = evaluate_trade_quality(candidate(), market(), stats, {'MODE': 'PAPER', 'MAX_TRADES_GLOBAL_PER_DAY': 3})
    allowed = evaluate_trade_quality(candidate(), market(), stats, {'MODE': 'PAPER', 'MAX_TRADES_GLOBAL_PER_DAY': 100})
    assert blocked.reject_reason == 'DAILY_GLOBAL_TRADE_LIMIT'
    assert allowed.accepted


def test_min_effective_rr_and_slippage_filters_change_decision():
    loose = evaluate_trade_quality(candidate(), market(), {}, {'MODE': 'BACKTEST', 'MAX_EXPECTED_SLIPPAGE_PCT': 0.01})
    strict = evaluate_trade_quality(candidate(), market(), {}, {'MODE': 'BACKTEST', 'MAX_EXPECTED_SLIPPAGE_PCT': 0.0001})
    assert loose.accepted
    assert strict.reject_reason == 'SLIPPAGE_TOO_HIGH'
