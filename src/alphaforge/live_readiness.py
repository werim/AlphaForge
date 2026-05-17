from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Engine

from alphaforge.contracts import ALLOWED_LIFECYCLE_TRANSITIONS, LifecycleEventType, canonical_utc_timestamp

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
    def __init__(self, engine: Engine, *, reject_rate_bounds: tuple[float, float] = (0.05, 0.98)) -> None:
        self.engine = engine
        self.reject_rate_bounds = reject_rate_bounds

    def evaluate(
        self,
        *,
        mode_parity: Mapping[str, bool],
        reconciliation_snapshot: Mapping[str, Any],
        observability_snapshot: Mapping[str, Any],
        canary_enabled: bool,
        shadow_mode_enabled: bool,
        operator_ack: bool,
    ) -> QualificationReport:
        checks: list[CheckResult] = []
        with self.engine.begin() as conn:
            checks.extend(self._check_lifecycle(conn))
            checks.extend(self._check_persistence(conn))
            checks.extend(self._check_stats(conn))

        checks.extend(self._check_runtime(mode_parity, reconciliation_snapshot))
        checks.extend(self._check_operational(observability_snapshot, canary_enabled, shadow_mode_enabled, operator_ack))

        qualified = all(c.passed for c in checks)
        deployment_state = "LIVE_ENABLED" if qualified and operator_ack else "LIVE_BLOCKED"
        return QualificationReport(
            qualified=qualified,
            checks=checks,
            generated_at=canonical_utc_timestamp(),
            deployment_state=deployment_state,
            acknowledgement_required=not operator_ack,
        )

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
            """), {
                "generated_at": report.generated_at,
                "qualified": 1 if report.qualified else 0,
                "deployment_state": report.deployment_state,
                "ack": 1 if report.acknowledgement_required else 0,
                "payload": json.dumps(report.to_dict()),
            })

    def write_forensic_snapshot(self, base_dir: str | Path, report: QualificationReport, runtime_snapshot: Mapping[str, Any]) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = Path(base_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"qualification_snapshot_{ts}.json"
        payload = {
            "version": "gen5",
            "timestamp": canonical_utc_timestamp(),
            "report": report.to_dict(),
            "runtime_snapshot": dict(runtime_snapshot),
        }
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
        for events in by_signal.values():
            for idx in range(1, len(events)):
                prev = str(events[idx - 1]["lifecycle_state"])
                nxt = str(events[idx]["lifecycle_state"])
                if nxt not in ALLOWED_LIFECYCLE_TRANSITIONS.get(prev, set()):
                    invalid_transitions += 1
            has_reject = any(e["lifecycle_state"] == LifecycleEventType.SIGNAL_REJECTED.value for e in events)
            if has_reject and not any((e.get("reject_reason") or "").strip() for e in events if e["lifecycle_state"] == LifecycleEventType.SIGNAL_REJECTED.value):
                reject_missing += 1
            if any(e["lifecycle_state"] == LifecycleEventType.ENTRY_TRIGGERED.value for e in events):
                if not any(e["lifecycle_state"] in {LifecycleEventType.TP_HIT.value, LifecycleEventType.SL_HIT.value, LifecycleEventType.CANCELLED.value, LifecycleEventType.OPEN_AT_END.value, LifecycleEventType.RUNTIME_PROTECTIVE_EXIT.value} for e in events):
                    exit_missing += 1

        return [
            CheckResult("lifecycle_no_orphans", not orphan_signals, f"orphan_signals={len(orphan_signals)}"),
            CheckResult("lifecycle_transitions_valid", invalid_transitions == 0, f"invalid_transitions={invalid_transitions}"),
            CheckResult("rejected_has_reason", reject_missing == 0, f"missing_reject_reason={reject_missing}"),
            CheckResult("entry_exit_completeness", exit_missing == 0, f"missing_exit={exit_missing}"),
        ]

    def _check_persistence(self, conn: Any) -> list[CheckResult]:
        checks: list[CheckResult] = []
        tables = {
            "signals": CRITICAL_SIGNAL_FIELDS,
            "order_decisions": CRITICAL_DECISION_FIELDS,
            "trade_lifecycle_events": CRITICAL_LIFECYCLE_FIELDS,
        }
        for table, fields in tables.items():
            cols = {str(r[1]) for r in conn.execute(text(f"PRAGMA table_info({table})")).all()}
            missing = [f for f in fields if f not in cols]
            checks.append(CheckResult(f"schema_{table}", len(missing) == 0, f"missing_fields={missing}"))
            null_row = conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE " + " OR ".join([f"{f} IS NULL" for f in fields]))).scalar_one()
            checks.append(CheckResult(f"critical_not_null_{table}", int(null_row) == 0, f"null_rows={null_row}"))

        rejected_decisions = conn.execute(text("SELECT COUNT(*) FROM order_decisions WHERE UPPER(decision)='REJECTED'" )).scalar_one()
        rejected_events = conn.execute(text("SELECT COUNT(*) FROM trade_lifecycle_events WHERE lifecycle_state='SIGNAL_REJECTED'" )).scalar_one()
        checks.append(CheckResult("reject_persistence_parity", int(rejected_decisions) <= int(rejected_events), f"rejected_decisions={rejected_decisions},rejected_events={rejected_events}"))
        return checks

    def _check_stats(self, conn: Any) -> list[CheckResult]:
        total = int(conn.execute(text("SELECT COUNT(*) FROM order_decisions")).scalar_one())
        rejected = int(conn.execute(text("SELECT COUNT(*) FROM order_decisions WHERE UPPER(decision)='REJECTED'" )).scalar_one())
        reject_rate = (rejected / total) if total else 0.0
        min_rr, max_rr = conn.execute(text("SELECT MIN(rr), MAX(rr) FROM order_decisions")).one()
        min_score, max_score = conn.execute(text("SELECT MIN(score), MAX(score) FROM order_decisions")).one()
        lower, upper = self.reject_rate_bounds
        return [
            CheckResult("reject_rate_sanity", lower <= reject_rate <= upper if total else False, f"reject_rate={reject_rate:.4f},total={total}"),
            CheckResult("rr_not_constant", (min_rr is not None and max_rr is not None and min_rr != max_rr), f"min_rr={min_rr},max_rr={max_rr}"),
            CheckResult("score_not_constant", (min_score is not None and max_score is not None and min_score != max_score), f"min_score={min_score},max_score={max_score}"),
        ]

    def _check_runtime(self, mode_parity: Mapping[str, bool], reconciliation: Mapping[str, Any]) -> list[CheckResult]:
        parity_ok = all(bool(v) for v in mode_parity.values()) if mode_parity else False
        checks = [CheckResult("mode_parity", parity_ok, f"parity={dict(mode_parity)}")]
        checks.append(CheckResult("reconciliation_no_orphans", int(reconciliation.get("orphan_positions", 0)) == 0 and int(reconciliation.get("orphan_orders", 0)) == 0, f"snapshot={dict(reconciliation)}"))
        checks.append(CheckResult("duplicate_execution_free", int(reconciliation.get("duplicate_fills", 0)) == 0, f"duplicate_fills={reconciliation.get('duplicate_fills', 0)}"))
        return checks

    def _check_operational(self, obs: Mapping[str, Any], canary_enabled: bool, shadow_mode_enabled: bool, operator_ack: bool) -> list[CheckResult]:
        coverage = bool(obs.get("alerts_configured", False)) and bool(obs.get("forensic_exports", False))
        rollback = bool(obs.get("rollback_ready", False))
        return [
            CheckResult("shadow_mode_enabled", shadow_mode_enabled, "shadow mode required"),
            CheckResult("canary_enabled", canary_enabled, "canary required for controlled enablement"),
            CheckResult("operator_acknowledged", operator_ack, "explicit operator acknowledgement required"),
            CheckResult("observability_coverage", coverage, f"observability={dict(obs)}"),
            CheckResult("rollback_ready", rollback, f"rollback_ready={rollback}"),
        ]
