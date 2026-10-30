import asyncio

from alphaforge.burnin_ops import VALID_INTERVALS
from alphaforge.exchange_market_scanner import (
    SUPPORTED_BINANCE_DECISION_TIMEFRAMES,
    _timeframe_seconds,
)
from alphaforge.historical_market_data import supported_intervals
from alphaforge.multi_timeframe import BinanceMTFProvider


def _provider_rows(closes, decision_ms):
    rows = []
    for index, close in enumerate(closes):
        close_ms = decision_ms - (len(closes) - index - 1) * 60_000
        rows.append([
            close_ms - 59_999,
            str(close + 0.02),
            str(close + 0.08),
            str(close - 0.08),
            str(close),
            "100",
            close_ms,
        ])
    return rows


def test_balanced_mtf_intervals_are_supported_across_runtime_contracts():
    expected = {"5m", "30m", "2h"}

    assert expected <= set(VALID_INTERVALS)
    assert expected <= set(supported_intervals())
    assert expected <= set(SUPPORTED_BINANCE_DECISION_TIMEFRAMES)
    assert _timeframe_seconds("5m") == 300
    assert _timeframe_seconds("30m") == 1_800
    assert _timeframe_seconds("2h") == 7_200


def test_binance_mtf_provider_builds_balanced_2h_30m_5m_context(monkeypatch):
    decision_ms = 20_000_000
    rows_by_tf = {
        "2h": _provider_rows([103 - i * 0.1 for i in range(24)], decision_ms),
        "30m": _provider_rows([100 + i * 0.1 for i in range(16)], decision_ms),
        "5m": _provider_rows([102.15 - i * 0.1 for i in range(8)], decision_ms),
    }
    seen = []
    provider = BinanceMTFProvider()

    def fake_fetch(_symbol, timeframe):
        seen.append(timeframe)
        return rows_by_tf[timeframe]

    monkeypatch.setattr(provider, "_fetch", fake_fetch)
    canonical = {
        "spread_pct": 0.0002,
        "expected_slippage_pct": 0.0004,
        "market_data_latency_ms": 20.0,
        "liquidity_score": 0.9,
    }

    mtf = asyncio.run(
        provider.build(
            "BTCUSDT",
            canonical,
            execution_ctx=canonical,
            decision_ts_ms=decision_ms,
            regime_timeframe="2h",
            setup_timeframe="30m",
            execution_timeframe="5m",
        )
    )

    assert set(seen) == {"2h", "30m", "5m"}
    assert mtf["regime"]["timeframe"] == "2h"
    assert mtf["setup"]["timeframe"] == "30m"
    assert mtf["execution"]["timeframe"] == "5m"
    assert "MTF_REGIME_UNAVAILABLE" not in mtf["alignment"]["reasons"]
    assert "MTF_SETUP_UNAVAILABLE" not in mtf["alignment"]["reasons"]
    assert "MTF_EXECUTION_UNAVAILABLE" not in mtf["alignment"]["reasons"]
    assert "MTF_CONTEXT_STALE" not in mtf["alignment"]["reasons"]
