from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json, os, uuid
from typing import Any, Mapping

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from alphaforge.contracts import canonical_utc_timestamp

RUNTIME_REJECT_REASONS = {
    "RUNTIME_DB_UNAVAILABLE", "KILL_SWITCH_ACTIVE", "RUNTIME_RECOVERY_REQUIRED",
    "UNCLEAN_SHUTDOWN_RECOVERY_REQUIRED", "EXCHANGE_RECONCILIATION_UNAVAILABLE",
    "EXCHANGE_STATE_UNKNOWN", "ORPHAN_ORDER_DETECTED", "ORPHAN_POSITION_DETECTED",
    "STALE_PENDING_ORDER", "UNRECONCILED_POSITION", "STALE_MARKET_DATA",
    "HEARTBEAT_STALE", "MODE_MISMATCH", "RUNTIME_STATE_UNAVAILABLE",
}

@dataclass(slots=True)
class RuntimeStateSnapshot:
    mode: str
    requested_mode: str
    actual_mode: str
    runtime_status: str
    timestamp: str = field(default_factory=canonical_utc_timestamp)
    heartbeat_age_sec: float | None = None
    process_id: int = field(default_factory=os.getpid)
    instance_id: str = ""
    startup_id: str = field(default_factory=lambda: f"startup:{uuid.uuid4().hex}")
    last_start_time: str | None = None
    last_shutdown_time: str | None = None
    last_error: str | None = None
    kill_switch_active: bool = False
    kill_switch_reason: str | None = None
    active_symbols: list[str] = field(default_factory=list)
    active_position_count: int = 0
    active_positions: list[dict[str, Any]] = field(default_factory=list)
    pending_order_count: int = 0
    pending_orders: list[dict[str, Any]] = field(default_factory=list)
    cooldown_symbols: list[str] = field(default_factory=list)
    stale_market_data_symbols: list[str] = field(default_factory=list)
    unreconciled_symbols: list[str] = field(default_factory=list)
    orphan_order_count: int = 0
    orphan_orders: list[dict[str, Any]] = field(default_factory=list)
    orphan_position_count: int = 0
    orphan_positions: list[dict[str, Any]] = field(default_factory=list)
    unknown_exchange_state: bool = True
    exchange_connectivity_status: str = "UNKNOWN"
    exchange_read_only_status: str = "UNKNOWN"
    reconciliation_status: str = "UNKNOWN"
    reconciliation_mismatch_count: int = 0
    recovery_action_required: bool = False
    fail_closed_reason: str | None = None
    runtime_flags: list[str] = field(default_factory=list)
    diagnostics_json: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ["active_symbols","active_positions","pending_orders","cooldown_symbols","stale_market_data_symbols","unreconciled_symbols","orphan_orders","orphan_positions","runtime_flags","diagnostics_json"]:
            data[key] = json.dumps(data[key], sort_keys=True, default=str)
        data["kill_switch_active"] = 1 if self.kill_switch_active else 0
        data["unknown_exchange_state"] = 1 if self.unknown_exchange_state else 0
        data["recovery_action_required"] = 1 if self.recovery_action_required else 0
        return data

def ensure_runtime_state_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS runtime_state_snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, instance_id TEXT NOT NULL,
          startup_id TEXT, process_id INTEGER, mode TEXT, requested_mode TEXT, actual_mode TEXT,
          runtime_status TEXT, heartbeat_age_sec REAL, last_start_time TEXT, last_shutdown_time TEXT,
          last_error TEXT, kill_switch_active INTEGER, kill_switch_reason TEXT, active_symbols TEXT,
          active_position_count INTEGER, active_positions TEXT, pending_order_count INTEGER,
          pending_orders TEXT, cooldown_symbols TEXT, stale_market_data_symbols TEXT,
          unreconciled_symbols TEXT, orphan_order_count INTEGER, orphan_orders TEXT,
          orphan_position_count INTEGER, orphan_positions TEXT, unknown_exchange_state INTEGER,
          exchange_connectivity_status TEXT, exchange_read_only_status TEXT, reconciliation_status TEXT,
          reconciliation_mismatch_count INTEGER, recovery_action_required INTEGER, fail_closed_reason TEXT,
          runtime_flags TEXT, diagnostics_json TEXT, created_at TEXT
        )"""))
        conn.execute(text("""CREATE TABLE IF NOT EXISTS runtime_recovery_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, event_ts TEXT, instance_id TEXT, startup_id TEXT,
          mode TEXT, status TEXT, reason TEXT, diagnostics_json TEXT)"""))
        conn.execute(text("""CREATE TABLE IF NOT EXISTS exchange_reconciliation_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, event_ts TEXT, instance_id TEXT, startup_id TEXT,
          mode TEXT, status TEXT, mismatch_count INTEGER, orphan_order_count INTEGER,
          orphan_position_count INTEGER, exchange_read_only_status TEXT, diagnostics_json TEXT)"""))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_runtime_state_latest ON runtime_state_snapshots(timestamp DESC, id DESC)"))

def save_runtime_state_snapshot(engine: Engine, snapshot: RuntimeStateSnapshot) -> None:
    ensure_runtime_state_schema(engine)
    rec = snapshot.to_record(); rec["created_at"] = canonical_utc_timestamp()
    cols = ",".join(rec.keys()); vals = ",".join(f":{k}" for k in rec)
    with engine.begin() as conn:
        conn.execute(text(f"INSERT INTO runtime_state_snapshots ({cols}) VALUES ({vals})"), rec)

def latest_runtime_state_snapshot(engine: Engine) -> dict[str, Any] | None:
    if not inspect(engine).has_table("runtime_state_snapshots"):
        return None
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM runtime_state_snapshots ORDER BY id DESC LIMIT 1")).mappings().first()
    if not row: return None
    out = dict(row)
    for key in ["active_symbols","active_positions","pending_orders","cooldown_symbols","stale_market_data_symbols","unreconciled_symbols","orphan_orders","orphan_positions","runtime_flags","diagnostics_json"]:
        try: out[key] = json.loads(out.get(key) or "[]")
        except Exception: out[key] = [] if key != "diagnostics_json" else {}
    return out

def save_runtime_recovery_event(engine: Engine, *, instance_id: str, startup_id: str, mode: str, status: str, reason: str, diagnostics: Mapping[str, Any] | None = None) -> None:
    ensure_runtime_state_schema(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO runtime_recovery_events(event_ts,instance_id,startup_id,mode,status,reason,diagnostics_json) VALUES (:ts,:i,:s,:m,:st,:r,:d)"), {"ts": canonical_utc_timestamp(),"i":instance_id,"s":startup_id,"m":mode,"st":status,"r":reason,"d":json.dumps(dict(diagnostics or {}), sort_keys=True, default=str)})

def save_exchange_reconciliation_event(engine: Engine, *, instance_id: str, startup_id: str, mode: str, status: str, mismatch_count: int = 0, orphan_order_count: int = 0, orphan_position_count: int = 0, exchange_read_only_status: str = "UNKNOWN", diagnostics: Mapping[str, Any] | None = None) -> None:
    ensure_runtime_state_schema(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO exchange_reconciliation_events(event_ts,instance_id,startup_id,mode,status,mismatch_count,orphan_order_count,orphan_position_count,exchange_read_only_status,diagnostics_json) VALUES (:ts,:i,:s,:m,:st,:mc,:oo,:op,:ro,:d)"), {"ts": canonical_utc_timestamp(),"i":instance_id,"s":startup_id,"m":mode,"st":status,"mc":mismatch_count,"oo":orphan_order_count,"op":orphan_position_count,"ro":exchange_read_only_status,"d":json.dumps(dict(diagnostics or {}), sort_keys=True, default=str)})
