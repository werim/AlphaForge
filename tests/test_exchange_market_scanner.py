from __future__ import annotations

import asyncio
import json
from urllib import request

import pytest

from alphaforge.config import load_config_from_env
from alphaforge.exchange_market_scanner import (_binance_kline_geometry, _fetch_json_with_latency,
    enrich_selected_market_geometry, scan_exchange_markets)
from alphaforge.execution import build_execution_context, build_execution_cost_model
from alphaforge.signal_geometry import build_breakout_geometry_with_diagnostics


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
    assert btc["market_data_latency_ms"] is not None
    assert btc["market_data_latency_source"] == "BINANCE_PUBLIC_HTTP_RTT"


def test_binance_public_http_latency_is_monotonic_round_trip_milliseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter((10.0, 11.25))
    monkeypatch.setattr("alphaforge.exchange_market_scanner.time.perf_counter", lambda: next(ticks))
    monkeypatch.setattr("alphaforge.exchange_market_scanner._fetch_json", lambda _url, *, timeout_sec: {"ok": True})
    payload, latency_ms = _fetch_json_with_latency("https://example.invalid", timeout_sec=1.0)
    assert payload == {"ok": True}
    assert latency_ms == pytest.approx(1250.0)
    model = build_execution_cost_model({
        "spread_pct": 0.0, "expected_slippage_pct": 0.0, "fee_pct": 0.0,
        "funding_rate_pct": 0.0, "latency_ms": latency_ms,
        "liquidity_score": 1.0, "volatility_regime": "normal",
    })
    assert model.latency_penalty == pytest.approx(0.25)


def test_binance_unavailable_monotonic_clock_does_not_fabricate_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYPERLIQUID_ENABLED", "false")
    monkeypatch.setattr("time.perf_counter", lambda: (_ for _ in ()).throw(RuntimeError("clock unavailable")))
    monkeypatch.setattr("urllib.request.urlopen", _urlopen_multi([
        {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]},
        [{"symbol": "BTCUSDT", "lastPrice": "100", "quoteVolume": "90000000", "priceChangePercent": "1"}],
        [{"symbol": "BTCUSDT", "bidPrice": "99.9", "askPrice": "100.1"}],
        [],
    ]))
    btc = asyncio.run(scan_exchange_markets(load_config_from_env()))[0]
    assert btc["market_data_latency_ms"] is None
    assert btc["market_data_latency_status"] == "UNAVAILABLE"


def test_canonical_short_geometry_overrides_unresolved_scanner_direction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("alphaforge.exchange_market_scanner._binance_kline_geometry", lambda *a, **k: {
        "side": "SHORT", "entry": 99.0, "sl": 101.0, "tp": 96.0, "rr": 1.5,
        "setup_type": "BREAKOUT_DOWN",
    })
    candidate = {"symbol": "BTCUSDT", "source_exchange": "binance", "timeframe": "1m", "entry": 100.0}
    result = asyncio.run(enrich_selected_market_geometry([candidate], load_config_from_env()))[0]
    assert (result["side"], result["entry"], result["sl"], result["tp"]) == ("SHORT", 99.0, 101.0, 96.0)


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
    assert btc["geometry_status"] == "COMPLETE"
    assert btc["execution_candle_open_ts"] == 60_000
    assert len(btc["recent_klines"]) == 2
    assert btc["recent_klines"][-1]["high"] == 101.0
    assert btc["recent_klines_source"] == "BINANCE_CLOSED_1M_KLINES"
    execution_ctx = build_execution_context(btc)
    assert execution_ctx["volatility_regime"] == "high"
    assert execution_ctx["volatility_status"] == "MEASURED"
    assert execution_ctx["volatility_source"] == "BINANCE_CLOSED_1M_KLINES"
    assert build_execution_cost_model(execution_ctx).volatility_penalty == pytest.approx(0.12)


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
    assert btc["geometry_status"] == "UNAVAILABLE"
    assert btc["geometry_reason"] == "KLINE_INSUFFICIENT_ROWS"
    assert btc["geometry_source"] == "BINANCE_CLOSED_1M_KLINES"


@pytest.mark.parametrize(("current", "previous", "reason"), [
    ({"open": "bad", "high": 2, "low": 1, "close": 2}, {"open": 1, "high": 2, "low": 1, "close": 1}, "KLINE_MALFORMED_PAYLOAD"),
    ({"open": 2, "high": 1, "low": 2, "close": 2}, {"open": 1, "high": 2, "low": 1, "close": 1}, "OHLC_INVALID"),
    ({"open": 1, "high": 2, "low": 1, "close": 1}, {"open": 1, "high": 2, "low": 1, "close": 1}, "ZERO_RISK_GEOMETRY"),
])
def test_geometry_diagnostics_never_fabricate(current, previous, reason):
    geometry, observed = build_breakout_geometry_with_diagnostics(current, previous)
    assert geometry == {}
    assert observed == reason


def test_kline_fetch_failure_is_queryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("alphaforge.exchange_market_scanner._fetch_json",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    result = _binance_kline_geometry("https://example.invalid", "BTCUSDT", timeout_sec=1)
    assert result == {"geometry_status": "UNAVAILABLE", "geometry_reason": "KLINE_FETCH_FAILED",
                      "geometry_source": "BINANCE_CLOSED_1M_KLINES"}


def test_kline_timeout_is_queryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("alphaforge.exchange_market_scanner._fetch_json",
                        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timed out")))
    result = _binance_kline_geometry("https://example.invalid", "BTCUSDT", timeout_sec=1)
    assert result == {"geometry_status": "UNAVAILABLE", "geometry_reason": "KLINE_TIMEOUT",
                      "geometry_source": "BINANCE_CLOSED_1M_KLINES"}


def test_binance_urls_use_fapi_v1_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_urls: list[str] = []

    # Isolate this test from repository/process .env values. This test
    # explicitly verifies production Binance Futures REST URL construction.
    monkeypatch.setenv("BINANCE_ENVIRONMENT", "production")
    monkeypatch.delenv("BINANCE_TESTNET", raising=False)
    monkeypatch.setenv("BINANCE_BASE_URL", "https://fapi.binance.com")
    monkeypatch.setenv("BINANCE_WS_URL", "wss://fstream.binance.com/ws")
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
