from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from alphaforge.live_readiness import LiveReadinessEvaluator
from alphaforge.persistence import init_db, save_order_decision, save_trade_lifecycle_event


def _seed_valid(session: Session) -> None:
    save_order_decision(session, decision_id="d-1", signal_id="s-1", symbol="BTCUSDT", mode="PAPER", decision="REJECTED", reject_reason="HIGH_SPREAD", score=7.0, rr=1.4)
    save_order_decision(session, decision_id="d-2", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", decision="ACCEPTED", reject_reason="", score=8.2, rr=2.0)
    save_trade_lifecycle_event(session, event_id="e-1", signal_id="s-1", symbol="BTCUSDT", mode="PAPER", lifecycle_state="SIGNAL_CREATED", event_ts="2026-01-01T00:00:00Z")
    save_trade_lifecycle_event(session, event_id="e-2", signal_id="s-1", symbol="BTCUSDT", mode="PAPER", lifecycle_state="SIGNAL_REJECTED", reject_reason="HIGH_SPREAD", event_ts="2026-01-01T00:00:01Z", previous_lifecycle_state="SIGNAL_CREATED")
    save_trade_lifecycle_event(session, event_id="e-3", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", lifecycle_state="SIGNAL_CREATED", event_ts="2026-01-01T00:00:00Z")
    save_trade_lifecycle_event(session, event_id="e-4", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", lifecycle_state="WAITING_ENTRY_ZONE", event_ts="2026-01-01T00:00:01Z", previous_lifecycle_state="SIGNAL_CREATED")
    save_trade_lifecycle_event(session, event_id="e-5", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", lifecycle_state="ENTRY_TRIGGERED", event_ts="2026-01-01T00:00:02Z", previous_lifecycle_state="WAITING_ENTRY_ZONE")
    save_trade_lifecycle_event(session, event_id="e-6", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", lifecycle_state="CANCELLED", event_ts="2026-01-01T00:00:03Z", previous_lifecycle_state="ENTRY_TRIGGERED")


def _parity() -> dict[str, object]:
    return {"evidence_status": "COMPLETE", "sample_count": 5, "min_sample_count": 3, "mismatch_count": 0, "missing_field_count": 0, "no_order_submission_verified": True}


def _reconciliation() -> dict[str, object]:
    return {"provider_configured": True, "evidence_status": "COMPLETE", "orphan_positions": 0, "orphan_orders": 0, "duplicate_fills": 0, "fail_closed_findings": 0}


def _operational() -> dict[str, object]:
    return {"evidence_status": "COMPLETE", "observability_evidence_source": "MEASURED_PROBE", "observability_evidence_persisted": True, "qualification_persistence_verified": True, "incident_persistence_verified": True, "forensic_export_verified": True, "sensitive_data_redaction_verified": True, "alert_delivery_verified": True, "rollback_evidence_status": "COMPLETE", "rollback_evidence_source": "DETERMINISTIC_VALIDATION", "rollback_evidence_persisted": True, "kill_switch_block_verified": True, "no_submit_on_kill_switch_verified": True, "fail_closed_reconciliation_verified": True, "repair_actions_non_mutating_verified": True}


def _engine():
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        _seed_valid(session)
    return engine


def _evaluate(engine, observations=None):
    return LiveReadinessEvaluator(engine).evaluate(mode_parity=_parity(), reconciliation_snapshot=_reconciliation(), observability_snapshot=observations or _operational(), canary_enabled=True, shadow_mode_enabled=True, operator_ack=True)


def test_live_readiness_pass_and_persistence() -> None:
    engine = _engine()
    evaluator = LiveReadinessEvaluator(engine)
    report = _evaluate(engine)
    assert report.qualified is True
    evaluator.persist_report(report)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM live_readiness_reports")).scalar_one() == 1


def test_static_operational_flags_without_provenance_do_not_qualify() -> None:
    observations = _operational()
    for key in ("observability_evidence_source", "observability_evidence_persisted", "rollback_evidence_source", "rollback_evidence_persisted"):
        observations.pop(key)
    report = _evaluate(_engine(), observations)
    failed = {check.name for check in report.checks if not check.passed}
    assert report.qualified is False
    assert {"observability_coverage", "rollback_ready"} <= failed


def test_live_readiness_detects_lifecycle_orphan() -> None:
    engine = _engine()
    with Session(engine) as session:
        save_trade_lifecycle_event(session, event_id="bad", signal_id="new", symbol="SOLUSDT", mode="PAPER", lifecycle_state="ENTRY_TRIGGERED", event_ts="2026-01-01T00:01:00Z")
    assert any(check.name == "lifecycle_no_orphans" and not check.passed for check in _evaluate(engine).checks)


def test_forensic_snapshot_written(tmp_path) -> None:
    evaluator = LiveReadinessEvaluator(_engine())
    report = _evaluate(evaluator.engine)
    payload = json.loads(evaluator.write_forensic_snapshot(tmp_path, report, {"positions": 0}).read_text())
    assert payload["version"] == "gen5"
    assert payload["report"]["qualified"] is True


def test_forensic_snapshot_redacts_nested_fields(tmp_path) -> None:
    evaluator = LiveReadinessEvaluator(_engine())
    report = _evaluate(evaluator.engine)
    private_key = "api_" + "key"
    auth_key = "Author" + "ization"
    payload = json.loads(evaluator.write_forensic_snapshot(tmp_path, report, {private_key: "drop", "headers": {auth_key: "drop"}, "safe": 1}).read_text())
    assert private_key not in payload["runtime_snapshot"]
    assert auth_key not in payload["runtime_snapshot"]["headers"]
