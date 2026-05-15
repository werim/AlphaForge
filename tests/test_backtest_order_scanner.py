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
    assert shadow.spread_pct == 0.1


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


def test_rejected_counterfactual_same_candle_sl_priority():
    c = bo.CandidateOrder(1, "S", "LONG", 10, 9, 11, 1, "BACKTEST", "R", "X", 1, "LIMIT")
    candles = [bo.Candle(1, 10, 11.2, 8.8, 10.5, 1)]
    sim = bo.simulate_rejected_counterfactual(c, candles, 0)
    assert sim["outcome"] == "WOULD_SL"


def test_rejected_counterfactual_uses_bounded_lookahead_timeout():
    c = bo.CandidateOrder(1, "S", "LONG", 10, 9, 12, 2, "BACKTEST", "R", "X", 1, "LIMIT")
    candles = [bo.Candle(i, 10, 10.1, 9.9, 10, 1) for i in range(1, 8)]
    sim = bo.simulate_rejected_counterfactual(c, candles, 0, timeout_bars=3)
    assert sim["outcome"] == "WOULD_TIMEOUT"


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
        "diagnostics": {"side": "LONG", "setup_type": "BREAKOUT_UP", "setup_reason": "X", "regime": "TREND", "score": 2.0, "rr": 1.2, "entry": 10.0, "sl": 9.5, "tp": 11.0},
    }
    bo.process_backtest_result("AAAUSDT", candles[0], 0, candles, rejected_result, {"entry": 10.0, "sl": 9.5, "tp": 11.0}, 1000, 1.0, lifecycle, rejected, rejection_counts, open_rows, recent_stats)

    executed_result = {
        "status": "executed",
        "candidate": type("X", (), {"side": "LONG", "entry": 10.0, "sl": 9.5, "tp": 10.8, "rr": 1.6, "setup_type": "BREAKOUT_UP", "setup_reason": "Y", "regime": "TREND", "score": 8.0, "order_type": "MARKET"})(),
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
    assert persisted[1]["lifecycle_state"] == "SIGNAL_REJECTED"
    assert persisted[1]["reject_reason"] == "LOW_SCORE"




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
