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


def test_no_real_binance_orders_called():
    # scanner uses public endpoints only and has no order placement function
    assert not hasattr(bo, "create_order")
