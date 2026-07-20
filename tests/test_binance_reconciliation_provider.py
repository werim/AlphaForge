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
