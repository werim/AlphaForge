from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import text

from alphaforge.burnin_campaign import (
    create_campaign,
    reject_candidate_feasibility_shadow,
    start_or_resume_campaign,
)
from alphaforge.persistence import init_db
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


def _runtime(tmp_path, *, candle_provider=None, **config_overrides):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'issue374.db'}")
    config = RuntimeConfig(
        execution_mode=ExecutionMode.PAPER,
        phase7_burnin_release_id="issue374",
        reject_forward_horizon_bars=1,
        min_signal_score=0.50,
        min_rr=1.20,
        min_effective_rr=1.10,
        min_sl_pct=0.15,
        max_sl_pct=1.50,
        **config_overrides,
    )
    runtime = RuntimeOrchestrator(
        config=config,
        ai_brain=object(),
        market_scanner=lambda: None,
        persistence_engine=engine,
        scanner_source="PAPER_RUNTIME",
        reject_candle_provider=candle_provider,
        paper_slippage_bps=2.0,
    )
    runtime._start_or_resume_burnin_run()
    assert runtime._burnin_run_id
    return engine, runtime


def _execution_ctx():
    return {
        "spread_pct": 0.0002,
        "expected_slippage_pct": 0.0002,
        "fee_pct": 0.0004,
        "funding_rate_pct": 0.0,
        "latency_ms": 50.0,
        "liquidity_score": 1.0,
        "volatility_regime": "normal",
    }


def test_primary_reason_is_preserved_while_all_failed_gates_are_queryable(tmp_path):
    _engine, runtime = _runtime(tmp_path)
    payload = runtime._canonical_reject_payload({
        "signal_id": "multi-gate",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": 100.0,
        "sl": 99.9,
        "tp": 102.0,
        "rr": 1.30,
        "candidate_rr": 1.30,
        "effective_rr": 0.90,
        "score": 0.40,
        "reason": "LOW_SCORE",
        "geometry_status": "COMPLETE",
        "execution_ctx": _execution_ctx(),
    })

    assert payload["primary_reject_reason"] == "LOW_SCORE"
    assert {"LOW_SCORE", "LOW_EFFECTIVE_RR", "STOP_TOO_TIGHT"}.issubset(
        set(payload["all_failed_gates"])
    )
    by_gate = {row["gate"]: row for row in payload["failed_gate_evidence"]}
    assert by_gate["LOW_SCORE"]["observed"] == pytest.approx(0.40)
    assert by_gate["LOW_SCORE"]["threshold"] == pytest.approx(0.50)
    assert by_gate["LOW_EFFECTIVE_RR"]["observed"] == pytest.approx(0.90)
    assert by_gate["LOW_EFFECTIVE_RR"]["threshold"] == pytest.approx(1.10)
    assert by_gate["STOP_TOO_TIGHT"]["observed"] == pytest.approx(0.10)
    assert by_gate["STOP_TOO_TIGHT"]["threshold"] == pytest.approx(0.15)


def test_execution_aligned_reject_uses_expected_fill_and_does_not_double_count_entry_slippage(tmp_path):
    def candles(symbol, start, end, timeframe):
        assert symbol == "BTCUSDT"
        assert timeframe == "1m"
        return [{
            "timestamp": "2026-01-01T00:01:00Z",
            "high": 102.1,
            "low": 99.5,
            "source_provenance": "ISSUE374_FIXTURE",
        }]

    engine, runtime = _runtime(tmp_path, candle_provider=candles)
    ctx = _execution_ctx()
    market = {
        "signal_id": "aligned-reject",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": 100.0,
        "sl": 99.0,
        "tp": 102.0,
        "rr": 2.0,
        "timeframe": "1m",
        "decision_timestamp": "2026-01-01T00:00:00Z",
        "geometry_status": "COMPLETE",
        "score": 0.40,
        "reason": "LOW_SCORE",
        "execution_ctx": ctx,
    }
    rr_metrics = runtime._execution_rr_metrics(2.0, market, ctx)
    source = {**market, **rr_metrics}

    asyncio.run(runtime._persist_reject(source))

    with engine.connect() as conn:
        pending = conn.execute(text("""
            SELECT entry, stop, target, execution_cost_assumptions_json, source_provenance_json
            FROM burnin_pending_reject_labels
            WHERE signal_id='aligned-reject'
        """)).mappings().one()
        observation = conn.execute(text("""
            SELECT metrics_json FROM burnin_observations
            WHERE burnin_run_id=:run_id AND decision='REJECTED'
              AND json_extract(metrics_json,'$.signal_id')='aligned-reject'
        """), {"run_id": runtime._burnin_run_id}).scalar_one()

    costs = json.loads(pending["execution_cost_assumptions_json"])
    provenance = json.loads(pending["source_provenance_json"])
    metrics = json.loads(observation)
    expected_fill = rr_metrics["expected_fill"]

    assert pending["entry"] == pytest.approx(expected_fill)
    assert provenance["planned_entry"] == pytest.approx(100.0)
    assert provenance["executable_entry"] == pytest.approx(expected_fill)
    assert provenance["reject_execution_basis"] == "EXPECTED_FILL_RUNTIME_PARITY"
    assert provenance["entry_slippage_embedded_in_fill"] is True
    assert provenance["embedded_entry_slippage_cost"] > 0
    assert costs["entry_slippage_cost"] == pytest.approx(0.0)
    assert provenance["candidate_raw_rr"] == pytest.approx(2.0)
    assert provenance["executable_raw_rr"] == pytest.approx(rr_metrics["executable_raw_rr"])
    assert provenance["remaining_execution_penalty"] == pytest.approx(
        rr_metrics["remaining_execution_penalty"]
    )
    assert provenance["effective_rr_at_decision"] == pytest.approx(rr_metrics["effective_rr"])
    assert "LOW_SCORE" in metrics["all_failed_gates"]

    result = asyncio.run(runtime._resolve_reject_forward_outcomes_once())
    assert result["resolved"] == 1

    with engine.connect() as conn:
        outcome = conn.execute(text("""
            SELECT hypothetical_entry,hypothetical_gross_r,hypothetical_net_r_after_costs,payload_json
            FROM burnin_reject_outcomes
            WHERE symbol='BTCUSDT'
        """)).mappings().one()
        expectancy = conn.execute(text("""
            SELECT evidence_complete,net_r
            FROM expectancy_evidence
            WHERE evidence_type='REJECT_FORWARD'
        """)).mappings().one()

    payload = json.loads(outcome["payload_json"])
    expected_gross_r = (102.0 - expected_fill) / (expected_fill - 99.0)
    expected_cost = sum(float(costs[key]) for key in (
        "spread_cost", "entry_slippage_cost", "exit_slippage_cost",
        "fee_cost", "funding_cost", "latency_cost",
    ))

    assert outcome["hypothetical_entry"] == pytest.approx(expected_fill)
    assert outcome["hypothetical_gross_r"] == pytest.approx(expected_gross_r)
    assert outcome["hypothetical_net_r_after_costs"] == pytest.approx(
        expected_gross_r - expected_cost
    )
    assert payload["execution_aligned"] is True
    assert payload["reject_execution_basis"] == "EXPECTED_FILL_RUNTIME_PARITY"
    assert payload["entry_slippage_embedded_in_fill"] is True
    assert expectancy["evidence_complete"] == 1
    assert expectancy["net_r"] == pytest.approx(outcome["hypothetical_net_r_after_costs"])


def test_fresh_reject_shadow_diagnostics_are_non_authoritative_and_execution_aligned(tmp_path, monkeypatch):
    def candles(_symbol, _start, _end, _timeframe):
        return [{"timestamp": "2026-01-01T00:01:00Z", "high": 102.1, "low": 99.5}]

    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'issue374-shadow.db'}")
    with engine.begin() as conn:
        campaign = create_campaign(
            conn,
            release_id="issue374-shadow",
            duration_days=1,
            symbols=["BTCUSDT"],
            intervals=["1m"],
        )
        run = start_or_resume_campaign(conn, campaign.campaign_id)
    monkeypatch.setenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID", campaign.campaign_id)
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=ExecutionMode.PAPER,
            phase7_burnin_release_id="issue374-shadow",
            reject_forward_horizon_bars=1,
            min_signal_score=0.50,
            min_rr=1.20,
            min_effective_rr=1.10,
            min_sl_pct=0.15,
            max_sl_pct=1.50,
        ),
        ai_brain=object(),
        market_scanner=lambda: None,
        persistence_engine=engine,
        scanner_source="PAPER_RUNTIME",
        reject_candle_provider=candles,
        paper_slippage_bps=2.0,
    )
    runtime._burnin_run_id = run["burnin_run_id"]
    ctx = _execution_ctx()
    market = {
        "signal_id": "shadow-diagnostic",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": 100.0,
        "sl": 99.0,
        "tp": 102.0,
        "rr": 2.0,
        "timeframe": "1m",
        "decision_timestamp": "2026-01-01T00:00:00Z",
        "geometry_status": "COMPLETE",
        "score": 0.40,
        "reason": "LOW_SCORE",
        "execution_ctx": ctx,
    }
    asyncio.run(runtime._persist_reject({
        **market,
        **runtime._execution_rr_metrics(2.0, market, ctx),
    }))
    asyncio.run(runtime._resolve_reject_forward_outcomes_once())

    with engine.connect() as conn:
        diagnostics = reject_candidate_feasibility_shadow(conn, campaign.campaign_id)

    assert diagnostics["authoritative"] is False
    assert diagnostics["purpose"] == "SHADOW_DIAGNOSTIC_ONLY"
    assert diagnostics["execution_basis"] == "EXPECTED_FILL_RUNTIME_PARITY"
    assert diagnostics["sample_count"] == 1
    assert diagnostics["candidate_raw_rr"]["count"] == 1
    assert diagnostics["executable_raw_rr"]["count"] == 1
    assert diagnostics["effective_rr"]["count"] == 1
    assert diagnostics["fill_shift_initial_risk_ratio"]["count"] == 1
    assert diagnostics["score_x_effective_rr_matrix"]
    assert any("LOW_SCORE" in key for key in diagnostics["failed_gate_combinations"])
