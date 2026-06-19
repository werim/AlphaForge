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


def test_timesfm_tuple_numpy_mean_plus_deciles_extracts_true_p10_p50_p90() -> None:
    np = pytest.importorskip("numpy")

    class MeanPlusDecilesModel:
        def forecast(self, *, inputs, horizon):
            assert horizon == 8
            point = np.array([[100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0]])
            quantiles = np.zeros((1, 8, 10), dtype=float)
            quantiles[:, :, 0] = 999.0  # mean column; must not be labeled p10.
            quantiles[:, :, 1] = 90.0
            quantiles[:, :, 5] = 104.0
            quantiles[:, :, 9] = 110.0
            return point, quantiles

    from alphaforge.models.timesfm_forecaster import TimesFMForecaster

    forecast = TimesFMForecaster(MeanPlusDecilesModel()).forecast_quantiles([100.0] * 64, 8)
    assert forecast.p10 == 90.0
    assert forecast.p50 == 104.0
    assert forecast.p90 == 110.0


def test_timesfm_tuple_numpy_older_nine_quantile_layout_is_supported() -> None:
    np = pytest.importorskip("numpy")

    class NineQuantileModel:
        def forecast(self, inputs, horizon_len):
            point = np.array([[100.0, 101.0, 102.0]])
            quantiles = np.zeros((1, horizon_len, 9), dtype=float)
            quantiles[:, :, 0] = 91.0
            quantiles[:, :, 4] = 105.0
            quantiles[:, :, 8] = 111.0
            return point, quantiles

    from alphaforge.models.timesfm_forecaster import TimesFMForecaster

    forecast = TimesFMForecaster(NineQuantileModel()).forecast_quantiles([100.0] * 64, 3)
    assert forecast == QuantileForecast(horizon=3, p10=91.0, p50=105.0, p90=111.0)


def test_timesfm_forecaster_tries_legacy_freq_signature() -> None:
    np = pytest.importorskip("numpy")

    class LegacyFreqModel:
        def forecast(self, inputs, freq):
            assert freq == [0]
            return np.array([[100.0, 101.0]]), np.array([[[90.0, 100.0, 110.0], [91.0, 101.0, 111.0]]])

    from alphaforge.models.timesfm_forecaster import TimesFMForecaster

    forecast = TimesFMForecaster(LegacyFreqModel()).forecast_quantiles([100.0] * 64, 2)
    assert forecast == QuantileForecast(horizon=2, p10=91.0, p50=101.0, p90=111.0)


def test_timesfm_malformed_numpy_output_raises_forecast_error() -> None:
    np = pytest.importorskip("numpy")

    class BadModel:
        def forecast(self, *, inputs, horizon):
            return np.array([[100.0] * horizon]), np.array([[[999.0, 110.0, 100.0, 90.0]] * horizon])

    from alphaforge.models.timesfm_forecaster import TimesFMForecaster

    with pytest.raises(TimesFMForecastError):
        TimesFMForecaster(BadModel()).forecast_quantiles([100.0] * 64, 8)


def test_replay_logs_invalid_forecast_for_malformed_real_shaped_model_output() -> None:
    np = pytest.importorskip("numpy")

    class BadModelForecaster:
        def forecast_quantiles(self, close_prices, horizon: int) -> QuantileForecast:
            from alphaforge.models.timesfm_forecaster import TimesFMForecaster

            class BadModel:
                def forecast(self, *, inputs, horizon):
                    return np.array([[100.0] * horizon]), np.array([[[999.0, 110.0, 100.0, 90.0]] * horizon])

            return TimesFMForecaster(BadModel()).forecast_quantiles(close_prices, horizon)

    decision = replay_timesfm_backtest(_candles(1), forecaster=BadModelForecaster(), timeframe="15m", horizon=8, min_history=1)[0]
    assert decision.side == "NO_TRADE"
    assert decision.rejection_reason == "INVALID_FORECAST"
