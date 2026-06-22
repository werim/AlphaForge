from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Engine

from alphaforge.alert_delivery import latest_persisted_alert_delivery_evidence
from alphaforge.contracts import ALLOWED_LIFECYCLE_TRANSITIONS, LifecycleEventType, canonical_utc_timestamp
from alphaforge.rollback_evidence import latest_persisted_rollback_evidence
from alphaforge.runtime_heartbeat import DEFAULT_MAX_AGE_SEC, evaluate_runtime_heartbeat_freshness

CRITICAL_SIGNAL_FIELDS = ("signal_id", "symbol", "mode", "created_at")
CRITICAL_DECISION_FIELDS = ("decision_id", "signal_id", "symbol", "mode", "decision", "created_at")
CRITICAL_LIFECYCLE_FIELDS = ("event_id", "signal_id", "symbol", "mode", "lifecycle_state", "event_ts")


@dataclass(slots=True)
class CheckResult:
    name: str
    passed: bool
    details: str


@dataclass(slots=True)
class QualificationReport:
    qualified: bool
    checks: list[CheckResult]
    generated_at: str
    deployment_state: str
    acknowledgement_required: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "qualified": self.qualified,
            "generated_at": self.generated_at,
            "deployment_state": self.deployment_state,
            "acknowledgement_required": self.acknowledgement_required,
            "checks": [{"name": c.name, "passed": c.passed, "details": c.details} for c in self.checks],
        }


class LiveReadinessEvaluator:
    def __init__(self, engine: Engine, *, reject_rate_bounds: tuple[float, float] = (0.05, 0.98), runtime_heartbeat_max_age_sec: float = DEFAULT_MAX_AGE_SEC) -> None:
        self.engine = engine
        self.reject_rate_bounds = reject_rate_bounds
        self.runtime_heartbeat_max_age_sec = max(1.0, float(runtime_heartbeat_max_age_sec))

    def evaluate(self, *, mode_parity: Mapping[str, Any], reconciliation_snapshot: Mapping[str, Any], observability_snapshot: Mapping[str, Any], canary_enabled: bool, shadow_mode_enabled: bool, operator_ack: bool) -> QualificationReport:
        checks: list[CheckResult] = []
        with self.engine.begin() as conn:
            checks.extend(self._check_lifecycle(conn))
            checks.extend(self._check_persistence(conn))
            checks.extend(self._check_stats(conn))
        checks.append(self._check_runtime_heartbeat())
        checks.extend(self._check_runtime(mode_parity, reconciliation_snapshot))
        checks.extend(self._check_operational(observability_snapshot, canary_enabled, shadow_mode_enabled, operator_ack))
        qualified = all(c.passed for c in checks)
        return QualificationReport(qualified=qualified, checks=checks, generated_at=canonical_utc_timestamp(), deployment_state="LIVE_ENABLED" if qualified and operator_ack else "LIVE_BLOCKED", acknowledgement_required=not operator_ack)

    def persist_report(self, report: QualificationReport) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS live_readiness_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    generated_at TEXT NOT NULL,
                    qualified INTEGER NOT NULL,
                    deployment_state TEXT NOT NULL,
                    acknowledgement_required INTEGER NOT NULL,
                    report_payload TEXT NOT NULL
                )
            """))
            conn.execute(text("""
                INSERT INTO live_readiness_reports(generated_at, qualified, deployment_state, acknowledgement_required, report_payload)
                VALUES (:generated_at, :qualified, :deployment_state, :ack, :payload)
            """), {"generated_at": report.generated_at, "qualified": 1 if report.qualified else 0, "deployment_state": report.deployment_state, "ack": 1 if report.acknowledgement_required else 0, "payload": json.dumps(report.to_dict())})

    def write_forensic_snapshot(self, base_dir: str | Path, report: QualificationReport, runtime_snapshot: Mapping[str, Any]) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = Path(base_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"qualification_snapshot_{ts}.json"
        payload = {"version": "gen5", "timestamp": canonical_utc_timestamp(), "report": report.to_dict(), "runtime_snapshot": self._sanitize_runtime_snapshot(runtime_snapshot)}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def _check_lifecycle(self, conn: Any) -> list[CheckResult]:
        rows = conn.execute(text("SELECT signal_id, lifecycle_state, event_ts, reject_reason FROM trade_lifecycle_events ORDER BY signal_id, event_ts")).mappings().all()
        by_signal: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_signal.setdefault(str(row["signal_id"]), []).append(dict(row))
        orphan_signals = [sid for sid, events in by_signal.items() if events and events[0]["lifecycle_state"] != LifecycleEventType.SIGNAL_CREATED.value]
        invalid_transitions = 0
        reject_missing = 0
        exit_missing = 0
        terminal_states = {LifecycleEventType.TP_HIT.value, LifecycleEventType.SL_HIT.value, LifecycleEventType.CANCELLED.value, LifecycleEventType.OPEN_AT_END.value, LifecycleEventType.RUNTIME_PROTECTIVE_EXIT.value}
        for events in by_signal.values():
            for idx in range(1, len(events)):
                if str(events[idx]["lifecycle_state"]) not in ALLOWED_LIFECYCLE_TRANSITIONS.get(str(events[idx - 1]["lifecycle_state"]), set()):
                    invalid_transitions += 1
            rejected = [e for e in events if e["lifecycle_state"] == LifecycleEventType.SIGNAL_REJECTED.value]
            if rejected and not any((e.get("reject_reason") or "").strip() for e in rejected):
                reject_missing += 1
            if any(e["lifecycle_state"] == LifecycleEventType.ENTRY_TRIGGERED.value for e in events) and not any(e["lifecycle_state"] in terminal_states for e in events):
                exit_missing += 1
        return [CheckResult("lifecycle_no_orphans", not orphan_signals, f"orphan_signals={len(orphan_signals)}"), CheckResult("lifecycle_transitions_valid", invalid_transitions == 0, f"invalid_transitions={invalid_transitions}"), CheckResult("rejected_has_reason", reject_missing == 0, f"missing_reject_reason={reject_missing}"), CheckResult("entry_exit_completeness", exit_missing == 0, f"missing_exit={exit_missing}")]

    def _check_persistence(self, conn: Any) -> list[CheckResult]:
        checks: list[CheckResult] = []
        for table, fields in {"signals": CRITICAL_SIGNAL_FIELDS, "order_decisions": CRITICAL_DECISION_FIELDS, "trade_lifecycle_events": CRITICAL_LIFECYCLE_FIELDS}.items():
            cols = {str(r[1]) for r in conn.execute(text(f"PRAGMA table_info({table})")).all()}
            missing = [field for field in fields if field not in cols]
            checks.append(CheckResult(f"schema_{table}", not missing, f"missing_fields={missing}"))
            null_rows = conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE " + " OR ".join([f"{field} IS NULL" for field in fields]))).scalar_one()
            checks.append(CheckResult(f"critical_not_null_{table}", int(null_rows) == 0, f"null_rows={null_rows}"))
        rejected_decisions = conn.execute(text("SELECT COUNT(*) FROM order_decisions WHERE UPPER(decision)='REJECTED' AND COALESCE(phase,'final')='final'")).scalar_one()
        rejected_events = conn.execute(text("SELECT COUNT(*) FROM trade_lifecycle_events WHERE lifecycle_state='SIGNAL_REJECTED'")).scalar_one()
        checks.append(CheckResult("reject_persistence_parity", int(rejected_decisions) <= int(rejected_events), f"rejected_decisions={rejected_decisions},rejected_events={rejected_events}"))
        return checks

    def _check_stats(self, conn: Any) -> list[CheckResult]:
        total = int(conn.execute(text("SELECT COUNT(*) FROM order_decisions WHERE COALESCE(phase,'final')='final'")).scalar_one())
        rejected = int(conn.execute(text("SELECT COUNT(*) FROM order_decisions WHERE UPPER(decision)='REJECTED' AND COALESCE(phase,'final')='final'")).scalar_one())
        reject_rate = rejected / total if total else 0.0
        min_rr, max_rr = conn.execute(text("SELECT MIN(rr), MAX(rr) FROM order_decisions")).one()
        min_score, max_score = conn.execute(text("SELECT MIN(score), MAX(score) FROM order_decisions")).one()
        lower, upper = self.reject_rate_bounds
        return [CheckResult("reject_rate_sanity", lower <= reject_rate <= upper if total else False, f"reject_rate={reject_rate:.4f},total={total}"), CheckResult("rr_not_constant", min_rr is not None and max_rr is not None and min_rr != max_rr, f"min_rr={min_rr},max_rr={max_rr}"), CheckResult("score_not_constant", min_score is not None and max_score is not None and min_score != max_score, f"min_score={min_score},max_score={max_score}")]

    def _check_runtime_heartbeat(self) -> CheckResult:
        evidence = evaluate_runtime_heartbeat_freshness(self.engine, required_mode="LIVE", max_age_sec=self.runtime_heartbeat_max_age_sec)
        latest = evidence.latest_heartbeat or {}
        details = f"state={evidence.state},reason={evidence.reason},heartbeat_ts={latest.get('heartbeat_ts')},execution_mode={latest.get('execution_mode')},runtime_instance_id={latest.get('runtime_instance_id')},max_age_sec={evidence.max_age_sec}"
        return CheckResult("runtime_heartbeat", evidence.is_fresh, details)

    @staticmethod
    def _parse_non_negative_int(value: Any, *, default: int = 0) -> tuple[int, bool]:
        if value is None or isinstance(value, bool):
            return default, False
        if isinstance(value, int):
            return (value, True) if value >= 0 else (default, False)
        if isinstance(value, float):
            return (int(value), True) if value == value and value >= 0 and value.is_integer() else (default, False)
        if isinstance(value, str):
            parsed = value.strip()
            return (int(parsed), True) if re.fullmatch(r"[0-9]+", parsed) else (default, False)
        return default, False

    def _check_runtime(self, mode_parity: Mapping[str, Any], reconciliation: Mapping[str, Any]) -> list[CheckResult]:
        status = str(mode_parity.get("evidence_status", "INCOMPLETE")).upper() if mode_parity else "INCOMPLETE"
        sample_count, sample_ok = self._parse_non_negative_int(mode_parity.get("sample_count", 0) if mode_parity else 0)
        min_samples, min_ok = self._parse_non_negative_int(mode_parity.get("min_sample_count", 1) if mode_parity else 1, default=1)
        mismatch_count, mismatch_ok = self._parse_non_negative_int(mode_parity.get("mismatch_count", 0) if mode_parity else 0)
        missing_count, missing_ok = self._parse_non_negative_int(mode_parity.get("missing_field_count", 0) if mode_parity else 0)
        no_submit_verified = bool(mode_parity.get("no_submit_verified", mode_parity.get("no_order_submission_verified", False)))
        execution_context_complete = bool(mode_parity.get("execution_context_complete", False))
        execution_evidence_status = str(mode_parity.get("execution_evidence_status", "COMPLETE_MEASURED") or "").upper()
        execution_evidence_blocking = execution_evidence_status in {"UNAVAILABLE_BLOCKING", "INVALID_FAKE_ZERO", "INCOMPLETE", "UNAVAILABLE"}
        parity_ok = status == "COMPLETE" and sample_ok and min_ok and mismatch_ok and missing_ok and sample_count >= min_samples and mismatch_count == 0 and missing_count == 0 and no_submit_verified and execution_context_complete and not execution_evidence_blocking
        configured = bool(reconciliation.get("provider_configured", False))
        evidence_status = str(reconciliation.get("evidence_status") or "INCOMPLETE").upper()
        complete = configured and evidence_status == "COMPLETE"
        no_orphans = int(reconciliation.get("orphan_positions", 0)) == 0 and int(reconciliation.get("orphan_orders", 0)) == 0
        parity_details = "MODE_PARITY_UNVERIFIED" if not parity_ok else f"parity={dict(mode_parity)}"
        if execution_evidence_blocking:
            parity_details = f"LIVE_PRECHECK_EXECUTION_EVIDENCE_BLOCKING:{execution_evidence_status}"
        elif not execution_context_complete:
            parity_details = "LIVE_PRECHECK_EXECUTION_CONTEXT_MISSING"
        elif not no_submit_verified:
            parity_details = "LIVE_PRECHECK_NO_SUBMIT_UNVERIFIED"
        return [CheckResult("mode_parity", parity_ok, parity_details), CheckResult("live_reconciliation_provider", configured, "LIVE_RECONCILIATION_PROVIDER_MISSING" if not configured else "provider_configured=true"), CheckResult("reconciliation_evidence_complete", complete, f"evidence_status={evidence_status}"), CheckResult("reconciliation_no_orphans", complete and no_orphans, f"snapshot={dict(reconciliation)}"), CheckResult("duplicate_execution_free", complete and int(reconciliation.get("duplicate_fills", 0)) == 0, f"duplicate_fills={reconciliation.get('duplicate_fills', 'UNVERIFIED')}"), CheckResult("reconciliation_fail_closed_clear", complete and int(reconciliation.get("fail_closed_findings", 0)) == 0, f"fail_closed_findings={reconciliation.get('fail_closed_findings', 'UNVERIFIED')}")]

    def _check_operational(self, obs: Mapping[str, Any], canary_enabled: bool, shadow_mode_enabled: bool, operator_ack: bool) -> list[CheckResult]:
        stored_alert = latest_persisted_alert_delivery_evidence(self.engine)
        stored_rollback = latest_persisted_rollback_evidence(self.engine)
        effective = {**dict(obs), **stored_alert, **stored_rollback}
        mutation_count, mutation_count_ok = self._parse_non_negative_int(effective.get("execution_mutation_attempt_count"))
        observability_provenance = str(effective.get("observability_evidence_source", "")).upper() == "MEASURED_PROBE" and bool(effective.get("observability_evidence_persisted", False))
        rollback_provenance = str(effective.get("rollback_evidence_source", "")).upper() == "DETERMINISTIC_VALIDATION" and bool(effective.get("rollback_evidence_persisted", False))
        coverage = observability_provenance and bool(effective.get("qualification_persistence_verified", False)) and bool(effective.get("incident_persistence_verified", False)) and bool(effective.get("forensic_export_verified", False)) and bool(effective.get("sensitive_data_redaction_verified", False)) and bool(effective.get("alert_delivery_verified", False)) and str(effective.get("evidence_status", "INCOMPLETE")).upper() == "COMPLETE"
        rollback = rollback_provenance and bool(effective.get("rollback_evidence_verified", False)) and bool(effective.get("kill_switch_block_verified", False)) and bool(effective.get("no_submit_on_kill_switch_verified", False)) and bool(effective.get("fail_closed_reconciliation_verified", False)) and bool(effective.get("repair_actions_non_mutating_verified", False)) and mutation_count_ok and mutation_count == 0 and str(effective.get("rollback_evidence_status", "INCOMPLETE")).upper() == "COMPLETE"
        return [CheckResult("shadow_mode_enabled", shadow_mode_enabled, "shadow mode required"), CheckResult("canary_enabled", canary_enabled, "canary required for controlled enablement"), CheckResult("operator_acknowledged", operator_ack, "explicit operator acknowledgement required"), CheckResult("alert_delivery_evidence", bool(stored_alert.get("alert_delivery_verified", False)), f"alert_evidence={stored_alert}"), CheckResult("observability_coverage", coverage, "OBSERVABILITY_EVIDENCE_UNVERIFIED" if not coverage else f"observability={effective}"), CheckResult("rollback_ready", rollback, f"rollback_evidence={stored_rollback}" if rollback else f"ROLLBACK_EVIDENCE_UNVERIFIED:{stored_rollback.get('rollback_blocking_reasons', [])}")]

    def _sanitize_runtime_snapshot(self, runtime_snapshot: Mapping[str, Any]) -> dict[str, Any]:
        blocked = ("api_" + "key", "api_" + "secret", "secret", "signature", "author" + "ization", "x-mbx-apikey")
        patterns = (re.compile(r"(?i)((?:api[_-]?key|api[_-]?secret|secret|signature|x-mbx-apikey)\s*[=:]\s*)[^&\s,;\"']+"), re.compile(r"(?i)([?&](?:signature|signed(?:_[a-z0-9_]+)?|authorization|x-mbx-apikey|api[_-]?key|api[_-]?secret|secret)=)[^&\s,;\"']+"), re.compile(r"(?i)(authorization\s*[=:]\s*)(?:bearer\s+)?[^,;\r\n\"']+"))
        def clean(value: Any) -> Any:
            if isinstance(value, Mapping):
                result: dict[str, Any] = {}
                for key, item in value.items():
                    name = str(key)
                    lower = name.lower()
                    signed_private = lower == "signed" or (lower.startswith("signed_") and not lower.endswith("_url"))
                    if signed_private or any(token in lower for token in blocked):
                        continue
                    result[name] = clean(item)
                return result
            if isinstance(value, (list, tuple)):
                return [clean(item) for item in value]
            if isinstance(value, str):
                for pattern in patterns:
                    value = pattern.sub(r"\1[REDACTED]", value)
            return value
        return clean(dict(runtime_snapshot))
