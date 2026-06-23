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
        "orderbook_imbalance": 0.1,
        "spread_status": "MEASURED",
        "slippage_status": "MEASURED",
        "market_data_latency_status": "MEASURED",
        "liquidity_status": "MEASURED",
        "funding_status": "MEASURED",
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
    backtest, paper, backtest_ctx, paper_ctx = _pair(market, config={"MIN_RR": 1.1})
    assert backtest["status"] == paper["status"] == "rejected"
    assert backtest["reject_reason"] == paper["reject_reason"] == "LOW_EFFECTIVE_RR"


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
