from __future__ import annotations

import asyncio
import json
from urllib import request

import pytest

from alphaforge.config import load_config_from_env
from alphaforge.exchange_market_scanner import enrich_selected_market_geometry, scan_exchange_markets


def _urlopen_multi(payloads: list[object], captured_urls: list[str] | None = None):
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

    def _open(url_or_request, *args, **kwargs):
        if captured_urls is not None:
            if isinstance(url_or_request, request.Request):
                captured_urls.append(url_or_request.full_url)
            else:
                captured_urls.append(str(url_or_request))
        payload = payloads[idx["i"]]
        idx["i"] += 1
        return _Resp(payload)

    return _open


def test_scan_exchange_markets_uses_public_endpoints_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _urlopen_multi(
            [
                {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]},
                [{"symbol": "BTCUSDT", "lastPrice": "100.05", "quoteVolume": "90000000", "priceChangePercent": "1.2"}],
                [{"symbol": "BTCUSDT", "bidPrice": "100", "askPrice": "100.1"}],
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


def test_binance_bookticker_spread_maps_correctly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _urlopen_multi(
            [
                {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]},
                [{"symbol": "BTCUSDT", "lastPrice": "100.10", "quoteVolume": "90000000", "priceChangePercent": "1.2"}],
                [{"symbol": "BTCUSDT", "bidPrice": "100", "askPrice": "100.2"}],
                [{"symbol": "BTCUSDT", "lastFundingRate": "0.0003"}],
                {},
            ]
        ),
    )
    cfg = load_config_from_env()
    rows = asyncio.run(scan_exchange_markets(cfg))
    btc = next(row for row in rows if row.get("source_exchange") == "binance")
    assert btc["entry"] == pytest.approx(100.1)
    assert btc["spread_pct"] == pytest.approx((100.2 - 100.0) / 100.1)
    assert btc["spread_bps"] == pytest.approx(btc["spread_pct"] * 10_000.0)
    assert btc["funding_rate_pct"] == pytest.approx(0.0003)
    assert btc["spread_status"] == "MEASURED"
    assert btc["spread_source"] == "BINANCE_BOOK_TICKER"
    assert btc["funding_status"] == "MEASURED"


def test_binance_closed_1m_candles_supply_canonical_trade_geometry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYPERLIQUID_ENABLED", "false")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _urlopen_multi([
            {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]},
            [{"symbol": "BTCUSDT", "lastPrice": "999", "lowPrice": "1", "highPrice": "2000",
              "quoteVolume": "90000000", "priceChangePercent": "1.2"}],
            [{"symbol": "BTCUSDT", "bidPrice": "99.9", "askPrice": "100.1"}],
            [{"symbol": "BTCUSDT", "lastFundingRate": "0.0001"}],
            [[0, "98", "100", "97", "99", "10"],
             [60_000, "99", "101", "98", "100", "12"],
             [120_000, "100", "500", "1", "400", "1"]],
        ]),
    )
    cfg = load_config_from_env()
    scanned = asyncio.run(scan_exchange_markets(cfg))
    assert "sl" not in scanned[0] and "tp" not in scanned[0]
    btc = asyncio.run(enrich_selected_market_geometry(scanned, cfg))[0]
    assert btc["sl"] == 97.0
    assert btc["entry"] == 100.0
    assert btc["tp"] == pytest.approx(100.0 + 3.0 * (1.2 + 8.0 / 99.0))
    assert btc["rr"] == pytest.approx(1.2 + 8.0 / 99.0)
    assert btc["setup_type"] == "BREAKOUT_UP"


def test_binance_invalid_or_missing_range_does_not_fabricate_geometry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYPERLIQUID_ENABLED", "false")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _urlopen_multi([
            {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]},
            [{"symbol": "BTCUSDT", "lastPrice": "100", "lowPrice": "100", "highPrice": "108",
              "quoteVolume": "90000000", "priceChangePercent": "1.2"}],
            [{"symbol": "BTCUSDT", "bidPrice": "99.9", "askPrice": "100.1"}],
            [],
            [[0, "98", "100", "97", "99", "10"]],
        ]),
    )
    cfg = load_config_from_env()
    scanned = asyncio.run(scan_exchange_markets(cfg))
    btc = asyncio.run(enrich_selected_market_geometry(scanned, cfg))[0]
    assert "sl" not in btc and "tp" not in btc and "rr" not in btc


def test_binance_urls_use_fapi_v1_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_urls: list[str] = []
    monkeypatch.setenv("BINANCE_BASE_URL", "https://fapi.binance.com")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _urlopen_multi(
            [
                {"symbols": []},
                [],
                [],
                [],
                {},
            ],
            captured_urls=captured_urls,
        ),
    )
    cfg = load_config_from_env()
    asyncio.run(scan_exchange_markets(cfg))
    assert any(url.endswith("/fapi/v1/ticker/24hr") for url in captured_urls)
    assert any(url.endswith("/fapi/v1/ticker/bookTicker") for url in captured_urls)
    assert any(url.endswith("/fapi/v1/premiumIndex") for url in captured_urls)
    assert not any("/api/v3/" in url for url in captured_urls)


def test_scan_exchange_markets_returns_empty_on_malformed_binance_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _urlopen_multi(
            [
                {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]},
                {"symbol": "BTCUSDT"},
                [{"symbol": "BTCUSDT", "bidPrice": "100", "askPrice": "100.1"}],
                [{"symbol": "BTCUSDT", "lastFundingRate": "0.0001"}],
                {},
            ]
        ),
    )
    cfg = load_config_from_env()
    rows = asyncio.run(scan_exchange_markets(cfg))
    assert rows == []


def test_scan_exchange_markets_handles_exchange_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    cfg = load_config_from_env()
    rows = asyncio.run(scan_exchange_markets(cfg))
    assert rows == []


def test_hyperliquid_mid_only_sets_unavailable_spread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("urllib.request.urlopen", _urlopen_multi([{"symbols": []}, [], [], [], {"ETH": "2500.0"}]))
    cfg = load_config_from_env()
    rows = asyncio.run(scan_exchange_markets(cfg))
    eth = next(row for row in rows if row.get("source_exchange") == "hyperliquid")
    assert eth["spread_pct"] is None
    assert eth["spread_status"] == "UNAVAILABLE"
    assert eth["spread_source"] == "MID_ONLY_NO_BOOK"


def test_binance_pending_unicode_is_not_a_new_trade_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    symbol = "龙虾USDT"
    monkeypatch.setattr("urllib.request.urlopen", _urlopen_multi([
        {"symbols": [{"symbol": symbol, "status": "PENDING_TRADING"}]},
        [{"symbol": symbol, "lastPrice": "1", "quoteVolume": "90000000", "priceChangePercent": "1"}],
        [{"symbol": symbol, "bidPrice": "0.99", "askPrice": "1.01"}],
        [{"symbol": symbol, "lastFundingRate": "0"}],
        {},
    ]))
    rows = asyncio.run(scan_exchange_markets(load_config_from_env()))
    assert not any(row["symbol"] == symbol for row in rows)
