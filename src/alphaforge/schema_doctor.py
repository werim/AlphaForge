"""Central, conservative SQLite schema inspection and additive migration."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PureWindowsPath
import sqlite3
from typing import Any


PREVIOUS_SCHEMA_VERSION = "2026_07_24_runtime_exposure_v1"
PREVIOUS_MIGRATION_CHECKSUM = hashlib.sha256("positions.status<-state|closed_at|exit_time;orders.status<-order_status".encode()).hexdigest()
SCHEMA_VERSION = "2026_07_24_runtime_exposure_v2"
MIGRATION_NAME = "canonical runtime exposure status adapters"
MIGRATION_SQL = "positions.status<-state|closed_at|exit_time;orders.status<-order_status;semantic-validation-v2"
MIGRATION_CHECKSUM = hashlib.sha256(MIGRATION_SQL.encode()).hexdigest()
KNOWN_MIGRATION_CHECKSUMS = {
    PREVIOUS_SCHEMA_VERSION: PREVIOUS_MIGRATION_CHECKSUM,
    SCHEMA_VERSION: MIGRATION_CHECKSUM,
}

POSITION_ACTIVE = frozenset({"OPEN", "POSITION_OPENED", "ACTIVE"})
POSITION_TERMINAL = frozenset({"CLOSED", "POSITION_CLOSED", "EXITED", "CANCELLED"})
ORDER_ACTIVE = frozenset({"PENDING", "OPEN", "ORDER_PLACED", "ENTRY_SUBMITTED"})
ORDER_TERMINAL = frozenset({"FILLED", "CANCELLED", "REJECTED", "EXPIRED", "CLOSED"})
RECOGNIZED_STATES = {
    "positions": POSITION_ACTIVE | POSITION_TERMINAL,
    "orders": ORDER_ACTIVE | ORDER_TERMINAL,
}
ACTIVE_STATES = {"positions": POSITION_ACTIVE, "orders": ORDER_ACTIVE}

RUNTIME_SCHEMA: dict[str, dict[str, str]] = {
    "positions": {"id": "INTEGER", "symbol": "TEXT", "qty": "REAL", "status": "TEXT"},
    "orders": {"id": "INTEGER", "order_id": "TEXT", "symbol": "TEXT", "status": "TEXT"},
}


@dataclass
class SchemaReport:
    database: str
    database_exists: bool = True
    tables: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    missing_tables: list[str] = field(default_factory=list)
    missing_columns: list[dict[str, str]] = field(default_factory=list)
    type_mismatches: list[dict[str, str]] = field(default_factory=list)
    unsupported_legacy_shapes: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    affected_rows: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MigrationReport:
    database: str
    schema_status: str
    schema_version: str
    reason: str | None = None
    reasons: list[str] = field(default_factory=list)
    applied_migrations: list[str] = field(default_factory=list)
    missing_tables: list[str] = field(default_factory=list)
    missing_columns: list[dict[str, str]] = field(default_factory=list)
    type_mismatches: list[dict[str, str]] = field(default_factory=list)
    unsupported_legacy_shapes: list[dict[str, Any]] = field(default_factory=list)
    affected_rows: list[dict[str, Any]] = field(default_factory=list)
    unsafe_changes_required: list[str] = field(default_factory=list)
    affected_features: list[str] = field(default_factory=list)
    next_action: str = "none"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ExposureStateError(RuntimeError):
    def __init__(self, report: MigrationReport):
        self.report = report
        super().__init__((report.reason or "SCHEMA_UNSUPPORTED") + ":" + json.dumps(report.as_dict(), sort_keys=True))


def normalize_database_path(value: str | Path) -> Path:
    raw = str(value)
    if raw.startswith("sqlite+pysqlite:///"):
        raw = raw.removeprefix("sqlite+pysqlite:///")
    elif raw.startswith("sqlite:///"):
        raw = raw.removeprefix("sqlite:///")
    if PureWindowsPath(raw).is_absolute() and not Path(raw).is_absolute():
        return Path(PureWindowsPath(raw).as_posix())
    return Path(raw).expanduser().resolve()


def _connection(target: Any, *, create: bool = False) -> tuple[Any | None, bool, str]:
    if isinstance(target, (str, Path)):
        path = normalize_database_path(target)
        if not path.is_file() and not create:
            return None, False, str(path)
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(path), True, str(path)
    if isinstance(target, sqlite3.Connection):
        row = target.execute("PRAGMA database_list").fetchone()
        return target, False, str(row[2] if row and row[2] else ":memory:")
    raw = getattr(getattr(target, "connection", None), "driver_connection", None)
    if isinstance(raw, sqlite3.Connection):
        row = raw.execute("PRAGMA database_list").fetchone()
        return raw, False, str(row[2] if row and row[2] else ":memory:")
    raise TypeError("schema doctor supports SQLite paths or connections")


def _unknown_state_rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    allowed = RECOGNIZED_STATES[table]
    placeholders = ",".join("?" for _ in allowed)
    rows = conn.execute(
        f"""SELECT id, status FROM {table}
            WHERE status IS NULL OR TRIM(CAST(status AS TEXT)) = ''
               OR UPPER(TRIM(CAST(status AS TEXT))) NOT IN ({placeholders})
            ORDER BY id LIMIT 100""",
        tuple(sorted(allowed)),
    ).fetchall()
    return [{"table": table, "id": row[0], "status": row[1]} for row in rows]


def inspect_database_schema(target: Any) -> SchemaReport:
    conn, owned, database = _connection(target)
    if conn is None:
        return SchemaReport(database=database, database_exists=False, missing_tables=sorted(RUNTIME_SCHEMA), reasons=["DATABASE_IDENTITY_UNVERIFIED", "EXPOSURE_TABLES_MISSING"])
    try:
        report = SchemaReport(database=database)
        names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in sorted(names):
            report.tables[table] = {
                str(row[1]): {"type": str(row[2] or "").upper(), "nullable": not bool(row[3]), "default": row[4], "primary_key": bool(row[5])}
                for row in conn.execute(f'PRAGMA table_info("{table}")')
            }
        for table, required in RUNTIME_SCHEMA.items():
            if table not in names:
                report.missing_tables.append(table)
                continue
            columns = report.tables[table]
            for column, expected_type in required.items():
                if column not in columns:
                    report.missing_columns.append({"table": table, "column": column})
                    if column == "id":
                        report.reasons.append("IDENTIFIER_COLUMN_MISSING")
                elif columns[column]["type"] and expected_type not in columns[column]["type"]:
                    report.type_mismatches.append({"table": table, "column": column, "expected": expected_type, "actual": columns[column]["type"]})
        if report.missing_tables:
            report.reasons.append("EXPOSURE_TABLES_MISSING")
        pcols = report.tables.get("positions", {})
        if pcols and "status" not in pcols and not ({"state", "closed_at", "exit_time"} & pcols.keys()):
            report.unsupported_legacy_shapes.append({"table": "positions", "reason": "no status/state/closed timestamp evidence"})
        ocols = report.tables.get("orders", {})
        if ocols and "status" not in ocols and "order_status" not in ocols:
            report.unsupported_legacy_shapes.append({"table": "orders", "reason": "no status/order_status evidence"})
        for table in RUNTIME_SCHEMA:
            if {"id", "status"}.issubset(report.tables.get(table, {})):
                report.affected_rows.extend(_unknown_state_rows(conn, table))
        if report.affected_rows:
            report.reasons.append("UNKNOWN_EXPOSURE_STATE")
        return report
    finally:
        if owned:
            conn.close()


def _migration_table(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations(
        version TEXT PRIMARY KEY, name TEXT, applied_at TEXT NOT NULL, checksum TEXT,
        success INTEGER NOT NULL DEFAULT 1, details_json TEXT, notes TEXT)""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(schema_migrations)")}
    for name, ddl in (("name", "TEXT"), ("checksum", "TEXT"), ("success", "INTEGER NOT NULL DEFAULT 1"), ("details_json", "TEXT"), ("notes", "TEXT")):
        if name not in columns:
            conn.execute(f"ALTER TABLE schema_migrations ADD COLUMN {name} {ddl}")


def _migration_integrity(conn: sqlite3.Connection) -> str | None:
    if "schema_migrations" not in {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        return None
    columns = {row[1] for row in conn.execute("PRAGMA table_info(schema_migrations)")}
    selected = ["checksum" if "checksum" in columns else "NULL", "success" if "success" in columns else "NULL"]
    current_applied = False
    for version, expected_checksum in KNOWN_MIGRATION_CHECKSUMS.items():
        row = conn.execute(f"SELECT {','.join(selected)} FROM schema_migrations WHERE version=?", (version,)).fetchone()
        if not row:
            continue
        if row[1] is None or str(row[1]).strip().lower() not in {"1", "true"}:
            return "MIGRATION_PREVIOUSLY_FAILED"
        if row[0] != expected_checksum:
            return "MIGRATION_CHECKSUM_MISMATCH"
        current_applied = current_applied or version == SCHEMA_VERSION
    return "OK" if current_applied else None


def _blocked(report: SchemaReport, reasons: list[str] | None = None) -> MigrationReport:
    all_reasons = list(dict.fromkeys((reasons or []) + report.reasons))
    if report.unsupported_legacy_shapes:
        all_reasons.append("SCHEMA_UNSUPPORTED")
    if report.type_mismatches:
        all_reasons.append("TYPE_MISMATCH")
    all_reasons = list(dict.fromkeys(all_reasons))
    return MigrationReport(
        database=report.database, schema_status="BLOCKED", schema_version=SCHEMA_VERSION,
        reason=all_reasons[0] if all_reasons else "SCHEMA_UNSUPPORTED", reasons=all_reasons,
        missing_tables=report.missing_tables, missing_columns=report.missing_columns,
        type_mismatches=report.type_mismatches, unsupported_legacy_shapes=report.unsupported_legacy_shapes,
        affected_rows=report.affected_rows, unsafe_changes_required=["manual schema/data migration required"],
        affected_features=["runtime_recovery", "reconciliation", "burnin_preflight"],
        next_action="verify database identity and perform a manual migration",
    )


def ensure_database_schema(target: Any, *, allow_fresh_bootstrap: bool = False) -> MigrationReport:
    before = inspect_database_schema(target)
    if not before.database_exists and not allow_fresh_bootstrap:
        return _blocked(before)
    existing_user_tables = set(before.tables) - {"sqlite_sequence", "schema_migrations"}
    if before.missing_tables and existing_user_tables:
        return _blocked(before, ["DATABASE_IDENTITY_UNVERIFIED", "EXPOSURE_TABLES_MISSING"])
    if before.missing_tables and not allow_fresh_bootstrap:
        return _blocked(before, ["EXPOSURE_TABLES_MISSING"])
    if before.unsupported_legacy_shapes or before.type_mismatches or "IDENTIFIER_COLUMN_MISSING" in before.reasons or "UNKNOWN_EXPOSURE_STATE" in before.reasons:
        return _blocked(before)

    conn, owned, database = _connection(target, create=allow_fresh_bootstrap)
    if conn is None:
        return _blocked(before)
    applied: list[str] = []
    details: dict[str, Any] = {"additive": True, "row_counts_before": {}, "row_counts_after": {}, "affected_rows": []}
    try:
        integrity = _migration_integrity(conn)
        if integrity in {"MIGRATION_CHECKSUM_MISMATCH", "MIGRATION_PREVIOUSLY_FAILED"}:
            return _blocked(before, [integrity])
        conn.execute("BEGIN")
        _migration_table(conn)
        if allow_fresh_bootstrap:
            conn.execute("CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY AUTOINCREMENT,symbol TEXT,qty REAL,status TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id TEXT,symbol TEXT,status TEXT)")
        for table in RUNTIME_SCHEMA:
            details["row_counts_before"][table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        pcols = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
        for column, ddl in (("symbol", "TEXT"), ("qty", "REAL")):
            if column not in pcols:
                conn.execute(f"ALTER TABLE positions ADD COLUMN {column} {ddl}")
        if "status" not in pcols:
            conn.execute("ALTER TABLE positions ADD COLUMN status TEXT")
            if "state" in pcols:
                conn.execute("UPDATE positions SET status=UPPER(TRIM(state))")
            elif "closed_at" in pcols:
                conn.execute("UPDATE positions SET status=CASE WHEN closed_at IS NULL THEN 'OPEN' ELSE 'CLOSED' END")
            elif "exit_time" in pcols:
                conn.execute("UPDATE positions SET status=CASE WHEN exit_time IS NULL THEN 'OPEN' ELSE 'CLOSED' END")
        ocols = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
        for column, ddl in (("order_id", "TEXT"), ("symbol", "TEXT")):
            if column not in ocols:
                conn.execute(f"ALTER TABLE orders ADD COLUMN {column} {ddl}")
        if "status" not in ocols:
            conn.execute("ALTER TABLE orders ADD COLUMN status TEXT")
            conn.execute("UPDATE orders SET status=UPPER(TRIM(order_status))")
        for table in RUNTIME_SCHEMA:
            details["row_counts_after"][table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            details["affected_rows"].extend(_unknown_state_rows(conn, table))
        if details["row_counts_before"] != details["row_counts_after"] or details["affected_rows"]:
            conn.rollback()
            after = inspect_database_schema(target)
            after.affected_rows = list(details["affected_rows"])
            return _blocked(after, ["UNKNOWN_EXPOSURE_STATE"] if details["affected_rows"] else ["MIGRATION_ROW_COUNT_MISMATCH"])
        conn.execute("CREATE INDEX IF NOT EXISTS ix_positions_status ON positions(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status)")
        if integrity != "OK":
            conn.execute("INSERT INTO schema_migrations(version,name,applied_at,checksum,success,details_json,notes) VALUES(?,?,?,?,1,?,?)",
                         (SCHEMA_VERSION, MIGRATION_NAME, datetime.now(timezone.utc).isoformat(), MIGRATION_CHECKSUM, json.dumps(details, sort_keys=True), MIGRATION_NAME))
            applied.append(SCHEMA_VERSION)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if owned:
            conn.close()
    result = validate_required_schema(target)
    result.applied_migrations = applied
    if applied and result.schema_status == "VALID":
        result.schema_status = "MIGRATED"
    return result


def validate_required_schema(target: Any, scope: str = "runtime") -> MigrationReport:
    del scope
    report = inspect_database_schema(target)
    if report.database_exists:
        conn, owned, _ = _connection(target)
        try:
            integrity = _migration_integrity(conn)
        finally:
            if owned:
                conn.close()
        if integrity in {"MIGRATION_CHECKSUM_MISMATCH", "MIGRATION_PREVIOUSLY_FAILED"}:
            return _blocked(report, [integrity])
    blocked = bool(report.missing_tables or report.missing_columns or report.type_mismatches or report.unsupported_legacy_shapes or report.reasons)
    if blocked:
        return _blocked(report)
    return MigrationReport(database=report.database, schema_status="VALID", schema_version=SCHEMA_VERSION)


def exposure_count(conn: Any, table: str) -> int:
    validation = validate_required_schema(conn)
    if validation.schema_status != "VALID":
        raise ExposureStateError(validation)
    if table not in ACTIVE_STATES:
        raise ValueError(f"unsupported exposure table:{table}")
    states = ACTIVE_STATES[table]
    placeholders = ",".join("?" for _ in states)
    raw, _, _ = _connection(conn)
    row = raw.execute(
        f"SELECT COUNT(*) FROM {table} WHERE UPPER(TRIM(CAST(status AS TEXT))) IN ({placeholders})",
        tuple(sorted(states)),
    ).fetchone()
    return int(row[0] or 0)
