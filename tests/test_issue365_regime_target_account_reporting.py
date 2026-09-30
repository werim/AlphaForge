from __future__ import annotations

import asyncio
import sqlite3
import time
from types import SimpleNamespace

import pytest

from alphaforge.account_performance import build_account_performance_report
from alphaforge.burnin import bootstrap_burnin_schema
from alphaforge.regime_targeting import build_regime_target_policy
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


def test_trending_continuation_keeps_structural_target_and_only_permits_trailing():
    policy = build_regime_target_policy(
        side="LONG",
        entry=100.02,
        stop=99.0,
        target=103.0,
        effective_rr=2.4,
        min_effective_rr=1.1,
        regime="TRENDING",
        setup_phase="CONTINUATION",
        target_source="setup_window_resistance",
    )
    assert policy["status"] == "COMPLETE"
    assert policy["target"] == pytest.approx(103.0)
    assert policy["target_action"] == "KEEP_STRUCTURAL_TARGET"
    assert policy["trailing_allowed"] is True
    assert policy["trailing_distance"] is None
    assert policy["profit_amount_targeting_used"] is False
    assert policy["monthly_return_targeting_used"] is False


def test_pullback_or_choppy_never_widens_target():
    policy = build_regime_target_policy(
        side="SHORT",
        entry=100.0,
        stop=101.0,
        target=98.0,
        effective_rr=1.7,
        min_effective_rr=1.1,
        regime="CHOPPY",
        setup_phase="PULLBACK",
        target_source="setup_window_support",
    )
    assert policy["status"] == "COMPLETE"
    assert policy["target"] == pytest.approx(98.0)
    assert policy["realization_profile"] == "EARLY_REALIZATION_ELIGIBLE"
    assert policy["trailing_allowed"] is False


def test_target_policy_rejects_geometry_that_fails_execution_adjusted_rr():
    policy = build_regime_target_policy(
        side="LONG",
        entry=100.0,
        stop=99.0,
        target=102.0,
        effective_rr=1.05,
        min_effective_rr=1.1,
        regime="TRENDING",
        setup_phase="CONTINUATION",
    )
    assert policy["status"] == "REJECTED"
    assert policy["reason"] == "LOW_EFFECTIVE_RR_AFTER_COSTS"


def test_risk_off_regime_does_not_create_new_target_or_trailing_geometry():
    policy = build_regime_target_policy(
        side="LONG",
        entry=100.0,
        stop=99.0,
        target=102.0,
        effective_rr=1.5,
        min_effective_rr=1.1,
        regime="NEWS_DRIVEN",
        setup_phase="CONTINUATION",
    )
    assert policy["status"] == "COMPLETE"
    assert policy["new_risk_posture"] == "NO_NEW_RISK_RECOMMENDED"
    assert policy["target"] == pytest.approx(102.0)
    assert policy["trailing_allowed"] is False


def _insert_trade(
    conn: sqlite3.Connection,
    *,
    oid: str,
    closed_at: str,
    symbol: str,
    regime: str,
    net_r: float,
    net_pnl: float,
    equity: float | None,
    risk_usdt: float | None,
    risk_fraction: float | None,
    notional: float | None,
    phase: str,
    evidence_complete: int = 1,
):
    payload = {
        "notional": notional,
        "source_provenance": {
            "portfolio_equity": equity,
            "risk_at_stop_usdt": risk_usdt,
            "risk_at_stop_pct_equity": risk_fraction,
            "selected_notional": notional,
            "setup_phase": phase,
        },
    }
    conn.execute(
        """INSERT INTO burnin_trade_outcomes(
            outcome_id,burnin_run_id,release_id,symbol,regime,closed_at,
            net_r,net_pnl,evidence_complete,missing_cost_fields_json,payload_json,schema_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            oid,
            "run-365",
            "rel-365",
            symbol,
            regime,
            closed_at,
            net_r,
            net_pnl,
            evidence_complete,
            "[]",
            __import__("json").dumps(payload, sort_keys=True),
            "1",
        ),
    )


def test_account_report_exposes_risk_return_drawdown_expectancy_and_contribution():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_burnin_schema(conn)
    _insert_trade(
        conn,
        oid="t1",
        closed_at="2026-09-10T00:00:00Z",
        symbol="BTCUSDT",
        regime="TRENDING",
        net_r=1.0,
        net_pnl=10.0,
        equity=1000.0,
        risk_usdt=10.0,
        risk_fraction=0.01,
        notional=500.0,
        phase="CONTINUATION",
    )
    _insert_trade(
        conn,
        oid="t2",
        closed_at="2026-09-20T00:00:00Z",
        symbol="ETHUSDT",
        regime="TRENDING",
        net_r=-0.5,
        net_pnl=-5.0,
        equity=1010.0,
        risk_usdt=10.1,
        risk_fraction=0.01,
        notional=450.0,
        phase="PULLBACK",
    )
    report = build_account_performance_report(
        conn,
        burnin_run_id="run-365",
        as_of="2026-09-30T00:00:00Z",
        monthly_return_target_fraction=0.10,
    )
    assert report["status"] == "COMPLETE"
    assert report["trade_count"] == 2
    assert report["average_risk_usdt"] == pytest.approx(10.05)
    assert report["average_risk_fraction"] == pytest.approx(0.01)
    assert report["average_selected_notional"] == pytest.approx(475.0)
    assert report["rolling_monthly_return_fraction"] == pytest.approx(0.005)
    assert report["rolling_realized_drawdown_fraction"] == pytest.approx(5.0 / 1010.0)
    assert report["expectancy_after_costs_net_r"] == pytest.approx(0.25)
    assert report["return_drawdown_ratio"] == pytest.approx(0.005 / (5.0 / 1010.0))
    assert report["contribution"]["symbol"]["BTCUSDT"]["net_pnl"] == pytest.approx(10.0)
    assert report["contribution"]["setup_phase"]["PULLBACK"]["net_r"] == pytest.approx(-0.5)
    assert report["monthly_return_target_role"] == "REPORTING_ONLY"
    assert report["monthly_return_target_gap_fraction"] == pytest.approx(-0.095)


def test_monthly_target_changes_only_reporting_fields():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_burnin_schema(conn)
    _insert_trade(
        conn,
        oid="t1",
        closed_at="2026-09-20T00:00:00Z",
        symbol="BTCUSDT",
        regime="TRENDING",
        net_r=0.5,
        net_pnl=5.0,
        equity=1000.0,
        risk_usdt=10.0,
        risk_fraction=0.01,
        notional=500.0,
        phase="CONTINUATION",
    )
    base = build_account_performance_report(
        conn, burnin_run_id="run-365", as_of="2026-09-30T00:00:00Z"
    )
    target = build_account_performance_report(
        conn,
        burnin_run_id="run-365",
        as_of="2026-09-30T00:00:00Z",
        monthly_return_target_fraction=0.25,
    )
    for field in (
        "average_risk_usdt",
        "average_risk_fraction",
        "average_selected_notional",
        "rolling_monthly_return_fraction",
        "rolling_realized_drawdown_fraction",
        "expectancy_after_costs_net_r",
        "return_drawdown_ratio",
        "contribution",
    ):
        assert target[field] == base[field]
    assert base["monthly_return_target_fraction"] is None
    assert target["monthly_return_target_fraction"] == pytest.approx(0.25)


def test_missing_risk_provenance_is_incomplete_not_zero():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_burnin_schema(conn)
    _insert_trade(
        conn,
        oid="legacy",
        closed_at="2026-09-20T00:00:00Z",
        symbol="BTCUSDT",
        regime="TRENDING",
        net_r=0.5,
        net_pnl=5.0,
        equity=None,
        risk_usdt=None,
        risk_fraction=None,
        notional=None,
        phase="UNKNOWN",
    )
    report = build_account_performance_report(
        conn, burnin_run_id="run-365", as_of="2026-09-30T00:00:00Z"
    )
    assert report["status"] == "INCOMPLETE"
    assert "portfolio_equity" in report["missing_fields"]
    assert "risk_at_stop_usdt" in report["missing_fields"]
    assert report["average_risk_usdt"] is None
    assert report["rolling_monthly_return_fraction"] is None


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


def _market() -> dict:
    now = time.time()
    return {
        "signal_id": "target-365",
        "entry": 100.0,
        "sl": 99.0,
        "tp": 103.0,
        "rr": 3.0,
        "expectancy": 0.2,
        "side": "LONG",
        "setup_phase": "CONTINUATION",
        "regime": "TRENDING",
        "target_source": "setup_window_resistance",
        "equity": 1000.0,
        "available_balance": 1000.0,
        "market_ts": now,
        "volume_24h_usdt": 90_000_000.0,
        "spread_pct": 0.0002,
        "spread_status": "MEASURED",
        "spread_source": "TEST",
        "expected_slippage_pct": 0.0002,
        "slippage_status": "MEASURED",
        "slippage_source": "TEST",
        "latency_ms": 10.0,
        "latency_status": "MEASURED",
        "latency_source": "TEST",
        "market_data_latency_ms": 10.0,
        "market_data_latency_status": "MEASURED",
        "market_data_latency_source": "TEST",
        "funding_rate_pct": 0.00001,
        "funding_status": "MEASURED",
        "funding_source": "TEST",
        "liquidity_score": 1.0,
        "liquidity_status": "MEASURED",
        "liquidity_source": "TEST",
        "liquidity_depth_usdt": 1_000_000.0,
        "liquidity_depth_status": "MEASURED",
        "liquidity_depth_source": "TEST",
        "volatility_regime": "normal",
        "volatility_status": "MEASURED",
        "volatility_source": "TEST",
        "volatility_penalty_pct": 0.02,
    }


def test_runtime_persists_target_policy_without_changing_tp(monkeypatch):
    captured: list[dict] = []

    def capture(self, payload, **kwargs):
        captured.append(dict(payload))

    monkeypatch.setattr(RuntimeOrchestrator, "_persist_burnin_decision", capture)
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=ExecutionMode.PAPER,
            paper_position_sizing_mode="FIXED",
            paper_candidate_notional=10.0,
            paper_initial_equity=1000.0,
            paper_fee_bps=0.0,
            paper_execution_latency_ms=0.0,
            max_concurrent_positions=10,
            max_open_positions=10,
            max_notional_exposure=100000.0,
            max_symbol_notional=100000.0,
            max_correlation_group_exposure=100000.0,
            max_correlated_positions=10,
        ),
        ai_brain=_AlwaysAcceptBrain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        paper_slippage_bps=2.0,
    )
    selection = SimpleNamespace(
        symbol="BTCUSDT", regime_hint="TRENDING", diagnostics={"inputs": _market()}
    )
    asyncio.run(runtime._process_symbol(selection))
    accepted = [row for row in captured if row.get("decision") == "ACCEPTED"]
    assert accepted
    row = accepted[-1]
    assert row["tp"] == pytest.approx(103.0)
    assert row["target_policy"]["target"] == pytest.approx(103.0)
    assert row["target_policy"]["target_action"] == "KEEP_STRUCTURAL_TARGET"
    assert row["target_policy"]["profit_amount_targeting_used"] is False
