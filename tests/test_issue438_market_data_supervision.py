from __future__ import annotations

import asyncio

from alphaforge.exchange_market_scanner import MarketScanRows
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


def _runtime_with_scans(scans: list[MarketScanRows]) -> RuntimeOrchestrator:
    pending = list(scans)

    async def scanner() -> MarketScanRows:
        return pending.pop(0)

    return RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=object(),
        market_scanner=scanner,
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
    )


def _unavailable(cause: str = "TIMEOUT") -> MarketScanRows:
    return MarketScanRows([], diagnostics={
        "status": "UNAVAILABLE",
        "provider": "binance",
        "cause": cause,
        "endpoint": "premiumIndex",
        "error_class": "TimeoutError",
        "http_status": None,
    })


def test_public_market_data_failure_is_not_reported_as_normal_empty_scan() -> None:
    runtime = _runtime_with_scans([_unavailable()])

    asyncio.run(runtime._scan_once())

    assert runtime.metrics.executions == 0
    assert runtime._market_data_health_status == "DEGRADED"
    assert runtime._market_data_failure_streak == 1
    assert runtime._last_scan_gate_blockers == ["MARKET_DATA_UNAVAILABLE"]
    snapshot = runtime._build_runtime_state_snapshot(status="OPERATING")
    assert "MARKET_DATA_DEGRADED" in snapshot.runtime_flags
    assert snapshot.diagnostics_json["market_data"]["health_status"] == "DEGRADED"
    assert snapshot.diagnostics_json["market_data"]["cause"] == "TIMEOUT"


def test_sustained_public_market_data_failure_is_explicitly_unavailable() -> None:
    runtime = _runtime_with_scans([_unavailable("HTTP_503"), _unavailable("HTTP_503")])

    asyncio.run(runtime._scan_once())
    asyncio.run(runtime._scan_once())

    assert runtime.metrics.executions == 0
    assert runtime._market_data_health_status == "UNAVAILABLE"
    assert runtime._market_data_failure_streak == 2
    assert runtime._last_scan_gate_blockers == ["MARKET_DATA_UNAVAILABLE"]
    snapshot = runtime._build_runtime_state_snapshot(status="OPERATING")
    assert "MARKET_DATA_UNAVAILABLE" in snapshot.runtime_flags
    market_data = snapshot.diagnostics_json["market_data"]
    assert market_data["health_status"] == "UNAVAILABLE"
    assert market_data["failure_streak"] == 2
    assert market_data["provider"] == "binance"


def test_valid_empty_scan_clears_provider_failure_supervision_state() -> None:
    runtime = _runtime_with_scans([
        _unavailable(),
        MarketScanRows([], diagnostics={
            "status": "VALID_EMPTY",
            "provider": "binance",
            "cause": "NO_CANDIDATES_AFTER_FILTERING",
        }),
    ])

    asyncio.run(runtime._scan_once())
    asyncio.run(runtime._scan_once())

    assert runtime._market_data_health_status == "VALID_EMPTY"
    assert runtime._market_data_failure_streak == 0
    assert runtime._last_scan_gate_blockers == ["NO_MARKET_CANDIDATES"]
    snapshot = runtime._build_runtime_state_snapshot(status="OPERATING")
    assert "MARKET_DATA_DEGRADED" not in snapshot.runtime_flags
    assert "MARKET_DATA_UNAVAILABLE" not in snapshot.runtime_flags
    assert snapshot.diagnostics_json["market_data"]["health_status"] == "VALID_EMPTY"
