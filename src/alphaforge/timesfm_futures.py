from __future__ import annotations

import csv
import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

from alphaforge.historical_market_data import HistoricalCandle, fetch_binance_klines_paginated
from alphaforge.models.timesfm_forecaster import ForecastProvider, QuantileForecast, TimesFMForecastError
from alphaforge.persistence import save_timesfm_forecast_evidence

SUPPORTED_SYMBOL = "BTCUSDT"
SUPPORTED_INTERVALS = {"15m", "1h"}
SUPPORTED_HORIZONS = (8, 16, 24)
PAPER_BACKTEST_MODES = {"PAPER", "BACKTEST"}


class TimesFMFuturesError(RuntimeError):
    pass


@dataclass(frozen=True)
class TimesFMDecision:
    forecast_id: str
    timestamp: int
    symbol: str
    timeframe: str
    current_price: float
    forecast_p10: float | None
    forecast_p50: float | None
    forecast_p90: float | None
    side: str
    entry: float | None
    stop: float | None
    take_profit: float | None
    expected_rr: float | None
    rejection_reason: str | None
    mode: str = "BACKTEST"
    horizon: int | None = None
    model_provider: str | None = None
    model_name: str | None = None
    model_version: str | None = None
    no_lookahead_input_end_ts: int | None = None


def load_btcusdt_futures_ohlcv(interval: str, start_ms: int, end_ms: int, *, fetcher=None) -> list[HistoricalCandle]:
    if interval not in SUPPORTED_INTERVALS:
        raise TimesFMFuturesError(f"Unsupported BTCUSDT TimesFM interval={interval}; expected one of {sorted(SUPPORTED_INTERVALS)}")
    return fetch_binance_klines_paginated(SUPPORTED_SYMBOL, interval, start_ms, end_ms, fetcher=fetcher)


def decide_from_forecast(
    *,
    timestamp: int,
    symbol: str,
    timeframe: str,
    current_price: float,
    forecast: QuantileForecast | None,
    min_edge_pct: float = 0.002,
    min_expected_rr: float = 1.5,
    mode: str = "BACKTEST",
    model_provider: str | None = None,
    model_name: str | None = None,
    model_version: str | None = None,
    no_lookahead_input_end_ts: int | None = None,
) -> TimesFMDecision:
    if symbol != SUPPORTED_SYMBOL or timeframe not in SUPPORTED_INTERVALS:
        return _reject(timestamp, symbol, timeframe, current_price, forecast, "UNSUPPORTED_MARKET", mode=mode, model_provider=model_provider, model_name=model_name, model_version=model_version, no_lookahead_input_end_ts=no_lookahead_input_end_ts)
    if current_price <= 0 or current_price != current_price:
        return _reject(timestamp, symbol, timeframe, current_price, forecast, "INVALID_CURRENT_PRICE", mode=mode, model_provider=model_provider, model_name=model_name, model_version=model_version, no_lookahead_input_end_ts=no_lookahead_input_end_ts)
    if forecast is None:
        return _reject(timestamp, symbol, timeframe, current_price, forecast, "INVALID_FORECAST", mode=mode, model_provider=model_provider, model_name=model_name, model_version=model_version, no_lookahead_input_end_ts=no_lookahead_input_end_ts)
    if forecast.horizon not in SUPPORTED_HORIZONS:
        return _reject(timestamp, symbol, timeframe, current_price, forecast, "UNSUPPORTED_HORIZON", mode=mode, model_provider=model_provider, model_name=model_name, model_version=model_version, no_lookahead_input_end_ts=no_lookahead_input_end_ts)
    if any(v <= 0 or v != v for v in (forecast.p10, forecast.p50, forecast.p90)) or not forecast.p10 <= forecast.p50 <= forecast.p90:
        return _reject(timestamp, symbol, timeframe, current_price, forecast, "INVALID_FORECAST", mode=mode, model_provider=model_provider, model_name=model_name, model_version=model_version, no_lookahead_input_end_ts=no_lookahead_input_end_ts)

    upside = forecast.p50 - current_price
    downside = current_price - forecast.p50
    long_risk = current_price - forecast.p10
    short_risk = forecast.p90 - current_price

    if upside / current_price >= min_edge_pct and long_risk > 0:
        rr = upside / long_risk
        if rr >= min_expected_rr:
            return _decision(timestamp, symbol, timeframe, current_price, forecast, "LONG", current_price, forecast.p10, forecast.p90, rr, None, mode, model_provider, model_name, model_version, no_lookahead_input_end_ts)
        return _reject(timestamp, symbol, timeframe, current_price, forecast, "LOW_EFFECTIVE_RR", mode=mode, model_provider=model_provider, model_name=model_name, model_version=model_version, no_lookahead_input_end_ts=no_lookahead_input_end_ts)

    if downside / current_price >= min_edge_pct and short_risk > 0:
        rr = downside / short_risk
        if rr >= min_expected_rr:
            return _decision(timestamp, symbol, timeframe, current_price, forecast, "SHORT", current_price, forecast.p90, forecast.p10, rr, None, mode, model_provider, model_name, model_version, no_lookahead_input_end_ts)
        return _reject(timestamp, symbol, timeframe, current_price, forecast, "LOW_EFFECTIVE_RR", mode=mode, model_provider=model_provider, model_name=model_name, model_version=model_version, no_lookahead_input_end_ts=no_lookahead_input_end_ts)

    return _reject(timestamp, symbol, timeframe, current_price, forecast, "LOW_CONFIDENCE", mode=mode, model_provider=model_provider, model_name=model_name, model_version=model_version, no_lookahead_input_end_ts=no_lookahead_input_end_ts)


def _stable_forecast_id(*, timestamp: int, symbol: str, timeframe: str, horizon: int | None, mode: str, no_lookahead_input_end_ts: int | None) -> str:
    material = f"timesfm|{mode}|{symbol}|{timeframe}|{horizon}|{timestamp}|{no_lookahead_input_end_ts}"
    return "timesfm-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _decision(
    timestamp: int, symbol: str, timeframe: str, current_price: float, forecast: QuantileForecast | None,
    side: str, entry: float | None, stop: float | None, take_profit: float | None, expected_rr: float | None,
    rejection_reason: str | None, mode: str, model_provider: str | None, model_name: str | None, model_version: str | None,
    no_lookahead_input_end_ts: int | None,
) -> TimesFMDecision:
    horizon = None if forecast is None else forecast.horizon
    evidence_end_ts = timestamp if no_lookahead_input_end_ts is None else no_lookahead_input_end_ts
    return TimesFMDecision(
        _stable_forecast_id(timestamp=timestamp, symbol=symbol, timeframe=timeframe, horizon=horizon, mode=mode, no_lookahead_input_end_ts=evidence_end_ts),
        timestamp, symbol, timeframe, current_price,
        None if forecast is None else forecast.p10, None if forecast is None else forecast.p50, None if forecast is None else forecast.p90,
        side, entry, stop, take_profit, expected_rr, rejection_reason, mode, horizon, model_provider, model_name, model_version, evidence_end_ts,
    )


def _reject(
    timestamp: int, symbol: str, timeframe: str, current_price: float, forecast: QuantileForecast | None, reason: str, *,
    mode: str = "BACKTEST", model_provider: str | None = None, model_name: str | None = None, model_version: str | None = None,
    no_lookahead_input_end_ts: int | None = None,
) -> TimesFMDecision:
    return _decision(timestamp, symbol, timeframe, current_price, forecast, "NO_TRADE", None, None, None, None, reason, mode, model_provider, model_name, model_version, no_lookahead_input_end_ts)


def replay_timesfm_backtest(
    candles: Sequence[HistoricalCandle],
    *,
    forecaster: ForecastProvider,
    symbol: str = SUPPORTED_SYMBOL,
    timeframe: str,
    horizon: int,
    min_history: int = 64,
    mode: str = "BACKTEST",
    persistence_session=None,
) -> list[TimesFMDecision]:
    if mode not in PAPER_BACKTEST_MODES:
        raise TimesFMFuturesError("TimesFM futures forecasting is PAPER/BACKTEST only and must never be used for LIVE orders")
    if horizon not in SUPPORTED_HORIZONS:
        raise TimesFMFuturesError(f"Unsupported horizon={horizon}; expected one of {SUPPORTED_HORIZONS}")
    if min_history < 1:
        raise TimesFMFuturesError("min_history must be positive")

    decisions: list[TimesFMDecision] = []
    ordered = sorted(candles, key=lambda c: c.timestamp)
    for idx in range(min_history - 1, len(ordered)):
        visible = ordered[: idx + 1]
        current = visible[-1]
        try:
            forecast = forecaster.forecast_quantiles([c.close for c in visible], horizon)
        except TimesFMForecastError:
            forecast = None
        decision = decide_from_forecast(
            timestamp=current.timestamp, symbol=symbol, timeframe=timeframe, current_price=current.close, forecast=forecast,
            mode=mode, model_provider=getattr(forecaster, "provider", forecaster.__class__.__name__),
            model_name=getattr(forecaster, "model_name", None), model_version=getattr(forecaster, "model_version", None),
            no_lookahead_input_end_ts=current.timestamp,
        )
        decisions.append(decision)
        if persistence_session is not None:
            save_timesfm_forecast_evidence(persistence_session, **asdict(decision))
    return decisions


def write_decision_log(path: Path, decisions: Iterable[TimesFMDecision]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [asdict(d) for d in decisions]
    fieldnames = ["forecast_id", "timestamp", "symbol", "timeframe", "horizon", "current_price", "forecast_p10", "forecast_p50", "forecast_p90", "side", "entry", "stop", "take_profit", "expected_rr", "rejection_reason", "mode", "model_provider", "model_name", "model_version", "no_lookahead_input_end_ts"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
