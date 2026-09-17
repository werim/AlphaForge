from __future__ import annotations

import asyncio
import sqlite3
import time
from types import SimpleNamespace

import pytest

from alphaforge.burnin_campaign import create_campaign, start_or_resume_campaign
from alphaforge.burnin_resolver import persist_pending_position, resolve_position_closure
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


class _AlwaysAcceptBrain:
    def before_real_order(self, signal_payload, market_ctx, regime_ctx, stats_ctx):
        class _Plan:
            decision = "ACCEPTED"
            reason = ""
            confidence = 0.9
            order_type = "MARKET"
            limit_price = None
            stop_price = None

        return SimpleNamespace(total_score=9.0, components={}), _Plan(), "ok"


def _runtime() -> RuntimeOrchestrator:
    return RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=ExecutionMode.PAPER,
            min_effective_rr=1.1,
            paper_fee_bps=0.0,
            paper_execution_latency_ms=0.0,
        ),
        ai_brain=_AlwaysAcceptBrain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        paper_slippage_bps=2.0,
    )


def _execution_ctx() -> dict[str, object]:
    return {
        "spread_pct": 0.0,
        "expected_slippage_pct": 0.0002,
        "latency_ms": 0.0,
        "fee_pct": 0.0,
        "funding_rate_pct": 0.0,
        "liquidity_score": 1.0,
        "volatility_regime": "normal",
        "volatility_status": "MODEL_ESTIMATE",
        "volatility_source": "TEST_EXECUTION_ASSUMPTION",
    }


@pytest.mark.parametrize(
    ("side", "stop", "target"),
    [("LONG", 99.9, 100.12), ("SHORT", 100.1, 99.88)],
)
def test_two_bps_fill_collapses_tight_stop_rr_symmetrically(side, stop, target):
    metrics = _runtime()._execution_rr_metrics(
        1.2,
        {"entry": 100.0, "sl": stop, "tp": target, "side": side},
        _execution_ctx(),
    )

    assert metrics["candidate_rr"] == pytest.approx(1.2)
    assert metrics["executable_raw_rr"] == pytest.approx(5.0 / 6.0, rel=1e-6)
    assert metrics["effective_rr"] < 1.1


@pytest.mark.parametrize(
    ("side", "stop", "target"),
    [("LONG", 99.0, 101.2), ("SHORT", 101.0, 98.8)],
)
def test_two_bps_fill_preserves_normal_width_rr_symmetrically(side, stop, target):
    metrics = _runtime()._execution_rr_metrics(
        1.2,
        {"entry": 100.0, "sl": stop, "tp": target, "side": side},
        _execution_ctx(),
    )

    assert metrics["executable_raw_rr"] == pytest.approx(1.18 / 1.02, rel=1e-6)
    assert metrics["effective_rr"] >= 1.1


def test_observed_eth_long_tp_geometry_reproduces_low_gross_r():
    runtime = _runtime()
    metrics = runtime._execution_rr_metrics(
        1.2015,
        {
            "entry": 2414.07,
            "sl": 2413.52,
            "tp": 2414.730838,
            "side": "LONG",
        },
        _execution_ctx(),
    )

    assert metrics["expected_fill"] == pytest.approx(2414.552814)
    assert metrics["executable_raw_rr"] == pytest.approx(0.1724, abs=5e-5)
    assert metrics["effective_rr"] < 1.1


def test_paper_final_gate_rejects_tight_stop_from_executable_geometry():
    rejects: list[dict] = []
    market = {
        "entry": 100.0,
        "sl": 99.9,
        "tp": 100.12,
        "rr": 1.2,
        "side": "LONG",
        "market_ts": time.time(),
        "volume_24h_usdt": 90_000_000.0,
        "spread_pct": 0.0,
        "expected_slippage_pct": 0.0002,
        "latency_ms": 0.0,
        "funding_rate_pct": 0.0,
        "liquidity_score": 1.0,
        "volatility_regime": "normal",
        "volatility_status": "MODEL_ESTIMATE",
        "volatility_source": "TEST_EXECUTION_ASSUMPTION",
    }
    runtime = _runtime()
    runtime.on_reject_persist = lambda payload: rejects.append(payload)

    asyncio.run(runtime._process_symbol(SimpleNamespace(
        symbol="ETHUSDT", regime_hint="TREND", diagnostics={"inputs": market},
    )))

    assert runtime.metrics.executions == 0
    assert rejects[-1]["reason"] == "LOW_EFFECTIVE_RR"
    assert rejects[-1]["candidate_rr"] == pytest.approx(1.2)
    assert rejects[-1]["executable_raw_rr"] == pytest.approx(5.0 / 6.0, rel=1e-6)


def test_paper_final_gate_accepts_normal_width_fill_geometry():
    runtime = _runtime()
    market = {
        "entry": 100.0,
        "sl": 99.0,
        "tp": 101.2,
        "rr": 1.2,
        "side": "LONG",
        "market_ts": time.time(),
        "volume_24h_usdt": 90_000_000.0,
        "spread_pct": 0.0,
        "expected_slippage_pct": 0.0002,
        "latency_ms": 0.0,
        "funding_rate_pct": 0.0,
        "liquidity_score": 1.0,
        "volatility_regime": "normal",
        "volatility_status": "MODEL_ESTIMATE",
        "volatility_source": "TEST_EXECUTION_ASSUMPTION",
    }

    asyncio.run(runtime._process_symbol(SimpleNamespace(
        symbol="BTCUSDT", regime_hint="TREND", diagnostics={"inputs": market},
    )))

    assert runtime.metrics.executions == 1
    assert runtime._active_positions["BTCUSDT"] == pytest.approx(10.0)


def test_runtime_rejects_same_direction_btc_eth_when_correlation_limit_is_one():
    rejects: list[dict] = []
    runtime = _runtime()
    runtime.config.max_correlated_positions = 1
    runtime._active_positions["BTCUSDT"] = 10.0
    runtime._active_position_sides["BTCUSDT"] = "LONG"
    runtime.on_reject_persist = lambda payload: rejects.append(payload)
    market = {
        "entry": 100.0, "sl": 99.0, "tp": 101.2, "rr": 1.2,
        "side": "LONG", "market_ts": time.time(),
        "volume_24h_usdt": 90_000_000.0, "spread_pct": 0.0,
        "expected_slippage_pct": 0.0002, "latency_ms": 0.0,
        "funding_rate_pct": 0.0, "liquidity_score": 1.0,
        "volatility_regime": "normal", "volatility_status": "MODEL_ESTIMATE",
        "volatility_source": "TEST_EXECUTION_ASSUMPTION",
    }

    asyncio.run(runtime._process_symbol(SimpleNamespace(
        symbol="ETHUSDT", regime_hint="TREND", diagnostics={"inputs": market},
    )))

    assert runtime.metrics.executions == 0
    assert rejects[-1]["reason"] == "CORRELATION_OVEREXPOSURE"
    snapshot = rejects[-1]["portfolio_diagnostics"]["snapshot"]
    assert snapshot["correlation_group"] == "CRYPTO_MAJOR"
    assert snapshot["correlated_position_count"] == 1


@pytest.mark.parametrize(
    ("side", "fill", "stop", "target"),
    [
        ("LONG", 100.02, 99.9, 100.12),
        ("SHORT", 99.98, 100.1, 99.88),
    ],
)
def test_tp_gross_r_matches_fill_adjusted_geometry(tmp_path, side, fill, stop, target):
    conn = sqlite3.connect(tmp_path / f"{side.lower()}.db")
    conn.row_factory = sqlite3.Row
    campaign = create_campaign(
        conn, release_id=f"rr-{side.lower()}", duration_days=1,
        symbols=["ETHUSDT"], intervals=["1m"],
    )
    run = start_or_resume_campaign(conn, campaign.campaign_id)
    persist_pending_position(
        conn,
        trade_id=f"trade-{side.lower()}", campaign_id=campaign.campaign_id,
        burnin_run_id=run["burnin_run_id"], signal_id="signal",
        symbol="ETHUSDT", side=side, entry_time="2026-01-01T00:00:00Z",
        planned_entry=100.0, simulated_fill=fill, stop=stop, target=target,
        quantity=1.0, notional=fill, entry_spread=0.0,
        entry_slippage=0.0, entry_fee=0.0, regime="TREND",
        source_provenance={"effective_rr_at_entry": 5.0 / 6.0},
    )
    resolve_position_closure(
        conn, trade_id=f"trade-{side.lower()}",
        exit_time="2026-01-01T00:01:00Z", exit_price=target,
        exit_reason="TP_HIT",
        exit_costs={
            "exit_spread": 0.0, "exit_slippage": 0.0, "exit_fee": 0.0,
            "funding": 0.0, "latency_impact_penalty": 0.0,
        },
    )
    row = conn.execute(
        "SELECT gross_r, exit_reason "
        "FROM burnin_trade_outcomes WHERE trade_id=?",
        (f"trade-{side.lower()}",),
    ).fetchone()

    assert row["exit_reason"] == "TP_HIT"
    assert row["gross_r"] == pytest.approx(5.0 / 6.0, rel=1e-6)
