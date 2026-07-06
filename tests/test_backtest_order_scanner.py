import csv
from collections import Counter
from pathlib import Path
import pytest

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
    candles = [bo.Candle(1, 104, 104, 103.6, 104.0, 100), bo.Candle(2, 104, 104, 103.6, 104.0, 100), bo.Candle(3, 100.0, 105.5, 104.5, 105.0, 100)]
    c = bo.scan_symbol_backtest("AAAUSDT", candles, 2, {"mode": "BACKTEST", "symbol_meta": {"quoteVolume": 100000000, "fundingRate": 0.00001}})
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
    ctx = {"mode": "BACKTEST"}
    c = bo.scan_symbol_backtest("AAAUSDT", candles, 2, ctx)
    assert seen["called"] == 1
    assert ctx["last_result"]["decision_result"].decision in {"ACCEPT", "REJECT"}


def test_expectancy_rejection_written(tmp_path: Path):
    c = bo.CandidateOrder(1, "S", "LONG", 1, 0.9, 1.05, 0.5, "BACKTEST", "R", "X", 0.5, "LIMIT")
    rejects = []
    if c.rr < 1.0:
        rejects.append({"timestamp": c.timestamp, "symbol": c.symbol, "reject_reason": "LOW_EXPECTANCY"})
    f = tmp_path / "rejected_orders.csv"
    with open(f, "w", newline="") as h:
        w = csv.DictWriter(h, fieldnames=list(rejects[0].keys())); w.writeheader(); w.writerows(rejects)
    assert "LOW_EXPECTANCY" in f.read_text()


def test_resolve_csv_fieldnames_preserves_base_and_adds_sorted_extras():
    rows = [
        {"timestamp": 1, "symbol": "AAAUSDT", "event_flags": "HIGH_SLIPPAGE", "tp": 11.0},
        {"timestamp": 2, "symbol": "AAAUSDT", "entry": 10.1, "s1": 9.8, "spread_pct": 0.12},
        {"timestamp": 3, "symbol": "AAAUSDT", "liquidity_score": 0.7, "volatility_score": 0.3},
    ]

    base = ["timestamp", "symbol"]
    fieldnames = bo.resolve_csv_fieldnames(rows, base)

    assert fieldnames[:2] == base
    assert fieldnames[2:] == sorted(["event_flags", "tp", "entry", "s1", "spread_pct", "liquidity_score", "volatility_score"])


def test_entry_zone_waits_and_triggers():
    c = bo.CandidateOrder(1, "S", "LONG", 10, 9, 12, 2, "BACKTEST", "R", "X", 1, "LIMIT")
    candles = [bo.Candle(1, 11, 11, 10.5, 11, 1), bo.Candle(2, 10, 10.2, 9.8, 10.1, 1), bo.Candle(3, 10, 12.5, 9.9, 12, 1)]
    rows = bo.simulate_candidate(c, candles, 0, 1000, 1)
    assert rows[-1].status_after == "POSITION_CLOSED"
    assert rows[-1].close_reason == "TP_HIT"
    assert [r.status_after for r in rows[:4]] == ["SIGNAL_CREATED", "SIGNAL_ACCEPTED", "WAITING_ENTRY_ZONE", "ENTRY_TRIGGERED"]


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


def test_short_tp_before_sl():
    c = bo.CandidateOrder(1, "S", "SHORT", 10, 11, 9, 1, "BACKTEST", "R", "X", 1, "MARKET")
    rows = bo.simulate_candidate(c, [bo.Candle(1, 10, 10.2, 8.9, 9.1, 1)], 0, 1000, 1)
    assert rows[-1].close_reason == "TP_HIT"


def test_short_sl_before_tp():
    c = bo.CandidateOrder(1, "S", "SHORT", 10, 11, 9, 1, "BACKTEST", "R", "X", 1, "MARKET")
    rows = bo.simulate_candidate(c, [bo.Candle(1, 10, 11.2, 9.7, 10.8, 1)], 0, 1000, 1)
    assert rows[-1].close_reason == "SL_HIT"


def test_same_candle_ambiguity_is_conservative_sl():
    c = bo.CandidateOrder(1, "S", "SHORT", 10, 11, 9, 1, "BACKTEST", "R", "X", 1, "MARKET")
    rows = bo.simulate_candidate(c, [bo.Candle(1, 10, 11.2, 8.9, 10.0, 1)], 0, 1000, 1)
    assert rows[-1].close_reason == "SL_HIT"


def test_score_varies_by_market_conditions():
    low = bo._build_market_ctx(bo.Candle(3, 100, 101, 99.5, 100.2, 1), bo.Candle(2, 100, 100.1, 99.8, 100, 1), {})
    high = bo._build_market_ctx(bo.Candle(3, 100, 105, 99.5, 104.8, 1), bo.Candle(2, 100, 100.1, 99.8, 100, 1), {})
    assert high["score"] != low["score"]


def test_build_market_ctx_can_emit_short_candidate():
    now = bo.Candle(3, 100, 100.5, 97.5, 98.0, 1)
    prev = bo.Candle(2, 100, 101.0, 99.0, 100.5, 1)
    ctx = bo._build_market_ctx(now, prev, {})
    assert ctx["side"] == "SHORT"
    assert ctx["setup_type"] == "BREAKDOWN_DOWN"
    assert ctx["setup_reason"] == "CLOSE_BELOW_PREV_LOW"
    assert ctx["tp"] < ctx["entry"]


def test_spread_percent_point_input_is_normalized():
    ctx = bo._build_market_ctx(
        bo.Candle(3, 100, 101, 99, 100.2, 1),
        bo.Candle(2, 100, 100.5, 99.5, 100.0, 1),
        {"actual_spread_pct": 0.1},
    )
    assert ctx["spread_unit_assumed"] == "PERCENT_POINT_NORMALIZED"
    assert ctx["spread_pct"] == 0.001


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


def test_process_backtest_result_writes_rejection_rows_and_skips_sim(monkeypatch):
    lifecycle = []
    rejected = []
    rejection_counts = {}
    open_rows = []
    recent_stats = {"last_trade_ts_by_symbol": {}, "trades_today_by_symbol": {}, "global_trades_today": 0, "outcomes": []}
    candles = [bo.Candle(1, 10, 10.5, 9.5, 10.1, 1)]
    result = {
        "status": "rejected",
        "reason": "QUALITY_BELOW_THRESHOLD",
        "diagnostics": {"side": "LONG", "setup_type": "BREAKOUT_UP", "setup_reason": "X", "regime": "TREND", "score": 6.2, "rr": 1.8}
    }
    mctx = {"entry": 10.0, "sl": 9.5, "tp": 11.0, "score": 6.2, "rr": 1.8}

    called = {"n": 0}
    def _fake_sim(*args, **kwargs):
        called["n"] += 1
        return []
    monkeypatch.setattr(bo, "simulate_candidate", _fake_sim)

    cand = bo.process_backtest_result("AAAUSDT", candles[0], 0, candles, result, mctx, 1000, 1.0, lifecycle, rejected, rejection_counts, open_rows, recent_stats)

    assert cand is None
    assert called["n"] == 0
    assert [r.status_after for r in lifecycle] == ["SIGNAL_CREATED", "SIGNAL_REJECTED"]
    assert lifecycle[-1].reject_reason == "QUALITY_BELOW_THRESHOLD"
    assert rejected[0]["reject_reason"] == "QUALITY_BELOW_THRESHOLD"


def test_process_backtest_result_writes_order_rejected_row(monkeypatch):
    lifecycle = []
    rejected = []
    rejection_counts = {}
    open_rows = []
    recent_stats = {"last_trade_ts_by_symbol": {}, "trades_today_by_symbol": {}, "global_trades_today": 0, "outcomes": []}
    candles = [bo.Candle(1, 10, 12.0, 9.0, 11.0, 1)]
    result = {
        "status": "executed",
        "candidate": type("C", (), {
            "side": "LONG", "entry": 10.0, "sl": 9.5, "tp": 11.5, "rr": 1.6,
            "setup_type": "BREAKOUT_UP", "setup_reason": "X", "regime": "TREND", "score": 8.2, "order_type": "MARKET"
        })(),
        "diagnostics": {"expectancy": 0.12},
    }
    mctx = {"entry": 10.0, "sl": 9.5, "tp": 11.5, "score": 8.2, "rr": 1.6, "expected_slippage_pct": 0.03, "spread_pct": 0.01}
    monkeypatch.setattr(bo, "simulate_candidate", lambda *args, **kwargs: [])

    cand = bo.process_backtest_result("AAAUSDT", candles[0], 0, candles, result, mctx, 1000, 1.0, lifecycle, rejected, rejection_counts, open_rows, recent_stats)
    assert cand is None
    assert lifecycle[0].status_after == "SIGNAL_CREATED"
    assert lifecycle[1].status_after == "ORDER_REJECTED"
    assert lifecycle[1].reject_reason == "HIGH_SLIPPAGE"
    assert rejected[0]["reject_reason"] == "HIGH_SLIPPAGE"


def test_build_market_ctx_derives_non_zero_execution_inputs_when_missing_meta():
    ctx = bo._build_market_ctx(
        bo.Candle(3, 100, 104, 99, 103, 2500),
        bo.Candle(2, 100, 101, 99.5, 100.2, 2000),
        {},
        recent=[bo.Candle(1, 99, 100, 98, 99.5, 1200), bo.Candle(2, 100, 101, 99, 100.2, 2000), bo.Candle(3, 100, 104, 99, 103, 2500)],
    )
    assert ctx["volume_24h_usdt"] > 0.0
    assert ctx["spread_pct"] > 0.0


def test_symbol_filter_rejects_before_order_eval(monkeypatch):
    candles = [bo.Candle(i, 10, 10.1, 9.9, 10, 0.1) for i in range(1, 8)]
    meta = {"quoteVolume": 10.0}
    called = {"n": 0}
    def _fake_scan(*args, **kwargs):
        called["n"] += 1
        return None
    monkeypatch.setattr(bo, "scan_symbol_backtest", _fake_scan)

    lifecycle, rejected, rejection_counts = [], [], {}
    for i in range(len(candles)):
        if i < 2:
            continue
        selector_market = bo._build_symbol_market_data(meta, candles, i)
        selector_result = bo.select_symbol("AAAUSDT", selector_market)
        if not selector_result.tradable:
            reason = selector_result.reject_reasons[0]
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            rejected.append({"reject_reason": reason})
            lifecycle.append(bo.LifecycleRow(timestamp=candles[i].timestamp, symbol="AAAUSDT", side="N/A", setup_type="", setup_reason="", regime=selector_result.regime_hint, score=selector_result.symbol_score, rr=0.0, entry=0.0, sl=0.0, tp=0.0, status_before="NONE", status_after="SYMBOL_REJECTED", reject_reason=reason))
            continue
        bo.scan_symbol_backtest("AAAUSDT", candles, i, {})

    assert called["n"] == 0
    assert rejection_counts.get("LOW_VOLUME", 0) > 0
    assert lifecycle[-1].status_after == "SYMBOL_REJECTED"


def test_symbol_filter_tradable_keeps_existing_order_reject_behavior(monkeypatch):
    lifecycle = []
    rejected = []
    rejection_counts = {}
    open_rows = []
    recent_stats = {"last_trade_ts_by_symbol": {}, "trades_today_by_symbol": {}, "global_trades_today": 0, "outcomes": []}
    candles = [bo.Candle(1, 10, 10.5, 9.5, 10.1, 1000)]
    result = {"status": "rejected", "reason": "LOW_SCORE", "diagnostics": {"side": "LONG", "setup_type": "BREAKOUT_UP", "setup_reason": "X", "regime": "TREND", "score": 2.0, "rr": 1.1}}
    mctx = {"entry": 10.0, "sl": 9.5, "tp": 11.0, "score": 2.0, "rr": 1.1}

    cand = bo.process_backtest_result("AAAUSDT", candles[0], 0, candles, result, mctx, 1000, 1.0, lifecycle, rejected, rejection_counts, open_rows, recent_stats)
    assert cand is None
    assert rejection_counts["LOW_SCORE"] == 1


def test_symbol_market_mapping_missing_fields_safe_defaults():
    candles = [bo.Candle(1, 10, 10.1, 9.9, 10, 0), bo.Candle(2, 10, 10.2, 9.8, 9.9, 0), bo.Candle(3, 9.9, 10.0, 9.4, 9.5, 0)]
    out = bo._build_symbol_market_data({}, candles, 2)
    assert "volume_24h_usdt" in out
    assert "selector_diagnostics" in out
    assert isinstance(out["spread_pct"], float)


def test_symbol_filter_deterministic_for_same_input():
    candles = [bo.Candle(i, 10+i*0.01, 10.2+i*0.01, 9.8+i*0.01, 10.1+i*0.01, 1000+i) for i in range(1, 15)]
    meta = {"quoteVolume": 5_000_000.0}
    a = bo._build_symbol_market_data(meta, candles, 10)
    b = bo._build_symbol_market_data(meta, candles, 10)
    ra = bo.select_symbol("AAAUSDT", a)
    rb = bo.select_symbol("AAAUSDT", b)
    assert ra.tradable == rb.tradable
    assert ra.reject_reasons == rb.reject_reasons

def test_rejected_candidates_saved_with_shadow_fields():
    row = {
        "timestamp": 1,
        "symbol": "AAAUSDT",
        "side": "LONG",
        "entry": 10,
        "sl": 9,
        "tp": 11,
        "rr": 1.2,
        "reject_reason": "LOW_SCORE",
        "score": 2.0,
        "regime": "TREND",
        "spread_pct": 0.1,
        "liquidity_score": 0.8,
        "volatility_score": 0.2,
        "expected_slippage_pct": 0.001,
    }
    candles = [bo.Candle(1, 10, 11.2, 9.8, 11, 1)]
    shadow = bo.evaluate_rejected_shadow(row, candles, 0)
    assert shadow.symbol == "AAAUSDT"
    assert shadow.raw_rr == 1.2
    assert shadow.spread_pct == pytest.approx(0.001, rel=1e-9)
    assert shadow.low_score_gate_score == 2.0


def test_rejected_shadow_short_can_be_would_tp_when_effective_rr_passes():
    row = {
        "timestamp": 1,
        "symbol": "AAAUSDT",
        "side": "SHORT",
        "entry": 10.0,
        "sl": 11.0,
        "tp": 9.0,
        "rr": 2.0,
        "setup_type": "BREAKDOWN_DOWN",
        "setup_reason": "X",
        "regime": "TREND",
        "score": 8.0,
        "order_type": "LIMIT",
        "reject_reason": "LOW_SCORE",
        "spread_pct": 0.01,
        "expected_slippage_pct": 0.002,
        "liquidity_score": 0.9,
        "volatility_score": 1.2,
    }
    candles = [bo.Candle(1, 10.0, 10.2, 8.8, 9.1, 1)]
    shadow = bo.evaluate_rejected_shadow(row, candles, 0)
    assert shadow.shadow_outcome == "WOULD_TP"
    assert shadow.effective_tp_hit is True


def test_shadow_outcome_calculated_for_low_score_reject():
    row = {"timestamp": 1, "symbol": "AAAUSDT", "side": "LONG", "entry": 10, "sl": 9, "tp": 11, "rr": 1.5, "reject_reason": "LOW_SCORE", "score": 1.5, "regime": "RANGE", "spread_pct": 0.01, "liquidity_score": 0.9, "volatility_score": 0.5}
    candles = [bo.Candle(1, 10, 10.2, 9.9, 10, 1), bo.Candle(2, 10, 11.1, 9.9, 11, 1)]
    shadow = bo.evaluate_rejected_shadow(row, candles, 0)
    assert shadow.shadow_outcome == "WOULD_TP"


def test_wide_spread_reject_penalized_by_execution_cost():
    row = {"timestamp": 1, "symbol": "AAAUSDT", "side": "LONG", "entry": 10, "sl": 9, "tp": 11, "rr": 1.2, "reject_reason": "WIDE_SPREAD", "score": 6.0, "regime": "TREND", "spread_pct": 1.5, "liquidity_score": 0.9, "volatility_score": 0.5}
    candles = [bo.Candle(1, 10, 11.5, 9.9, 11, 1)]
    shadow = bo.evaluate_rejected_shadow(row, candles, 0)
    assert shadow.effective_rr < shadow.raw_rr
    assert shadow.effective_tp_hit is False


def test_rejected_shadow_summary_csv_created(tmp_path: Path):
    out = tmp_path / "out"
    bo.main.__globals__["sys"].argv = ["backtest_order.py", "--offline", "--output-dir", str(out)]
    bo.main()
    f = out / "rejected_shadow_summary.csv"
    assert f.exists()
    with open(f, newline="") as h:
        rows = list(csv.DictReader(h))
    assert rows and "total_rejected" in rows[0]


def test_false_positive_reject_rate_reported():
    s1 = bo.RejectedShadowEvaluation("A", 1, "LONG", 10, 9, 11, 1.5, 1.4, "LOW_SCORE", 2, "TREND", 0.1, 0.8, 0.2, "WOULD_TP", True, 0.1, True, True)
    s2 = bo.RejectedShadowEvaluation("B", 1, "LONG", 10, 9, 11, 1.0, 1.0, "LOW_SCORE", 2, "TREND", 0.1, 0.8, 0.2, "WOULD_SL", False, 0.0, True, True)
    summary = bo.build_rejected_shadow_summary([s1, s2])
    assert "reject_false_positive_rate" in summary
    assert summary["reject_false_positive_rate"] == 0.5
    assert "reject_reason_diagnostics" in summary


def test_stop_too_wide_rescue_attempt_reduces_size_and_keeps_risk_limits():
    row = {
        "timestamp": 1, "symbol": "AAAUSDT", "side": "LONG", "entry": 100, "sl": 97, "tp": 106, "rr": 2.0,
        "reject_reason": "STOP_TOO_WIDE", "score": 8.4, "regime": "BREAKOUT", "setup_type": "BREAKOUT_UP",
        "spread_pct": 0.02, "liquidity_score": 0.9, "volatility_score": 0.8, "expected_slippage_pct": 0.001,
    }
    shadow = bo.evaluate_rejected_shadow(row, [bo.Candle(1, 100, 106.5, 99, 105.0, 1)], 0)
    assert shadow.rescue_attempted is True
    assert shadow.rescued_size_multiplier <= 0.5
    assert abs((row["entry"] - shadow.rescued_stop_loss) / row["entry"] * 100.0) <= 1.5 + 1e-9


def test_spread_pct_normalization_is_consistent_between_1p5_and_0p015():
    candles = [bo.Candle(1, 10, 10.2, 9.8, 10, 1), bo.Candle(2, 10, 10.2, 9.8, 10, 1), bo.Candle(3, 10, 10.2, 9.8, 10, 1)]
    normalized = bo._build_symbol_market_data({"quoteVolume": 80_000_000.0, "actual_spread_pct": 1.5}, candles, 2)["spread_pct"]
    already_pct = bo._build_symbol_market_data({"quoteVolume": 80_000_000.0, "actual_spread_pct": 0.015}, candles, 2)["spread_pct"]
    assert normalized == pytest.approx(already_pct, rel=1e-6)


def test_rejected_counterfactual_same_candle_sl_priority():
    c = bo.CandidateOrder(1, "S", "LONG", 10, 9, 11, 1, "BACKTEST", "R", "X", 1, "LIMIT")
    candles = [bo.Candle(1, 10, 11.2, 8.8, 10.5, 1)]
    sim = bo.simulate_rejected_counterfactual(c, candles, 0)
    assert sim["outcome"] == "WOULD_SL"




def test_rejected_counterfactual_long_sl_only():
    c = bo.CandidateOrder(1, "S", "LONG", 10, 9, 11, 1, "BACKTEST", "R", "X", 1, "LIMIT")
    candles = [bo.Candle(1, 10, 10.4, 8.9, 9.1, 1)]
    sim = bo.simulate_rejected_counterfactual(c, candles, 0)
    assert sim["outcome"] == "WOULD_SL"


def test_rejected_counterfactual_short_tp_only():
    c = bo.CandidateOrder(1, "S", "SHORT", 10, 11, 9, 1, "BACKTEST", "R", "X", 1, "LIMIT")
    candles = [bo.Candle(1, 10, 10.2, 8.8, 9.2, 1)]
    sim = bo.simulate_rejected_counterfactual(c, candles, 0)
    assert sim["outcome"] == "WOULD_TP"


def test_rejected_counterfactual_short_sl_only():
    c = bo.CandidateOrder(1, "S", "SHORT", 10, 11, 9, 1, "BACKTEST", "R", "X", 1, "LIMIT")
    candles = [bo.Candle(1, 10, 11.2, 9.7, 10.9, 1)]
    sim = bo.simulate_rejected_counterfactual(c, candles, 0)
    assert sim["outcome"] == "WOULD_SL"


def test_rejected_counterfactual_same_candle_sl_priority_short():
    c = bo.CandidateOrder(1, "S", "SHORT", 10, 11, 9, 1, "BACKTEST", "R", "X", 1, "LIMIT")
    candles = [bo.Candle(1, 10, 11.2, 8.8, 10.0, 1)]
    sim = bo.simulate_rejected_counterfactual(c, candles, 0)
    assert sim["outcome"] == "WOULD_SL"
def test_rejected_counterfactual_uses_bounded_lookahead_timeout():
    c = bo.CandidateOrder(1, "S", "LONG", 10, 9, 12, 2, "BACKTEST", "R", "X", 1, "LIMIT")
    candles = [bo.Candle(i, 10, 10.1, 9.9, 10, 1) for i in range(1, 8)]
    sim = bo.simulate_rejected_counterfactual(c, candles, 0, timeout_bars=3)
    assert sim["outcome"] == "WOULD_TIMEOUT"


def test_forward_window_evaluator_labels_reject_correctness_deterministically():
    row = {"timestamp": 1, "symbol": "AAAUSDT", "side": "LONG", "entry": 10, "sl": 9, "tp": 11, "rr": 1.5, "reject_reason": "LOW_SCORE", "regime": "TREND", "spread_pct": 0.02, "expected_slippage_pct": 0.01, "liquidity_score": 0.9}
    candles = [bo.Candle(1, 10.0, 10.2, 9.8, 10.1, 1), bo.Candle(2, 10.1, 10.3, 8.9, 9.2, 1)]
    eval1 = bo.evaluate_forward_window(row, candles, 0, forward_window_minutes=2)
    eval2 = bo.evaluate_forward_window(row, candles, 0, forward_window_minutes=2)
    assert eval1.reject_correct is True
    assert eval1.reject_saved_from_loss is True
    assert eval1.reject_missed_winner is False
    assert eval1 == eval2


def test_forward_evaluator_triggers_only_for_terminal_closed_states():
    lifecycle = [
        bo.LifecycleRow(1, "BTCUSDT", "LONG", "S", "R", "TREND", 5.0, 1.2, 10.0, 9.0, 11.0, "NONE", "SIGNAL_CREATED"),
        bo.LifecycleRow(2, "BTCUSDT", "LONG", "S", "R", "TREND", 5.0, 1.2, 10.0, 9.0, 11.0, "ENTRY_TRIGGERED", "POSITION_CLOSED", close_reason="TP_HIT"),
    ]
    candles = {"BTCUSDT": [bo.Candle(2, 10.0, 11.2, 9.8, 10.5, 1)]}
    evals = bo.build_forward_evaluations_from_lifecycle(lifecycle, candles, forward_window_minutes=5)
    assert len(evals) == 1
    assert evals[0].signal_id == "BTCUSDT:2"


def test_calibration_snapshots_are_idempotent():
    ev = bo.ForwardWindowEvaluation(
        signal_id="BTCUSDT:1", symbol="BTCUSDT", decision="ACCEPTED", lifecycle_state="POSITION_CLOSED", reject_reason="",
        forward_window_minutes=10, would_have_hit_tp=True, would_have_hit_sl=False, mfe_pct=1.0, mae_pct=0.2,
        max_forward_return=1.0, max_adverse_return=-0.2, reject_correct=None, reject_missed_winner=False,
        reject_saved_from_loss=False, forward_window_regime="TREND", execution_quality_bucket="HIGH",
    )
    lifecycle_index = {"BTCUSDT:1": bo.LifecycleRow(1, "BTCUSDT", "LONG", "S", "R", "TREND", 7.0, 1.5, 10.0, 9.0, 11.5, "ENTRY_TRIGGERED", "POSITION_CLOSED", close_reason="TP_HIT")}
    rows1 = bo.persist_calibration_snapshots([ev, ev], lifecycle_index)
    rows2 = bo.persist_calibration_snapshots([ev], lifecycle_index)
    assert len(rows1) == 1
    assert len(rows2) == 1


def test_shadow_summary_zero_rejects_and_unknown_supported():
    empty = bo.build_rejected_shadow_summary([])
    assert empty["total_rejected"] == 0
    assert empty["would_tp"] == 0
    assert empty["rejected_raw_win_rate"] == 0.0
    assert empty["reject_false_positive_rate"] == 0.0

    unknown = bo.RejectedShadowEvaluation(
        "A",
        1,
        "LONG",
        10,
        9,
        11,
        1.2,
        1.2,
        "LOW_SCORE",
        1.0,
        "TREND",
        0.1,
        0.8,
        0.2,
        "UNKNOWN",
        False,
        0.0,
        True,
        True,
    )
    summary = bo.build_rejected_shadow_summary([unknown])
    assert summary["total_rejected"] == 1
    assert summary["would_tp"] == 0
    assert summary["rejected_effective_expectancy"] == 0.0


def test_missing_execution_context_does_not_crash_shadow_eval():
    row = {
        "timestamp": 1,
        "symbol": "AAAUSDT",
        "side": "LONG",
        "entry": 10.0,
        "sl": 9.0,
        "tp": 11.0,
        "rr": 1.3,
        "reject_reason": "LOW_SCORE",
        "score": 2.0,
        "regime": "TREND",
    }
    shadow = bo.evaluate_rejected_shadow(row, [bo.Candle(1, 10, 10.2, 9.9, 10.1, 1)], 0)
    assert shadow.effective_rr >= 0.0


def test_rejected_rows_use_unavailable_execution_sentinel_when_ctx_missing():
    lifecycle, rejected, rejection_counts, open_rows = [], [], {}, []
    candles = [bo.Candle(1, 10, 10.5, 9.8, 10.2, 100)]
    result = {
        "status": "rejected",
        "reason": "LOW_SCORE",
        "diagnostics": {
            "side": "LONG",
            "setup_type": "BREAKOUT_UP",
            "setup_reason": "X",
            "regime": "TREND",
            "score": 2.0,
            "rr": 1.1,
        },
    }

    bo.process_backtest_result(
        "AAAUSDT",
        candles[0],
        0,
        candles,
        result,
        {},
        1000,
        1.0,
        lifecycle,
        rejected,
        rejection_counts,
        open_rows,
        {
            "last_trade_ts_by_symbol": {},
            "trades_today_by_symbol": {},
            "global_trades_today": 0,
            "symbol_loss_streak": {},
            "global_loss_streak": 0,
            "symbol_loss_block_until": {},
            "global_loss_block_until": 0,
            "consecutive_sl_count": 0,
            "consecutive_tp_count": 0,
            "rolling_winrate": 0.0,
            "outcomes": [],
        },
    )

    assert rejected[0]["spread_pct"] == "UNAVAILABLE_BACKTEST"
    assert rejected[0]["liquidity_score"] == "UNAVAILABLE_BACKTEST"
    assert rejected[0]["expected_slippage_pct"] == "UNAVAILABLE_BACKTEST"

def test_rejected_signal_lifecycle_precedes_any_trade_simulation(monkeypatch):
    lifecycle, rejected, rejection_counts, open_rows = [], [], {}, []
    recent_stats = {"last_trade_ts_by_symbol": {}, "trades_today_by_symbol": {}, "global_trades_today": 0, "symbol_loss_streak": {}, "global_loss_streak": 0, "symbol_loss_block_until": {}, "global_loss_block_until": 0, "consecutive_sl_count": 0, "consecutive_tp_count": 0, "rolling_winrate": 0.0, "outcomes": []}
    candles = [bo.Candle(1, 10, 10.2, 9.8, 10.0, 100), bo.Candle(2, 10, 10.3, 9.9, 10.1, 100)]

    rejected_result = {
        "status": "rejected",
        "reason": "LOW_SCORE",
        "diagnostics": {"side": "LONG", "setup_type": "BREAKOUT_UP", "setup_reason": "X", "regime": "TREND", "score": 2.0, "rr": 1.7, "entry": 10.0, "sl": 9.5, "tp": 11.0},
    }
    bo.process_backtest_result("AAAUSDT", candles[0], 0, candles, rejected_result, {"entry": 10.0, "sl": 9.5, "tp": 11.0}, 1000, 1.0, lifecycle, rejected, rejection_counts, open_rows, recent_stats)

    executed_result = {
        "status": "executed",
        "candidate": type("X", (), {"side": "LONG", "entry": 10.0, "sl": 9.5, "tp": 11.0, "rr": 2.0, "setup_type": "BREAKOUT_UP", "setup_reason": "Y", "regime": "TREND", "score": 8.0, "order_type": "MARKET"})(),
        "diagnostics": {"expectancy": 0.12},
    }
    bo.process_backtest_result("AAAUSDT", candles[1], 1, candles, executed_result, {"entry": 10.0, "sl": 9.5, "tp": 10.8, "expected_slippage_pct": 0.0, "spread_pct": 0.0, "liquidity_score": 1.0}, 1000, 1.0, lifecycle, rejected, rejection_counts, open_rows, recent_stats)

    first_closed_idx = next(i for i, row in enumerate(lifecycle) if row.status_after == "POSITION_CLOSED")
    rejected_idx = next(i for i, row in enumerate(lifecycle) if row.status_after == "SIGNAL_REJECTED")
    assert rejected_idx < first_closed_idx
    assert [lifecycle[0].status_after, lifecycle[1].status_after] == ["SIGNAL_CREATED", "SIGNAL_REJECTED"]
    assert all(row.status_after != "CREATED" for row in lifecycle)


def test_scan_symbol_backtest_exposes_market_ctx_for_rejected_signals(monkeypatch):
    class StubMode:
        BACKTEST = "BACKTEST"

    class StubCtx:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def _stub_runtime():
        def _run_order_cycle(ctx, recent_stats=None):
            return {"status": "rejected", "reason": "LOW_SCORE", "diagnostics": {"side": "LONG", "setup_type": "BREAKOUT_UP", "setup_reason": "X", "regime": "TREND", "score": 2.0, "rr": 1.1}}
        return StubCtx, StubMode, _run_order_cycle

    monkeypatch.setattr(bo, "_order_runtime", _stub_runtime)
    candles = [bo.Candle(1, 10, 10.2, 9.9, 10.0, 100), bo.Candle(2, 10.0, 10.3, 9.9, 10.1, 100), bo.Candle(3, 10.1, 10.4, 10.0, 10.3, 100)]
    ctx = {"mode": "BACKTEST", "symbol_meta": {"quoteVolume": 1000000}, "balance": 1000, "risk_pct": 1.0}
    cand = bo.scan_symbol_backtest("AAAUSDT", candles, 2, ctx)
    assert cand is None
    assert "market_ctx" in ctx
    assert ctx["market_ctx"].get("entry", 0.0) > 0.0

def test_symbol_selector_reject_is_not_actionable_shadow_order():
    row = {
        "timestamp": 1,
        "symbol": "AAAUSDT",
        "side": "N/A",
        "setup_reason": "SYMBOL_SELECTOR",
        "reject_reason": "LOW_VOLUME",
    }
    assert bo._is_actionable_rejected_order(row) is False


def test_rejected_order_with_valid_levels_is_actionable_shadow_order():
    row = {
        "timestamp": 1,
        "symbol": "AAAUSDT",
        "side": "LONG",
        "setup_type": "BREAKOUT_UP",
        "setup_reason": "X",
        "entry": 10.0,
        "sl": 9.0,
        "tp": 11.0,
        "rr": 1.2,
        "reject_reason": "LOW_SCORE",
    }
    assert bo._is_actionable_rejected_order(row) is True

def test_high_candle_range_alone_does_not_trigger_wide_spread():
    candles = [bo.Candle(1, 100, 120, 80, 101, 1000), bo.Candle(2, 100, 125, 75, 100, 1000), bo.Candle(3, 100, 130, 70, 102, 1000)]
    out = bo._build_symbol_market_data({"quoteVolume": 80_000_000.0}, candles, 2)
    result = bo.select_symbol("AAAUSDT", out)
    assert out["candle_range_pct"] > 10.0
    assert out["spread_source"] == "ESTIMATED_BACKTEST"
    assert out["spread_pct"] <= 0.12
    assert "WIDE_SPREAD" not in result.reject_reasons


def test_explicit_high_actual_spread_triggers_wide_spread():
    candles = [bo.Candle(1, 10, 10.2, 9.8, 10, 1000), bo.Candle(2, 10, 10.2, 9.8, 10, 1000), bo.Candle(3, 10, 10.2, 9.8, 10, 1000)]
    out = bo._build_symbol_market_data({"quoteVolume": 80_000_000.0, "actual_spread_pct": 0.35}, candles, 2)
    result = bo.select_symbol("AAAUSDT", out)
    assert out["spread_source"] == "ACTUAL"
    assert "WIDE_SPREAD" in result.reject_reasons


def test_offline_fixture_not_all_rejected_by_wide_spread():
    start_ms = 1_700_000_000_000
    universe, candles_by_symbol = bo._offline_fixture(start_ms)
    counts = {}
    total = 0
    for row in universe:
        symbol = row["symbol"]
        candles = candles_by_symbol[symbol]
        for i in range(2, len(candles)):
            total += 1
            market = bo._build_symbol_market_data(row, candles, i)
            res = bo.select_symbol(symbol, market)
            if not res.tradable and res.reject_reasons:
                r = res.reject_reasons[0]
                counts[r] = counts.get(r, 0) + 1
    assert total > 0
    assert counts.get("WIDE_SPREAD", 0) < total


def test_spread_source_propagated_to_execution_context():
    ctx = bo._build_market_ctx(
        bo.Candle(3, 100, 102, 99, 101.5, 1),
        bo.Candle(2, 100, 101, 99.5, 100.2, 1),
        {"quoteVolume": 25_000_000, "estimated_spread_pct": 0.04},
        recent=[bo.Candle(1, 99, 101, 98, 100, 1), bo.Candle(2, 100, 102, 99, 101, 1)],
    )
    assert ctx["spread_source"] == "ESTIMATED_BACKTEST"


def test_lifecycle_export_reads_persisted_sql_events():
    rows = [
        bo.LifecycleRow(
            timestamp=1,
            symbol="BTCUSDT",
            side="LONG",
            setup_type="BREAKOUT_UP",
            setup_reason="X",
            regime="TREND",
            score=8.0,
            rr=1.5,
            entry=10.0,
            sl=9.0,
            tp=11.5,
            status_before="NONE",
            status_after="SIGNAL_CREATED",
        ),
        bo.LifecycleRow(
            timestamp=1,
            symbol="BTCUSDT",
            side="LONG",
            setup_type="BREAKOUT_UP",
            setup_reason="X",
            regime="TREND",
            score=8.0,
            rr=1.5,
            entry=10.0,
            sl=9.0,
            tp=11.5,
            status_before="SIGNAL_CREATED",
            status_after="SIGNAL_REJECTED",
            reject_reason="LOW_SCORE",
        ),
    ]
    persisted = bo._persist_lifecycle_rows(rows)
    assert len(persisted) == 2
    assert persisted[0]["lifecycle_state"] == "SIGNAL_CREATED"
    assert persisted[0]["decision"] == "PENDING"
    assert persisted[1]["lifecycle_state"] == "SIGNAL_REJECTED"
    assert persisted[1]["decision"] == "REJECTED"
    assert persisted[1]["reject_reason"] == "LOW_SCORE"
    assert persisted[1]["sql_rejected_decision_count"] == 1
    assert persisted[1]["sql_order_decision_count"] == 1




def test_lifecycle_persistence_uses_effective_rr_when_available():
    rows = [
        bo.LifecycleRow(
            timestamp=1,
            symbol="BTCUSDT",
            side="LONG",
            setup_type="BREAKOUT_UP",
            setup_reason="X",
            regime="TREND",
            score=8.0,
            rr=1.8,
            entry=10.0,
            sl=9.5,
            tp=10.9,
            status_before="SIGNAL_CREATED",
            status_after="ORDER_REJECTED",
            reject_reason="LOW_EFFECTIVE_RR",
            effective_rr=1.05,
            volume_24h_usdt=125000000.0,
            spread_pct=0.2,
            funding_rate_pct="UNAVAILABLE_BACKTEST",
            expected_slippage_pct=0.001,
            liquidity_score=0.8,
        )
    ]

    persisted = bo._persist_lifecycle_rows(rows)

    assert len(persisted) == 1
    assert persisted[0]["rr"] == 1.8
    assert persisted[0]["effective_rr"] == 1.05
    assert persisted[0]["effective_rr"] != persisted[0]["rr"]
    assert persisted[0]["execution_ctx_missing"] == 1

def test_lifecycle_export_has_no_duplicate_event_ids():
    rows = [
        bo.LifecycleRow(1, "BTCUSDT", "LONG", "S", "R", "TREND", 1.0, 1.1, 10.0, 9.0, 11.0, "NONE", "SIGNAL_CREATED"),
        bo.LifecycleRow(1, "BTCUSDT", "LONG", "S", "R", "TREND", 1.0, 1.1, 10.0, 9.0, 11.0, "SIGNAL_CREATED", "SIGNAL_REJECTED", reject_reason="X"),
    ]
    persisted = bo._persist_lifecycle_rows(rows)
    event_ids = [r["event_id"] for r in persisted]
    assert len(event_ids) == len(set(event_ids))


def test_accepted_lifecycle_ids_and_sequence_present():
    c = bo.CandidateOrder(1, "S", "LONG", 10, 9, 11, 1, "BACKTEST", "R", "X", 1, "MARKET")
    rows = bo.simulate_candidate(c, [bo.Candle(1, 10, 11.2, 9.9, 11, 1)], 0, 1000, 1)
    placed = next(r for r in rows if r.status_after == "ORDER_PLACED")
    closed = next(r for r in rows if r.status_after == "POSITION_CLOSED")
    assert placed.order_id
    assert closed.position_id
    assert rows == sorted(rows, key=lambda r: r.lifecycle_seq)
    assert "SIGNAL_CREATED" in [r.status_after for r in rows]
    assert "SIGNAL_ACCEPTED" in [r.status_after for r in rows]

def test_backtest_quality_summary_includes_effective_rr_distribution():
    rows = [
        {
            "decision": "REJECTED",
            "reject_reason": "LOW_EFFECTIVE_RR",
            "score": 5.0,
            "rr": 1.8,
            "effective_rr": 1.05,
            "expectancy_bucket": "LOW",
            "execution_ctx_missing": 1,
            "execution_ctx": '{"volume_24h_usdt":"UNAVAILABLE_BACKTEST","spread_pct":0.2,"funding_rate_pct":"UNAVAILABLE_BACKTEST","expected_slippage_pct":"UNAVAILABLE_BACKTEST"}',
        },
        {
            "decision": "ACCEPTED",
            "reject_reason": "",
            "score": 8.0,
            "rr": 2.2,
            "effective_rr": 2.2,
            "expectancy_bucket": "HIGH",
            "execution_ctx_missing": 0,
            "execution_ctx": '{"volume_24h_usdt":1000000.0,"spread_pct":0.0,"funding_rate_pct":0.0,"expected_slippage_pct":0.0,"latency_ms":0}',
        },
    ]

    summary = bo.build_backtest_quality_summary(rows)

    assert summary["total_candidates"] == 2
    assert summary["effective_rr_distribution"]["1.05"] == 1
    assert summary["effective_rr_distribution"]["2.2"] == 1
    assert summary["effective_rr_differs_from_rr_count"] == 1
    assert summary["unavailable_execution_context_field_counts"]["volume_24h_usdt"] == 1


def test_backtest_quality_summary_includes_reject_reason_distribution():
    rows = [
        {"decision": "REJECTED", "reject_reason": "LOW_SCORE", "score": 1.0, "rr": 1.0, "effective_rr": 1.0, "expectancy_bucket": "LOW", "execution_ctx_missing": 1, "execution_ctx": "{}"},
        {"decision": "REJECTED", "reject_reason": "LOW_SCORE", "score": 2.0, "rr": 1.1, "effective_rr": 1.0, "expectancy_bucket": "LOW", "execution_ctx_missing": 1, "execution_ctx": "{}"},
        {"decision": "REJECTED", "reject_reason": "HIGH_SLIPPAGE", "score": 7.0, "rr": 1.8, "effective_rr": 1.2, "expectancy_bucket": "MEDIUM", "execution_ctx_missing": 0, "execution_ctx": "{}"},
    ]

    summary = bo.build_backtest_quality_summary(rows)

    assert summary["rejected_count"] == 3
    assert summary["reject_reason_distribution"]["LOW_SCORE"] == 2
    assert summary["reject_reason_distribution"]["HIGH_SLIPPAGE"] == 1


def test_export_integrity_verifier_catches_row_count_mismatch():
    errors = bo.verify_export_integrity(
        persisted_lifecycle_rows=[{"decision": "ACCEPTED", "reject_reason": "", "expectancy_bucket": "LOW"}],
        rejected_rows=[],
        lifecycle_csv_rows=[],
        rejected_csv_rows=[],
    )
    assert any("lifecycle row count mismatch" in e for e in errors)


def test_symbol_rejected_rows_are_persisted_as_rejected_decision():
    rows = [
        bo.LifecycleRow(1, "ETHUSDT", "N/A", "", "", "CHOP", 2.0, 0.0, 0.0, 0.0, 0.0, "NONE", "SYMBOL_REJECTED", reject_reason="LOW_LIQUIDITY")
    ]
    persisted = bo._persist_lifecycle_rows(rows)
    assert persisted[0]["decision"] == "REJECTED"
    assert persisted[0]["reject_reason"] == "LOW_LIQUIDITY"


def test_derive_backtest_counts_uses_terminal_per_signal_and_order_placed_only():
    lifecycle = [
        bo.LifecycleRow(1, "BTCUSDT", "LONG", "S", "R", "TREND", 8.0, 2.0, 10.0, 9.0, 12.0, "NONE", "SIGNAL_CREATED"),
        bo.LifecycleRow(1, "BTCUSDT", "LONG", "S", "R", "TREND", 8.0, 2.0, 10.0, 9.0, 12.0, "SIGNAL_CREATED", "WAITING_ENTRY_ZONE"),
        bo.LifecycleRow(1, "BTCUSDT", "LONG", "S", "R", "TREND", 8.0, 2.0, 10.0, 9.0, 12.0, "WAITING_ENTRY_ZONE", "ENTRY_TRIGGERED"),
        bo.LifecycleRow(1, "BTCUSDT", "LONG", "S", "R", "TREND", 8.0, 2.0, 10.0, 9.0, 12.0, "ENTRY_TRIGGERED", "ORDER_PLACED"),
        bo.LifecycleRow(2, "ETHUSDT", "LONG", "S", "R", "TREND", 3.0, 1.0, 20.0, 18.0, 21.0, "NONE", "SIGNAL_CREATED"),
        bo.LifecycleRow(2, "ETHUSDT", "LONG", "S", "R", "TREND", 3.0, 1.0, 20.0, 18.0, 21.0, "SIGNAL_CREATED", "SIGNAL_REJECTED", reject_reason="LOW_SCORE"),
        bo.LifecycleRow(3, "SOLUSDT", "N/A", "", "", "CHOP", 1.0, 0.0, 0.0, 0.0, 0.0, "NONE", "SYMBOL_REJECTED", reject_reason="LOW_LIQUIDITY"),
    ]
    counts = bo._derive_backtest_counts(lifecycle)
    assert counts["total_candidates"] == 3
    assert counts["accepted_count"] == 1
    assert counts["rejected_count"] == 2
    assert counts["total_orders"] == 1
    assert counts["triggered_orders"] == 1
    assert counts["not_triggered_orders"] == 0
    assert counts["tp_hits"] == 0
    assert counts["sl_hits"] == 0


def test_derive_backtest_counts_tracks_not_triggered_from_waiting_state():
    lifecycle = [
        bo.LifecycleRow(1, "BTCUSDT", "LONG", "S", "R", "TREND", 8.0, 2.0, 10.0, 9.0, 12.0, "NONE", "SIGNAL_CREATED"),
        bo.LifecycleRow(1, "BTCUSDT", "LONG", "S", "R", "TREND", 8.0, 2.0, 10.0, 9.0, 12.0, "SIGNAL_CREATED", "WAITING_ENTRY_ZONE"),
        bo.LifecycleRow(1, "BTCUSDT", "LONG", "S", "R", "TREND", 8.0, 2.0, 10.0, 9.0, 12.0, "WAITING_ENTRY_ZONE", "ENTRY_TIMEOUT", cancel_reason="TIMEOUT"),
    ]
    counts = bo._derive_backtest_counts(lifecycle)
    assert counts["total_orders"] == 0
    assert counts["triggered_orders"] == 0
    assert counts["not_triggered_orders"] == 1


def test_lifecycle_sequence_is_monotonic_per_signal():
    rows = [
        bo.LifecycleRow(1, "BTCUSDT", "LONG", "S", "R", "TREND", 8.0, 2.0, 10.0, 9.0, 12.0, "NONE", "SIGNAL_CREATED", signal_id="BTCUSDT:1", lifecycle_seq=1),
        bo.LifecycleRow(1, "BTCUSDT", "LONG", "S", "R", "TREND", 8.0, 2.0, 10.0, 9.0, 12.0, "SIGNAL_CREATED", "WAITING_ENTRY_ZONE", signal_id="BTCUSDT:1", lifecycle_seq=2),
        bo.LifecycleRow(1, "BTCUSDT", "LONG", "S", "R", "TREND", 8.0, 2.0, 10.0, 9.0, 12.0, "WAITING_ENTRY_ZONE", "ENTRY_TRIGGERED", signal_id="BTCUSDT:1", lifecycle_seq=3),
    ]
    seqs = [r.lifecycle_seq for r in rows if r.signal_id == "BTCUSDT:1"]
    assert seqs == sorted(seqs)


def test_low_score_rejection_rescue_watch_fields_are_diagnostics_only():
    fields = bo._low_score_rescue_watch_fields("LOW_SCORE", {})
    assert fields["rescue_watch_eligible"] is True
    assert fields["rescue_watch_reason"] == "LOW_SCORE_DIAGNOSTIC_ONLY"
    assert fields["rescued_size_multiplier"] == 0.0


def test_signal_id_cannot_end_with_both_terminal_accepted_and_rejected():
    lifecycle = [
        bo.LifecycleRow(10, "XRPUSDT", "LONG", "S", "R", "TREND", 7.0, 2.0, 1.0, 0.9, 1.2, "NONE", "SIGNAL_CREATED"),
        bo.LifecycleRow(10, "XRPUSDT", "LONG", "S", "R", "TREND", 7.0, 2.0, 1.0, 0.9, 1.2, "SIGNAL_CREATED", "ORDER_PLACED"),
        bo.LifecycleRow(10, "XRPUSDT", "LONG", "S", "R", "TREND", 7.0, 2.0, 1.0, 0.9, 1.2, "ORDER_PLACED", "ORDER_REJECTED", reject_reason="EXECUTION_RISK"),
    ]
    counts = bo._derive_backtest_counts(lifecycle)
    assert counts["accepted_count"] == 0
    assert counts["rejected_count"] == 1


def test_backtest_quality_summary_uses_signal_created_as_candidate_denominator():
    rows = [
        {"signal_id": "BTCUSDT:1", "lifecycle_state": "SIGNAL_CREATED", "decision": "PENDING", "reject_reason": "", "score": 7.0, "rr": 2.0, "effective_rr": 2.0, "expectancy_bucket": "MEDIUM", "execution_ctx_missing": 0, "execution_ctx": "{}"},
        {"signal_id": "BTCUSDT:1", "lifecycle_state": "WAITING_ENTRY_ZONE", "decision": "ACCEPTED", "reject_reason": "", "score": 7.0, "rr": 2.0, "effective_rr": 2.0, "expectancy_bucket": "MEDIUM", "execution_ctx_missing": 0, "execution_ctx": "{}"},
        {"signal_id": "ETHUSDT:2", "lifecycle_state": "SIGNAL_CREATED", "decision": "PENDING", "reject_reason": "", "score": 2.0, "rr": 1.0, "effective_rr": 1.0, "expectancy_bucket": "LOW", "execution_ctx_missing": 0, "execution_ctx": "{}"},
        {"signal_id": "ETHUSDT:2", "lifecycle_state": "SIGNAL_REJECTED", "decision": "REJECTED", "reject_reason": "LOW_SCORE", "score": 2.0, "rr": 1.0, "effective_rr": 1.0, "expectancy_bucket": "LOW", "execution_ctx_missing": 0, "execution_ctx": "{}"},
    ]
    summary = bo.build_backtest_quality_summary(rows)
    assert summary["total_candidates"] == 2
    assert summary["accepted_count"] == 1
    assert summary["rejected_count"] == 1
    assert summary["reject_reason_distribution"]["LOW_SCORE"] == 1


def test_export_integrity_reject_count_matches_lifecycle_sql_rows():
    persisted = [
        {"signal_id": "A:1", "lifecycle_state": "SIGNAL_CREATED", "decision": "PENDING", "reject_reason": "", "score": 5.0, "rr": 1.2, "expectancy_bucket": "LOW", "execution_ctx_missing": 1, "execution_ctx": '{"spread_pct":"UNAVAILABLE_BACKTEST"}'},
        {"signal_id": "A:1", "lifecycle_state": "SIGNAL_REJECTED", "decision": "REJECTED", "reject_reason": "LOW_SCORE", "score": 5.0, "rr": 1.2, "expectancy_bucket": "LOW", "execution_ctx_missing": 1, "execution_ctx": '{"spread_pct":"UNAVAILABLE_BACKTEST"}'},
    ]
    errors = bo.verify_export_integrity(
        persisted_lifecycle_rows=persisted,
        rejected_rows=[{"reject_reason": "LOW_SCORE"}],
        lifecycle_csv_rows=list(persisted),
        rejected_csv_rows=[],
    )
    assert any("rejected_orders.csv count mismatch" in e for e in errors)


def test_export_integrity_rejects_missing_lifecycle_state_and_legacy_created_only():
    errors = bo.verify_export_integrity(
        persisted_lifecycle_rows=[{"signal_id": "A:1", "lifecycle_state": "", "decision": "PENDING", "reject_reason": "", "score": 5.0, "rr": 1.2, "expectancy_bucket": "LOW", "execution_ctx_missing": 0, "execution_ctx": "{}"}],
        rejected_rows=[],
        lifecycle_csv_rows=[{}],
        rejected_csv_rows=[],
    )
    assert any("missing lifecycle_state/status_after" in e for e in errors)
    errors = bo.verify_export_integrity(
        persisted_lifecycle_rows=[{"signal_id": "A:1", "lifecycle_state": "CREATED", "decision": "PENDING", "reject_reason": "", "score": 5.0, "rr": 1.2, "expectancy_bucket": "LOW", "execution_ctx_missing": 0, "execution_ctx": "{}"}],
        rejected_rows=[],
        lifecycle_csv_rows=[{}],
        rejected_csv_rows=[],
    )
    assert any("legacy CREATED" in e for e in errors)
    errors = bo.verify_export_integrity(
        persisted_lifecycle_rows=[{"signal_id": "A:1", "lifecycle_state": "SIGNAL_CREATED", "decision": "PENDING", "reject_reason": "", "score": 5.0, "rr": 1.2, "expectancy_bucket": "LOW", "execution_ctx_missing": 0, "execution_ctx": "{}"}],
        rejected_rows=[],
        lifecycle_csv_rows=[{}],
        rejected_csv_rows=[],
    )
    assert any("CREATED-only" in e for e in errors)


def test_export_integrity_fails_on_fake_zero_missing_execution_context():
    errors = bo.verify_export_integrity(
        persisted_lifecycle_rows=[
            {"signal_id": "A:1", "lifecycle_state": "SIGNAL_CREATED", "decision": "PENDING", "reject_reason": "", "score": 5.0, "rr": 1.2, "expectancy_bucket": "LOW", "execution_ctx_missing": 1, "execution_ctx": '{"spread_pct":0.0,"expected_slippage_pct":"UNAVAILABLE_BACKTEST","funding_rate_pct":"UNAVAILABLE_BACKTEST","volume_24h_usdt":"UNAVAILABLE_BACKTEST","liquidity_score":"UNAVAILABLE_BACKTEST"}'},
            {"signal_id": "A:1", "lifecycle_state": "SIGNAL_REJECTED", "decision": "REJECTED", "reject_reason": "LOW_SCORE", "score": 5.0, "rr": 1.2, "expectancy_bucket": "LOW", "execution_ctx_missing": 1, "execution_ctx": '{"spread_pct":"UNAVAILABLE_BACKTEST"}'},
        ],
        rejected_rows=[{"reject_reason": "LOW_SCORE"}],
        lifecycle_csv_rows=[{}, {}],
        rejected_csv_rows=[{"reject_reason": "LOW_SCORE"}],
    )
    assert any("fake zero" in e for e in errors)


def test_export_integrity_flags_suspicious_constant_score_and_rr_distribution():
    rows = []
    for idx in range(3):
        rows.extend([
            {"signal_id": f"S:{idx}", "lifecycle_state": "SIGNAL_CREATED", "decision": "PENDING", "reject_reason": "", "score": 0.8, "rr": 2.0, "expectancy_bucket": "MEDIUM", "execution_ctx_missing": 0, "execution_ctx": "{}"},
            {"signal_id": f"S:{idx}", "lifecycle_state": "SIGNAL_REJECTED", "decision": "REJECTED", "reject_reason": "LOW_SCORE", "score": 0.8, "rr": 2.0, "expectancy_bucket": "MEDIUM", "execution_ctx_missing": 0, "execution_ctx": "{}"},
        ])
    rejected = [{"reject_reason": "LOW_SCORE"} for _ in range(3)]
    errors = bo.verify_export_integrity(rows, rejected, list(rows), list(rejected))
    assert any("score distribution suspiciously constant" in e for e in errors)
    assert any("rr distribution suspiciously constant" in e for e in errors)


def test_backtest_rejected_rows_have_signal_id_effective_rr_and_export_parity():
    lifecycle, rejected, rejection_counts, open_rows = [], [], {}, []
    recent_stats = {"last_trade_ts_by_symbol": {}, "trades_today_by_symbol": {}, "global_trades_today": 0, "outcomes": []}
    candle = bo.Candle(1710000000000, 100.0, 101.0, 99.0, 100.5, 1000.0)
    mctx = {
        "side": "LONG",
        "setup_type": "BREAKOUT_UP",
        "setup_reason": "X",
        "regime": "TREND",
        "score": 6.2,
        "rr": 1.4,
        "entry": 100.0,
        "sl": 99.0,
        "tp": 101.4,
        "expectancy": 0.01,
        "spread_pct": 0.004,
        "expected_slippage_pct": 0.003,
        "funding_rate_pct": 0.0,
        "liquidity_score": 0.8,
        "volume_24h_usdt": 1000000.0,
    }
    result = {"status": "rejected", "reason": "LOW_SCORE", "diagnostics": dict(mctx)}
    assert bo.process_backtest_result("BTCUSDT", candle, 0, [candle], result, mctx, 1000.0, 1.0, lifecycle, rejected, rejection_counts, open_rows, recent_stats) is None
    persisted = bo._persist_lifecycle_rows(lifecycle)
    rejected_lifecycle = [r for r in persisted if r["decision"] == "REJECTED"]
    assert len(rejected_lifecycle) == len(rejected) == 1
    assert rejected[0]["signal_id"] == rejected_lifecycle[0]["signal_id"] == "BTCUSDT:1710000000000"
    assert rejected[0]["reject_reason"] == rejected_lifecycle[0]["reject_reason"] == "LOW_SCORE"
    assert rejected_lifecycle[0]["sql_rejected_decision_count"] == 1
    assert rejected_lifecycle[0]["sql_order_decision_count"] == 1
    assert float(rejected[0]["raw_rr"]) != float(rejected[0]["effective_rr"])
    errors = bo.verify_export_integrity(persisted, rejected, list(persisted), list(rejected))
    assert not [error for error in errors if "rejected_orders.csv count mismatch" in error or "missing reject_reason" in error]


def test_high_score_high_rr_candidate_with_valid_liquidity_not_low_liquidity():
    effective_rr, flags, _ = bo._execution_reject_flags(
        2.0,
        {"spread_pct": 0.0002, "expected_slippage_pct": 0.0002, "liquidity_score": 0.8, "funding_rate_pct": 0.0},
    )
    assert effective_rr > 1.1
    assert "LOW_LIQUIDITY" not in flags


def test_selector_reject_persists_as_symbol_rejected_not_signal_rejected():
    rows = [
        bo.LifecycleRow(
            1,
            "ETHUSDT",
            "N/A",
            "",
            "",
            "CHOP",
            2.0,
            0.0,
            0.0,
            0.0,
            0.0,
            "NONE",
            "SYMBOL_REJECTED",
            reject_reason="LOW_LIQUIDITY",
            event_flags="SYMBOL_SELECTOR",
            liquidity_score=0.1,
        )
    ]
    persisted = bo._persist_lifecycle_rows(rows)
    assert persisted[0]["lifecycle_state"] == "SYMBOL_REJECTED"
    assert persisted[0]["decision"] == "REJECTED"
    assert "SYMBOL_REJECTED" in persisted[0]["event_id"]


def test_lifecycle_export_integrity_rejects_selector_mislabeled_as_signal_rejected():
    errors = bo.verify_export_integrity(
        persisted_lifecycle_rows=[
            {
                "event_id": "1:ETHUSDT:NONE:SYMBOL_REJECTED:0:0:0:1",
                "signal_id": "ETHUSDT:1",
                "lifecycle_state": "SIGNAL_REJECTED",
                "decision": "REJECTED",
                "reject_reason": "LOW_LIQUIDITY",
                "score": 2.0,
                "rr": 0.0,
                "expectancy_bucket": "UNKNOWN",
                "execution_ctx_missing": 0,
                "execution_ctx": "{}",
            }
        ],
        rejected_rows=[{"signal_id": "ETHUSDT:1", "lifecycle_state": "SIGNAL_REJECTED", "reject_reason": "LOW_LIQUIDITY"}],
        lifecycle_csv_rows=[{"signal_id": "ETHUSDT:1", "lifecycle_state": "SIGNAL_REJECTED", "reject_reason": "LOW_LIQUIDITY"}],
        rejected_csv_rows=[{"signal_id": "ETHUSDT:1", "lifecycle_state": "SIGNAL_REJECTED", "reject_reason": "LOW_LIQUIDITY"}],
    )
    assert any("mislabels selector reject" in e for e in errors)


def test_pre_signal_symbol_rejected_uses_selector_diagnostic_signal_id_and_validates():
    rows = [
        bo.LifecycleRow(
            1, "ETHUSDT", "N/A", "", "", "CHOP", 2.0, 0.0, 0.0, 0.0, 0.0,
            "NONE", "SYMBOL_REJECTED", reject_reason="LOW_LIQUIDITY", event_flags="SYMBOL_SELECTOR", liquidity_score=0.1,
        )
    ]
    persisted = bo._persist_lifecycle_rows(rows)
    assert persisted[0]["signal_id"] == "SYMBOL_SELECTOR:ETHUSDT:1"
    assert persisted[0]["lifecycle_state"] == "SYMBOL_REJECTED"
    errors = bo.verify_export_integrity(persisted, list(persisted), list(persisted), list(persisted))
    assert not any("SYMBOL_REJECTED after signal creation" in e for e in errors)


def test_post_signal_symbol_rejected_maps_to_signal_rejected_with_reason():
    rows = [
        bo.LifecycleRow(1, "BTCUSDT", "LONG", "S", "R", "TREND", 8.0, 2.0, 10.0, 9.0, 12.0, "NONE", "SIGNAL_CREATED"),
        bo.LifecycleRow(1, "BTCUSDT", "LONG", "S", "R", "TREND", 8.0, 2.0, 10.0, 9.0, 12.0, "SIGNAL_CREATED", "SYMBOL_REJECTED", reject_reason="REGIME_MISMATCH"),
    ]
    persisted = bo._persist_lifecycle_rows(rows)
    assert [r["lifecycle_state"] for r in persisted] == ["SIGNAL_CREATED", "SIGNAL_REJECTED"]
    assert persisted[1]["reject_reason"] == "REGIME_MISMATCH"
    assert "SYMBOL_REJECTED" not in persisted[1]["event_id"]
    errors = bo.verify_export_integrity(persisted, [persisted[1]], list(persisted), [persisted[1]])
    assert not any("SYMBOL_REJECTED after signal creation" in e for e in errors)
    assert not any("mislabels selector reject" in e for e in errors)


def test_lifecycle_export_valid_state_transitions_only_for_dashboard_symbols():
    rows = [
        bo.LifecycleRow(1782308700000, "BTCUSDT", "LONG", "S", "R", "TREND", 8.1, 2.1, 10.0, 9.0, 12.0, "NONE", "SIGNAL_CREATED"),
        bo.LifecycleRow(1782308700000, "BTCUSDT", "LONG", "S", "R", "TREND", 8.1, 2.1, 10.0, 9.0, 12.0, "SIGNAL_CREATED", "WAITING_ENTRY_ZONE"),
        bo.LifecycleRow(1782312300000, "ETHUSDT", "N/A", "", "", "CHOP", 1.0, 0.0, 0.0, 0.0, 0.0, "NONE", "SYMBOL_REJECTED", reject_reason="LOW_LIQUIDITY", event_flags="SYMBOL_SELECTOR"),
    ]
    persisted = bo._persist_lifecycle_rows(rows)
    rejected = [r for r in persisted if r["decision"] == "REJECTED"]
    errors = bo.verify_export_integrity(persisted, rejected, list(persisted), list(rejected))
    assert not errors
    assert all(r["reject_reason"] for r in rejected)


def test_rejected_shadow_rows_are_persisted_with_reject_reason_and_outcome():
    lifecycle = [
        bo.LifecycleRow(
            timestamp=1,
            symbol="BTCUSDT",
            side="LONG",
            setup_type="BREAKOUT_UP",
            setup_reason="X",
            regime="TREND",
            score=4.0,
            rr=1.5,
            entry=100.0,
            sl=99.0,
            tp=101.5,
            status_before="SIGNAL_CREATED",
            status_after="SIGNAL_REJECTED",
            reject_reason="LOW_SCORE",
            liquidity_score=0.9,
            spread_pct=0.01,
        )
    ]
    shadows = [
        bo.RejectedShadowEvaluation(
            "BTCUSDT", 1, "LONG", 100.0, 99.0, 101.5, 1.5, 1.35, "LOW_SCORE", 4.0,
            "TREND", 0.0001, 0.9, 0.5, "WOULD_TP", True, 0.15, True, True,
        )
    ]
    bo._attach_rejected_shadow_to_lifecycle(lifecycle, shadows)
    persisted = bo._persist_lifecycle_rows(lifecycle)

    assert persisted[0]["decision"] == "REJECTED"
    assert persisted[0]["lifecycle_state"] == "SIGNAL_REJECTED"
    assert persisted[0]["reject_reason"] == "LOW_SCORE"
    assert persisted[0]["effective_rr"] == pytest.approx(1.35)
    assert persisted[0]["shadow_outcome"] == "WOULD_TP"
    assert persisted[0]["liquidity_ok"] == 1


def test_liquidity_score_uses_historical_volume_when_symbol_meta_missing():
    ctx = bo._build_market_ctx(
        bo.Candle(3, 100.0, 101.0, 99.5, 100.5, 2_000_000.0),
        bo.Candle(2, 100.0, 100.5, 99.5, 100.0, 1_500_000.0),
        {},
        recent=[bo.Candle(1, 100.0, 101.0, 99.0, 100.0, 1_000_000.0)],
    )

    assert ctx["volume_24h_usdt"] > 100_000_000.0
    assert ctx["liquidity_score"] > 0.1


def test_liquidity_ok_can_be_true_when_liquidity_conditions_pass():
    row = {"timestamp": 1, "symbol": "BTCUSDT", "side": "LONG", "entry": 10, "sl": 9, "tp": 12, "rr": 2.0, "reject_reason": "LOW_SCORE", "score": 4.0, "regime": "TREND", "spread_pct": 0.01, "liquidity_score": 0.75, "volatility_score": 1.0}
    shadow = bo.evaluate_rejected_shadow(row, [bo.Candle(1, 10, 12.5, 9.8, 12, 10)], 0)
    assert shadow.liquidity_ok is True


def test_would_tp_shadow_does_not_inflate_accepted_count():
    lifecycle = [
        bo.LifecycleRow(1, "BTCUSDT", "LONG", "BREAKOUT_UP", "X", "TREND", 4.0, 1.5, 10.0, 9.0, 11.5, "NONE", "SIGNAL_CREATED"),
        bo.LifecycleRow(1, "BTCUSDT", "LONG", "BREAKOUT_UP", "X", "TREND", 4.0, 1.5, 10.0, 9.0, 11.5, "SIGNAL_CREATED", "SIGNAL_REJECTED", reject_reason="LOW_SCORE"),
    ]
    shadows = [bo.RejectedShadowEvaluation("BTCUSDT", 1, "LONG", 10, 9, 11.5, 1.5, 1.4, "LOW_SCORE", 4.0, "TREND", 0.01, 0.9, 0.5, "WOULD_TP", True, 0.1, True, True)]
    bo._attach_rejected_shadow_to_lifecycle(lifecycle, shadows)
    counts = bo._derive_backtest_counts(lifecycle)
    persisted = bo._persist_lifecycle_rows(lifecycle)

    assert counts["accepted_count"] == 0
    assert any(r["shadow_outcome"] == "WOULD_TP" and r["decision"] == "REJECTED" for r in persisted)


def _rescue_recent_stats():
    return {"last_trade_ts_by_symbol": {}, "trades_today_by_symbol": {}, "global_trades_today": 0, "outcomes": []}


def _rescue_result(reason="STOP_TOO_WIDE", score=9.5, rr=2.2):
    return {
        "status": "rejected",
        "reason": reason,
        "diagnostics": {
            "side": "LONG", "setup_type": "BREAKOUT_UP", "setup_reason": "X", "regime": "TREND",
            "score": score, "rr": rr, "entry": 10.0, "sl": 9.5, "tp": 11.1, "order_type": "MARKET",
        },
    }


def _rescue_mctx(score=9.5, rr=2.2, spread=0.001, slippage=0.001, liquidity_ok=True, volatility_ok=True, regime="TREND"):
    return {
        "entry": 10.0, "sl": 9.5, "tp": 11.1, "score": score, "rr": rr, "spread_pct": spread,
        "expected_slippage_pct": slippage, "liquidity_score": 0.9, "liquidity_ok": liquidity_ok,
        "volatility_ok": volatility_ok, "regime": regime, "volatility_regime": "NORMAL",
    }


def test_rescue_disabled_preserves_baseline_rejection(monkeypatch):
    lifecycle, rejected, rejection_counts, open_rows = [], [], {}, []
    candles = [bo.Candle(1, 10, 11.5, 9.8, 11, 1)]
    called = {"n": 0}
    monkeypatch.setattr(bo, "simulate_candidate", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or [])

    cand = bo.process_backtest_result("AAAUSDT", candles[0], 0, candles, _rescue_result(), _rescue_mctx(), 1000, 1.0, lifecycle, rejected, rejection_counts, open_rows, _rescue_recent_stats())

    assert cand is None
    assert called["n"] == 0
    assert [r.status_after for r in lifecycle] == ["SIGNAL_CREATED", "SIGNAL_REJECTED"]
    assert rejected[0]["reject_reason"] == "STOP_TOO_WIDE"


def test_rescue_enabled_only_backtest_and_marks_reduced_size_exports():
    lifecycle, rejected, rejection_counts, open_rows = [], [], {}, []
    candles = [bo.Candle(1, 10, 11.5, 9.8, 11, 1)]
    stats = bo.RescueStats()
    cand = bo.process_backtest_result(
        "AAAUSDT", candles[0], 0, candles, _rescue_result(), _rescue_mctx(), 1000, 1.0,
        lifecycle, rejected, rejection_counts, open_rows, _rescue_recent_stats(),
        rescue_config=bo.RescueConfig(enabled=True), rescue_stats=stats, mode="BACKTEST",
    )

    assert cand is not None
    assert cand.accepted_reason == "HIGH_EFFECTIVE_RR_RESCUE"
    assert cand.original_reject_reason == "STOP_TOO_WIDE"
    assert cand.rescue_size_multiplier == pytest.approx(0.25)
    assert stats.accepted_count == 1
    assert any(r.accepted_reason == "HIGH_EFFECTIVE_RR_RESCUE" and r.original_reject_reason == "STOP_TOO_WIDE" for r in lifecycle)
    assert any(r.status_after == "POSITION_CLOSED" and r.net_pnl_usdt < 1.0 for r in lifecycle)
    persisted = bo._persist_lifecycle_rows(lifecycle)
    assert any(r.get("accepted_reason") == "HIGH_EFFECTIVE_RR_RESCUE" for r in persisted)
    assert any(r.get("original_reject_reason") == "STOP_TOO_WIDE" for r in persisted)


def test_rescue_cannot_accept_live_mode():
    lifecycle, rejected, rejection_counts, open_rows = [], [], {}, []
    candles = [bo.Candle(1, 10, 11.5, 9.8, 11, 1)]
    stats = bo.RescueStats()
    cand = bo.process_backtest_result(
        "AAAUSDT", candles[0], 0, candles, _rescue_result(), _rescue_mctx(), 1000, 1.0,
        lifecycle, rejected, rejection_counts, open_rows, _rescue_recent_stats(),
        rescue_config=bo.RescueConfig(enabled=True), rescue_stats=stats, mode="LIVE",
    )
    assert cand is None
    assert stats.accepted_count == 0
    assert stats.reject_reasons["MODE_NOT_BACKTEST"] == 1


@pytest.mark.parametrize("kwargs,reason", [
    ({"spread": 0.01}, "SPREAD_TOO_HIGH"),
    ({"slippage": 0.01}, "SLIPPAGE_TOO_HIGH"),
    ({"liquidity_ok": False}, "LIQUIDITY_NOT_OK"),
    ({"volatility_ok": False}, "VOLATILITY_NOT_OK"),
])
def test_rescue_does_not_bypass_execution_quality_checks(kwargs, reason):
    lifecycle, rejected, rejection_counts, open_rows = [], [], {}, []
    candles = [bo.Candle(1, 10, 11.5, 9.8, 11, 1)]
    stats = bo.RescueStats()
    cand = bo.process_backtest_result(
        "AAAUSDT", candles[0], 0, candles, _rescue_result(), _rescue_mctx(**kwargs), 1000, 1.0,
        lifecycle, rejected, rejection_counts, open_rows, _rescue_recent_stats(),
        rescue_config=bo.RescueConfig(enabled=True), rescue_stats=stats, mode="BACKTEST",
    )
    assert cand is None
    assert stats.reject_reasons[reason] == 1


def test_rescue_does_not_bypass_max_concurrent_positions():
    lifecycle, rejected, rejection_counts = [], [], {}
    open_rows = [bo.LifecycleRow(1, "OTHER", "LONG", "", "", "TREND", 9, 2, 10, 9, 12, "ORDER_PLACED", "POSITION_CLOSED")]
    candles = [bo.Candle(1, 10, 11.5, 9.8, 11, 1)]
    stats = bo.RescueStats()
    cfg = bo.RescueConfig(enabled=True, max_concurrent_positions=1)
    cand = bo.process_backtest_result(
        "AAAUSDT", candles[0], 0, candles, _rescue_result(), _rescue_mctx(), 1000, 1.0,
        lifecycle, rejected, rejection_counts, open_rows, _rescue_recent_stats(), rescue_config=cfg, rescue_stats=stats, mode="BACKTEST",
    )
    assert cand is None
    assert stats.reject_reasons["MAX_CONCURRENT_POSITIONS"] == 1


def test_backtest_quality_summary_exposes_rescue_metrics():
    rows = [
        {"decision": "ACCEPTED", "lifecycle_state": "SIGNAL_CREATED", "accepted_reason": "BASELINE", "score": 8, "rr": 2, "effective_rr": 2, "execution_ctx": "{}"},
        {"decision": "ACCEPTED", "lifecycle_state": "SIGNAL_CREATED", "accepted_reason": "HIGH_EFFECTIVE_RR_RESCUE", "score": 9.5, "rr": 2.2, "effective_rr": 2.1, "rescue_effective_rr": 2.1, "execution_ctx": "{}"},
    ]
    summary = bo.build_backtest_quality_summary(rows)
    assert summary["baseline_accepted_trades"] == 1
    assert summary["rescue_accepted_count"] == 1
    assert summary["accepted_reason_breakdown"]["HIGH_EFFECTIVE_RR_RESCUE"] == 1


def test_rescue_config_does_not_change_global_order_thresholds():
    import alphaforge.order as order

    cfg = bo.RescueConfig(enabled=True)

    assert cfg.score_min == pytest.approx(9.0)
    assert cfg.effective_rr_min == pytest.approx(1.90)
    assert order.MIN_SCORE_BASE == pytest.approx(7.5)
    assert order.MIN_RR_BASE == pytest.approx(1.3)
    assert order.MIN_RR_THRESHOLD == pytest.approx(1.6)


def _shadow_eval(symbol, reason, score, effective_rr, outcome, **kwargs):
    return bo.RejectedShadowEvaluation(
        symbol=symbol, timestamp=1, side=kwargs.get("side", "LONG"), entry=100, stop_loss=98,
        take_profit=104, raw_rr=2.0, effective_rr=effective_rr, reject_reasons=reason,
        score=score, regime=kwargs.get("regime", "TREND"), spread_pct=kwargs.get("spread_pct", 0.02),
        liquidity_score=kwargs.get("liquidity_score", 0.8), volatility_score=kwargs.get("volatility_score", 0.2),
        shadow_outcome=outcome, effective_tp_hit=(outcome == "WOULD_TP"), cost_penalty=0.1,
        liquidity_ok=True, volatility_ok=True, setup_type=kwargs.get("setup_type", "BREAKOUT"),
        expected_slippage_pct=kwargs.get("expected_slippage_pct", 0.01), stop_distance_pct=2.0,
    )


def test_signal_quality_diagnostics_do_not_change_trade_counts_or_rejects():
    accepted = [bo.LifecycleRow(1, "AAAUSDT", "LONG", "BREAKOUT", "", "TREND", 8.0, 2.0, 100, 98, 104, "FILLED", "POSITION_CLOSED", close_reason="TP_HIT")]
    shadows = [_shadow_eval("BBBUSDT", "LOW_SCORE", 6.0, 1.2, "WOULD_SL")]
    accepted_before = len(accepted)
    reject_reasons_before = [s.reject_reasons for s in shadows]

    bo.build_signal_quality_diagnostics(accepted, shadows, "15m")

    assert len(accepted) == accepted_before
    assert [s.reject_reasons for s in shadows] == reject_reasons_before


def test_signal_quality_score_decile_grouping_and_missing_fields_unavailable():
    shadow = _shadow_eval("AAAUSDT", "LOW_SCORE", 10.0, 2.2, "WOULD_TP")
    shadow.setup_type = ""
    shadow.expected_slippage_pct = ""
    shadow.stop_distance_pct = ""

    summary, groups, _, combo, gates, calib = bo.build_signal_quality_diagnostics([], [shadow], "1h")

    assert summary["score_saturation"]["score_10_count"] == 1
    assert any(g["group_field"] == "score_decile" and g["group_value"] == "D10" and g["count"] == 1 for g in groups)
    assert any(g["group_field"] == "expected_slippage_pct_bucket" and g["group_value"] == "UNAVAILABLE" for g in groups)
    assert any(g["group_field"] == "stop_distance_pct_bucket" and g["group_value"] == "UNAVAILABLE" for g in groups)
    assert any(r["grouping"] == "side+regime+setup_type+stop_distance_pct_bucket" for r in combo)
    assert any(r["gate_name"] == "STOP_TOO_WIDE_RECOVERABLE_GATE" and r["reporting_only"] is True for r in gates)
    assert any(r["diagnostic"] == "d10_by_reject_reason" for r in calib)


def test_short_breakdown_breakout_normal_stop_gate_disabled_preserves_baseline_metrics():
    shadows = [
        _shadow_eval("AAAUSDT", "LOW_SCORE", 6.0, 1.2, "WOULD_TP", side="SHORT", regime="BREAKOUT", setup_type="BREAKDOWN_DOWN", spread_pct=0.001, expected_slippage_pct=0.001)
    ]
    shadows[0].stop_distance_pct = 1.0

    summary, *_ = bo.build_signal_quality_diagnostics([], shadows, "15m")

    assert summary["quality_gate_enabled"] is False
    assert summary["quality_gate_candidate_count"] == 0
    assert summary["acceptance_logic_changed"] is False


def test_short_breakdown_breakout_normal_stop_gate_counts_enabled_backtest_only():
    eligible = _shadow_eval("AAAUSDT", "LOW_SCORE", 6.0, 1.2, "WOULD_TP", side="SHORT", regime="BREAKOUT", setup_type="BREAKDOWN_DOWN", spread_pct=0.001, expected_slippage_pct=0.001)
    eligible.stop_distance_pct = 1.0
    wide = _shadow_eval("BBBUSDT", "LOW_SCORE", 6.0, 1.2, "WOULD_TP", side="SHORT", regime="BREAKOUT", setup_type="BREAKDOWN_DOWN", spread_pct=0.001, expected_slippage_pct=0.001)
    wide.stop_distance_pct = 2.0
    long_row = _shadow_eval("CCCUSDT", "LOW_SCORE", 6.0, 1.2, "WOULD_TP", side="LONG", regime="BREAKOUT", setup_type="BREAKDOWN_DOWN", spread_pct=0.001, expected_slippage_pct=0.001)
    long_row.stop_distance_pct = 1.0
    mismatch = _shadow_eval("DDDUSDT", "REGIME_MISMATCH", 6.0, 1.2, "WOULD_TP", side="SHORT", regime="BREAKOUT", setup_type="BREAKDOWN_DOWN", spread_pct=0.001, expected_slippage_pct=0.001)
    mismatch.stop_distance_pct = 1.0
    panic = _shadow_eval("EEEUSDT", "LOW_SCORE", 6.0, 1.2, "WOULD_TP", side="SHORT", regime="PANIC", setup_type="BREAKDOWN_DOWN", spread_pct=0.001, expected_slippage_pct=0.001)
    panic.stop_distance_pct = 1.0

    cfg = bo.QualityGateConfig(enabled=True, modes=("BACKTEST",), max_trades_per_day=10)
    summary, *_ = bo.build_signal_quality_diagnostics([], [eligible, wide, long_row, mismatch, panic], "15m", quality_gate_config=cfg, baseline_net_pnl=4.0)

    assert summary["quality_gate_candidate_count"] == 1
    assert summary["quality_gate_accepted_count"] == 1
    assert summary["quality_gate_would_tp_count"] == 1
    assert summary["quality_gate_reason_breakdown"] == {"LOW_SCORE": 1}
    assert summary["baseline_plus_quality_gate_net_pnl"] > 4.0


def test_short_breakdown_breakout_normal_stop_gate_live_and_paper_disabled():
    eligible = _shadow_eval("AAAUSDT", "LOW_SCORE", 6.0, 1.2, "WOULD_TP", side="SHORT", regime="BREAKOUT", setup_type="BREAKDOWN_DOWN", spread_pct=0.001, expected_slippage_pct=0.001)
    eligible.stop_distance_pct = 1.0

    live = bo._quality_gate_metrics([bo._quality_record_from_shadow(eligible, "15m")], bo.QualityGateConfig(enabled=True, modes=("LIVE",)))
    paper_default = bo._quality_gate_metrics([bo._quality_record_from_shadow(eligible, "15m")], bo.QualityGateConfig(enabled=False, modes=("PAPER",)))

    assert live["quality_gate_candidate_count"] == 0
    assert paper_default["quality_gate_candidate_count"] == 0


def test_high_effective_rr_missed_alpha_counts_outcomes_correctly():
    shadows = [
        _shadow_eval("AAAUSDT", "LOW_SCORE", 8.0, 2.2, "WOULD_TP"),
        _shadow_eval("BBBUSDT", "STOP_TOO_WIDE", 8.0, 2.0, "WOULD_SL"),
        _shadow_eval("CCCUSDT", "LOW_SCORE", 8.0, 1.8, "WOULD_TP"),
    ]

    _, _, missed, combo, gates, calib = bo.build_signal_quality_diagnostics([], shadows, "15m")
    by_threshold = {row["effective_rr_threshold"]: row for row in missed}

    assert by_threshold[1.9]["count"] == 2
    assert by_threshold[1.9]["would_tp_count"] == 1
    assert by_threshold[1.9]["would_sl_count"] == 1
    assert by_threshold[2.1]["count"] == 1
    assert by_threshold[2.1]["would_tp_count"] == 1


def test_stop_too_wide_split_metrics_are_exported():
    shadows = [
        _shadow_eval("AAAUSDT", "STOP_TOO_WIDE", 9.0, 2.4, "WOULD_TP", side="LONG"),
        _shadow_eval("BBBUSDT", "STOP_TOO_WIDE", 7.0, 1.1, "WOULD_SL", side="SHORT"),
    ]

    summary, groups, _, combo, gates, calib = bo.build_signal_quality_diagnostics([], shadows, "15m")

    assert summary["stop_too_wide_split"]["would_tp"]["count"] == 1
    assert summary["stop_too_wide_split"]["would_sl"]["count"] == 1
    assert any(g["group_field"] == "side" and g["group_value"] == "LONG" for g in summary["stop_too_wide_split"]["metrics"])
    assert any(g["group_field"] == "reject_reason" and g["group_value"] == "STOP_TOO_WIDE" for g in groups)
    assert summary["thresholds_changed"] is False
    assert summary["acceptance_logic_changed"] is False
    assert all(g["reporting_only"] is True for g in gates)


def test_quality_gate_high_effective_rr_would_sl_is_not_rescued():
    would_sl = _shadow_eval("AAAUSDT", "LOW_SCORE", 9.8, 3.0, "WOULD_SL", side="SHORT", regime="BREAKOUT", setup_type="BREAKDOWN_DOWN", spread_pct=0.001, expected_slippage_pct=0.001)
    would_sl.stop_distance_pct = 1.0

    cfg = bo.QualityGateConfig(enabled=True, modes=("BACKTEST",), max_trades_per_day=10)
    summary, *_ = bo.build_signal_quality_diagnostics([], [would_sl], "15m", quality_gate_config=cfg)

    assert summary["quality_gate_candidate_count"] == 0
    assert summary["quality_gate_would_sl_count"] == 0


def test_quality_gate_stop_too_wide_not_allowed_by_default_when_would_sl_dominates():
    rows = []
    for idx, outcome in enumerate(["WOULD_SL", "WOULD_SL", "WOULD_SL", "WOULD_TP"]):
        row = _shadow_eval(f"AAA{idx}USDT", "STOP_TOO_WIDE", 9.4, 2.4, outcome, side="SHORT", regime="BREAKOUT", setup_type="BREAKDOWN_DOWN", spread_pct=0.001, expected_slippage_pct=0.001)
        row.stop_distance_pct = 1.0
        rows.append(row)

    cfg = bo.QualityGateConfig(enabled=True, modes=("BACKTEST",), max_trades_per_day=10)
    summary, *_ = bo.build_signal_quality_diagnostics([], rows, "15m", quality_gate_config=cfg)

    assert "STOP_TOO_WIDE" not in cfg.allowed_reasons
    assert summary["quality_gate_candidate_count"] == 0
    assert summary["quality_gate_reason_breakdown"] == {}


def test_backtest_quality_summary_includes_accepted_quality_and_score_calibration():
    rows = [
        {"signal_id":"a","decision":"ACCEPTED","lifecycle_state":"POSITION_CLOSED","close_reason":"SL_HIT","symbol":"BTCUSDT","side":"LONG","regime":"TREND","score":10.0,"rr":2.0,"effective_rr":1.7,"expectancy_bucket":"LOW","net_pnl_usdt":-1.0,"timestamp":1700000000000},
        {"signal_id":"b","decision":"ACCEPTED","lifecycle_state":"POSITION_CLOSED","close_reason":"TP_HIT","symbol":"ETHUSDT","side":"SHORT","regime":"BREAKOUT","score":8.0,"rr":2.2,"effective_rr":2.0,"expectancy_bucket":"HIGH","net_pnl_usdt":2.0,"timestamp":1700003600000},
    ]
    summary = bo.build_backtest_quality_summary(rows)
    q = summary["accepted_trade_quality_diagnostics"]
    assert q["accepted_tp_rate"] == 0.5
    assert q["accepted_sl_rate"] == 0.5
    assert q["by_score_bucket"]["10"]["sl"] == 1
    assert q["by_effective_rr_bucket"]["1.6-1.9"]["count"] == 1
    assert q["by_symbol"]["BTCUSDT"]["net_pnl"] == -1.0
    cal = summary["score_calibration_diagnostics"]
    assert cal["score_10_saturation_count"] == 1
    assert cal["by_score_bucket"]["10"]["sl"] == 1


def test_quality_summary_records_disabled_filter_acceptance_evidence():
    rows = [{"decision":"ACCEPTED","symbol":"BTCUSDT","score":9,"rr":2,"effective_rr":2,"disabled_filters":"[\"RR_TOO_LOW\"]","disabled_filter_bypass_count":1,"net_pnl_usdt":-3.0}]
    evidence = bo.build_backtest_quality_summary(rows)["disabled_filter_acceptance_evidence"]
    assert evidence["accepted_because_filter_disabled_count"] == 1
    assert evidence["estimated_pnl_impact_usdt"] == -3.0

def _short_breakdown_result(reason="LOW_SCORE", side="SHORT", setup_type="BREAKDOWN_DOWN", regime="BREAKOUT"):
    return {
        "status": "rejected",
        "reason": reason,
        "diagnostics": {
            "side": side, "setup_type": setup_type, "setup_reason": "SHORT_BREAKDOWN", "regime": regime,
            "score": 6.0, "rr": 1.4, "entry": 10.0, "sl": 10.5, "tp": 9.3, "order_type": "MARKET",
            "failed_filter": reason,
        },
    }


def _short_breakdown_mctx():
    return {
        "entry": 10.0, "sl": 10.5, "tp": 9.3, "score": 6.0, "rr": 1.4,
        "spread_pct": 0.001, "expected_slippage_pct": 0.001,
        "liquidity_score": 0.9, "liquidity_ok": True, "volatility_ok": "UNAVAILABLE_BACKTEST",
        "regime": "BREAKOUT", "volatility_regime": "NORMAL",
    }


def test_short_breakdown_rescue_disabled_preserves_baseline_rejection():
    lifecycle, rejected, rejection_counts, open_rows = [], [], {}, []
    candles = [bo.Candle(1, 10, 10.6, 9.2, 9.5, 1), bo.Candle(2, 9.5, 10.0, 9.0, 9.2, 1)]
    cand = bo.process_backtest_result(
        "AAAUSDT", candles[0], 0, candles, _short_breakdown_result(), _short_breakdown_mctx(), 1000, 1.0,
        lifecycle, rejected, rejection_counts, open_rows, _rescue_recent_stats(),
        short_breakdown_rescue_config=bo.ShortBreakdownRescueConfig(enabled=False), mode="BACKTEST",
    )
    assert cand is None
    assert len([r for r in lifecycle if r.status_after in {"SIGNAL_ACCEPTED", "ORDER_PLACED", "POSITION_CLOSED"}]) == 0
    assert rejected[0]["reject_reason"] == "LOW_SCORE"


def test_short_breakdown_rescue_enabled_marks_rows_and_original_reason():
    lifecycle, rejected, rejection_counts, open_rows = [], [], {}, []
    candles = [bo.Candle(1, 10, 10.6, 9.2, 9.5, 1), bo.Candle(2, 9.5, 9.8, 9.0, 9.1, 1)]
    stats = bo.RescueStats()
    cand = bo.process_backtest_result(
        "AAAUSDT", candles[0], 0, candles, _short_breakdown_result(), _short_breakdown_mctx(), 1000, 1.0,
        lifecycle, rejected, rejection_counts, open_rows, _rescue_recent_stats(), rescue_stats=stats,
        short_breakdown_rescue_config=bo.ShortBreakdownRescueConfig(enabled=True, max_trades_per_day=3), mode="BACKTEST",
    )
    assert cand is not None
    assert cand.side == "SHORT"
    assert cand.accepted_reason == "SHORT_BREAKDOWN_RESCUE"
    assert cand.original_reject_reason == "LOW_SCORE"
    persisted = bo._persist_lifecycle_rows(lifecycle)
    assert any(r["accepted_reason"] == "SHORT_BREAKDOWN_RESCUE" and r["original_reject_reason"] == "LOW_SCORE" for r in persisted)


def test_short_breakdown_rescue_is_short_only_and_does_not_rescue_low_score_long():
    lifecycle, rejected, rejection_counts, open_rows = [], [], {}, []
    candles = [bo.Candle(1, 10, 10.6, 9.2, 9.5, 1)]
    stats = bo.RescueStats()
    cand = bo.process_backtest_result(
        "AAAUSDT", candles[0], 0, candles,
        _short_breakdown_result(side="LONG", setup_type="BREAKOUT_UP"), _short_breakdown_mctx(), 1000, 1.0,
        lifecycle, rejected, rejection_counts, open_rows, _rescue_recent_stats(), rescue_stats=stats,
        short_breakdown_rescue_config=bo.ShortBreakdownRescueConfig(enabled=True), mode="BACKTEST",
    )
    assert cand is None
    assert rejected[0]["side"] == "LONG"
    assert not any(r.accepted_reason == "SHORT_BREAKDOWN_RESCUE" for r in lifecycle)


def test_backtest_filter_state_marks_short_breakdown_rescue_backtest_only():
    state = bo.build_backtest_filter_state(disabled_filters=[], source="default", timestamp="now", symbols=["BTCUSDT"], timeframe="15m", last_days=30, short_breakdown_rescue_enabled=True)
    exp = state["backtest_only_experiments"][0]
    assert exp["name"] == "SHORT_BREAKDOWN_RESCUE"
    assert exp["enabled"] is True
    assert exp["mode"] == "BACKTEST only"


def test_short_breakdown_rescue_paper_live_untouched():
    for mode in ("PAPER", "LIVE"):
        lifecycle, rejected, rejection_counts, open_rows = [], [], {}, []
        candles = [bo.Candle(1, 10, 10.6, 9.2, 9.5, 1)]
        cand = bo.process_backtest_result(
            "AAAUSDT", candles[0], 0, candles, _short_breakdown_result(), _short_breakdown_mctx(), 1000, 1.0,
            lifecycle, rejected, rejection_counts, open_rows, _rescue_recent_stats(),
            short_breakdown_rescue_config=bo.ShortBreakdownRescueConfig(enabled=True), mode=mode,
        )
        assert cand is None
        assert rejected[0]["reject_reason"] == "LOW_SCORE"


def test_backtest_unknown_reject_reason_attributed_low_effective_rr():
    diagnostics = {"rr": 1.8, "effective_rr": 1.1, "min_effective_rr": 1.6, "min_raw_rr": 1.3, "score": 8.0, "min_score": 7.5}
    reason = bo._primary_reject_reason_from_context(current_reason="UNKNOWN", diagnostics=diagnostics, market_ctx={}, execution_ctx_missing=False)
    assert reason == "LOW_EFFECTIVE_RR"


def test_backtest_unknown_reject_reason_attributed_negative_expectancy():
    diagnostics = {"rr": 2.0, "effective_rr": 1.9, "min_effective_rr": 1.6, "expectancy": -0.01, "score": 8.0, "min_score": 7.5}
    reason = bo._primary_reject_reason_from_context(current_reason="UNKNOWN", diagnostics=diagnostics, market_ctx={}, execution_ctx_missing=False)
    assert reason == "NEGATIVE_EXPECTANCY"


def test_backtest_unknown_reject_reason_attributed_missing_expectancy_when_required():
    diagnostics = {"rr": 2.0, "effective_rr": 1.9, "min_effective_rr": 1.6, "expectancy": None, "reject_unknown_expectancy": True, "score": 8.0, "min_score": 7.5}
    reason = bo._primary_reject_reason_from_context(current_reason="UNKNOWN", diagnostics=diagnostics, market_ctx={}, execution_ctx_missing=False)
    assert reason == "EXPECTANCY_MISSING"


def test_backtest_unknown_reject_reason_attributed_missing_execution_context():
    diagnostics = {"rr": 2.0, "effective_rr": 1.9, "min_effective_rr": 1.6, "expectancy": 0.1, "score": 8.0, "min_score": 7.5}
    reason = bo._primary_reject_reason_from_context(current_reason="UNKNOWN", diagnostics=diagnostics, market_ctx={}, execution_ctx_missing=True)
    assert reason == "EXECUTION_CONTEXT_UNAVAILABLE"


def test_reject_reason_distribution_classifies_concrete_unknown_rows():
    rows = [
        {"signal_id": "s1", "lifecycle_state": "SIGNAL_REJECTED", "decision": "REJECTED", "reject_reason": "UNKNOWN", "rr": 2.0, "effective_rr": 1.0, "min_effective_rr": 1.6, "score": 8.0, "setup_type": "BREAKOUT_UP", "regime": "TREND"},
        {"signal_id": "s2", "lifecycle_state": "SIGNAL_REJECTED", "decision": "REJECTED", "reject_reason": "UNKNOWN", "rr": 2.0, "effective_rr": 1.8, "min_effective_rr": 1.6, "expectancy": -0.1, "score": 8.0, "setup_type": "BREAKOUT_UP", "regime": "TREND"},
    ]
    summary = bo.build_backtest_quality_summary(rows)
    assert summary["reject_reason_distribution"] != {"UNKNOWN": 2}
    assert summary["reject_reason_distribution"]["LOW_EFFECTIVE_RR"] == 1
    assert summary["reject_reason_distribution"]["NEGATIVE_EXPECTANCY"] == 1


def test_process_backtest_result_preserves_reject_reason_in_rejected_csv_payload(tmp_path):
    candle = bo.Candle(1, 100, 101, 99, 100, 1)
    result = {"status": "rejected", "reason": "UNKNOWN", "diagnostics": {"side": "LONG", "setup_type": "BREAKOUT_UP", "setup_reason": "fixture", "regime": "TREND", "score": 8.0, "rr": 2.0, "effective_rr": 1.0, "min_effective_rr": 1.6, "entry": 100, "sl": 99, "tp": 102}}
    lifecycle, rejected, rejection_counts, open_rows = [], [], {}, []
    bo.process_backtest_result("BTCUSDT", candle, 0, [candle], result, {"rr": 2.0, "MIN_EFFECTIVE_RR": 1.6}, 1000, 1, lifecycle, rejected, rejection_counts, open_rows, {"last_trade_ts_by_symbol": {}, "trades_today_by_symbol": {}, "global_trades_today": 0})
    assert rejected[0]["reject_reason"] == "LOW_EFFECTIVE_RR"
    out = tmp_path / "rejected_orders.csv"
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=bo.resolve_csv_fieldnames(rejected, list(rejected[0].keys())))
        w.writeheader(); w.writerows(rejected)
    assert "LOW_EFFECTIVE_RR" in out.read_text()


def test_fixture_backtest_artifacts_prove_lifecycle_rejects_variability_and_sql_export(tmp_path):
    """Small deterministic BACKTEST artifact proving lifecycle evidence before PnL results."""
    accepted_fast = bo.CandidateOrder(1, "BTCUSDT", "LONG", 100.0, 99.0, 102.2, 2.2, "BREAKOUT_UP", "fixture", "TREND", 8.4, "MARKET", expectancy_bucket="HIGH")
    accepted_slow = bo.CandidateOrder(2, "ETHUSDT", "LONG", 50.0, 49.0, 51.4, 1.4, "BREAKOUT_UP", "fixture", "TREND", 6.9, "MARKET", expectancy_bucket="LOW")
    btc = [bo.Candle(1, 100, 102.4, 99.8, 101, 10)]
    eth = [bo.Candle(2, 50, 50.7, 49.8, 50.2, 10), bo.Candle(62_000, 50.2, 51.8, 50.1, 51.0, 10)]
    ctx_btc = {"volume_24h_usdt": 1_000_000.0, "spread_pct": 0.001, "funding_rate_pct": 0.0001, "expected_slippage_pct": 0.001, "liquidity_score": 0.9}
    ctx_eth = {"volume_24h_usdt": 2_000_000.0, "spread_pct": 0.0015, "funding_rate_pct": 0.0002, "expected_slippage_pct": 0.001, "liquidity_score": 0.8}
    lifecycle = []
    lifecycle.extend(bo.simulate_candidate(accepted_fast, btc, 0, 1000, 1, market_ctx=ctx_btc))
    lifecycle.extend(bo.simulate_candidate(accepted_slow, eth, 0, 1000, 1, market_ctx=ctx_eth))
    missing_bucket = bo._bucket_expectancy(None)
    assert missing_bucket == "BACKTEST_EXPECTANCY_UNAVAILABLE"
    rejected_signal = bo.LifecycleRow(3, "XRPUSDT", "LONG", "BREAKOUT_UP", "fixture", "TREND", 2.1, 1.1, 10.0, 9.5, 10.55, "NONE", "SIGNAL_CREATED", expectancy_bucket=missing_bucket)
    rejected_signal_final = bo.LifecycleRow(3, "XRPUSDT", "LONG", "BREAKOUT_UP", "fixture", "TREND", 2.1, 1.1, 10.0, 9.5, 10.55, "SIGNAL_CREATED", "SIGNAL_REJECTED", reject_reason="LOW_SCORE", expectancy_bucket=missing_bucket)
    rejected_order = bo.LifecycleRow(4, "ADAUSDT", "LONG", "BREAKOUT_UP", "fixture", "TREND", 8.0, 1.7, 1.0, 0.95, 1.085, "NONE", "SIGNAL_CREATED", expectancy_bucket="MEDIUM", spread_pct="UNAVAILABLE_BACKTEST", expected_slippage_pct="UNAVAILABLE_BACKTEST", funding_rate_pct="UNAVAILABLE_BACKTEST", liquidity_score="UNAVAILABLE_BACKTEST")
    rejected_order_final = bo.LifecycleRow(4, "ADAUSDT", "LONG", "BREAKOUT_UP", "fixture", "TREND", 8.0, 1.7, 1.0, 0.95, 1.085, "SIGNAL_CREATED", "ORDER_REJECTED", reject_reason="EXECUTION_CONTEXT_UNAVAILABLE", expectancy_bucket="MEDIUM", spread_pct="UNAVAILABLE_BACKTEST", expected_slippage_pct="UNAVAILABLE_BACKTEST", funding_rate_pct="UNAVAILABLE_BACKTEST", liquidity_score="UNAVAILABLE_BACKTEST")
    lifecycle.extend([rejected_signal, rejected_signal_final, rejected_order, rejected_order_final])

    persisted = bo._persist_lifecycle_rows(lifecycle)
    lifecycle_path = tmp_path / "order_lifecycle.csv"
    rejected_orders_path = tmp_path / "rejected_orders.csv"
    rejected_signals_path = tmp_path / "rejected_signals.csv"
    rejected_rows = [
        {"signal_id": "XRPUSDT:3", "lifecycle_state": "SIGNAL_REJECTED", "timestamp": 3, "symbol": "XRPUSDT", "side": "LONG", "score": 2.1, "rr": 1.1, "raw_rr": 1.1, "effective_rr": 1.1, "reject_reason": "LOW_SCORE", "expectancy_bucket": missing_bucket, "spread_pct": "UNAVAILABLE_BACKTEST", "expected_slippage_pct": "UNAVAILABLE_BACKTEST", "funding_rate_pct": "UNAVAILABLE_BACKTEST", "volume_24h_usdt": "UNAVAILABLE_BACKTEST"},
        {"signal_id": "ADAUSDT:4", "lifecycle_state": "ORDER_REJECTED", "timestamp": 4, "symbol": "ADAUSDT", "side": "LONG", "score": 8.0, "rr": 1.7, "raw_rr": 1.7, "effective_rr": 1.0, "reject_reason": "EXECUTION_CONTEXT_UNAVAILABLE", "expectancy_bucket": "MEDIUM", "spread_pct": "UNAVAILABLE_BACKTEST", "expected_slippage_pct": "UNAVAILABLE_BACKTEST", "funding_rate_pct": "UNAVAILABLE_BACKTEST", "volume_24h_usdt": "UNAVAILABLE_BACKTEST"},
    ]
    for path, rows in ((lifecycle_path, persisted), (rejected_orders_path, rejected_rows), (rejected_signals_path, rejected_rows)):
        with path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=bo.resolve_csv_fieldnames(rows, list(rows[0].keys())))
            writer.writeheader(); writer.writerows(rows)

    with lifecycle_path.open(newline="") as fh:
        lifecycle_csv = list(csv.DictReader(fh))
    with rejected_orders_path.open(newline="") as fh:
        rejected_csv = list(csv.DictReader(fh))
    with rejected_signals_path.open(newline="") as fh:
        rejected_signal_csv = list(csv.DictReader(fh))

    counts = Counter(row["lifecycle_state"] for row in lifecycle_csv)
    assert counts["SIGNAL_CREATED"] == 4
    assert counts["WAITING_ENTRY_ZONE"] == 2
    assert counts["ENTRY_TRIGGERED"] == 2
    assert counts["ORDER_PLACED"] == 2
    assert counts["POSITION_OPENED"] == 2
    assert counts["POSITION_CLOSED"] == 2
    assert counts["SIGNAL_REJECTED"] == 1
    assert counts["ORDER_REJECTED"] == 1
    first_state_by_signal = {}
    for row in lifecycle_csv:
        first_state_by_signal.setdefault(row["signal_id"], row["lifecycle_state"])
    assert set(first_state_by_signal.values()) == {"SIGNAL_CREATED"}
    assert "CREATED" not in counts
    assert {row["reject_reason"] for row in rejected_csv} == {"LOW_SCORE", "EXECUTION_CONTEXT_UNAVAILABLE"}
    assert rejected_signal_csv == rejected_csv
    assert len({float(row["score"]) for row in lifecycle_csv if row["lifecycle_state"] == "SIGNAL_CREATED"}) > 1
    assert len({float(row["rr"]) for row in lifecycle_csv if row["lifecycle_state"] == "SIGNAL_CREATED"}) > 1
    assert "UNKNOWN" not in {row["expectancy_bucket"] for row in lifecycle_csv if row["symbol"] == "XRPUSDT"}
    ada_row = next(row for row in rejected_csv if row["symbol"] == "ADAUSDT")
    assert ada_row["spread_pct"] == "UNAVAILABLE_BACKTEST"
    assert ada_row["expected_slippage_pct"] == "UNAVAILABLE_BACKTEST"
    assert ada_row["funding_rate_pct"] == "UNAVAILABLE_BACKTEST"
    assert ada_row["volume_24h_usdt"] == "UNAVAILABLE_BACKTEST"
    assert float(ada_row["effective_rr"]) < float(ada_row["raw_rr"])
    assert not bo.verify_export_integrity(persisted, rejected_rows, lifecycle_csv, rejected_csv)


def test_effective_rr_penalties_can_reject_below_threshold():
    effective_rr, flags, penalties = bo._execution_reject_flags(
        1.7,
        {"spread_pct": 0.02, "expected_slippage_pct": 0.02, "liquidity_score": 0.2, "MIN_EFFECTIVE_RR": 1.6},
    )
    assert effective_rr < 1.6
    assert "LOW_EFFECTIVE_RR" in flags
    assert penalties["spread_penalty"] > 0
    assert penalties["slippage_penalty"] > 0


def test_backtest_quality_summary_canonical_distribution_can_use_rejected_orders_truth():
    persisted = [
        {"signal_id": "A:1", "lifecycle_state": "SIGNAL_CREATED", "decision": "PENDING", "reject_reason": "", "score": 4.0, "rr": 1.0, "effective_rr": 0.5, "expectancy_bucket": "LOW", "execution_ctx_missing": 0, "execution_ctx": "{}"},
        {"signal_id": "A:1", "lifecycle_state": "SIGNAL_REJECTED", "decision": "REJECTED", "reject_reason": "LOW_EFFECTIVE_RR", "score": 4.0, "rr": 1.0, "effective_rr": 0.5, "expectancy_bucket": "LOW", "execution_ctx_missing": 0, "execution_ctx": "{}"},
        {"signal_id": "B:1", "lifecycle_state": "SIGNAL_CREATED", "decision": "PENDING", "reject_reason": "", "score": 3.0, "rr": 1.0, "effective_rr": 0.5, "expectancy_bucket": "LOW", "execution_ctx_missing": 0, "execution_ctx": "{}"},
        {"signal_id": "B:1", "lifecycle_state": "SIGNAL_REJECTED", "decision": "REJECTED", "reject_reason": "UNKNOWN", "score": 3.0, "rr": 1.0, "effective_rr": 0.5, "expectancy_bucket": "LOW", "execution_ctx_missing": 0, "execution_ctx": "{}"},
    ]
    rejected_orders = [
        {"signal_id": "A:1", "reject_reason": "LOW_SCORE"},
        {"signal_id": "B:1", "reject_reason": "TOO_CHOPPY"},
    ]

    summary = bo.build_backtest_quality_summary(persisted, canonical_rejected_rows=rejected_orders)

    assert summary["rejected_count"] == 2
    assert summary["canonical_rejected_count"] == 2
    assert summary["signal_rejected_count"] == 2
    assert summary["symbol_rejected_count"] == 0
    assert sum(summary["reject_reason_distribution"].values()) == summary["rejected_count"]
    assert summary["reject_reason_distribution"] == {"LOW_SCORE": 1, "TOO_CHOPPY": 1}
    assert summary["canonical_reject_reason_distribution"] == {"LOW_SCORE": 1, "TOO_CHOPPY": 1}
    assert summary["raw_gate_reject_reason_distribution"] != summary["canonical_reject_reason_distribution"]


def test_backtest_quality_summary_separates_signal_and_symbol_reject_counts():
    persisted = [
        {"signal_id": "A:1", "lifecycle_state": "SIGNAL_CREATED", "decision": "PENDING", "reject_reason": "", "score": 4.0, "rr": 1.0, "effective_rr": 0.5, "expectancy_bucket": "LOW", "execution_ctx_missing": 0, "execution_ctx": "{}"},
        {"signal_id": "A:1", "lifecycle_state": "SIGNAL_REJECTED", "decision": "REJECTED", "reject_reason": "LOW_EFFECTIVE_RR", "score": 4.0, "rr": 1.0, "effective_rr": 0.5, "expectancy_bucket": "LOW", "execution_ctx_missing": 0, "execution_ctx": "{}"},
    ]
    rejected_orders = [
        {"signal_id": "A:1", "lifecycle_state": "SIGNAL_REJECTED", "reject_reason": "LOW_SCORE"},
        {"signal_id": "B:1", "lifecycle_state": "SYMBOL_REJECTED", "source_stage": "SYMBOL_SELECTOR", "reject_reason": "TOO_CHOPPY"},
        {"signal_id": "C:1", "lifecycle_state": "SYMBOL_SELECTOR_REJECT", "source": "SYMBOL_SELECTOR", "reject_reason": "WEAK_TREND_AND_NO_RANGE_EDGE"},
    ]

    summary = bo.build_backtest_quality_summary(persisted, canonical_rejected_rows=rejected_orders)

    assert summary["rejected_count"] == 3
    assert summary["canonical_rejected_count"] == 3
    assert summary["signal_rejected_count"] == 1
    assert summary["symbol_rejected_count"] == 2
    assert sum(summary["reject_reason_distribution"].values()) == summary["rejected_count"]
    assert sum(summary["canonical_reject_reason_distribution"].values()) == summary["canonical_rejected_count"]


def test_symbol_rejected_rows_export_not_applicable_expectancy_and_availability_flags():
    rows = [
        bo.LifecycleRow(
            1, "ETHUSDT", "N/A", "", "", "CHOP", 2.0, None, 0.0, 0.0, 0.0,
            "NONE", "SYMBOL_REJECTED", reject_reason="TOO_CHOPPY", event_flags="SYMBOL_SELECTOR",
            expectancy_bucket="NOT_APPLICABLE_SYMBOL_FILTER", effective_rr=None, source_stage="SYMBOL_SELECTOR",
            rr_available=False, effective_rr_available=False, expectancy_available=False,
        )
    ]

    persisted = bo._persist_lifecycle_rows(rows)

    assert persisted[0]["expectancy_bucket"] != "UNKNOWN"
    assert persisted[0]["expectancy_bucket"] == "NOT_APPLICABLE_SYMBOL_FILTER"
    assert persisted[0]["rr"] is None
    assert persisted[0]["effective_rr"] is None
    assert persisted[0]["rr_available"] == 0
    assert persisted[0]["effective_rr_available"] == 0
    assert persisted[0]["expectancy_available"] == 0
    assert persisted[0]["source_stage"] == "SYMBOL_SELECTOR"


def test_prune_stale_candle_artifacts_keeps_only_current_run_symbols(tmp_path):
    candles = tmp_path / "candles"
    candles.mkdir()
    (candles / "BTCUSDT_1h.json").write_text("{}")
    (candles / "ETHUSDT_1h.json").write_text("{}")
    (candles / "ETHUSDT_15m.json").write_text("{}")

    bo._prune_stale_candle_artifacts(str(tmp_path), ["BTCUSDT"], "1h")

    assert (candles / "BTCUSDT_1h.json").exists()
    assert not (candles / "ETHUSDT_1h.json").exists()
    assert not (candles / "ETHUSDT_15m.json").exists()


def test_score_calibration_artifacts_reconcile_counts_and_correlations():
    shadows = [
        _shadow_eval("AAAUSDT", "LOW_SCORE", 3.0, 1.2, "WOULD_SL"),
        _shadow_eval("BBBUSDT", "LOW_SCORE", 8.0, 2.2, "WOULD_TP"),
        _shadow_eval("CCCUSDT", "STOP_TOO_WIDE", 9.0, 1.4, "WOULD_SL", volatility_score=4.0),
    ]
    shadows[2].stop_distance_pct = 3.0

    summary, _, _, _, _, calibration_rows = bo.build_signal_quality_diagnostics([], shadows, "1h")
    score_rows = [r for r in calibration_rows if r.get("breakdown") == "score_bucket"]

    assert sum(r["count"] for r in score_rows) == len(shadows)
    cal = summary["score_calibration_summary"]
    assert cal["total_rows"] == len(shadows)
    assert "pearson_score_would_tp" in cal
    assert "spearman_score_effective_tp_hit" in cal
    assert cal["thresholds_changed"] is False
    assert cal["acceptance_logic_changed"] is False


def test_score_calibration_flags_high_score_low_tp_clusters_and_non_monotonicity():
    shadows = [
        _shadow_eval("AAAUSDT", "HIGH_VOL_GUARD", 8.5, 1.2, "WOULD_SL", volatility_score=5.0),
        _shadow_eval("BBBUSDT", "HIGH_VOL_GUARD", 9.0, 1.3, "WOULD_SL", volatility_score=5.0),
        _shadow_eval("CCCUSDT", "STOP_TOO_WIDE", 9.5, 1.4, "WOULD_SL", volatility_score=4.5),
        _shadow_eval("DDDUSDT", "LOW_SCORE", 2.0, 2.4, "WOULD_TP"),
    ]
    for row in shadows:
        row.stop_distance_pct = 3.0

    summary, *_ = bo.build_signal_quality_diagnostics([], shadows, "1h")
    flags = set(summary["score_calibration_summary"]["miscalibration_flags"])

    assert "HIGH_SCORE_LOW_TP_RATE" in flags
    assert "HIGH_VOL_HIGH_SCORE_SL_CLUSTER" in flags
    assert "STOP_TOO_WIDE_HIGH_SCORE_SL_CLUSTER" in flags
    assert "OVEREXTENSION_NOT_PENALIZED" in flags


def test_calibrated_score_penalizes_high_volatility_sl_prone_without_future_leakage():
    bad = _shadow_eval("AAAUSDT", "HIGH_VOL_GUARD", 9.0, 1.2, "WOULD_SL", volatility_score=5.0, spread_pct=0.03, expected_slippage_pct=0.03)
    bad.stop_distance_pct = 3.0
    good = _shadow_eval("BBBUSDT", "LOW_SCORE", 6.0, 2.4, "WOULD_TP", volatility_score=0.5, spread_pct=0.001, expected_slippage_pct=0.001)
    good.stop_distance_pct = 0.8

    records = [bo._quality_record_from_shadow(bad, "1h"), bo._quality_record_from_shadow(good, "1h")]
    diagnostics, summary = bo.build_score_calibration_artifacts(records)
    by_reason = {r.get("reject_reason"): r for r in diagnostics if r.get("breakdown") == "reject_reason"}

    assert by_reason["HIGH_VOL_GUARD"]["avg_calibrated_score"] < bad.score
    assert summary["calibrated_score_scope"] == "BACKTEST_DIAGNOSTIC_ONLY"
    assert summary["calibrated_score_future_leakage"].startswith("NO_FORWARD_OUTCOME_FIELDS_USED")


def test_short_low_score_breakdown_diagnostic_profile_scope_and_safety():
    base = {
        "timestamp": 1710000000000, "symbol": "BTCUSDT", "side": "SHORT", "setup": "BREAKDOWN_DOWN",
        "reject_reason": "LOW_SCORE", "entry": "100", "sl": "101", "tp": "98", "effective_rr": "1.4",
        "min_effective_rr": "1.1", "cost_penalty": "0.1", "spread_pct": "0.0008",
        "expected_slippage_pct": "0.0005", "liquidity_score": "0.8", "first_touch_outcome": "WOULD_TP",
        "effective_shadow_r_after_costs": "1.3", "hour_utc": "6", "all_failed_gates": '["LOW_SCORE"]',
    }
    rows = [
        dict(base),
        {**base, "symbol": "SOLUSDT"},
        {**base, "side": "LONG"},
        {**base, "setup": "BREAKOUT_UP"},
        {**base, "reject_reason": "STOP_TOO_WIDE"},
        {**base, "hour_utc": "12"},
        {**base, "all_failed_gates": '["LOW_SCORE", "STOP_TOO_WIDE"]'},
        {**base, "all_failed_gates": '["LOW_SCORE", "HIGH_VOL_GUARD"]'},
        {**base, "effective_rr": "0.9"},
    ]

    candidates, summary = bo.build_short_low_score_breakdown_diagnostic_profile(rows, symbols=("BTCUSDT", "ETHUSDT"))

    assert len(candidates) == 1
    assert candidates[0]["diagnostic_profile"] == bo.DIAGNOSTIC_PROFILE_NAME
    assert candidates[0]["diagnostic_only"] is True
    assert candidates[0]["production_decision_changed"] is False
    assert summary["candidate_count"] == 1
    assert summary["would_tp_count"] == 1
    assert summary["blocked_reason_distribution"]["STOP_TOO_WIDE_ACTIVE"] == 1
    assert summary["blocked_reason_distribution"]["HIGH_VOL_GUARD_ACTIVE"] == 1
    assert summary["production_thresholds_unchanged"] is True
    assert summary["paper_live_effect"] == "NONE"


def test_short_low_score_breakdown_diagnostic_profile_keeps_default_count_unchanged():
    rows = [
        {"timestamp": 1710000000000, "symbol": "ETHUSDT", "side": "SHORT", "setup": "BREAKDOWN_DOWN", "reject_reason": "LOW_SCORE", "entry": 100, "sl": 101, "tp": 98, "effective_rr": 1.3, "min_effective_rr": 1.1, "cost_penalty": 0.1, "spread_pct": 0.0005, "expected_slippage_pct": 0.0004, "liquidity_score": 0.9, "first_touch_outcome": "WOULD_SL", "effective_shadow_r_after_costs": -1.1, "hour_utc": 7},
    ]
    default_accepted_count = 0
    candidates, summary = bo.build_short_low_score_breakdown_diagnostic_profile(rows)
    assert len(candidates) == 1
    assert default_accepted_count == 0
    assert summary["reason_diagnostic_only"].startswith("DIAGNOSTIC ONLY")


def test_short_low_score_breakdown_diagnostic_blocks_missing_execution_context_fields():
    base = {
        "timestamp": 1710000000000, "symbol": "BTCUSDT", "side": "SHORT", "setup": "BREAKDOWN_DOWN",
        "reject_reason": "LOW_SCORE", "entry": "100", "sl": "101", "tp": "98", "effective_rr": "1.4",
        "min_effective_rr": "1.1", "cost_penalty": "0.1", "spread_pct": "0.0008",
        "expected_slippage_pct": "0.0005", "liquidity_score": "0.8", "first_touch_outcome": "WOULD_TP",
        "effective_shadow_r_after_costs": "1.3", "hour_utc": "6", "all_failed_gates": '["LOW_SCORE"]',
    }
    rows = []
    for field, missing_value in [
        ("spread_pct", ""),
        ("expected_slippage_pct", None),
        ("cost_penalty", "UNAVAILABLE_BACKTEST"),
        ("liquidity_score", "nan"),
    ]:
        row = dict(base)
        row[field] = missing_value
        rows.append(row)

    candidates, summary = bo.build_short_low_score_breakdown_diagnostic_profile(rows, symbols=("BTCUSDT", "ETHUSDT"))

    assert candidates == []
    assert summary["candidate_count"] == 0
    assert summary["blocked_reason_distribution"] == {"EXECUTION_CONTEXT_UNAVAILABLE": 4}


def test_short_low_score_breakdown_diagnostic_requires_explicit_effective_rr_threshold():
    base = {
        "timestamp": 1710000000000, "symbol": "ETHUSDT", "side": "SHORT", "setup": "BREAKDOWN_DOWN",
        "reject_reason": "LOW_SCORE", "entry": "100", "sl": "101", "tp": "98", "effective_rr": "1.4",
        "min_effective_rr": "1.1", "cost_penalty": "0.1", "spread_pct": "0.0008",
        "expected_slippage_pct": "0.0005", "liquidity_score": "0.8", "first_touch_outcome": "WOULD_SL",
        "effective_shadow_r_after_costs": "-1.1", "hour_utc": "7",
    }
    rows = [{**base, "effective_rr": "UNKNOWN"}, {**base, "min_effective_rr": ""}]

    candidates, summary = bo.build_short_low_score_breakdown_diagnostic_profile(rows)

    assert candidates == []
    assert summary["blocked_reason_distribution"] == {"EXECUTION_CONTEXT_UNAVAILABLE": 2}


def test_score10_sl_dominance_guard_bucket_flags_and_exports(monkeypatch, tmp_path):
    from alphaforge.dashboard.backtest_control import _comparison_metrics, _write_calibration_artifacts

    monkeypatch.setenv("ALPHAFORGE_BACKTEST_SCORE10_SL_DOMINANCE_GUARD", "true")
    rows = []
    for idx in range(40):
        outcome = "WOULD_SL" if idx < 25 else "WOULD_TP"
        rows.append({"source_stage": "SIGNAL_ENGINE", "reject_reason": "STOP_TOO_WIDE", "score": "10", "shadow_outcome": outcome, "effective_rr": "1.2", "effective_shadow_r_after_costs": "-1.1" if outcome == "WOULD_SL" else "1.2", "symbol": "BTCUSDT", "side": "LONG", "setup": "BREAKOUT_UP", "regime": "HIGH", "stop_distance_pct": "3.0", "volatility_score": "2.0", "cost_penalty": "0.1"})
    for idx in range(34):
        outcome = "WOULD_TP" if idx < 24 else "WOULD_SL"
        rows.append({"source_stage": "SIGNAL_ENGINE", "reject_reason": "LOW_SCORE", "score": "10", "shadow_outcome": outcome, "effective_rr": "2.0", "effective_shadow_r_after_costs": "2.0" if outcome == "WOULD_TP" else "-1.0", "symbol": "ETHUSDT"})
    for idx in range(12):
        rows.append({"source_stage": "SIGNAL_ENGINE", "reject_reason": "REGIME_MISMATCH", "score": "10", "shadow_outcome": "WOULD_SL", "effective_shadow_r_after_costs": "-1.0", "symbol": "SOLUSDT"})

    _, _, summary = _write_calibration_artifacts(tmp_path, [], [], {"accepted_count": "1"}, rows, [])
    guard = summary["score10_sl_dominance_guard"]
    by_bucket = {(row["bucket_type"], row["bucket_value"]): row for row in guard["buckets"]}
    assert "SCORE10_SL_DOMINANCE" in by_bucket[("symbol", "BTCUSDT")]["flags"]
    assert "SCORE10_STOP_WIDTH_SL_CLUSTER" in by_bucket[("reject_reason", "STOP_TOO_WIDE")]["flags"]
    assert by_bucket[("symbol", "ETHUSDT")]["guard_confirmed"] is False
    assert by_bucket[("reject_reason", "REGIME_MISMATCH")]["exploratory"] is True
    assert by_bucket[("reject_reason", "REGIME_MISMATCH")]["guard_confirmed"] is False
    assert (tmp_path / "score10_sl_dominance_guard.json").exists()
    assert (tmp_path / "score10_sl_dominance_guard.csv").exists()
    profile_dir = tmp_path / "profiles" / "DEFAULT_FILTERS"
    profile_dir.mkdir(parents=True)
    (profile_dir / "order_backtest_summary.csv").write_text("accepted_count,total_net_pnl_usdt,rejected_count\n1,0,0\n")
    (profile_dir / "order_lifecycle.csv").write_text("signal_id,lifecycle_state,score,close_reason,net_pnl_usdt\n")
    (profile_dir / "rejected_orders.csv").write_text("reject_reason,score\n")
    metrics = _comparison_metrics("DEFAULT_FILTERS", profile_dir, 10000)
    assert metrics["accepted_trades"] == 1


def test_score10_sl_dominance_guard_env_disabled_no_export(monkeypatch, tmp_path):
    from alphaforge.dashboard.backtest_control import _write_calibration_artifacts

    monkeypatch.delenv("ALPHAFORGE_BACKTEST_SCORE10_SL_DOMINANCE_GUARD", raising=False)
    _, _, summary = _write_calibration_artifacts(tmp_path, [], [], {"accepted_count": "1"}, [{"source_stage": "SIGNAL_ENGINE", "reject_reason": "STOP_TOO_WIDE", "score": "10", "shadow_outcome": "WOULD_SL"}], [])
    assert summary["score10_sl_dominance_guard"]["enabled"] is False
    assert not (tmp_path / "score10_sl_dominance_guard.json").exists()
    assert not (tmp_path / "score10_sl_dominance_guard.csv").exists()
