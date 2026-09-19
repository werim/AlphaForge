import importlib.util
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from alphaforge.ai_brain import AIBrain
from alphaforge.persistence import init_db
from alphaforge.scoring_context import build_signal_payload, empty_stats_context, normalize_scoring_context


_spec = importlib.util.spec_from_file_location("backtest_order", Path(__file__).resolve().parents[1] / "backtest_order.py")
bo = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(bo)


def _candles(count=24):
    return [bo.Candle(index * 60_000, 100 + index * .1, 100.3 + index * .1, 99.8 + index * .1, 100.15 + index * .1, 1_000 + index) for index in range(count)]


def _market():
    candles = _candles()
    return candles, bo._build_market_ctx(candles[-1], candles[-2], {"quoteVolume": 100_000_000.0}, candles)


def test_stateless_backtest_and_paper_ai_brains_score_identical_normalized_input():
    market = {"entry": 100.0, "sl": 99.0, "tp": 103.0, "rr": 3.0, "side": "LONG", "setup_type": "BREAKOUT_UP", "regime": "TREND", "liquidity_score": .9,
              "mtf": {"setup": {"setup_quality": .8}, "execution": {"momentum_confirmation": .8, "volatility_fit": .7}, "regime": {"regime": "TRENDING", "regime_alignment": .8}}}
    signal = build_signal_payload("BTCUSDT", market, signal_id="same", default_mode="PAPER")
    normalized, regime, stats = normalize_scoring_context(signal, market, stats_ctx=empty_stats_context())
    paper = AIBrain(Session(init_db("sqlite+pysqlite:///:memory:"))).score_signal(signal, normalized, regime, stats)
    backtest = AIBrain.for_stateless_scoring().score_signal(signal, normalized, regime, stats)
    assert backtest.total_score == pytest.approx(paper.total_score)
    assert backtest.reason_flags == paper.reason_flags


def test_historical_score_is_variable_timestamp_bounded_and_offline(monkeypatch):
    candles, market = _market()
    monkeypatch.setattr(bo, "fetch_json", lambda _url: pytest.fail("scoring attempted network access"))
    at_decision = bo._historical_authoritative_score("BTCUSDT", candles, 20, market, min_accept_score=.62)
    changed_future = [*candles, bo.Candle(1_800_000, 1, 2_000, .1, 1_900, 999_999)]
    unchanged = bo._historical_authoritative_score("BTCUSDT", changed_future, 20, market, min_accept_score=.62)
    weak_market = {**market, "liquidity_score": .1, "spread_pct": .02, "expected_slippage_pct": .02}
    weak = bo._historical_authoritative_score("BTCUSDT", candles, 20, weak_market, min_accept_score=.62)
    assert at_decision["score"] == pytest.approx(unchanged["score"])
    assert at_decision["score"] != weak["score"]
    assert market["funding_rate_pct"] is None
    assert at_decision["diagnostics"]["historical_stats"] == "UNAVAILABLE_ZERO_HISTORY_NO_ASOF_STATS"
