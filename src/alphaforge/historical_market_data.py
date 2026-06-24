from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import urlopen


class HistoricalDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class HistoricalCandle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    funding_rate_pct: float | None = None


def _interval_ms(interval: str) -> int:
    m = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}
    if interval not in m:
        raise HistoricalDataError(f"Unsupported interval={interval}")
    return m[interval]


def _fetch_json(url: str) -> Any:
    with urlopen(url) as resp:  # nosec - market data endpoint
        return json.loads(resp.read().decode("utf-8"))


def _ceil_to_step(value: int, step: int) -> int:
    return ((value + step - 1) // step) * step


def _floor_to_step(value: int, step: int) -> int:
    return (value // step) * step


def expected_candle_count(start_ms: int, end_ms: int, interval: str) -> int:
    step = _interval_ms(interval)
    first_expected = _ceil_to_step(start_ms, step)
    last_expected = _floor_to_step(end_ms, step)
    if last_expected < first_expected:
        return 0
    return ((last_expected - first_expected) // step) + 1


def _format_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _coverage_error(reason: str, candles: list[HistoricalCandle], start_ms: int, end_ms: int, step: int, symbol: str, interval: str) -> HistoricalDataError:
    expected = expected_candle_count(start_ms, end_ms, interval)
    actual = len(candles)
    actual_first = candles[0].timestamp if candles else None
    actual_last = candles[-1].timestamp if candles else None
    details = (
        f"{reason}: symbol={symbol} timeframe={interval} "
        f"requested_start={_format_ms(start_ms)} requested_end={_format_ms(end_ms)} "
        f"expected_candles={expected} actual_candles={actual} "
        f"actual_first={_format_ms(actual_first) if actual_first is not None else None} "
        f"actual_last={_format_ms(actual_last) if actual_last is not None else None}"
    )
    return HistoricalDataError(details)


def fetch_binance_klines_paginated(symbol: str, interval: str, start_ms: int, end_ms: int, fetcher: Callable[[str], Any] | None = None) -> list[HistoricalCandle]:
    fetch = fetcher or _fetch_json
    step = _interval_ms(interval)
    cursor = start_ms
    out: list[HistoricalCandle] = []
    seen: set[int] = set()
    max_pages = 10_000
    for _ in range(max_pages):
        if cursor > end_ms:
            break
        params = urlencode({"symbol": symbol, "interval": interval, "startTime": cursor, "endTime": end_ms, "limit": 1500})
        rows = fetch(f"https://fapi.binance.com/fapi/v1/klines?{params}")
        if not rows:
            break
        page_new = 0
        last_ts = None
        for r in rows:
            ts = int(r[0])
            last_ts = ts
            if ts in seen:
                continue
            seen.add(ts)
            out.append(HistoricalCandle(ts, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
            page_new += 1
        if last_ts is None:
            break
        nxt = last_ts + step
        if nxt <= cursor or page_new == 0:
            raise HistoricalDataError(f"Pagination stalled for {symbol} interval={interval} at {cursor}")
        cursor = nxt
    else:
        raise HistoricalDataError("Exceeded pagination limit")

    out.sort(key=lambda c: c.timestamp)
    _validate_coverage(out, start_ms, end_ms, step, symbol, interval)
    return out


def _validate_coverage(candles: list[HistoricalCandle], start_ms: int, end_ms: int, step: int, symbol: str, interval: str) -> None:
    expected = expected_candle_count(start_ms, end_ms, interval)
    first_expected = _ceil_to_step(start_ms, step)
    last_expected = _floor_to_step(end_ms, step)
    if expected <= 0:
        raise _coverage_error("Requested range is shorter than one complete candle boundary", candles, start_ms, end_ms, step, symbol, interval)
    if not candles:
        raise _coverage_error("No candles returned by Binance", candles, start_ms, end_ms, step, symbol, interval)
    if candles[0].timestamp > first_expected:
        raise _coverage_error("Historical coverage starts after requested start boundary", candles, start_ms, end_ms, step, symbol, interval)
    if candles[-1].timestamp < last_expected:
        raise _coverage_error("Historical coverage ends before requested end boundary", candles, start_ms, end_ms, step, symbol, interval)
    for i in range(1, len(candles)):
        gap = candles[i].timestamp - candles[i - 1].timestamp
        if gap != step:
            raise _coverage_error(f"Historical gap detected at {candles[i-1].timestamp}->{candles[i].timestamp}", candles, start_ms, end_ms, step, symbol, interval)
    if len(candles) < expected:
        raise _coverage_error("Insufficient candles returned by Binance", candles, start_ms, end_ms, step, symbol, interval)


def fetch_historical_funding_rates(symbol: str, start_ms: int, end_ms: int, fetcher: Callable[[str], Any] | None = None) -> list[tuple[int, float]]:
    fetch = fetcher or _fetch_json
    params = urlencode({"symbol": symbol, "startTime": start_ms, "endTime": end_ms, "limit": 1000})
    rows = fetch(f"https://fapi.binance.com/fapi/v1/fundingRate?{params}")
    out = sorted([(int(r["fundingTime"]), float(r["fundingRate"])) for r in rows], key=lambda x: x[0])
    return out


def join_funding_to_candles(candles: list[HistoricalCandle], funding_rows: list[tuple[int, float]]) -> list[HistoricalCandle]:
    out: list[HistoricalCandle] = []
    fi = 0
    last_rate: float | None = None
    for c in candles:
        while fi < len(funding_rows) and funding_rows[fi][0] <= c.timestamp:
            last_rate = funding_rows[fi][1]
            fi += 1
        out.append(HistoricalCandle(**{**asdict(c), "funding_rate_pct": last_rate}))
    return out


def write_cache(cache_path: Path, candles: list[HistoricalCandle], metadata: dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"metadata": metadata, "candles": [asdict(c) for c in candles]}
    cache_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def load_cache(cache_path: Path) -> tuple[dict[str, Any], list[HistoricalCandle]]:
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    md = payload.get("metadata") or {}
    candles = [HistoricalCandle(**row) for row in payload.get("candles") or []]
    return md, candles


def cache_covers(metadata: dict[str, Any], start_ms: int, end_ms: int) -> bool:
    return int(metadata.get("actual_first_ts", 10**30)) <= start_ms and int(metadata.get("actual_last_ts", -1)) >= end_ms


def build_cache_metadata(symbol: str, interval: str, requested_start_ms: int, requested_end_ms: int, candles: list[HistoricalCandle]) -> dict[str, Any]:
    return {
        "exchange": "BINANCE",
        "market_type": "USD_M_FUTURES",
        "symbol": symbol,
        "interval": interval,
        "requested_start_ms": requested_start_ms,
        "requested_end_ms": requested_end_ms,
        "actual_first_ts": candles[0].timestamp,
        "actual_last_ts": candles[-1].timestamp,
        "row_count": len(candles),
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
    }


def load_or_fetch_candles(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    output_dir: str | Path,
    force_refresh: bool = False,
) -> list[HistoricalCandle]:
    """Load cached Binance candles or fetch the full requested range.

    Cache coverage is treated as an optimization only. A stale cache must not
    fail a backtest before Binance has been asked for the requested range.
    """
    cache_path = Path(output_dir) / "candles" / f"{symbol}_{interval}.json"
    if not force_refresh and cache_path.exists():
        metadata, cached = load_cache(cache_path)
        if cache_covers(metadata, start_ms, end_ms):
            step = _interval_ms(interval)
            selected = [c for c in cached if start_ms <= c.timestamp <= end_ms]
            _validate_coverage(selected, start_ms, end_ms, step, symbol, interval)
            return selected

    rows = fetch_binance_klines_paginated(symbol=symbol, interval=interval, start_ms=start_ms, end_ms=end_ms)
    funding = fetch_historical_funding_rates(symbol=symbol, start_ms=start_ms, end_ms=end_ms)
    rows = join_funding_to_candles(rows, funding)
    metadata = build_cache_metadata(symbol=symbol, interval=interval, requested_start_ms=start_ms, requested_end_ms=end_ms, candles=rows)
    write_cache(cache_path, rows, metadata)
    return rows
