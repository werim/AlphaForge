from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from alphaforge.alert_delivery import AlertDeliveryProbeConfig, WebhookAlertDeliveryEvidenceProvider, capture_alert_delivery_evidence, latest_persisted_alert_delivery_evidence
from alphaforge.burnin import persist_burnin_observation
from alphaforge.burnin_campaign import create_campaign, start_or_resume_campaign
from alphaforge.live_readiness import LiveReadinessEvaluator
from alphaforge.persistence import init_db, save_order_decision, save_trade_lifecycle_event
from alphaforge.release_gates import build_release_snapshot, run_canary_mutation_trap_validation, persist_operator_ack, persist_release_snapshot, persist_rollback_verification, persist_runbook_evidence, required_operator_ack_text
from alphaforge.rollback_evidence import persist_rollback_validation_evidence
from alphaforge.runtime_heartbeat import save_runtime_heartbeat
from alphaforge.runtime_state import RuntimeStateSnapshot, save_runtime_state_snapshot


def _seed_valid(session: Session) -> None:
    save_order_decision(session, decision_id="d-1", signal_id="s-1", symbol="BTCUSDT", mode="PAPER", decision="REJECTED", reject_reason="HIGH_SPREAD", score=7.0, rr=1.4)
    save_order_decision(session, decision_id="d-2", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", decision="ACCEPTED", reject_reason="", score=8.2, rr=2.0)
    save_trade_lifecycle_event(session, event_id="e-1", signal_id="s-1", symbol="BTCUSDT", mode="PAPER", lifecycle_state="SIGNAL_CREATED", event_ts="2026-01-01T00:00:00Z")
    save_trade_lifecycle_event(session, event_id="e-2", signal_id="s-1", symbol="BTCUSDT", mode="PAPER", lifecycle_state="SIGNAL_REJECTED", reject_reason="HIGH_SPREAD", event_ts="2026-01-01T00:00:01Z", previous_lifecycle_state="SIGNAL_CREATED")
    save_trade_lifecycle_event(session, event_id="e-3", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", lifecycle_state="SIGNAL_CREATED", event_ts="2026-01-01T00:00:00Z")
    save_trade_lifecycle_event(session, event_id="e-4", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", lifecycle_state="WAITING_ENTRY_ZONE", event_ts="2026-01-01T00:00:01Z", previous_lifecycle_state="SIGNAL_CREATED")
    save_trade_lifecycle_event(session, event_id="e-5", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", lifecycle_state="ENTRY_TRIGGERED", event_ts="2026-01-01T00:00:02Z", previous_lifecycle_state="WAITING_ENTRY_ZONE")
    save_trade_lifecycle_event(session, event_id="e-6", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", lifecycle_state="CANCELLED", event_ts="2026-01-01T00:00:03Z", previous_lifecycle_state="ENTRY_TRIGGERED")

    session.execute(text("""
        INSERT INTO decision_evidence (
            evidence_id, mode, timestamp, symbol, side, lifecycle_state_before, lifecycle_state_after,
            decision, score, raw_rr, effective_rr, min_effective_rr, expectancy_bucket, reject_reason,
            cost_penalty, total_cost_pct, total_explicit_cost_pct, spread_pct, expected_slippage_pct, liquidity_score,
            diagnostics_json, portfolio_equity, open_position_count, max_open_positions, total_notional_exposure, max_notional_exposure,
            symbol_notional_exposure, max_symbol_notional, daily_loss_pct, max_daily_loss_pct, rolling_drawdown_pct,
            correlation_group, correlation_group_exposure, correlated_position_count, portfolio_reject_reason,
            portfolio_risk_state, portfolio_diagnostics_json, signal_id, lifecycle_seq, created_at
        ) VALUES
            ('de-1', 'PAPER', '2026-01-01T00:00:01Z', 'BTCUSDT', 'LONG', 'SIGNAL_CREATED', 'SIGNAL_REJECTED', 'REJECT', 7.0, 1.4, 1.2, 1.6, 'LOW', 'HIGH_SPREAD',
             0.2, 0.011, 0.011, 0.01, 0.001, 0.5, '{"spread_penalty": 0.2, "total_explicit_cost_pct": 0.011, "cost_penalty_rr": 0.2}',
             10000, 1, 3, 4000, 5000, 1000, 2000, 0.01, 0.05, 0.02, 'CRYPTO_MAJOR_BTC', 4000, 1, 'MAX_NOTIONAL_EXPOSURE', 'MAX_NOTIONAL_EXPOSURE', '{"engine":"evaluate_portfolio_risk"}', 's-1', 2, '2026-01-01T00:00:01Z'),
            ('de-2', 'PAPER', '2026-01-01T00:00:01Z', 'ETHUSDT', 'LONG', 'SIGNAL_CREATED', 'WAITING_ENTRY_ZONE', 'ACCEPT', 8.2, 2.0, 1.8, 1.6, 'HIGH', '',
             0.2, 0.011, 0.011, 0.01, 0.001, 0.8, '{"spread_penalty": 0.2, "total_explicit_cost_pct": 0.011, "cost_penalty_rr": 0.2}',
             10000, 1, 3, 1000, 5000, 500, 2000, 0.01, 0.05, 0.02, 'CRYPTO_MAJOR_ETH', 500, 1, '', 'ACCEPTED', '{"engine":"evaluate_portfolio_risk"}', 's-2', 2, '2026-01-01T00:00:01Z'),
            ('de-3', 'BACKTEST', '2026-01-01T00:00:01Z', 'SOLUSDT', 'LONG', 'SIGNAL_CREATED', 'SIGNAL_REJECTED', 'REJECT', 7.1, 1.5, 1.2, 1.6, 'LOW', 'CORRELATION_OVEREXPOSURE',
             0.2, 0.011, 0.011, 0.01, 0.001, 0.5, '{"spread_penalty": 0.2, "total_explicit_cost_pct": 0.011, "cost_penalty_rr": 0.2}',
             10000, 1, 3, 4500, 5000, 500, 2000, 0.01, 0.05, 0.02, 'CRYPTO_HIGH_BETA_ALT', 4500, 2, 'CORRELATION_OVEREXPOSURE', 'CORRELATION_OVEREXPOSURE', '{"engine":"evaluate_portfolio_risk"}', 's-3', 2, '2026-01-01T00:00:01Z')
        ON CONFLICT(evidence_id) DO NOTHING
    """))
    session.execute(text("""
        UPDATE decision_evidence
        SET funding_rate_pct=0.0001,
            latency_ms=50.0,
            volatility_regime='normal',
            unavailable_fields='[]'
        WHERE evidence_id IN ('de-1','de-2','de-3')
    """))
    session.commit()


def _parity() -> dict[str, object]:
    return {"evidence_status": "COMPLETE", "sample_count": 5, "min_sample_count": 3, "mismatch_count": 0, "missing_field_count": 0, "no_order_submission_verified": True, "no_submit_verified": True, "execution_context_complete": True, "effective_rr_penalty_breakdown_complete": True}


def _reconciliation() -> dict[str, object]:
    return {"provider_configured": True, "evidence_status": "COMPLETE", "orphan_positions": 0, "orphan_orders": 0, "duplicate_fills": 0, "fail_closed_findings": 0, "exchange_connectivity_healthy": True, "authenticated": True}


def _operational() -> dict[str, object]:
    return {"evidence_status": "COMPLETE", "observability_evidence_source": "MEASURED_PROBE", "observability_evidence_persisted": True, "qualification_persistence_verified": True, "incident_persistence_verified": True, "forensic_export_verified": True, "sensitive_data_redaction_verified": True, "alert_delivery_verified": True, "rollback_evidence_status": "COMPLETE", "rollback_evidence_source": "DETERMINISTIC_VALIDATION", "rollback_evidence_persisted": True, "kill_switch_block_verified": True, "no_submit_on_kill_switch_verified": True, "fail_closed_reconciliation_verified": True, "repair_actions_non_mutating_verified": True}


class _StaticProvider:
    def __init__(self, result: dict[str, object]):
        self.result = result

    def snapshot(self):
        return self.result


def _verified_alert() -> dict[str, object]:
    return {"provider_configured": True, "evidence_status": "COMPLETE", "observability_evidence_source": "MEASURED_PROBE", "alert_delivery_verified": True, "delivery_attempted": True, "delivery_acknowledged": True, "non_trading_probe_verified": True, "probe_id": "probe-verified", "endpoint_origin": "https://alerts.example.test", "blocking_reasons": []}


def _persist_verified_rollback(engine) -> None:
    persist_rollback_validation_evidence(engine, {
        "validation_id": "rollback:readiness-test",
        "kill_switch_block_verified": True,
        "no_submit_on_kill_switch_verified": True,
        "fail_closed_reconciliation_verified": True,
        "repair_actions_non_mutating_verified": True,
        "execution_mutation_attempt_count": 0,
        "blocking_reasons": [],
        "evidence_payload": {"validation_scope": "READINESS_TEST_FIXTURE"},
    })




def _persist_verified_phase6_release(engine, *, release_id: str = "default", phase: str = "PHASE6") -> None:
    persist_operator_ack(
        engine,
        release_id=release_id,
        phase=phase,
        acknowledgement_text=required_operator_ack_text(release_id),
        evidence={"source": "readiness-test"},
    )
    run_canary_mutation_trap_validation(engine, release_id=release_id, phase=phase)
    persist_rollback_verification(engine, release_id=release_id, phase=phase)
    persist_runbook_evidence(engine, release_id=release_id, phase=phase, runbook_path="RUNBOOK.md")
    persist_release_snapshot(engine, build_release_snapshot(engine, release_id=release_id, phase=phase))

def _engine(*, persist_alert: bool = True, persist_live_heartbeat: bool = True, persist_rollback: bool = True, persist_runtime_snapshot: bool = True):
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        _seed_valid(session)
    if persist_alert:
        capture_alert_delivery_evidence(engine, _StaticProvider(_verified_alert()))
    if persist_live_heartbeat:
        save_runtime_heartbeat(engine, runtime_instance_id="runtime:live-qualified-test", execution_mode="LIVE", scanner_source="EXCHANGE_PUBLIC_MARKET_DATA")
    _persist_verified_rollback(engine)
    _persist_verified_phase6_release(engine)
    if not persist_rollback:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM live_rollback_validation_evidence"))
    if persist_runtime_snapshot:
        save_runtime_state_snapshot(engine, RuntimeStateSnapshot(mode="LIVE_PRECHECK", requested_mode="LIVE_PRECHECK", actual_mode="LIVE_PRECHECK", runtime_status="RECONCILED", heartbeat_age_sec=1.0, instance_id="runtime:phase5-readiness", kill_switch_active=False, unknown_exchange_state=False, exchange_read_only_status="AVAILABLE", reconciliation_status="CLEAN", recovery_action_required=False))
    return engine


def _dashboard_security() -> dict[str, object]:
    return {"rbac_verified": True, "secrets_redacted": True, "live_switch_fail_closed": True}

def _timesfm_evidence() -> dict[str, object]:
    return {"non_ordering": True, "satisfies_execution_readiness": False}

def _paper_burnin() -> dict[str, object]:
    return {"status": "ACCEPTABLE"}

def _tests_evidence() -> dict[str, object]:
    return {"status": "PASS", "command": "pytest -q"}

def _evaluate(engine, observations=None, **overrides):
    kwargs = {
        "mode_parity": _parity(),
        "reconciliation_snapshot": _reconciliation(),
        "observability_snapshot": observations or _operational(),
        "canary_enabled": True,
        "shadow_mode_enabled": True,
        "operator_ack": True,
        "dashboard_security": _dashboard_security(),
        "timesfm_evidence": _timesfm_evidence(),
        "paper_burnin_report": _paper_burnin(),
        "tests_passing_evidence": _tests_evidence(),
    }
    kwargs.update(overrides)
    return LiveReadinessEvaluator(engine, min_effective_rr=1.6).evaluate(**kwargs)


def test_live_readiness_pass_and_persistence() -> None:
    engine = _engine()
    evaluator = LiveReadinessEvaluator(engine)
    report = _evaluate(engine)
    assert report.qualified is False
    assert report.verdict == "LIVE_REAL_ORDERS_BLOCKED"
    assert next(check for check in report.checks if check.name == "runtime_heartbeat").passed is True
    assert next(check for check in report.checks if check.name == "rollback_ready").passed is True
    evaluator.persist_report(report)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM live_readiness_reports")).scalar_one() == 1


def test_lifecycle_error_is_an_explicit_readiness_blocker() -> None:
    engine = _engine()
    with Session(engine) as session:
        assert save_trade_lifecycle_event(
            session,
            event_id="e-readiness-error",
            signal_id="s-1",
            symbol="BTCUSDT",
            mode="PAPER",
            lifecycle_state="ERROR",
            previous_lifecycle_state="SIGNAL_REJECTED",
            failure_reason="INVALID_LIFECYCLE_TRANSITION",
            payload={
                "attempted_state": "ORDER_PLACED",
                "previous_state": "SIGNAL_REJECTED",
            },
            event_ts="2026-01-01T00:00:02Z",
        ) is True
        session.commit()

    with engine.connect() as conn:
        checks = {check.name: check for check in LiveReadinessEvaluator(engine)._check_lifecycle(conn)}
    assert checks["lifecycle_error_free"].passed is False
    assert checks["lifecycle_error_free"].details == "lifecycle_errors=1"


def test_live_readiness_rejects_missing_runtime_heartbeat() -> None:
    report = _evaluate(_engine(persist_live_heartbeat=False))
    heartbeat = next(check for check in report.checks if check.name == "runtime_heartbeat")
    assert report.qualified is False
    assert heartbeat.passed is False
    assert "NO_PERSISTED_LIVE_RUNTIME_HEARTBEAT" in heartbeat.details


def test_optimistic_rollback_flags_without_persisted_evidence_do_not_qualify() -> None:
    report = _evaluate(_engine(persist_rollback=False), _operational())
    rollback = next(check for check in report.checks if check.name == "rollback_ready")
    assert report.qualified is False
    assert rollback.passed is False
    assert "ROLLBACK_EVIDENCE_MISSING" in rollback.details


def test_live_readiness_rejects_paper_only_runtime_heartbeat() -> None:
    engine = _engine(persist_live_heartbeat=False)
    save_runtime_heartbeat(engine, runtime_instance_id="runtime:paper-only", execution_mode="PAPER", scanner_source="EXCHANGE_PUBLIC_MARKET_DATA")
    heartbeat = next(check for check in _evaluate(engine).checks if check.name == "runtime_heartbeat")
    assert heartbeat.passed is False
    assert "NO_PERSISTED_LIVE_RUNTIME_HEARTBEAT" in heartbeat.details


def test_live_readiness_rejects_stale_live_runtime_heartbeat() -> None:
    engine = _engine(persist_live_heartbeat=False)
    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    save_runtime_heartbeat(engine, runtime_instance_id="runtime:stale-live", execution_mode="LIVE", scanner_source="EXCHANGE_PUBLIC_MARKET_DATA", heartbeat_ts=stale)
    heartbeat = next(check for check in _evaluate(engine).checks if check.name == "runtime_heartbeat")
    assert heartbeat.passed is False
    assert "state=STALE" in heartbeat.details


def test_fresh_live_heartbeat_satisfies_only_its_independent_subcheck() -> None:
    report = _evaluate(_engine(persist_alert=False))
    results = {check.name: check for check in report.checks}
    assert results["runtime_heartbeat"].passed is True
    assert results["alert_delivery_evidence"].passed is False
    assert report.qualified is False


def test_in_memory_alert_delivery_flag_without_persisted_ack_does_not_qualify() -> None:
    report = _evaluate(_engine(persist_alert=False), _operational())
    results = {check.name: check for check in report.checks}
    assert report.qualified is False
    assert results["alert_delivery_evidence"].passed is False
    assert results["observability_coverage"].passed is False


def test_persisted_incomplete_alert_overrides_optimistic_operational_flag() -> None:
    engine = _engine(persist_alert=False)
    incomplete = {"evidence_status": "INCOMPLETE", "alert_delivery_verified": False, "delivery_attempted": True, "delivery_acknowledged": False, "non_trading_probe_verified": True, "endpoint_origin": "https://alerts.example.test", "blocking_reasons": ["ACKNOWLEDGEMENT_NOT_VERIFIED"]}
    capture_alert_delivery_evidence(engine, _StaticProvider(incomplete))
    report = _evaluate(engine, _operational())
    failed = {check.name for check in report.checks if not check.passed}
    assert report.qualified is False
    assert {"alert_delivery_evidence", "observability_coverage"} <= failed


def test_matching_diagnostic_probe_ack_is_persisted_and_accepted() -> None:
    def transport(url: str, payload: bytes, headers, timeout: float):
        request = json.loads(payload.decode("utf-8"))
        return {"status": "ACKNOWLEDGED", "acknowledged": True, "probe_id": request["probe_id"]}
    engine = _engine(persist_alert=False)
    provider = WebhookAlertDeliveryEvidenceProvider(AlertDeliveryProbeConfig(endpoint_url="https://alerts.example.test/probe"), transport=transport, probe_id_factory=lambda: "probe-measured")
    saved = capture_alert_delivery_evidence(engine, provider)
    loaded = latest_persisted_alert_delivery_evidence(engine)
    assert saved["alert_delivery_verified"] is True
    assert loaded["alert_delivery_verified"] is True
    assert loaded["observability_evidence_persisted"] is True


def test_nonmatching_probe_ack_is_persisted_but_keeps_readiness_blocked() -> None:
    provider = WebhookAlertDeliveryEvidenceProvider(AlertDeliveryProbeConfig(endpoint_url="https://alerts.example.test/probe"), transport=lambda url, payload, headers, timeout: {"status": "ACKNOWLEDGED", "acknowledged": True, "probe_id": "different"}, probe_id_factory=lambda: "probe-measured")
    engine = _engine(persist_alert=False)
    saved = capture_alert_delivery_evidence(engine, provider)
    assert saved["alert_delivery_verified"] is False
    assert _evaluate(engine).qualified is False


def test_insecure_probe_destination_is_not_called() -> None:
    invoked: list[bool] = []
    provider = WebhookAlertDeliveryEvidenceProvider(AlertDeliveryProbeConfig(endpoint_url="http://alerts.example.test/probe"), transport=lambda url, payload, headers, timeout: invoked.append(True) or {}, probe_id_factory=lambda: "probe-measured")
    result = provider.snapshot()
    assert invoked == []
    assert result["alert_delivery_verified"] is False


def test_stale_persisted_alert_evidence_is_rejected() -> None:
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(text("UPDATE live_alert_delivery_evidence SET recorded_at='2026-01-01T00:00:00+00:00'"))
    evidence = latest_persisted_alert_delivery_evidence(engine, now=datetime(2026, 5, 22, tzinfo=timezone.utc))
    assert evidence["alert_delivery_verified"] is False
    assert evidence["alert_delivery_blocking_reasons"] == ["ALERT_DELIVERY_EVIDENCE_STALE"]


def test_static_operational_flags_without_persisted_alert_or_rollback_provenance_do_not_qualify() -> None:
    observations = _operational()
    observations.pop("rollback_evidence_source")
    observations.pop("rollback_evidence_persisted")
    report = _evaluate(_engine(persist_alert=False, persist_rollback=False), observations)
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
    assert payload["report"]["qualified"] is False
    assert payload["report"]["verdict"] == "LIVE_REAL_ORDERS_BLOCKED"


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
        reconciliation_snapshot=_reconciliation(), observability_snapshot=_operational(), canary_enabled=True, shadow_mode_enabled=True, operator_ack=True,
    )
    assert report.qualified is False
    assert any(check.name == "mode_parity" and not check.passed for check in report.checks)
    evaluator.persist_report(report)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM live_readiness_reports")).scalar_one() == 1


def test_live_precheck_parity_mismatch_blocks_readiness() -> None:
    engine = _engine()
    parity = _parity()
    parity["mismatch_count"] = 1
    parity["evidence_status"] = "INCOMPLETE"
    report = LiveReadinessEvaluator(engine).evaluate(mode_parity=parity, reconciliation_snapshot=_reconciliation(), observability_snapshot=_operational(), canary_enabled=True, shadow_mode_enabled=True, operator_ack=True)
    mode_parity = next(check for check in report.checks if check.name == "mode_parity")
    assert report.qualified is False
    assert mode_parity.passed is False


def test_live_precheck_missing_execution_context_blocks_readiness() -> None:
    engine = _engine()
    parity = _parity()
    parity["execution_context_complete"] = False
    report = LiveReadinessEvaluator(engine).evaluate(mode_parity=parity, reconciliation_snapshot=_reconciliation(), observability_snapshot=_operational(), canary_enabled=True, shadow_mode_enabled=True, operator_ack=True)
    mode_parity = next(check for check in report.checks if check.name == "mode_parity")
    assert report.qualified is False
    assert mode_parity.passed is False
    assert "LIVE_PRECHECK_EXECUTION_CONTEXT_MISSING" in mode_parity.details


def test_successful_live_precheck_parity_alone_does_not_unlock_live_real_orders() -> None:
    engine = _engine(persist_alert=False, persist_rollback=False, persist_live_heartbeat=False)
    report = LiveReadinessEvaluator(engine).evaluate(mode_parity=_parity(), reconciliation_snapshot=_reconciliation(), observability_snapshot=_operational(), canary_enabled=True, shadow_mode_enabled=True, operator_ack=True)
    results = {check.name: check for check in report.checks}
    assert results["mode_parity"].passed is True
    assert report.qualified is False
    assert results["runtime_heartbeat"].passed is False


def test_live_precheck_invalid_execution_evidence_blocks_readiness() -> None:
    engine = _engine()
    parity = _parity()
    parity["execution_evidence_status"] = "INVALID_FAKE_ZERO"
    report = LiveReadinessEvaluator(engine).evaluate(mode_parity=parity, reconciliation_snapshot=_reconciliation(), observability_snapshot=_operational(), canary_enabled=True, shadow_mode_enabled=True, operator_ack=True)
    mode_parity = next(check for check in report.checks if check.name == "mode_parity")
    assert report.qualified is False
    assert mode_parity.passed is False
    assert "LIVE_PRECHECK_EXECUTION_EVIDENCE_BLOCKING:INVALID_FAKE_ZERO" in mode_parity.details


def test_final_gate_missing_each_individual_gate_blocks_real_orders() -> None:
    baseline = _evaluate(_engine())
    assert baseline.verdict == "LIVE_REAL_ORDERS_BLOCKED"
    for gate_name in [gate.name for gate in baseline.gates or []]:
        kwargs = {}
        if gate_name == "exchange_connectivity_healthy":
            rec = _reconciliation(); rec["exchange_connectivity_healthy"] = False; kwargs["reconciliation_snapshot"] = rec
        elif gate_name == "timesfm_evidence_safe_non_ordering":
            kwargs["timesfm_evidence"] = {"non_ordering": False, "satisfies_execution_readiness": True}
        elif gate_name == "paper_burnin_report_acceptable":
            kwargs["paper_burnin_report"] = {"status": "MISSING"}
        elif gate_name == "full_tests_passing_evidence_recorded":
            kwargs["tests_passing_evidence"] = {"status": "MISSING"}
        elif gate_name == "dashboard_rbac_secrets_safe":
            kwargs["dashboard_security"] = {"rbac_verified": False, "secrets_redacted": True, "live_switch_fail_closed": True}
        elif gate_name == "operator_acknowledgement_required":
            kwargs["operator_ack"] = False
        else:
            continue
        report = _evaluate(_engine(), **kwargs)
        assert report.verdict != "LIVE_REAL_ORDERS_READY"
        assert any(g.name == gate_name and not g.passed for g in (report.gates or []))


def test_lower_gates_only_allow_live_precheck_ready_not_real_orders() -> None:
    report = _evaluate(_engine(), dashboard_security={}, timesfm_evidence={}, paper_burnin_report={}, tests_passing_evidence={})
    assert report.verdict == "LIVE_PRECHECK_READY"
    assert report.qualified is False


def test_kill_switch_on_blocks_all_live_readiness() -> None:
    report = _evaluate(_engine(), kill_switch_active=True)
    assert report.verdict == "NOT_LIVE_READY"
    assert any(g.name == "kill_switch_verified" and not g.passed for g in (report.gates or []))


def test_timesfm_cannot_satisfy_execution_or_order_readiness_by_itself() -> None:
    rec = _reconciliation(); rec["exchange_connectivity_healthy"] = False; rec["authenticated"] = False
    report = _evaluate(_engine(), reconciliation_snapshot=rec, timesfm_evidence={"non_ordering": True, "satisfies_execution_readiness": True})
    gates = {g.name: g for g in (report.gates or [])}
    assert gates["timesfm_evidence_safe_non_ordering"].passed is False
    assert gates["exchange_connectivity_healthy"].passed is False
    assert report.verdict != "LIVE_REAL_ORDERS_READY"


def test_phase2_readiness_fails_when_decision_evidence_empty_even_if_csv_artifacts_exist(tmp_path) -> None:
    (tmp_path / "decision_evidence.csv").write_text("evidence_id,decision\ncsv-only,ACCEPT\n")
    (tmp_path / "order_backtest_lifecycle.csv").write_text("signal_id,lifecycle_state\ncsv-only,SIGNAL_ACCEPTED\n")
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM decision_evidence"))
    report = _evaluate(engine)
    checks = {check.name: check for check in report.checks}
    assert report.qualified is False
    assert checks["phase2_decision_evidence_rows_present"].passed is False
    assert any(gate.name == "phase2_persisted_evidence_complete" and not gate.passed for gate in report.gates or [])


def test_phase2_readiness_fails_on_decision_evidence_parity_mismatch() -> None:
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(text("UPDATE decision_evidence SET reject_reason='DECISION_PARITY_MISMATCH' WHERE evidence_id='de-1'"))
    report = _evaluate(engine)
    check = next(check for check in report.checks if check.name == "phase2_no_decision_parity_mismatch")
    assert report.qualified is False
    assert check.passed is False


def test_phase3_execution_gate_blocks_when_each_required_check_fails() -> None:
    mutations = {
        "execution_cost_breakdown_present": "UPDATE decision_evidence SET cost_penalty=NULL, diagnostics_json='{}'",
        "effective_rr_available": "UPDATE decision_evidence SET effective_rr=NULL",
        "execution_rejects_persisted": "UPDATE decision_evidence SET reject_reason='' WHERE decision='REJECT'",
        "no_accepted_trade_with_effective_rr_below_threshold": "UPDATE decision_evidence SET effective_rr=1.0 WHERE decision='ACCEPT'",
        "no_accepted_trade_with_missing_critical_execution_context": "UPDATE decision_evidence SET spread_pct=NULL WHERE decision='ACCEPT'",
        "no_fake_zero_execution_costs": "UPDATE decision_evidence SET diagnostics_json='UNAVAILABLE', spread_pct=0 WHERE decision='ACCEPT'",
    }
    for check_name, sql in mutations.items():
        engine = _engine()
        with engine.begin() as conn:
            conn.execute(text(sql))
        report = _evaluate(engine)
        checks = {c.name: c for c in report.checks}
        gates = {g.name: g for g in (report.gates or [])}
        assert checks[check_name].passed is False
        assert gates["phase3_execution_realism_complete"].passed is False
        assert report.verdict == "NOT_LIVE_READY"


def test_phase3_readiness_recognizes_canonical_execution_safety_reject_names() -> None:
    canonical_reasons = (
        "SPREAD_TOO_HIGH",
        "SLIPPAGE_TOO_HIGH",
        "HIGH_TOTAL_COST",
        "THIN_LIQUIDITY",
        "HIGH_LATENCY",
        "EXECUTION_CONTEXT_UNAVAILABLE",
        "INVALID_FAKE_ZERO",
        "EXCESSIVE_VOLATILITY",
        "FUNDING_TOO_HIGH",
        "LOW_EFFECTIVE_RR",
    )
    for reason in canonical_reasons:
        engine = _engine()
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE decision_evidence SET reject_reason=:reason WHERE evidence_id='de-1'"),
                {"reason": reason},
            )
        with engine.connect() as conn:
            checks = {
                check.name: check
                for check in LiveReadinessEvaluator(engine, min_effective_rr=1.6)._check_persistence(conn)
            }
        assert checks["execution_rejects_persisted"].passed is True, reason


def test_phase3_readiness_blocks_all_critical_execution_context_gaps() -> None:
    mutations = (
        "UPDATE decision_evidence SET latency_ms=NULL WHERE evidence_id='de-2'",
        "UPDATE decision_evidence SET funding_rate_pct=NULL WHERE evidence_id='de-2'",
        "UPDATE decision_evidence SET volatility_regime=NULL WHERE evidence_id='de-2'",
        "UPDATE decision_evidence SET unavailable_fields='[\"latency_ms\"]' WHERE evidence_id='de-2'",
    )
    for sql in mutations:
        engine = _engine()
        with engine.begin() as conn:
            conn.execute(text(sql))
        with engine.connect() as conn:
            checks = {
                check.name: check
                for check in LiveReadinessEvaluator(engine, min_effective_rr=1.6)._check_persistence(conn)
            }
        assert checks["no_accepted_trade_with_missing_critical_execution_context"].passed is False


def test_phase6_readiness_fails_when_release_evidence_absent() -> None:
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS release_gate_snapshots"))
        conn.execute(text("DROP TABLE IF EXISTS operator_acknowledgements"))
        conn.execute(text("DROP TABLE IF EXISTS canary_run_events"))
        conn.execute(text("DROP TABLE IF EXISTS rollback_verification_events"))
        conn.execute(text("DROP TABLE IF EXISTS runbook_evidence"))

    report = _evaluate(engine)
    gates = {gate.name: gate for gate in (report.gates or [])}
    checks = {check.name: check for check in report.checks}
    assert report.qualified is False
    assert report.verdict == "NOT_LIVE_READY"
    assert checks["phase6_release_gate_evidence"].passed is False
    assert gates["phase6_release_gates_verified"].passed is False


def test_phase6_all_gates_pass_still_blocks_real_live_orders() -> None:
    report = _evaluate(_engine())
    assert report.qualified is False
    assert report.verdict == "LIVE_REAL_ORDERS_BLOCKED"
    assert any(g.name == "phase6_release_gates_verified" and g.passed for g in (report.gates or []))


def _seed_scoped_readiness_run(engine) -> tuple[str, str]:
    with engine.begin() as conn:
        campaign = create_campaign(
            conn,
            release_id="scope-rel",
            duration_days=1,
            symbols=["BTCUSDT", "ETHUSDT"],
            intervals=["1m"],
        )
        run = start_or_resume_campaign(conn, campaign.campaign_id)
        run_id = str(run["burnin_run_id"])
        persist_burnin_observation(
            conn,
            observation_id="scope-reject",
            burnin_run_id=run_id,
            release_id=campaign.release_id,
            execution_mode="PAPER",
            symbol="BTCUSDT",
            decision="REJECTED",
            lifecycle_state="SIGNAL_REJECTED",
            metrics={"signal_id": "s-1", "reject_decision_id": "d-1"},
        )
        persist_burnin_observation(
            conn,
            observation_id="scope-accept",
            burnin_run_id=run_id,
            release_id=campaign.release_id,
            execution_mode="PAPER",
            symbol="ETHUSDT",
            decision="ACCEPTED",
            lifecycle_state="CANCELLED",
            metrics={"signal_id": "s-2"},
        )
        conn.execute(
            text("UPDATE decision_evidence SET run_id=:run_id WHERE UPPER(mode)='PAPER'"),
            {"run_id": run_id},
        )
    return campaign.campaign_id, run_id


def test_readiness_run_scope_ignores_dirty_neighbor_evidence() -> None:
    engine = _engine()
    campaign_id, run_id = _seed_scoped_readiness_run(engine)

    with Session(engine) as session:
        save_order_decision(
            session,
            decision_id="dirty-decision",
            signal_id="dirty-signal",
            symbol="SOLUSDT",
            mode="PAPER",
            decision="REJECTED",
            reject_reason="DECISION_PARITY_MISMATCH",
            score=1.0,
            rr=1.0,
            parity_result="DECISION_PARITY_MISMATCH",
        )
        save_trade_lifecycle_event(
            session,
            event_id="dirty-event",
            signal_id="dirty-signal",
            symbol="SOLUSDT",
            mode="PAPER",
            lifecycle_state="ENTRY_TRIGGERED",
            event_ts="2026-01-01T00:00:00Z",
        )
        session.execute(text("""
            INSERT INTO decision_evidence(
                evidence_id,run_id,mode,timestamp,symbol,decision,reject_reason,
                diagnostics_json,spread_pct,expected_slippage_pct,funding_rate_pct,
                volume_24h_usdt,liquidity_score,created_at
            ) VALUES(
                'dirty-evidence','other-run','PAPER','2026-01-01T00:00:00Z','SOLUSDT','REJECT',
                'DECISION_PARITY_MISMATCH','DECISION_PARITY_MISMATCH UNAVAILABLE',
                0,0,0,0,0,'2026-01-01T00:00:00Z'
            )
        """))
        session.commit()

    evaluator = LiveReadinessEvaluator(
        engine,
        evidence_mode="PAPER",
        campaign_id=campaign_id,
        require_run_scope=True,
    )
    assert evaluator.burnin_run_id == run_id
    assert evaluator._scope_check().passed is True

    with engine.connect() as conn:
        lifecycle = {check.name: check for check in evaluator._check_lifecycle(conn)}
        persistence = {check.name: check for check in evaluator._check_persistence(conn)}
        stats = {check.name: check for check in evaluator._check_stats(conn)}

    assert lifecycle["lifecycle_no_orphans"].passed is True
    assert lifecycle["lifecycle_error_free"].passed is True
    assert persistence["phase2_no_decision_parity_mismatch"].passed is True
    assert persistence["phase2_no_fake_zero_execution_evidence"].passed is True
    assert persistence["phase2_decision_evidence_rows_present"].details == "decision_evidence_rows=2"
    assert stats["reject_rate_sanity"].details.endswith("total=2")


def test_readiness_mode_scope_ignores_backtest_only_poison() -> None:
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO decision_evidence(
                evidence_id,mode,timestamp,symbol,decision,reject_reason,diagnostics_json,
                spread_pct,expected_slippage_pct,funding_rate_pct,volume_24h_usdt,
                liquidity_score,created_at
            ) VALUES(
                'backtest-poison','BACKTEST','2026-01-01T00:00:00Z','SOLUSDT','REJECT',
                'DECISION_PARITY_MISMATCH','DECISION_PARITY_MISMATCH UNAVAILABLE',
                0,0,0,0,0,'2026-01-01T00:00:00Z'
            )
        """))
    with engine.connect() as conn:
        checks = {check.name: check for check in LiveReadinessEvaluator(engine, evidence_mode="PAPER")._check_persistence(conn)}
    assert checks["phase2_no_decision_parity_mismatch"].passed is True
    assert checks["phase2_no_fake_zero_execution_evidence"].passed is True


def test_required_readiness_scope_fails_closed_when_campaign_run_is_missing() -> None:
    engine = _engine()
    evaluator = LiveReadinessEvaluator(
        engine,
        evidence_mode="PAPER",
        campaign_id="missing-campaign",
        require_run_scope=True,
    )
    scope = evaluator._scope_check()
    assert scope.passed is False
    assert "burnin_run_id=UNRESOLVED" in scope.details
    with engine.connect() as conn:
        persistence = {check.name: check for check in evaluator._check_persistence(conn)}
        stats = {check.name: check for check in evaluator._check_stats(conn)}
    assert persistence["phase2_decision_evidence_rows_present"].passed is False
    assert persistence["phase2_decision_evidence_rows_present"].details == "decision_evidence_rows=0"
    assert stats["reject_rate_sanity"].passed is False
    assert stats["reject_rate_sanity"].details.endswith("total=0")


def test_scoped_release_gate_does_not_borrow_default_release() -> None:
    engine = _engine()
    check = LiveReadinessEvaluator(
        engine,
        release_id="different-release",
        require_run_scope=True,
    )._check_release_gates()[0]
    assert check.passed is False
    assert "different-release" in check.details


def test_scoped_runtime_state_does_not_borrow_other_instance() -> None:
    engine = _engine()
    checks = {
        check.name: check
        for check in LiveReadinessEvaluator(
            engine,
            runtime_instance_id="runtime:not-present",
        )._check_runtime_state_snapshot()
    }
    assert checks["runtime_state_snapshot_present"].passed is False


def test_scoped_precheck_startup_snapshot_does_not_require_paper_run_attachment() -> None:
    engine = _engine(persist_runtime_snapshot=False)
    save_runtime_state_snapshot(
        engine,
        RuntimeStateSnapshot(
            mode="LIVE_PRECHECK",
            requested_mode="LIVE_PRECHECK",
            actual_mode="LIVE_PRECHECK",
            runtime_status="STARTUP",
            heartbeat_age_sec=1.0,
            instance_id="runtime:precheck-current",
            campaign_id="camp-current",
            burnin_run_id=None,
            release_id="rel-current",
            kill_switch_active=False,
            unknown_exchange_state=False,
            exchange_read_only_status="AVAILABLE",
            reconciliation_status="CLEAN",
            recovery_action_required=False,
        ),
    )
    checks = {
        check.name: check
        for check in LiveReadinessEvaluator(
            engine,
            campaign_id="camp-current",
            burnin_run_id=None,
            release_id="rel-current",
            runtime_instance_id="runtime:precheck-current",
        )._check_runtime_state_snapshot()
    }
    assert checks["runtime_state_snapshot_present"].passed is True
    assert checks["runtime_db_persistence_verified"].passed is True


def test_effective_rr_readiness_uses_persisted_decision_threshold_not_current_config() -> None:
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE decision_evidence SET effective_rr=1.35, min_effective_rr=1.10 WHERE evidence_id='de-2'"
        ))

    with engine.connect() as conn:
        low_threshold = {
            check.name: check
            for check in LiveReadinessEvaluator(engine, min_effective_rr=9.9)._check_persistence(conn)
        }
    assert low_threshold["effective_rr_threshold_provenance_valid"].passed is True
    assert low_threshold["no_accepted_trade_with_effective_rr_below_threshold"].passed is True
    assert "source=decision_evidence.min_effective_rr" in low_threshold[
        "no_accepted_trade_with_effective_rr_below_threshold"
    ].details

    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE decision_evidence SET min_effective_rr=1.60 WHERE evidence_id='de-2'"
        ))

    with engine.connect() as conn:
        high_threshold = {
            check.name: check
            for check in LiveReadinessEvaluator(engine, min_effective_rr=1.1)._check_persistence(conn)
        }
    assert high_threshold["effective_rr_threshold_provenance_valid"].passed is True
    assert high_threshold["no_accepted_trade_with_effective_rr_below_threshold"].passed is False


def test_effective_rr_readiness_missing_or_invalid_row_threshold_fails_closed() -> None:
    engine = _engine()
    for value in (None, 0.0, -0.1):
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE decision_evidence SET min_effective_rr=:value WHERE evidence_id='de-2'"),
                {"value": value},
            )
        with engine.connect() as conn:
            checks = {
                check.name: check
                for check in LiveReadinessEvaluator(engine, min_effective_rr=0.1)._check_persistence(conn)
            }
        assert checks["effective_rr_threshold_provenance_valid"].passed is False
        assert checks["no_accepted_trade_with_effective_rr_below_threshold"].passed is False
        assert checks["no_accepted_trade_with_missing_critical_execution_context"].passed is False
        assert "invalid_or_missing_threshold_rows=1" in checks[
            "no_accepted_trade_with_effective_rr_below_threshold"
        ].details


def test_phase3_gate_requires_valid_effective_rr_threshold_provenance() -> None:
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(text("UPDATE decision_evidence SET min_effective_rr=NULL WHERE evidence_id='de-2'"))
    report = LiveReadinessEvaluator(engine, min_effective_rr=1.6).evaluate(
        mode_parity=_parity(),
        reconciliation_snapshot=_reconciliation(),
        observability_snapshot=_operational(),
        canary_enabled=True,
        shadow_mode_enabled=True,
        operator_ack=True,
        dashboard_security=_dashboard_security(),
        timesfm_evidence=_timesfm_evidence(),
        paper_burnin_report=_paper_burnin(),
        tests_passing_evidence=_tests_evidence(),
    )
    checks = {check.name: check for check in report.checks}
    gates = {gate.name: gate for gate in report.gates or []}
    assert checks["effective_rr_threshold_provenance_valid"].passed is False
    assert gates["phase3_execution_realism_complete"].passed is False
    assert report.verdict == "NOT_LIVE_READY"
