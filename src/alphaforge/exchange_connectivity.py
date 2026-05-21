from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import os
import time
from typing import Any
from urllib import error, request


@dataclass(frozen=True)
class ExchangeHealth:
    exchange: str
    connected: bool
    public_market_data_ok: bool
    private_api_ok: bool | None
    orderbook_ok: bool
    funding_ok: bool | None
    latency_ms: float | None
    error: str | None
    checked_at: str
    supports_orderbook: bool
    supports_funding: bool
    supports_execution_updates: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def check_exchange_connectivity(exchange_name: str, timeout_sec: float = 2.0) -> ExchangeHealth:
    exchange = str(exchange_name or "").strip().lower()
    checked_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    if exchange == "binance":
        return _check_binance(timeout_sec=timeout_sec, checked_at=checked_at)
    if exchange == "hyperliquid":
        return _check_hyperliquid(timeout_sec=timeout_sec, checked_at=checked_at)
    return ExchangeHealth(
        exchange=exchange_name,
        connected=False,
        public_market_data_ok=False,
        private_api_ok=None,
        orderbook_ok=False,
        funding_ok=None,
        latency_ms=None,
        error=f"UNSUPPORTED_EXCHANGE:{exchange_name}",
        checked_at=checked_at,
        supports_orderbook=False,
        supports_funding=False,
        supports_execution_updates=False,
    )


def check_required_exchanges_health(exchanges: list[str], timeout_sec: float = 2.0) -> list[ExchangeHealth]:
    return [check_exchange_connectivity(name, timeout_sec=timeout_sec) for name in exchanges]


def health_has_secret_leak(health: ExchangeHealth) -> bool:
    blob = " ".join(str(v) for v in health.to_dict().values() if v is not None)
    secret_envs = ["BINANCE_API_KEY", "BINANCE_SECRET", "HYPERLIQUID_API_KEY", "HYPERLIQUID_SECRET"]
    for env in secret_envs:
        val = os.getenv(env)
        if val and val in blob:
            return True
    return False


def _check_binance(*, timeout_sec: float, checked_at: str) -> ExchangeHealth:
    start = time.perf_counter()
    url = "https://api.binance.com/api/v3/ticker/bookTicker?symbol=BTCUSDT"
    try:
        payload = _fetch_json(url, timeout_sec=timeout_sec)
        bid = float(payload.get("bidPrice", 0.0) or 0.0)
        ask = float(payload.get("askPrice", 0.0) or 0.0)
        orderbook_ok = bid > 0.0 and ask > 0.0 and ask >= bid
        connected = orderbook_ok
        return ExchangeHealth(
            exchange="binance",
            connected=connected,
            public_market_data_ok=connected,
            private_api_ok=None,
            orderbook_ok=orderbook_ok,
            funding_ok=None,
            latency_ms=round((time.perf_counter() - start) * 1000.0, 3),
            error=None if connected else "BINANCE_INVALID_BOOK_TICKER",
            checked_at=checked_at,
            supports_orderbook=True,
            supports_funding=True,
            supports_execution_updates=True,
        )
    except Exception as exc:  # noqa: BLE001
        return ExchangeHealth(
            exchange="binance",
            connected=False,
            public_market_data_ok=False,
            private_api_ok=None,
            orderbook_ok=False,
            funding_ok=None,
            latency_ms=round((time.perf_counter() - start) * 1000.0, 3),
            error=f"BINANCE_CONNECTIVITY_ERROR:{type(exc).__name__}",
            checked_at=checked_at,
            supports_orderbook=True,
            supports_funding=True,
            supports_execution_updates=True,
        )


def _check_hyperliquid(*, timeout_sec: float, checked_at: str) -> ExchangeHealth:
    start = time.perf_counter()
    url = "https://api.hyperliquid.xyz/info"
    body = b'{"type":"allMids"}'
    req = request.Request(url, method="POST", data=body, headers={"Content-Type": "application/json"})
    try:
        payload = _fetch_json(req, timeout_sec=timeout_sec)
        connected = isinstance(payload, dict) and bool(payload)
        return ExchangeHealth(
            exchange="hyperliquid",
            connected=connected,
            public_market_data_ok=connected,
            private_api_ok=None,
            orderbook_ok=connected,
            funding_ok=None,
            latency_ms=round((time.perf_counter() - start) * 1000.0, 3),
            error=None if connected else "HYPERLIQUID_EMPTY_RESPONSE",
            checked_at=checked_at,
            supports_orderbook=True,
            supports_funding=True,
            supports_execution_updates=False,
        )
    except Exception as exc:  # noqa: BLE001
        return ExchangeHealth(
            exchange="hyperliquid",
            connected=False,
            public_market_data_ok=False,
            private_api_ok=None,
            orderbook_ok=False,
            funding_ok=None,
            latency_ms=round((time.perf_counter() - start) * 1000.0, 3),
            error=f"HYPERLIQUID_CONNECTIVITY_ERROR:{type(exc).__name__}",
            checked_at=checked_at,
            supports_orderbook=True,
            supports_funding=True,
            supports_execution_updates=False,
        )


def _fetch_json(url_or_request: str | request.Request, *, timeout_sec: float) -> Any:
    with request.urlopen(url_or_request, timeout=timeout_sec) as resp:  # noqa: S310
        raw = resp.read().decode("utf-8")
    import json

    return json.loads(raw)
