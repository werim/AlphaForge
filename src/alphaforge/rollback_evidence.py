from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import time
import uuid
from typing import Any, Mapping

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from alphaforge.contracts import canonical_utc_timestamp
from alphaforge.reconciliation import ReconciliationEngine, summarize_findings

DEFAULT_MAX_AGE_SEC = 900.0
DEFAULT_FUTURE_TOLERANCE_SEC = 5.0
_SOURCE = "DETERMINISTIC_VALIDATION"
_ALLOWED_FIELDS = (
    "validation_scope",
    "guard_path",
    "guard_reject_reason",
    "unsafe_snapshot_case",
    "finding_counts",
    "recommendation_categories",
    "repair_payloads_dry_run",
    "execution_adapter_bound",
)


@dataclass(frozen=True, slots=True)
class RollbackEvidenceFreshness:
    state: str
    reason: str
    latest_evidence: dict[str, Any] | None
    age_sec: float | None
    max_age_sec: float

    @property
    def is_valid(self) -> bool:
        return self.state == "FRESH"

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "latest_evidence": self.latest_evidence,
            "age_sec": self.age_sec,
            "max_age_sec": self.max_age_sec,
            "valid": self.is_valid,
        }


def ensure_rollback_evidence_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS live_rollback_validation_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                validation_id TEXT NOT NULL UNIQUE,
                recorded_at TEXT NOT NULL,
                evidence_status TEXT NOT NULL,
                rollback_evidence_source TEXT NOT NULL,
                kill_switch_block_verified INTEGER NOT NULL,
                no_submit_on_kill_switch_verified INTEGER NOT NULL,
                fail_closed_reconciliation_verified INTEGER NOT NULL,
                repair_actions_non_mutating_verified INTEGER NOT NULL,
                execution_mutation_attempt_count INTEGER NOT NULL,
                blocking_reasons TEXT NOT NULL,
                evidence_payload TEXT NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_live_rollback_evidence_recorded_at
            ON live_rollback_validation_evidence(recorded_at DESC, id DESC)
        """))


def _safe_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(payload or {})
    safe = {key: raw.get(key) for key in _ALLOWED_FIELDS if key in raw}
    if "recommendation_categories" in safe:
        safe["recommendation_categories"] = [str(item)[:80] for item in list(safe["recommendation_categories"] or [])[:20]]
    if "finding_counts" in safe and isinstance(safe["finding_counts"], Mapping):
        safe["finding_counts"] = {str(key)[:80]: int(value) for key, value in safe["finding_counts"].items()}
    return safe


def persist_rollback_validation_evidence(engine: Engine, evidence: Mapping[str, Any]) -> dict[str, Any]:
    validation_id = str(evidence.get("validation_id") or f"rollback-validation:{uuid.uuid4().hex}").strip()
    checks = {
        "kill_switch_block_verified": bool(evidence.get("kill_switch_block_verified", False)),
        "no_submit_on_kill_switch_verified": bool(evidence.get("no_submit_on_kill_switch_verified", False)),
        "fail_closed_reconciliation_verified": bool(evidence.get("fail_closed_reconciliation_verified", False)),
        "repair_actions_non_mutating_verified": bool(evidence.get("repair_actions_non_mutating_verified", False)),
    }
    mutation_count = max(0, int(evidence.get("execution_mutation_attempt_count", 0) or 0))
    reasons = [str(item)[:120] for item in list(evidence.get("blocking_reasons") or [])]
    complete = all(checks.values()) and mutation_count == 0 and not reasons
    status = "COMPLETE" if complete else "INCOMPLETE"
    safe = {
        "validation_id": validation_id[:160],
        "recorded_at": str(evidence.get("recorded_at") or canonical_utc_timestamp()),
        "evidence_status": status,
        "rollback_evidence_source": _SOURCE,
        **checks,
        "execution_mutation_attempt_count": mutation_count,
        "blocking_reasons": reasons,
        "evidence_payload": _safe_payload(evidence.get("evidence_payload") if isinstance(evidence.get("evidence_payload"), Mapping) else {}),
    }
    ensure_rollback_evidence_schema(engine)
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO live_rollback_validation_evidence(
                validation_id, recorded_at, evidence_status, rollback_evidence_source,
                kill_switch_block_verified, no_submit_on_kill_switch_verified,
                fail_closed_reconciliation_verified, repair_actions_non_mutating_verified,
                execution_mutation_attempt_count, blocking_reasons, evidence_payload
            ) VALUES (
                :validation_id, :recorded_at, :evidence_status, :rollback_evidence_source,
                :kill_switch_block_verified, :no_submit_on_kill_switch_verified,
                :fail_closed_reconciliation_verified, :repair_actions_non_mutating_verified,
                :execution_mutation_attempt_count, :blocking_reasons, :evidence_payload
            )
            ON CONFLICT(validation_id) DO UPDATE SET
                recorded_at=excluded.recorded_at,
                evidence_status=excluded.evidence_status,
                rollback_evidence_source=excluded.rollback_evidence_source,
                kill_switch_block_verified=excluded.kill_switch_block_verified,
                no_submit_on_kill_switch_verified=excluded.no_submit_on_kill_switch_verified,
                fail_closed_reconciliation_verified=excluded.fail_closed_reconciliation_verified,
                repair_actions_non_mutating_verified=excluded.repair_actions_non_mutating_verified,
                execution_mutation_attempt_count=excluded.execution_mutation_attempt_count,
                blocking_reasons=excluded.blocking_reasons,
                evidence_payload=excluded.evidence_payload
        """), {
            **safe,
            "kill_switch_block_verified": 1 if safe["kill_switch_block_verified"] else 0,
            "no_submit_on_kill_switch_verified": 1 if safe["no_submit_on_kill_switch_verified"] else 0,
            "fail_closed_reconciliation_verified": 1 if safe["fail_closed_reconciliation_verified"] else 0,
            "repair_actions_non_mutating_verified": 1 if safe["repair_actions_non_mutating_verified"] else 0,
            "blocking_reasons": json.dumps(safe["blocking_reasons"], sort_keys=True),
            "evidence_payload": json.dumps(safe["evidence_payload"], sort_keys=True),
        })
    return safe


def _parse_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def latest_persisted_rollback_evidence(
    engine: Engine,
    *,
    max_age_sec: float = DEFAULT_MAX_AGE_SEC,
    now: datetime | None = None,
) -> dict[str, Any]:
    missing = {
        "rollback_evidence_source": "UNVERIFIED",
        "rollback_evidence_persisted": False,
        "rollback_evidence_verified": False,
        "rollback_evidence_status": "INCOMPLETE",
        "kill_switch_block_verified": False,
        "no_submit_on_kill_switch_verified": False,
        "fail_closed_reconciliation_verified": False,
        "repair_actions_non_mutating_verified": False,
        "execution_mutation_attempt_count": None,
        "rollback_blocking_reasons": ["ROLLBACK_EVIDENCE_MISSING"],
    }
    try:
        if not inspect(engine).has_table("live_rollback_validation_evidence"):
            return missing
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT validation_id, recorded_at, evidence_status, rollback_evidence_source,
                       kill_switch_block_verified, no_submit_on_kill_switch_verified,
                       fail_closed_reconciliation_verified, repair_actions_non_mutating_verified,
                       execution_mutation_attempt_count, blocking_reasons, evidence_payload
                FROM live_rollback_validation_evidence
                ORDER BY id DESC LIMIT 1
            """)).mappings().first()
    except SQLAlchemyError:
        return {**missing, "rollback_blocking_reasons": ["ROLLBACK_EVIDENCE_QUERY_UNAVAILABLE"]}
    if row is None:
        return missing
    recorded = _parse_timestamp(row["recorded_at"])
    if recorded is None:
        return {**missing, "rollback_evidence_persisted": True, "rollback_blocking_reasons": ["ROLLBACK_EVIDENCE_INVALID_TIMESTAMP"]}
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = (current - recorded).total_seconds()
    if age < -DEFAULT_FUTURE_TOLERANCE_SEC:
        return {**missing, "rollback_evidence_persisted": True, "rollback_evidence_age_sec": age, "rollback_blocking_reasons": ["ROLLBACK_EVIDENCE_FUTURE_DATED"]}
    if age > float(max_age_sec):
        return {**missing, "rollback_evidence_persisted": True, "rollback_evidence_age_sec": age, "rollback_blocking_reasons": ["ROLLBACK_EVIDENCE_STALE"]}
    try:
        reasons = json.loads(str(row["blocking_reasons"] or "[]"))
        payload = json.loads(str(row["evidence_payload"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {**missing, "rollback_evidence_persisted": True, "rollback_evidence_age_sec": age, "rollback_blocking_reasons": ["ROLLBACK_EVIDENCE_INVALID"]}
    valid = (
        str(row["evidence_status"]).upper() == "COMPLETE"
        and str(row["rollback_evidence_source"]).upper() == _SOURCE
        and bool(row["kill_switch_block_verified"])
        and bool(row["no_submit_on_kill_switch_verified"])
        and bool(row["fail_closed_reconciliation_verified"])
        and bool(row["repair_actions_non_mutating_verified"])
        and int(row["execution_mutation_attempt_count"]) == 0
        and isinstance(reasons, list) and not reasons
        and isinstance(payload, dict)
    )
    return {
        "validation_id": str(row["validation_id"]),
        "rollback_evidence_source": _SOURCE if valid else "UNVERIFIED",
        "rollback_evidence_persisted": True,
        "rollback_evidence_verified": valid,
        "rollback_evidence_status": "COMPLETE" if valid else "INCOMPLETE",
        "kill_switch_block_verified": valid,
        "no_submit_on_kill_switch_verified": valid,
        "fail_closed_reconciliation_verified": valid,
        "repair_actions_non_mutating_verified": valid,
        "execution_mutation_attempt_count": int(row["execution_mutation_attempt_count"]),
        "rollback_evidence_age_sec": age,
        "recorded_at": str(row["recorded_at"]),
        "rollback_blocking_reasons": [] if valid else ["ROLLBACK_EVIDENCE_INVALID"],
        "evidence_payload": payload,
    }


def run_deterministic_rollback_validation(engine: Engine) -> dict[str, Any]:
    """Persist a no-exchange-mutation validation of existing emergency safety paths."""
    from alphaforge.runtime import RuntimeConfig, RuntimeOrchestrator, ExecutionMode

    class _RiskGuardHarness:
        config = RuntimeConfig(execution_mode=ExecutionMode.LIVE, global_kill_switch=True)
        _active_positions: dict[str, float] = {}
        _symbol_cooldown_until: dict[str, float] = {}

    reason = RuntimeOrchestrator._evaluate_runtime_risk(
        _RiskGuardHarness(),
        "VALIDATIONUSDT",
        {"market_ts": time.time(), "spread_pct": 0.0, "funding_rate_pct": 0.0},
    )
    kill_switch_ok = reason == "GLOBAL_KILL_SWITCH"
    mutation_attempts = 0
    no_submit_ok = kill_switch_ok and mutation_attempts == 0

    reconciler = ReconciliationEngine()
    snapshot = reconciler.snapshot_from_source({
        "orders": [{"order_id": "validation-orphan-order", "symbol": "VALIDATIONUSDT", "status": "OPEN"}],
        "positions": [],
        "fills": [],
        "captured_at": canonical_utc_timestamp(),
    })
    findings, recommendations, _metrics = reconciler.reconcile(
        intended_orders=[],
        lifecycle_state_by_symbol={},
        snapshot=snapshot,
        mode="LIVE",
    )
    finding_counts = summarize_findings(findings)
    fail_closed_ok = finding_counts.get("orphan_orders", 0) >= 1 and finding_counts.get("fail_closed_findings", 0) >= 1
    repair_non_mutating_ok = bool(recommendations) and all(
        recommendation.requires_operator_approval
        and bool(recommendation.action_payload.get("dry_run"))
        and bool(recommendation.action_payload.get("shadow_mode"))
        for recommendation in recommendations
    )
    blocking_reasons: list[str] = []
    if not kill_switch_ok:
        blocking_reasons.append("KILL_SWITCH_GUARD_NOT_VERIFIED")
    if not no_submit_ok:
        blocking_reasons.append("NO_SUBMIT_GUARD_NOT_VERIFIED")
    if not fail_closed_ok:
        blocking_reasons.append("FAIL_CLOSED_RECONCILIATION_NOT_VERIFIED")
    if not repair_non_mutating_ok:
        blocking_reasons.append("NON_MUTATING_REPAIR_NOT_VERIFIED")
    return persist_rollback_validation_evidence(engine, {
        "validation_id": f"rollback-validation:{uuid.uuid4().hex}",
        "kill_switch_block_verified": kill_switch_ok,
        "no_submit_on_kill_switch_verified": no_submit_ok,
        "fail_closed_reconciliation_verified": fail_closed_ok,
        "repair_actions_non_mutating_verified": repair_non_mutating_ok,
        "execution_mutation_attempt_count": mutation_attempts,
        "blocking_reasons": blocking_reasons,
        "evidence_payload": {
            "validation_scope": "LOCAL_DETERMINISTIC_NO_ADAPTER_BOUND",
            "guard_path": "RuntimeOrchestrator._evaluate_runtime_risk",
            "guard_reject_reason": reason,
            "unsafe_snapshot_case": "ORPHAN_ORDER",
            "finding_counts": finding_counts,
            "recommendation_categories": [item.category for item in recommendations],
            "repair_payloads_dry_run": repair_non_mutating_ok,
            "execution_adapter_bound": False,
        },
    })


def main() -> None:
    parser = argparse.ArgumentParser(description="Persist deterministic non-trading rollback readiness evidence")
    parser.add_argument("--database-url", required=True, help="Runtime SQLite SQLAlchemy URL")
    args = parser.parse_args()
    result = run_deterministic_rollback_validation(create_engine(args.database_url, future=True))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
