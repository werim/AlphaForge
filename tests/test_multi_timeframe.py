import asyncio
import time
from types import SimpleNamespace

import pytest

from alphaforge.execution import build_execution_context as build_canonical_execution_context
from alphaforge.multi_timeframe import BinanceMTFProvider, closed_candles, evaluate_mtf_alignment
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator

NOW = 10_000_000

def context(tf, direction, close=NOW, **extra):
    return {"timeframe": tf, "direction": direction, "last_closed_candle_ms": close,
            "evidence_status": "COMPLETE", **extra}

def aligned(direction="LONG"):
    return (context("1h", direction), context("15m", direction),
            context("1m", direction, trigger="CONFIRMED"))

def test_perfect_long_and_short_alignment():
    for direction in ("LONG", "SHORT"):
        result = evaluate_mtf_alignment(*aligned(direction), decision_ts_ms=NOW)
        assert result["aligned"] and result["direction"] == direction

def test_regime_setup_and_execution_mismatches_are_specific():
    r, s, e = aligned("SHORT")
    r["direction"] = "LONG"
    assert evaluate_mtf_alignment(r, s, e, decision_ts_ms=NOW)["reasons"] == ["MTF_REGIME_SETUP_MISMATCH"]
    r, s, e = aligned("LONG"); e["direction"] = "SHORT"
    assert evaluate_mtf_alignment(r, s, e, decision_ts_ms=NOW)["reasons"] == ["MTF_SETUP_EXECUTION_MISMATCH"]

def test_missing_layers_no_setup_and_stale_fail_closed():
    r, s, e = aligned()
    assert "MTF_REGIME_UNAVAILABLE" in evaluate_mtf_alignment(None, s, e, decision_ts_ms=NOW)["reasons"]
    assert "MTF_SETUP_UNAVAILABLE" in evaluate_mtf_alignment(r, None, e, decision_ts_ms=NOW)["reasons"]
    assert "MTF_EXECUTION_UNAVAILABLE" in evaluate_mtf_alignment(r, s, None, decision_ts_ms=NOW)["reasons"]
    s["direction"] = "NONE"
    assert "MTF_NO_VALID_SETUP" in evaluate_mtf_alignment(r, s, e, decision_ts_ms=NOW)["reasons"]
    r, s, e = aligned(); r["last_closed_candle_ms"] = NOW - 8_000_000
    assert "MTF_CONTEXT_STALE" in evaluate_mtf_alignment(r, s, e, decision_ts_ms=NOW)["reasons"]

def test_partial_and_future_candles_cannot_contaminate_decision():
    rows = [[0,"1","2",".5","1.5","10",9999], [5000,"1.5","99","1","98","10",15000]]
    selected = closed_candles(rows, timeframe="1h", decision_ts_ms=10_000)
    assert len(selected) == 1 and selected[0]["close"] == 1.5


def test_realistic_binance_candidate_uses_canonical_normalized_execution_context(monkeypatch):
    decision_ms = 4_000_000
    rows = []
    for index in range(30):
        close = 100.0 + index * 0.1
        rows.append([index * 60_000, str(close - .02), str(close + .03), str(close - .03),
                     str(close), "100", min(index * 60_000 + 59_999, decision_ms)])
    provider = BinanceMTFProvider()
    monkeypatch.setattr(provider, "_fetch", lambda _symbol, _timeframe: rows)
    raw_market = {"spread_pct": 0.0002, "market_data_latency_ms": 42.0,
                  "liquidity_score": 0.9, "volatility_regime": "MODERATE"}
    assert "expected_slippage_pct" not in raw_market and "latency_ms" not in raw_market
    canonical = build_canonical_execution_context(raw_market)
    assert canonical["expected_slippage_pct"] is not None and canonical["latency_ms"] == 42.0

    mtf = asyncio.run(provider.build("BTCUSDT", raw_market, execution_ctx=canonical,
        decision_ts_ms=decision_ms, regime_timeframe="1h", setup_timeframe="15m",
        execution_timeframe="1m"))

    assert mtf["execution"]["evidence_status"] == "COMPLETE"
    assert mtf["execution"]["expected_slippage_pct"] == canonical["expected_slippage_pct"]
    assert mtf["execution"]["latency_ms"] == 42.0
    assert "MTF_EXECUTION_UNAVAILABLE" not in mtf["alignment"]["reasons"]


class _AlignedProvider:
    def __init__(self, direction):
        self.direction = direction
        self.execution_ctx = None

    async def build(self, _symbol, _market, *, execution_ctx, **_kwargs):
        self.execution_ctx = execution_ctx
        return {"provider": "BINANCE_FUTURES_CLOSED_KLINES", "alignment": {
            "aligned": True, "direction": self.direction, "reasons": [],
            "timeframes": {"regime": "1h", "setup": "15m", "execution": "1m"}}}


@pytest.mark.parametrize(("mtf_side", "geometry_side", "expected_reason"), [
    ("LONG", "LONG", "SPREAD_TOO_HIGH"),
    ("SHORT", "SHORT", "SPREAD_TOO_HIGH"),
    ("LONG", "SHORT", "MTF_DIRECTION_MISMATCH"),
    ("SHORT", "LONG", "MTF_DIRECTION_MISMATCH"),
])
def test_runtime_binds_aligned_direction_to_geometry_and_persists_reject(
        mtf_side, geometry_side, expected_reason):
    provider = _AlignedProvider(mtf_side)
    rejects = []
    candidate = {"symbol": "BTCUSDT", "source_exchange": "binance", "side": geometry_side,
        "entry": 100.0, "sl": 99.0 if geometry_side == "LONG" else 101.0,
        "tp": 102.0 if geometry_side == "LONG" else 98.0, "rr": 2.0,
        "spread_pct": 0.02, "market_data_latency_ms": 31.0, "liquidity_score": .9,
        "volume_24h_usdt": 100_000_000, "market_ts": time.time()}
    selection = SimpleNamespace(symbol="BTCUSDT", regime_hint="FAVORABLE",
        diagnostics={"inputs": candidate})
    orchestrator = RuntimeOrchestrator(RuntimeConfig(execution_mode=ExecutionMode.PAPER,
        require_mtf_alignment=True, max_spread_pct=.001), SimpleNamespace(),
        lambda: asyncio.sleep(0, result=[]), mtf_context_provider=provider,
        on_reject_persist=lambda payload: rejects.append(payload))

    asyncio.run(orchestrator._process_symbol(selection))

    assert rejects and rejects[-1]["reason"] == expected_reason
    assert provider.execution_ctx["latency_ms"] == 31.0
    if expected_reason == "MTF_DIRECTION_MISMATCH":
        assert rejects[-1]["mtf"]["alignment"]["aligned"] is False


def test_non_binance_source_is_not_evaluated_with_binance_provider():
    provider = _AlignedProvider("LONG")
    rejects = []
    candidate = {"symbol": "BTCUSDT", "source_exchange": "hyperliquid", "side": "LONG",
        "spread_pct": .0001, "market_data_latency_ms": 20.0, "liquidity_score": .9,
        "market_ts": 4_000.0}
    selection = SimpleNamespace(symbol="BTCUSDT", regime_hint="FAVORABLE",
        diagnostics={"inputs": candidate})
    orchestrator = RuntimeOrchestrator(RuntimeConfig(execution_mode=ExecutionMode.PAPER,
        require_mtf_alignment=True), SimpleNamespace(), lambda: asyncio.sleep(0, result=[]),
        mtf_context_provider=provider, on_reject_persist=lambda payload: rejects.append(payload))

    asyncio.run(orchestrator._process_symbol(selection))

    assert provider.execution_ctx is None
    assert rejects[-1]["reason"] == "MTF_EXECUTION_UNAVAILABLE"
    assert rejects[-1]["mtf"]["provenance_error"] == "UNSUPPORTED_MTF_PROVIDER"
