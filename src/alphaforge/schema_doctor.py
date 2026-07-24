"""Central, conservative SQLite schema inspection and additive migration.

The doctor intentionally owns only the safety-critical runtime exposure shape.
The broader persistence bootstrap remains in :mod:`alphaforge.persistence`.
Unknown legacy shapes are reported and blocked rather than interpreted as an
empty account.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PureWindowsPath
import sqlite3
from typing import Any


SCHEMA_VERSION = "2026_07_24_runtime_exposure_v1"
MIGRATION_NAME = "canonical runtime exposure status adapters"
MIGRATION_SQL = "positions.status<-state|closed_at|exit_time;orders.status<-order_status"
MIGRATION_CHECKSUM = hashlib.sha256(MIGRATION_SQL.encode()).hexdigest()

RUNTIME_SCHEMA: dict[str, dict[str, str]] = {
    "positions": {"id": "INTEGER", "symbol": "TEXT", "qty": "REAL", "status": "TEXT"},
    "orders": {"id": "INTEGER", "order_id": "TEXT", "symbol": "TEXT", "status": "TEXT"},
}


@dataclass
class SchemaReport:
    database: str
    tables: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    missing_tables: list[str] = field(default_factory=list)
    missing_columns: list[dict[str, str]] = field(default_factory=list)
    type_mismatches: list[dict[str, str]] = field(default_factory=list)
    unsupported_legacy_shapes: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MigrationReport:
    database: str
    schema_status: str
    schema_version: str
    applied_migrations: list[str] = field(default_factory=list)
    missing_tables: list[str] = field(default_factory=list)
    missing_columns: list[dict[str, str]] = field(default_factory=list)
    type_mismatches: list[dict[str, str]] = field(default_factory=list)
    unsupported_legacy_shapes: list[dict[str, Any]] = field(default_factory=list)
    unsafe_changes_required: list[str] = field(default_factory=list)
    affected_features: list[str] = field(default_factory=list)
    next_action: str = "none"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_database_path(value: str | Path) -> Path:
    raw = str(value)
    if raw.startswith("sqlite+pysqlite:///"):
        raw = raw.removeprefix("sqlite+pysqlite:///")
    elif raw.startswith("sqlite:///"):
        raw = raw.removeprefix("sqlite:///")
    if PureWindowsPath(raw).is_absolute() and not Path(raw).is_absolute():
        # Preserve the drive-qualified spelling on Windows; on POSIX this path
        # is inspectable only as a spelling and must not be cwd-prefixed.
        return Path(PureWindowsPath(raw).as_posix())
    return Path(raw).expanduser().resolve()


def _connection(target: Any) -> tuple[Any, bool, str]:
    if isinstance(target, (str, Path)):
        path = normalize_database_path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(path), True, str(path)
    if isinstance(target, sqlite3.Connection):
        row = target.execute("PRAGMA database_list").fetchone()
        return target, False, str(row[2] if row and row[2] else ":memory:")
    # SQLAlchemy Connection (the public functions deliberately accept either).
    raw = getattr(getattr(target, "connection", None), "driver_connection", None)
    if isinstance(raw, sqlite3.Connection):
        row = raw.execute("PRAGMA database_list").fetchone()
        return raw, False, str(row[2] if row and row[2] else ":memory:")
    raise TypeError("schema doctor supports SQLite paths or connections")


def inspect_database_schema(target: Any) -> SchemaReport:
    conn, owned, database = _connection(target)
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
                elif columns[column]["type"] and expected_type not in columns[column]["type"]:
                    report.type_mismatches.append({"table": table, "column": column, "expected": expected_type, "actual": columns[column]["type"]})
        pcols = report.tables.get("positions", {})
        if pcols and "status" not in pcols and not ({"state", "closed_at", "exit_time"} & pcols.keys()):
            report.unsupported_legacy_shapes.append({"table": "positions", "reason": "no status/state/closed timestamp evidence"})
        ocols = report.tables.get("orders", {})
        if ocols and "status" not in ocols and "order_status" not in ocols:
            report.unsupported_legacy_shapes.append({"table": "orders", "reason": "no status/order_status evidence"})
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


def ensure_database_schema(target: Any) -> MigrationReport:
    before = inspect_database_schema(target)
    conn, owned, database = _connection(target)
    applied: list[str] = []
    try:
        if before.unsupported_legacy_shapes or before.type_mismatches:
            return validate_required_schema(target)
        conn.execute("BEGIN")
        _migration_table(conn)
        # Missing safety tables are additive and contain no invented exposure.
        conn.execute("CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY AUTOINCREMENT,symbol TEXT,qty REAL,status TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id TEXT,symbol TEXT,status TEXT)")
        pcols = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
        for column, ddl in (("symbol", "TEXT"), ("qty", "REAL")):
            if column not in pcols:
                conn.execute(f"ALTER TABLE positions ADD COLUMN {column} {ddl}")
        if "status" not in pcols:
            conn.execute("ALTER TABLE positions ADD COLUMN status TEXT")
            if "state" in pcols:
                conn.execute("UPDATE positions SET status=state WHERE status IS NULL")
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
            conn.execute("UPDATE orders SET status=order_status WHERE status IS NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_positions_status ON positions(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status)")
        existed = conn.execute("SELECT 1 FROM schema_migrations WHERE version=? AND success=1", (SCHEMA_VERSION,)).fetchone()
        if not existed:
            conn.execute("INSERT INTO schema_migrations(version,name,applied_at,checksum,success,details_json,notes) VALUES(?,?,?,?,1,?,?)",
                         (SCHEMA_VERSION, MIGRATION_NAME, datetime.now(timezone.utc).isoformat(), MIGRATION_CHECKSUM, json.dumps({"additive": True}), MIGRATION_NAME))
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
    del scope  # reserved for future registries; runtime is safety-critical now.
    report = inspect_database_schema(target)
    blocked = bool(report.missing_tables or report.missing_columns or report.type_mismatches or report.unsupported_legacy_shapes)
    return MigrationReport(
        database=report.database, schema_status="BLOCKED" if blocked else "VALID", schema_version=SCHEMA_VERSION,
        missing_tables=report.missing_tables, missing_columns=report.missing_columns,
        type_mismatches=report.type_mismatches, unsupported_legacy_shapes=report.unsupported_legacy_shapes,
        unsafe_changes_required=["manual schema/data mapping required"] if report.unsupported_legacy_shapes or report.type_mismatches else [],
        affected_features=["runtime_recovery", "reconciliation", "burnin_preflight"] if blocked else [],
        next_action="manual migration required; runtime must remain blocked" if blocked else "none",
    )


def exposure_count(conn: Any, table: str) -> int:
    """Count exposure only after validating a known schema; never fail open."""
    validation = validate_required_schema(conn)
    if validation.schema_status != "VALID":
        raise RuntimeError("SCHEMA_UNSUPPORTED:" + json.dumps(validation.as_dict(), sort_keys=True))
    if table == "positions":
        sql = "SELECT COUNT(*) FROM positions WHERE UPPER(COALESCE(status,'')) IN ('OPEN','POSITION_OPENED','ACTIVE')"
    elif table == "orders":
        sql = "SELECT COUNT(*) FROM orders WHERE UPPER(COALESCE(status,'')) IN ('PENDING','OPEN','ORDER_PLACED','ENTRY_SUBMITTED')"
    else:
        raise ValueError(f"unsupported exposure table:{table}")
    row = conn.execute(sql).fetchone()
    return int(row[0] or 0)
