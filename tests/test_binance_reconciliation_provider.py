from __future__ import annotations

from urllib import error

import pytest

from alphaforge.binance_reconciliation_provider import (
    BinanceReadonlyReconciliationConfig,
    BinanceReadonlyReconciliationProvider,
)
from alphaforge.config import load_reconciliation_settings
from alphaforge.env_contract import DEMO_REST_URL
import alphaforge.binance_reconciliation_provider as provider_module


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


def test_demo_reconciliation_settings_do_not_require_websocket() -> None:
    settings = load_reconciliation_settings({
        "BINANCE_ENVIRONMENT": "demo",
        "BINANCE_BASE_URL": DEMO_REST_URL,
        "BINANCE_WS_URL": "",
        "BINANCE_API_KEY": "key",
        "BINANCE_API_SECRET": "secret",
    })
    assert settings.base_url == DEMO_REST_URL


def test_canonical_reconciliation_settings_preserve_all_safety_fields() -> None:
    settings = load_reconciliation_settings({
        "BINANCE_ENVIRONMENT": "demo",
        "BINANCE_BASE_URL": DEMO_REST_URL,
        "ALPHAFORGE_BINANCE_RECV_WINDOW_MS": "30000",
        "ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC": "7.5",
        "ALPHAFORGE_BINANCE_RECONCILIATION_TRADE_LOOKBACK_MS": "90000",
        "ALPHAFORGE_RECONCILIATION_POSITION_EPSILON": "0.000000001",
        "ALPHAFORGE_RECONCILIATION_MAX_FILL_SYMBOLS": "17",
    })
    assert settings.recv_window_ms == 30000
    assert settings.timeout_sec == 7.5
    assert settings.trade_lookback_ms == 90000
    assert str(settings.position_epsilon) == "1E-9"
    assert settings.max_fill_symbols == 17
    assert settings.provenance["recv_window_ms"] == "canonical"


def test_legacy_recv_window_compatibility_and_conflict_fail_closed() -> None:
    base = {"BINANCE_ENVIRONMENT": "production"}
    legacy = load_reconciliation_settings({**base, "BINANCE_RECV_WINDOW_MS": "7000"})
    assert legacy.recv_window_ms == 7000
    assert legacy.provenance["recv_window_ms"] == "deprecated_alias"
    with pytest.raises(ValueError, match="alias conflict"):
        load_reconciliation_settings({
            **base,
            "ALPHAFORGE_BINANCE_RECV_WINDOW_MS": "5000",
            "BINANCE_RECV_WINDOW_MS": "7000",
        })


def test_provider_module_does_not_own_environment_settings_loader() -> None:
    """Conflict resolution must not reintroduce a second parsing authority."""
    assert not hasattr(provider_module, "load_reconciliation_settings")
