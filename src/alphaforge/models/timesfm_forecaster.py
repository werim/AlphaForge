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

        inputs = [list(close_prices)]
        forecast = _call_timesfm_forecast(self._model, inputs, horizon)
        return _parse_timesfm_output(forecast, horizon)


def _call_timesfm_forecast(model: object, inputs: list[list[float]], horizon: int) -> object:
    forecast_fn = getattr(model, "forecast", None)
    if forecast_fn is None:
        raise TimesFMForecastError("Configured TimesFM model does not expose forecast()")

    call_attempts = (
        lambda: forecast_fn(inputs=inputs, horizon=horizon),
        lambda: forecast_fn(inputs=inputs, horizon_len=horizon),
        lambda: forecast_fn(inputs, horizon_len=horizon),
        lambda: forecast_fn(inputs, horizon=horizon),
        lambda: forecast_fn(inputs=inputs, freq=[0]),
        lambda: forecast_fn(inputs, freq=[0]),
    )
    last_type_error: TypeError | None = None
    for attempt in call_attempts:
        try:
            return attempt()
        except TypeError as exc:
            last_type_error = exc
    raise TimesFMForecastError(f"TimesFM forecast() signature is unsupported: {last_type_error}")


def _parse_timesfm_output(raw: object, horizon: int) -> QuantileForecast:
    if isinstance(raw, dict):
        p10 = _last_value(_first_present(raw, ("p10", "0.1")), horizon)
        p50 = _last_value(_first_present(raw, ("p50", "0.5", "median", "mean")), horizon)
        p90 = _last_value(_first_present(raw, ("p90", "0.9")), horizon)
    elif isinstance(raw, tuple) and len(raw) >= 2:
        point, quantiles = raw[0], raw[1]
        p10, p50, p90 = _tuple_quantile_values(point, quantiles, horizon)
    else:
        raise TimesFMForecastError("Unsupported TimesFM forecast output")

    values = (float(p10), float(p50), float(p90))
    if any(v != v or v <= 0 for v in values) or not values[0] <= values[1] <= values[2]:
        raise TimesFMForecastError("Invalid forecast quantiles")
    return QuantileForecast(horizon=horizon, p10=values[0], p50=values[1], p90=values[2])


def _first_present(raw: dict[object, object], keys: tuple[str, ...]) -> object:
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
    raise TimesFMForecastError("Missing forecast quantiles")


def _tuple_quantile_values(point: object, quantiles: object, horizon: int) -> tuple[float, float, float]:
    row = _horizon_quantile_row(quantiles, horizon)
    width = len(row)
    if width >= 10:
        return float(row[1]), float(row[5]), float(row[9])
    if width >= 9:
        return float(row[0]), float(row[4]), float(row[8])
    if width >= 3:
        return float(row[0]), float(row[1]), float(row[2])
    if width == 1:
        median = _last_value(point, horizon)
        return float(row[0]), float(median), float(row[0])
    raise TimesFMForecastError("Missing forecast quantiles")


def _last_value(values: object, horizon: int) -> float:
    seq = _first_series(values)
    if len(seq) < horizon:
        raise TimesFMForecastError("Forecast output shorter than requested horizon")
    return float(seq[horizon - 1])


def _horizon_quantile_row(values: object, horizon: int) -> object:
    series = _first_series(values)
    if len(series) < horizon:
        raise TimesFMForecastError("Forecast quantiles shorter than requested horizon")
    row = series[horizon - 1]
    if not _is_sequence_like(row):
        raise TimesFMForecastError("Forecast quantile row is not sequence-like")
    return row


def _first_series(values: object) -> object:
    if not _is_sequence_like(values) or len(values) == 0:
        raise TimesFMForecastError("Missing forecast values")
    first = values[0]
    if _is_sequence_like(first) and len(first) > 0 and not _is_number(first[0]):
        return first
    if _is_sequence_like(first) and len(first) > 0 and _is_number(first[0]):
        return first
    return values


def _is_sequence_like(value: object) -> bool:
    return not isinstance(value, str | bytes | dict) and hasattr(value, "__len__") and hasattr(value, "__getitem__")


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) or hasattr(value, "item")
