"""Bounded SQLite maintenance. Safety evidence is protected unless archived.

Inventory and planning are read-only. Maintenance never rewrites a historical
payload, unlinks a WAL, or vacuums an actively written database.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import fcntl
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from alphaforge.config_registry import CONFIG_REGISTRY, canonical_field_default
from alphaforge.sqlite_safety import SQLiteBusyExhausted, is_sqlite_full_error, run_sqlite_write_with_retry, sqlite_writer_guard
from alphaforge.universe_evidence import load_persisted_universe_selection

POLICY_VERSION = "sqlite-storage-v1"
SETTINGS = tuple(s for s in CONFIG_REGISTRY if s.field_name.startswith("storage_"))
UNIVERSE_TABLES = ("burnin_universe_selection_links", "universe_selection_candidates", "universe_selection_cycles")


@dataclass(frozen=True)
class StoragePolicy:
    enabled: bool
    high_bytes: int
    low_bytes: int
    min_age_sec: float
    check_interval_sec: float
    batch_rows: int
    batch_seconds: float
    free_reserve_bytes: int
    growth_budget_bytes_per_sec: int
    archive_dir: str
    stats_only_enabled: bool
    stats_only_start_bytes: int
    stats_only_target_bytes: int
    stats_only_min_age_sec: float
    stats_only_recent_cycles: int

    def __post_init__(self):
        for setting in SETTINGS:
            value = getattr(self, setting.field_name.removeprefix("storage_"))
            if setting.parse(value) != value:
                raise ValueError(f"invalid typed storage setting: {setting.field_name}")
        if not self.high_bytes > self.low_bytes > 0:
            raise ValueError("storage high_bytes must exceed low_bytes > 0")
        if self.stats_only_enabled and not (
            self.high_bytes > self.stats_only_start_bytes
            > self.stats_only_target_bytes >= self.low_bytes > 0
        ):
            raise ValueError(
                "stats-only storage requires high_bytes > start_bytes > "
                "target_bytes >= low_bytes > 0"
            )

    @classmethod
    def from_values(cls, values):
        return cls(**{s.field_name.removeprefix("storage_"): s.parse(values.get(s.env_name, s.default)) for s in SETTINGS})

    @classmethod
    def from_config(cls, config):
        return cls(**{s.field_name.removeprefix("storage_"): getattr(config, s.field_name, canonical_field_default(s.field_name)) for s in SETTINGS})

    def identity(self):
        return {"version": POLICY_VERSION, **asdict(self)}

    def identity_hash(self):
        payload = json.dumps(self.identity(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def reserve(self, wal_bytes: int = 0, duration_seconds: float = 0):
        # Configured budget, not an invented measured growth forecast. Duration
        # adds headroom only when an operator has configured a growth budget.
        return (self.free_reserve_bytes + self.high_bytes - self.low_bytes
                + wal_bytes + int(max(0, duration_seconds) * self.growth_budget_bytes_per_sec))


def database_path(engine: Engine) -> Path:
    if engine.dialect.name != "sqlite" or not engine.url.database or engine.url.database == ":memory:":
        raise ValueError("STORAGE_REQUIRES_FILE_SQLITE")
    return Path(engine.url.database).expanduser().resolve()


def readonly(path: Path):
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.05)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _q(name: str):
    return '"' + name.replace('"', '""') + '"'


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({_q(table)})")}


def _size(path):
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def disk_budget(path: Path, policy: StoragePolicy, *, duration_seconds=0):
    parent = path.resolve().parent
    while not parent.exists():
        parent = parent.parent
    free = shutil.disk_usage(parent).free
    wal = _size(Path(str(path) + "-wal"))
    reserve = policy.reserve(wal, duration_seconds)
    return {"free_bytes": free, "required_reserve_bytes": reserve,
            "duration_seconds": duration_seconds,
            "growth_budget_bytes_per_sec": policy.growth_budget_bytes_per_sec,
            "status": "PASS" if free >= reserve else "STORAGE_DISK_RESERVE_INSUFFICIENT"}


def inventory(path: str | Path, policy: StoragePolicy, *, detailed=True):
    path = Path(path).expanduser().resolve()
    with readonly(path) as conn:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
        schema = conn.execute("PRAGMA schema_version").fetchone()[0]
        allocation = None
        allocation_reason = None
        if detailed:
            try:
                allocation = [dict(r) for r in conn.execute("SELECT name,SUM(pgsize) AS bytes,COUNT(*) AS pages FROM dbstat GROUP BY name ORDER BY bytes DESC")]
            except sqlite3.OperationalError as exc:
                if "no such table: dbstat" not in str(exc):
                    raise
                allocation_reason = "DBSTAT_UNAVAILABLE"
        manifest_rows = ([dict(r) for r in conn.execute("SELECT campaign_id,archive_path,checksum,state FROM storage_archives")]
                         if "storage_archives" in tables(conn) else [])
        retention_scopes = []
        campaign_files = []
        if detailed:
            tab = tables(conn)
            timestamps = {"runtime_heartbeats": "heartbeat_ts", "runtime_state_snapshots": "timestamp", "burnin_health_history": "generated_at", "universe_selection_cycles": "decision_timestamp"}
            for table in sorted(tab):
                timestamp = timestamps.get(table)
                oldest = (conn.execute(f"SELECT MIN({_q(timestamp)}) FROM {_q(table)}").fetchone()[0]
                          if timestamp and timestamp in columns(conn, table) else None)
                retention_scopes.append({"table": table, "rows": conn.execute(f"SELECT COUNT(*) FROM {_q(table)}").fetchone()[0],
                                         "oldest_timestamp": oldest,
                                         "preservation_reason": "ELIGIBILITY_GRAPH_REQUIRED"})
            # Inventory artifact locations declared by the database, rather
            # than guessing ownership of arbitrary directories on the host.
            directories = set()
            for table in ("burnin_campaign_exports", "burnin_preflight_reports"):
                if table in tab and "output_dir" in columns(conn, table):
                    directories.update(str(r[0]) for r in conn.execute(f"SELECT DISTINCT output_dir FROM {table}") if r[0])
            for directory in sorted(directories):
                location = Path(directory).expanduser().resolve()
                measured = 0
                files = 0
                truncated = False
                if location.is_dir():
                    for owned in location.rglob("*"):
                        if owned.is_file():
                            measured += owned.stat().st_size
                            files += 1
                        if files >= 10000:
                            truncated = True
                            break
                campaign_files.append({"path": str(location), "measured_bytes": measured,
                                       "files": files, "available": location.is_dir(), "truncated": truncated})
    main, wal, shm = (_size(Path(str(path) + suffix)) for suffix in ("", "-wal", "-shm"))
    result = {"policy": policy.identity(), "database_path": str(path),
              "database_file_identity": {"device": path.stat().st_dev, "inode": path.stat().st_ino},
              "main_bytes": main, "wal_bytes": wal, "shm_bytes": shm,
              "physical_bytes": main + wal, "page_size": page_size, "page_count": page_count,
              "freelist_count": free_pages, "reusable_bytes": free_pages * page_size,
              "allocated_live_bytes": (page_count - free_pages) * page_size,
              "schema_version": schema, "allocations": allocation,
              "allocation_unavailable_reason": allocation_reason,
              "archives": [{**r, "physical_bytes": _size(Path(r["archive_path"]))} for r in manifest_rows],
              **disk_budget(path, policy)}
    if detailed:
        # Files are attributed by the concrete DB basename, never a host-wide
        # recursive glob or inferred ownership of unrelated database files.
        result["owned_sidecar_files"] = [{"path": str(p), "bytes": _size(p)} for p in sorted(path.parent.glob(path.name + ".*")) if p.is_file()]
        result["owned_sidecar_scope"] = "DB_BASENAME_ONLY; campaign artifacts require explicit operator inventory"
        result["table_retention_scopes"] = retention_scopes
        result["owned_campaign_files"] = campaign_files
        with readonly(path) as conn:
            result["retention_plan"] = plan_telemetry(conn, policy)
    return result


def _incoming_references(conn, target):
    return [name for name in tables(conn)
            if any(r[2] == target for r in conn.execute(f"PRAGMA foreign_key_list({_q(name)})"))]


def plan_telemetry(conn, policy: StoragePolicy, *, now: float | None = None):
    """Only healthy standalone redundant heartbeats are dispensable.

    State snapshots are qualification/audit/recovery authority. Health history
    supplies transition diagnostics. Both remain protected in this version.
    Campaign-bound heartbeat instances also remain protected.
    """
    now = time.time() if now is None else now
    tab = tables(conn)
    protected = [{"table": t, "reason": "CANONICAL_STATE_TRANSITION_OR_CAMPAIGN_EVIDENCE"}
                 for t in sorted(tab - {"runtime_heartbeats", "storage_archive_authorizations"})]
    if "runtime_heartbeats" not in tab:
        return {"ids": [], "protected": protected, "reason": "NO_ELIGIBLE_TELEMETRY_TABLE"}
    required = {"id", "runtime_instance_id", "execution_mode", "heartbeat_ts", "runtime_state", "evidence_status", "active_positions_count", "pending_orders_count"}
    if not required <= columns(conn, "runtime_heartbeats") or _incoming_references(conn, "runtime_heartbeats"):
        return {"ids": [], "protected": protected, "reason": "UNKNOWN_OR_REFERENCED_HEARTBEAT_SCHEMA"}
    bound = ""
    if "runtime_state_snapshots" in tab:
        state_cols = columns(conn, "runtime_state_snapshots")
        if not {"instance_id", "campaign_id", "burnin_run_id"} <= state_cols:
            return {"ids": [], "protected": protected, "reason": "HEARTBEAT_OWNERSHIP_UNAVAILABLE"}
        bound = " AND NOT EXISTS (SELECT 1 FROM runtime_state_snapshots s WHERE s.instance_id=h.runtime_instance_id AND (s.campaign_id IS NOT NULL OR s.burnin_run_id IS NOT NULL))"
    # Never change historical qualification sample counts or audit evidence.
    for authority in sorted(tab):
        if ("qualification" in authority or authority.startswith("audit_")) and conn.execute(f"SELECT 1 FROM {_q(authority)} LIMIT 1").fetchone():
            return {"ids": [], "protected": protected, "reason": "QUALIFICATION_OR_AUDIT_TELEMETRY_DEPENDENCY"}
    cutoff = datetime.fromtimestamp(now - policy.min_age_sec, timezone.utc).isoformat()
    query = f"""SELECT h.id,h.heartbeat_ts FROM runtime_heartbeats h
        WHERE julianday(h.heartbeat_ts)<julianday(?) AND h.runtime_state='OPERATING'
          AND h.evidence_status='MEASURED_RUNTIME_HEARTBEAT' AND h.active_positions_count=0 AND h.pending_orders_count=0
          {bound}
          AND EXISTS (SELECT 1 FROM runtime_heartbeats newer
            WHERE newer.runtime_instance_id=h.runtime_instance_id AND newer.execution_mode=h.execution_mode
              AND newer.runtime_state=h.runtime_state AND newer.evidence_status=h.evidence_status
              AND newer.active_positions_count=0 AND newer.pending_orders_count=0 AND newer.id>h.id)
        ORDER BY h.id LIMIT ?"""
    rows = conn.execute(query, (cutoff, policy.batch_rows)).fetchall()
    return {"ids": [r[0] for r in rows], "oldest_eligible_timestamp": rows[0][1] if rows else None,
            "eligible_rows_in_batch": len(rows), "protected": protected,
            "reason": "AGED_REDUNDANT_STANDALONE_HEARTBEAT" if rows else "PROTECTED_OR_TOO_RECENT"}


def prune_telemetry(engine, policy):
    deadline = time.monotonic() + policy.batch_seconds
    def operation(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        raw = conn.connection.driver_connection
        raw.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            plan = plan_telemetry(raw, policy)
            removed = 0
            for row_id in plan["ids"]:
                if time.monotonic() >= deadline:
                    break
                removed += conn.exec_driver_sql("DELETE FROM runtime_heartbeats WHERE id=?", (row_id,)).rowcount
            return {"rows_removed": removed, "reason": plan["reason"]}
        finally:
            raw.set_progress_handler(None, 0)
    try:
        return run_sqlite_write_with_retry(engine, operation, operation_name="storage_telemetry_retention")
    except OperationalError as exc:
        if "interrupted" not in str(exc):
            raise
        return {"rows_removed": 0, "reason": "MAINTENANCE_TIME_BUDGET_EXHAUSTED"}


class MaintenanceBudgetExhausted(ValueError):
    """A bounded maintenance transaction rolled back without deleting evidence."""


def run_bounded_maintenance_write(engine, policy, operation, *, operation_name):
    # Bound dependency scans/schema work as well as the final DELETE loop.
    # The guard/retry authority rolls back interrupted transactions. Install the
    # deadline only after arbitration so waiting for another legitimate writer
    # does not consume the maintenance SQL budget.
    def bounded(conn):
        raw = conn.connection.driver_connection
        deadline = time.monotonic() + policy.batch_seconds
        raw.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            return operation(conn)
        finally:
            raw.set_progress_handler(None, 0)
    try:
        return run_sqlite_write_with_retry(engine, bounded, operation_name=operation_name)
    except OperationalError as exc:
        if getattr(exc.orig, "sqlite_errorcode", None) != sqlite3.SQLITE_INTERRUPT:
            raise
        raise MaintenanceBudgetExhausted("MAINTENANCE_TIME_BUDGET_EXHAUSTED") from exc


def checkpoint(engine, *, truncate=False):
    # PASSIVE does not wait on a long reader and never unlinks sidecar files.
    mode = "TRUNCATE" if truncate else "PASSIVE"
    with sqlite_writer_guard(engine), engine.connect() as conn:
        timeout = conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
        try:
            if truncate:
                conn.exec_driver_sql("PRAGMA busy_timeout=0")
            busy, frames, completed = conn.exec_driver_sql(f"PRAGMA wal_checkpoint({mode})").one()
        finally:
            if truncate:
                conn.exec_driver_sql(f"PRAGMA busy_timeout={timeout}")
    return {"busy": bool(busy or (frames >= 0 and completed < frames)), "wal_frames": frames,
            "checkpointed_frames": completed, "mode": mode}


ARCHIVE_DDL = (
    """CREATE TABLE IF NOT EXISTS storage_archives (
        campaign_id TEXT PRIMARY KEY, archive_path TEXT NOT NULL, checksum TEXT NOT NULL,
        manifest_json TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('VERIFIED','REMOVING','ARCHIVED'))
    )""",
    """CREATE TABLE IF NOT EXISTS storage_archive_authorizations (
        token TEXT NOT NULL, campaign_id TEXT NOT NULL, cycle_id TEXT PRIMARY KEY,
        evidence_hash TEXT NOT NULL, FOREIGN KEY(campaign_id) REFERENCES storage_archives(campaign_id)
    )""",
    """CREATE TRIGGER IF NOT EXISTS storage_archive_authorization_guard
        BEFORE INSERT ON storage_archive_authorizations
        WHEN COALESCE(storage_archive_authorize(NEW.token),0) <> 1
        BEGIN SELECT RAISE(ABORT,'archive authorization required'); END""",
)

STATS_ONLY_SCHEMA_VERSION = "paper-universe-stats-only-v1"
STATS_ONLY_TABLES = {
    "storage_universe_compaction_rollups",
    "storage_universe_compaction_checkpoints",
    "storage_stats_only_authorizations",
}
STATS_ONLY_DDL = (
    """CREATE TABLE IF NOT EXISTS storage_universe_compaction_rollups (
        cycle_id TEXT PRIMARY KEY,
        campaign_id TEXT NOT NULL,
        burnin_run_id TEXT NOT NULL,
        decision_timestamp REAL NOT NULL,
        execution_mode TEXT NOT NULL CHECK(execution_mode='PAPER'),
        campaign_config_hash TEXT NOT NULL,
        config_hash TEXT NOT NULL,
        strategy_config_hash TEXT,
        universe_hash TEXT NOT NULL,
        evidence_hash TEXT NOT NULL,
        git_sha TEXT NOT NULL,
        candidate_count INTEGER NOT NULL CHECK(candidate_count >= 0),
        eligible_count INTEGER NOT NULL CHECK(eligible_count >= 0),
        selected_count INTEGER NOT NULL CHECK(selected_count >= 0),
        rejected_count INTEGER NOT NULL CHECK(rejected_count >= 0),
        state_counts_json TEXT NOT NULL,
        exclusion_reason_counts_json TEXT NOT NULL,
        score_summary_json TEXT NOT NULL,
        selected_symbols_json TEXT NOT NULL,
        source_rows_hash TEXT NOT NULL,
        policy_hash TEXT NOT NULL,
        rollup_hash TEXT NOT NULL UNIQUE,
        compacted_at TEXT NOT NULL,
        schema_version TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS storage_universe_compaction_checkpoints (
        checkpoint_id TEXT PRIMARY KEY,
        campaign_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        cycle_id TEXT NOT NULL UNIQUE,
        previous_checkpoint_hash TEXT,
        cumulative_cycles INTEGER NOT NULL,
        cumulative_candidates INTEGER NOT NULL,
        cumulative_eligible INTEGER NOT NULL,
        cumulative_selected INTEGER NOT NULL,
        cumulative_rejected INTEGER NOT NULL,
        checkpoint_hash TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        UNIQUE(campaign_id, sequence),
        FOREIGN KEY(cycle_id) REFERENCES storage_universe_compaction_rollups(cycle_id)
    )""",
    """CREATE TABLE IF NOT EXISTS storage_stats_only_authorizations (
        token TEXT NOT NULL,
        campaign_id TEXT NOT NULL,
        cycle_id TEXT PRIMARY KEY,
        evidence_hash TEXT NOT NULL,
        rollup_hash TEXT NOT NULL,
        FOREIGN KEY(cycle_id) REFERENCES storage_universe_compaction_rollups(cycle_id)
    )""",
    """CREATE TRIGGER IF NOT EXISTS storage_stats_only_authorization_guard
        BEFORE INSERT ON storage_stats_only_authorizations
        WHEN COALESCE(storage_stats_only_authorize(NEW.token),0) <> 1
        BEGIN SELECT RAISE(ABORT,'stats-only authorization required'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_storage_universe_compaction_rollups_no_update
        BEFORE UPDATE ON storage_universe_compaction_rollups
        BEGIN SELECT RAISE(ABORT,'stats-only rollup is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_storage_universe_compaction_rollups_no_delete
        BEFORE DELETE ON storage_universe_compaction_rollups
        BEGIN SELECT RAISE(ABORT,'stats-only rollup is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_storage_universe_compaction_checkpoints_no_update
        BEFORE UPDATE ON storage_universe_compaction_checkpoints
        BEGIN SELECT RAISE(ABORT,'stats-only checkpoint is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_storage_universe_compaction_checkpoints_no_delete
        BEFORE DELETE ON storage_universe_compaction_checkpoints
        BEGIN SELECT RAISE(ABORT,'stats-only checkpoint is immutable'); END""",
)


def _install_universe_delete_guards(conn):
    tab = tables(conn.connection.driver_connection)
    for table in UNIVERSE_TABLES:
        if table not in tab:
            continue
        trigger = f"trg_{table}_no_delete"
        conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger}")
        archive_hash = " AND a.evidence_hash=OLD.evidence_hash" if table == "universe_selection_cycles" else ""
        archive_campaign = " AND a.campaign_id=OLD.campaign_id" if table == "burnin_universe_selection_links" else ""
        stats_hash = " AND s.evidence_hash=OLD.evidence_hash" if table == "universe_selection_cycles" else ""
        stats_campaign = " AND s.campaign_id=OLD.campaign_id" if table == "burnin_universe_selection_links" else ""
        conn.exec_driver_sql(f"""CREATE TRIGGER {trigger} BEFORE DELETE ON {table}
            WHEN NOT EXISTS (SELECT 1 FROM storage_archive_authorizations a
              JOIN storage_archives m ON m.campaign_id=a.campaign_id
              WHERE a.cycle_id=OLD.cycle_id {archive_hash} {archive_campaign}
                AND m.state IN ('VERIFIED','REMOVING'))
             AND NOT EXISTS (SELECT 1 FROM storage_stats_only_authorizations s
              JOIN storage_universe_compaction_rollups r ON r.cycle_id=s.cycle_id
              WHERE s.cycle_id=OLD.cycle_id {stats_hash} {stats_campaign}
                AND s.rollup_hash=r.rollup_hash)
            BEGIN SELECT RAISE(ABORT,'universe selection evidence is immutable'); END""")


def ensure_stats_only_schema(conn):
    for sql in ARCHIVE_DDL:
        conn.exec_driver_sql(sql)
    for sql in STATS_ONLY_DDL:
        conn.exec_driver_sql(sql)
    _install_universe_delete_guards(conn)


def ensure_archive_schema(conn):
    for sql in ARCHIVE_DDL:
        conn.exec_driver_sql(sql)
    # Install both guarded capability paths so later stats-only bootstrap never
    # weakens archival immutability, and vice versa.
    for sql in STATS_ONLY_DDL:
        conn.exec_driver_sql(sql)
    _install_universe_delete_guards(conn)


def _schema_hash(conn):
    rows = [list(r) for r in conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def _file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path, value):
    tmp = path.with_suffix(path.suffix + ".partial")
    with tmp.open("w") as f:
        json.dump(value, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


@contextmanager
def _archive_guard(path):
    # File publication has separate cross-process arbitration. Backups and
    # verification never hold the production SQLite writer lock.
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("ARCHIVE_WORK_ALREADY_IN_PROGRESS") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _canonical_json_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _mapping(cursor, row) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return dict(zip((item[0] for item in cursor.description), row))


def _timestamp(value: Any) -> float:
    if isinstance(value, (float, int)):
        return float(value)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _stats_only_unknown_dependency(conn) -> str | None:
    known = set(UNIVERSE_TABLES) | STATS_ONLY_TABLES | {
        "storage_archive_authorizations", "exchange_reconciliation_events"
    }
    for table in sorted(tables(conn) - known):
        cols = columns(conn, table)
        if "cycle_id" in cols or any(
            "universe" in col and "cycle_id" in col for col in cols
        ) or any(row[2] in UNIVERSE_TABLES for row in conn.execute(
            f"PRAGMA foreign_key_list({_q(table)})"
        )):
            return f"UNKNOWN_UNIVERSE_DEPENDENCY:{table}"
    return None


def plan_stats_only_universe(conn, policy: StoragePolicy, *, now: float | None = None):
    """Return oldest safe PAPER cycles; unknown ownership fails closed."""
    if not policy.stats_only_enabled:
        return {"cycles": [], "reason": "STATS_ONLY_DISABLED"}
    tab = tables(conn)
    required = {
        "burnin_campaigns", "burnin_campaign_runs", "burnin_runs",
        "burnin_pending_reject_labels", "burnin_pending_position_outcomes",
        "runtime_state_snapshots", "storage_universe_compaction_rollups",
        *UNIVERSE_TABLES,
    }
    if not required <= tab:
        return {"cycles": [], "reason": "STATS_ONLY_SCHEMA_OR_OWNERSHIP_UNAVAILABLE"}
    contracts = {
        "burnin_campaigns": {
            "campaign_id", "config_hash", "strategy_config_hash", "git_commit", "source_provenance_json",
            "latest_qualification_id", "qualification_status",
        },
        "burnin_campaign_runs": {"campaign_id", "burnin_run_id"},
        "burnin_runs": {"burnin_run_id", "execution_mode", "git_commit", "config_hash"},
        "burnin_pending_reject_labels": {
            "campaign_id", "burnin_run_id", "decision_timestamp", "status",
        },
        "burnin_pending_position_outcomes": {
            "campaign_id", "burnin_run_id", "decision_time", "entry_time", "status",
        },
        "runtime_state_snapshots": {
            "campaign_id", "timestamp", "active_position_count", "pending_order_count",
            "recovery_action_required", "unknown_exchange_state",
        },
        "universe_selection_cycles": {
            "cycle_id", "decision_timestamp", "execution_mode", "evidence_hash",
            "candidate_count", "strategy_config_hash", "git_sha",
        },
        "universe_selection_candidates": {"cycle_id", "candidate_index"},
        "burnin_universe_selection_links": {
            "cycle_id", "campaign_id", "burnin_run_id", "decision_timestamp",
        },
    }
    if any(not needed <= columns(conn, table) for table, needed in contracts.items()):
        return {"cycles": [], "reason": "STATS_ONLY_SCHEMA_OR_OWNERSHIP_UNAVAILABLE"}
    dependency = _stats_only_unknown_dependency(conn)
    if dependency:
        return {"cycles": [], "reason": dependency}

    # Unscoped exposure/reconciliation authority cannot be attributed to a safe
    # historical cycle. Preserve every detail until the state is terminal.
    for table, field, terminal in (
        ("positions", "status", ("CLOSED",)),
        ("orders", "status", ("FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED")),
        ("reconciliation_incidents", "remediation_status", ("RESOLVED",)),
    ):
        if table not in tab:
            continue
        if field not in columns(conn, table):
            return {"cycles": [], "reason": "OUTSTANDING_OR_UNKNOWN_SAFETY_STATE"}
        placeholders = ",".join("?" for _ in terminal)
        if conn.execute(
            f"SELECT 1 FROM {_q(table)} WHERE {_q(field)} IS NULL "
            f"OR UPPER({_q(field)}) NOT IN ({placeholders}) LIMIT 1", terminal
        ).fetchone():
            return {"cycles": [], "reason": "OUTSTANDING_OR_UNKNOWN_SAFETY_STATE"}

    cutoff = float(time.time() if now is None else now) - policy.stats_only_min_age_sec
    cursor = conn.execute("""
        WITH ranked AS (
          SELECT l.campaign_id,l.burnin_run_id,c.*,
                 ROW_NUMBER() OVER (
                   PARTITION BY l.campaign_id
                   ORDER BY c.decision_timestamp DESC,c.cycle_id DESC
                 ) AS recent_rank,
                 COUNT(*) OVER (PARTITION BY c.cycle_id) AS link_count
          FROM universe_selection_cycles c
          JOIN burnin_universe_selection_links l ON l.cycle_id=c.cycle_id
        )
        SELECT r.*,bc.config_hash AS campaign_config_hash,
               bc.strategy_config_hash AS campaign_strategy_config_hash,
               bc.git_commit AS campaign_git_commit,
               bc.source_provenance_json AS campaign_source_provenance_json,
               bc.latest_qualification_id,
               bc.qualification_status
        FROM ranked r
        JOIN burnin_campaigns bc ON bc.campaign_id=r.campaign_id
        JOIN burnin_campaign_runs cr
          ON cr.campaign_id=r.campaign_id AND cr.burnin_run_id=r.burnin_run_id
        JOIN burnin_runs br ON br.burnin_run_id=r.burnin_run_id
        WHERE r.execution_mode='PAPER' AND br.execution_mode='PAPER'
          AND br.git_commit=bc.git_commit AND br.config_hash=bc.config_hash
          AND r.decision_timestamp<? AND r.recent_rank>?
          AND NOT EXISTS (
            SELECT 1 FROM storage_universe_compaction_rollups s
            WHERE s.cycle_id=r.cycle_id
          )
        ORDER BY r.decision_timestamp,r.cycle_id
    """, (cutoff, policy.stats_only_recent_cycles))
    rows = [_mapping(cursor, row) for row in cursor.fetchall()]
    chosen: list[dict[str, Any]] = []
    budget = 0
    blocked_reasons: list[str] = []
    for row in rows:
        try:
            provenance = json.loads(row["campaign_source_provenance_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            blocked_reasons.append("STATS_ONLY_CAMPAIGN_PROVENANCE_INVALID")
            continue
        if (
            provenance.get("storage_retention_mode") != "PAPER_STATS_ONLY"
            or provenance.get("storage_policy_hash") != policy.identity_hash()
        ):
            blocked_reasons.append("CAMPAIGN_NOT_BOUND_TO_STATS_ONLY_POLICY")
            continue
        if row["latest_qualification_id"] or row["qualification_status"]:
            blocked_reasons.append("QUALIFICATION_AUTHORITY_PROTECTED")
            continue
        if "burnin_qualification_snapshots" in tab and conn.execute("""
            SELECT 1 FROM burnin_qualification_snapshots q
            JOIN burnin_campaign_runs cr ON cr.burnin_run_id=q.burnin_run_id
            WHERE cr.campaign_id=? LIMIT 1
        """, (row["campaign_id"],)).fetchone():
            blocked_reasons.append("QUALIFICATION_AUTHORITY_PROTECTED")
            continue
        if int(row["link_count"]) != 1:
            blocked_reasons.append("SHARED_CYCLE_PROTECTED")
            continue
        if row["git_sha"] != row["campaign_git_commit"]:
            blocked_reasons.append("CYCLE_CAMPAIGN_IDENTITY_MISMATCH")
            continue
        if row["strategy_config_hash"] != row["campaign_strategy_config_hash"]:
            blocked_reasons.append("CYCLE_CAMPAIGN_IDENTITY_MISMATCH")
            continue
        snapshot_cursor = conn.execute("""
            SELECT active_position_count,pending_order_count,recovery_action_required,
                   unknown_exchange_state
            FROM runtime_state_snapshots WHERE campaign_id=?
            ORDER BY id DESC LIMIT 1
        """, (row["campaign_id"],))
        snapshot = snapshot_cursor.fetchone()
        if snapshot is None or any(value is None for value in snapshot) or any(int(value) != 0 for value in snapshot):
            blocked_reasons.append("RUNTIME_SAFETY_STATE_UNRESOLVED")
            continue

        unresolved_times: list[float] = []
        try:
            for value, in conn.execute("""
                SELECT decision_timestamp FROM burnin_pending_reject_labels
                WHERE campaign_id=? AND burnin_run_id=?
                  AND (status IS NULL OR UPPER(status)<>'RESOLVED')
            """, (row["campaign_id"], row["burnin_run_id"])):
                unresolved_times.append(_timestamp(value))
            for decision_time, entry_time in conn.execute("""
                SELECT decision_time,entry_time FROM burnin_pending_position_outcomes
                WHERE campaign_id=? AND burnin_run_id=?
                  AND (status IS NULL OR UPPER(status)<>'CLOSED')
            """, (row["campaign_id"], row["burnin_run_id"])):
                unresolved_times.append(_timestamp(decision_time or entry_time))
        except (TypeError, ValueError):
            blocked_reasons.append("UNRESOLVED_STATE_TIME_UNKNOWN")
            continue
        if unresolved_times and float(row["decision_timestamp"]) >= min(unresolved_times):
            blocked_reasons.append("UNRESOLVED_OUTCOME_WINDOW_PROTECTED")
            continue
        cycle_rows = int(row["candidate_count"]) + 2
        if cycle_rows > policy.batch_rows:
            blocked_reasons.append("CYCLE_EXCEEDS_MAINTENANCE_ROW_BUDGET")
            continue
        if budget + cycle_rows > policy.batch_rows:
            break
        budget += cycle_rows
        chosen.append(row)
    reason = "OLDEST_ELIGIBLE_PAPER_CYCLES" if chosen else (
        blocked_reasons[0] if blocked_reasons else "PROTECTED_OR_TOO_RECENT"
    )
    return {"cycles": chosen, "reason": reason, "planned_rows": budget,
            "blockers": sorted(set(blocked_reasons))}


def _score_summary(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [float(row["score"]) for row in candidates if row.get("score") is not None]
    return {
        "count": len(scores), "min": min(scores) if scores else None,
        "max": max(scores) if scores else None,
        "sum": sum(scores) if scores else 0.0,
    }


def _rollup_payload(conn, policy: StoragePolicy, row: dict[str, Any], compacted_at: str):
    payload = load_persisted_universe_selection(conn, row["cycle_id"])
    candidates = payload["candidates"]
    state_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    for candidate in candidates:
        state = str(candidate["state"])
        state_counts[state] = state_counts.get(state, 0) + 1
        for reason in sorted(set(map(str, candidate.get("reasons") or []))):
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    link_cursor = conn.execute("""
        SELECT link_id,campaign_id,burnin_run_id,cycle_id,decision_timestamp,schema_version
        FROM burnin_universe_selection_links WHERE cycle_id=?
    """, (row["cycle_id"],))
    links = [_mapping(link_cursor, item) for item in link_cursor.fetchall()]
    if len(links) != 1:
        raise ValueError("SHARED_OR_MISSING_CYCLE_LINK")
    eligible = state_counts.get("ELIGIBLE", 0)
    selected = sum(bool(candidate.get("selected")) for candidate in candidates)
    core = {
        "cycle_id": row["cycle_id"], "campaign_id": row["campaign_id"],
        "burnin_run_id": row["burnin_run_id"],
        "decision_timestamp": float(row["decision_timestamp"]),
        "execution_mode": "PAPER",
        "campaign_config_hash": row["campaign_config_hash"],
        "config_hash": payload["config_hash"],
        "strategy_config_hash": payload.get("strategy_config_hash"),
        "universe_hash": payload["universe_hash"],
        "evidence_hash": payload["evidence_hash"], "git_sha": payload["git_sha"],
        "candidate_count": len(candidates), "eligible_count": eligible,
        "selected_count": selected, "rejected_count": len(candidates) - eligible,
        "state_counts_json": json.dumps(state_counts, sort_keys=True, separators=(",", ":")),
        "exclusion_reason_counts_json": json.dumps(reason_counts, sort_keys=True, separators=(",", ":")),
        "score_summary_json": json.dumps(_score_summary(candidates), sort_keys=True, separators=(",", ":"), allow_nan=False),
        "selected_symbols_json": json.dumps(payload["selected_symbols"], sort_keys=True, separators=(",", ":")),
        "source_rows_hash": _canonical_json_hash({"selection": payload, "links": links}),
        "policy_hash": policy.identity_hash(), "compacted_at": compacted_at,
        "schema_version": STATS_ONLY_SCHEMA_VERSION,
    }
    return {**core, "rollup_hash": _canonical_json_hash(core)}


def stats_only_campaign_summary(conn, campaign_id: str) -> dict[str, Any]:
    """Read-only, hash-verified statistics. Full replay remains unavailable."""
    tab = tables(conn)
    if not {"storage_universe_compaction_rollups", "storage_universe_compaction_checkpoints"} <= tab:
        return {"status": "NO_STATS_ONLY_COMPACTION", "campaign_id": campaign_id,
                "qualification_eligible": True, "full_universe_replay_available": True}
    rollup_cursor = conn.execute("""
        SELECT * FROM storage_universe_compaction_rollups
        WHERE campaign_id=? ORDER BY decision_timestamp,cycle_id
    """, (campaign_id,))
    rollups = [_mapping(rollup_cursor, row) for row in rollup_cursor.fetchall()]
    checkpoint_cursor = conn.execute("""
        SELECT * FROM storage_universe_compaction_checkpoints
        WHERE campaign_id=? ORDER BY sequence
    """, (campaign_id,))
    checkpoints = [_mapping(checkpoint_cursor, row) for row in checkpoint_cursor.fetchall()]
    if not rollups and not checkpoints:
        return {"status": "NO_STATS_ONLY_COMPACTION", "campaign_id": campaign_id,
                "qualification_eligible": True, "full_universe_replay_available": True}
    if len(rollups) != len(checkpoints):
        raise ValueError("STATS_ONLY_LEDGER_INCOMPLETE")
    for rollup in rollups:
        expected = dict(rollup)
        actual = expected.pop("rollup_hash")
        if _canonical_json_hash(expected) != actual:
            raise ValueError(f"STATS_ONLY_ROLLUP_HASH_INVALID:{rollup['cycle_id']}")
    prior = None
    cumulative = {"cycles": 0, "candidates": 0, "eligible": 0, "selected": 0, "rejected": 0}
    by_cycle = {row["cycle_id"]: row for row in rollups}
    for sequence, checkpoint_row in enumerate(checkpoints, 1):
        checkpoint = dict(checkpoint_row)
        actual_hash = checkpoint.pop("checkpoint_hash")
        if checkpoint["sequence"] != sequence or checkpoint["previous_checkpoint_hash"] != prior:
            raise ValueError("STATS_ONLY_CHECKPOINT_CHAIN_INVALID")
        rollup = by_cycle.get(checkpoint["cycle_id"])
        if rollup is None:
            raise ValueError("STATS_ONLY_CHECKPOINT_ROLLUP_MISSING")
        if checkpoint["cycle_id"] != rollups[sequence - 1]["cycle_id"]:
            raise ValueError("STATS_ONLY_CHECKPOINT_ORDER_INVALID")
        cumulative["cycles"] += 1
        rollup_counts = {
            "candidates": "candidate_count", "eligible": "eligible_count",
            "selected": "selected_count", "rejected": "rejected_count",
        }
        for key, column in rollup_counts.items():
            cumulative[key] += int(rollup[column])
        if any(int(checkpoint[f"cumulative_{key}"]) != value for key, value in cumulative.items()):
            raise ValueError("STATS_ONLY_CHECKPOINT_TOTAL_INVALID")
        if _canonical_json_hash(checkpoint) != actual_hash:
            raise ValueError("STATS_ONLY_CHECKPOINT_HASH_INVALID")
        prior = actual_hash
    return {
        "status": "STATS_ONLY_VERIFIED", "campaign_id": campaign_id,
        "qualification_eligible": False, "full_universe_replay_available": False,
        "compacted_cycle_count": cumulative["cycles"],
        "cumulative_candidate_count": cumulative["candidates"],
        "cumulative_eligible_count": cumulative["eligible"],
        "cumulative_selected_count": cumulative["selected"],
        "cumulative_rejected_count": cumulative["rejected"],
        "latest_checkpoint_hash": prior,
    }


def compact_stats_only_universe_batch(engine, policy: StoragePolicy):
    token = secrets.token_hex(24)
    compacted_at = datetime.now(timezone.utc).isoformat()

    def operation(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        ensure_stats_only_schema(conn)
        raw = conn.connection.driver_connection
        plan = plan_stats_only_universe(raw, policy)
        deadline = time.monotonic() + policy.batch_seconds
        removed = 0
        compacted = 0
        validated_campaigns: set[str] = set()
        raw.create_function("storage_stats_only_authorize", 1, lambda value: int(value == token))
        try:
            for row in plan["cycles"]:
                if time.monotonic() >= deadline:
                    raise MaintenanceBudgetExhausted("MAINTENANCE_TIME_BUDGET_EXHAUSTED")
                if row["campaign_id"] not in validated_campaigns:
                    stats_only_campaign_summary(raw, row["campaign_id"])
                    validated_campaigns.add(row["campaign_id"])
                rollup = _rollup_payload(raw, policy, row, compacted_at)
                if time.monotonic() >= deadline:
                    raise MaintenanceBudgetExhausted("MAINTENANCE_TIME_BUDGET_EXHAUSTED")
                existing = raw.execute(
                    "SELECT rollup_hash FROM storage_universe_compaction_rollups WHERE cycle_id=?",
                    (row["cycle_id"],),
                ).fetchone()
                if existing:
                    raise ValueError("STATS_ONLY_SOURCE_AND_TOMBSTONE_BOTH_PRESENT")
                columns_sql = ",".join(rollup)
                placeholders = ",".join("?" for _ in rollup)
                raw.execute(
                    f"INSERT INTO storage_universe_compaction_rollups ({columns_sql}) VALUES ({placeholders})",
                    tuple(rollup.values()),
                )
                prior_cursor = raw.execute("""
                    SELECT * FROM storage_universe_compaction_checkpoints
                    WHERE campaign_id=? ORDER BY sequence DESC LIMIT 1
                """, (row["campaign_id"],))
                prior_row = prior_cursor.fetchone()
                prior = _mapping(prior_cursor, prior_row) if prior_row else None
                sequence = int(prior["sequence"] if prior else 0) + 1
                checkpoint = {
                    "checkpoint_id": f"stats-only:{row['campaign_id']}:{sequence}",
                    "campaign_id": row["campaign_id"], "sequence": sequence,
                    "cycle_id": row["cycle_id"],
                    "previous_checkpoint_hash": prior["checkpoint_hash"] if prior else None,
                    "cumulative_cycles": int(prior["cumulative_cycles"] if prior else 0) + 1,
                    "cumulative_candidates": int(prior["cumulative_candidates"] if prior else 0) + rollup["candidate_count"],
                    "cumulative_eligible": int(prior["cumulative_eligible"] if prior else 0) + rollup["eligible_count"],
                    "cumulative_selected": int(prior["cumulative_selected"] if prior else 0) + rollup["selected_count"],
                    "cumulative_rejected": int(prior["cumulative_rejected"] if prior else 0) + rollup["rejected_count"],
                    "created_at": compacted_at, "schema_version": STATS_ONLY_SCHEMA_VERSION,
                }
                checkpoint["checkpoint_hash"] = _canonical_json_hash(checkpoint)
                checkpoint_columns = ",".join(checkpoint)
                checkpoint_placeholders = ",".join("?" for _ in checkpoint)
                raw.execute(
                    f"INSERT INTO storage_universe_compaction_checkpoints ({checkpoint_columns}) VALUES ({checkpoint_placeholders})",
                    tuple(checkpoint.values()),
                )
                raw.execute(
                    "INSERT INTO storage_stats_only_authorizations VALUES (?,?,?,?,?)",
                    (token, row["campaign_id"], row["cycle_id"], rollup["evidence_hash"], rollup["rollup_hash"]),
                )
                deleted_links = raw.execute(
                    "DELETE FROM burnin_universe_selection_links WHERE cycle_id=?", (row["cycle_id"],)
                ).rowcount
                deleted_candidates = raw.execute(
                    "DELETE FROM universe_selection_candidates WHERE cycle_id=?", (row["cycle_id"],)
                ).rowcount
                deleted_cycle = raw.execute(
                    "DELETE FROM universe_selection_cycles WHERE cycle_id=?", (row["cycle_id"],)
                ).rowcount
                if (deleted_links, deleted_candidates, deleted_cycle) != (1, rollup["candidate_count"], 1):
                    raise ValueError("STATS_ONLY_DELETE_COUNT_MISMATCH")
                raw.execute("DELETE FROM storage_stats_only_authorizations WHERE cycle_id=?", (row["cycle_id"],))
                removed += deleted_links + deleted_candidates + deleted_cycle
                compacted += 1
        finally:
            raw.create_function("storage_stats_only_authorize", 1, lambda value: 0)
        return {"rows_removed": removed, "cycles_compacted": compacted,
                "reason": plan["reason"], "blockers": plan.get("blockers", [])}

    return run_bounded_maintenance_write(
        engine, policy, operation, operation_name="storage_stats_only_universe_retention"
    )


def campaign_eligibility(conn, campaign_id, *, min_age_sec=0):
    tab = tables(conn)
    required = {"burnin_campaigns", "burnin_campaign_runs", "burnin_runs", "burnin_pending_reject_labels", "burnin_pending_position_outcomes", *UNIVERSE_TABLES}
    if not required <= tab:
        return "ARCHIVE_SCHEMA_OR_OWNERSHIP_UNAVAILABLE"
    contracts = {
        "burnin_campaigns": {"campaign_id", "campaign_status", "completed_at", "worker_pid", "latest_qualification_id"},
        "burnin_campaign_runs": {"campaign_id", "burnin_run_id", "status"},
        "burnin_runs": {"burnin_run_id", "status", "open_trade_count"},
        "burnin_pending_reject_labels": {"burnin_run_id", "status"},
        "burnin_pending_position_outcomes": {"burnin_run_id", "status"},
        "universe_selection_cycles": {"cycle_id", "evidence_hash", "payload_json", "candidate_count"},
        "universe_selection_candidates": {"cycle_id", "candidate_index"},
        "burnin_universe_selection_links": {"cycle_id", "campaign_id", "burnin_run_id"},
    }
    if any(not needed <= columns(conn, table) for table, needed in contracts.items()):
        return "ARCHIVE_SCHEMA_OR_OWNERSHIP_UNAVAILABLE"
    campaign = conn.execute("SELECT * FROM burnin_campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
    if campaign is None:
        return "CAMPAIGN_MISSING"
    if ("storage_universe_compaction_rollups" in tab and conn.execute(
        "SELECT 1 FROM storage_universe_compaction_rollups WHERE campaign_id=? LIMIT 1",
        (campaign_id,),
    ).fetchone()):
        return "STATS_ONLY_CAMPAIGN_NOT_FULL_ARCHIVE_ELIGIBLE"
    campaign = dict(zip([r[1] for r in conn.execute("PRAGMA table_info(burnin_campaigns)")], campaign))
    if campaign.get("campaign_status") != "COMPLETED" or not campaign.get("completed_at"):
        return "UNFINISHED_FAILED_OR_ACCEPTANCE_CAMPAIGN_PROTECTED"
    try:
        completed = datetime.fromisoformat(campaign["completed_at"].replace("Z", "+00:00"))
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=timezone.utc)
        if time.time() - completed.timestamp() < min_age_sec:
            return "TERMINAL_CAMPAIGN_TOO_RECENT"
    except (ValueError, TypeError):
        return "TERMINAL_CAMPAIGN_TIME_UNKNOWN"
    if campaign.get("worker_pid") or campaign.get("latest_qualification_id"):
        return "WORKER_OR_QUALIFICATION_DEPENDENCY"
    runs = [r[0] for r in conn.execute("SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id=?", (campaign_id,))]
    if not runs:
        return "CAMPAIGN_RUN_OWNERSHIP_UNAVAILABLE"
    # Aggregate qualifications can reference source runs indirectly in JSON.
    # Protect them unless the dependency graph proves removal safe.
    for authority in sorted(tab):
        if ("qualification" in authority or authority.startswith("audit_")) and conn.execute(f"SELECT 1 FROM {_q(authority)} LIMIT 1").fetchone():
            return "QUALIFICATION_OR_AUDIT_DEPENDENCY"
    for run_id in runs:
        if conn.execute("SELECT 1 FROM burnin_campaign_runs WHERE burnin_run_id=? AND (campaign_id<>? OR status<>'COMPLETED') LIMIT 1", (run_id, campaign_id)).fetchone():
            return "AMBIGUOUS_OR_NONTERMINAL_RUN_OWNERSHIP"
        row = conn.execute("SELECT status,open_trade_count FROM burnin_runs WHERE burnin_run_id=?", (run_id,)).fetchone()
        if row is None or row[0] != "COMPLETED" or row[1] is None or row[1] != 0:
            return "NONTERMINAL_OR_OPEN_RUN"
        for table in ("burnin_pending_reject_labels", "burnin_pending_position_outcomes"):
            # Mature records also stay protected if their status is unknown.
            statuses = ("RESOLVED",) if table.endswith("reject_labels") else ("CLOSED",)
            ph = ",".join("?" for _ in statuses)
            if conn.execute(f"SELECT 1 FROM {table} WHERE burnin_run_id=? AND (status IS NULL OR status NOT IN ({ph})) LIMIT 1", (run_id, *statuses)).fetchone():
                return "UNRESOLVED_STATE"
        if "burnin_qualification_snapshots" in tab and conn.execute("SELECT 1 FROM burnin_qualification_snapshots WHERE burnin_run_id=? LIMIT 1", (run_id,)).fetchone():
            return "QUALIFICATION_DEPENDENCY"
    # Unscoped outstanding state cannot safely be attributed to another campaign.
    for table, field, terminal in (("positions", "status", ("CLOSED",)), ("orders", "status", ("FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED")), ("reconciliation_incidents", "remediation_status", ("RESOLVED",))):
        if table in tab:
            ph = ",".join("?" for _ in terminal)
            if field not in columns(conn, table) or conn.execute(f"SELECT 1 FROM {_q(table)} WHERE {_q(field)} IS NULL OR UPPER({_q(field)}) NOT IN ({ph}) LIMIT 1", terminal).fetchone():
                return "OUTSTANDING_OR_UNKNOWN_SAFETY_STATE"
    for table in tab - set(UNIVERSE_TABLES) - STATS_ONLY_TABLES - {"storage_archive_authorizations"}:
        cols = columns(conn, table)
        if any((c == "cycle_id" and table != "exchange_reconciliation_events") or "universe" in c and "cycle_id" in c for c in cols) or any(r[2] in UNIVERSE_TABLES for r in conn.execute(f"PRAGMA foreign_key_list({_q(table)})")):
            return f"UNKNOWN_UNIVERSE_DEPENDENCY:{table}"
    if conn.execute("""SELECT 1 FROM burnin_universe_selection_links a
         JOIN burnin_universe_selection_links b ON b.cycle_id=a.cycle_id
         WHERE a.campaign_id=? AND b.campaign_id<>a.campaign_id LIMIT 1""", (campaign_id,)).fetchone():
        return "SHARED_CYCLE_PROTECTED"
    if conn.execute("""SELECT 1 FROM burnin_universe_selection_links l
        LEFT JOIN universe_selection_cycles c ON c.cycle_id=l.cycle_id
        WHERE l.campaign_id=? AND (c.cycle_id IS NULL OR c.candidate_count IS NULL
            OR NOT json_valid(c.payload_json)
            OR c.candidate_count<>(SELECT COUNT(*) FROM universe_selection_candidates u WHERE u.cycle_id=c.cycle_id)
            OR NOT EXISTS (SELECT 1 FROM burnin_campaign_runs r
                WHERE r.campaign_id=l.campaign_id AND r.burnin_run_id=l.burnin_run_id)) LIMIT 1""", (campaign_id,)).fetchone():
        return "UNIVERSE_DOMAIN_OR_REPLAY_INCOMPLETE"
    # Normalize-era cycles are validated against their immutable candidate rows
    # *before* archival. A syntactically valid small v2 metadata JSON is not
    # enough to establish replay completeness or canonical evidence integrity.
    # Legacy full-payload rows retain the pre-#622 domain checks above.
    for cycle_id, payload_json in conn.execute("""
        SELECT c.cycle_id, c.payload_json FROM universe_selection_cycles c
        JOIN burnin_universe_selection_links l ON l.cycle_id=c.cycle_id
        WHERE l.campaign_id=? ORDER BY c.cycle_id
    """, (campaign_id,)).fetchall():
        try:
            payload = json.loads(payload_json)
            if isinstance(payload, dict) and payload.get("_storage_codec") is not None:
                load_persisted_universe_selection(conn, cycle_id)
        except (TypeError, ValueError, KeyError, IndexError, sqlite3.Error):
            return "UNIVERSE_DOMAIN_OR_REPLAY_INCOMPLETE"
    if "runtime_state_snapshots" in tab and "campaign_id" in columns(conn, "runtime_state_snapshots"):
        row = conn.execute("SELECT * FROM runtime_state_snapshots WHERE campaign_id=? ORDER BY id DESC LIMIT 1", (campaign_id,)).fetchone()
        if row:
            row = dict(zip([r[1] for r in conn.execute("PRAGMA table_info(runtime_state_snapshots)")], row))
            if row.get("runtime_status") not in {"STOPPED", "COMPLETED", "CLEAN_SHUTDOWN"} or any(row.get(k) != 0 for k in ("active_position_count", "pending_order_count", "recovery_action_required", "unknown_exchange_state")):
                return "RUNTIME_NOT_PROVEN_QUIESCENT"
        else:
            return "RUNTIME_QUIESCENCE_EVIDENCE_MISSING"
    else:
        return "RUNTIME_QUIESCENCE_EVIDENCE_MISSING"
    return None


def verified_archive(engine, policy, campaign_id, *, deadline_seconds=30):
    """WAL-aware full snapshot; no source writer lock during backup/verification."""
    path = database_path(engine)
    if not policy.archive_dir:
        raise ValueError("ARCHIVE_DESTINATION_MISSING")
    destination = Path(policy.archive_dir).expanduser().resolve()
    if not destination.is_dir():
        raise ValueError("ARCHIVE_DESTINATION_MISSING")
    key = hashlib.sha256((str(path) + "\0" + campaign_id).encode()).hexdigest()[:24]
    with _archive_guard(destination / f"{path.name}.{key}.archive.lock"):
        return _verified_archive(engine, policy, campaign_id, deadline_seconds=deadline_seconds)


def _verified_archive(engine, policy, campaign_id, *, deadline_seconds):
    path = database_path(engine)
    destination = Path(policy.archive_dir).expanduser().resolve()
    deadline = time.monotonic() + deadline_seconds
    with readonly(path) as source:
        reason = campaign_eligibility(source, campaign_id, min_age_sec=policy.min_age_sec)
        if reason:
            raise ValueError(reason)
        key = hashlib.sha256((str(path) + "\0" + campaign_id).encode()).hexdigest()[:24]
        archive = destination / f"{path.name}.{key}.archive.db"
        manifest_path = archive.with_suffix(archive.suffix + ".json")
        if not manifest_path.exists():
            if shutil.disk_usage(destination).free < _size(path) + policy.reserve(_size(Path(str(path) + "-wal"))):
                raise ValueError("ARCHIVE_DESTINATION_CAPACITY_INSUFFICIENT")
            partial = archive.with_suffix(archive.suffix + ".partial")
            partial.unlink(missing_ok=True)  # only unpublished incomplete work
            with sqlite3.connect(partial) as target:
                target.row_factory = sqlite3.Row
                def progress(status, remaining, total):
                    if time.monotonic() > deadline:
                        raise TimeoutError("ARCHIVE_TIME_BUDGET_EXHAUSTED")
                source.backup(target, pages=256, progress=progress, sleep=0.01)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or target.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("ARCHIVE_INTEGRITY_FAILED")
                if campaign_eligibility(target, campaign_id):
                    raise ValueError("ARCHIVE_DOMAIN_INCOMPLETE")
                counts = {t: target.execute(f"SELECT COUNT(*) FROM {_q(t)}").fetchone()[0] for t in sorted(tables(target))}
                # Read identity through the read-only source below; backup is
                # checked again through an independent connection after publish.
                schema_hash = _schema_hash(target)
            with partial.open("rb") as f:
                os.fsync(f.fileno())
            os.replace(partial, archive)
            _fsync_dir(destination)
            with readonly(archive) as archived:
                identity = dict(archived.execute("SELECT * FROM burnin_campaigns WHERE campaign_id=?", (campaign_id,)).fetchone())
            manifest = {"version": POLICY_VERSION, "campaign_id": campaign_id, "source_path": str(path),
                        "archive_path": str(archive), "checksum": _file_hash(archive), "schema_hash": schema_hash,
                        "row_counts": counts, "identity": identity, "verified_at": datetime.now(timezone.utc).isoformat(),
                        "same_device": destination.stat().st_dev == path.stat().st_dev,
                        "state": "VERIFIED"}
            _atomic_json(manifest_path, manifest)
        else:
            manifest = json.loads(manifest_path.read_text())
    verify_manifest(manifest)
    if manifest["campaign_id"] != campaign_id or manifest["source_path"] != str(path):
        raise ValueError("ARCHIVE_IDENTITY_MISMATCH")
    def publish(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        ensure_archive_schema(conn)
        raw = conn.connection.driver_connection
        reason = campaign_eligibility(raw, campaign_id)
        if reason:
            raise ValueError(reason)
        old = conn.exec_driver_sql("SELECT checksum FROM storage_archives WHERE campaign_id=?", (campaign_id,)).first()
        if old and old[0] != manifest["checksum"]:
            raise ValueError("ARCHIVE_MANIFEST_CONFLICT")
        conn.exec_driver_sql("INSERT OR IGNORE INTO storage_archives VALUES (?,?,?,?, 'VERIFIED')", (campaign_id, str(archive), manifest["checksum"], json.dumps(manifest, sort_keys=True)))
    run_bounded_maintenance_write(engine, policy, publish, operation_name="storage_archive_manifest")
    return manifest


def verify_manifest(manifest):
    path = Path(manifest["archive_path"])
    if manifest.get("state") != "VERIFIED" or _file_hash(path) != manifest["checksum"]:
        raise ValueError("ARCHIVE_CHECKSUM_OR_STATE_INVALID")
    with readonly(path) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or conn.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("ARCHIVE_INTEGRITY_FAILED")
        if _schema_hash(conn) != manifest["schema_hash"]:
            raise ValueError("ARCHIVE_SCHEMA_MISMATCH")
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {_q(t)}").fetchone()[0] for t in sorted(tables(conn))}
        if counts != manifest["row_counts"]:
            raise ValueError("ARCHIVE_COUNTS_MISMATCH")
        identity = dict(conn.execute("SELECT * FROM burnin_campaigns WHERE campaign_id=?", (manifest["campaign_id"],)).fetchone())
        if identity != manifest["identity"] or campaign_eligibility(conn, manifest["campaign_id"]):
            raise ValueError("ARCHIVE_DOMAIN_OR_IDENTITY_MISMATCH")
    return path


def remove_archived_batch(engine, policy, manifest):
    archive_path = verify_manifest(manifest)
    cid = manifest["campaign_id"]
    with readonly(archive_path) as archived:
        token = secrets.token_hex(24)
        def operation(conn):
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            raw = conn.connection.driver_connection
            reason = campaign_eligibility(raw, cid)
            if reason:
                raise ValueError(reason)
            durable = conn.exec_driver_sql("SELECT checksum FROM storage_archives WHERE campaign_id=?", (cid,)).first()
            if not durable or durable[0] != manifest["checksum"]:
                raise ValueError("DURABLE_ARCHIVE_VERIFICATION_MISSING")
            campaign = conn.exec_driver_sql("SELECT * FROM burnin_campaigns WHERE campaign_id=?", (cid,)).mappings().one()
            if dict(campaign) != manifest["identity"]:
                raise ValueError("SOURCE_CAMPAIGN_IDENTITY_CHANGED_AFTER_ARCHIVE")
            raw.create_function("storage_archive_authorize", 1, lambda value: int(value == token))
            deadline = time.monotonic() + policy.batch_seconds
            removed = 0
            try:
                cycles = conn.exec_driver_sql("SELECT DISTINCT cycle_id FROM burnin_universe_selection_links WHERE campaign_id=? ORDER BY cycle_id LIMIT ?", (cid, policy.batch_rows)).scalars().all()
                for cycle in cycles:
                    if time.monotonic() >= deadline:
                        break
                    count = sum(conn.exec_driver_sql(f"SELECT COUNT(*) FROM {table} WHERE cycle_id=?", (cycle,)).scalar_one() for table in UNIVERSE_TABLES)
                    if count + removed > policy.batch_rows:
                        break
                    source_rows, archived_rows = {}, {}
                    for table in UNIVERSE_TABLES:
                        source_rows[table] = [tuple(r) for r in raw.execute(f"SELECT * FROM {table} WHERE cycle_id=? ORDER BY rowid", (cycle,))]
                        archived_rows[table] = [tuple(r) for r in archived.execute(f"SELECT * FROM {table} WHERE cycle_id=? ORDER BY rowid", (cycle,))]
                    if source_rows != archived_rows or not source_rows["universe_selection_cycles"]:
                        raise ValueError("ARCHIVE_REPLAY_ROWS_MISMATCH")
                    evidence_hash = raw.execute("SELECT evidence_hash FROM universe_selection_cycles WHERE cycle_id=?", (cycle,)).fetchone()[0]
                    conn.exec_driver_sql("INSERT INTO storage_archive_authorizations VALUES (?,?,?,?)", (token, cid, cycle, evidence_hash))
                    for table in UNIVERSE_TABLES:
                        removed += conn.exec_driver_sql(f"DELETE FROM {table} WHERE cycle_id=?", (cycle,)).rowcount
                    conn.exec_driver_sql("DELETE FROM storage_archive_authorizations WHERE cycle_id=?", (cycle,))
                remaining = conn.exec_driver_sql("SELECT 1 FROM burnin_universe_selection_links WHERE campaign_id=? LIMIT 1", (cid,)).first()
                conn.exec_driver_sql("UPDATE storage_archives SET state=? WHERE campaign_id=?", ("REMOVING" if remaining else "ARCHIVED", cid))
            finally:
                raw.create_function("storage_archive_authorize", 1, lambda value: 0)
            return {"rows_removed": removed, "campaign_id": cid, "archive_path": str(archive_path), "state": "REMOVING" if remaining else "ARCHIVED"}
        return run_bounded_maintenance_write(engine, policy, operation, operation_name="storage_archive_removal")


def archive_location(conn, campaign_id):
    """Read-only locator. Never returns an unverified/partial archive as evidence."""
    if "storage_archives" not in tables(conn):
        return None
    row = conn.execute("SELECT manifest_json,state FROM storage_archives WHERE campaign_id=?", (campaign_id,)).fetchone()
    if not row:
        return None
    manifest = json.loads(row[0])
    try:
        path = verify_manifest(manifest)
    except (OSError, ValueError, sqlite3.Error) as exc:
        return {"status": "ARCHIVED_EVIDENCE_UNAVAILABLE", "reason": str(exc), "state": row[1]}
    return {"status": "ARCHIVED_EVIDENCE_REQUIRES_REPLAY", "archive_path": str(path), "state": row[1], "checksum": manifest["checksum"]}


class StorageController:
    def __init__(self, engine, policy):
        self.engine, self.policy = engine, policy
        self.next_check = 0.0
        self.cleaning = False
        self.stats_cleaning = False
        self.last_report = None

    def check(self, *, force=False):
        if not force and time.monotonic() < self.next_check:
            return self.last_report
        self.next_check = time.monotonic() + self.policy.check_interval_sec
        path = database_path(self.engine)
        before = inventory(path, self.policy, detailed=False)
        live = before["allocated_live_bytes"]
        disk_pressure = before["status"] != "PASS"
        if before["physical_bytes"] >= self.policy.high_bytes and live > self.policy.low_bytes:
            self.cleaning = True
        if live <= self.policy.low_bytes:
            self.cleaning = False
        if (self.policy.stats_only_enabled
                and before["physical_bytes"] >= self.policy.stats_only_start_bytes
                and live > self.policy.stats_only_target_bytes):
            self.stats_cleaning = True
        if live <= self.policy.stats_only_target_bytes:
            self.stats_cleaning = False
        report = {"before": before, "rows_removed": 0, "batches": 0, "archives_verified": 0,
                  "cycles_compacted": 0, "checkpoint": None, "blockers": [],
                  "blocked": disk_pressure, "status": "BELOW_HIGH_WATERMARK"}
        if before["wal_bytes"] >= self.policy.high_bytes - self.policy.low_bytes or disk_pressure:
            report["checkpoint"] = checkpoint(self.engine)
            if not report["checkpoint"]["busy"]:
                report["checkpoint"] = checkpoint(self.engine, truncate=True)
        if self.policy.enabled and (self.cleaning or self.stats_cleaning or disk_pressure):
            try:
                if self.policy.stats_only_enabled and (self.stats_cleaning or disk_pressure):
                    result = compact_stats_only_universe_batch(self.engine, self.policy)
                else:
                    result = prune_telemetry(self.engine, self.policy)
                result_blockers = result.pop("blockers", [])
                report.update(result)
                report["blockers"].extend(result_blockers)
                report["batches"] = int(result["rows_removed"] > 0)
                if not result["rows_removed"]:
                    telemetry = prune_telemetry(self.engine, self.policy)
                    report["rows_removed"] = telemetry["rows_removed"]
                    report["reason"] = telemetry["reason"] if telemetry["rows_removed"] else report.get("reason")
                    report["batches"] = int(telemetry["rows_removed"] > 0)
                if not report["rows_removed"] and self.cleaning:
                    # Archive at most one demonstrably terminal campaign per
                    # interval. Failed/incomplete/qualification campaigns stay.
                    with readonly(path) as conn:
                        tab = tables(conn)
                        candidates = (conn.execute("SELECT campaign_id FROM burnin_campaigns WHERE campaign_status='COMPLETED' ORDER BY completed_at LIMIT 10").fetchall() if "burnin_campaigns" in tab else [])
                        eligible = next((r[0] for r in candidates if campaign_eligibility(conn, r[0], min_age_sec=self.policy.min_age_sec) is None and conn.execute("SELECT 1 FROM burnin_universe_selection_links WHERE campaign_id=? LIMIT 1", (r[0],)).fetchone()), None)
                    if eligible and self.policy.archive_dir:
                        manifest = verified_archive(self.engine, self.policy, eligible)
                        report["archives_verified"] = 1
                        result = remove_archived_batch(self.engine, self.policy, manifest)
                        report.update(result)
                        report["batches"] = int(result["rows_removed"] > 0)
                    else:
                        report["blockers"].append("PROTECTED_EVIDENCE_OR_ARCHIVE_DESTINATION_MISSING")
                report["checkpoint"] = checkpoint(self.engine)
                if disk_pressure and not report["checkpoint"]["busy"]:
                    report["checkpoint"] = checkpoint(self.engine, truncate=True)
            except SQLiteBusyExhausted:
                report["blockers"].append("STORAGE_WRITER_BUSY")
            except (sqlite3.Error, OperationalError) as exc:
                if not is_sqlite_full_error(exc):
                    raise
                report["blockers"].append("STORAGE_ARCHIVE_OR_MAINTENANCE_DISK_FULL")
            except (ValueError, TimeoutError, OSError) as exc:
                report["blockers"].append(str(exc))
        after = inventory(path, self.policy, detailed=False)
        report["after"] = after
        if after["allocated_live_bytes"] <= self.policy.low_bytes:
            self.cleaning = False
            report["status"] = "REUSABLE_CAPACITY" if after["physical_bytes"] >= self.policy.high_bytes else "TARGET_REACHED"
        elif self.policy.stats_only_enabled and after["allocated_live_bytes"] <= self.policy.stats_only_target_bytes:
            self.stats_cleaning = False
            report["status"] = "STATS_ONLY_REUSABLE_CAPACITY" if after["physical_bytes"] >= self.policy.stats_only_start_bytes else "STATS_ONLY_TARGET_REACHED"
        elif self.cleaning:
            report["status"] = "CLEANUP_IN_PROGRESS" if report["rows_removed"] else "TARGET_NOT_REACHABLE_PROTECTED_DATA"
        elif self.stats_cleaning:
            report["status"] = ("STATS_ONLY_CLEANUP_IN_PROGRESS" if report["rows_removed"]
                                else "STATS_ONLY_TARGET_NOT_REACHABLE_PROTECTED_DATA")
        report["blocked"] = after["status"] != "PASS"
        if after["status"] != "PASS":
            report["blocked"] = True
            report["status"] = "STORAGE_DISK_PRESSURE"
        elif report["status"] == "TARGET_NOT_REACHABLE_PROTECTED_DATA":
            report["blocked"] = True
        if "STORAGE_ARCHIVE_OR_MAINTENANCE_DISK_FULL" in report["blockers"]:
            report["blocked"] = True
            report["status"] = "STORAGE_DISK_FULL_RECOVERY_REQUIRED"
        cp = report["checkpoint"]
        if cp and cp["busy"] and after["allocated_live_bytes"] + max(0, cp["wal_frames"] - cp["checkpointed_frames"]) * after["page_size"] >= self.policy.high_bytes:
            report["status"] = "WAL_CHECKPOINT_BLOCKED"
            report["blocked"] = True
            report["blockers"].append("LONG_READER_OR_WRITER_RETAINS_WAL_BACKLOG")
        report["next_action"] = ("BLOCK_NEW_EVIDENCE_PRODUCERS; PRESERVE_RECONCILIATION_AND_POSITION_MANAGEMENT; PROVIDE_ARCHIVE_CAPACITY_OR_OFFLINE_COMPACTION" if report["blocked"] else "NEXT_BOUNDED_CHECK")
        self.last_report = report  # one bounded diagnostic; no append table
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inventory", "replay-location", "maintain"))
    parser.add_argument("database")
    parser.add_argument("--campaign-id")
    args = parser.parse_args()
    from alphaforge.config import load_config_from_env
    policy = StoragePolicy.from_config(load_config_from_env().runtime)
    if args.command == "inventory":
        result = inventory(args.database, policy)
    elif args.command == "replay-location":
        if not args.campaign_id:
            parser.error("--campaign-id required")
        with readonly(Path(args.database).resolve()) as conn:
            result = archive_location(conn, args.campaign_id)
    else:
        from alphaforge.persistence import init_db
        engine = init_db(f"sqlite+pysqlite:///{Path(args.database).resolve()}")
        try:
            result = StorageController(engine, policy).check(force=True)
        finally:
            engine.dispose()
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
