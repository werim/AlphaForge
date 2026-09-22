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
from alphaforge.release_gates import release_gate_status
from alphaforge.runtime_heartbeat import DEFAULT_MAX_AGE_SEC, evaluate_runtime_heartbeat_freshness
from alphaforge.runtime_state import latest_runtime_state_snapshot

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
    verdict: str = "NOT_LIVE_READY"
    gates: list[CheckResult] | None = None
    blockers: list[str] | None = None
    readiness_inputs: dict[str, dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        gates = self.gates or []
        blockers = self.blockers or [c.name for c in self.checks if not c.passed]
        return {
            "qualified": self.qualified,
            "verdict": self.verdict,
            "generated_at": self.generated_at,
            "deployment_state": self.deployment_state,
            "acknowledgement_required": self.acknowledgement_required,
            "blockers": blockers,
            "checks": [{"name": c.name, "passed": c.passed, "details": c.details} for c in self.checks],
            "gates": [{"name": c.name, "passed": c.passed, "details": c.details} for c in gates],
            "readiness_inputs": self.readiness_inputs or {},
        }


class LiveReadinessEvaluator:
    def __init__(
        self,
        engine: Engine,
        *,
        reject_rate_bounds: tuple[float, float] = (0.05, 0.98),
        runtime_heartbeat_max_age_sec: float = DEFAULT_MAX_AGE_SEC,
        campaign_id: str | None = None,
        burnin_run_id: str | None = None,
        release_id: str | None = None,
        runtime_instance_id: str | None = None,
        execution_mode: str | None = None,
        strict_scope: bool = False,
    ) -> None:
        self.engine = engine
        self.reject_rate_bounds = reject_rate_bounds
        self.runtime_heartbeat_max_age_sec = max(1.0, float(runtime_heartbeat_max_age_sec))
        self.campaign_id = campaign_id
        self.burnin_run_id = burnin_run_id
        self.release_id = release_id
        self.runtime_instance_id = runtime_instance_id
        self.execution_mode = str(execution_mode or "").strip().upper() or None
        self.strict_scope = bool(strict_scope)

    def _resolved_run_id(self, conn: Any) -> str | None:
        if self.burnin_run_id:
            return self.burnin_run_id
        if self.campaign_id:
            try:
                row = conn.execute(
                    text("SELECT active_run_id FROM burnin_campaigns WHERE campaign_id=:cid"),
                    {"cid": self.campaign_id},
                ).first()
                if row and row[0]:
                    return str(row[0])
            except Exception:
                return None
        return None

    def _scoped_signal_ids(self, conn: Any) -> set[str] | None:
        run_id = self._resolved_run_id(conn)
        if not run_id:
            return set() if self.strict_scope else None
        try:
            rows = conn.execute(
                text("SELECT metrics_json FROM burnin_observations WHERE burnin_run_id=:run_id"),
                {"run_id": run_id},
            ).all()
        except Exception:
            return set()
        signal_ids: set[str] = set()
        for row in rows:
            try:
                metrics = json.loads(row[0] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                metrics = {}
            signal_id = str(metrics.get("signal_id") or "").strip() if isinstance(metrics, Mapping) else ""
            if signal_id:
                signal_ids.add(signal_id)
        return signal_ids

    def _scoped_table_rows(self, conn: Any, table: str) -> list[dict[str, Any]]:
        rows = [dict(row) for row in conn.execute(text(f"SELECT * FROM {table}")).mappings().all()]
        signal_ids = self._scoped_signal_ids(conn)
        if signal_ids is None:
            return rows
        return [row for row in rows if str(row.get("signal_id") or "") in signal_ids]

    def _scoped_decision_evidence_rows(self, conn: Any) -> list[dict[str, Any]]:
        rows = [dict(row) for row in conn.execute(text(
            "SELECT * FROM decision_evidence WHERE UPPER(COALESCE(mode,''))='PAPER'"
        )).mappings().all()]
        run_id = self._resolved_run_id(conn)
        if run_id:
            return [row for row in rows if str(row.get("run_id") or "") == run_id]
        return [] if self.strict_scope else rows

    def evaluate(self, *, mode_parity: Mapping[str, Any], reconciliation_snapshot: Mapping[str, Any], observability_snapshot: Mapping[str, Any], canary_enabled: bool, shadow_mode_enabled: bool, operator_ack: bool, kill_switch_active: bool = False, dashboard_security: Mapping[str, Any] | None = None, timesfm_evidence: Mapping[str, Any] | None = None, paper_burnin_report: Mapping[str, Any] | None = None, tests_passing_evidence: Mapping[str, Any] | None = None) -> QualificationReport:
        checks: list[CheckResult] = []
        with self.engine.begin() as conn:
            checks.extend(self._check_lifecycle(conn))
            checks.extend(self._check_persistence(conn))
            checks.extend(self._check_stats(conn))
        checks.append(self._check_runtime_heartbeat())
        checks.extend(self._check_runtime_state_snapshot())
        checks.extend(self._check_release_gates())
        checks.extend(self._check_runtime(mode_parity, reconciliation_snapshot))
        checks.extend(self._check_operational(observability_snapshot, canary_enabled, shadow_mode_enabled, operator_ack))
        gates = self._aggregate_gates(
            checks,
            mode_parity=mode_parity,
            reconciliation=reconciliation_snapshot,
            observability=observability_snapshot,
            kill_switch_active=kill_switch_active,
            operator_ack=operator_ack,
            dashboard_security=dashboard_security or {},
            timesfm_evidence=timesfm_evidence or {},
            paper_burnin_report=paper_burnin_report or {},
            tests_passing_evidence=tests_passing_evidence or {},
        )
        blockers = [f"{gate.name}:{gate.details}" for gate in gates if not gate.passed]
        verdict = self._verdict_from_gates(gates)
        qualified = False  # Phase 6 never enables real LIVE order readiness; it only permits blocked/canary states.
        return QualificationReport(qualified=qualified, checks=checks, gates=gates, blockers=blockers, verdict=verdict, generated_at=canonical_utc_timestamp(), deployment_state=verdict, acknowledgement_required=not operator_ack)


    @staticmethod
    def _checks_pass(checks: list[CheckResult], names: set[str]) -> bool:
        observed = {check.name: check.passed for check in checks}
        return all(observed.get(name) is True for name in names)

    def _aggregate_gates(self, checks: list[CheckResult], *, mode_parity: Mapping[str, Any], reconciliation: Mapping[str, Any], observability: Mapping[str, Any], kill_switch_active: bool, operator_ack: bool, dashboard_security: Mapping[str, Any], timesfm_evidence: Mapping[str, Any], paper_burnin_report: Mapping[str, Any], tests_passing_evidence: Mapping[str, Any]) -> list[CheckResult]:
        lifecycle = {"lifecycle_no_orphans", "lifecycle_transitions_valid", "entry_exit_completeness"}
        reject = {"rejected_has_reason", "reject_persistence_parity"}
        parity = {"mode_parity"}
        realism = {"rr_not_constant", "score_not_constant", "reject_rate_sanity"}
        reconciliation_checks = {"live_reconciliation_provider", "reconciliation_evidence_complete", "reconciliation_no_orphans", "duplicate_execution_free", "reconciliation_fail_closed_clear"}
        phase5_runtime = {"runtime_state_snapshot_present", "runtime_heartbeat_fresh", "runtime_recovery_not_required", "no_unclean_shutdown_unresolved", "kill_switch_state_persisted", "no_orphan_orders", "no_orphan_positions", "no_stale_pending_orders", "exchange_reconciliation_evidence_present", "exchange_reconciliation_clean", "exchange_read_only_evidence_present", "runtime_db_persistence_verified"}
        operational = {"alert_delivery_evidence", "observability_coverage"}
        rollback = {"rollback_ready"}
        phase6_release = {"phase6_release_gate_evidence"}
        phase3_execution_realism = {"execution_cost_breakdown_present", "effective_rr_available", "execution_rejects_persisted", "no_accepted_trade_with_effective_rr_below_threshold", "no_accepted_trade_with_missing_critical_execution_context", "no_fake_zero_execution_costs"}
        phase4_portfolio_risk = {"portfolio_risk_snapshot_present", "portfolio_risk_rejects_persisted", "no_accepted_trade_over_position_limit", "no_accepted_trade_over_notional_limit", "no_accepted_trade_over_symbol_notional_limit", "no_accepted_trade_after_daily_loss_limit", "no_accepted_trade_with_unknown_portfolio_risk", "correlation_risk_evidence_present", "drawdown_guard_evidence_present", "portfolio_accounting_reconciliation_present", "backtest_and_paper_share_portfolio_risk_engine"}
        gates = [
            CheckResult("lifecycle_integrity_complete", self._checks_pass(checks, lifecycle), "requires lifecycle ordering, no orphans, and terminal completeness"),
            CheckResult("reject_persistence_complete", self._checks_pass(checks, reject), "requires rejected decisions and lifecycle reject reasons persisted"),
            CheckResult("phase2_persisted_evidence_complete", self._checks_pass(checks, {"phase2_decision_evidence_table_exists", "phase2_decision_evidence_rows_present", "phase2_lifecycle_evidence_present", "phase2_reject_evidence_present", "phase2_accept_evidence_present", "phase2_no_fake_zero_execution_evidence", "phase2_no_decision_parity_mismatch"}), "requires SQL-backed lifecycle/reject/accept evidence and no fake-zero/parity blockers"),
            CheckResult("mode_parity_complete", self._checks_pass(checks, parity), "requires BACKTEST/PAPER/LIVE_PRECHECK parity evidence"),
            CheckResult("execution_realism_complete", self._checks_pass(checks, realism), "requires measured selectivity plus non-constant RR/score evidence"),
            CheckResult("phase3_execution_realism_complete", self._checks_pass(checks, phase3_execution_realism), "requires execution cost breakdown, effective RR, execution reject persistence, no fake-zero costs, and no accepted trade with below-threshold/missing execution context"),
            CheckResult("phase4_portfolio_risk_complete", self._checks_pass(checks, phase4_portfolio_risk), "requires portfolio risk snapshots, persisted portfolio rejects, exposure/drawdown/correlation guards, and BACKTEST/PAPER shared engine evidence"),
            CheckResult("effective_rr_penalty_breakdown_complete", bool(mode_parity.get("effective_rr_penalty_breakdown_complete", False) or mode_parity.get("execution_context_complete", False)), "requires persisted execution-context/effective-RR penalty evidence"),
            CheckResult("exchange_connectivity_healthy", bool(reconciliation.get("exchange_connectivity_healthy", False)), "requires measured healthy exchange connectivity; PAPER success is insufficient"),
            CheckResult("authenticated_reconciliation_evidence_complete", self._checks_pass(checks, reconciliation_checks) and bool(reconciliation.get("authenticated", reconciliation.get("authenticated_reconciliation", False))), "requires authenticated read-only reconciliation evidence"),
            CheckResult("no_submit_live_precheck_verified", bool(mode_parity.get("no_submit_verified", mode_parity.get("no_order_submission_verified", False))), "LIVE_PRECHECK must prove no submit/cancel/modify calls"),
            CheckResult("kill_switch_verified", (not kill_switch_active) and self._checks_pass(checks, rollback), "active kill switch or missing deterministic kill-switch evidence blocks LIVE"),
            CheckResult("rollback_operator_controls_verified", self._checks_pass(checks, rollback) and bool(observability.get("repair_actions_non_mutating_verified", False)), "requires rollback/operator controls evidence"),
            CheckResult("heartbeat_alerts_incidents_verified", self._checks_pass(checks, {"runtime_heartbeat"} | operational), "requires fresh LIVE heartbeat, alert delivery, and incident/observability persistence"),
            CheckResult("phase5_runtime_resilience_complete", self._checks_pass(checks, phase5_runtime), "requires persisted runtime snapshot, fresh heartbeat, no recovery/orphans/stale pending orders, and read-only reconciliation evidence"),
            CheckResult("dashboard_rbac_secrets_safe", bool(dashboard_security.get("rbac_verified", False)) and bool(dashboard_security.get("secrets_redacted", False)) and bool(dashboard_security.get("live_switch_fail_closed", False)), "requires dashboard switch/RBAC/secrets safety evidence"),
            CheckResult("timesfm_evidence_safe_non_ordering", bool(timesfm_evidence.get("non_ordering", False)) and not bool(timesfm_evidence.get("satisfies_execution_readiness", False)), "TimesFM evidence may inform research only and cannot satisfy order/execution gates"),
            CheckResult("paper_burnin_report_acceptable", str(paper_burnin_report.get("status", "MISSING")).upper() == "ACCEPTABLE", "PAPER burn-in must be acceptable but never promotes LIVE by itself"),
            CheckResult("full_tests_passing_evidence_recorded", str(tests_passing_evidence.get("status", "MISSING")).upper() == "PASS", "requires current full test evidence"),
            CheckResult("phase6_release_gates_verified", self._checks_pass(checks, phase6_release), "requires Phase 6 release, canary, rollback, runbook, and unexpired operator acknowledgement evidence"),
            CheckResult("operator_acknowledgement_required", bool(operator_ack), "explicit operator acknowledgement is required"),
        ]
        return gates

    @staticmethod
    def _verdict_from_gates(gates: list[CheckResult]) -> str:
        passed = {gate.name: gate.passed for gate in gates}
        lower_gate_names = ["lifecycle_integrity_complete", "reject_persistence_complete", "phase2_persisted_evidence_complete", "mode_parity_complete", "execution_realism_complete", "phase3_execution_realism_complete", "phase4_portfolio_risk_complete", "effective_rr_penalty_breakdown_complete", "no_submit_live_precheck_verified"]
        if not all(passed.get(name, False) for name in lower_gate_names):
            return "NOT_LIVE_READY"
        if not passed.get("kill_switch_verified", False):
            return "NOT_LIVE_READY"
        if not passed.get("phase5_runtime_resilience_complete", False):
            return "NOT_LIVE_READY"
        if not passed.get("phase6_release_gates_verified", False):
            return "NOT_LIVE_READY"
        if all(passed.values()):
            return "LIVE_REAL_ORDERS_BLOCKED"
        if all(value for name, value in passed.items() if name != "operator_acknowledgement_required"):
            return "LIVE_REAL_ORDERS_BLOCKED"
        dry_run_blockers = {"operator_acknowledgement_required", "full_tests_passing_evidence_recorded"}
        if all(value for name, value in passed.items() if name not in dry_run_blockers):
            return "LIVE_DRY_RUN_READY"
        return "LIVE_PRECHECK_READY"

    def persist_report(self, report: QualificationReport) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS live_readiness_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    generated_at TEXT NOT NULL,
                    qualified INTEGER NOT NULL,
                    deployment_state TEXT NOT NULL,
                    acknowledgement_required INTEGER NOT NULL,
                    report_payload TEXT NOT NULL,
                    readiness_inputs_json TEXT
                )
            """))
            cols = {str(r[1]) for r in conn.execute(text("PRAGMA table_info(live_readiness_reports)")).all()}
            if "readiness_inputs_json" not in cols:
                conn.execute(text("ALTER TABLE live_readiness_reports ADD COLUMN readiness_inputs_json TEXT"))
            conn.execute(text("""
                INSERT INTO live_readiness_reports(generated_at, qualified, deployment_state, acknowledgement_required, report_payload, readiness_inputs_json)
                VALUES (:generated_at, :qualified, :deployment_state, :ack, :payload, :inputs)
            """), {"generated_at": report.generated_at, "qualified": 1 if report.qualified else 0, "deployment_state": report.deployment_state, "ack": 1 if report.acknowledgement_required else 0, "payload": json.dumps(report.to_dict()), "inputs": json.dumps(report.readiness_inputs or {}, sort_keys=True)})

    def write_forensic_snapshot(self, base_dir: str | Path, report: QualificationReport, runtime_snapshot: Mapping[str, Any]) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = Path(base_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"qualification_snapshot_{ts}.json"
        payload = {"version": "gen5", "timestamp": canonical_utc_timestamp(), "report": report.to_dict(), "runtime_snapshot": self._sanitize_runtime_snapshot(runtime_snapshot)}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path



    def _check_release_gates(self) -> list[CheckResult]:
        if self.strict_scope and not self.release_id:
            return [CheckResult("phase6_release_gate_evidence", False, "release_id_missing_for_strict_scope")]
        evidence = release_gate_status(
            self.engine,
            release_id=self.release_id or "default",
            phase="PHASE6",
        )
        passed = bool(evidence.get("passed", False))
        status = str(evidence.get("status") or "NO_EVIDENCE")
        details = (
            f"status={status};release_id={evidence.get('release_id')};phase={evidence.get('phase')};"
            f"operator_acknowledged={evidence.get('operator_acknowledged')};"
            f"canary_ready={evidence.get('canary_ready')};"
            f"rollback_verified={evidence.get('rollback_verified')};"
            f"runbook_verified={evidence.get('runbook_verified')};"
            f"mutation_attempt_count={evidence.get('mutation_attempt_count')};"
            f"blocking_reasons={evidence.get('blocking_reasons')}"
        )
        return [CheckResult("phase6_release_gate_evidence", passed, details)]

    def _check_runtime_state_snapshot(self) -> list[CheckResult]:
        try:
            scoped = any((self.campaign_id, self.burnin_run_id, self.release_id, self.runtime_instance_id))
            if scoped or self.strict_scope:
                with self.engine.connect() as conn:
                    run_id = self._resolved_run_id(conn)
                    clauses: list[str] = []
                    params: dict[str, Any] = {}
                    if self.campaign_id:
                        clauses.append("campaign_id=:campaign_id"); params["campaign_id"] = self.campaign_id
                    if run_id:
                        clauses.append("burnin_run_id=:burnin_run_id"); params["burnin_run_id"] = run_id
                    if self.release_id:
                        clauses.append("release_id=:release_id"); params["release_id"] = self.release_id
                    if self.runtime_instance_id:
                        clauses.append("instance_id=:instance_id"); params["instance_id"] = self.runtime_instance_id
                    if self.strict_scope and not clauses:
                        snapshot = None
                    else:
                        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
                        row = conn.execute(text(
                            "SELECT * FROM runtime_state_snapshots" + where + " ORDER BY id DESC LIMIT 1"
                        ), params).mappings().first()
                        snapshot = dict(row) if row else None
                        if snapshot:
                            for key in ("active_symbols","active_positions","pending_orders","cooldown_symbols",
                                        "stale_market_data_symbols","unreconciled_symbols","orphan_orders",
                                        "orphan_positions","runtime_flags","diagnostics_json"):
                                try:
                                    snapshot[key] = json.loads(snapshot.get(key) or ("{}" if key == "diagnostics_json" else "[]"))
                                except (TypeError, ValueError, json.JSONDecodeError):
                                    snapshot[key] = {} if key == "diagnostics_json" else []
            else:
                snapshot = latest_runtime_state_snapshot(self.engine)
        except Exception as exc:
            return [CheckResult("runtime_db_persistence_verified", False, f"runtime_state_query_failed={exc.__class__.__name__}")]
        if not snapshot:
            return [
                CheckResult("runtime_state_snapshot_present", False, "missing runtime_state_snapshots row"),
                CheckResult("runtime_db_persistence_verified", False, "runtime_state_snapshots missing"),
            ]
        status = str(snapshot.get("runtime_status") or "UNKNOWN").upper()
        recon = str(snapshot.get("reconciliation_status") or "UNKNOWN").upper()
        ro = str(snapshot.get("exchange_read_only_status") or "UNKNOWN").upper()
        flags = snapshot.get("runtime_flags") or []
        return [
            CheckResult("runtime_state_snapshot_present", True, f"instance_id={snapshot.get('instance_id')};status={status}"),
            CheckResult("runtime_db_persistence_verified", True, "runtime_state_snapshots readable"),
            CheckResult("runtime_heartbeat_fresh", (snapshot.get("heartbeat_age_sec") is not None and float(snapshot.get("heartbeat_age_sec") or 999999) <= self.runtime_heartbeat_max_age_sec) or status in {"STARTUP","RECONCILED"}, f"heartbeat_age_sec={snapshot.get('heartbeat_age_sec')}"),
            CheckResult("runtime_recovery_not_required", not bool(snapshot.get("recovery_action_required")), f"fail_closed_reason={snapshot.get('fail_closed_reason')}"),
            CheckResult("no_unclean_shutdown_unresolved", "UNCLEAN_SHUTDOWN_RECOVERY_REQUIRED" not in flags and snapshot.get("fail_closed_reason") != "UNCLEAN_SHUTDOWN_RECOVERY_REQUIRED", f"flags={flags}"),
            CheckResult("kill_switch_state_persisted", snapshot.get("kill_switch_active") is not None, f"kill_switch_active={snapshot.get('kill_switch_active')}"),
            CheckResult("no_orphan_orders", int(snapshot.get("orphan_order_count") or 0) == 0, f"orphan_order_count={snapshot.get('orphan_order_count')}"),
            CheckResult("no_orphan_positions", int(snapshot.get("orphan_position_count") or 0) == 0, f"orphan_position_count={snapshot.get('orphan_position_count')}"),
            CheckResult("no_stale_pending_orders", snapshot.get("fail_closed_reason") != "STALE_PENDING_ORDER", f"pending_order_count={snapshot.get('pending_order_count')}"),
            CheckResult("exchange_reconciliation_evidence_present", recon not in {"", "UNKNOWN"}, f"reconciliation_status={recon}"),
            CheckResult("exchange_reconciliation_clean", recon in {"CLEAN", "NOT_REQUIRED_BACKTEST"}, f"reconciliation_status={recon}"),
            CheckResult("exchange_read_only_evidence_present", ro not in {"", "UNKNOWN"}, f"exchange_read_only_status={ro}"),
            CheckResult("backtest_runtime_state_reconciles", recon in {"CLEAN", "NOT_REQUIRED_BACKTEST"}, f"reconciliation_status={recon}"),
            CheckResult("paper_runtime_state_reconciles", recon == "CLEAN", f"reconciliation_status={recon}"),
            CheckResult("live_precheck_no_mutation_verified", True, "readiness consumes persisted snapshot only; no mutation call is made"),
        ]

    def _check_lifecycle(self, conn: Any) -> list[CheckResult]:
        rows = [dict(row) for row in conn.execute(text(
            "SELECT signal_id, lifecycle_state, event_ts, reject_reason FROM trade_lifecycle_events ORDER BY signal_id, event_ts"
        )).mappings().all()]
        signal_ids = self._scoped_signal_ids(conn)
        if signal_ids is not None:
            rows = [row for row in rows if str(row.get("signal_id") or "") in signal_ids]
            if self.strict_scope and not rows:
                detail = "scoped_lifecycle_evidence_missing"
                return [
                    CheckResult("lifecycle_no_orphans", False, detail),
                    CheckResult("lifecycle_transitions_valid", False, detail),
                    CheckResult("lifecycle_error_free", False, detail),
                    CheckResult("rejected_has_reason", False, detail),
                    CheckResult("entry_exit_completeness", False, detail),
                ]
        by_signal: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_signal.setdefault(str(row["signal_id"]), []).append(dict(row))
        orphan_signals = [sid for sid, events in by_signal.items() if events and events[0]["lifecycle_state"] != LifecycleEventType.SIGNAL_CREATED.value]
        invalid_transitions = 0
        lifecycle_errors = 0
        reject_missing = 0
        exit_missing = 0
        terminal_states = {LifecycleEventType.TP_HIT.value, LifecycleEventType.SL_HIT.value, LifecycleEventType.CANCELLED.value, LifecycleEventType.OPEN_AT_END.value, LifecycleEventType.RUNTIME_PROTECTIVE_EXIT.value}
        for events in by_signal.values():
            lifecycle_errors += sum(
                str(event["lifecycle_state"]).upper() in {"ERROR", "EXECUTION_ERROR"}
                for event in events
            )
            for idx in range(1, len(events)):
                if str(events[idx]["lifecycle_state"]) not in ALLOWED_LIFECYCLE_TRANSITIONS.get(str(events[idx - 1]["lifecycle_state"]), set()):
                    invalid_transitions += 1
            rejected = [e for e in events if e["lifecycle_state"] == LifecycleEventType.SIGNAL_REJECTED.value]
            if rejected and not any((e.get("reject_reason") or "").strip() for e in rejected):
                reject_missing += 1
            if any(e["lifecycle_state"] == LifecycleEventType.ENTRY_TRIGGERED.value for e in events) and not any(e["lifecycle_state"] in terminal_states for e in events):
                exit_missing += 1
        return [CheckResult("lifecycle_no_orphans", not orphan_signals, f"orphan_signals={len(orphan_signals)}"), CheckResult("lifecycle_transitions_valid", invalid_transitions == 0, f"invalid_transitions={invalid_transitions}"), CheckResult("lifecycle_error_free", lifecycle_errors == 0, f"lifecycle_errors={lifecycle_errors}"), CheckResult("rejected_has_reason", reject_missing == 0, f"missing_reject_reason={reject_missing}"), CheckResult("entry_exit_completeness", exit_missing == 0, f"missing_exit={exit_missing}")]

    def _check_persistence(self, conn: Any) -> list[CheckResult]:
        checks: list[CheckResult] = []
        scoped_by_table: dict[str, list[dict[str, Any]]] = {}
        for table, fields in {"signals": CRITICAL_SIGNAL_FIELDS, "order_decisions": CRITICAL_DECISION_FIELDS, "trade_lifecycle_events": CRITICAL_LIFECYCLE_FIELDS}.items():
            cols = {str(r[1]) for r in conn.execute(text(f"PRAGMA table_info({table})")).all()}
            missing = [field for field in fields if field not in cols]
            checks.append(CheckResult(f"schema_{table}", not missing, f"missing_fields={missing}"))
            rows = self._scoped_table_rows(conn, table) if not missing else []
            scoped_by_table[table] = rows
            null_rows = sum(any(row.get(field) is None for field in fields) for row in rows)
            checks.append(CheckResult(f"critical_not_null_{table}", not missing and null_rows == 0, f"null_rows={null_rows}"))

        decision_rows = scoped_by_table["order_decisions"]
        lifecycle = scoped_by_table["trade_lifecycle_events"]
        rejected_decisions = sum(
            str(row.get("decision") or "").upper() == "REJECTED"
            and str(row.get("phase") or "final") == "final"
            for row in decision_rows
        )
        rejected_events = sum(str(row.get("lifecycle_state") or "") == "SIGNAL_REJECTED" for row in lifecycle)
        checks.append(CheckResult(
            "reject_persistence_parity",
            rejected_decisions <= rejected_events,
            f"rejected_decisions={rejected_decisions},rejected_events={rejected_events}",
        ))
        lifecycle_rows = len(lifecycle)
        accepted_states = {
            "SIGNAL_ACCEPTED","WAITING_ENTRY_ZONE","ENTRY_TRIGGERED","ORDER_PLACED",
            "POSITION_OPENED","POSITION_CLOSED","TP_HIT","SL_HIT","OPEN_AT_END",
            "CANCELLED","ENTRY_TIMEOUT",
        }
        accepted_events = len({
            str(row.get("signal_id") or "")
            for row in lifecycle
            if str(row.get("lifecycle_state") or "") in accepted_states and row.get("signal_id")
        })

        decision_evidence_exists = conn.execute(text(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='decision_evidence'"
        )).scalar_one() > 0
        portfolio_cols: list[str] = []
        evidence: list[dict[str, Any]] = []
        backtest_evidence: list[dict[str, Any]] = []
        if decision_evidence_exists:
            portfolio_cols = [str(r[1]) for r in conn.execute(text("PRAGMA table_info(decision_evidence)")).all()]
            evidence = self._scoped_decision_evidence_rows(conn)
            backtest_evidence = [dict(row) for row in conn.execute(text(
                "SELECT * FROM decision_evidence WHERE UPPER(COALESCE(mode,''))='BACKTEST'"
            )).mappings().all()]

        accepted_values = {"ACCEPT","ACCEPTED"}
        rejected_values = {"REJECT","REJECTED"}
        evidence_rows = len(evidence)
        evidence_lifecycle_states = len({
            str(row.get("lifecycle_state_after") or "") for row in evidence
            if str(row.get("lifecycle_state_after") or "")
        })
        evidence_accepted = sum(str(row.get("decision") or "").upper() in accepted_values for row in evidence)
        evidence_rejected = sum(str(row.get("decision") or "").upper() in rejected_values for row in evidence)
        evidence_parity_rows = sum(
            str(row.get("reject_reason") or "").upper() == "DECISION_PARITY_MISMATCH"
            or "DECISION_PARITY_MISMATCH" in str(row.get("diagnostics_json") or "").upper()
            for row in evidence
        )
        fake_keys = ("spread_pct","expected_slippage_pct","funding_rate_pct","volume_24h_usdt","liquidity_score")
        evidence_fake_zero_rows = sum(
            "UNAVAILABLE" in str(row.get("diagnostics_json") or "").upper()
            and any(row.get(key) == 0 for key in fake_keys)
            for row in evidence
        )
        phase3_breakdown_rows = sum(
            row.get("cost_penalty") is not None
            and ("spread_penalty" in str(row.get("diagnostics_json") or "")
                 or "cost_penalty" in str(row.get("diagnostics_json") or ""))
            for row in evidence
        )
        phase3_effective_rr_rows = sum(
            row.get("raw_rr") is not None and row.get("effective_rr") is not None
            for row in evidence
        )
        execution_rejects = {
            "LOW_EFFECTIVE_RR","HIGH_SPREAD","HIGH_SLIPPAGE","HIGH_TOTAL_COST",
            "LOW_LIQUIDITY","HIGH_LATENCY","EXECUTION_CONTEXT_UNAVAILABLE",
            "EXCESSIVE_VOLATILITY_PENALTY","FUNDING_UNAVAILABLE","FUNDING_TOO_HIGH",
        }
        phase3_execution_reject_rows = sum(
            str(row.get("reject_reason") or "").upper() in execution_rejects
            for row in evidence
        )
        phase3_missing_critical_accepted = sum(
            str(row.get("decision") or "").upper() in accepted_values
            and any(row.get(key) is None for key in (
                "effective_rr","cost_penalty","spread_pct","expected_slippage_pct","liquidity_score"
            ))
            for row in evidence
        )
        phase3_low_effective_accepted = sum(
            str(row.get("decision") or "").upper() in accepted_values
            and row.get("effective_rr") is not None
            and float(row["effective_rr"]) < 1.6
            for row in evidence
        )
        parity_rows = sum(
            str(row.get("reject_reason") or "").upper() == "DECISION_PARITY_MISMATCH"
            or str(row.get("parity_result") or "").upper() == "DECISION_PARITY_MISMATCH"
            for row in decision_rows
        ) + evidence_parity_rows
        fake_zero_rows = sum(
            int(row.get("execution_ctx_missing") or 0) == 1
            and "UNAVAILABLE" in str(row.get("execution_ctx") or "").upper()
            and any(row.get(key) == 0 for key in ("spread_pct","expected_slippage_pct","funding_rate_pct"))
            for row in decision_rows
        ) + evidence_fake_zero_rows

        checks.append(CheckResult("phase2_decision_evidence_table_exists", decision_evidence_exists, f"decision_evidence_exists={decision_evidence_exists}"))
        checks.append(CheckResult("phase2_decision_evidence_rows_present", evidence_rows > 0, f"decision_evidence_rows={evidence_rows}"))
        checks.append(CheckResult("phase2_lifecycle_evidence_present", lifecycle_rows > 0 and evidence_lifecycle_states > 0, f"lifecycle_rows={lifecycle_rows},decision_evidence_lifecycle_states={evidence_lifecycle_states}"))
        checks.append(CheckResult("phase2_reject_evidence_present", rejected_decisions > 0 and rejected_events > 0 and evidence_rejected > 0, f"rejected_decisions={rejected_decisions},rejected_events={rejected_events},decision_evidence_rejected={evidence_rejected}"))
        checks.append(CheckResult("phase2_accept_evidence_present", accepted_events > 0 and evidence_accepted > 0, f"accepted_signal_ids={accepted_events},decision_evidence_accepted={evidence_accepted}"))
        checks.append(CheckResult("phase2_no_fake_zero_execution_evidence", fake_zero_rows == 0, f"fake_zero_execution_rows={fake_zero_rows}"))
        checks.append(CheckResult("phase2_no_decision_parity_mismatch", parity_rows == 0, f"decision_parity_mismatch_rows={parity_rows}"))
        checks.append(CheckResult("execution_cost_breakdown_present", phase3_breakdown_rows > 0, f"breakdown_rows={phase3_breakdown_rows}"))
        checks.append(CheckResult("effective_rr_available", phase3_effective_rr_rows > 0, f"effective_rr_rows={phase3_effective_rr_rows}"))
        checks.append(CheckResult("execution_rejects_persisted", phase3_execution_reject_rows > 0, f"execution_reject_rows={phase3_execution_reject_rows},evidence_rejected={evidence_rejected}"))
        checks.append(CheckResult("no_accepted_trade_with_effective_rr_below_threshold", phase3_low_effective_accepted == 0, f"low_effective_accepted={phase3_low_effective_accepted}"))
        checks.append(CheckResult("no_accepted_trade_with_missing_critical_execution_context", phase3_missing_critical_accepted == 0, f"missing_critical_accepted={phase3_missing_critical_accepted}"))

        has_portfolio_cols = {"portfolio_equity","open_position_count","total_notional_exposure","portfolio_risk_state","portfolio_diagnostics_json"}.issubset(set(portfolio_cols))
        if decision_evidence_exists and has_portfolio_cols:
            portfolio_snapshot_rows = sum(
                row.get("portfolio_risk_state") is not None or row.get("portfolio_diagnostics_json") is not None
                for row in evidence
            )
            portfolio_reject_rows = sum(bool(str(row.get("portfolio_reject_reason") or "").strip()) for row in evidence)
            accepted_over_position = sum(
                str(row.get("decision") or "").upper() in accepted_values
                and row.get("max_open_positions") is not None
                and row.get("open_position_count") is not None
                and float(row["open_position_count"]) > float(row["max_open_positions"])
                for row in evidence
            ) if "max_open_positions" in portfolio_cols else 1
            accepted_over_notional = sum(
                str(row.get("decision") or "").upper() in accepted_values
                and row.get("max_notional_exposure") is not None
                and row.get("total_notional_exposure") is not None
                and float(row["total_notional_exposure"]) > float(row["max_notional_exposure"])
                for row in evidence
            )
            accepted_over_symbol = sum(
                str(row.get("decision") or "").upper() in accepted_values
                and row.get("max_symbol_notional") is not None
                and row.get("symbol_notional_exposure") is not None
                and float(row["symbol_notional_exposure"]) > float(row["max_symbol_notional"])
                for row in evidence
            )
            accepted_after_daily_loss = sum(
                str(row.get("decision") or "").upper() in accepted_values
                and row.get("max_daily_loss_pct") is not None
                and row.get("daily_loss_pct") is not None
                and float(row["daily_loss_pct"]) >= float(row["max_daily_loss_pct"])
                for row in evidence
            )
            accepted_unknown = sum(
                str(row.get("decision") or "").upper() in accepted_values
                and "UNKNOWN" in str(row.get("portfolio_risk_state") or "").upper()
                for row in evidence
            )
            correlation_rows = sum(
                row.get("correlation_group") is not None or row.get("correlated_position_count") is not None
                for row in evidence
            )
            drawdown_rows = sum(
                row.get("rolling_drawdown_pct") is not None or row.get("daily_loss_pct") is not None
                for row in evidence
            )
            reconcile_rows = sum(
                row.get("open_position_count") is not None and row.get("total_notional_exposure") is not None
                for row in evidence
            )
            accounting_states = {
                (row.get("open_position_count"),row.get("total_notional_exposure"),row.get("portfolio_equity"))
                for row in evidence
                if row.get("open_position_count") is not None and row.get("total_notional_exposure") is not None
            }
            accounting_distinct_states = len(accounting_states)
            current_paper_portfolio = any(row.get("portfolio_risk_state") is not None for row in evidence)
            backtest_portfolio = any(row.get("portfolio_risk_state") is not None for row in backtest_evidence)
            shared_engine_rows = int(current_paper_portfolio) + int(backtest_portfolio)
        else:
            portfolio_snapshot_rows = portfolio_reject_rows = correlation_rows = drawdown_rows = reconcile_rows = shared_engine_rows = accounting_distinct_states = 0
            accepted_over_position = accepted_over_notional = accepted_over_symbol = accepted_after_daily_loss = accepted_unknown = 1

        checks.append(CheckResult("no_fake_zero_execution_costs", fake_zero_rows == 0, f"fake_zero_execution_rows={fake_zero_rows}"))
        checks.append(CheckResult("portfolio_risk_snapshot_present", portfolio_snapshot_rows > 0, f"portfolio_snapshot_rows={portfolio_snapshot_rows},portfolio_columns={has_portfolio_cols}"))
        checks.append(CheckResult("portfolio_risk_rejects_persisted", portfolio_reject_rows > 0, f"portfolio_reject_rows={portfolio_reject_rows}"))
        checks.append(CheckResult("no_accepted_trade_over_position_limit", accepted_over_position == 0, f"accepted_over_position={accepted_over_position}"))
        checks.append(CheckResult("no_accepted_trade_over_notional_limit", accepted_over_notional == 0, f"accepted_over_notional={accepted_over_notional}"))
        checks.append(CheckResult("no_accepted_trade_over_symbol_notional_limit", accepted_over_symbol == 0, f"accepted_over_symbol={accepted_over_symbol}"))
        checks.append(CheckResult("no_accepted_trade_after_daily_loss_limit", accepted_after_daily_loss == 0, f"accepted_after_daily_loss={accepted_after_daily_loss}"))
        checks.append(CheckResult("no_accepted_trade_with_unknown_portfolio_risk", accepted_unknown == 0, f"accepted_unknown_portfolio_risk={accepted_unknown}"))
        checks.append(CheckResult("correlation_risk_evidence_present", correlation_rows > 0, f"correlation_rows={correlation_rows}"))
        checks.append(CheckResult("drawdown_guard_evidence_present", drawdown_rows > 0, f"drawdown_rows={drawdown_rows}"))
        checks.append(CheckResult("portfolio_accounting_reconciliation_present", reconcile_rows > 0 and accounting_distinct_states > 1, f"reconcile_rows={reconcile_rows},distinct_accounting_states={accounting_distinct_states}"))
        checks.append(CheckResult("backtest_and_paper_share_portfolio_risk_engine", shared_engine_rows >= 2, f"modes_with_portfolio_risk={shared_engine_rows}"))
        return checks

    def _check_stats(self, conn: Any) -> list[CheckResult]:
        rows = self._scoped_table_rows(conn, "order_decisions")
        final_rows = [row for row in rows if str(row.get("phase") or "final") == "final"]
        total = len(final_rows)
        rejected = sum(str(row.get("decision") or "").upper() == "REJECTED" for row in final_rows)
        reject_rate = rejected / total if total else 0.0
        rr_values = [float(row["rr"]) for row in rows if row.get("rr") is not None]
        score_values = [float(row["score"]) for row in rows if row.get("score") is not None]
        min_rr, max_rr = (min(rr_values), max(rr_values)) if rr_values else (None, None)
        min_score, max_score = (min(score_values), max(score_values)) if score_values else (None, None)
        lower, upper = self.reject_rate_bounds
        return [CheckResult("reject_rate_sanity", lower <= reject_rate <= upper if total else False, f"reject_rate={reject_rate:.4f},total={total}"), CheckResult("rr_not_constant", min_rr is not None and max_rr is not None and min_rr != max_rr, f"min_rr={min_rr},max_rr={max_rr}"), CheckResult("score_not_constant", min_score is not None and max_score is not None and min_score != max_score, f"min_score={min_score},max_score={max_score}")]

    def _check_runtime_heartbeat(self) -> CheckResult:
        evidence = evaluate_runtime_heartbeat_freshness(
            self.engine,
            required_mode=self.execution_mode or "LIVE",
            runtime_instance_id=self.runtime_instance_id if self.strict_scope else None,
            max_age_sec=self.runtime_heartbeat_max_age_sec,
        )
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

if __name__ == "__main__":
    # v1 is isolated from this legacy evaluator's persistence API.
    from alphaforge.live_readiness_agent import main as _read_only_main
    raise SystemExit(_read_only_main())
