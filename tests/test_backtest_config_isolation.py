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


def test_dashboard_backtest_defaults_consume_backtest_settings(monkeypatch):
    import pytest
    pytest.importorskip('fastapi')
    from alphaforge.dashboard.backtest_control import default_form_values, parse_backtest_form
    monkeypatch.setenv('ALPHAFORGE_BACKTEST_LAST_N_DAYS', '12')
    monkeypatch.setenv('ALPHAFORGE_BACKTEST_TIMEFRAME', '15m')
    monkeypatch.setenv('ALPHAFORGE_BACKTEST_TOP_N', '7')
    defaults = default_form_values()
    assert defaults['last_days'] == 12
    assert defaults['timeframe'] == '15m'
    assert defaults['max_symbols'] == 7
    req, errors = parse_backtest_form({'symbols': 'BTCUSDT', 'timeframe': defaults['timeframe']})
    assert not errors
    assert req.last_days == 12
    assert req.max_symbols == 7


def test_low_effective_rr_threshold_is_conservative_by_default():
    from alphaforge.config_registry import decision_filter_config
    cfg = decision_filter_config('BACKTEST')
    assert cfg['MIN_EFFECTIVE_RR'] >= 1.6
    assert cfg['MIN_RR'] >= 1.7
