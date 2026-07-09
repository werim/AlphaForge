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
