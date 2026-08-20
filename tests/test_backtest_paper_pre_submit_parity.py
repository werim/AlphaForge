from alphaforge.execution import build_execution_context
from alphaforge.order import (
    OrderExecutionContext,
    TradingMode,
    evaluate_paper_style_pre_submit,
)


def _market(**overrides):
    base = {
        "entry": 100.0,
        "sl": 99.0,
        "tp": 103.0,
        "side": "LONG",
        "score": 9.0,
        "rr": 2.0,
        "expectancy": 0.2,
        "setup_type": "BREAKOUT_UP",
        "setup_reason": "TEST",
        "regime": "TREND",
        "volatility_regime": "normal",
        "atr_pct": 0.5,
        "spread_pct": 0.0005,
        "expected_slippage_pct": 0.0005,
        "latency_ms": 50.0,
        "market_data_latency_ms": 50.0,
        "liquidity_score": 0.9,
        "funding_rate_pct": 0.00001,
        "fee_pct": 0.0004,
        "fee_source": "CONFIGURED_PAPER_ASSUMPTION",
        "orderbook_imbalance": 0.1,
        "spread_status": "MEASURED",
        "slippage_status": "MEASURED",
        "market_data_latency_status": "MEASURED",
        "liquidity_status": "MEASURED",
        "funding_status": "MEASURED",
        "fee_status": "CONFIGURED",
        "orderbook_status": "MEASURED",
        "volatility_status": "MEASURED",
    }
    base.update(overrides)
    base["execution_ctx"] = build_execution_context(base)
    return base


def _decision(mode, market_ctx, config=None):
    ctx = OrderExecutionContext(
        mode=mode,
        timestamp=1,
        symbol="BTCUSDT",
        balance=1000.0,
        risk_pct=1.0,
        market_ctx=market_ctx,
    )
    return evaluate_paper_style_pre_submit(ctx, config=config or {}, recent_stats={}), ctx


def _pair(market_ctx, config=None):
    backtest, backtest_ctx = _decision(TradingMode.BACKTEST, dict(market_ctx), config=config)
    paper, paper_ctx = _decision(TradingMode.PAPER, dict(market_ctx), config=config)
    return backtest, paper, backtest_ctx, paper_ctx


def test_backtest_paper_parity_low_score():
    backtest, paper, backtest_ctx, paper_ctx = _pair(_market(score=1.0))
    assert backtest["status"] == paper["status"] == "rejected"
    assert backtest["reject_reason"] == paper["reject_reason"] == "LOW_SCORE"


def test_backtest_paper_parity_low_effective_rr():
    market = _market(rr=1.15, spread_pct=0.003, expectancy=0.2)
    market["execution_ctx"] = build_execution_context(market)
    backtest, paper, backtest_ctx, paper_ctx = _pair(market, config={"MIN_RR": 1.1, "MAX_SPREAD_PCT": 0.02})
    assert backtest["status"] == paper["status"] == "rejected"
    assert backtest["reject_reason"] == paper["reject_reason"] == "RR_TOO_LOW"


def test_backtest_paper_parity_expectancy_missing():
    backtest, paper, backtest_ctx, paper_ctx = _pair(_market(expectancy=None))
    assert backtest["status"] == paper["status"] == "rejected"
    assert backtest["reject_reason"] == paper["reject_reason"] == "EXPECTANCY_MISSING"


def test_backtest_paper_parity_high_spread():
    market = _market(spread_pct=0.01)
    market["execution_ctx"] = build_execution_context(market)
    backtest, paper, backtest_ctx, paper_ctx = _pair(market, config={"MAX_SPREAD_PCT": 0.02})
    assert backtest["status"] == paper["status"] == "rejected"
    assert backtest["reject_reason"] == paper["reject_reason"] == "HIGH_SPREAD"


def test_backtest_paper_parity_accepted_candidate_lifecycle_sequence():
    backtest, paper, backtest_ctx, paper_ctx = _pair(_market())
    assert backtest["status"] == paper["status"] == "executed"
    assert [e["status_after"] for e in backtest_ctx.storage["audit"]] == ["ORDER_PLACED"]
    assert [e["status_after"] for e in paper_ctx.storage["audit"]] == ["ORDER_PLACED"]
    assert backtest["accepted"] is paper["accepted"] is True


def test_backtest_paper_parity_rejected_candidate_lifecycle_sequence():
    backtest, paper, backtest_ctx, paper_ctx = _pair(_market(score=1.0))
    assert backtest["status"] == paper["status"] == "rejected"
    assert backtest["reject_reason"] == paper["reject_reason"] == "LOW_SCORE"
    assert [e["status_after"] for e in backtest_ctx.storage["audit"]] == ["SIGNAL_REJECTED"]
    assert [e["status_after"] for e in paper_ctx.storage["audit"]] == ["SIGNAL_REJECTED"]


def test_shared_decision_boundary_shape_and_parity():
    from alphaforge.order import evaluate_signal_decision

    market = _market(score=9.0, rr=2.4, expectancy=0.3, volume_24h_usdt=123456.0)
    backtest = evaluate_signal_decision(market, {}, {"balance": 1000, "risk_pct": 1}, market["execution_ctx"], TradingMode.BACKTEST)
    paper = evaluate_signal_decision(market, {}, {"balance": 1000, "risk_pct": 1}, market["execution_ctx"], TradingMode.PAPER)
    assert backtest.decision == paper.decision == "ACCEPT"
    assert backtest.lifecycle_events == paper.lifecycle_events == ("SIGNAL_CREATED", "WAITING_ENTRY_ZONE", "ENTRY_TRIGGERED", "ORDER_PLACED")
    assert backtest.raw_rr == paper.raw_rr == 2.4
    assert backtest.effective_rr == paper.effective_rr
    assert backtest.volume_24h_usdt == 123456.0
    assert backtest.liquidity_status == "MEASURED"


def test_shared_decision_boundary_rejects_low_score_in_backtest():
    from alphaforge.order import evaluate_signal_decision

    decision = evaluate_signal_decision(_market(score=1.0), {}, {"balance": 1000, "risk_pct": 1}, None, TradingMode.BACKTEST)
    assert decision.decision == "REJECT"
    assert decision.reject_reason == "LOW_SCORE"
    assert decision.lifecycle_events == ("SIGNAL_CREATED", "SIGNAL_REJECTED")


def test_shared_decision_boundary_score_varies_with_snapshot_inputs():
    from alphaforge.order import evaluate_signal_decision

    weak = evaluate_signal_decision(_market(score=2.0), {}, {"balance": 1000, "risk_pct": 1}, None, TradingMode.BACKTEST)
    strong = evaluate_signal_decision(_market(score=9.0), {}, {"balance": 1000, "risk_pct": 1}, None, TradingMode.BACKTEST)
    assert weak.score != strong.score
    assert weak.raw_rr == strong.raw_rr == 2.0


def test_backtest_offline_funding_unavailable_not_fake_zero():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("backtest_order", Path(__file__).resolve().parents[1] / "backtest_order.py")
    bo = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(bo)

    prev = bo.Candle(1, 100, 101, 99, 100, 10)
    now = bo.Candle(2, 100, 102, 99, 101, 10)
    ctx = bo._build_market_ctx(now, prev, {}, [prev, now])
    assert ctx["funding_rate_pct"] is None
    assert ctx["funding_status"] == "UNAVAILABLE_BACKTEST"


def test_accepted_shared_decision_does_not_emit_order_audit():
    from alphaforge.order import evaluate_signal_decision

    storage = {"audit": []}
    decision = evaluate_signal_decision(
        _market(score=9.0, rr=2.4, expectancy=0.3),
        {},
        {"balance": 1000, "risk_pct": 1, "storage": storage},
        None,
        TradingMode.BACKTEST,
    )
    assert decision.decision == "ACCEPT"
    assert storage["audit"] == []


def test_shared_decision_boundary_score_varies_from_market_snapshot_shape():
    import importlib.util
    from pathlib import Path
    from alphaforge.order import evaluate_signal_decision

    spec = importlib.util.spec_from_file_location("backtest_order", Path(__file__).resolve().parents[1] / "backtest_order.py")
    bo = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(bo)

    calm_prev = bo.Candle(1, 100, 101, 99, 100, 100)
    calm_now = bo.Candle(2, 100, 101.1, 99.9, 100.1, 100)
    breakout_prev = bo.Candle(3, 100, 101, 99, 100, 100)
    breakout_now = bo.Candle(4, 100, 110, 99.5, 109, 100)
    calm_ctx = bo._build_market_ctx(calm_now, calm_prev, {"quoteVolume": 100000000, "fundingRate": 0.00001}, [calm_prev, calm_now])
    breakout_ctx = bo._build_market_ctx(breakout_now, breakout_prev, {"quoteVolume": 100000000, "fundingRate": 0.00001}, [breakout_prev, breakout_now])

    calm = evaluate_signal_decision(calm_ctx, {}, {"balance": 1000, "risk_pct": 1}, calm_ctx.get("execution_ctx"), TradingMode.BACKTEST)
    breakout = evaluate_signal_decision(breakout_ctx, {}, {"balance": 1000, "risk_pct": 1}, breakout_ctx.get("execution_ctx"), TradingMode.BACKTEST)
    assert calm_ctx["score"] != breakout_ctx["score"]
    assert calm.score != breakout.score


def test_backtest_scan_fails_closed_when_runtime_would_ignore_boundary(monkeypatch):
    import importlib.util
    from pathlib import Path
    import alphaforge.order as order_mod

    spec = importlib.util.spec_from_file_location("backtest_order", Path(__file__).resolve().parents[1] / "backtest_order.py")
    bo = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(bo)

    def fake_boundary(*args, **kwargs):
        return order_mod.DecisionResult(
            symbol="AAAUSDT",
            side="LONG",
            decision="REJECT",
            score=1.0,
            raw_rr=2.0,
            effective_rr=2.0,
            reject_reason="LOW_SCORE",
            lifecycle_events=("SIGNAL_CREATED", "SIGNAL_REJECTED"),
            diagnostics={"score": 1.0, "rr": 2.0, "reject_reason": "LOW_SCORE"},
        )

    class _Mode:
        BACKTEST = order_mod.TradingMode.BACKTEST

    class _Ctx:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def fake_cycle(ctx, *args, **kwargs):
        class _C:
            side = "LONG"
            entry = ctx.market_ctx["entry"]
            sl = ctx.market_ctx["sl"]
            tp = ctx.market_ctx["tp"]
            rr = ctx.market_ctx["rr"]
            setup_type = ctx.market_ctx["setup_type"]
            setup_reason = ctx.market_ctx["setup_reason"]
            regime = ctx.market_ctx["regime"]
            score = ctx.market_ctx["score"]
            order_type = "LIMIT"
        return {"status": "executed", "candidate": _C()}

    monkeypatch.setattr(order_mod, "evaluate_signal_decision", fake_boundary)
    monkeypatch.setattr(bo, "_order_runtime", lambda: (_Ctx, _Mode, fake_cycle))
    candles = [bo.Candle(1, 1, 1.1, 0.9, 1.0, 1), bo.Candle(2, 1, 1.1, 0.9, 1.0, 1), bo.Candle(3, 1.05, 1.3, 1.0, 1.2, 1)]
    ctx = {"mode": "BACKTEST"}
    assert bo.scan_symbol_backtest("AAAUSDT", candles, 2, ctx) is None
    assert ctx["last_result"]["reject_reason"] == "DECISION_PARITY_MISMATCH"
    assert ctx["last_result"]["diagnostics"]["fail_closed"] is True
