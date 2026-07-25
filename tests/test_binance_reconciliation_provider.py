from __future__ import annotations

from urllib import error

import pytest

from alphaforge.binance_reconciliation_provider import BinanceReadonlyReconciliationConfig, BinanceReadonlyReconciliationProvider


def test_signature_and_headers_deterministic() -> None:
    seen = {"urls": []}

    def fake_http(url, headers, timeout):
        seen["url"] = url
        seen["urls"].append(url)
        seen["headers"] = headers
        return []

    provider = BinanceReadonlyReconciliationProvider(
        config=BinanceReadonlyReconciliationConfig(base_url="https://fapi.binance.com", api_key="k", api_secret="s", recv_window_ms=7000),
        tracked_symbols=lambda: set(),
        now_ms=lambda: 1700000000000,
        http_get_json=fake_http,
    )
    provider.snapshot()
    assert "timestamp=1700000000000" in seen["urls"][0]
    assert "timestamp=1700000000001" in seen["urls"][1]
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

import json
from decimal import Decimal
from pathlib import Path


def _provider(positions, orders=(), tracked=(), **config):
    calls = []
    def http(url, headers, timeout):
        calls.append(url)
        if "positionRisk" in url: return positions
        if "exchangeInfo" in url:
            return {"symbols": [{"symbol": row["symbol"]} for row in positions
                                if isinstance(row, dict) and isinstance(row.get("symbol"), str)
                                and "_" in row["symbol"]]}
        if "openOrders" in url: return list(orders)
        if "userTrades" in url: return []
        return {"serverTime": 1700000000001}
    p = BinanceReadonlyReconciliationProvider(
        config=BinanceReadonlyReconciliationConfig(base_url="https://demo-fapi.binance.com", api_key="k", api_secret="s", **config),
        tracked_symbols=lambda: set(tracked), now_ms=lambda: 1700000000000, http_get_json=http)
    return p, calls


def test_synthetic_53_zero_rows_do_not_fan_out():
    rows = json.loads(Path("tests/fixtures/synthetic_position_risk_53_rows.json").read_text())
    provider, calls = _provider(rows)
    snap = provider.snapshot()
    assert snap["evidence_status"] == "COMPLETE"
    assert snap["selected_symbols"] == []
    assert not any("userTrades" in call for call in calls)


def test_only_real_position_and_tracked_symbol_expand_scope():
    rows = json.loads(Path("tests/fixtures/synthetic_position_risk_53_rows.json").read_text())
    rows[-1] = {**rows[-1], "symbol": "BTCUSDT", "positionAmt": "0.00000002"}
    provider, calls = _provider(rows, tracked={"ETHUSDT"}, position_epsilon=Decimal("0.00000001"))
    snap = provider.snapshot()
    assert snap["selected_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert sum("userTrades" in call for call in calls) == 2


def test_decimal_epsilon_and_open_status_scope_are_exact():
    positions = [
        {"symbol":"DUSTUSDT", "positionAmt":"0.000000010", "positionSide":"BOTH"},
        {"symbol":"SMALLUSDT", "positionAmt":"0.000000011", "positionSide":"BOTH"},
    ]
    orders = [{"symbol":"BTCUSDT", "status":"CANCELED"}, {"symbol":"ETHUSDT", "status":"NEW"}]
    provider, _ = _provider(positions, orders, position_epsilon=Decimal("0.000000010"))
    snap = provider.snapshot()
    assert snap["selected_symbols"] == ["ETHUSDT", "SMALLUSDT"]
    assert snap["positions"][0]["epsilon_filtered"] is True


@pytest.mark.parametrize("row,error_text", [
    ({"symbol":"BTC/USDT", "positionAmt":"1"}, "invalid_symbol"),
    ({"symbol":"BTCUSDT", "positionAmt":"not-a-number"}, "malformed_position_amount"),
])
def test_corrupt_exposure_fails_closed(row, error_text):
    provider, calls = _provider([row])
    snap = provider.snapshot()
    assert snap["evidence_status"] == "INCOMPLETE"
    assert error_text in str(snap["errors"])
    assert not any("userTrades" in call for call in calls)


def test_scope_cap_fails_before_fill_and_preserves_evidence():
    rows = [{"symbol":f"S{i}USDT", "positionAmt":"1"} for i in range(3)]
    provider, calls = _provider(rows, max_fill_symbols=2)
    snap = provider.snapshot()
    assert snap["evidence_status"] == "INCOMPLETE"
    assert len(snap["positions"]) == 3 and snap["orphan_positions"] is None
    assert snap["selected_count"] == 3 and snap["configured_max"] == 2
    assert not any("userTrades" in call for call in calls)


def test_later_fill_failure_preserves_positions_orders_and_unknown_counts():
    def http(url, headers, timeout):
        if "positionRisk" in url: return [{"symbol":"BTCUSDT", "positionAmt":"1"}]
        if "openOrders" in url: return [{"symbol":"BTCUSDT", "status":"NEW"}]
        raise TimeoutError("secret signed url")
    provider = BinanceReadonlyReconciliationProvider(config=BinanceReadonlyReconciliationConfig(base_url="https://x", api_key="key", api_secret="secret", transport_retries=0), http_get_json=http)
    snap = provider.snapshot()
    assert snap["evidence_status"] == "INCOMPLETE"
    assert len(snap["positions"]) == len(snap["orders"]) == 1
    assert snap["orphan_orders"] is snap["orphan_positions"] is None
    assert snap["unknown_unreconciled_symbols"] == ["BTCUSDT"]
    assert "secret" not in str(snap) and "signature=" not in str(snap)

import io
import ssl
import urllib.parse
from alphaforge.binance_reconciliation_provider import BinanceHttpTransport
import alphaforge.binance_reconciliation_provider as provider_module


class _Response:
    def __init__(self, status=200, payload=None, reason="OK"):
        self.status = status; self.reason = reason; self.headers = {"content-type": "application/json"}
        self._body = json.dumps([] if payload is None else payload).encode()
    def read(self): return self._body


class _Connection:
    def __init__(self, responses, failure=None):
        self.responses = responses; self.failure = failure; self.closed = 0; self.requests = []
    def request(self, method, path, headers):
        self.requests.append(path)
        if self.failure: raise self.failure
    def getresponse(self): return self.responses.pop(0)
    def close(self): self.closed += 1


def _install_connections(monkeypatch, responses_or_failures):
    made = []
    queue = list(responses_or_failures)
    def factory(host, port, timeout):
        item = queue.pop(0)
        connection = _Connection(item if isinstance(item, list) else [], item if isinstance(item, BaseException) else None)
        made.append(connection); return connection
    monkeypatch.setattr(provider_module.http.client, "HTTPSConnection", factory)
    monkeypatch.setattr(provider_module.time, "sleep", lambda _: None)
    return made


def test_default_transport_preserves_http_error_body(monkeypatch):
    made = _install_connections(monkeypatch, [[_Response(400, {"code": -1100, "msg": "Illegal characters"})]])
    transport = BinanceHttpTransport()
    with pytest.raises(error.HTTPError) as caught:
        transport.get_json("https://demo-fapi.binance.com/fapi/v3/positionRisk?secret=query", {}, 1)
    assert json.loads(caught.value.read()) == {"code": -1100, "msg": "Illegal characters"}
    assert made[0].closed == 1 and transport._connection is None


def test_default_transport_1021_refreshes_same_host_and_resigns_once(monkeypatch):
    made = _install_connections(monkeypatch, [
        [_Response(400, {"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."})],
        [_Response(200, {"serverTime": 1700000001000}), _Response(200, []), _Response(200, [])],
    ])
    provider = BinanceReadonlyReconciliationProvider(config=BinanceReadonlyReconciliationConfig(
        base_url="https://demo-fapi.binance.com", api_key="k", api_secret="s"), now_ms=lambda: 1700000000000)
    snap = provider.snapshot()
    assert snap["evidence_status"] == "COMPLETE"
    paths = [path for connection in made for path in connection.requests]
    assert paths[0].startswith("/fapi/v3/positionRisk?")
    assert paths[1] == "/fapi/v1/time"
    assert paths[2].startswith("/fapi/v3/positionRisk?") and paths[2] != paths[0]
    assert sum(path == "/fapi/v1/time" for path in paths) == 1
    assert snap["request_evidence"][0]["time_refresh_performed"] is True
    assert snap["http_request_count"] == 4
    assert [a["request_kind"] for a in snap["request_attempts"]] == ["SIGNED", "SERVER_TIME", "SIGNED", "SIGNED"]
    assert made[0].closed and all(connection.closed for connection in made)


def test_default_transport_deterministic_400_not_retried(monkeypatch):
    made = _install_connections(monkeypatch, [[_Response(400, {"code": -1100, "msg": "Illegal characters"})]])
    provider = BinanceReadonlyReconciliationProvider(config=BinanceReadonlyReconciliationConfig(
        base_url="https://demo-fapi.binance.com", api_key="k", api_secret="s"))
    snap = provider.snapshot()
    assert snap["evidence_status"] == "INCOMPLETE"
    assert "status=400:code=-1100" in snap["errors"][0]
    assert snap["request_count"] == 1 and snap["http_request_count"] == 1
    assert snap["request_evidence"][0]["binance_code"] == -1100
    assert len(made) == 1 and made[0].closed == 1


@pytest.mark.parametrize("failure", [TimeoutError("timeout"), ssl.SSLError("tls")])
def test_default_transport_failure_closes_cached_connection(monkeypatch, failure):
    made = _install_connections(monkeypatch, [failure])
    transport = BinanceHttpTransport()
    with pytest.raises(type(failure)):
        transport.get_json("https://demo-fapi.binance.com/fapi/v1/time", {}, 1)
    assert made[0].closed == 1 and transport._connection is None
    transport.close(); transport.close()


@pytest.mark.parametrize("status", [429, 500, 503])
def test_default_transport_transient_http_retry_uses_new_connection(monkeypatch, status):
    made = _install_connections(monkeypatch, [
        [_Response(status, {"code": -1003, "msg": "transient"})],
        [_Response(200, []), _Response(200, [])],
    ])
    provider = BinanceReadonlyReconciliationProvider(config=BinanceReadonlyReconciliationConfig(
        base_url="https://demo-fapi.binance.com", api_key="k", api_secret="s", transport_retries=1))
    snap = provider.snapshot()
    assert snap["evidence_status"] == "COMPLETE"
    assert len(made) == 2 and made[0] is not made[1] and made[0].closed == 1


def test_failed_snapshot_does_not_poison_next_snapshot(monkeypatch):
    made = _install_connections(monkeypatch, [TimeoutError("first"), TimeoutError("retry"), [_Response(200, []), _Response(200, [])]])
    provider = BinanceReadonlyReconciliationProvider(config=BinanceReadonlyReconciliationConfig(
        base_url="https://demo-fapi.binance.com", api_key="k", api_secret="s", transport_retries=1))
    assert provider.snapshot()["evidence_status"] == "INCOMPLETE"
    assert provider.snapshot()["evidence_status"] == "COMPLETE"
    assert all(connection.closed for connection in made)


@pytest.mark.parametrize("symbol", ["????USDT", "éUSDT", "\ufffd\ufffdUSDT"])
def test_exact_zero_invalid_symbol_is_preserved_warning_without_fanout(symbol):
    provider, calls = _provider([{"symbol": symbol, "positionAmt": "0"}])
    snap = provider.snapshot()
    assert snap["evidence_status"] == "COMPLETE"
    assert snap["positions"][0]["exact_zero"] and not snap["positions"][0]["symbol_valid"]
    assert snap["position_warnings"][0]["category"] == "zero_exposure_invalid_symbol"
    assert snap["selected_symbols"] == [] and not any("userTrades" in call for call in calls)
    assert symbol not in str(snap)


def test_legitimate_usdm_delivery_symbol_is_verified_by_exchange_info_then_reconciled():
    symbol = "BTCUSDT_250627"
    provider, calls = _provider([{"symbol": symbol, "positionAmt": "1"}])
    snap = provider.snapshot()
    assert snap["evidence_status"] == "COMPLETE"
    assert snap["positions"][0]["symbol"] == symbol and snap["positions"][0]["symbol_valid"]
    assert snap["selected_symbols"] == [symbol]
    assert snap["endpoint_statuses"] == {
        "positionRisk": "PASS", "exchangeInfo": "PASS", "openOrders": "PASS", "userTrades": "PASS"}
    assert [next(key for key in ("positionRisk", "exchangeInfo", "openOrders", "userTrades") if key in url)
            for url in calls] == ["positionRisk", "exchangeInfo", "openOrders", "userTrades"]


@pytest.mark.parametrize(("symbol", "reason"), [
    ("A" * 28, "overlong"),
    (" BTCUSDT", "surrounding_whitespace"),
    ("BTCUSDT\n", "control_character"),
    ("ＢＴＣUSDT", "non_ascii"),
])
def test_active_malformed_symbol_reasons_are_safe_and_fail_closed(symbol, reason):
    provider, calls = _provider([{"symbol": symbol, "positionAmt": "1"}])
    snap = provider.snapshot()
    assert snap["evidence_status"] == "INCOMPLETE"
    assert f"reason={reason}" in snap["errors"][0]
    assert symbol not in str(snap)
    assert snap["endpoint_statuses"]["positionRisk"] == "PAYLOAD_VALIDATION_FAILED"
    assert snap["endpoint_statuses"]["openOrders"] == "NOT_ATTEMPTED"
    assert not any("openOrders" in call for call in calls)


def test_exchange_info_rejection_of_candidate_symbol_fails_closed():
    symbol = "BTCUSDT_250627"
    calls = []
    def http(url, headers, timeout):
        calls.append(url)
        if "positionRisk" in url: return [{"symbol": symbol, "positionAmt": "1"}]
        if "exchangeInfo" in url: return {"symbols": [{"symbol": "BTCUSDT"}]}
        raise AssertionError("must stop before openOrders")
    provider = BinanceReadonlyReconciliationProvider(config=BinanceReadonlyReconciliationConfig(
        base_url="https://demo-fapi.binance.com", api_key="k", api_secret="s"), http_get_json=http)
    snap = provider.snapshot()
    assert snap["evidence_status"] == "INCOMPLETE"
    assert "reason=not_in_exchange_info" in snap["errors"][0]
    assert snap["endpoint_statuses"]["exchangeInfo"] == "PASS"
    assert snap["endpoint_statuses"]["openOrders"] == "NOT_ATTEMPTED"


@pytest.mark.parametrize(("amount", "reason"), [("0.000000001", "epsilon_position_invalid_symbol"), ("1", "active_position_invalid_symbol")])
def test_nonzero_invalid_symbol_fails_closed_and_is_preserved(amount, reason):
    provider, calls = _provider([{"symbol": "????USDT", "positionAmt": amount}])
    snap = provider.snapshot()
    assert snap["evidence_status"] == "INCOMPLETE" and reason in snap["errors"][0]
    assert len(snap["positions"]) == 1 and Decimal(snap["positions"][0]["qty_exact"]) == Decimal(amount)
    assert snap["orphan_positions"] is None and not any("userTrades" in call for call in calls)


def test_malformed_amount_precedes_invalid_symbol_policy():
    provider, _ = _provider([{"symbol": "????USDT", "positionAmt": "bad"}])
    snap = provider.snapshot()
    assert "malformed_position_amount" in snap["errors"][0]
    assert "invalid_symbol" not in snap["errors"][0]


def test_completed_fill_evidence_survives_later_symbol_failure():
    def http(url, headers, timeout):
        if "positionRisk" in url: return [{"symbol":"AAAUSDT", "positionAmt":"1"}, {"symbol":"BBBUSDT", "positionAmt":"1"}]
        if "openOrders" in url: return []
        if "symbol=AAAUSDT" in url: return [{"id":1, "orderId":2, "symbol":"AAAUSDT", "qty":"1", "price":"1"}]
        raise TimeoutError("second fill failed")
    provider = BinanceReadonlyReconciliationProvider(config=BinanceReadonlyReconciliationConfig(
        base_url="https://demo-fapi.binance.com", api_key="k", api_secret="s", transport_retries=0), http_get_json=http)
    snap = provider.snapshot()
    assert snap["evidence_status"] == "INCOMPLETE" and len(snap["positions"]) == 2
    assert [fill["symbol"] for fill in snap["fills"]] == ["AAAUSDT"]
    assert snap["coverage"]["userTrades"] == ["AAAUSDT"]
    assert snap["unknown_unreconciled_symbols"] == ["BBBUSDT"]
    assert snap["orphan_orders"] is snap["orphan_positions"] is snap["duplicate_fills"] is None


def test_invalid_zero_warning_does_not_erase_valid_exchange_evidence():
    provider, _ = _provider(
        [{"symbol":"BTCUSDT", "positionAmt":"1"}, {"symbol":"????USDT", "positionAmt":"0"}],
        orders=[{"symbol":"ETHUSDT", "status":"NEW"}])
    snap = provider.snapshot()
    assert snap["evidence_status"] == "COMPLETE" and len(snap["positions"]) == 2 and len(snap["orders"]) == 1
    assert snap["selected_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert len(snap["position_warnings"]) == 1


def test_1021_refresh_failure_preserves_prior_evidence():
    def http(url, headers, timeout):
        if url.endswith("/fapi/v1/time"): raise TimeoutError("time unavailable")
        if "positionRisk" in url: return [{"symbol":"BTCUSDT", "positionAmt":"1"}]
        if "openOrders" in url: return []
        raise error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(json.dumps({"code":-1021,"msg":"timestamp"}).encode()))
    provider = BinanceReadonlyReconciliationProvider(config=BinanceReadonlyReconciliationConfig(
        base_url="https://demo-fapi.binance.com", api_key="k", api_secret="s", transport_retries=0), http_get_json=http)
    snap = provider.snapshot()
    assert snap["evidence_status"] == "INCOMPLETE" and len(snap["positions"]) == 1 and snap["orders"] == []
    assert snap["failed_endpoint"] == "userTrades" and snap["failed_symbol"] == "BTCUSDT"
    assert snap["unknown_unreconciled_symbols"] == ["BTCUSDT"]
    assert snap["orphan_orders"] is snap["orphan_positions"] is None


def test_http_count_clean_empty_and_resets_each_snapshot():
    provider, _ = _provider([])
    first = provider.snapshot(); second = provider.snapshot()
    assert first["http_request_count"] == second["http_request_count"] == 2
    assert [a["sequence"] for a in second["request_attempts"]] == [1, 2]


def test_http_count_tracked_one_and_two_symbols():
    one, _ = _provider([], tracked={"BTCUSDT"})
    two, _ = _provider([], tracked={"BTCUSDT", "ETHUSDT"})
    assert one.snapshot()["http_request_count"] == 3
    snap = two.snapshot()
    assert snap["http_request_count"] == 4
    assert [a["symbol"] for a in snap["request_attempts"] if a["endpoint_class"] == "userTrades"] == ["BTCUSDT", "ETHUSDT"]


@pytest.mark.parametrize("failure", [TimeoutError("timeout"), error.HTTPError("safe", 429, "rate", {}, io.BytesIO(b'{"code":-1003}')), error.HTTPError("safe", 503, "server", {}, io.BytesIO(b'{"code":-1000}'))])
def test_http_count_transient_retry_then_success(failure):
    calls = 0
    def http(url, headers, timeout):
        nonlocal calls
        calls += 1
        if calls == 1: raise failure
        return []
    provider = BinanceReadonlyReconciliationProvider(config=BinanceReadonlyReconciliationConfig(
        base_url="https://demo-fapi.binance.com", api_key="k", api_secret="s", transport_retries=1), http_get_json=http)
    snap = provider.snapshot()
    assert snap["evidence_status"] == "COMPLETE" and snap["http_request_count"] == 3
    assert [a["outcome"] for a in snap["request_attempts"]] == ["RETRY", "PASS", "PASS"]


def test_http_count_auth_and_user_trade_failure_are_exact():
    auth = BinanceReadonlyReconciliationProvider(config=BinanceReadonlyReconciliationConfig(
        base_url="https://demo-fapi.binance.com", api_key="k", api_secret="s"),
        http_get_json=lambda url, headers, timeout: (_ for _ in ()).throw(error.HTTPError("safe", 401, "auth", {}, io.BytesIO(b'{"code":-2015}'))))
    assert auth.snapshot()["http_request_count"] == 1
    def http(url, headers, timeout):
        if "positionRisk" in url or "openOrders" in url: return []
        raise TimeoutError("fill")
    fills = BinanceReadonlyReconciliationProvider(config=BinanceReadonlyReconciliationConfig(
        base_url="https://demo-fapi.binance.com", api_key="k", api_secret="s", transport_retries=0),
        tracked_symbols=lambda:{"BTCUSDT"}, http_get_json=http)
    snap = fills.snapshot()
    assert snap["http_request_count"] == 3 and snap["failed_endpoint"] == "userTrades"


def test_http_attempt_evidence_is_secret_free():
    provider, _ = _provider([], tracked={"BTCUSDT"})
    blob = json.dumps(provider.snapshot()["request_attempts"])
    assert "signature=" not in blob and "api_key" not in blob.lower() and "secret" not in blob.lower()
    assert "?" not in blob and "timestamp" not in blob.lower()
