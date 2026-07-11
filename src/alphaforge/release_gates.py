from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import uuid
from typing import Any, Mapping

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from alphaforge.contracts import canonical_utc_timestamp

RELEASE_GATE_SNAPSHOTS_TABLE = "release_gate_snapshots"
OPERATOR_ACKNOWLEDGEMENTS_TABLE = "operator_acknowledgements"
CANARY_RUN_EVENTS_TABLE = "canary_run_events"
ROLLBACK_VERIFICATION_EVENTS_TABLE = "rollback_verification_events"
RUNBOOK_EVIDENCE_TABLE = "runbook_evidence"


@dataclass(slots=True)
class ReleaseGateSnapshot:
    release_id: str
    phase: str
    status: str
    generated_at: str
    canary_ready: bool
    rollback_verified: bool
    runbook_verified: bool
    operator_acknowledged: bool
    mutation_attempt_count: int | None
    blocking_reasons: list[str]
    evidence: dict[str, Any]

    def to_record(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("canary_ready", "rollback_verified", "runbook_verified", "operator_acknowledged"):
            data[key] = int(bool(data[key]))
        data["blocking_reasons"] = json.dumps(data["blocking_reasons"], sort_keys=True)
        data["evidence_json"] = json.dumps(data.pop("evidence"), sort_keys=True, default=str)
        return data


def _parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def read_only_table_exists(engine: Engine, table_name: str) -> bool:
    """Inspect table presence without issuing schema bootstrap DDL."""
    try:
        return bool(inspect(engine).has_table(table_name))
    except SQLAlchemyError:
        return False


def ensure_release_gate_schema(engine: Engine) -> None:
    """Canonical PR 269 release-control schema; call only from init/write/bootstrap paths."""
    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {RELEASE_GATE_SNAPSHOTS_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                release_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                status TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                canary_ready INTEGER NOT NULL,
                rollback_verified INTEGER NOT NULL,
                runbook_verified INTEGER NOT NULL,
                operator_acknowledged INTEGER NOT NULL,
                mutation_attempt_count INTEGER,
                blocking_reasons TEXT NOT NULL,
                evidence_json TEXT NOT NULL
            )
        """))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_release_gate_snapshots_release_phase ON {RELEASE_GATE_SNAPSHOTS_TABLE}(release_id, phase, id DESC)"))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {OPERATOR_ACKNOWLEDGEMENTS_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ack_id TEXT NOT NULL UNIQUE,
                release_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                acknowledged_at TEXT NOT NULL,
                valid_until TEXT NOT NULL,
                operator_id TEXT,
                acknowledgement_text TEXT,
                evidence_json TEXT NOT NULL
            )
        """))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_operator_ack_release_phase ON {OPERATOR_ACKNOWLEDGEMENTS_TABLE}(release_id, phase, id DESC)"))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {CANARY_RUN_EVENTS_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                release_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_ts TEXT NOT NULL,
                shadow_mode INTEGER NOT NULL,
                canary_mode INTEGER NOT NULL,
                mutation_attempted INTEGER NOT NULL,
                mutation_blocked INTEGER NOT NULL,
                evidence_json TEXT NOT NULL
            )
        """))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_canary_run_events_release_phase ON {CANARY_RUN_EVENTS_TABLE}(release_id, phase, id DESC)"))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {ROLLBACK_VERIFICATION_EVENTS_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                verification_id TEXT NOT NULL UNIQUE,
                release_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                verified_at TEXT NOT NULL,
                status TEXT NOT NULL,
                evidence_json TEXT NOT NULL
            )
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {RUNBOOK_EVIDENCE_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                evidence_id TEXT NOT NULL UNIQUE,
                release_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                status TEXT NOT NULL,
                evidence_json TEXT NOT NULL
            )
        """))


def _json_load(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value or json.dumps(fallback)))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def latest_release_snapshot(engine: Engine, *, release_id: str | None = None, phase: str | None = None) -> ReleaseGateSnapshot | None:
    if not read_only_table_exists(engine, RELEASE_GATE_SNAPSHOTS_TABLE):
        return None
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if release_id:
        clauses.append("release_id = :release_id")
        params["release_id"] = release_id
    if phase:
        clauses.append("UPPER(phase) = UPPER(:phase)")
        params["phase"] = phase
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        with engine.connect() as conn:
            row = conn.execute(text(f"""
                SELECT release_id, phase, status, generated_at, canary_ready, rollback_verified,
                       runbook_verified, operator_acknowledged, mutation_attempt_count,
                       blocking_reasons, evidence_json
                FROM {RELEASE_GATE_SNAPSHOTS_TABLE}
                {where}
                ORDER BY id DESC LIMIT 1
            """), params).mappings().first()
    except SQLAlchemyError:
        return None
    if row is None:
        return None
    return ReleaseGateSnapshot(
        release_id=str(row["release_id"]),
        phase=str(row["phase"]),
        status=str(row["status"]),
        generated_at=str(row["generated_at"]),
        canary_ready=bool(row["canary_ready"]),
        rollback_verified=bool(row["rollback_verified"]),
        runbook_verified=bool(row["runbook_verified"]),
        operator_acknowledged=bool(row["operator_acknowledged"]),
        mutation_attempt_count=None if row["mutation_attempt_count"] is None else int(row["mutation_attempt_count"]),
        blocking_reasons=[str(v) for v in _json_load(row["blocking_reasons"], [])],
        evidence=dict(_json_load(row["evidence_json"], {})),
    )


def release_snapshot_by_id(engine: Engine, release_id: str, *, phase: str | None = None) -> ReleaseGateSnapshot | None:
    return latest_release_snapshot(engine, release_id=release_id, phase=phase)


def latest_valid_operator_ack(engine: Engine, *, release_id: str, phase: str, now: datetime | None = None) -> dict[str, Any] | None:
    if not read_only_table_exists(engine, OPERATOR_ACKNOWLEDGEMENTS_TABLE):
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(text(f"""
                SELECT ack_id, release_id, phase, acknowledged_at, valid_until, operator_id,
                       acknowledgement_text, evidence_json
                FROM {OPERATOR_ACKNOWLEDGEMENTS_TABLE}
                WHERE release_id = :release_id AND UPPER(phase) = UPPER(:phase)
                ORDER BY id DESC LIMIT 1
            """), {"release_id": release_id, "phase": phase}).mappings().first()
    except SQLAlchemyError:
        return None
    if row is None:
        return None
    valid_until = _parse_ts(row["valid_until"])
    if valid_until is None:
        return None
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if valid_until <= current:
        return None
    return {
        "ack_id": str(row["ack_id"]),
        "release_id": str(row["release_id"]),
        "phase": str(row["phase"]),
        "acknowledged_at": str(row["acknowledged_at"]),
        "valid_until": str(row["valid_until"]),
        "operator_id": row["operator_id"],
        "acknowledgement_text": row["acknowledgement_text"],
        "evidence": dict(_json_load(row["evidence_json"], {})),
    }


def canary_mutation_attempt_count(engine: Engine, *, release_id: str, phase: str) -> int | None:
    if not read_only_table_exists(engine, CANARY_RUN_EVENTS_TABLE):
        return None
    try:
        with engine.connect() as conn:
            return int(conn.execute(text(f"""
                SELECT COUNT(*) FROM {CANARY_RUN_EVENTS_TABLE}
                WHERE release_id = :release_id AND UPPER(phase) = UPPER(:phase)
                  AND mutation_attempted = 1
            """), {"release_id": release_id, "phase": phase}).scalar_one())
    except SQLAlchemyError:
        return None


def _canary_event_count(engine: Engine, *, release_id: str, phase: str) -> int | None:
    if not read_only_table_exists(engine, CANARY_RUN_EVENTS_TABLE):
        return None
    try:
        with engine.connect() as conn:
            return int(conn.execute(text(f"""
                SELECT COUNT(*) FROM {CANARY_RUN_EVENTS_TABLE}
                WHERE release_id = :release_id AND UPPER(phase) = UPPER(:phase)
            """), {"release_id": release_id, "phase": phase}).scalar_one())
    except SQLAlchemyError:
        return None


def _latest_status(engine: Engine, table: str, *, release_id: str, phase: str, status_column: str, time_column: str) -> str | None:
    if not read_only_table_exists(engine, table):
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(text(f"""
                SELECT {status_column} AS status FROM {table}
                WHERE release_id = :release_id AND UPPER(phase) = UPPER(:phase)
                ORDER BY {time_column} DESC, id DESC LIMIT 1
            """), {"release_id": release_id, "phase": phase}).mappings().first()
    except SQLAlchemyError:
        return None
    return None if row is None else str(row["status"]).upper()


def build_release_snapshot(engine: Engine, *, release_id: str, phase: str = "PHASE6", now: datetime | None = None) -> ReleaseGateSnapshot:
    ack = latest_valid_operator_ack(engine, release_id=release_id, phase=phase, now=now)
    mutation_count = canary_mutation_attempt_count(engine, release_id=release_id, phase=phase)
    canary_event_count = _canary_event_count(engine, release_id=release_id, phase=phase)
    rollback_status = _latest_status(engine, ROLLBACK_VERIFICATION_EVENTS_TABLE, release_id=release_id, phase=phase, status_column="status", time_column="verified_at")
    runbook_status = _latest_status(engine, RUNBOOK_EVIDENCE_TABLE, release_id=release_id, phase=phase, status_column="status", time_column="recorded_at")
    canary_ready = mutation_count == 0 and bool(canary_event_count)
    rollback_verified = rollback_status == "PASS"
    runbook_verified = runbook_status == "PASS"
    reasons: list[str] = []
    if ack is None:
        reasons.append("OPERATOR_ACK_MISSING_OR_EXPIRED")
    if mutation_count is None or canary_event_count in (None, 0):
        reasons.append("CANARY_EVIDENCE_MISSING")
    elif mutation_count > 0:
        reasons.append("CANARY_MUTATION_ATTEMPTED")
    if rollback_status is None:
        reasons.append("ROLLBACK_EVIDENCE_MISSING")
    elif not rollback_verified:
        reasons.append("ROLLBACK_NOT_VERIFIED")
    if runbook_status is None:
        reasons.append("RUNBOOK_EVIDENCE_MISSING")
    elif not runbook_verified:
        reasons.append("RUNBOOK_NOT_VERIFIED")
    passed = not reasons
    return ReleaseGateSnapshot(
        release_id=release_id,
        phase=phase,
        status="CANARY_READY" if passed else "NO_EVIDENCE" if any(reason.endswith("MISSING") or "MISSING_OR_EXPIRED" in reason for reason in reasons) else "FAIL",
        generated_at=canonical_utc_timestamp(),
        canary_ready=canary_ready,
        rollback_verified=rollback_verified,
        runbook_verified=runbook_verified,
        operator_acknowledged=ack is not None,
        mutation_attempt_count=mutation_count,
        blocking_reasons=reasons,
        evidence={"operator_ack": ack, "canary_event_count": canary_event_count, "rollback_status": rollback_status, "runbook_status": runbook_status},
    )


def release_gate_status(engine: Engine, *, release_id: str = "default", phase: str = "PHASE6") -> dict[str, Any]:
    snapshot = build_release_snapshot(engine, release_id=release_id, phase=phase)
    return {"status": snapshot.status, "passed": snapshot.status == "CANARY_READY", **asdict(snapshot)}


def persist_release_snapshot(engine: Engine, snapshot: ReleaseGateSnapshot) -> ReleaseGateSnapshot:
    ensure_release_gate_schema(engine)
    with engine.begin() as conn:
        conn.execute(text(f"""
            INSERT INTO {RELEASE_GATE_SNAPSHOTS_TABLE}(
                release_id, phase, status, generated_at, canary_ready, rollback_verified,
                runbook_verified, operator_acknowledged, mutation_attempt_count,
                blocking_reasons, evidence_json
            ) VALUES (
                :release_id, :phase, :status, :generated_at, :canary_ready, :rollback_verified,
                :runbook_verified, :operator_acknowledged, :mutation_attempt_count,
                :blocking_reasons, :evidence_json
            )
        """), snapshot.to_record())
    return snapshot


def persist_operator_ack(engine: Engine, *, release_id: str, phase: str, valid_until: str, operator_id: str = "operator", acknowledgement_text: str = "acknowledged", evidence: Mapping[str, Any] | None = None, ack_id: str | None = None) -> dict[str, Any]:
    ensure_release_gate_schema(engine)
    row = {
        "ack_id": ack_id or f"ack:{uuid.uuid4().hex}",
        "release_id": release_id,
        "phase": phase,
        "acknowledged_at": canonical_utc_timestamp(),
        "valid_until": valid_until,
        "operator_id": operator_id,
        "acknowledgement_text": acknowledgement_text,
        "evidence_json": json.dumps(dict(evidence or {}), sort_keys=True),
    }
    with engine.begin() as conn:
        conn.execute(text(f"""
            INSERT INTO {OPERATOR_ACKNOWLEDGEMENTS_TABLE}(
                ack_id, release_id, phase, acknowledged_at, valid_until, operator_id,
                acknowledgement_text, evidence_json
            ) VALUES (
                :ack_id, :release_id, :phase, :acknowledged_at, :valid_until, :operator_id,
                :acknowledgement_text, :evidence_json
            )
        """), row)
    return row


def persist_canary_event(engine: Engine, *, release_id: str, phase: str, event_type: str = "CANARY_CHECK", shadow_mode: bool = True, canary_mode: bool = True, mutation_attempted: bool = False, mutation_blocked: bool = True, evidence: Mapping[str, Any] | None = None, event_id: str | None = None) -> dict[str, Any]:
    ensure_release_gate_schema(engine)
    row = {
        "event_id": event_id or f"canary:{uuid.uuid4().hex}",
        "release_id": release_id,
        "phase": phase,
        "event_type": event_type,
        "event_ts": canonical_utc_timestamp(),
        "shadow_mode": int(bool(shadow_mode)),
        "canary_mode": int(bool(canary_mode)),
        "mutation_attempted": int(bool(mutation_attempted)),
        "mutation_blocked": int(bool(mutation_blocked)),
        "evidence_json": json.dumps(dict(evidence or {}), sort_keys=True),
    }
    with engine.begin() as conn:
        conn.execute(text(f"""
            INSERT INTO {CANARY_RUN_EVENTS_TABLE}(
                event_id, release_id, phase, event_type, event_ts, shadow_mode, canary_mode,
                mutation_attempted, mutation_blocked, evidence_json
            ) VALUES (
                :event_id, :release_id, :phase, :event_type, :event_ts, :shadow_mode, :canary_mode,
                :mutation_attempted, :mutation_blocked, :evidence_json
            )
        """), row)
    return row


class MutationTrapExecutionAdapter:
    """Execution-adapter guard used by canary/shadow validation to record mutation attempts."""

    def __init__(self, engine: Engine, *, release_id: str, phase: str = "PHASE6") -> None:
        self.engine = engine
        self.release_id = release_id
        self.phase = phase

    def __getattr__(self, name: str) -> Any:
        if name.lower().startswith(("submit", "place", "cancel", "modify", "create")):
            def _blocked(*args: Any, **kwargs: Any) -> None:
                persist_canary_event(
                    self.engine,
                    release_id=self.release_id,
                    phase=self.phase,
                    event_type=f"MUTATION_BLOCKED:{name}",
                    mutation_attempted=True,
                    mutation_blocked=True,
                    evidence={"method": name},
                )
                raise RuntimeError("CANARY_MUTATION_BLOCKED")
            return _blocked
        raise AttributeError(name)
