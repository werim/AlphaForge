from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib import request


async def scan_exchange_markets(config: Any) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_scan_exchange_markets_sync, config)


def _scan_exchange_markets_sync(config: Any) -> list[dict[str, Any]]:
    timeout = float(getattr(getattr(config, "exchange", object()), "timeout_sec", 2.0) or 2.0)
    rows: list[dict[str, Any]] = []
    rows.extend(_scan_binance(config, timeout_sec=timeout))
    rows.extend(_scan_hyperliquid(config, timeout_sec=timeout))
    return rows


def _scan_binance(config: Any, *, timeout_sec: float) -> list[dict[str, Any]]:
    binance = getattr(getattr(config, "exchange", object()), "binance", object())
    if str(getattr(binance, "default_market_type", "USD_M")).upper() != "USD_M":
        return []
    base_url = str(getattr(binance, "base_url", "https://fapi.binance.com"))
    quote_asset = str(getattr(binance, "default_quote_asset", "USDT")).upper()
    try:
        exchange_info = _fetch_json(f"{base_url.rstrip('/')}/fapi/v1/exchangeInfo", timeout_sec=timeout_sec)
        tickers = _fetch_json(f"{base_url.rstrip('/')}/fapi/v1/ticker/24hr", timeout_sec=timeout_sec)
        book_tickers = _fetch_json(f"{base_url.rstrip('/')}/fapi/v1/ticker/bookTicker", timeout_sec=timeout_sec)
        funding = _fetch_json(f"{base_url.rstrip('/')}/fapi/v1/premiumIndex", timeout_sec=timeout_sec)
    except Exception:  # noqa: BLE001
        return []
    if (not isinstance(exchange_info, dict) or not isinstance(exchange_info.get("symbols"), list)
            or not isinstance(tickers, list) or not isinstance(book_tickers, list)):
        return []
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

    funding_map = {
        str(item.get("symbol")): float(item.get("lastFundingRate", 0.0) or 0.0)
        for item in (funding if isinstance(funding, list) else [])
        if isinstance(item, dict) and item.get("symbol")
    }
    now_ts = time.time()
    market_data_latency_ms = None
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
                "side": "LONG",
                "market_ts": now_ts,
                "timeframe": "1m",
                "volume_24h_usdt": volume_quote,
                "spread_pct": spread_pct,
                "spread_bps": spread_pct * 10_000.0,
                "spread_status": "MEASURED",
                "spread_source": "BINANCE_BOOK_TICKER",
                "funding_rate_pct": funding_map.get(symbol),
                "funding_status": "MEASURED" if symbol in funding_map else "UNAVAILABLE",
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
    return candidates[:30]


def _scan_hyperliquid(config: Any, *, timeout_sec: float) -> list[dict[str, Any]]:
    hyperliquid = getattr(getattr(config, "exchange", object()), "hyperliquid", object())
    if not bool(getattr(hyperliquid, "enabled", True)):
        return []
    api_url = str(getattr(hyperliquid, "api_url", "https://api.hyperliquid.xyz"))
    req = request.Request(
        f"{api_url.rstrip('/')}/info",
        method="POST",
        data=b'{"type":"allMids"}',
        headers={"Content-Type": "application/json"},
    )
    try:
        mids = _fetch_json(req, timeout_sec=timeout_sec)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(mids, dict):
        return []
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
    return rows[:20]


def _fetch_json(url_or_request: str | request.Request, *, timeout_sec: float) -> Any:
    with request.urlopen(url_or_request, timeout=timeout_sec) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))
