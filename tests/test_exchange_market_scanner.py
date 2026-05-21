from __future__ import annotations

import asyncio
import json

import pytest

from alphaforge.config import load_config_from_env
from alphaforge.exchange_market_scanner import scan_exchange_markets


def _urlopen_multi(payloads: list[object]):
    class _Resp:
        def __init__(self, payload: object):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps(self.payload).encode("utf-8")

    idx = {"i": 0}

    def _open(*args, **kwargs):
        payload = payloads[idx["i"]]
        idx["i"] += 1
        return _Resp(payload)

    return _open


def test_scan_exchange_markets_uses_public_endpoints_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _urlopen_multi(
            [
                [{"symbol": "BTCUSDT", "bidPrice": "100", "askPrice": "100.1", "lastPrice": "100.05", "quoteVolume": "90000000", "priceChangePercent": "1.2"}],
                [{"symbol": "BTCUSDT", "lastFundingRate": "0.0001"}],
                {"ETH": "2500.0"},
            ]
        ),
    )
    cfg = load_config_from_env()
    rows = asyncio.run(scan_exchange_markets(cfg))
    assert rows
    assert any(row.get("source_exchange") == "binance" for row in rows)
    assert any(row.get("source_exchange") == "hyperliquid" for row in rows)
    assert all("symbol" in row and "entry" in row for row in rows)


def test_scan_exchange_markets_handles_exchange_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    cfg = load_config_from_env()
    rows = asyncio.run(scan_exchange_markets(cfg))
    assert rows == []
