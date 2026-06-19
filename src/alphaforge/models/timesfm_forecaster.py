from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module, util
from typing import Protocol, Sequence


class TimesFMForecastError(RuntimeError):
    pass


@dataclass(frozen=True)
class QuantileForecast:
    horizon: int
    p10: float
    p50: float
    p90: float


def _validate_close_series(close_prices: Sequence[float], horizon: int) -> None:
    if horizon <= 0:
        raise TimesFMForecastError("Forecast horizon must be positive")
    if not close_prices:
        raise TimesFMForecastError("At least one historical close is required")
    for price in close_prices:
        if not isinstance(price, int | float) or price != price or price <= 0:
            raise TimesFMForecastError("Historical close series contains invalid prices")


class ForecastProvider(Protocol):
    def forecast_quantiles(self, close_prices: Sequence[float], horizon: int) -> QuantileForecast:
        ...


class TimesFMForecaster:
    """Thin TimesFM wrapper for PAPER/BACKTEST forecasting only.

    The wrapper intentionally has no order-placement capability. It accepts only the
    close-price history visible at decision time and returns quantiles for the
    requested horizon. The optional ``timesfm`` package is imported lazily so the
    repository remains testable in offline environments; callers that need actual
    TimesFM inference must install and configure the package explicitly.
    """

    def __init__(self, model: object | None = None) -> None:
        self._model = model

    @classmethod
    def from_timesfm(cls, **model_kwargs: object) -> "TimesFMForecaster":
        if util.find_spec("timesfm") is None:
            raise TimesFMForecastError("timesfm package is not installed; cannot run TimesFM inference")
        timesfm = import_module("timesfm")
        model_factory = getattr(timesfm, "TimesFm", None) or getattr(timesfm, "TimesFM", None)
        if model_factory is None:
            raise TimesFMForecastError("Installed timesfm package does not expose a TimesFm/TimesFM factory")
        return cls(model_factory(**model_kwargs))

    def forecast_quantiles(self, close_prices: Sequence[float], horizon: int) -> QuantileForecast:
        _validate_close_series(close_prices, horizon)
        if self._model is None:
            raise TimesFMForecastError("No TimesFM model configured")

        # TimesFM releases expose slightly different call surfaces. Support the
        # common ``forecast(..., horizon_len=...)`` shape without masking model
        # failures; invalid or missing quantiles are rejected by the parser.
        forecast = self._model.forecast([list(close_prices)], horizon_len=horizon)
        return _parse_timesfm_output(forecast, horizon)


def _parse_timesfm_output(raw: object, horizon: int) -> QuantileForecast:
    if isinstance(raw, dict):
        p10 = _last_value(raw.get("p10") or raw.get("0.1"), horizon)
        p50 = _last_value(raw.get("p50") or raw.get("0.5") or raw.get("mean"), horizon)
        p90 = _last_value(raw.get("p90") or raw.get("0.9"), horizon)
    elif isinstance(raw, tuple) and len(raw) >= 2:
        mean, quantiles = raw[0], raw[1]
        p50 = _last_value(mean, horizon)
        p10 = _quantile_value(quantiles, 0, horizon)
        p90 = _quantile_value(quantiles, -1, horizon)
    else:
        raise TimesFMForecastError("Unsupported TimesFM forecast output")

    values = (float(p10), float(p50), float(p90))
    if any(v != v or v <= 0 for v in values) or not values[0] <= values[1] <= values[2]:
        raise TimesFMForecastError("Invalid forecast quantiles")
    return QuantileForecast(horizon=horizon, p10=values[0], p50=values[1], p90=values[2])


def _last_value(values: object, horizon: int) -> float:
    seq = values[0] if isinstance(values, list | tuple) and values and isinstance(values[0], list | tuple) else values
    if not isinstance(seq, list | tuple) or len(seq) < horizon:
        raise TimesFMForecastError("Forecast output shorter than requested horizon")
    return float(seq[horizon - 1])


def _quantile_value(values: object, quantile_index: int, horizon: int) -> float:
    if not isinstance(values, list | tuple) or not values:
        raise TimesFMForecastError("Missing forecast quantiles")
    first_series = values[0] if isinstance(values[0], list | tuple) else values
    horizon_row = first_series[horizon - 1]
    if not isinstance(horizon_row, list | tuple):
        return float(horizon_row)
    return float(horizon_row[quantile_index])
