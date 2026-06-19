from __future__ import annotations

import pytest

from alphaforge.historical_market_data import HistoricalCandle
from alphaforge.models.timesfm_forecaster import QuantileForecast, TimesFMForecastError
from alphaforge.timesfm_futures import (
    decide_from_forecast,
    load_btcusdt_futures_ohlcv,
    replay_timesfm_backtest,
)


class RecordingForecaster:
    def __init__(self, forecast: QuantileForecast | None = None, fail: bool = False) -> None:
        self.forecast = forecast or QuantileForecast(horizon=8, p10=90.0, p50=104.0, p90=110.0)
        self.fail = fail
        self.lengths: list[int] = []
        self.last_closes: list[float] = []

    def forecast_quantiles(self, close_prices, horizon: int) -> QuantileForecast:
        self.lengths.append(len(close_prices))
        self.last_closes.append(close_prices[-1])
        if self.fail:
            raise TimesFMForecastError("bad forecast")
        return self.forecast


def _candles(count: int) -> list[HistoricalCandle]:
    return [HistoricalCandle(timestamp=i * 900_000, open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10) for i in range(count)]


def test_loader_uses_binance_futures_btcusdt_15m_and_1h() -> None:
    urls: list[str] = []

    def fetcher(url: str):
        urls.append(url)
        return [[0, 1, 1, 1, 1, 1], [900_000, 1, 1, 1, 1, 1]]

    rows = load_btcusdt_futures_ohlcv("15m", 0, 900_000, fetcher=fetcher)
    assert len(rows) == 2
    assert "https://fapi.binance.com/fapi/v1/klines" in urls[0]
    assert "symbol=BTCUSDT" in urls[0]
    assert "interval=15m" in urls[0]

    with pytest.raises(Exception):
        load_btcusdt_futures_ohlcv("5m", 0, 900_000, fetcher=fetcher)


def test_backtest_replay_prevents_lookahead_bias() -> None:
    forecaster = RecordingForecaster()
    candles = _candles(70)
    decisions = replay_timesfm_backtest(candles, forecaster=forecaster, timeframe="15m", horizon=8, min_history=64)
    assert len(decisions) == 7
    assert forecaster.lengths == [64, 65, 66, 67, 68, 69, 70]
    assert forecaster.last_closes == [candles[i].close for i in range(63, 70)]
    assert decisions[0].timestamp == candles[63].timestamp


def test_invalid_forecast_handling_logs_no_trade_rejection() -> None:
    forecaster = RecordingForecaster(fail=True)
    decision = replay_timesfm_backtest(_candles(3), forecaster=forecaster, timeframe="15m", horizon=8, min_history=1)[0]
    assert decision.side == "NO_TRADE"
    assert decision.rejection_reason == "INVALID_FORECAST"
    assert decision.entry is None


def test_long_decision_from_quantile_forecast() -> None:
    decision = decide_from_forecast(timestamp=1, symbol="BTCUSDT", timeframe="15m", current_price=100.0, forecast=QuantileForecast(8, 99.0, 104.0, 110.0))
    assert decision.side == "LONG"
    assert decision.entry == 100.0
    assert decision.stop == 99.0
    assert decision.take_profit == 110.0
    assert decision.expected_rr == pytest.approx(4.0)
    assert decision.rejection_reason is None


def test_short_decision_from_quantile_forecast() -> None:
    decision = decide_from_forecast(timestamp=1, symbol="BTCUSDT", timeframe="1h", current_price=100.0, forecast=QuantileForecast(16, 90.0, 96.0, 101.0))
    assert decision.side == "SHORT"
    assert decision.entry == 100.0
    assert decision.stop == 101.0
    assert decision.take_profit == 90.0
    assert decision.expected_rr == pytest.approx(4.0)
    assert decision.rejection_reason is None


def test_no_trade_decision_from_low_confidence_forecast() -> None:
    decision = decide_from_forecast(timestamp=1, symbol="BTCUSDT", timeframe="15m", current_price=100.0, forecast=QuantileForecast(24, 99.5, 100.1, 100.6))
    assert decision.side == "NO_TRADE"
    assert decision.rejection_reason == "LOW_CONFIDENCE"
    assert decision.entry is None
