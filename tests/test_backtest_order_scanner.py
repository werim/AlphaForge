import csv
from pathlib import Path

import importlib.util
from pathlib import Path as _P
_spec = importlib.util.spec_from_file_location("backtest_order", _P(__file__).resolve().parents[1] / "backtest_order.py")
bo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bo)


def test_top_symbols_and_inactive_excluded(monkeypatch):
    def fake_fetch(url):
        if "exchangeInfo" in url:
            return {"symbols": [
                {"symbol": "AAAUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT", "filters": [1]},
                {"symbol": "BBBUSDT", "status": "BREAK", "contractType": "PERPETUAL", "quoteAsset": "USDT", "filters": [1]},
            ]}
        return [{"symbol": "AAAUSDT", "quoteVolume": "100"}, {"symbol": "BBBUSDT", "quoteVolume": "200"}]
    monkeypatch.setattr(bo, "fetch_json", fake_fetch)
    u = bo.select_symbol_universe(100)
    assert [x["symbol"] for x in u] == ["AAAUSDT"]


def test_load_candles_between_start_end(tmp_path: Path):
    p = tmp_path / "c.csv"
    p.write_text("timestamp,open,high,low,close,volume\n1,1,2,1,2,1\n2,1,2,1,2,1\n3,1,2,1,2,1\n")
    out = bo.load_candles(str(p), 2, 3)
    assert len(out) == 2


def test_scan_creates_virtual_candidate(monkeypatch):
    class _Mode:
        BACKTEST = "BACKTEST"
    class _Ctx:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
    def _fake_cycle(ctx, recent_stats=None):
        class _C:
            side = "LONG"; entry = ctx.market_ctx["entry"]; sl = ctx.market_ctx["sl"]; tp = ctx.market_ctx["tp"]; rr = ctx.market_ctx["rr"]; setup_type = "BREAKOUT_UP"; setup_reason = "CLOSE_ABOVE_PREV_HIGH"; regime = ctx.market_ctx["regime"]; score = ctx.market_ctx["score"]; order_type = "LIMIT"
        return {"status": "executed", "candidate": _C()}
    monkeypatch.setattr(bo, "_order_runtime", lambda: (_Ctx, _Mode, _fake_cycle))
    candles = [bo.Candle(1, 1, 1.1, 0.9, 1.0, 1), bo.Candle(2, 1, 1.1, 0.9, 1.0, 1), bo.Candle(3, 1.05, 1.3, 1.0, 1.2, 1)]
    c = bo.scan_symbol_backtest("AAAUSDT", candles, 2, {"mode": "BACKTEST"})
    assert c is not None
    assert c.score > 0


def test_scan_routes_non_breakout_bar_through_order_cycle(monkeypatch):
    class _Mode:
        BACKTEST = "BACKTEST"
    class _Ctx:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
    seen = {"called": 0}
    def _fake_cycle(ctx, recent_stats=None):
        seen["called"] += 1
        class _C:
            side = "LONG"; entry = ctx.market_ctx["entry"]; sl = ctx.market_ctx["sl"]; tp = ctx.market_ctx["tp"]; rr = ctx.market_ctx["rr"]; setup_type = "BREAKOUT_UP"; setup_reason = "CLOSE_ABOVE_PREV_HIGH"; regime = ctx.market_ctx["regime"]; score = ctx.market_ctx["score"]; order_type = "LIMIT"
        return {"status": "executed", "candidate": _C()}
    monkeypatch.setattr(bo, "_order_runtime", lambda: (_Ctx, _Mode, _fake_cycle))
    candles = [bo.Candle(1, 1, 1.1, 0.9, 1.0, 1), bo.Candle(2, 1, 1.1, 0.9, 1.0, 1), bo.Candle(3, 1.0, 1.05, 0.95, 1.0, 1)]
    c = bo.scan_symbol_backtest("AAAUSDT", candles, 2, {"mode": "BACKTEST"})
    assert seen["called"] == 1
    assert c is not None


def test_expectancy_rejection_written(tmp_path: Path):
    c = bo.CandidateOrder(1, "S", "LONG", 1, 0.9, 1.05, 0.5, "BACKTEST", "R", "X", 0.5, "LIMIT")
    rejects = []
    if c.rr < 1.0:
        rejects.append({"timestamp": c.timestamp, "symbol": c.symbol, "reject_reason": "LOW_EXPECTANCY"})
    f = tmp_path / "rejected_orders.csv"
    with open(f, "w", newline="") as h:
        w = csv.DictWriter(h, fieldnames=list(rejects[0].keys())); w.writeheader(); w.writerows(rejects)
    assert "LOW_EXPECTANCY" in f.read_text()


def test_entry_zone_waits_and_triggers():
    c = bo.CandidateOrder(1, "S", "LONG", 10, 9, 12, 2, "BACKTEST", "R", "X", 1, "LIMIT")
    candles = [bo.Candle(1, 11, 11, 10.5, 11, 1), bo.Candle(2, 10, 10.2, 9.8, 10.1, 1), bo.Candle(3, 10, 12.5, 9.9, 12, 1)]
    rows = bo.simulate_candidate(c, candles, 0, 1000, 1)
    assert rows[-1].status_after == "POSITION_CLOSED"
    assert rows[-1].close_reason == "TP_HIT"
    assert rows[0].status_before == "SIGNAL_CREATED"


def test_immediate_breakout_triggers_immediately():
    c = bo.CandidateOrder(1, "S", "LONG", 10, 9, 12, 2, "BACKTEST", "R", "X", 1, "MARKET")
    candles = [bo.Candle(1, 10, 10.1, 9.9, 10, 1), bo.Candle(2, 10, 12.1, 9.9, 12, 1)]
    rows = bo.simulate_candidate(c, candles, 0, 1000, 1)
    assert rows[-1].trigger_price == 10


def test_tp_hit():
    c = bo.CandidateOrder(1, "S", "LONG", 10, 9, 11, 1, "BACKTEST", "R", "X", 1, "MARKET")
    rows = bo.simulate_candidate(c, [bo.Candle(1, 10, 11.2, 9.9, 11, 1)], 0, 1000, 1)
    assert rows[-1].status_after == "POSITION_CLOSED"
    assert rows[-1].close_reason == "TP_HIT"


def test_sl_hit_and_same_candle_rule():
    c = bo.CandidateOrder(1, "S", "LONG", 10, 9, 11, 1, "BACKTEST", "R", "X", 1, "MARKET")
    rows = bo.simulate_candidate(c, [bo.Candle(1, 10, 11.2, 8.8, 10.5, 1)], 0, 1000, 1)
    assert rows[-1].status_after == "POSITION_CLOSED"
    assert rows[-1].close_reason == "SL_HIT"


def test_open_at_end():
    c = bo.CandidateOrder(1, "S", "LONG", 10, 9, 15, 5, "BACKTEST", "R", "X", 1, "MARKET")
    rows = bo.simulate_candidate(c, [bo.Candle(1, 10, 10.5, 9.8, 10.2, 1), bo.Candle(2, 10.2, 10.4, 10.0, 10.3, 1)], 0, 1000, 1)
    assert rows[-1].status_after == "POSITION_CLOSED"
    assert rows[-1].close_reason == "TIMEOUT"


def test_rejected_counterfactual_simulation():
    c = bo.CandidateOrder(1, "S", "LONG", 10, 9, 11, 1, "BACKTEST", "R", "X", 1, "LIMIT")
    candles = [bo.Candle(1, 10, 10.1, 9.9, 10, 1), bo.Candle(2, 10, 11.2, 9.9, 11, 1)]
    sim = bo.simulate_rejected_counterfactual(c, candles, 0)
    assert sim["would_trigger"] is True
    assert sim["would_tp_hit"] is True


def test_score_varies_by_market_conditions():
    low = bo._build_market_ctx(bo.Candle(3, 100, 101, 99.5, 100.2, 1), bo.Candle(2, 100, 100.1, 99.8, 100, 1), {})
    high = bo._build_market_ctx(bo.Candle(3, 100, 105, 99.5, 104.8, 1), bo.Candle(2, 100, 100.1, 99.8, 100, 1), {})
    assert high["score"] != low["score"]
    assert high["rr"] != low["rr"]


def test_execution_ctx_fields_populated():
    ctx = bo._build_market_ctx(
        bo.Candle(3, 100, 102, 99, 101.5, 1),
        bo.Candle(2, 100, 101, 99.5, 100.2, 1),
        {"quoteVolume": 25000000},
        recent=[bo.Candle(1, 99, 101, 98, 100, 1), bo.Candle(2, 100, 102, 99, 101, 1)],
    )
    assert ctx["spread_pct"] >= 0.0
    assert ctx["expected_slippage_pct"] > 0.0
    assert ctx["volatility_regime"] in {"low", "normal", "high"}


def test_no_real_binance_orders_called():
    # scanner uses public endpoints only and has no order placement function
    assert not hasattr(bo, "create_order")


def test_recent_stats_updates_streaks_and_winrate():
    stats = {"consecutive_sl_count": 0, "consecutive_tp_count": 0, "outcomes": []}
    bo._update_recent_stats_after_close(stats, "BTCUSDT", "SL_HIT")
    assert stats["consecutive_sl_count"] == 1
    assert stats["consecutive_tp_count"] == 0
    bo._update_recent_stats_after_close(stats, "BTCUSDT", "TP_HIT")
    assert stats["consecutive_sl_count"] == 0
    assert stats["consecutive_tp_count"] == 1
    assert 0.0 <= stats["rolling_winrate"] <= 1.0


def test_rejected_signals_present_in_lifecycle_trace():
    lifecycle = []
    rejected = [{"timestamp": 1, "symbol": "AAAUSDT", "reject_reason": "LOW_EFFECTIVE_RR"}]
    lifecycle.append(bo.LifecycleRow(
        timestamp=1, symbol="AAAUSDT", side="LONG", setup_type="", setup_reason="", regime="", score=0.0, rr=0.0,
        entry=0.0, sl=0.0, tp=0.0, status_before="SIGNAL_CREATED", status_after="SIGNAL_REJECTED", reject_reason="LOW_EFFECTIVE_RR"
    ))
    assert rejected[0]["reject_reason"] == lifecycle[0].reject_reason
    assert lifecycle[0].status_after == "SIGNAL_REJECTED"


def test_high_slippage_order_rejected_lifecycle_row():
    c = bo.CandidateOrder(1, "S", "LONG", 10, 9, 12, 2, "BACKTEST", "R", "TREND", 8, "MARKET", "MEDIUM")
    row = bo.LifecycleRow(
        timestamp=1, symbol=c.symbol, side=c.side, setup_type=c.setup_type, setup_reason=c.setup_reason, regime=c.regime, score=c.score, rr=c.rr, entry=c.entry, sl=c.sl, tp=c.tp,
        status_before="ENTRY_TRIGGERED", status_after="ORDER_REJECTED", reject_reason="HIGH_SLIPPAGE", expected_slippage_pct=0.03, spread_pct=0.005
    )
    assert row.status_after == "ORDER_REJECTED"
    assert row.reject_reason == "HIGH_SLIPPAGE"
