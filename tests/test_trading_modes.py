from alphaforge.order import OrderExecutionContext, TradingMode, run_order_cycle


def _market_ctx():
    return {
        "entry": 100.0,
        "sl": 99.0,
        "tp": 102.0,
        "rr": 2.0,
        "score": 0.9,
        "setup_type": "BREAKOUT",
        "setup_reason": "TEST",
        "regime": "TREND",
        "expected_regime": "TREND",
        "expectancy": 0.2,
        "side": "LONG",
        "spread_pct": 0.0001,
    }


def test_backtest_never_calls_live_endpoints_by_default():
    called = {"order": 0, "balance": 0, "tg": 0}
    ctx = OrderExecutionContext(mode=TradingMode.BACKTEST, timestamp=1, symbol="BTCUSDT", balance=1000, risk_pct=1, market_ctx=_market_ctx(), storage={
        "binance_place_order": lambda c: called.__setitem__("order", called["order"] + 1),
        "real_balance_fetcher": lambda: called.__setitem__("balance", called["balance"] + 1),
        "telegram_sender": lambda m: called.__setitem__("tg", called["tg"] + 1),
    })
    run_order_cycle(ctx)
    assert called == {"order": 0, "balance": 0, "tg": 0}


def test_live_calls_execution_adapter():
    called = {"order": 0, "balance": 0}
    ctx = OrderExecutionContext(mode=TradingMode.LIVE, timestamp=1, symbol="BTCUSDT", balance=1000, risk_pct=1, allow_live_orders=True, market_ctx=_market_ctx(), storage={
        "binance_place_order": lambda c: {"id": 1, "ok": called.__setitem__("order", called["order"] + 1)},
        "real_balance_fetcher": lambda: called.__setitem__("balance", called["balance"] + 1) or 1000,
    })
    run_order_cycle(ctx)
    assert called == {"order": 1, "balance": 1}


def test_paper_uses_paper_balance():
    ctx = OrderExecutionContext(mode=TradingMode.PAPER, timestamp=1, symbol="BTCUSDT", balance=321.0, risk_pct=1, market_ctx=_market_ctx())
    result = run_order_cycle(ctx)
    assert result["execution"]["paper_balance"] == 321.0


def test_same_candidate_and_rejection_reason_across_modes_and_quality_filters():
    m = _market_ctx()
    b = OrderExecutionContext(mode=TradingMode.BACKTEST, timestamp=1, symbol="BTCUSDT", balance=1000, risk_pct=1, market_ctx=m)
    l = OrderExecutionContext(mode=TradingMode.LIVE, timestamp=1, symbol="BTCUSDT", balance=1000, risk_pct=1, allow_live_orders=True, market_ctx=m, storage={"binance_place_order": lambda c: {"ok": True}, "real_balance_fetcher": lambda: 1000})
    rb = run_order_cycle(b)
    rl = run_order_cycle(l)
    assert rb["candidate"] == rl["candidate"]

    bad = dict(m)
    bad["score"] = 0.1
    r1 = run_order_cycle(OrderExecutionContext(mode=TradingMode.BACKTEST, timestamp=1, symbol="BTCUSDT", balance=1, risk_pct=1, market_ctx=bad), {"MIN_TRADE_SCORE": 0.5})
    r2 = run_order_cycle(OrderExecutionContext(mode=TradingMode.LIVE, timestamp=1, symbol="BTCUSDT", balance=1, risk_pct=1, allow_live_orders=True, market_ctx=bad, storage={"binance_place_order": lambda c: {}, "real_balance_fetcher": lambda: 1}), {"MIN_TRADE_SCORE": 0.5})
    assert r1["reason"] == r2["reason"] == "SCORE_TOO_LOW"
