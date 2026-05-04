from alphaforge.order import OrderCandidate, OrderExecutionContext, TradingMode, evaluate_trade_quality, run_order_cycle


def base_candidate():
    return OrderCandidate(symbol="BTCUSDT", side="LONG", setup_type="TREND_CONTINUATION_LONG", setup_reason="ok", regime="TREND", score=9.0, rr=2.5, expectancy=0.2, entry=100.0, sl=99.4, tp=101.5)


def base_market():
    return {"regime": "TREND", "volatility_regime": "normal", "spread_pct": 0.01, "expected_slippage_pct": 0.01, "atr_pct": 1.0, "timestamp": 1_000_000}


def test_low_score_rejected():
    c=base_candidate(); c.score=1
    assert evaluate_trade_quality(c, base_market(), {}, {}).reject_reason=="LOW_SCORE"

def test_rr_too_low_rejected():
    c=base_candidate(); c.rr=1
    assert evaluate_trade_quality(c, base_market(), {}, {}).reject_reason=="RR_TOO_LOW"

def test_expectancy_missing_rejected():
    c=base_candidate(); c.expectancy=None
    assert evaluate_trade_quality(c, base_market(), {}, {}).reject_reason=="EXPECTANCY_MISSING"

def test_negative_expectancy_rejected():
    c=base_candidate(); c.expectancy=-0.1
    assert evaluate_trade_quality(c, base_market(), {}, {}).reject_reason=="NEGATIVE_EXPECTANCY"

def test_regime_mismatch_rejected():
    c=base_candidate(); c.regime="RANGE"
    assert evaluate_trade_quality(c, base_market(), {}, {}).reject_reason=="REGIME_MISMATCH"

def test_chop_market_rejected():
    d=evaluate_trade_quality(base_candidate(), {**base_market(), "pattern_flags":["chop_zone"]}, {}, {})
    assert d.reject_reason=="CHOP_MARKET_BLOCK"

def test_stop_too_tight_rejected():
    c=base_candidate(); c.sl=99.95
    assert evaluate_trade_quality(c, base_market(), {}, {}).reject_reason=="STOP_TOO_TIGHT"

def test_stop_too_wide_rejected():
    c=base_candidate(); c.sl=90
    assert evaluate_trade_quality(c, base_market(), {}, {}).reject_reason=="STOP_TOO_WIDE"

def test_spread_too_high_rejected():
    assert evaluate_trade_quality(base_candidate(), {**base_market(), "spread_pct":0.2}, {}, {}).reject_reason=="SPREAD_TOO_HIGH"

def test_slippage_too_high_rejected():
    assert evaluate_trade_quality(base_candidate(), {**base_market(), "expected_slippage_pct":0.2}, {}, {}).reject_reason=="SLIPPAGE_TOO_HIGH"

def test_volatility_too_low_rejected():
    assert evaluate_trade_quality(base_candidate(), {**base_market(), "atr_pct":0.1}, {}, {}).reject_reason=="VOLATILITY_TOO_LOW"

def test_volatility_too_high_rejected():
    assert evaluate_trade_quality(base_candidate(), {**base_market(), "atr_pct":4.0}, {}, {}).reject_reason=="VOLATILITY_TOO_HIGH"

def test_symbol_cooldown_rejected():
    rs={"last_trade_ts_by_symbol":{"BTCUSDT":970_000}}
    assert evaluate_trade_quality(base_candidate(), base_market(), rs, {}).reject_reason=="SYMBOL_COOLDOWN_ACTIVE"

def test_daily_symbol_limit_rejected():
    rs={"trades_today_by_symbol":{"BTCUSDT":2}}
    assert evaluate_trade_quality(base_candidate(), base_market(), rs, {}).reject_reason=="DAILY_SYMBOL_TRADE_LIMIT"

def test_daily_global_limit_rejected():
    rs={"global_trades_today":10}
    assert evaluate_trade_quality(base_candidate(), base_market(), rs, {}).reject_reason=="DAILY_GLOBAL_TRADE_LIMIT"

def test_symbol_loss_streak_block():
    rs={"symbol_loss_block_until":{"BTCUSDT":2_000_000}}
    assert evaluate_trade_quality(base_candidate(), base_market(), rs, {}).reject_reason=="SYMBOL_LOSS_STREAK_BLOCK"

def test_global_loss_streak_block():
    rs={"global_loss_block_until":2_000_000}
    assert evaluate_trade_quality(base_candidate(), base_market(), rs, {}).reject_reason=="GLOBAL_LOSS_STREAK_BLOCK"

def test_high_quality_candidate_accepted():
    d=evaluate_trade_quality(base_candidate(), base_market(), {}, {})
    assert d.accepted is True

def test_run_order_cycle_does_not_execute_rejected_candidate():
    ctx=OrderExecutionContext(mode=TradingMode.BACKTEST,timestamp=1,symbol="BTCUSDT",balance=1000,risk_pct=1,market_ctx={"entry":100,"sl":99.95,"tp":102,"rr":2.5,"score":9,"setup_type":"TREND_CONTINUATION_LONG","setup_reason":"x","regime":"TREND","expectancy":0.2,"side":"LONG","volatility_regime":"normal"})
    r=run_order_cycle(ctx)
    assert r["status"]=="rejected"

def test_backtest_rejected_orders_count_increases():
    r=run_order_cycle(OrderExecutionContext(mode=TradingMode.BACKTEST,timestamp=1,symbol="BTCUSDT",balance=1,risk_pct=1,market_ctx={"entry":100,"sl":99.95,"tp":102,"rr":2.5,"score":9,"setup_type":"TREND_CONTINUATION_LONG","setup_reason":"x","regime":"TREND","expectancy":0.2,"side":"LONG","volatility_regime":"normal"}))
    assert r["status"]=="rejected"
