from __future__ import annotations

import json
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from alphaforge.runtime_heartbeat import evaluate_runtime_heartbeat_freshness
from alphaforge.runtime_state import latest_runtime_state_snapshot
from .rollback_queries import fetch_rollback_evidence_status


READINESS_PROBE_CATALOG: tuple[dict[str, Any], ...] = (
    {"name": "runtime_heartbeat", "category": "runtime_presence", "surface": "runtime_heartbeats", "critical": True, "implemented": True},
    {"name": "lifecycle_no_orphans", "category": "lifecycle_integrity", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "lifecycle_transitions_valid", "category": "lifecycle_integrity", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "rejected_has_reason", "category": "decision_integrity", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "entry_exit_completeness", "category": "lifecycle_integrity", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "schema_signals", "category": "persistence_integrity", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "critical_not_null_signals", "category": "persistence_integrity", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "schema_order_decisions", "category": "persistence_integrity", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "critical_not_null_order_decisions", "category": "persistence_integrity", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "schema_trade_lifecycle_events", "category": "persistence_integrity", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "critical_not_null_trade_lifecycle_events", "category": "persistence_integrity", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "reject_persistence_parity", "category": "decision_integrity", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "reject_rate_sanity", "category": "selectivity", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "rr_not_constant", "category": "decision_quality", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "score_not_constant", "category": "decision_quality", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "mode_parity", "category": "paper_live_parity", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "live_reconciliation_provider", "category": "exchange_reconciliation", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "reconciliation_evidence_complete", "category": "exchange_reconciliation", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "reconciliation_no_orphans", "category": "exchange_reconciliation", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "duplicate_execution_free", "category": "exchange_reconciliation", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "reconciliation_fail_closed_clear", "category": "exchange_reconciliation", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "shadow_mode_enabled", "category": "deployment_guard", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "canary_enabled", "category": "deployment_guard", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "operator_acknowledged", "category": "deployment_guard", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "alert_delivery_evidence", "category": "observability", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "observability_coverage", "category": "observability", "surface": "live_readiness_reports", "critical": True, "implemented": True},
    {"name": "rollback_ready", "category": "emergency_control", "surface": "live_rollback_validation_evidence", "critical": True, "implemented": True},
)


def _has_table(engine: Engine, table_name: str) -> bool:
    try:
        return bool(inspect(engine).has_table(table_name))
    except SQLAlchemyError:
        return False


def _column_names(engine: Engine, table_name: str) -> set[str]:
    if not _has_table(engine, table_name):
        return set()
    try:
        return {str(column["name"]) for column in inspect(engine).get_columns(table_name)}
    except SQLAlchemyError:
        return set()


def fetch_runtime_heartbeat_status(engine: Engine, *, max_age_sec: float = 120.0) -> dict[str, Any]:
    evidence = evaluate_runtime_heartbeat_freshness(engine, max_age_sec=max_age_sec)
    heartbeat = evidence.latest_heartbeat or {}
    try:
        runtime_snapshot = latest_runtime_state_snapshot(engine) or {}
    except Exception as exc:
        runtime_snapshot = {"missing_evidence_reason": f"runtime_state_snapshot_query_failed:{exc.__class__.__name__}"}
    missing_reason = "" if runtime_snapshot else "runtime_state_snapshot_missing"
    return {
        "runtime_process_status": evidence.state,
        "runtime_process_status_reason": evidence.reason,
        "latest_heartbeat_ts": heartbeat.get("heartbeat_ts"),
        "runtime_instance_id": heartbeat.get("runtime_instance_id"),
        "execution_mode": heartbeat.get("execution_mode"),
        "scanner_source": heartbeat.get("scanner_source"),
        "runtime_state": heartbeat.get("runtime_state"),
        "heartbeat_age_sec": evidence.age_sec,
        "heartbeat_max_age_sec": evidence.max_age_sec,
        "last_scan_ts": heartbeat.get("last_scan_ts"),
        "last_decision_ts": heartbeat.get("last_decision_ts"),
        "active_positions_count": heartbeat.get("active_positions_count"),
        "pending_orders_count": heartbeat.get("pending_orders_count"),
        "runtime_snapshot_instance_id": runtime_snapshot.get("instance_id"),
        "runtime_snapshot_status": runtime_snapshot.get("runtime_status"),
        "runtime_recovery_required": runtime_snapshot.get("recovery_action_required"),
        "runtime_unclean_shutdown_detected": runtime_snapshot.get("fail_closed_reason") == "UNCLEAN_SHUTDOWN_RECOVERY_REQUIRED",
        "runtime_orphan_order_count": runtime_snapshot.get("orphan_order_count"),
        "runtime_orphan_position_count": runtime_snapshot.get("orphan_position_count"),
        "runtime_reconciliation_status": runtime_snapshot.get("reconciliation_status"),
        "runtime_exchange_read_only_status": runtime_snapshot.get("exchange_read_only_status"),
        "runtime_fail_closed_reason": runtime_snapshot.get("fail_closed_reason"),
        "runtime_last_error": runtime_snapshot.get("last_error"),
        "runtime_missing_evidence_reason": runtime_snapshot.get("missing_evidence_reason") or missing_reason,
    }


def fetch_latest_readiness(engine: Engine) -> dict[str, Any]:
    if not _has_table(engine, "live_readiness_reports"):
        return {"status": "NOT_AVAILABLE", "reason": "NO_READINESS_REPORT_TABLE"}
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT generated_at, qualified, deployment_state, acknowledgement_required, report_payload
                FROM live_readiness_reports ORDER BY id DESC LIMIT 1
            """)).mappings().first()
    except SQLAlchemyError:
        return {"status": "NOT_AVAILABLE", "reason": "READINESS_QUERY_UNAVAILABLE"}
    if row is None:
        return {"status": "NOT_AVAILABLE", "reason": "NO_READINESS_REPORT"}
    try:
        payload = json.loads(row.get("report_payload") or "{}")
    except json.JSONDecodeError:
        payload = {"status": "ERROR", "reason": "INVALID_REPORT_PAYLOAD"}
    return {
        "status": "PASS" if bool(row["qualified"]) else "FAIL",
        "qualified": bool(row["qualified"]),
        "deployment_state": row["deployment_state"],
        "acknowledgement_required": bool(row["acknowledgement_required"]),
        "generated_at": row["generated_at"],
        "payload": payload,
    }


def _rollback_probe(evidence: dict[str, Any]) -> dict[str, Any]:
    passed = bool(evidence.get("rollback_evidence_verified", False))
    persisted = bool(evidence.get("rollback_evidence_persisted", False))
    reasons = evidence.get("rollback_blocking_reasons") or []
    status = "PASS" if passed else ("NO_EVIDENCE" if not persisted else "FAIL")
    details = (
        f"source={evidence.get('rollback_evidence_source', 'UNVERIFIED')};"
        f"status={evidence.get('rollback_evidence_status', 'INCOMPLETE')};"
        f"kill_switch_block={evidence.get('kill_switch_block_verified', False)};"
        f"no_submit={evidence.get('no_submit_on_kill_switch_verified', False)};"
        f"fail_closed_reconciliation={evidence.get('fail_closed_reconciliation_verified', False)};"
        f"repair_non_mutating={evidence.get('repair_actions_non_mutating_verified', False)};"
        f"mutation_attempts={evidence.get('execution_mutation_attempt_count')};"
        f"blocking_reasons={reasons}"
    )
    return {"status": status, "details": details, "recorded_at": evidence.get("recorded_at"), "age_sec": evidence.get("rollback_evidence_age_sec")}


def fetch_readiness_probe_matrix(engine: Engine) -> dict[str, Any]:
    """Expose expected readiness probes and evidence gaps without running probes or mutating state."""
    readiness = fetch_latest_readiness(engine)
    heartbeat = evaluate_runtime_heartbeat_freshness(engine, required_mode="LIVE")
    rollback = fetch_rollback_evidence_status(engine)
    report_checks = {
        str(check.get("name")): check
        for check in readiness.get("payload", {}).get("checks", [])
        if isinstance(check, dict) and check.get("name")
    }
    probes: list[dict[str, Any]] = []
    for expected in READINESS_PROBE_CATALOG:
        probe = dict(expected)
        if probe["name"] == "runtime_heartbeat":
            latest = heartbeat.latest_heartbeat or {}
            probe.update({
                "status": "PASS" if heartbeat.is_fresh else heartbeat.state,
                "details": heartbeat.reason,
                "heartbeat_ts": latest.get("heartbeat_ts"),
                "execution_mode": latest.get("execution_mode"),
                "runtime_instance_id": latest.get("runtime_instance_id"),
                "freshness_state": heartbeat.state,
            })
        elif probe["name"] == "rollback_ready":
            probe.update(_rollback_probe(rollback))
        elif readiness.get("status") == "NOT_AVAILABLE":
            probe.update({"status": "NO_EVIDENCE", "details": readiness.get("reason", "NO_READINESS_REPORT")})
        elif probe["name"] not in report_checks:
            probe.update({"status": "MISSING_IN_REPORT", "details": "EXPECTED_CHECK_NOT_PRESENT_IN_LATEST_REPORT"})
        else:
            observed = report_checks[probe["name"]]
            probe.update({"status": "PASS" if bool(observed.get("passed")) else "FAIL", "details": str(observed.get("details", ""))})
        probes.append(probe)
    status_values = ("PASS", "FAIL", "MISSING", "STALE", "INVALID", "FUTURE_DATED", "MISSING_IN_REPORT", "NO_EVIDENCE")
    counts = {status: sum(1 for probe in probes if probe["status"] == status) for status in status_values}
    gaps = [probe for probe in probes if probe["status"] != "PASS"]
    return {
        "status": "COMPLETE" if not gaps else "INCOMPLETE",
        "readiness_report_status": readiness.get("status", "NOT_AVAILABLE"),
        "expected_probe_count": len(probes),
        "counts": counts,
        "critical_gap_count": sum(1 for probe in gaps if probe["critical"]),
        "probes": probes,
        "control_boundary": {
            "dashboard_mutation_controls": "INTENTIONALLY_OMITTED",
            "reason": "READ_ONLY_OBSERVABILITY_INCREMENT",
            "forbidden_actions": ["ORDER_SUBMISSION", "LIVE_ACTIVATION", "KILL_SWITCH_MUTATION", "CONFIG_EDIT", "HEARTBEAT_WRITE"],
        },
    }


def fetch_reject_summary(engine: Engine) -> dict[str, Any]:
    columns = _column_names(engine, "order_decisions")
    required_columns = {"decision", "reject_reason", "signal_id", "symbol"}
    if not required_columns.issubset(columns):
        return {"status": "NOT_AVAILABLE", "reason": "ORDER_DECISIONS_SCHEMA_INCOMPLETE", "total_final_decisions": None, "total_rejected": None, "rejection_rate": None, "reasons": [], "incomplete_rejected_rows": {}}
    where_final = "COALESCE(phase, 'final') = 'final'" if "phase" in columns else "1 = 1"
    try:
        with engine.connect() as conn:
            total = int(conn.execute(text(f"SELECT COUNT(*) FROM order_decisions WHERE {where_final}")).scalar_one())
            rejected = int(conn.execute(text(f"SELECT COUNT(*) FROM order_decisions WHERE {where_final} AND UPPER(COALESCE(decision, '')) = 'REJECTED'")).scalar_one())
            rows = conn.execute(text(f"""
                SELECT COALESCE(NULLIF(TRIM(reject_reason), ''), 'MISSING_REASON') AS reject_reason, COUNT(*) AS count
                FROM order_decisions
                WHERE {where_final} AND UPPER(COALESCE(decision, '')) = 'REJECTED'
                GROUP BY COALESCE(NULLIF(TRIM(reject_reason), ''), 'MISSING_REASON')
                ORDER BY count DESC, reject_reason ASC
            """)).mappings().all()
            incomplete = conn.execute(text(f"""
                SELECT
                    SUM(CASE WHEN signal_id IS NULL OR TRIM(signal_id) = '' THEN 1 ELSE 0 END) AS empty_signal_id_count,
                    SUM(CASE WHEN symbol IS NULL OR TRIM(symbol) = '' THEN 1 ELSE 0 END) AS empty_symbol_count,
                    SUM(CASE WHEN reject_reason IS NULL OR TRIM(reject_reason) = '' THEN 1 ELSE 0 END) AS empty_reject_reason_count
                FROM order_decisions
                WHERE {where_final} AND UPPER(COALESCE(decision, '')) = 'REJECTED'
            """)).mappings().one()
    except SQLAlchemyError:
        return {"status": "NOT_AVAILABLE", "reason": "ORDER_DECISIONS_QUERY_UNAVAILABLE", "total_final_decisions": None, "total_rejected": None, "rejection_rate": None, "reasons": [], "incomplete_rejected_rows": {}}
    return {
        "status": "AVAILABLE",
        "total_final_decisions": total,
        "total_rejected": rejected,
        "rejection_rate": (rejected / total) if total else None,
        "reasons": [{"reason": row["reject_reason"], "count": int(row["count"]), "ratio": (int(row["count"]) / rejected) if rejected else None} for row in rows],
        "incomplete_rejected_rows": {key: int(incomplete[key] or 0) for key in incomplete.keys()},
    }


def fetch_recent_lifecycle(engine: Engine, *, limit: int = 100, signal_id: str | None = None, symbol: str | None = None) -> list[dict[str, Any]]:
    columns = _column_names(engine, "trade_lifecycle_events")
    required_columns = {"signal_id", "symbol", "mode", "lifecycle_state", "reject_reason", "event_ts"}
    if not required_columns.issubset(columns):
        return []
    conditions: list[str] = []
    params: dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
    if signal_id:
        conditions.append("signal_id = :signal_id")
        params["signal_id"] = signal_id
    if symbol:
        conditions.append("symbol = :symbol")
        params["symbol"] = symbol
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT signal_id, symbol, mode, lifecycle_state, reject_reason, event_ts
                FROM trade_lifecycle_events
                {where}
                ORDER BY event_ts DESC
                LIMIT :limit
            """), params).mappings().all()
    except SQLAlchemyError:
        return []
    return [dict(row) for row in rows]


def fetch_signal_timeline(engine: Engine, signal_id: str) -> dict[str, Any]:
    events = list(reversed(fetch_recent_lifecycle(engine, limit=500, signal_id=signal_id)))
    rejected_without_reason = any(
        str(row.get("lifecycle_state") or "") == "SIGNAL_REJECTED" and not str(row.get("reject_reason") or "").strip()
        for row in events
    )
    return {
        "signal_id": signal_id,
        "events": events,
        "event_count": len(events),
        "has_signal_created": any(str(row.get("lifecycle_state") or "") == "SIGNAL_CREATED" for row in events),
        "rejected_without_reason": rejected_without_reason,
    }


def fetch_phase7_burnin(engine: Engine) -> dict[str, Any]:
    """Read-only Phase 7 burn-in dashboard evidence; intentionally emits no DDL."""
    if not _has_table(engine, "burnin_qualification_snapshots"):
        return {"status": "UNAVAILABLE", "reason": "NO_PHASE7_TABLES", "metrics": {}, "blockers": [], "warnings": [], "suspension_reasons": []}
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT * FROM burnin_qualification_snapshots
                ORDER BY generated_at DESC, id DESC LIMIT 1
            """)).mappings().first()
            if row is None:
                return {"status": "UNAVAILABLE", "reason": "NO_PHASE7_SNAPSHOT", "metrics": {}, "blockers": [], "warnings": [], "suspension_reasons": []}
            susp = []
            if _has_table(engine, "burnin_suspension_events"):
                susp = [dict(r) for r in conn.execute(text("""
                    SELECT timestamp, reason_codes_json, observed_values_json FROM burnin_suspension_events
                    WHERE burnin_run_id=:bid ORDER BY timestamp DESC, id DESC LIMIT 5
                """), {"bid": row["burnin_run_id"]}).mappings().all()]
    except SQLAlchemyError:
        return {"status": "UNAVAILABLE", "reason": "PHASE7_QUERY_UNAVAILABLE", "metrics": {}, "blockers": [], "warnings": [], "suspension_reasons": []}
    def loads(v, fallback):
        try: return json.loads(v or json.dumps(fallback))
        except Exception: return fallback
    return {
        "status": row["status"],
        "burnin_run_id": row["burnin_run_id"],
        "release_id": row["release_id"],
        "generated_at": row["generated_at"],
        "sample_status": row["sample_status"],
        "expectancy_status": row["expectancy_status"],
        "execution_status": row["execution_status"],
        "regime_status": row["regime_status"],
        "reject_quality_status": row["reject_quality_status"],
        "calibration_status": row["calibration_status"],
        "drawdown_status": row["drawdown_status"],
        "concentration_status": row["concentration_status"],
        "evidence_completeness_status": row["evidence_completeness_status"],
        "blockers": loads(row["blockers_json"], []),
        "warnings": loads(row["warnings_json"], []),
        "thresholds": loads(row["thresholds_json"], {}),
        "metrics": loads(row["metrics_json"], {}),
        "evidence_hash": row["evidence_hash"],
        "suspension_reasons": [loads(r.get("reason_codes_json"), []) for r in susp],
    }


def fetch_phase8_campaign(engine: Engine, campaign_id: str | None = None) -> dict[str, Any]:
    """Read-only Phase 8 campaign operations evidence; emits no DDL and never coerces unavailable counts to zero."""
    if not _has_table(engine, "burnin_campaigns"):
        return {"status": "UNAVAILABLE", "reason": "NO_PHASE8_CAMPAIGN_TABLES"}
    where = "WHERE campaign_id=:cid" if campaign_id else ""
    params = {"cid": campaign_id} if campaign_id else {}
    try:
        with engine.connect() as conn:
            row = conn.execute(text(f"SELECT * FROM burnin_campaigns {where} ORDER BY created_at DESC, id DESC LIMIT 1"), params).mappings().first()
            if row is None:
                return {"status": "UNAVAILABLE", "reason": "NO_PHASE8_CAMPAIGN"}
            cid = row["campaign_id"]
            runs = conn.execute(text("SELECT burnin_run_id, continuation_sequence, status FROM burnin_campaign_runs WHERE campaign_id=:cid ORDER BY continuation_sequence"), {"cid": cid}).mappings().all() if _has_table(engine,"burnin_campaign_runs") else []
            run_ids = [r["burnin_run_id"] for r in runs]
            def count_table(table: str, expr: str = "COUNT(*)", extra: str = ""):
                if not run_ids or not _has_table(engine, table): return None
                ph=",".join([f":r{i}" for i in range(len(run_ids))]); p={f"r{i}":v for i,v in enumerate(run_ids)}
                with engine.connect() as count_conn:
                    return count_conn.execute(text(f"SELECT {expr} FROM {table} WHERE burnin_run_id IN ({ph}) {extra}"), p).scalar()
            latest = None
            if _has_table(engine,"burnin_qualification_snapshots") and row.get("latest_qualification_id"):
                latest = conn.execute(text("SELECT status, blockers_json, warnings_json, generated_at FROM burnin_qualification_snapshots WHERE qualification_id=:qid"), {"qid": row["latest_qualification_id"]}).mappings().first()
    except SQLAlchemyError:
        return {"status":"UNAVAILABLE","reason":"PHASE8_CAMPAIGN_QUERY_UNAVAILABLE"}
    def loads(v, fallback):
        try: return json.loads(v or json.dumps(fallback))
        except Exception: return fallback
    decisions=count_table("burnin_observations")
    accepted=count_table("burnin_observations","COUNT(*)","AND UPPER(COALESCE(decision,''))='ACCEPTED'")
    rejected=count_table("burnin_observations","COUNT(*)","AND UPPER(COALESCE(decision,''))='REJECTED'")
    closed=count_table("burnin_trade_outcomes","COUNT(*)","AND closed_at IS NOT NULL AND evidence_complete=1")
    pending_rejects=None
    pending_positions=None
    if _has_table(engine,"burnin_pending_reject_labels"):
        try:
            with engine.connect() as conn: pending_rejects=conn.execute(text("SELECT COUNT(*) FROM burnin_pending_reject_labels WHERE campaign_id=:cid AND status IN ('PENDING','READY')"), {"cid": row["campaign_id"]}).scalar()
        except SQLAlchemyError: pending_rejects=None
    if _has_table(engine,"burnin_pending_position_outcomes"):
        try:
            with engine.connect() as conn: pending_positions=conn.execute(text("SELECT COUNT(*) FROM burnin_pending_position_outcomes WHERE campaign_id=:cid AND status='OPEN'"), {"cid": row["campaign_id"]}).scalar()
        except SQLAlchemyError: pending_positions=None
    latest_incident = None
    latest_decision = None
    latest_audit = None
    try:
        with engine.connect() as conn:
            if _has_table(engine, "burnin_ops_incidents"):
                latest_incident = conn.execute(text("SELECT incident_type, severity, status, detected_at, details_json FROM burnin_ops_incidents WHERE campaign_id=:cid ORDER BY id DESC LIMIT 1"), {"cid": row["campaign_id"]}).mappings().first()
            if _has_table(engine, "burnin_release_decisions"):
                latest_decision = conn.execute(text("SELECT decision, generated_at, blockers_json, package_dir FROM burnin_release_decisions WHERE campaign_id=:cid ORDER BY id DESC LIMIT 1"), {"cid": row["campaign_id"]}).mappings().first()
            if _has_table(engine, "burnin_integrity_audits"):
                latest_audit = conn.execute(text("SELECT status, generated_at, violations_json, aggregate_evidence_hash FROM burnin_integrity_audits WHERE campaign_id=:cid ORDER BY id DESC LIMIT 1"), {"cid": row["campaign_id"]}).mappings().first()
    except SQLAlchemyError:
        latest_incident = latest_decision = latest_audit = None
    return {"status": row["campaign_status"], "campaign_id": row["campaign_id"], "release_id": row["release_id"], "active_run_id": row["active_run_id"], "worker_pid": (row["worker_pid"] if "worker_pid" in row.keys() else None), "worker_liveness": "UNKNOWN_READ_ONLY", "continuation_lineage": [dict(r) for r in runs], "continuation_count": len(runs) if runs is not None else None, "restart_count": row["restart_count"], "expected_duration_seconds": row["expected_duration_seconds"], "observed_duration_seconds": row["observed_duration_seconds"], "decisions": decisions, "accepted": accepted, "rejected": rejected, "closed_trades": closed, "open_positions": pending_positions, "pending_reject_labels": pending_rejects, "resolver_backlog": pending_rejects, "integrity_status": dict(latest_audit) if latest_audit else None, "latest_incident": dict(latest_incident) if latest_incident else None, "final_release_decision": dict(latest_decision) if latest_decision else None, "downloadable_evidence_bundle": (dict(latest_decision).get("package_dir") if latest_decision else None), "preflight_status": None, "evidence_completeness": row["evidence_completeness_status"], "latest_qualification": dict(latest) if latest else None, "blockers": loads(latest.get("blockers_json") if latest else None, []), "warnings": loads(latest.get("warnings_json") if latest else None, []), "last_heartbeat": row["last_heartbeat_at"], "last_error": row["last_error"], "config_drift": row["last_error"] == "CONFIG_DRIFT", "export_status": None}
