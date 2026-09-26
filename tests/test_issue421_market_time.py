from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from alphaforge import runtime as runtime_module
from alphaforge.config import load_config_from_env
from alphaforge.runtime import (
    ExecutionMode,
    RuntimeConfig,
    RuntimeOrchestrator,
    _runtime_config_from_app_config,
)


class _ShouldNotRunBrain:
    def before_real_order(self, *args: Any, **kwargs: Any):
        raise AssertionError("market-time rejects must stop before AI/scoring")


def _runtime(*, max_clock_skew_ms: int = 5000, stale_market_data_sec: float = 15.0) -> RuntimeOrchestrator:
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=ExecutionMode.PAPER,
            max_clock_skew_ms=max_clock_skew_ms,
            stale_market_data_sec=stale_market_data_sec,
        ),
        ai_brain=_ShouldNotRunBrain(),
        market_scanner=None,
    )
    runtime._unknown_exchange_state = False
    return runtime


def _ctx(market_ts: Any) -> dict[str, Any]:
    return {
        "market_ts": market_ts,
        "volume_24h_usdt": 90_000_000.0,
    }


def test_runtime_clock_skew_comes_from_canonical_registry(tmp_path) -> None:
    cfg = load_config_from_env(
        env={"ALPHAFORGE_MAX_CLOCK_SKEW_MS": "2300"},
        root=tmp_path,
    )
    assert cfg.runtime.max_clock_skew_ms == 2300

    snapshot = _runtime_config_from_app_config(cfg, ExecutionMode.PAPER)
    assert snapshot.max_clock_skew_ms == 2300


def test_future_timestamp_within_allowed_clock_skew_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_module.time, "time", lambda: 1000.0)
    runtime = _runtime(max_clock_skew_ms=5000)

    market = _ctx(1004.999)
    assert runtime._evaluate_runtime_risk("BTCUSDT", market) is None
    evidence = market["market_time_evidence"]
    assert evidence["status"] == "PASS"
    assert evidence["configured_max_clock_skew_ms"] == 5000
    assert evidence["policy_source"] == "CONFIG_REGISTRY:ALPHAFORGE_MAX_CLOCK_SKEW_MS"


def test_future_timestamp_beyond_allowed_clock_skew_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_module.time, "time", lambda: 1000.0)
    runtime = _runtime(max_clock_skew_ms=5000)

    market = _ctx(1005.001)
    assert runtime._evaluate_runtime_risk("BTCUSDT", market) == "MARKET_TIMESTAMP_IN_FUTURE"
    evidence = market["market_time_evidence"]
    assert evidence["status"] == "REJECT"
    assert evidence["reason"] == "MARKET_TIMESTAMP_IN_FUTURE"
    assert evidence["reject_threshold"] == pytest.approx(1005.0)


def test_stale_market_timestamp_behavior_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_module.time, "time", lambda: 1000.0)
    runtime = _runtime(stale_market_data_sec=15.0)

    market = _ctx(984.999)
    assert runtime._evaluate_runtime_risk("BTCUSDT", market) == "STALE_MARKET_DATA"
    assert "BTCUSDT" in runtime._stale_market_data_symbols
    assert market["market_time_evidence"]["reason"] == "STALE_MARKET_DATA"


@pytest.mark.parametrize(
    "market_ts",
    [None, "", "not-a-timestamp", float("nan"), float("inf"), float("-inf"), True],
)
def test_non_finite_or_malformed_market_timestamp_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    market_ts: Any,
) -> None:
    monkeypatch.setattr(runtime_module.time, "time", lambda: 1000.0)
    runtime = _runtime()

    market = _ctx(market_ts)
    assert runtime._evaluate_runtime_risk("BTCUSDT", market) == "INVALID_MARKET_TIMESTAMP"
    assert market["market_time_evidence"]["reason"] == "INVALID_MARKET_TIMESTAMP"


def test_epoch_milliseconds_are_rejected_as_unit_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_module.time, "time", lambda: 1000.0)
    runtime = _runtime()

    market = _ctx(1_000_000.0)
    assert runtime._evaluate_runtime_risk("BTCUSDT", market) == "MARKET_TIMESTAMP_UNIT_MISMATCH"
    evidence = market["market_time_evidence"]
    assert evidence["detected_unit"] == "epoch_milliseconds"
    assert evidence["normalized_candidate_seconds"] == pytest.approx(1000.0)


def test_production_process_symbol_persists_future_timestamp_reason_and_threshold_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module.time, "time", lambda: 1000.0)
    rejects: list[dict[str, Any]] = []
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=ExecutionMode.PAPER,
            max_clock_skew_ms=5000,
        ),
        ai_brain=_ShouldNotRunBrain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        on_reject_persist=lambda payload: rejects.append(payload),
        paper_slippage_bps=2.0,
    )
    runtime._unknown_exchange_state = False

    market = {
        "symbol": "BTCUSDT",
        "source_exchange": "fixture",
        "timeframe": "1m",
        "entry": 100.0,
        "sl": 99.0,
        "tp": 103.0,
        "rr": 3.0,
        "side": "LONG",
        "market_ts": 1006.0,
        "volume_24h_usdt": 90_000_000.0,
        "spread_pct": 0.0002,
        "spread_status": "MEASURED",
        "expected_slippage_pct": 0.0002,
        "slippage_status": "MODEL_ESTIMATE",
        "liquidity_score": 0.90,
        "liquidity_status": "MEASURED",
        "funding_rate_pct": 0.00005,
        "funding_status": "MEASURED",
        "orderbook_imbalance": 0.10,
        "orderbook_status": "MEASURED",
        "volatility_regime": "normal",
        "volatility_status": "MEASURED",
        "trend_strength": 0.90,
        "chop_score": 0.10,
    }
    selection = SimpleNamespace(
        symbol="BTCUSDT",
        diagnostics={"inputs": market},
    )

    asyncio.run(runtime._process_symbol(selection))

    assert runtime.metrics.executions == 0
    assert rejects
    reject = rejects[-1]
    assert reject["reason"] == "MARKET_TIMESTAMP_IN_FUTURE"
    assert "MARKET_TIMESTAMP_IN_FUTURE" in reject["all_failed_gates"]
    assert reject["market_time_evidence"]["configured_max_clock_skew_ms"] == 5000
    assert any(
        row["gate"] == "MARKET_TIMESTAMP_IN_FUTURE"
        and row["source"] == "MARKET_TIME_CONTRACT"
        and row["threshold"] == pytest.approx(1005.0)
        for row in reject["failed_gate_evidence"]
    )
