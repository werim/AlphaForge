from __future__ import annotations

from urllib import error
from urllib.parse import parse_qs, urlparse
import io

import pytest

from alphaforge.binance_reconciliation_provider import BinanceReadonlyReconciliationConfig, BinanceReadonlyReconciliationProvider
from alphaforge.config import load_config_from_env


def test_signature_and_headers_deterministic() -> None:
    seen = {}

    def fake_http(url, headers, timeout):
        seen["url"] = url
        seen["headers"] = headers
        return []

    provider = BinanceReadonlyReconciliationProvider(
        config=BinanceReadonlyReconciliationConfig(base_url="https://fapi.binance.com", api_key="k", api_secret="s", recv_window_ms=7000),
        tracked_symbols=lambda: set(),
        now_ms=lambda: 1700000000000,
        http_get_json=fake_http,
    )
    provider.snapshot()
    assert "timestamp=1700000000000" in seen["url"]
    assert "recvWindow=7000" in seen["url"]
    assert "signature=" in seen["url"]
    assert seen["headers"]["X-MBX-APIKEY"] == "k"


def test_provider_success_normalization_hedge_mode() -> None:
    responses = {
        "/fapi/v3/positionRisk": [
            {"symbol": "BTCUSDT", "positionAmt": "0.10", "entryPrice": "40000", "positionSide": "LONG", "unRealizedProfit": "1.2"},
            {"symbol": "BTCUSDT", "positionAmt": "-0.05", "entryPrice": "41000", "positionSide": "SHORT", "unRealizedProfit": "-0.4"},
        ],
        "/fapi/v1/openOrders": [],
        "/fapi/v1/userTrades": [],
    }

    def fake_http(url, headers, timeout):
        for path, payload in responses.items():
            if path in url:
                return payload
        return []

    provider = BinanceReadonlyReconciliationProvider(
        config=BinanceReadonlyReconciliationConfig(base_url="https://fapi.binance.com", api_key="k", api_secret="s"),
        tracked_symbols=lambda: {"BTCUSDT"},
        now_ms=lambda: 1700000000000,
        http_get_json=fake_http,
    )
    snap = provider.snapshot()
    assert snap["evidence_status"] == "COMPLETE"
    assert len(snap["positions"]) == 2
    assert {p["position_side"] for p in snap["positions"]} == {"LONG", "SHORT"}


def test_provider_fail_closed_and_redacts_secrets() -> None:
    def fake_http(url, headers, timeout):
        raise error.HTTPError(url, 401, "bad", hdrs=None, fp=None)

    provider = BinanceReadonlyReconciliationProvider(
        config=BinanceReadonlyReconciliationConfig(base_url="https://fapi.binance.com", api_key="mykey", api_secret="mysecret"),
        now_ms=lambda: 1700000000000,
        http_get_json=fake_http,
    )
    snap = provider.snapshot()
    assert snap["evidence_status"] == "INCOMPLETE"
    blob = str(snap)
    assert "mykey" not in blob
    assert "mysecret" not in blob
    assert "signature=" not in blob


def test_provider_missing_credentials_rejected() -> None:
    with pytest.raises(Exception):
        BinanceReadonlyReconciliationProvider(config=BinanceReadonlyReconciliationConfig(base_url="https://fapi.binance.com", api_key="", api_secret="s"))


def test_dusty_53_row_position_risk_queries_only_tracked_symbol() -> None:
    calls: list[str] = []
    positions = [{"symbol": f"DUST{i}USDT", "positionAmt": "0" if i % 2 else "0.000000001"} for i in range(53)]

    def fake_http(url, headers, timeout):
        calls.append(url)
        if "/positionRisk" in url:
            return positions
        return []

    provider = BinanceReadonlyReconciliationProvider(
        config=BinanceReadonlyReconciliationConfig(base_url="https://demo-fapi.binance.com", api_key="k", api_secret="s", request_timeout_sec=2, recv_window_ms=5000),
        tracked_symbols=lambda: {"BTCUSDT"}, now_ms=lambda: 1_700_000_000_000, http_get_json=fake_http,
    )
    snapshot = provider.snapshot()

    trade_calls = [url for url in calls if "/userTrades" in url]
    assert len(trade_calls) == 1
    assert parse_qs(urlparse(trade_calls[0]).query)["symbol"] == ["BTCUSDT"]
    assert snapshot["fill_symbol_evidence"]["symbols"] == ["BTCUSDT"]
    assert snapshot["orphan_positions"] == 0
    assert snapshot["evidence_status"] == "COMPLETE"


def test_fill_scope_is_union_of_real_positions_tracked_orders_and_recent_lifecycle() -> None:
    queried: set[str] = set()
    positions = [
        {"symbol": "BTCUSDT", "positionAmt": "0.01"},
        {"symbol": "ETHUSDT", "positionAmt": "-0.02"},
        {"symbol": "XRPUSDT", "positionAmt": "0.000000001"},
    ]
    orders = [{"symbol": "SOLUSDT", "orderId": 1}]

    def fake_http(url, headers, timeout):
        if "/positionRisk" in url: return positions
        if "/openOrders" in url: return orders
        if "/userTrades" in url:
            queried.add(parse_qs(urlparse(url).query)["symbol"][0])
        return []

    provider = BinanceReadonlyReconciliationProvider(
        config=BinanceReadonlyReconciliationConfig(base_url="https://fapi.binance.com", api_key="k", api_secret="s"),
        tracked_symbols=lambda: {"BNBUSDT"}, recently_active_symbols=lambda: {"ADAUSDT"},
        now_ms=lambda: 1_700_000_000_000, http_get_json=fake_http,
    )
    snapshot = provider.snapshot()
    assert snapshot["evidence_status"] == "COMPLETE"
    assert queried == {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT"}


def test_fill_symbol_cap_fails_closed_with_precise_scope_evidence() -> None:
    calls: list[str] = []

    def fake_http(url, headers, timeout):
        calls.append(url)
        return []

    provider = BinanceReadonlyReconciliationProvider(
        config=BinanceReadonlyReconciliationConfig(base_url="https://demo-fapi.binance.com", api_key="k", api_secret="s", max_fill_symbols=2),
        tracked_symbols=lambda: {"BTCUSDT", "ETHUSDT", "SOLUSDT"}, http_get_json=fake_http,
    )
    snapshot = provider.snapshot()
    assert snapshot["evidence_status"] == "INCOMPLETE"
    assert not any("/userTrades" in url for url in calls)
    assert snapshot["errors"] == [{
        "reason": "max_fill_symbols_exceeded", "endpoint_class": "USER_TRADES_SCOPE", "symbol": None,
        "http_status": None, "binance_code": None, "retry_count": 0, "timeout_category": None,
        "environment": "DEMO", "selected_count": 3, "max_fill_symbols": 2,
        "selected_symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    }]


def test_minus_1021_refreshes_time_and_resigns_once() -> None:
    timestamps: list[int] = []
    now_values = iter([1000, 1001, 1002, 1003, 1004, 1005, 1006, 1007])
    failed = False

    def fake_http(url, headers, timeout):
        nonlocal failed
        if url.endswith("/fapi/v1/time"):
            return {"serverTime": 5001}
        timestamps.append(int(parse_qs(urlparse(url).query)["timestamp"][0]))
        if not failed:
            failed = True
            raise error.HTTPError(url, 400, "timestamp", None, io.BytesIO(b'{"code":-1021,"msg":"outside recvWindow"}'))
        return []

    provider = BinanceReadonlyReconciliationProvider(
        config=BinanceReadonlyReconciliationConfig(base_url="https://demo-fapi.binance.com", api_key="k", api_secret="s"),
        now_ms=lambda: next(now_values), http_get_json=fake_http,
    )
    snapshot = provider.snapshot()
    assert snapshot["evidence_status"] == "COMPLETE"
    assert timestamps[1] != timestamps[0]
    assert any(item.get("action") == "server_time_offset_refreshed" for item in snapshot["request_evidence"])


def test_timeout_retries_are_bounded_and_remain_fail_closed() -> None:
    attempts = 0

    def fake_http(url, headers, timeout):
        nonlocal attempts
        attempts += 1
        raise TimeoutError("timed out")

    provider = BinanceReadonlyReconciliationProvider(
        config=BinanceReadonlyReconciliationConfig(base_url="https://fapi.binance.com", api_key="k", api_secret="s", max_retries=2),
        http_get_json=fake_http, sleep=lambda _: None,
    )
    snapshot = provider.snapshot()
    assert attempts == 3
    assert snapshot["evidence_status"] == "INCOMPLETE"
    assert snapshot["errors"][0]["retry_count"] == 2
    assert snapshot["errors"][0]["timeout_category"] == "timeout"


def test_handshake_urlerror_is_retried_but_deterministic_4xx_is_not() -> None:
    attempts = 0

    def handshake_then_ok(url, headers, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error.URLError(TimeoutError("TLS handshake timed out"))
        return []

    provider = BinanceReadonlyReconciliationProvider(
        config=BinanceReadonlyReconciliationConfig(base_url="https://demo-fapi.binance.com", api_key="k", api_secret="s"),
        http_get_json=handshake_then_ok, sleep=lambda _: None,
    )
    assert provider.snapshot()["evidence_status"] == "COMPLETE"
    assert attempts == 3  # positionRisk retry plus openOrders

    deterministic_attempts = 0

    def bad_request(url, headers, timeout):
        nonlocal deterministic_attempts
        deterministic_attempts += 1
        raise error.HTTPError(url, 400, "bad request", None, io.BytesIO(b'{"code":-1100}'))

    provider = BinanceReadonlyReconciliationProvider(
        config=BinanceReadonlyReconciliationConfig(base_url="https://fapi.binance.com", api_key="k", api_secret="s"),
        http_get_json=bad_request, sleep=lambda _: None,
    )
    snapshot = provider.snapshot()
    assert snapshot["evidence_status"] == "INCOMPLETE"
    assert deterministic_attempts == 1
    assert snapshot["errors"][0]["binance_code"] == -1100


def test_reconciliation_bounds_come_from_canonical_config_registry(monkeypatch) -> None:
    monkeypatch.setenv("ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC", "3.5")
    monkeypatch.setenv("ALPHAFORGE_BINANCE_RECV_WINDOW_MS", "9000")
    monkeypatch.setenv("ALPHAFORGE_RECONCILIATION_POSITION_EPSILON", "0.00002")
    monkeypatch.setenv("ALPHAFORGE_RECONCILIATION_MAX_FILL_SYMBOLS", "7")
    runtime = load_config_from_env().runtime
    assert runtime.reconciliation_timeout_sec == 3.5
    assert runtime.binance_reconciliation_recv_window_ms == 9000
    assert runtime.reconciliation_position_epsilon == 0.00002
    assert runtime.reconciliation_max_fill_symbols == 7
