from __future__ import annotations

from datetime import datetime, timezone, timedelta

from sqlalchemy import text

from alphaforge.live_readiness import LiveReadinessEvaluator
from alphaforge.persistence import init_db
from alphaforge.release_gates import (
    ACK_RISK_PHRASE,
    ReleaseGateSnapshot,
    build_release_snapshot,
    evaluate_canary_candidate,
    latest_release_snapshot,
    persist_canary_event,
    persist_operator_ack,
    persist_release_gate_snapshot,
    persist_rollback_verification,
    persist_runbook_evidence,
)


def test_release_gate_snapshot_and_evidence_persisted(tmp_path):
    rb = tmp_path / "RUNBOOK.md"
    rb.write_text("# runbook\nrollback procedure\n", encoding="utf-8")
    engine = init_db()
    rid = "rel-6"
    ack = persist_operator_ack(engine, release_id=rid, operator_user="ops", ack_text=f"{ACK_RISK_PHRASE}; release_id={rid}")
    persist_canary_event(engine, release_id=rid, event_type="START", status="PASS", symbol="BTCUSDT")
    rollback = persist_rollback_verification(engine, release_id=rid, procedure_path=str(rb), dry_run=True, kill_switch_verified=True, runtime_stop_verified=True)
    runbook = persist_runbook_evidence(engine, release_id=rid, runbook_path=str(rb))
    snap = build_release_snapshot(engine, release_id=rid, requested_mode="LIVE_PRECHECK", actual_mode="LIVE_PRECHECK", canary_enabled=True, shadow_mode_enabled=True, canary_symbols=["BTCUSDT"], test_evidence_status="PASS", paper_burnin_status="ACCEPTABLE", runbook_path=str(rb))
    persist_release_gate_snapshot(engine, snap)
    latest = latest_release_snapshot(engine)
    assert ack["valid"] == 1
    assert rollback["status"] == "PASS"
    assert runbook["status"] == "PASS"
    assert latest["release_id"] == rid
    assert latest["live_order_submission_enabled"] == 0
    assert latest["operator_ack_present"] == 1
    assert latest["rollback_ready"] == 1
    assert latest["runbook_present"] == 1


def test_operator_ack_missing_expired_or_wrong_release_blocks_canary(tmp_path):
    rb = tmp_path / "RUNBOOK.md"; rb.write_text("rollback", encoding="utf-8")
    engine = init_db()
    persist_operator_ack(engine, release_id="other", operator_user="ops", ack_text=f"{ACK_RISK_PHRASE}; release_id=other")
    persist_rollback_verification(engine, release_id="rel", procedure_path=str(rb), dry_run=True, kill_switch_verified=True, runtime_stop_verified=True)
    snap = build_release_snapshot(engine, release_id="rel", requested_mode="LIVE_PRECHECK", actual_mode="LIVE_PRECHECK", canary_enabled=True, canary_symbols=["BTCUSDT"], runbook_path=str(rb))
    assert "CANARY_OPERATOR_ACK_MISSING" in snap.readiness_blockers
    bad = persist_operator_ack(engine, release_id="rel", operator_user="ops", ack_text="generic ack")
    assert bad["valid"] == 0
    expired = persist_operator_ack(engine, release_id="rel", operator_user="ops", ack_text=f"{ACK_RISK_PHRASE}; release_id=rel", ttl_minutes=-1)
    assert expired["valid"] == 1
    snap2 = build_release_snapshot(engine, release_id="rel", requested_mode="LIVE_PRECHECK", actual_mode="LIVE_PRECHECK", canary_enabled=True, canary_symbols=["BTCUSDT"], runbook_path=str(rb))
    assert "CANARY_OPERATOR_ACK_MISSING" in snap2.readiness_blockers


def test_canary_scope_notional_risk_and_no_live_submit():
    snap = ReleaseGateSnapshot(timestamp="t", release_id="r", version="v", git_commit="g", branch="b", requested_mode="LIVE_PRECHECK", actual_mode="LIVE_PRECHECK", live_precheck_enabled=True, canary_enabled=True, canary_symbols=["BTCUSDT"], canary_max_notional=100.0, canary_max_risk_pct=0.01, operator_ack_required=True, operator_ack_present=True)
    assert evaluate_canary_candidate(snap, symbol="ETHUSDT", notional=10, risk_pct=0.001)[1] == "CANARY_SYMBOL_SCOPE_VIOLATION"
    assert evaluate_canary_candidate(snap, symbol="BTCUSDT", notional=101, risk_pct=0.001)[1] == "CANARY_NOTIONAL_LIMIT"
    assert evaluate_canary_candidate(snap, symbol="BTCUSDT", notional=10, risk_pct=0.02)[1] == "CANARY_RISK_LIMIT"
    snap.live_order_submission_enabled = True
    assert evaluate_canary_candidate(snap, symbol="BTCUSDT", notional=10, risk_pct=0.001)[1] == "CANARY_MUTATION_ATTEMPT"


def test_release_readiness_phase6_fails_without_snapshot_and_blocks_real_live_orders():
    engine = init_db()
    report = LiveReadinessEvaluator(engine).evaluate(mode_parity={"no_submit_verified": True}, reconciliation_snapshot={}, observability_snapshot={}, canary_enabled=True, shadow_mode_enabled=True, operator_ack=True, paper_burnin_report={"status":"ACCEPTABLE"}, tests_passing_evidence={"status":"PASS"})
    assert any(g.name == "release_gate_snapshot_present" and not g.passed for g in report.gates or [])
    assert report.verdict != "LIVE_REAL_ORDERS_READY"


def test_canary_mutation_attempt_blocks_snapshot(tmp_path):
    rb = tmp_path / "RUNBOOK.md"; rb.write_text("rollback", encoding="utf-8")
    engine = init_db(); rid="rel"
    persist_operator_ack(engine, release_id=rid, ack_text=f"{ACK_RISK_PHRASE}; release_id={rid}")
    persist_rollback_verification(engine, release_id=rid, procedure_path=str(rb), dry_run=True, kill_switch_verified=True, runtime_stop_verified=True)
    persist_canary_event(engine, release_id=rid, event_type="MUTATION", status="FAIL", reason="CANARY_MUTATION_ATTEMPT", mutation_attempt=True)
    snap = build_release_snapshot(engine, release_id=rid, requested_mode="LIVE_PRECHECK", actual_mode="LIVE_PRECHECK", canary_enabled=True, canary_symbols=["BTCUSDT"], runbook_path=str(rb))
    assert "CANARY_MUTATION_ATTEMPT" in snap.readiness_blockers

import asyncio
import json
from pathlib import Path

from sqlalchemy.orm import Session

from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator
from alphaforge.release_gates import MutationTrapExecutionAdapter, canary_mutation_attempt_count


class _AcceptBrain:
    def before_real_order(self, signal_payload, market_ctx, regime_ctx, stats_ctx):
        class _Score:
            total_score = 0.9
        class _Plan:
            decision = "ACCEPTED"
            reason = ""
            confidence = 0.9
            order_type = "MARKET"
            limit_price = None
            stop_price = None
        return _Score(), _Plan(), "accepted"

    def score_signal(self, signal_payload, market_ctx, regime_ctx, stats_ctx):
        class _Score:
            total_score = 0.9
        return _Score()

    def choose_order_plan(self, signal_payload, market_ctx, score_ctx):
        class _Plan:
            decision = "ACCEPTED"
            reason = ""
            confidence = 0.9
            order_type = "MARKET"
            limit_price = None
            stop_price = None
        return _Plan()

    def explain_decision(self, signal_payload, score_ctx, order_plan):
        return "accepted"


def _market(symbol="BTCUSDT", notional=100.0, risk_pct=0.001):
    return {"symbol": symbol, "entry": 100.0, "sl": 99.0, "tp": 103.0, "rr": 3.0, "side": "LONG", "market_ts": 9999999999.0, "equity": 100000.0, "available_balance": 100000.0, "notional": notional, "risk_pct": risk_pct, "volume_24h_usdt": 90_000_000, "spread_pct": 0.0002, "volatility_pct": 0.4, "trend_strength": 0.9, "liquidity_score": 0.9, "chop_score": 0.1}


def _seed_release(engine, tmp_path: Path, *, release_id="rel-runtime", symbols=("BTCUSDT",), max_notional=1000.0, max_risk=0.01, ack=True):
    rb = tmp_path / f"RUNBOOK-{release_id}.md"
    rb.write_text("rollback procedure", encoding="utf-8")
    if ack:
        persist_operator_ack(engine, release_id=release_id, ack_text=f"{ACK_RISK_PHRASE}; release_id={release_id}")
    persist_rollback_verification(engine, release_id=release_id, procedure_path=str(rb), dry_run=True, kill_switch_verified=True, runtime_stop_verified=True)
    snap = build_release_snapshot(engine, release_id=release_id, requested_mode="LIVE_PRECHECK", actual_mode="LIVE_PRECHECK", canary_enabled=True, shadow_mode_enabled=False, canary_symbols=list(symbols), canary_max_notional=max_notional, canary_max_risk_pct=max_risk, test_evidence_status="PASS", paper_burnin_status="ACCEPTABLE", runbook_path=str(rb))
    persist_release_gate_snapshot(engine, snap)
    return release_id


def _runtime(engine, scanner, release_id="rel-runtime", **config_kwargs):
    return RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.LIVE_PRECHECK, enable_canary_mode=True, release_id=release_id, min_liquidity_usd=1.0, max_spread_pct=0.01, max_expected_slippage_pct=0.01, **config_kwargs),
        ai_brain=_AcceptBrain(),
        market_scanner=scanner,
        persistence_engine=engine,
    )


def test_runtime_canary_gate_rejects_out_of_scope_symbol(tmp_path):
    engine = init_db(); rid = _seed_release(engine, tmp_path, symbols=("BTCUSDT",))
    async def scanner(): return [_market("ETHUSDT")]
    rt = _runtime(engine, scanner, release_id=rid)
    asyncio.run(rt._scan_once())
    assert rt.metrics.executions == 0
    assert rt._pending_orders == {}
    with engine.connect() as conn:
        reasons = [r[0] for r in conn.execute(text("SELECT reason FROM canary_run_events WHERE release_id=:rid"), {"rid": rid})]
    assert "CANARY_SYMBOL_SCOPE_VIOLATION" in reasons


def test_runtime_canary_gate_rejects_notional_and_risk(tmp_path):
    engine = init_db(); rid = _seed_release(engine, tmp_path, max_notional=50.0, max_risk=0.001)
    async def scanner(): return [_market("BTCUSDT", notional=100.0, risk_pct=0.01)]
    rt = _runtime(engine, scanner, release_id=rid)
    asyncio.run(rt._scan_once())
    with engine.connect() as conn:
        reason = conn.execute(text("SELECT reason FROM canary_run_events WHERE status='REJECTED' ORDER BY id DESC LIMIT 1")).scalar_one()
    assert reason in {"CANARY_NOTIONAL_LIMIT", "CANARY_RISK_LIMIT"}
    assert rt._active_positions == {}


def test_runtime_canary_missing_ack_blocks_real_path(tmp_path):
    engine = init_db(); rid = _seed_release(engine, tmp_path, ack=False)
    async def scanner(): return [_market("BTCUSDT")]
    rt = _runtime(engine, scanner, release_id=rid)
    asyncio.run(rt._scan_once())
    with engine.connect() as conn:
        reason = conn.execute(text("SELECT reason FROM canary_run_events WHERE status='REJECTED' ORDER BY id DESC LIMIT 1")).scalar_one()
    assert reason == "CANARY_OPERATOR_ACK_MISSING"


def test_shadow_mode_persists_decision_without_position_or_pending_order(tmp_path):
    engine = init_db()
    async def scanner(): return [_market("BTCUSDT")]
    rt = RuntimeOrchestrator(config=RuntimeConfig(execution_mode=ExecutionMode.PAPER, enable_shadow_mode=True, min_liquidity_usd=1.0, max_spread_pct=0.01, max_expected_slippage_pct=0.01), ai_brain=_AcceptBrain(), market_scanner=scanner, persistence_engine=engine)
    asyncio.run(rt._scan_once())
    assert rt._active_positions == {}
    assert rt._pending_orders == {}
    with Session(engine) as session:
        row = session.execute(text("SELECT mode, phase, decision, no_submit_verified, order_payload FROM order_decisions WHERE mode='SHADOW'")).mappings().one()
    payload = json.loads(row["order_payload"])
    assert row["phase"] == "shadow"
    assert row["decision"] == "SHADOW_DECISION"
    assert row["no_submit_verified"] == 1
    assert payload["active_position_mutated"] is False
    assert payload["pending_order_mutated"] is False


def test_mutation_trap_persists_attempt_and_blocks_readiness(tmp_path):
    engine = init_db(); rid = _seed_release(engine, tmp_path)
    trap = MutationTrapExecutionAdapter(engine=engine, release_id=rid)
    try:
        asyncio.run(trap.submit({"order_type": "MARKET"}, {"symbol": "BTCUSDT"}))
    except RuntimeError as exc:
        assert str(exc) == "CANARY_MUTATION_ATTEMPT"
    assert trap.submit_calls == 1
    assert canary_mutation_attempt_count(engine, rid) == 1
    report = LiveReadinessEvaluator(engine).evaluate(mode_parity={"no_submit_verified": True, "execution_context_complete": True}, reconciliation_snapshot={}, observability_snapshot={}, canary_enabled=True, shadow_mode_enabled=True, operator_ack=True, paper_burnin_report={"status":"ACCEPTABLE"}, tests_passing_evidence={"status":"PASS"})
    assert any(g.name == "no_canary_mutation_attempts" and not g.passed for g in report.gates or [])


def test_canary_duration_reject_spike_and_runtime_error_limits_stop(tmp_path):
    engine = init_db(); rid = _seed_release(engine, tmp_path, symbols=("BTCUSDT",))
    async def scanner(): return [_market("ETHUSDT")]
    rt = _runtime(engine, scanner, release_id=rid, canary_max_reject_rate=0.0, canary_stop_on_reject_spike=True)
    asyncio.run(rt._scan_once())
    assert rt._canary_stopped is True
    assert rt._canary_stop_reason == "CANARY_REJECT_SPIKE"

    rt2 = _runtime(engine, scanner, release_id=rid, canary_duration_min=0.000001)
    rt2._canary_started_monotonic = 0.0
    ok, reason = rt2._canary_candidate_gate(symbol="BTCUSDT", notional=10.0, risk_pct=0.001)
    assert not ok and reason == "CANARY_DURATION_EXCEEDED"

    rt3 = _runtime(engine, scanner, release_id=rid, canary_max_runtime_errors=1)
    rt3.metrics.canary_runtime_errors = 1
    ok, reason = rt3._canary_candidate_gate(symbol="BTCUSDT", notional=10.0, risk_pct=0.001)
    assert not ok and reason == "CANARY_RUNTIME_ERROR_LIMIT"


def test_canary_evidence_persistence_failure_blocks_canary():
    async def scanner(): return [_market("BTCUSDT")]
    rt = RuntimeOrchestrator(config=RuntimeConfig(execution_mode=ExecutionMode.LIVE_PRECHECK, enable_canary_mode=True, release_id="missing"), ai_brain=_AcceptBrain(), market_scanner=scanner, persistence_engine=None)
    asyncio.run(rt._scan_once())
    assert rt.metrics.executions == 0
    assert rt._pending_orders == {}
