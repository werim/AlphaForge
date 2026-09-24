from __future__ import annotations

import asyncio
import json
import math
import socket
import time
from typing import Any, Iterable
from urllib import error, parse, request

from alphaforge.signal_geometry import build_breakout_geometry_with_diagnostics

GEOMETRY_SOURCE = "BINANCE_CLOSED_1M_KLINES"


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


def _binance_kline_geometry(base_url: str, symbol: str, *, timeout_sec: float) -> dict[str, Any]:
    """Return canonical geometry from the last two closed 1m setup candles."""
    query = parse.urlencode({"symbol": symbol, "interval": "1m", "limit": 21})
    try:
        rows = _fetch_json(f"{base_url.rstrip('/')}/fapi/v1/klines?{query}", timeout_sec=timeout_sec)
    except (TimeoutError, asyncio.TimeoutError):
        return {"geometry_status": "UNAVAILABLE", "geometry_reason": "KLINE_TIMEOUT", "geometry_source": GEOMETRY_SOURCE}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"geometry_status": "UNAVAILABLE", "geometry_reason": "KLINE_FETCH_FAILED", "geometry_source": GEOMETRY_SOURCE}
    if not isinstance(rows, list):
        return {"geometry_status": "INVALID", "geometry_reason": "KLINE_MALFORMED_PAYLOAD", "geometry_source": GEOMETRY_SOURCE}
    if len(rows) < 3:
        return {"geometry_status": "UNAVAILABLE", "geometry_reason": "KLINE_INSUFFICIENT_ROWS", "geometry_source": GEOMETRY_SOURCE}
    candles = []
    execution_candle_open_ts = None
    for row in rows[-3:-1]:
        if not isinstance(row, list) or len(row) < 5:
            return {"geometry_status": "INVALID", "geometry_reason": "KLINE_MALFORMED_PAYLOAD", "geometry_source": GEOMETRY_SOURCE}
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
        return {**identity, "geometry_status": "INVALID", "geometry_reason": reason, "geometry_source": GEOMETRY_SOURCE}
    volatility_evidence = ({"recent_klines": recent_klines,
                            "recent_klines_status": "MEASURED",
                            "recent_klines_source": GEOMETRY_SOURCE}
                           if len(recent_klines) >= 2 else {})
    return {**geometry, **identity, **volatility_evidence, "geometry_status": "COMPLETE", "geometry_reason": None, "geometry_source": GEOMETRY_SOURCE}


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
    keys: list[tuple[str, str] | None] = []
    tasks: dict[tuple[str, str], asyncio.Task[dict[str, Any]]] = {}
    for candidate in candidates:
        source = str(candidate.get("source_exchange") or "").lower()
        symbol = str(candidate.get("symbol") or "")
        timeframe = str(candidate.get("timeframe") or "").lower()
        key = (symbol, timeframe) if source == "binance" and symbol and timeframe == "1m" else None
        keys.append(key)
        if key is not None and key not in tasks:
            tasks[key] = asyncio.create_task(
                asyncio.to_thread(_binance_kline_geometry, base_url, symbol, timeout_sec=timeout)
            )
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
    decision_timeframe = str(getattr(getattr(config, "runtime", object()), "paper_decision_timeframe", "1m"))
    if decision_timeframe != "1m":
        return MarketScanRows([], diagnostics=_market_scan_diagnostics(
            status="VALID_EMPTY", provider="binance", cause="UNSUPPORTED_TIMEFRAME",
        ))  # the canonical geometry provider currently supports closed 1m setup candles only

    endpoint_specs = (
        ("exchangeInfo", f"{base_url.rstrip('/')}/fapi/v1/exchangeInfo", False),
        ("ticker_24hr", f"{base_url.rstrip('/')}/fapi/v1/ticker/24hr", False),
        ("bookTicker", f"{base_url.rstrip('/')}/fapi/v1/ticker/bookTicker", True),
        ("premiumIndex", f"{base_url.rstrip('/')}/fapi/v1/premiumIndex", False),
    )
    payloads: dict[str, Any] = {}
    market_data_latency_ms: float | None = None
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
    trading_symbols = {
        row.get("symbol") for row in exchange_info["symbols"]
        if isinstance(row, dict) and isinstance(row.get("symbol"), str) and row.get("status") == "TRADING"
    }

    book_map: dict[str, tuple[float, float]] = {}
    for item in book_tickers:
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        bid = float(item.get("bidPrice", 0.0) or 0.0)
        ask = float(item.get("askPrice", 0.0) or 0.0)
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
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
    now_ts = time.time()
    candidates: list[dict[str, Any]] = []
    for item in tickers:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "")
        if symbol not in trading_symbols or not symbol.endswith(quote_asset):
            continue

        last_price = float(item.get("lastPrice", 0.0) or 0.0)
        if last_price <= 0.0:
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

        change_pct = abs(float(item.get("priceChangePercent", 0.0) or 0.0)) / 100.0
        volume_quote = float(item.get("quoteVolume", 0.0) or 0.0)
        trend_strength = min(1.0, change_pct / 0.02)

        candidates.append(
            {
                "symbol": symbol,
                "source_exchange": "binance",
                "entry": entry,
                "market_ts": now_ts,
                "timeframe": decision_timeframe,
                "volume_24h_usdt": volume_quote,
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
                "volatility_pct": max(0.0001, change_pct),
                "trend_strength": trend_strength,
                "liquidity_score": 1.0 if volume_quote >= 50_000_000 else 0.7,
                "chop_score": max(0.0, 1.0 - trend_strength),
            }
        )
    candidates.sort(key=lambda row: float(row.get("volume_24h_usdt", 0.0)), reverse=True)
    selected = candidates[:30]
    return MarketScanRows(selected, diagnostics=_market_scan_diagnostics(
        status="AVAILABLE" if selected else "VALID_EMPTY",
        provider="binance",
        cause=None if selected else "NO_CANDIDATES_AFTER_FILTERING",
    ))


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
                "entry": price,
                "side": "LONG",
                "market_ts": now_ts,
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
            }
        )
    selected = rows[:20]
    return MarketScanRows(selected, diagnostics=_market_scan_diagnostics(
        status="AVAILABLE" if selected else "VALID_EMPTY",
        provider="hyperliquid",
        cause=None if selected else "NO_CANDIDATES_AFTER_FILTERING",
    ))


def _fetch_json(url_or_request: str | request.Request, *, timeout_sec: float) -> Any:
    with request.urlopen(url_or_request, timeout=timeout_sec) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))
