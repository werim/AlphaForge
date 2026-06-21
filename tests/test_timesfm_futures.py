from __future__ import annotations

import os

import numpy as np
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


def test_timesfm_replay_rejects_live_mode_without_order_path() -> None:
    with pytest.raises(Exception, match="PAPER/BACKTEST only"):
        replay_timesfm_backtest(_candles(3), forecaster=RecordingForecaster(), timeframe="15m", horizon=8, min_history=1, mode="LIVE")


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


def test_timesfm_tuple_numpy_batched_mean_plus_deciles_extracts_true_p10_p50_p90() -> None:
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


def test_timesfm_tuple_numpy_batched_older_nine_quantile_layout_is_supported() -> None:
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


def test_timesfm_tuple_numpy_unbatched_mean_plus_deciles_extracts_true_p10_p50_p90() -> None:
    class UnbatchedMeanPlusDecilesModel:
        def forecast(self, *, inputs, horizon):
            assert horizon == 8
            point = np.array([100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0])
            quantiles = np.zeros((8, 10), dtype=float)
            quantiles[:, 0] = 999.0  # mean column; must not be labeled p10.
            quantiles[:, 1] = 90.0
            quantiles[:, 5] = 104.0
            quantiles[:, 9] = 110.0
            return point, quantiles

    from alphaforge.models.timesfm_forecaster import TimesFMForecaster

    forecast = TimesFMForecaster(UnbatchedMeanPlusDecilesModel()).forecast_quantiles([100.0] * 64, 8)
    assert forecast == QuantileForecast(horizon=8, p10=90.0, p50=104.0, p90=110.0)


def test_timesfm_tuple_numpy_unbatched_older_nine_quantile_layout_is_supported() -> None:
    class UnbatchedNineQuantileModel:
        def forecast(self, inputs, horizon_len):
            point = np.array([100.0, 101.0, 102.0])
            quantiles = np.zeros((horizon_len, 9), dtype=float)
            quantiles[:, 0] = 91.0
            quantiles[:, 4] = 105.0
            quantiles[:, 8] = 111.0
            return point, quantiles

    from alphaforge.models.timesfm_forecaster import TimesFMForecaster

    forecast = TimesFMForecaster(UnbatchedNineQuantileModel()).forecast_quantiles([100.0] * 64, 3)
    assert forecast == QuantileForecast(horizon=3, p10=91.0, p50=105.0, p90=111.0)


def test_timesfm_forecaster_tries_legacy_freq_signature() -> None:
    class LegacyFreqModel:
        def forecast(self, inputs, freq):
            assert freq == [0]
            return np.array([[100.0, 101.0]]), np.array([[[90.0, 100.0, 110.0], [91.0, 101.0, 111.0]]])

    from alphaforge.models.timesfm_forecaster import TimesFMForecaster

    forecast = TimesFMForecaster(LegacyFreqModel()).forecast_quantiles([100.0] * 64, 2)
    assert forecast == QuantileForecast(horizon=2, p10=91.0, p50=101.0, p90=111.0)


def test_timesfm_malformed_numpy_output_raises_forecast_error() -> None:
    class BadModel:
        def forecast(self, *, inputs, horizon):
            return np.array([[100.0] * horizon]), np.array([[[999.0, 110.0, 100.0, 90.0]] * horizon])

    from alphaforge.models.timesfm_forecaster import TimesFMForecaster

    with pytest.raises(TimesFMForecastError):
        TimesFMForecaster(BadModel()).forecast_quantiles([100.0] * 64, 8)


def test_replay_logs_invalid_forecast_for_malformed_real_shaped_model_output() -> None:
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


@pytest.mark.integration
def test_optional_real_timesfm_integration_smoke() -> None:
    if os.environ.get("ALPHAFORGE_RUN_TIMESFM_INTEGRATION") != "1":
        pytest.skip("set ALPHAFORGE_RUN_TIMESFM_INTEGRATION=1 to run the optional real TimesFM smoke test")

    from alphaforge.models.timesfm_forecaster import TimesFMForecaster

    forecast = TimesFMForecaster.from_timesfm().forecast_quantiles([100.0] * 64, 8)
    assert forecast.horizon == 8
    assert forecast.p10 <= forecast.p50 <= forecast.p90
    assert forecast.p10 > 0

from sqlalchemy import text
from sqlalchemy.orm import Session
from alphaforge.persistence import init_db


def test_timesfm_decision_log_contains_canonical_evidence_fields(tmp_path) -> None:
    from alphaforge.timesfm_futures import write_decision_log

    decisions = replay_timesfm_backtest(_candles(1), forecaster=RecordingForecaster(), timeframe="15m", horizon=8, min_history=1, mode="PAPER")
    path = tmp_path / "timesfm.csv"
    write_decision_log(path, decisions)
    content = path.read_text(encoding="utf-8")
    assert "forecast_id,timestamp,symbol,timeframe,horizon,current_price,forecast_p10,forecast_p50,forecast_p90" in content
    assert "expected_rr,rejection_reason,mode,model_provider,model_name,model_version,no_lookahead_input_end_ts" in content
    assert decisions[0].forecast_id.startswith("timesfm-")
    assert decisions[0].no_lookahead_input_end_ts == decisions[0].timestamp


def test_timesfm_sql_persistence_contains_evidence_rows_and_is_idempotent() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    forecaster = RecordingForecaster(QuantileForecast(horizon=8, p10=99.0, p50=104.0, p90=110.0))
    with Session(engine) as session:
        first = replay_timesfm_backtest(_candles(2), forecaster=forecaster, timeframe="15m", horizon=8, min_history=1, persistence_session=session)
        second = replay_timesfm_backtest(_candles(2), forecaster=forecaster, timeframe="15m", horizon=8, min_history=1, persistence_session=session)
        rows = session.execute(text("SELECT * FROM timesfm_forecast_evidence ORDER BY timestamp")).mappings().all()
        order_rows = session.execute(text("SELECT COUNT(*) FROM order_decisions")).scalar_one()
        label_table = session.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='timesfm_forward_outcome_labels'")).scalar_one_or_none()

    assert [d.forecast_id for d in first] == [d.forecast_id for d in second]
    assert len(rows) == 2
    assert rows[0]["forecast_id"] == first[0].forecast_id
    assert rows[0]["horizon"] == 8
    assert rows[0]["forecast_p10"] == 99.0
    assert rows[0]["forecast_p50"] == 104.0
    assert rows[0]["forecast_p90"] == 110.0
    assert rows[0]["side"] == "LONG"
    assert rows[1]["side"] == "LONG"
    assert rows[0]["mode"] == "BACKTEST"
    assert rows[0]["no_lookahead_input_end_ts"] == rows[0]["timestamp"]
    assert order_rows == 0
    assert label_table == "timesfm_forward_outcome_labels"


def test_invalid_timesfm_forecast_persists_no_trade_invalid_forecast() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        decisions = replay_timesfm_backtest(_candles(1), forecaster=RecordingForecaster(fail=True), timeframe="15m", horizon=8, min_history=1, persistence_session=session)
        row = session.execute(text("SELECT side, rejection_reason, forecast_p10 FROM timesfm_forecast_evidence")).first()
    assert decisions[0].side == "NO_TRADE"
    assert row == ("NO_TRADE", "INVALID_FORECAST", None)


def test_timesfm_default_fixture_persists_low_effective_rr_no_trade() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        decisions = replay_timesfm_backtest(_candles(2), forecaster=RecordingForecaster(), timeframe="15m", horizon=8, min_history=1, persistence_session=session)
        rows = session.execute(text("SELECT side, rejection_reason FROM timesfm_forecast_evidence ORDER BY timestamp")).all()
        order_rows = session.execute(text("SELECT COUNT(*) FROM order_decisions")).scalar_one()

    assert [d.side for d in decisions] == ["NO_TRADE", "NO_TRADE"]
    assert rows == [("NO_TRADE", "LOW_EFFECTIVE_RR"), ("NO_TRADE", "LOW_EFFECTIVE_RR")]
    assert order_rows == 0
