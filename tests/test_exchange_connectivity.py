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


def _mock_urlopen_factory(payload: dict[str, object]):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps(payload).encode("utf-8")

    def _open(*args, **kwargs):
        return _Resp()

    return _open


def test_binance_connectivity_success_mocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("urllib.request.urlopen", _mock_urlopen_factory({"bidPrice": "100.0", "askPrice": "100.2"}))
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
    monkeypatch.setattr("urllib.request.urlopen", _mock_urlopen_factory({"BTC": "68000.0"}))
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
    )

    def _failed_health(*args, **kwargs):
        return [ExchangeHealth("binance", False, False, None, False, None, None, "UNAVAILABLE", "2026-05-21T00:00:00Z", True, True, True)]

    monkeypatch.setattr("alphaforge.runtime.check_required_exchanges_health", _failed_health)
    with pytest.raises(RuntimeError, match="LIVE mode blocked: exchange connectivity unavailable"):
        import asyncio

        asyncio.run(rt.start())


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
