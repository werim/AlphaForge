from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Engine

from alphaforge.contracts import canonical_utc_timestamp

PHASE6_LIVE_ORDER_SUBMISSION_ENABLED = False
ACK_RISK_PHRASE = "I acknowledge AlphaForge Phase 6 canary risk and LIVE real orders remain disabled"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_sha256(path: str | Path) -> str | None:
    p = Path(path)
    if not p.exists() or not p.is_file():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _git(args: list[str], default: str = "UNKNOWN") -> str:
    try:
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip() or default
    except Exception:
        return default


@dataclass(slots=True)
class ReleaseGateSnapshot:
    timestamp: str
    release_id: str
    version: str
    git_commit: str
    branch: str
    requested_mode: str
    actual_mode: str
    live_enabled: bool = False
    live_order_submission_enabled: bool = False
    live_precheck_enabled: bool = False
    shadow_mode_enabled: bool = False
    canary_enabled: bool = False
    canary_scope: str = "NONE"
    canary_symbols: list[str] = field(default_factory=list)
    canary_max_notional: float | None = None
    canary_max_risk_pct: float | None = None
    canary_start_time: str | None = None
    canary_end_time: str | None = None
    canary_status: str = "NOT_STARTED"
    operator_ack_required: bool = True
    operator_ack_present: bool = False
    operator_ack_user: str | None = None
    operator_ack_timestamp: str | None = None
    operator_ack_text_hash: str | None = None
    kill_switch_active: bool = False
    rollback_ready: bool = False
    rollback_last_tested_at: str | None = None
    rollback_procedure_hash: str | None = None
    runbook_present: bool = False
    runbook_hash: str | None = None
    test_evidence_status: str = "MISSING"
    paper_burnin_status: str = "MISSING"
    readiness_verdict: str = "NOT_LIVE_READY"
    readiness_blockers: list[str] = field(default_factory=list)
    release_flags: dict[str, Any] = field(default_factory=dict)
    diagnostics_json: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ensure_release_gate_schema(engine: Engine) -> None:
    ddl = [
        """CREATE TABLE IF NOT EXISTS release_gate_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, release_id TEXT NOT NULL,
            version TEXT, git_commit TEXT, branch TEXT, requested_mode TEXT, actual_mode TEXT,
            live_enabled INTEGER NOT NULL, live_order_submission_enabled INTEGER NOT NULL,
            live_precheck_enabled INTEGER NOT NULL, shadow_mode_enabled INTEGER NOT NULL,
            canary_enabled INTEGER NOT NULL, canary_scope TEXT, canary_symbols_json TEXT,
            canary_max_notional REAL, canary_max_risk_pct REAL, canary_start_time TEXT,
            canary_end_time TEXT, canary_status TEXT, operator_ack_required INTEGER NOT NULL,
            operator_ack_present INTEGER NOT NULL, operator_ack_user TEXT, operator_ack_timestamp TEXT,
            operator_ack_text_hash TEXT, kill_switch_active INTEGER NOT NULL, rollback_ready INTEGER NOT NULL,
            rollback_last_tested_at TEXT, rollback_procedure_hash TEXT, runbook_present INTEGER NOT NULL,
            runbook_hash TEXT, test_evidence_status TEXT, paper_burnin_status TEXT,
            readiness_verdict TEXT, readiness_blockers_json TEXT, release_flags_json TEXT,
            diagnostics_json TEXT, created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS operator_acknowledgements (
            id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL, operator_user TEXT,
            ack_timestamp TEXT NOT NULL, ack_text_hash TEXT NOT NULL, expires_at TEXT NOT NULL,
            valid INTEGER NOT NULL, blocker_reason TEXT, created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS canary_run_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL, event_ts TEXT NOT NULL,
            event_type TEXT NOT NULL, status TEXT, reason TEXT, symbol TEXT, notional REAL,
            risk_pct REAL, mutation_attempt INTEGER NOT NULL DEFAULT 0, payload_json TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS rollback_verification_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL, event_ts TEXT NOT NULL,
            procedure_path TEXT, procedure_hash TEXT, dry_run INTEGER NOT NULL, kill_switch_verified INTEGER NOT NULL,
            runtime_stop_verified INTEGER NOT NULL, recovery_state TEXT, non_mutating INTEGER NOT NULL,
            status TEXT NOT NULL, blockers_json TEXT, payload_json TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS runbook_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL, recorded_at TEXT NOT NULL,
            runbook_path TEXT NOT NULL, runbook_hash TEXT, present INTEGER NOT NULL, status TEXT NOT NULL
        )""",
    ]
    with engine.begin() as conn:
        for stmt in ddl:
            conn.execute(text(stmt))
        # additive link for existing readiness reports
        cols = {str(r[1]) for r in conn.execute(text("PRAGMA table_info(live_readiness_reports)")).all()} if conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='live_readiness_reports'")).first() else set()
        if cols and "release_id" not in cols:
            conn.execute(text("ALTER TABLE live_readiness_reports ADD COLUMN release_id TEXT"))


def persist_operator_ack(engine: Engine, *, release_id: str, ack_text: str, operator_user: str | None = None, ttl_minutes: int = 240) -> dict[str, Any]:
    ensure_release_gate_schema(engine)
    now = datetime.now(timezone.utc)
    valid = release_id in ack_text and ACK_RISK_PHRASE in ack_text
    reason = None if valid else "ACK_TEXT_MISSING_RELEASE_OR_RISK_PHRASE"
    row = {"release_id": release_id, "operator_user": operator_user or os.getenv("USER") or "UNKNOWN", "ack_timestamp": canonical_utc_timestamp(), "ack_text_hash": _sha256_text(ack_text), "expires_at": (now + timedelta(minutes=ttl_minutes)).isoformat().replace("+00:00", "Z"), "valid": 1 if valid else 0, "blocker_reason": reason, "created_at": canonical_utc_timestamp()}
    with engine.begin() as conn:
        conn.execute(text("""INSERT INTO operator_acknowledgements(release_id, operator_user, ack_timestamp, ack_text_hash, expires_at, valid, blocker_reason, created_at)
        VALUES (:release_id,:operator_user,:ack_timestamp,:ack_text_hash,:expires_at,:valid,:blocker_reason,:created_at)"""), row)
    return row


def latest_valid_operator_ack(engine: Engine, release_id: str) -> dict[str, Any] | None:
    ensure_release_gate_schema(engine)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM operator_acknowledgements WHERE release_id=:rid ORDER BY id DESC LIMIT 1"), {"rid": release_id}).mappings().first()
    if not row or not bool(row["valid"]):
        return None
    exp = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
    return dict(row) if exp >= datetime.now(timezone.utc) else None


def persist_canary_event(engine: Engine, *, release_id: str, event_type: str, status: str, reason: str | None = None, symbol: str | None = None, notional: float | None = None, risk_pct: float | None = None, mutation_attempt: bool = False, payload: Mapping[str, Any] | None = None) -> None:
    ensure_release_gate_schema(engine)
    with engine.begin() as conn:
        conn.execute(text("""INSERT INTO canary_run_events(release_id,event_ts,event_type,status,reason,symbol,notional,risk_pct,mutation_attempt,payload_json)
        VALUES (:release_id,:event_ts,:event_type,:status,:reason,:symbol,:notional,:risk_pct,:mutation_attempt,:payload_json)"""), {"release_id": release_id, "event_ts": canonical_utc_timestamp(), "event_type": event_type, "status": status, "reason": reason, "symbol": symbol, "notional": notional, "risk_pct": risk_pct, "mutation_attempt": 1 if mutation_attempt else 0, "payload_json": json.dumps(payload or {}, sort_keys=True)})


def persist_rollback_verification(engine: Engine, *, release_id: str, procedure_path: str = "RUNBOOK.md", dry_run: bool, kill_switch_verified: bool, runtime_stop_verified: bool, recovery_state: str = "NON_MUTATING", non_mutating: bool = True, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    ensure_release_gate_schema(engine)
    blockers = []
    if not dry_run: blockers.append("ROLLBACK_DRY_RUN_REQUIRED")
    if not kill_switch_verified: blockers.append("ROLLBACK_KILL_SWITCH_UNVERIFIED")
    if not runtime_stop_verified: blockers.append("ROLLBACK_RUNTIME_STOP_UNVERIFIED")
    if not non_mutating: blockers.append("ROLLBACK_MUTATION_ATTEMPT")
    proc_hash = file_sha256(procedure_path)
    if not proc_hash: blockers.append("ROLLBACK_PROCEDURE_MISSING")
    status = "PASS" if not blockers else "FAIL"
    row = {"release_id": release_id, "event_ts": canonical_utc_timestamp(), "procedure_path": procedure_path, "procedure_hash": proc_hash, "dry_run": 1 if dry_run else 0, "kill_switch_verified": 1 if kill_switch_verified else 0, "runtime_stop_verified": 1 if runtime_stop_verified else 0, "recovery_state": recovery_state, "non_mutating": 1 if non_mutating else 0, "status": status, "blockers_json": json.dumps(blockers), "payload_json": json.dumps(payload or {}, sort_keys=True)}
    with engine.begin() as conn:
        conn.execute(text("""INSERT INTO rollback_verification_events(release_id,event_ts,procedure_path,procedure_hash,dry_run,kill_switch_verified,runtime_stop_verified,recovery_state,non_mutating,status,blockers_json,payload_json)
        VALUES (:release_id,:event_ts,:procedure_path,:procedure_hash,:dry_run,:kill_switch_verified,:runtime_stop_verified,:recovery_state,:non_mutating,:status,:blockers_json,:payload_json)"""), row)
    return row


def persist_runbook_evidence(engine: Engine, *, release_id: str, runbook_path: str = "RUNBOOK.md") -> dict[str, Any]:
    ensure_release_gate_schema(engine)
    h = file_sha256(runbook_path)
    row = {"release_id": release_id, "recorded_at": canonical_utc_timestamp(), "runbook_path": runbook_path, "runbook_hash": h, "present": 1 if h else 0, "status": "PASS" if h else "MISSING"}
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO runbook_evidence(release_id,recorded_at,runbook_path,runbook_hash,present,status) VALUES (:release_id,:recorded_at,:runbook_path,:runbook_hash,:present,:status)"), row)
    return row


def persist_release_gate_snapshot(engine: Engine, snapshot: ReleaseGateSnapshot) -> None:
    ensure_release_gate_schema(engine)
    d = snapshot.to_dict()
    params = {**d, "live_enabled": int(d["live_enabled"]), "live_order_submission_enabled": int(d["live_order_submission_enabled"]), "live_precheck_enabled": int(d["live_precheck_enabled"]), "shadow_mode_enabled": int(d["shadow_mode_enabled"]), "canary_enabled": int(d["canary_enabled"]), "operator_ack_required": int(d["operator_ack_required"]), "operator_ack_present": int(d["operator_ack_present"]), "kill_switch_active": int(d["kill_switch_active"]), "rollback_ready": int(d["rollback_ready"]), "runbook_present": int(d["runbook_present"]), "canary_symbols_json": json.dumps(d["canary_symbols"]), "readiness_blockers_json": json.dumps(d["readiness_blockers"]), "release_flags_json": json.dumps(d["release_flags"], sort_keys=True), "diagnostics_json": json.dumps(d["diagnostics_json"], sort_keys=True), "created_at": canonical_utc_timestamp()}
    with engine.begin() as conn:
        conn.execute(text("""INSERT INTO release_gate_snapshots(timestamp,release_id,version,git_commit,branch,requested_mode,actual_mode,live_enabled,live_order_submission_enabled,live_precheck_enabled,shadow_mode_enabled,canary_enabled,canary_scope,canary_symbols_json,canary_max_notional,canary_max_risk_pct,canary_start_time,canary_end_time,canary_status,operator_ack_required,operator_ack_present,operator_ack_user,operator_ack_timestamp,operator_ack_text_hash,kill_switch_active,rollback_ready,rollback_last_tested_at,rollback_procedure_hash,runbook_present,runbook_hash,test_evidence_status,paper_burnin_status,readiness_verdict,readiness_blockers_json,release_flags_json,diagnostics_json,created_at)
        VALUES (:timestamp,:release_id,:version,:git_commit,:branch,:requested_mode,:actual_mode,:live_enabled,:live_order_submission_enabled,:live_precheck_enabled,:shadow_mode_enabled,:canary_enabled,:canary_scope,:canary_symbols_json,:canary_max_notional,:canary_max_risk_pct,:canary_start_time,:canary_end_time,:canary_status,:operator_ack_required,:operator_ack_present,:operator_ack_user,:operator_ack_timestamp,:operator_ack_text_hash,:kill_switch_active,:rollback_ready,:rollback_last_tested_at,:rollback_procedure_hash,:runbook_present,:runbook_hash,:test_evidence_status,:paper_burnin_status,:readiness_verdict,:readiness_blockers_json,:release_flags_json,:diagnostics_json,:created_at)"""), params)


def latest_release_snapshot(engine: Engine) -> dict[str, Any] | None:
    ensure_release_gate_schema(engine)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM release_gate_snapshots ORDER BY id DESC LIMIT 1")).mappings().first()
    return dict(row) if row else None


def release_snapshot_by_id(engine: Engine, release_id: str) -> dict[str, Any] | None:
    ensure_release_gate_schema(engine)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM release_gate_snapshots WHERE release_id=:rid ORDER BY id DESC LIMIT 1"), {"rid": release_id}).mappings().first()
    return dict(row) if row else None


def release_snapshot_from_row(row: Mapping[str, Any]) -> ReleaseGateSnapshot:
    raw_symbols = row.get("canary_symbols_json") or row.get("canary_symbols") or "[]"
    try:
        symbols = json.loads(str(raw_symbols)) if isinstance(raw_symbols, str) else list(raw_symbols or [])
    except json.JSONDecodeError:
        symbols = []
    try:
        blockers = json.loads(str(row.get("readiness_blockers_json") or "[]"))
    except json.JSONDecodeError:
        blockers = []
    try:
        flags = json.loads(str(row.get("release_flags_json") or "{}"))
    except json.JSONDecodeError:
        flags = {}
    try:
        diagnostics = json.loads(str(row.get("diagnostics_json") or "{}"))
    except json.JSONDecodeError:
        diagnostics = {}
    return ReleaseGateSnapshot(
        timestamp=str(row.get("timestamp") or canonical_utc_timestamp()),
        release_id=str(row.get("release_id") or ""),
        version=str(row.get("version") or "UNKNOWN"),
        git_commit=str(row.get("git_commit") or "UNKNOWN"),
        branch=str(row.get("branch") or "UNKNOWN"),
        requested_mode=str(row.get("requested_mode") or "UNKNOWN"),
        actual_mode=str(row.get("actual_mode") or "UNKNOWN"),
        live_enabled=bool(row.get("live_enabled")),
        live_order_submission_enabled=bool(row.get("live_order_submission_enabled")),
        live_precheck_enabled=bool(row.get("live_precheck_enabled")),
        shadow_mode_enabled=bool(row.get("shadow_mode_enabled")),
        canary_enabled=bool(row.get("canary_enabled")),
        canary_scope=str(row.get("canary_scope") or "NONE"),
        canary_symbols=[str(v) for v in symbols],
        canary_max_notional=row.get("canary_max_notional"),
        canary_max_risk_pct=row.get("canary_max_risk_pct"),
        canary_start_time=row.get("canary_start_time"),
        canary_end_time=row.get("canary_end_time"),
        canary_status=str(row.get("canary_status") or "NOT_STARTED"),
        operator_ack_required=bool(row.get("operator_ack_required")),
        operator_ack_present=bool(row.get("operator_ack_present")),
        operator_ack_user=row.get("operator_ack_user"),
        operator_ack_timestamp=row.get("operator_ack_timestamp"),
        operator_ack_text_hash=row.get("operator_ack_text_hash"),
        kill_switch_active=bool(row.get("kill_switch_active")),
        rollback_ready=bool(row.get("rollback_ready")),
        rollback_last_tested_at=row.get("rollback_last_tested_at"),
        rollback_procedure_hash=row.get("rollback_procedure_hash"),
        runbook_present=bool(row.get("runbook_present")),
        runbook_hash=row.get("runbook_hash"),
        test_evidence_status=str(row.get("test_evidence_status") or "MISSING"),
        paper_burnin_status=str(row.get("paper_burnin_status") or "MISSING"),
        readiness_verdict=str(row.get("readiness_verdict") or "NOT_LIVE_READY"),
        readiness_blockers=[str(v) for v in blockers],
        release_flags=dict(flags),
        diagnostics_json=dict(diagnostics),
    )


def canary_mutation_attempt_count(engine: Engine, release_id: str | None = None) -> int:
    ensure_release_gate_schema(engine)
    with engine.connect() as conn:
        if release_id:
            return int(conn.execute(text("SELECT COUNT(*) FROM canary_run_events WHERE release_id=:rid AND mutation_attempt=1"), {"rid": release_id}).scalar_one() or 0)
        return int(conn.execute(text("SELECT COUNT(*) FROM canary_run_events WHERE mutation_attempt=1")).scalar_one() or 0)


def build_release_snapshot(engine: Engine, *, release_id: str, requested_mode: str, actual_mode: str, version: str = "UNKNOWN", canary_enabled: bool = False, shadow_mode_enabled: bool = False, live_precheck_enabled: bool = True, canary_symbols: list[str] | None = None, canary_max_notional: float | None = None, canary_max_risk_pct: float | None = None, test_evidence_status: str = "MISSING", paper_burnin_status: str = "MISSING", runbook_path: str = "RUNBOOK.md") -> ReleaseGateSnapshot:
    ack = latest_valid_operator_ack(engine, release_id)
    runbook_hash = file_sha256(runbook_path)
    with engine.connect() as conn:
        rollback = conn.execute(text("SELECT * FROM rollback_verification_events WHERE release_id=:rid ORDER BY id DESC LIMIT 1"), {"rid": release_id}).mappings().first()
        mut = conn.execute(text("SELECT COUNT(*) FROM canary_run_events WHERE release_id=:rid AND mutation_attempt=1"), {"rid": release_id}).scalar_one()
    blockers: list[str] = []
    if PHASE6_LIVE_ORDER_SUBMISSION_ENABLED: blockers.append("LIVE_ORDER_SUBMISSION_ENABLED_IN_PHASE6")
    if canary_enabled and not ack: blockers.append("CANARY_OPERATOR_ACK_MISSING")
    if not runbook_hash: blockers.append("RUNBOOK_EVIDENCE_MISSING")
    rollback_ready = bool(rollback and rollback["status"] == "PASS")
    if not rollback_ready: blockers.append("ROLLBACK_EVIDENCE_MISSING")
    if int(mut or 0) > 0: blockers.append("CANARY_MUTATION_ATTEMPT")
    verdict = "CANARY_READY" if canary_enabled and not blockers else ("LIVE_PRECHECK_READY" if not blockers else "NOT_LIVE_READY")
    return ReleaseGateSnapshot(timestamp=canonical_utc_timestamp(), release_id=release_id, version=version, git_commit=_git(["rev-parse", "HEAD"]), branch=_git(["branch", "--show-current"]), requested_mode=requested_mode, actual_mode=actual_mode, live_enabled=False, live_order_submission_enabled=False, live_precheck_enabled=live_precheck_enabled, shadow_mode_enabled=shadow_mode_enabled, canary_enabled=canary_enabled, canary_scope="SYMBOL_ALLOWLIST" if canary_symbols else "NONE", canary_symbols=canary_symbols or [], canary_max_notional=canary_max_notional, canary_max_risk_pct=canary_max_risk_pct, operator_ack_required=canary_enabled, operator_ack_present=bool(ack), operator_ack_user=(ack or {}).get("operator_user"), operator_ack_timestamp=(ack or {}).get("ack_timestamp"), operator_ack_text_hash=(ack or {}).get("ack_text_hash"), rollback_ready=rollback_ready, rollback_last_tested_at=(dict(rollback).get("event_ts") if rollback else None), rollback_procedure_hash=(dict(rollback).get("procedure_hash") if rollback else None), runbook_present=bool(runbook_hash), runbook_hash=runbook_hash, test_evidence_status=test_evidence_status, paper_burnin_status=paper_burnin_status, readiness_verdict=verdict, readiness_blockers=blockers, release_flags={"phase": "6", "live_real_orders_blocked": True}, diagnostics_json={"mutation_attempt_count": int(mut or 0)})


def evaluate_canary_candidate(snapshot: ReleaseGateSnapshot, *, symbol: str, notional: float, risk_pct: float, now: str | None = None) -> tuple[bool, str]:
    if snapshot.kill_switch_active: return False, "KILL_SWITCH_ACTIVE"
    if snapshot.operator_ack_required and not snapshot.operator_ack_present: return False, "CANARY_OPERATOR_ACK_MISSING"
    if symbol not in set(snapshot.canary_symbols): return False, "CANARY_SYMBOL_SCOPE_VIOLATION"
    if snapshot.canary_max_notional is not None and notional > snapshot.canary_max_notional: return False, "CANARY_NOTIONAL_LIMIT"
    if snapshot.canary_max_risk_pct is not None and risk_pct > snapshot.canary_max_risk_pct: return False, "CANARY_RISK_LIMIT"
    if snapshot.live_order_submission_enabled: return False, "CANARY_MUTATION_ATTEMPT"
    return True, "PASS"


class SupportsExecutionMutation(Protocol):
    async def submit(self, decision: Mapping[str, Any], market_ctx: Mapping[str, Any]) -> Mapping[str, Any]: ...


class MutationTrapExecutionAdapter:
    """Non-mutating adapter wrapper for Phase 6 shadow/canary paths.

    Any submit/cancel/modify call is recorded as a mutation attempt and then
    rejected. This makes no-submit evidence measured instead of declarative.
    """

    def __init__(self, adapter: Any | None = None, *, engine: Engine | None = None, release_id: str | None = None, phase: str = "CANARY") -> None:
        self.adapter = adapter
        self.engine = engine
        self.release_id = release_id or "UNKNOWN_RELEASE"
        self.phase = phase
        self.submit_calls = 0
        self.cancel_calls = 0
        self.modify_calls = 0

    @property
    def mutation_attempt_count(self) -> int:
        return self.submit_calls + self.cancel_calls + self.modify_calls

    def _record(self, action: str, payload: Mapping[str, Any] | None = None) -> None:
        if self.engine is not None:
            persist_canary_event(
                self.engine,
                release_id=self.release_id,
                event_type="MUTATION_ATTEMPT",
                status="FAIL",
                reason="CANARY_MUTATION_ATTEMPT",
                mutation_attempt=True,
                payload={"action": action, "phase": self.phase, **dict(payload or {})},
            )

    async def submit(self, decision: Mapping[str, Any], market_ctx: Mapping[str, Any]) -> Mapping[str, Any]:
        self.submit_calls += 1
        self._record("submit", {"symbol": market_ctx.get("symbol")})
        raise RuntimeError("CANARY_MUTATION_ATTEMPT")

    async def cancel(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        self.cancel_calls += 1
        self._record("cancel", {"args": [str(a) for a in args], "kwargs": {k: str(v) for k, v in kwargs.items()}})
        raise RuntimeError("CANARY_MUTATION_ATTEMPT")

    async def modify(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        self.modify_calls += 1
        self._record("modify", {"args": [str(a) for a in args], "kwargs": {k: str(v) for k, v in kwargs.items()}})
        raise RuntimeError("CANARY_MUTATION_ATTEMPT")
