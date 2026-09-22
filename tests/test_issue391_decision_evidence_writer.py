from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from alphaforge.order import LifecycleState
from alphaforge.persistence import init_db, save_decision_evidence
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


def _orchestrator(tmp_path: Path, mode: ExecutionMode, *, run_id: str):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / (mode.value.lower() + '-391.db')}")
    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=mode),
        ai_brain=object(),
        market_scanner=lambda: None,
        persistence_engine=engine,
    )
    orchestrator._burnin_run_id = run_id
    return engine, orchestrator


def test_paper_accept_writes_one_execution_aligned_decision_evidence_row(tmp_path: Path) -> None:
    engine, orchestrator = _orchestrator(tmp_path, ExecutionMode.PAPER, run_id="run-391-accept")
    payload = {
        "signal_id": "sig-391-accept", "setup_identity": "setup-391-accept",
        "decision": "ACCEPTED", "decision_time": "2026-09-22T15:00:00Z",
        "symbol": "BTCUSDT", "side": "LONG", "score": 0.73, "rr": 2.0,
        "candidate_rr": 2.0, "expected_fill": 100.1, "executable_raw_rr": 1.88,
        "remaining_execution_penalty": 0.08, "effective_rr": 1.80,
        "entry": 100.0, "sl": 95.0, "tp": 110.0,
        "execution_ctx": {
            "spread_pct": 0.0001, "expected_slippage_pct": 0.0002,
            "funding_rate_pct": 0.0, "market_data_latency_ms": 25.0,
            "liquidity_score": 0.91, "volatility_regime": "NORMAL",
        },
    }
    orchestrator._persist_burnin_decision(payload, lifecycle_state=LifecycleState.ORDER_PLACED.value)
    orchestrator._persist_burnin_decision(payload, lifecycle_state=LifecycleState.POSITION_OPENED.value)
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT run_id,mode,decision,signal_id,raw_rr,effective_rr,min_effective_rr,
                   entry,sl,tp,spread_pct,expected_slippage_pct,lifecycle_state_after,diagnostics_json
            FROM decision_evidence WHERE signal_id='sig-391-accept'
        """)).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == "run-391-accept"
    assert row["mode"] == "PAPER"
    assert row["decision"] == "ACCEPT"
    assert row["raw_rr"] == pytest.approx(1.88)
    assert row["effective_rr"] == pytest.approx(1.80)
    assert row["min_effective_rr"] == pytest.approx(orchestrator.config.min_effective_rr)
    assert row["entry"] == pytest.approx(100.1)
    assert row["sl"] == pytest.approx(95.0)
    assert row["tp"] == pytest.approx(110.0)
    assert row["spread_pct"] == pytest.approx(0.0001)
    assert row["expected_slippage_pct"] == pytest.approx(0.0002)
    assert row["lifecycle_state_after"] == LifecycleState.POSITION_OPENED.value
    diagnostics = json.loads(row["diagnostics_json"])
    assert diagnostics["planned_entry"] == pytest.approx(100.0)
    assert diagnostics["expected_fill"] == pytest.approx(100.1)


def test_paper_reject_persists_primary_reason_and_multi_gate_evidence(tmp_path: Path) -> None:
    engine, orchestrator = _orchestrator(tmp_path, ExecutionMode.PAPER, run_id="run-391-reject")
    orchestrator._persist_burnin_decision({
        "signal_id": "sig-391-reject", "reject_decision_id": "reject:391",
        "decision": "REJECTED", "decision_time": "2026-09-22T15:01:00Z",
        "symbol": "ETHUSDT", "side": "SHORT", "score": 0.40, "rr": 1.25,
        "candidate_rr": 1.25, "expected_fill": 2500.5, "executable_raw_rr": 0.96,
        "effective_rr": 0.90, "min_effective_rr": 1.10,
        "entry": 2500.0, "sl": 2520.0, "tp": 2475.0,
        "primary_reject_reason": "LOW_SCORE", "reason": "LOW_SCORE",
        "all_failed_gates": ["LOW_SCORE", "LOW_EFFECTIVE_RR", "STOP_TOO_TIGHT"],
        "failed_gate_evidence": [
            {"gate": "LOW_SCORE", "observed": 0.40, "threshold": 0.50},
            {"gate": "LOW_EFFECTIVE_RR", "observed": 0.90, "threshold": 1.10},
        ],
        "execution_ctx": {},
    }, lifecycle_state=LifecycleState.SIGNAL_REJECTED.value)
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT run_id,mode,decision,reject_reason,raw_rr,effective_rr,min_effective_rr,
                   reject_flags,diagnostics_json
            FROM decision_evidence WHERE signal_id='sig-391-reject'
        """)).mappings().one()
    assert row["run_id"] == "run-391-reject"
    assert row["mode"] == "PAPER"
    assert row["decision"] == "REJECT"
    assert row["reject_reason"] == "LOW_SCORE"
    assert row["raw_rr"] == pytest.approx(0.96)
    assert row["effective_rr"] == pytest.approx(0.90)
    assert row["min_effective_rr"] == pytest.approx(1.10)
    assert json.loads(row["reject_flags"]) == ["LOW_SCORE", "LOW_EFFECTIVE_RR", "STOP_TOO_TIGHT"]
    assert json.loads(row["diagnostics_json"])["all_failed_gates"] == ["LOW_SCORE", "LOW_EFFECTIVE_RR", "STOP_TOO_TIGHT"]


def test_live_precheck_writes_scoped_no_submit_decision_evidence(tmp_path: Path) -> None:
    engine, orchestrator = _orchestrator(tmp_path, ExecutionMode.LIVE_PRECHECK, run_id="run-391-precheck")
    orchestrator._persist_burnin_decision({
        "signal_id": "sig-391-precheck", "setup_identity": "setup-391-precheck",
        "decision": "ACCEPTED", "decision_time": "2026-09-22T15:02:00Z",
        "symbol": "BTCUSDT", "side": "LONG", "score": 0.75, "rr": 1.7,
        "executable_raw_rr": 1.55, "effective_rr": 1.42, "entry": 100.0,
        "expected_fill": 100.05, "sl": 98.0, "tp": 103.4, "execution_ctx": {},
    }, lifecycle_state=LifecycleState.ORDER_PLACED.value)
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT run_id,mode,decision,diagnostics_json
            FROM decision_evidence WHERE signal_id='sig-391-precheck'
        """)).mappings().one()
    assert row["run_id"] == "run-391-precheck"
    assert row["mode"] == "LIVE_PRECHECK"
    assert row["decision"] == "ACCEPT"
    assert json.loads(row["diagnostics_json"])["no_submit_verified"] is True


def test_shared_writer_preserves_null_semantics_and_non_null_evidence_on_replay(tmp_path: Path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'writer-391.db'}")
    with engine.begin() as conn:
        first = save_decision_evidence(
            conn, evidence_id="e-391", run_id="run-391", mode="PAPER",
            timestamp="2026-09-22T15:03:00Z", symbol="BTCUSDT",
            decision="ACCEPTED", signal_id="sig-391", spread_pct=0.0001,
            expected_slippage_pct=None,
        )
        second = save_decision_evidence(
            conn, evidence_id="e-391", run_id="run-391", mode="PAPER",
            decision="ACCEPTED", signal_id="sig-391", spread_pct=None,
            expected_slippage_pct=None,
        )
    assert first == second == "e-391"
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT COUNT(*) AS n,spread_pct,expected_slippage_pct,funding_rate_pct,latency_ms
            FROM decision_evidence WHERE evidence_id='e-391'
        """)).mappings().one()
    assert row["n"] == 1
    assert row["spread_pct"] == pytest.approx(0.0001)
    assert row["expected_slippage_pct"] is None
    assert row["funding_rate_pct"] is None
    assert row["latency_ms"] is None
