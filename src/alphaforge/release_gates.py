from __future__ import annotations

import json
from typing import Any, Mapping

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from alphaforge.contracts import canonical_utc_timestamp

RELEASE_SNAPSHOT_TABLE = "release_gate_snapshots"
OPERATOR_ACK_TABLE = "release_operator_acks"
CANARY_MUTATION_TABLE = "canary_mutation_attempts"


def read_only_table_exists(engine: Engine, table_name: str) -> bool:
    """Return whether a table exists without bootstrapping schema.

    This helper is intentionally read-only: dashboard GET/readiness read paths use it
    to fail closed when release-gate evidence tables are absent instead of issuing
    CREATE/ALTER against read-only SQLite connections.
    """
    try:
        return bool(inspect(engine).has_table(table_name))
    except SQLAlchemyError:
        return False


def ensure_release_gate_schema(engine: Engine) -> None:
    """Create release-gate evidence tables for explicit bootstrap/write paths only."""
    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {RELEASE_SNAPSHOT_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id TEXT NOT NULL UNIQUE,
                recorded_at TEXT NOT NULL,
                phase TEXT NOT NULL,
                status TEXT NOT NULL,
                ready INTEGER NOT NULL,
                blocking_reasons TEXT NOT NULL,
                evidence_payload TEXT NOT NULL
            )
        """))
        conn.execute(text(f"""
            CREATE INDEX IF NOT EXISTS ix_{RELEASE_SNAPSHOT_TABLE}_recorded_at
            ON {RELEASE_SNAPSHOT_TABLE}(recorded_at DESC, id DESC)
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {OPERATOR_ACK_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ack_id TEXT NOT NULL UNIQUE,
                recorded_at TEXT NOT NULL,
                operator_id TEXT,
                phase TEXT NOT NULL,
                valid_until TEXT,
                acknowledged INTEGER NOT NULL,
                evidence_payload TEXT NOT NULL
            )
        """))
        conn.execute(text(f"""
            CREATE INDEX IF NOT EXISTS ix_{OPERATOR_ACK_TABLE}_recorded_at
            ON {OPERATOR_ACK_TABLE}(recorded_at DESC, id DESC)
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {CANARY_MUTATION_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id TEXT NOT NULL UNIQUE,
                recorded_at TEXT NOT NULL,
                mutation_type TEXT NOT NULL,
                blocked INTEGER NOT NULL,
                evidence_payload TEXT NOT NULL
            )
        """))


def _decode_row(row: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in ("blocking_reasons", "evidence_payload"):
        if key in out:
            try:
                out[key] = json.loads(str(out.get(key) or ("[]" if key == "blocking_reasons" else "{}")))
            except (TypeError, ValueError, json.JSONDecodeError):
                out[key] = [] if key == "blocking_reasons" else {}
    for key in ("ready", "acknowledged", "blocked"):
        if key in out:
            out[key] = bool(out[key])
    return out


def latest_release_snapshot(engine: Engine) -> dict[str, Any] | None:
    if not read_only_table_exists(engine, RELEASE_SNAPSHOT_TABLE):
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(text(f"SELECT * FROM {RELEASE_SNAPSHOT_TABLE} ORDER BY id DESC LIMIT 1")).mappings().first()
    except SQLAlchemyError:
        return None
    return _decode_row(row) if row else None


def release_snapshot_by_id(engine: Engine, snapshot_id: str) -> dict[str, Any] | None:
    if not read_only_table_exists(engine, RELEASE_SNAPSHOT_TABLE):
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(text(f"SELECT * FROM {RELEASE_SNAPSHOT_TABLE} WHERE snapshot_id = :snapshot_id LIMIT 1"), {"snapshot_id": snapshot_id}).mappings().first()
    except SQLAlchemyError:
        return None
    return _decode_row(row) if row else None


def latest_valid_operator_ack(engine: Engine, *, phase: str = "PHASE6") -> dict[str, Any] | None:
    if not read_only_table_exists(engine, OPERATOR_ACK_TABLE):
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(text(f"""
                SELECT * FROM {OPERATOR_ACK_TABLE}
                WHERE acknowledged = 1 AND UPPER(phase) = UPPER(:phase)
                ORDER BY id DESC LIMIT 1
            """), {"phase": phase}).mappings().first()
    except SQLAlchemyError:
        return None
    return _decode_row(row) if row else None


def canary_mutation_attempt_count(engine: Engine) -> int | None:
    if not read_only_table_exists(engine, CANARY_MUTATION_TABLE):
        return None
    try:
        with engine.connect() as conn:
            return int(conn.execute(text(f"SELECT COUNT(*) FROM {CANARY_MUTATION_TABLE}")).scalar_one())
    except SQLAlchemyError:
        return None


def release_gate_status(engine: Engine) -> dict[str, Any]:
    snapshot = latest_release_snapshot(engine)
    ack = latest_valid_operator_ack(engine)
    mutation_count = canary_mutation_attempt_count(engine)
    missing = []
    if snapshot is None:
        missing.append(RELEASE_SNAPSHOT_TABLE)
    if ack is None:
        missing.append(OPERATOR_ACK_TABLE)
    if mutation_count is None:
        missing.append(CANARY_MUTATION_TABLE)
    passed = bool(snapshot and snapshot.get("ready") and ack and mutation_count == 0)
    return {
        "status": "PASS" if passed else ("NO_EVIDENCE" if missing else "FAIL"),
        "passed": passed,
        "snapshot_id": snapshot.get("snapshot_id") if snapshot else None,
        "operator_ack_present": ack is not None,
        "canary_mutation_attempt_count": mutation_count,
        "missing_evidence_tables": missing,
        "blocking_reasons": [] if passed else (["RELEASE_GATE_EVIDENCE_MISSING"] if missing else ["RELEASE_GATE_NOT_READY"]),
    }


def persist_release_snapshot(engine: Engine, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    ensure_release_gate_schema(engine)
    row = {
        "snapshot_id": str(snapshot.get("snapshot_id") or f"release:{canonical_utc_timestamp()}"),
        "recorded_at": str(snapshot.get("recorded_at") or canonical_utc_timestamp()),
        "phase": str(snapshot.get("phase") or "PHASE6"),
        "status": str(snapshot.get("status") or "INCOMPLETE"),
        "ready": bool(snapshot.get("ready", False)),
        "blocking_reasons": list(snapshot.get("blocking_reasons") or []),
        "evidence_payload": dict(snapshot.get("evidence_payload") or {}),
    }
    with engine.begin() as conn:
        conn.execute(text(f"""
            INSERT INTO {RELEASE_SNAPSHOT_TABLE}(snapshot_id, recorded_at, phase, status, ready, blocking_reasons, evidence_payload)
            VALUES (:snapshot_id, :recorded_at, :phase, :status, :ready, :blocking_reasons, :evidence_payload)
        """), {**row, "ready": int(row["ready"]), "blocking_reasons": json.dumps(row["blocking_reasons"]), "evidence_payload": json.dumps(row["evidence_payload"], sort_keys=True)})
    return row


def persist_operator_ack(engine: Engine, ack: Mapping[str, Any]) -> dict[str, Any]:
    ensure_release_gate_schema(engine)
    row = {
        "ack_id": str(ack.get("ack_id") or f"ack:{canonical_utc_timestamp()}"),
        "recorded_at": str(ack.get("recorded_at") or canonical_utc_timestamp()),
        "operator_id": str(ack.get("operator_id") or "operator"),
        "phase": str(ack.get("phase") or "PHASE6"),
        "valid_until": ack.get("valid_until"),
        "acknowledged": bool(ack.get("acknowledged", True)),
        "evidence_payload": dict(ack.get("evidence_payload") or {}),
    }
    with engine.begin() as conn:
        conn.execute(text(f"""
            INSERT INTO {OPERATOR_ACK_TABLE}(ack_id, recorded_at, operator_id, phase, valid_until, acknowledged, evidence_payload)
            VALUES (:ack_id, :recorded_at, :operator_id, :phase, :valid_until, :acknowledged, :evidence_payload)
        """), {**row, "acknowledged": int(row["acknowledged"]), "evidence_payload": json.dumps(row["evidence_payload"], sort_keys=True)})
    return row
