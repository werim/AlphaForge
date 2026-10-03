from __future__ import annotations

import asyncio
import json
import math
import socket
import threading
import time
from typing import Any, Iterable
from urllib import error, parse, request

from alphaforge.signal_geometry import build_breakout_geometry_with_diagnostics

SUPPORTED_BINANCE_DECISION_TIMEFRAMES = frozenset({"1m", "15m", "1h", "4h", "1d"})

_BINANCE_SNAPSHOT_CACHE: dict[tuple[object, object, str], dict[str, Any]] = {}
_BINANCE_SNAPSHOT_CACHE_LOCK = threading.Lock()
_BINANCE_GEOMETRY_CACHE: dict[tuple[object, object, str, str, str, int], dict[str, Any]] = {}
_BINANCE_GEOMETRY_CACHE_LOCK = threading.Lock()


def _timeframe_seconds(timeframe: str) -> int | None:
    raw = str(timeframe or "").strip().lower()
    if raw not in SUPPORTED_BINANCE_DECISION_TIMEFRAMES:
        return None
    try:
        value = int(raw[:-1])
    except ValueError:
        return None
    unit = raw[-1]
    multiplier = {"m": 60, "h": 3600, "d": 86400}.get(unit)
    return None if multiplier is None else value * multiplier


def _latest_closed_candle_bucket(timeframe: str, *, now_ts: float | None = None) -> int | None:
    seconds = _timeframe_seconds(timeframe)
    if seconds is None:
        return None
    observed = time.time() if now_ts is None else float(now_ts)
    return int(observed // seconds) * seconds - seconds


def _paper_scanner_cache_enabled(config: Any) -> bool:
    runtime = getattr(config, "runtime", object())
    return str(getattr(runtime, "execution_mode", "")).strip().upper() == "PAPER"


def _binance_snapshot_ttl_sec(config: Any) -> float:
    if not _paper_scanner_cache_enabled(config):
        return 0.0
    try:
        stale = float(getattr(getattr(config, "runtime", object()), "stale_market_data_sec", 15.0))
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(stale) or stale <= 0.0:
        return 0.0
    # Keep reused public evidence comfortably inside the canonical stale-data
    # boundary.  This bounds all-symbol request weight without inventing a new
    # favorable freshness threshold.
    return min(10.0, stale / 2.0)


def _snapshot_cache_key(config: Any, base_url: str) -> tuple[object, object, str]:
    del config  # freshness policy is evaluated on every lookup, not frozen into the key.
    # Keep the actual provider callables in the key. Using id(...) here is unsafe:
    # CPython may recycle an object's id after a monkeypatched/test callable is
    # released, which can make unrelated provider authorities collide.
    return (_fetch_json, request.urlopen, base_url.rstrip("/"))


def _load_cached_binance_snapshot(config: Any, base_url: str) -> dict[str, Any] | None:
    ttl = _binance_snapshot_ttl_sec(config)
    if ttl <= 0.0:
        return None
    key = _snapshot_cache_key(config, base_url)
    now_mono = time.monotonic()
    with _BINANCE_SNAPSHOT_CACHE_LOCK:
        cached = _BINANCE_SNAPSHOT_CACHE.get(key)
        if cached is None:
            return None
        age = now_mono - float(cached["stored_monotonic"])
        if age < 0.0 or age >= ttl:
            _BINANCE_SNAPSHOT_CACHE.pop(key, None)
            return None
        return {
            "payloads": cached["payloads"],
            "market_data_latency_ms": cached["market_data_latency_ms"],
            "observed_at": cached["observed_at"],
            "age_sec": age,
        }


def _store_binance_snapshot(
    config: Any,
    base_url: str,
    *,
    payloads: dict[str, Any],
    market_data_latency_ms: float | None,
    observed_at: float,
) -> None:
    if _binance_snapshot_ttl_sec(config) <= 0.0:
        return
    key = _snapshot_cache_key(config, base_url)
    with _BINANCE_SNAPSHOT_CACHE_LOCK:
        _BINANCE_SNAPSHOT_CACHE[key] = {
            "payloads": payloads,
            "market_data_latency_ms": market_data_latency_ms,
            "observed_at": observed_at,
            "stored_monotonic": time.monotonic(),
        }


def _geometry_source(timeframe: str) -> str:
    return f"BINANCE_CLOSED_{str(timeframe).upper()}_KLINES"


GEOMETRY_SOURCE = _geometry_source("1m")


class MarketScanRows(list[dict[str, Any]]):
    """List-compatible market scan result carrying queryable provider diagnostics."""

    def __init__(
        self,
        rows: Iterable[dict[str, Any]] = (),
        *,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(rows)
        self.diagnostics = dict(diagnostics or {})


def _market_scan_diagnostics(
    *,
    status: str,
    provider: str,
    cause: str | None = None,
    endpoint: str | None = None,
    error_class: str | None = None,
    http_status: int | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "provider": provider,
        "cause": cause,
        "endpoint": endpoint,
        "error_class": error_class,
        "http_status": http_status,
    }


def _classify_public_fetch_error(exc: BaseException) -> tuple[str, int | None]:
    if isinstance(exc, error.HTTPError):
        code = int(exc.code)
        if code == 429:
            return "HTTP_429", code
        if 500 <= code <= 599:
            return "HTTP_5XX", code
        return "HTTP_ERROR", code
    if isinstance(exc, error.URLError):
        reason = exc.reason
        if isinstance(reason, socket.gaierror):
            return "DNS_FAILURE", None
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "TIMEOUT", None
        return "URL_ERROR", None
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, socket.timeout)):
        return "TIMEOUT", None
    if isinstance(exc, json.JSONDecodeError):
        return "JSON_DECODE_ERROR", None
    if isinstance(exc, OSError):
        return "NETWORK_ERROR", None
    return "FETCH_ERROR", None


async def scan_exchange_markets(config: Any) -> MarketScanRows:
    return await asyncio.to_thread(_scan_exchange_markets_sync, config)


def _binance_kline_geometry(
    base_url: str,
    symbol: str,
    *,
    timeframe: str = "1m",
    timeout_sec: float,
    cache_enabled: bool = False,
) -> dict[str, Any]:
    """Return canonical geometry from the last two closed execution candles."""
    timeframe = str(timeframe or "").lower()
    geometry_source = _geometry_source(timeframe)
    if timeframe not in SUPPORTED_BINANCE_DECISION_TIMEFRAMES:
        return {
            "geometry_status": "UNAVAILABLE",
            "geometry_reason": "UNSUPPORTED_TIMEFRAME",
            "geometry_source": geometry_source,
        }
    cache_key: tuple[object, object, str, str, str, int] | None = None
    if cache_enabled:
        boundary = _latest_closed_candle_bucket(timeframe)
        cache_key = (
            _fetch_json,
            request.urlopen,
            base_url.rstrip("/"),
            symbol.upper(),
            timeframe,
            -1 if boundary is None else boundary,
        )
        with _BINANCE_GEOMETRY_CACHE_LOCK:
            cached_geometry = _BINANCE_GEOMETRY_CACHE.get(cache_key)
            if cached_geometry is not None:
                return dict(cached_geometry)

    query = parse.urlencode({"symbol": symbol, "interval": timeframe, "limit": 21})
    try:
        rows = _fetch_json(f"{base_url.rstrip('/')}/fapi/v1/klines?{query}", timeout_sec=timeout_sec)
    except (TimeoutError, asyncio.TimeoutError):
        return {"geometry_status": "UNAVAILABLE", "geometry_reason": "KLINE_TIMEOUT", "geometry_source": geometry_source}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"geometry_status": "UNAVAILABLE", "geometry_reason": "KLINE_FETCH_FAILED", "geometry_source": geometry_source}
    if not isinstance(rows, list):
        return {"geometry_status": "INVALID", "geometry_reason": "KLINE_MALFORMED_PAYLOAD", "geometry_source": geometry_source}
    if len(rows) < 3:
        return {"geometry_status": "UNAVAILABLE", "geometry_reason": "KLINE_INSUFFICIENT_ROWS", "geometry_source": geometry_source}
    candles = []
    execution_candle_open_ts = None
    for row in rows[-3:-1]:
        if not isinstance(row, list) or len(row) < 5:
            return {"geometry_status": "INVALID", "geometry_reason": "KLINE_MALFORMED_PAYLOAD", "geometry_source": geometry_source}
        candles.append({"open": row[1], "high": row[2], "low": row[3], "close": row[4]})
        execution_candle_open_ts = row[0]
    recent_klines: list[dict[str, float]] = []
    for row in rows[:-1][-20:]:
        if not isinstance(row, list) or len(row) < 5:
            continue
        try:
            open_price, high, low, close = map(float, row[1:5])
        except (TypeError, ValueError):
            continue
        if (not all(math.isfinite(value) and value > 0 for value in (open_price, high, low, close))
                or high < max(open_price, close) or low > min(open_price, close)):
            continue
        recent_klines.append({"open": open_price, "high": high, "low": low, "close": close})
    identity = {"execution_candle_open_ts": execution_candle_open_ts}
    geometry, reason = build_breakout_geometry_with_diagnostics(candles[1], candles[0])
    if reason:
        return {**identity, "geometry_status": "INVALID", "geometry_reason": reason, "geometry_source": geometry_source}
    volatility_evidence = ({"recent_klines": recent_klines,
                            "recent_klines_status": "MEASURED",
                            "recent_klines_source": geometry_source}
                           if len(recent_klines) >= 2 else {})
    result = {**geometry, **identity, **volatility_evidence, "geometry_status": "COMPLETE", "geometry_reason": None, "geometry_source": geometry_source}
    if cache_key is not None:
        with _BINANCE_GEOMETRY_CACHE_LOCK:
            # Keep only the current closed-candle identity for this market/provider.
            prefix = cache_key[:-1]
            for old_key in tuple(_BINANCE_GEOMETRY_CACHE):
                if old_key[:-1] == prefix and old_key != cache_key:
                    _BINANCE_GEOMETRY_CACHE.pop(old_key, None)
            _BINANCE_GEOMETRY_CACHE[cache_key] = dict(result)
    return result


def _fetch_json_with_latency(url: str, *, timeout_sec: float) -> tuple[Any, float | None]:
    """Fetch public price data and return its monotonic HTTP RTT when measurable."""
    try:
        started = time.perf_counter()
    except Exception:  # an unavailable clock must not fabricate latency
        return _fetch_json(url, timeout_sec=timeout_sec), None
    payload = _fetch_json(url, timeout_sec=timeout_sec)
    try:
        elapsed = (time.perf_counter() - started) * 1000.0
    except Exception:
        return payload, None
    return payload, elapsed if elapsed >= 0 else None


async def enrich_selected_market_geometry(
    candidates: list[dict[str, Any]], config: Any,
) -> list[dict[str, Any]]:
    """Enrich only canonically selected Binance candidates, once per symbol.

    Calls are bounded by the already-selected candidate list and complete within
    this coroutine. Provider/unavailable-data failures leave geometry absent.
    """
    binance = getattr(getattr(config, "exchange", object()), "binance", object())
    base_url = str(getattr(binance, "market_data_base_url", getattr(binance, "base_url", "https://fapi.binance.com")))
    timeout = float(getattr(getattr(config, "exchange", object()), "timeout_sec", 2.0) or 2.0)
    geometry_cache_enabled = _paper_scanner_cache_enabled(config)
    keys: list[tuple[str, str] | None] = []
    tasks: dict[tuple[str, str], asyncio.Task[dict[str, Any]]] = {}
    for candidate in candidates:
        source = str(candidate.get("source_exchange") or "").lower()
        symbol = str(candidate.get("symbol") or "")
        timeframe = str(candidate.get("timeframe") or "").lower()
        key = (
            (symbol, timeframe)
            if source == "binance"
            and symbol
            and timeframe in SUPPORTED_BINANCE_DECISION_TIMEFRAMES
            else None
        )
        keys.append(key)
        if key is not None and key not in tasks:
            if timeframe == "1m":
                geometry_call = asyncio.to_thread(
                    _binance_kline_geometry,
                    base_url,
                    symbol,
                    timeout_sec=timeout,
                    cache_enabled=geometry_cache_enabled,
                )
            else:
                geometry_call = asyncio.to_thread(
                    _binance_kline_geometry,
                    base_url,
                    symbol,
                    timeframe=timeframe,
                    timeout_sec=timeout,
                    cache_enabled=geometry_cache_enabled,
                )
            tasks[key] = asyncio.create_task(geometry_call)
    results = dict(zip(tasks, await asyncio.gather(*tasks.values()))) if tasks else {}
    enriched: list[dict[str, Any]] = []
    for candidate, key in zip(candidates, keys):
        geometry = results.get(key, {})
        enriched.append({**candidate, **geometry})
    return enriched


def _scan_exchange_markets_sync(config: Any) -> MarketScanRows:
    timeout = float(getattr(getattr(config, "exchange", object()), "timeout_sec", 2.0) or 2.0)
    binance_rows = _scan_binance(config, timeout_sec=timeout)
    hyperliquid_rows = _scan_hyperliquid(config, timeout_sec=timeout)
    rows = [*binance_rows, *hyperliquid_rows]
    provider_diagnostics = [binance_rows.diagnostics, hyperliquid_rows.diagnostics]
    if rows:
        status = "AVAILABLE"
        active_providers = {
            str(row.get("source_exchange") or "unknown") for row in rows
        }
        provider = (
            next(iter(active_providers))
            if len(active_providers) == 1
            else "multi"
        )
        cause = endpoint = error_class = http_status = None
    else:
        unavailable = next(
            (item for item in provider_diagnostics if item.get("status") == "UNAVAILABLE"),
            None,
        )
        if unavailable is not None:
            status = "UNAVAILABLE"
            provider = unavailable.get("provider")
            cause = unavailable.get("cause")
            endpoint = unavailable.get("endpoint")
            error_class = unavailable.get("error_class")
            http_status = unavailable.get("http_status")
        else:
            status = "VALID_EMPTY"
            provider = None
            cause = "NO_CANDIDATES"
            endpoint = error_class = http_status = None
    diagnostics = {
        "status": status,
        "provider": provider,
        "cause": cause,
        "endpoint": endpoint,
        "error_class": error_class,
        "http_status": http_status,
        "providers": provider_diagnostics,
    }
    return MarketScanRows(rows, diagnostics=diagnostics)


def _scan_binance(config: Any, *, timeout_sec: float) -> MarketScanRows:
    binance = getattr(getattr(config, "exchange", object()), "binance", object())
    if str(getattr(binance, "default_market_type", "USD_M")).upper() != "USD_M":
        return MarketScanRows([], diagnostics=_market_scan_diagnostics(
            status="VALID_EMPTY", provider="binance", cause="UNSUPPORTED_MARKET_TYPE",
        ))
    base_url = str(getattr(binance, "market_data_base_url", getattr(binance, "base_url", "https://fapi.binance.com")))
    quote_asset = str(getattr(binance, "default_quote_asset", "USDT")).upper()
    decision_timeframe = str(
        getattr(getattr(config, "runtime", object()), "paper_decision_timeframe", "1m")
    ).lower()
    if decision_timeframe not in SUPPORTED_BINANCE_DECISION_TIMEFRAMES:
        return MarketScanRows([], diagnostics=_market_scan_diagnostics(
            status="VALID_EMPTY", provider="binance", cause="UNSUPPORTED_TIMEFRAME",
        ))

    endpoint_specs = (
        ("exchangeInfo", f"{base_url.rstrip('/')}/fapi/v1/exchangeInfo", False),
        ("ticker_24hr", f"{base_url.rstrip('/')}/fapi/v1/ticker/24hr", False),
        ("bookTicker", f"{base_url.rstrip('/')}/fapi/v1/ticker/bookTicker", True),
        ("premiumIndex", f"{base_url.rstrip('/')}/fapi/v1/premiumIndex", False),
    )
    cached_snapshot = _load_cached_binance_snapshot(config, base_url)
    payloads: dict[str, Any] = {}
    market_data_latency_ms: float | None = None
    snapshot_observed_at: float | None = None
    snapshot_cache_status = "MISS"
    snapshot_cache_age_sec = 0.0

    if cached_snapshot is not None:
        payloads = dict(cached_snapshot["payloads"])
        market_data_latency_ms = cached_snapshot["market_data_latency_ms"]
        snapshot_observed_at = float(cached_snapshot["observed_at"])
        snapshot_cache_status = "HIT"
        snapshot_cache_age_sec = float(cached_snapshot["age_sec"])
    else:
        for endpoint, url, measure_latency in endpoint_specs:
            try:
                if measure_latency:
                    payload, market_data_latency_ms = _fetch_json_with_latency(
                        url, timeout_sec=timeout_sec
                    )
                else:
                    payload = _fetch_json(url, timeout_sec=timeout_sec)
            except Exception as exc:  # noqa: BLE001
                cause, http_status = _classify_public_fetch_error(exc)
                return MarketScanRows([], diagnostics=_market_scan_diagnostics(
                    status="UNAVAILABLE",
                    provider="binance",
                    cause=cause,
                    endpoint=endpoint,
                    error_class=exc.__class__.__name__,
                    http_status=http_status,
                ))
            payloads[endpoint] = payload

    exchange_info = payloads["exchangeInfo"]
    tickers = payloads["ticker_24hr"]
    book_tickers = payloads["bookTicker"]
    funding = payloads["premiumIndex"]
    malformed_endpoint = None
    if not isinstance(exchange_info, dict) or not isinstance(exchange_info.get("symbols"), list):
        malformed_endpoint = "exchangeInfo"
    elif not isinstance(tickers, list):
        malformed_endpoint = "ticker_24hr"
    elif not isinstance(book_tickers, list):
        malformed_endpoint = "bookTicker"
    elif not isinstance(funding, list):
        malformed_endpoint = "premiumIndex"
    if malformed_endpoint is not None:
        return MarketScanRows([], diagnostics=_market_scan_diagnostics(
            status="UNAVAILABLE",
            provider="binance",
            cause="MALFORMED_PAYLOAD",
            endpoint=malformed_endpoint,
            error_class="PayloadShapeError",
        ))
    if cached_snapshot is None:
        snapshot_observed_at = time.time()
        _store_binance_snapshot(
            config,
            base_url,
            payloads=payloads,
            market_data_latency_ms=market_data_latency_ms,
            observed_at=snapshot_observed_at,
        )
    assert snapshot_observed_at is not None
    trading_instruments = {
        str(row["symbol"]): row for row in exchange_info["symbols"]
        if isinstance(row, dict)
        and isinstance(row.get("symbol"), str)
        and row.get("status") == "TRADING"
        and row.get("contractType") == "PERPETUAL"
        and row.get("quoteAsset") == quote_asset
    }

    book_map: dict[str, tuple[float, float]] = {}
    for item in book_tickers:
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        try:
            bid = float(item.get("bidPrice"))
            ask = float(item.get("askPrice"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0.0 or ask <= 0.0 or ask < bid:
            continue
        book_map[str(item.get("symbol"))] = (bid, ask)

    funding_map: dict[str, float] = {}
    for item in (funding if isinstance(funding, list) else []):
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        raw_rate = item.get("lastFundingRate")
        if raw_rate in (None, ""):
            continue
        try:
            rate = float(raw_rate)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(rate):
            continue
        funding_map[str(item.get("symbol"))] = rate
    now_ts = snapshot_observed_at
    candidates: list[dict[str, Any]] = []
    for item in tickers:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "")
        instrument = trading_instruments.get(symbol)
        if instrument is None or not symbol.endswith(quote_asset):
            continue

        try:
            last_price = float(item.get("lastPrice"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(last_price) or last_price <= 0.0:
            continue

        book = book_map.get(symbol)
        if book is None:
            # fail-closed: require valid public bid/ask for spread-aware runtime candidate
            continue
        bid, ask = book
        mid = (bid + ask) / 2.0
        entry = min(last_price, mid)
        if entry <= 0.0:
            continue
        spread_pct = (ask - bid) / max(entry, 1e-12)

        try:
            raw_change = float(item.get("priceChangePercent"))
            change_pct = abs(raw_change) / 100.0 if math.isfinite(raw_change) else None
        except (TypeError, ValueError):
            change_pct = None
        try:
            raw_volume = float(item.get("quoteVolume"))
            volume_quote = raw_volume if math.isfinite(raw_volume) and raw_volume >= 0 else None
        except (TypeError, ValueError):
            volume_quote = None
        trend_strength = None if change_pct is None else min(1.0, change_pct / 0.02)

        candidates.append(
            {
                "symbol": symbol,
                "source_exchange": "binance",
                "instrument_status": instrument["status"],
                "contract_type": instrument["contractType"],
                "quote_asset": instrument["quoteAsset"],
                "entry": entry,
                "market_ts": now_ts,
                "market_observed_at": now_ts,
                "market_data_source": "BINANCE_PUBLIC_SNAPSHOT",
                "market_data_snapshot_cache_status": snapshot_cache_status,
                "market_data_snapshot_age_sec": snapshot_cache_age_sec,
                "timeframe": decision_timeframe,
                "volume_24h_usdt": volume_quote,
                "volume_24h_source": "BINANCE_TICKER_24HR" if volume_quote is not None else "UNAVAILABLE",
                "spread_pct": spread_pct,
                "spread_bps": spread_pct * 10_000.0,
                "spread_status": "MEASURED",
                "spread_source": "BINANCE_BOOK_TICKER",
                "spread_pct_zero_verified": spread_pct == 0.0,
                "funding_rate_pct": funding_map.get(symbol),
                "funding_status": "MEASURED" if symbol in funding_map else "UNAVAILABLE",
                "funding_rate_pct_zero_verified": (
                    symbol in funding_map and funding_map[symbol] == 0.0
                ),
                "funding_source": "BINANCE_PREMIUM_INDEX" if symbol in funding_map else "UNAVAILABLE",
                "market_data_latency_ms": market_data_latency_ms,
                "market_data_latency_status": "UNAVAILABLE" if market_data_latency_ms is None else "MEASURED",
                "market_data_latency_source": "UNAVAILABLE" if market_data_latency_ms is None else "BINANCE_PUBLIC_HTTP_RTT",
                "volatility_pct": None if change_pct is None else max(0.0001, change_pct),
                "universe_volatility_source": "BINANCE_TICKER_24HR_PRICE_CHANGE" if change_pct is not None else "UNAVAILABLE",
                "trend_strength": trend_strength,
                "liquidity_score": None if volume_quote is None else (1.0 if volume_quote >= 50_000_000 else 0.7),
                "liquidity_status": "ESTIMATED" if volume_quote is not None else "UNAVAILABLE",
                "liquidity_source": "BINANCE_TICKER_24HR_QUOTE_VOLUME" if volume_quote is not None else "UNAVAILABLE",
                "chop_score": None if trend_strength is None else max(0.0, 1.0 - trend_strength),
                "market_cap_usd": None,
                "market_cap_status": "UNAVAILABLE",
                "market_cap_source": "UNAVAILABLE",
                "market_cap_observed_at": None,
            }
        )
    candidates.sort(key=lambda row: (-(row.get("volume_24h_usdt") or -1.0), str(row.get("symbol") or "")))
    selected = candidates
    diagnostics = _market_scan_diagnostics(
        status="AVAILABLE" if selected else "VALID_EMPTY",
        provider="binance",
        cause=None if selected else "NO_CANDIDATES_AFTER_FILTERING",
    )
    diagnostics.update({
        "snapshot_cache_status": snapshot_cache_status,
        "snapshot_cache_age_sec": snapshot_cache_age_sec,
        "snapshot_observed_at": snapshot_observed_at,
    })
    return MarketScanRows(selected, diagnostics=diagnostics)


def _scan_hyperliquid(config: Any, *, timeout_sec: float) -> MarketScanRows:
    hyperliquid = getattr(getattr(config, "exchange", object()), "hyperliquid", object())
    if not bool(getattr(hyperliquid, "enabled", True)):
        return MarketScanRows([], diagnostics=_market_scan_diagnostics(
            status="DISABLED", provider="hyperliquid", cause="PROVIDER_DISABLED",
        ))
    api_url = str(getattr(hyperliquid, "api_url", "https://api.hyperliquid.xyz"))
    req = request.Request(
        f"{api_url.rstrip('/')}/info",
        method="POST",
        data=b'{"type":"allMids"}',
        headers={"Content-Type": "application/json"},
    )
    try:
        mids = _fetch_json(req, timeout_sec=timeout_sec)
    except Exception as exc:  # noqa: BLE001
        cause, http_status = _classify_public_fetch_error(exc)
        return MarketScanRows([], diagnostics=_market_scan_diagnostics(
            status="UNAVAILABLE",
            provider="hyperliquid",
            cause=cause,
            endpoint="allMids",
            error_class=exc.__class__.__name__,
            http_status=http_status,
        ))
    if not isinstance(mids, dict):
        return MarketScanRows([], diagnostics=_market_scan_diagnostics(
            status="UNAVAILABLE",
            provider="hyperliquid",
            cause="MALFORMED_PAYLOAD",
            endpoint="allMids",
            error_class="PayloadShapeError",
        ))
    now_ts = time.time()
    market_data_latency_ms = None
    rows: list[dict[str, Any]] = []
    for symbol, mid in mids.items():
        normalized = f"{symbol}USDT" if not str(symbol).endswith("USDT") else str(symbol)
        price = float(mid or 0.0)
        if price <= 0.0:
            continue
        rows.append(
            {
                "symbol": normalized,
                "source_exchange": "hyperliquid",
                "instrument_status": "ACTIVE",
                "contract_type": "SWAP",
                "quote_asset": "USDT",
                "entry": price,
                "side": "LONG",
                "market_ts": now_ts,
                "market_observed_at": now_ts,
                "market_data_source": "HYPERLIQUID_ALL_MIDS",
                "timeframe": "1m",
                "volume_24h_usdt": 0.0,
                "spread_pct": None,
                "spread_status": "UNAVAILABLE",
                "spread_source": "MID_ONLY_NO_BOOK",
                "funding_rate_pct": None,
                "funding_status": "UNAVAILABLE",
                "funding_source": "UNAVAILABLE",
                "market_data_latency_ms": None,
                "market_data_latency_status": "UNAVAILABLE",
                "market_data_latency_source": "UNAVAILABLE",
                "volatility_pct": None,
                "trend_strength": 0.0,
                "liquidity_score": 0.5,
                "chop_score": 1.0,
                "market_cap_usd": None,
                "market_cap_status": "UNAVAILABLE",
                "market_cap_source": "UNAVAILABLE",
                "market_cap_observed_at": None,
            }
        )
    selected = sorted(rows, key=lambda row: str(row.get("symbol") or ""))
    return MarketScanRows(selected, diagnostics=_market_scan_diagnostics(
        status="AVAILABLE" if selected else "VALID_EMPTY",
        provider="hyperliquid",
        cause=None if selected else "NO_CANDIDATES_AFTER_FILTERING",
    ))


def _fetch_json(url_or_request: str | request.Request, *, timeout_sec: float) -> Any:
    with request.urlopen(url_or_request, timeout=timeout_sec) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))
