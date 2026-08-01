from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Mapping

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from alphaforge.contracts import canonical_utc_timestamp

DEFAULT_MAX_AGE_SEC = 120.0
DEFAULT_FUTURE_TOLERANCE_SEC = 5.0
_ALLOWED_MODES = {"PAPER", "LIVE"}
_ALLOWED_STATES = {"OPERATING", "STOPPING"}
_ALLOWED_EVIDENCE_STATUS = {"MEASURED_RUNTIME_HEARTBEAT"}


@dataclass(frozen=True, slots=True)
class HeartbeatFreshness:
    state: str
    reason: str
    latest_heartbeat: dict[str, Any] | None
    age_sec: float | None
    max_age_sec: float

    @property
    def is_fresh(self) -> bool:
        return self.state == "FRESH"

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "latest_heartbeat": self.latest_heartbeat,
            "age_sec": self.age_sec,
            "max_age_sec": self.max_age_sec,
            "fresh": self.is_fresh,
        }


def ensure_runtime_heartbeat_schema(engine: Engine) -> None:
    """Add the runtime-owned heartbeat evidence table without altering existing data."""
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS runtime_heartbeats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                runtime_instance_id TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                heartbeat_ts TEXT NOT NULL,
                scanner_source TEXT,
                runtime_state TEXT NOT NULL,
                last_scan_ts TEXT,
                last_decision_ts TEXT,
                active_positions_count INTEGER,
                pending_orders_count INTEGER,
                evidence_status TEXT NOT NULL,
                payload_json TEXT
            )
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_runtime_heartbeats_mode_ts
            ON runtime_heartbeats(execution_mode, heartbeat_ts DESC, id DESC)
        """))


def _safe_payload(payload: Mapping[str, Any] | None) -> str:
    permitted = {
        "scans",
        "symbols_selected",
        "decisions_generated",
        "rejects_persisted",
        "executions",
        "lifecycle_events",
        "reconciliation_runs",
        "reconciliation_fail_closed",
        "persistence_enabled",
        "top_selection_reject_reasons",
        "decision_gate_blockers",
        "agent_shadow_queue_depth",
        "agent_shadow_dropped",
        "agent_shadow_deferred",
        "agent_shadow_persistence_retries",
        "agent_shadow_lock_wait_ms",
        "agent_shadow_worker_count",
    }
    safe = {key: value for key, value in dict(payload or {}).items() if key in permitted}
    return json.dumps(safe, separators=(",", ":"), sort_keys=True)


def save_runtime_heartbeat(
    engine: Engine,
    *,
    runtime_instance_id: str,
    execution_mode: str,
    scanner_source: str | None,
    runtime_state: str = "OPERATING",
    heartbeat_ts: str | None = None,
    last_scan_ts: str | None = None,
    last_decision_ts: str | None = None,
    active_positions_count: int = 0,
    pending_orders_count: int = 0,
    payload: Mapping[str, Any] | None = None,
) -> None:
    mode = str(execution_mode or "").strip().upper()
    if mode not in _ALLOWED_MODES:
        return
    instance_id = str(runtime_instance_id or "").strip()
    if not instance_id:
        raise ValueError("runtime_instance_id is required")
    state = str(runtime_state or "").strip().upper()
    if state not in _ALLOWED_STATES:
        raise ValueError("unsupported runtime_state")
    ensure_runtime_heartbeat_schema(engine)
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO runtime_heartbeats(
                runtime_instance_id, execution_mode, heartbeat_ts, scanner_source, runtime_state,
                last_scan_ts, last_decision_ts, active_positions_count, pending_orders_count,
                evidence_status, payload_json
            ) VALUES (
                :runtime_instance_id, :execution_mode, :heartbeat_ts, :scanner_source, :runtime_state,
                :last_scan_ts, :last_decision_ts, :active_positions_count, :pending_orders_count,
                :evidence_status, :payload_json
            )
        """), {
            "runtime_instance_id": instance_id,
            "execution_mode": mode,
            "heartbeat_ts": heartbeat_ts or canonical_utc_timestamp(),
            "scanner_source": scanner_source,
            "runtime_state": state,
            "last_scan_ts": last_scan_ts,
            "last_decision_ts": last_decision_ts,
            "active_positions_count": max(0, int(active_positions_count)),
            "pending_orders_count": max(0, int(pending_orders_count)),
            "evidence_status": "MEASURED_RUNTIME_HEARTBEAT",
            "payload_json": _safe_payload(payload),
        })


def fetch_latest_runtime_heartbeat(engine: Engine, *, execution_mode: str | None = None) -> dict[str, Any] | None:
    try:
        if not inspect(engine).has_table("runtime_heartbeats"):
            return None
        params: dict[str, Any] = {}
        where = ""
        if execution_mode:
            where = "WHERE UPPER(execution_mode) = :execution_mode"
            params["execution_mode"] = str(execution_mode).strip().upper()
        with engine.connect() as conn:
            row = conn.execute(text(f"""
                SELECT id, runtime_instance_id, execution_mode, heartbeat_ts, scanner_source,
                       runtime_state, last_scan_ts, last_decision_ts, active_positions_count,
                       pending_orders_count, evidence_status, payload_json
                FROM runtime_heartbeats
                {where}
                ORDER BY id DESC
                LIMIT 1
            """), params).mappings().first()
    except SQLAlchemyError:
        return None
    return dict(row) if row else None


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def evaluate_runtime_heartbeat_freshness(
    engine: Engine,
    *,
    required_mode: str | None = None,
    max_age_sec: float = DEFAULT_MAX_AGE_SEC,
    now: datetime | None = None,
    future_tolerance_sec: float = DEFAULT_FUTURE_TOLERANCE_SEC,
) -> HeartbeatFreshness:
    mode = str(required_mode or "").strip().upper() or None
    latest = fetch_latest_runtime_heartbeat(engine, execution_mode=mode)
    max_age = max(1.0, float(max_age_sec))
    if latest is None:
        reason = "NO_PERSISTED_RUNTIME_HEARTBEAT" if mode is None else f"NO_PERSISTED_{mode}_RUNTIME_HEARTBEAT"
        return HeartbeatFreshness("MISSING", reason, None, None, max_age)
    heartbeat_ts = _parse_utc_timestamp(latest.get("heartbeat_ts"))
    if heartbeat_ts is None:
        return HeartbeatFreshness("INVALID", "HEARTBEAT_TIMESTAMP_INVALID", latest, None, max_age)
    if str(latest.get("evidence_status") or "").upper() not in _ALLOWED_EVIDENCE_STATUS:
        return HeartbeatFreshness("INVALID", "HEARTBEAT_EVIDENCE_STATUS_INVALID", latest, None, max_age)
    if str(latest.get("runtime_state") or "").upper() != "OPERATING":
        return HeartbeatFreshness("INVALID", "RUNTIME_NOT_OPERATING", latest, None, max_age)
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    delta = (now_utc - heartbeat_ts).total_seconds()
    if delta < -abs(float(future_tolerance_sec)):
        return HeartbeatFreshness("FUTURE_DATED", "HEARTBEAT_TIMESTAMP_IN_FUTURE", latest, delta, max_age)
    age = max(0.0, delta)
    if age > max_age:
        return HeartbeatFreshness("STALE", "HEARTBEAT_EXCEEDS_MAX_AGE", latest, age, max_age)
    return HeartbeatFreshness("FRESH", "HEARTBEAT_WITHIN_MAX_AGE", latest, age, max_age)
