from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from alphaforge.alert_delivery import capture_alert_delivery_evidence, latest_persisted_alert_delivery_evidence, persist_alert_delivery_evidence
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


def _verified_alert() -> dict[str, object]:
    return {"provider_configured": True, "evidence_status": "COMPLETE", "observability_evidence_source": "MEASURED_PROBE", "alert_delivery_verified": True, "delivery_attempted": True, "delivery_acknowledged": True, "non_trading_probe_verified": True, "probe_id": "probe-verified", "endpoint_origin": "https://alerts.example.test", "blocking_reasons": []}


def _engine(*, persist_alert: bool = True):
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        _seed_valid(session)
    if persist_alert:
        persist_alert_delivery_evidence(engine, _verified_alert())
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


def test_in_memory_alert_delivery_flag_without_persisted_ack_does_not_qualify() -> None:
    report = _evaluate(_engine(persist_alert=False), _operational())
    results = {check.name: check for check in report.checks}
    assert report.qualified is False
    assert results["alert_delivery_evidence"].passed is False
    assert results["observability_coverage"].passed is False


def test_persisted_incomplete_alert_overrides_optimistic_operational_flag() -> None:
    engine = _engine(persist_alert=False)
    persist_alert_delivery_evidence(engine, {"evidence_status": "INCOMPLETE", "alert_delivery_verified": False, "delivery_attempted": True, "delivery_acknowledged": False, "non_trading_probe_verified": True, "endpoint_origin": "https://alerts.example.test", "blocking_reasons": ["ACKNOWLEDGEMENT_NOT_VERIFIED"]})
    report = _evaluate(engine, _operational())
    failed = {check.name for check in report.checks if not check.passed}
    assert report.qualified is False
    assert {"alert_delivery_evidence", "observability_coverage"} <= failed


def test_capture_alert_delivery_evidence_persists_provider_result() -> None:
    class _VerifiedProvider:
        def snapshot(self):
            return _verified_alert()

    engine = _engine(persist_alert=False)
    saved = capture_alert_delivery_evidence(engine, _VerifiedProvider())
    loaded = latest_persisted_alert_delivery_evidence(engine)
    assert saved["alert_delivery_verified"] is True
    assert loaded["alert_delivery_verified"] is True
    assert loaded["observability_evidence_persisted"] is True


def test_capture_incomplete_evidence_keeps_readiness_blocked() -> None:
    class _IncompleteProvider:
        def snapshot(self):
            return {"evidence_status": "INCOMPLETE", "alert_delivery_verified": False, "delivery_attempted": True, "delivery_acknowledged": False, "non_trading_probe_verified": True, "endpoint_origin": "UNAVAILABLE", "blocking_reasons": ["NO_ACK"]}

    engine = _engine(persist_alert=False)
    capture_alert_delivery_evidence(engine, _IncompleteProvider())
    assert _evaluate(engine).qualified is False


def test_static_operational_flags_without_persisted_alert_or_rollback_provenance_do_not_qualify() -> None:
    observations = _operational()
    observations.pop("rollback_evidence_source")
    observations.pop("rollback_evidence_persisted")
    report = _evaluate(_engine(persist_alert=False), observations)
    failed = {check.name for check in report.checks if not check.passed}
    assert report.qualified is False
    assert {"alert_delivery_evidence", "observability_coverage", "rollback_ready"} <= failed


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


def test_invalid_numeric_parity_evidence_fails_closed_and_persists_report() -> None:
    engine = _engine()
    evaluator = LiveReadinessEvaluator(engine)
    report = evaluator.evaluate(
        mode_parity={"evidence_status": "COMPLETE", "sample_count": "N/A", "min_sample_count": None, "mismatch_count": "", "missing_field_count": "bad-value", "no_order_submission_verified": True},
        reconciliation_snapshot=_reconciliation(),
        observability_snapshot=_operational(),
        canary_enabled=True,
        shadow_mode_enabled=True,
        operator_ack=True,
    )
    assert report.qualified is False
    assert any(check.name == "mode_parity" and not check.passed for check in report.checks)
    evaluator.persist_report(report)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM live_readiness_reports")).scalar_one() == 1
