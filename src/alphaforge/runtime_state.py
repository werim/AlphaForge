from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json, os, uuid
from typing import Any, Mapping

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from alphaforge.contracts import canonical_utc_timestamp
from alphaforge.schema_doctor import ExposureStateError, exposure_count


def build_readonly_reconciliation_probe(provider: Any | None) -> Any:
    """Return one normalized, read-only probe used by preflight and startup."""
    def probe() -> dict[str, Any]:
        if provider is None:
            raise RuntimeError("read_only_reconciliation_provider_unavailable")
        raw_value = provider.snapshot()
        if not isinstance(raw_value, Mapping):
            raise RuntimeError("read_only_reconciliation_malformed_response")
        raw = dict(raw_value)
        required = {"evidence_status", "orders", "positions"}
        missing = sorted(required.difference(raw))
        if missing:
            raise RuntimeError(f"read_only_reconciliation_missing_fields:{','.join(missing)}")
        if not isinstance(raw["orders"], list) or not isinstance(raw["positions"], list):
            raise RuntimeError("read_only_reconciliation_malformed_collections")
        return {
            "provider": raw.get("exchange") or provider.__class__.__name__,
            "retrieved_at": raw.get("retrieved_at") or raw.get("captured_at") or canonical_utc_timestamp(),
            "evidence_status": str(raw.get("evidence_status") or "INCOMPLETE").upper(),
            "authenticated": raw.get("authenticated") is True,
            "input_source": str(raw.get("input_source") or "UNKNOWN").upper(),
            "orders": list(raw.get("orders") or []),
            "positions": list(raw.get("positions") or []),
            "errors": list(raw.get("errors") or []),
        }
    return probe

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
    campaign_id: str | None = None
    burnin_run_id: str | None = None
    release_id: str | None = None
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
          startup_id TEXT, campaign_id TEXT, burnin_run_id TEXT, release_id TEXT, process_id INTEGER, mode TEXT, requested_mode TEXT, actual_mode TEXT,
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
        # Existing production databases predate lineage columns.  Additive migration
        # preserves the append-only snapshot history and is safe on SQLite.
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(runtime_state_snapshots)"))}
        # Some early ops databases created a reduced snapshot table.  Keep the
        # canonical writer append-only by additively completing that schema.
        canonical_columns = {
            "timestamp": "TEXT", "instance_id": "TEXT", "startup_id": "TEXT",
            "campaign_id": "TEXT", "burnin_run_id": "TEXT", "release_id": "TEXT",
            "process_id": "INTEGER", "mode": "TEXT", "requested_mode": "TEXT", "actual_mode": "TEXT",
            "runtime_status": "TEXT", "heartbeat_age_sec": "REAL", "last_start_time": "TEXT",
            "last_shutdown_time": "TEXT", "last_error": "TEXT", "kill_switch_active": "INTEGER",
            "kill_switch_reason": "TEXT", "active_symbols": "TEXT", "active_position_count": "INTEGER",
            "active_positions": "TEXT", "pending_order_count": "INTEGER", "pending_orders": "TEXT",
            "cooldown_symbols": "TEXT", "stale_market_data_symbols": "TEXT", "unreconciled_symbols": "TEXT",
            "orphan_order_count": "INTEGER", "orphan_orders": "TEXT", "orphan_position_count": "INTEGER",
            "orphan_positions": "TEXT", "unknown_exchange_state": "INTEGER", "exchange_connectivity_status": "TEXT",
            "exchange_read_only_status": "TEXT", "reconciliation_status": "TEXT", "reconciliation_mismatch_count": "INTEGER",
            "recovery_action_required": "INTEGER", "fail_closed_reason": "TEXT", "runtime_flags": "TEXT",
            "diagnostics_json": "TEXT", "created_at": "TEXT",
        }
        for name, sql_type in canonical_columns.items():
            if name not in columns:
                conn.execute(text(f"ALTER TABLE runtime_state_snapshots ADD COLUMN {name} {sql_type}"))

def save_runtime_state_snapshot(engine: Engine, snapshot: RuntimeStateSnapshot) -> int:
    ensure_runtime_state_schema(engine)
    rec = snapshot.to_record(); rec["created_at"] = canonical_utc_timestamp()
    cols = ",".join(rec.keys()); vals = ",".join(f":{k}" for k in rec)
    with engine.begin() as conn:
        result = conn.execute(text(f"INSERT INTO runtime_state_snapshots ({cols}) VALUES ({vals})"), rec)
        return int(result.lastrowid)

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


def evaluate_runtime_recovery(engine: Engine, *, mode: str, campaign_id: str | None = None,
                              burnin_run_id: str | None = None, release_id: str | None = None,
                              instance_id: str | None = None, startup_id: str | None = None,
                              reconciliation_probe: Any | None = None) -> dict[str, Any]:
    """Evaluate recovery from authoritative current state, not snapshot counters.

    PAPER may isolate a dead, terminal, unrelated campaign only when SQL exposure
    and the latest reconciliation evidence are clean.  LIVE variants deliberately
    retain the historical unclean-start fail-closed rule.
    """
    query_errors: list[str] = []
    provider_unavailable_errors: list[str] = []
    local_exposure_query_errors: list[str] = []
    reconciliation_storage_errors: list[str] = []
    kill_switch_query_errors: list[str] = []
    campaign_state_query_errors: list[str] = []
    availability = {
        "active_positions_available": False,
        "pending_orders_available": False,
        "orphan_evidence_available": False,
        "kill_switch_available": False,
        "campaign_state_available": True,
    }
    try:
        ensure_runtime_state_schema(engine)
        latest = latest_runtime_state_snapshot(engine)
    except Exception as exc:
        err = f"runtime_state:{type(exc).__name__}:{exc}"
        return {"blocked": True, "reason": "RECOVERY_EVIDENCE_UNAVAILABLE", "query_errors": [err], "provider_unavailable_errors": [], "local_exposure_query_errors": [err], "reconciliation_storage_errors": [], "kill_switch_query_errors": [], "campaign_state_query_errors": [], "availability": availability, "scope": "GLOBAL_EXECUTION_RISK", "latest": None, "current_exposure_check": {}}
    clean_statuses = {"STOPPED", "CLEAN_SHUTDOWN", "STOPPING", "RECONCILED", "LOCAL_DIAGNOSTIC_RECOVERY"}
    exposure = {"active_positions": 0, "pending_orders": 0, "orphan_orders": 0, "orphan_positions": 0}
    kill_switch = False
    reconciliation_status = "UNKNOWN"
    prior_campaign_terminal = False
    process_alive = False
    with engine.connect() as conn:
        def count(name: str, available_key: str) -> int:
            try:
                value = exposure_count(conn, name)
                availability[available_key] = True
                return value
            except Exception as exc:
                reason = exc.report.reason if isinstance(exc, ExposureStateError) else type(exc).__name__
                err = f"{name}:{reason}:{exc}"
                query_errors.append(err); local_exposure_query_errors.append(err)
                return 0
        exposure["active_positions"] = count("positions", "active_positions_available")
        exposure["pending_orders"] = count("orders", "pending_orders_available")
        try:
            row = conn.execute(text("SELECT status, orphan_order_count, orphan_position_count FROM exchange_reconciliation_events ORDER BY id DESC LIMIT 1")).mappings().first()
            availability["orphan_evidence_available"] = True
            if row:
                reconciliation_status = str(row.get("status") or "UNKNOWN").upper()
                exposure["orphan_orders"] = int(row.get("orphan_order_count") or 0)
                exposure["orphan_positions"] = int(row.get("orphan_position_count") or 0)
        except Exception as exc:
            err = f"reconciliation:{type(exc).__name__}:{exc}"
            query_errors.append(err); reconciliation_storage_errors.append(err)
        try:
            kill_switch = bool(conn.execute(text("SELECT kill_switch_active FROM runtime_control_state WHERE id=1")).scalar_one_or_none())
            availability["kill_switch_available"] = True
        except Exception as exc:
            err = f"kill_switch:{type(exc).__name__}:{exc}"
            query_errors.append(err); kill_switch_query_errors.append(err)
        if latest and latest.get("campaign_id"):
            try:
                state = conn.execute(text("SELECT campaign_status FROM burnin_campaigns WHERE campaign_id=:id"), {"id": latest["campaign_id"]}).mappings().first()
                if state:
                    prior_campaign_terminal = str(state.get("campaign_status") or "").upper() in {"FAILED", "COMPLETED", "QUALIFIED", "SUSPENDED"}
            except Exception as exc:
                err = f"campaign:{type(exc).__name__}:{exc}"
                query_errors.append(err); campaign_state_query_errors.append(err); availability["campaign_state_available"] = False
    if latest and latest.get("process_id"):
        try:
            pid = int(latest["process_id"]); os.kill(pid, 0)
            # PID existence alone is not lineage evidence: Linux exposes a
            # command line cheaply; an unrelated reused PID is non-blocking.
            cmdline_path = f"/proc/{pid}/cmdline"
            if os.path.exists(cmdline_path):
                cmdline = open(cmdline_path, "rb").read().decode("utf-8", "replace").replace("\x00", " ").lower()
                expected = (not latest.get("campaign_id")) or ("alphaforge" in cmdline and str(latest["campaign_id"]).lower() in cmdline)
                process_alive = expected
            else:
                # On platforms without process inspection, only fail closed if
                # the snapshot has matching campaign lineage.
                process_alive = bool(latest.get("campaign_id") and latest.get("campaign_id") == campaign_id)
        except (OSError, ValueError): pass
    prior_unclean = bool(latest and str(latest.get("runtime_status") or "").upper() not in clean_statuses)
    prior_campaign = (latest or {}).get("campaign_id")
    same_campaign = bool(campaign_id and prior_campaign and campaign_id == prior_campaign)
    same_lineage = bool(latest and instance_id and startup_id and latest.get("instance_id") == instance_id and latest.get("startup_id") == startup_id)
    probe_clean = False
    if reconciliation_probe is not None and prior_unclean:
        try:
            probe = dict(reconciliation_probe() or {})
            probe_clean = (str(probe.get("evidence_status") or "").upper() == "COMPLETE"
                           and not probe.get("errors") and not probe.get("orders") and not probe.get("positions"))
            authoritative_probe_clean = (probe_clean and probe.get("authenticated") is True
                                          and str(probe.get("input_source") or "").upper() == "AUTHENTICATED_EXCHANGE_SNAPSHOT")
            if authoritative_probe_clean:
                # This is the authoritative current observation.  The retained
                # event remains immutable history, but must not be reported as
                # the effective reconciliation result of this evaluation.
                reconciliation_status = "CLEAN"
            if not probe_clean and (str(probe.get("evidence_status") or "").upper() != "COMPLETE" or probe.get("errors")):
                query_errors.append("reconciliation_probe:incomplete_or_error")
        except Exception as exc:
            err = f"reconciliation_probe:{type(exc).__name__}:{exc}"
            query_errors.append(err)
            if "read_only_reconciliation_provider_unavailable" in str(exc):
                provider_unavailable_errors.append(err)
            else:
                reconciliation_storage_errors.append(err)
    unresolved_reconciliation = prior_unclean and reconciliation_status not in {"CLEAN", "NOT_REQUIRED_BACKTEST", "LOCAL_ONLY_DIAGNOSTIC"} and not probe_clean
    # Pending orders are global recovery exposure when a predecessor exists;
    # an initial clean startup retains the established age-validation flow.
    authoritative_local_error = bool(local_exposure_query_errors or reconciliation_storage_errors or kill_switch_query_errors)
    provider_only_unresolved = bool(provider_unavailable_errors) and sorted(provider_unavailable_errors) == sorted(query_errors)
    global_risk = bool(exposure["active_positions"] or (prior_unclean and exposure["pending_orders"]) or exposure["orphan_orders"] or exposure["orphan_positions"] or kill_switch or authoritative_local_error or (unresolved_reconciliation and not provider_only_unresolved))
    if same_lineage: scope = "SAME_RUNTIME_LINEAGE"
    elif same_campaign: scope = "SAME_CAMPAIGN"
    elif global_risk: scope = "GLOBAL_EXECUTION_RISK"
    elif prior_unclean: scope = "UNRELATED_HISTORICAL_RUNTIME"
    else: scope = "UNRELATED_HISTORICAL_RUNTIME"
    strict_live = str(mode).upper() in {"LIVE", "LIVE_PRECHECK"}
    blocked = bool(query_errors) or global_risk or (strict_live and prior_unclean) or (same_campaign and prior_unclean) or process_alive
    reason = "RECOVERY_EVIDENCE_UNAVAILABLE" if query_errors else ("UNCLEAN_SHUTDOWN_RECOVERY_REQUIRED" if blocked and prior_unclean else ("RUNTIME_RECOVERY_REQUIRED" if blocked else None))
    return {"blocked": blocked, "reason": reason, "scope": scope, "latest": latest, "current_exposure_check": exposure,
            "kill_switch_active": kill_switch, "reconciliation_status": reconciliation_status,
            "previous_process_alive": process_alive, "campaign_terminal": prior_campaign_terminal,
            "prior_unclean": prior_unclean, "original_reason": (latest or {}).get("fail_closed_reason"), "query_errors": query_errors,
            "provider_unavailable_errors": provider_unavailable_errors, "local_exposure_query_errors": local_exposure_query_errors,
            "reconciliation_storage_errors": reconciliation_storage_errors, "kill_switch_query_errors": kill_switch_query_errors,
            "campaign_state_query_errors": campaign_state_query_errors, "availability": availability,
            "reconciliation_probe_clean": probe_clean, "reconciliation_probe": probe if reconciliation_probe is not None and 'probe' in locals() else None}

def save_runtime_recovery_event(engine: Engine, *, instance_id: str, startup_id: str, mode: str, status: str, reason: str, diagnostics: Mapping[str, Any] | None = None) -> None:
    ensure_runtime_state_schema(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO runtime_recovery_events(event_ts,instance_id,startup_id,mode,status,reason,diagnostics_json) VALUES (:ts,:i,:s,:m,:st,:r,:d)"), {"ts": canonical_utc_timestamp(),"i":instance_id,"s":startup_id,"m":mode,"st":status,"r":reason,"d":json.dumps(dict(diagnostics or {}), sort_keys=True, default=str)})

def save_exchange_reconciliation_event(engine: Engine, *, instance_id: str, startup_id: str, mode: str, status: str, mismatch_count: int = 0, orphan_order_count: int = 0, orphan_position_count: int = 0, exchange_read_only_status: str = "UNKNOWN", diagnostics: Mapping[str, Any] | None = None) -> None:
    ensure_runtime_state_schema(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO exchange_reconciliation_events(event_ts,instance_id,startup_id,mode,status,mismatch_count,orphan_order_count,orphan_position_count,exchange_read_only_status,diagnostics_json) VALUES (:ts,:i,:s,:m,:st,:mc,:oo,:op,:ro,:d)"), {"ts": canonical_utc_timestamp(),"i":instance_id,"s":startup_id,"m":mode,"st":status,"mc":mismatch_count,"oo":orphan_order_count,"op":orphan_position_count,"ro":exchange_read_only_status,"d":json.dumps(dict(diagnostics or {}), sort_keys=True, default=str)})


def persist_historical_paper_recovery_without_provider(engine: Engine, *, prior_snapshot: Mapping[str, Any] | None, diagnostics: Mapping[str, Any]) -> None:
    """Append auditable PAPER historical recovery evidence when exchange probe is unavailable.

    This fallback is deliberately weaker than verified exchange reconciliation and
    is valid only for dead, unrelated historical burn-in runtime recovery after
    callers have proven local SQL exposure is zero. It records the unavailable
    provider explicitly and creates a local diagnostic reconciled snapshot so the
    same stale historical row cannot deadlock future PAPER preflight.
    """
    instance_id = f"recovery:{uuid.uuid4().hex}"
    startup_id = f"recovery:{uuid.uuid4().hex}"
    evidence = {**dict(diagnostics), "prior_snapshot_id": (prior_snapshot or {}).get("id"), "recovery_action": "UNRELATED_HISTORICAL_ZERO_LOCAL_EXPOSURE_PROVIDER_UNAVAILABLE"}
    save_runtime_recovery_event(engine, instance_id=instance_id, startup_id=startup_id, mode="PAPER", status="HISTORICAL_RUNTIME_RECOVERED_LOCAL_EVIDENCE", reason="UNRELATED_HISTORICAL_RUNTIME_ZERO_LOCAL_EXPOSURE", diagnostics=evidence)
    save_exchange_reconciliation_event(engine, instance_id=instance_id, startup_id=startup_id,
                                       mode="PAPER", status="LOCAL_ONLY_DIAGNOSTIC", exchange_read_only_status="UNAVAILABLE",
                                       diagnostics=evidence)
    save_runtime_state_snapshot(engine, RuntimeStateSnapshot(
        mode="PAPER", requested_mode="PAPER", actual_mode="PAPER", runtime_status="LOCAL_DIAGNOSTIC_RECOVERY",
        instance_id=instance_id, startup_id=startup_id, process_id=0,
        unknown_exchange_state=True, exchange_read_only_status="UNAVAILABLE", reconciliation_status="LOCAL_ONLY_DIAGNOSTIC",
        recovery_action_required=False,
        diagnostics_json=evidence,
    ))


def persist_verified_paper_recovery(engine: Engine, *, probe: Mapping[str, Any], prior_snapshot: Mapping[str, Any] | None) -> None:
    """Append verified PAPER recovery evidence; never rewrite an unclean snapshot.

    This is deliberately limited to a complete, empty read-only exchange snapshot.
    It records both the exchange observation and a new clean runtime state, so a
    historical unscoped failure cannot remain the latest recovery record forever.
    """
    if (str(probe.get("evidence_status") or "").upper() != "COMPLETE" or probe.get("errors")
            or probe.get("orders") or probe.get("positions")):
        raise RuntimeError("verified_paper_recovery_requires_complete_zero_exposure_probe")
    instance_id = f"recovery:{uuid.uuid4().hex}"
    startup_id = f"recovery:{uuid.uuid4().hex}"
    save_exchange_reconciliation_event(engine, instance_id=instance_id, startup_id=startup_id,
                                       mode="PAPER", status="CLEAN", exchange_read_only_status="AVAILABLE",
                                       diagnostics={"probe": dict(probe), "prior_snapshot_id": (prior_snapshot or {}).get("id"),
                                                    "recovery_action": "VERIFIED_ZERO_EXPOSURE"})
    save_runtime_state_snapshot(engine, RuntimeStateSnapshot(
        mode="PAPER", requested_mode="PAPER", actual_mode="PAPER", runtime_status="RECONCILED",
        instance_id=instance_id, startup_id=startup_id, process_id=0,
        unknown_exchange_state=False, exchange_read_only_status="AVAILABLE", reconciliation_status="CLEAN",
        recovery_action_required=False,
        diagnostics_json={"recovery_action": "VERIFIED_ZERO_EXPOSURE", "prior_snapshot_id": (prior_snapshot or {}).get("id")},
    ))


def persist_campaign_linked_zero_exposure_reconciliation_evidence(
    engine: Engine, *, probe: Mapping[str, Any], prior_snapshot: Mapping[str, Any] | None,
    campaign_id: str, burnin_run_id: str, release_id: str,
) -> dict[str, Any]:
    """Append the exact fresh PAPER reconciliation used by manual terminalization."""
    if (probe.get("authenticated") is not True
            or str(probe.get("input_source") or "").upper() != "AUTHENTICATED_EXCHANGE_SNAPSHOT"
            or str(probe.get("evidence_status") or "").upper() != "COMPLETE" or probe.get("errors")
            or probe.get("orders") or probe.get("positions")):
        raise RuntimeError("campaign_linked_evidence_requires_authenticated_complete_zero_exposure_probe")
    instance_id = f"terminalization:{uuid.uuid4().hex}"
    startup_id = f"terminalization:{uuid.uuid4().hex}"
    diagnostics = {
        "probe": dict(probe), "prior_snapshot_id": (prior_snapshot or {}).get("id"),
        "recovery_action": "CAMPAIGN_LINKED_ZERO_EXPOSURE_TERMINALIZATION",
        "campaign_id": campaign_id, "burnin_run_id": burnin_run_id, "release_id": release_id,
    }
    snapshot = RuntimeStateSnapshot(
        mode="PAPER", requested_mode="PAPER", actual_mode="PAPER", runtime_status="RECONCILED",
        instance_id=instance_id, startup_id=startup_id, process_id=0,
        campaign_id=campaign_id, burnin_run_id=burnin_run_id, release_id=release_id,
        unknown_exchange_state=False, exchange_read_only_status="AVAILABLE", reconciliation_status="CLEAN",
        recovery_action_required=False, diagnostics_json=diagnostics,
    )
    ensure_runtime_state_schema(engine)
    rec = snapshot.to_record(); rec["created_at"] = canonical_utc_timestamp()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO exchange_reconciliation_events(event_ts,instance_id,startup_id,mode,status,mismatch_count,orphan_order_count,orphan_position_count,exchange_read_only_status,diagnostics_json) VALUES (:ts,:i,:s,'PAPER','CLEAN',0,0,0,'AVAILABLE',:d)"),
                     {"ts": canonical_utc_timestamp(), "i": instance_id, "s": startup_id, "d": json.dumps(diagnostics, sort_keys=True, default=str)})
        cols = ",".join(rec); vals = ",".join(f":{key}" for key in rec)
        result = conn.execute(text(f"INSERT INTO runtime_state_snapshots ({cols}) VALUES ({vals})"), rec)
        snapshot_id = int(result.lastrowid)
        row = conn.execute(text("SELECT * FROM runtime_state_snapshots WHERE id=:id"), {"id": snapshot_id}).mappings().one()
    return dict(row)
