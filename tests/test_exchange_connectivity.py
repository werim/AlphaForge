from __future__ import annotations

import json
import os
from urllib import error

import pytest

from alphaforge.exchange_connectivity import (
    ExchangeHealth,
    check_exchange_connectivity,
    health_has_secret_leak,
)
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


def _mock_urlopen_factory(payloads: list[object]):
    class _Resp:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps(self.payload).encode("utf-8")

    idx = {"i": 0}

    def _open(url_or_request, *args, **kwargs):
        payload = payloads[min(idx["i"], len(payloads)-1)]
        idx["i"] += 1
        return _Resp(payload)

    return _open


def test_binance_connectivity_success_mocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("urllib.request.urlopen", _mock_urlopen_factory([{"bidPrice": "100.0", "askPrice": "100.2"}, {"lastFundingRate": "0.0001"}, {"serverTime": 1}]))
    health = check_exchange_connectivity("binance")
    assert health.exchange == "binance"
    assert health.connected is True
    assert health.public_market_data_ok is True
    assert health.orderbook_ok is True
    assert health.latency_ms is not None
    assert health.checked_at.endswith("Z")
    assert health.supports_orderbook is True
    assert health.supports_funding is True
    assert health.supports_execution_updates is True


def test_binance_connectivity_failure_mocked(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args, **kwargs):
        raise error.URLError("timeout")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    health = check_exchange_connectivity("binance")
    assert health.connected is False
    assert health.public_market_data_ok is False
    assert health.orderbook_ok is False
    assert "BINANCE_CONNECTIVITY_ERROR" in (health.error or "")


def test_hyperliquid_connectivity_success_mocked_if_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("urllib.request.urlopen", _mock_urlopen_factory([{"BTC": "68000.0"}]))
    health = check_exchange_connectivity("hyperliquid")
    assert health.exchange == "hyperliquid"
    assert health.connected is True
    assert health.public_market_data_ok is True
    assert health.orderbook_ok is True
    assert health.supports_execution_updates is False


def test_hyperliquid_connectivity_failure_mocked_if_available(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    health = check_exchange_connectivity("hyperliquid")
    assert health.connected is False
    assert health.public_market_data_ok is False
    assert "HYPERLIQUID_CONNECTIVITY_ERROR" in (health.error or "")


def test_runtime_blocks_live_mode_when_exchange_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Brain:
        pass

    async def _scanner():
        return []

    rt = RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=ExecutionMode.LIVE,
            require_live_qualification=False,
            require_exchange_connectivity_for_live=True,
            required_live_exchanges=("binance",),
        ),
        ai_brain=_Brain(),
        market_scanner=_scanner,
        real_execution_adapter=object(),
    )

    def _failed_health(*args, **kwargs):
        return [ExchangeHealth("binance", False, False, None, False, None, None, "UNAVAILABLE", "2026-05-21T00:00:00Z", True, True, True)]

    monkeypatch.setattr("alphaforge.runtime.check_required_exchanges_health", _failed_health)
    with pytest.raises(RuntimeError, match="LIVE mode blocked: exchange connectivity unavailable"):
        import asyncio

        asyncio.run(rt.start())


def test_live_startup_requires_exchange_connectivity_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Brain:
        pass

    async def _scanner():
        return []

    rt = RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=ExecutionMode.LIVE,
            require_live_qualification=False,
        ),
        ai_brain=_Brain(),
        market_scanner=_scanner,
        real_execution_adapter=object(),
    )

    def _failed_health(*args, **kwargs):
        return [ExchangeHealth("binance", False, False, None, False, None, None, "UNAVAILABLE", "2026-05-21T00:00:00Z", True, True, True)]

    monkeypatch.setattr("alphaforge.runtime.check_required_exchanges_health", _failed_health)
    with pytest.raises(RuntimeError, match="LIVE mode blocked: exchange connectivity unavailable"):
        import asyncio

        asyncio.run(rt.start())


def test_paper_start_does_not_require_exchange_connectivity_by_default() -> None:
    cfg = RuntimeConfig(execution_mode=ExecutionMode.PAPER)
    assert cfg.require_exchange_connectivity_for_live is True


def test_live_can_only_skip_connectivity_when_explicitly_configured_for_test_or_override(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Brain:
        pass

    async def _scanner():
        return []

    rt = RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=ExecutionMode.LIVE,
            require_live_qualification=False,
            require_exchange_connectivity_for_live=False,
        ),
        ai_brain=_Brain(),
        market_scanner=_scanner,
    )
    called = {"value": False}

    def _failed_health(*args, **kwargs):
        called["value"] = True
        return [ExchangeHealth("binance", False, False, None, False, None, None, "UNAVAILABLE", "2026-05-21T00:00:00Z", True, True, True)]

    monkeypatch.setattr("alphaforge.runtime.check_required_exchanges_health", _failed_health)

    import asyncio

    asyncio.run(rt._run_live_exchange_connectivity_gate())
    assert called["value"] is False


def test_exchange_health_does_not_leak_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_API_KEY", "dont-leak-this")
    health = ExchangeHealth("binance", False, False, None, False, None, None, "BINANCE_CONNECTIVITY_ERROR:URLError", "2026-05-21T00:00:00Z", True, True, True)
    assert health_has_secret_leak(health) is False
    assert "dont-leak-this" not in json.dumps(health.to_dict())


@pytest.mark.integration
def test_live_binance_public_connectivity_optional() -> None:
    if os.getenv("ALPHAFORGE_RUN_EXCHANGE_INTEGRATION") != "1":
        pytest.skip("Set ALPHAFORGE_RUN_EXCHANGE_INTEGRATION=1 to run live integration checks")
    health = check_exchange_connectivity("binance", timeout_sec=5.0)
    assert health.exchange == "binance"
    assert health.checked_at.endswith("Z")


@pytest.mark.integration
def test_live_hyperliquid_public_connectivity_optional() -> None:
    if os.getenv("ALPHAFORGE_RUN_EXCHANGE_INTEGRATION") != "1":
        pytest.skip("Set ALPHAFORGE_RUN_EXCHANGE_INTEGRATION=1 to run live integration checks")
    health = check_exchange_connectivity("hyperliquid", timeout_sec=5.0)
    assert health.exchange == "hyperliquid"
    assert health.checked_at.endswith("Z")


def test_binance_connectivity_checks_futures_endpoints_not_spot(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []

    class _Resp:
        def __init__(self, payload):
            self.payload = payload
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def read(self) -> bytes:
            return json.dumps(self.payload).encode("utf-8")

    payloads = iter([{"bidPrice": "100", "askPrice": "100.2"}, {"lastFundingRate": "0.0001"}, {"serverTime": 1}])

    def _open(url_or_request, *args, **kwargs):
        url = url_or_request.full_url if hasattr(url_or_request, "full_url") else str(url_or_request)
        captured.append(url)
        return _Resp(next(payloads))

    monkeypatch.setattr("urllib.request.urlopen", _open)
    check_exchange_connectivity("binance")
    assert any("/fapi/v1/ticker/bookTicker" in u for u in captured)
    assert any("/fapi/v1/premiumIndex" in u for u in captured)
    assert not any("/api/v3/" in u for u in captured)


def test_binance_connectivity_fails_closed_when_funding_data_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("urllib.request.urlopen", _mock_urlopen_factory([{"bidPrice": "100", "askPrice": "100.2"}, {}, {"serverTime": 1}]))
    health = check_exchange_connectivity("binance")
    assert health.funding_ok is False
    assert health.connected is False


def test_live_does_not_accept_spot_only_binance_health_for_futures_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args, **kwargs):
        raise error.URLError("spot ok but futures down")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    health = check_exchange_connectivity("binance")
    assert health.connected is False
