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
V2_SCHEMA_VERSION = "2026_07_24_runtime_exposure_v2"
V2_MIGRATION_CHECKSUM = hashlib.sha256("positions.status<-state|closed_at|exit_time;orders.status<-order_status;semantic-validation-v2".encode()).hexdigest()
V3_SCHEMA_VERSION = "2026_07_24_runtime_exposure_v3"
V3_MIGRATION_CHECKSUM = hashlib.sha256("sqlite-affinity;trusted-alembic-0005;dedicated-runtime-exposure-tables".encode()).hexdigest()
SCHEMA_VERSION = "2026_07_24_runtime_exposure_v4"
MIGRATION_NAME = "canonical runtime exposure status adapters"
MIGRATION_SQL = "central-runtime-exposure-readers;runtime-orders-created-at"
MIGRATION_CHECKSUM = hashlib.sha256(MIGRATION_SQL.encode()).hexdigest()
KNOWN_MIGRATION_CHECKSUMS = {
    PREVIOUS_SCHEMA_VERSION: PREVIOUS_MIGRATION_CHECKSUM,
    V2_SCHEMA_VERSION: V2_MIGRATION_CHECKSUM,
    V3_SCHEMA_VERSION: V3_MIGRATION_CHECKSUM,
    SCHEMA_VERSION: MIGRATION_CHECKSUM,
}

KNOWN_ALEMBIC_HEADS = frozenset({"0005_core_identifier_normalization", "0006_reject_label_identity_timeframe", "0007_repair_runtime_lifecycle_schema", "0008_database_doctor_lifecycle_contract"})

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
    "orders": {"id": "INTEGER", "order_id": "TEXT", "symbol": "TEXT", "status": "TEXT", "created_at": "TEXT"},
}

# Tables in this map are optional, but once present their runtime-consumed
# columns are mandatory.  This lets the central doctor verify additive feature
# schemas without requiring those features in every supported database family.
CONDITIONAL_RUNTIME_SCHEMA: dict[str, dict[str, str]] = {
    "burnin_pending_reject_labels": {
        "timeframe": "TEXT",
        "horizon_bars": "INTEGER",
        "claim_token": "TEXT",
        "claimed_at": "TEXT",
    },
}

LIFECYCLE_RUNTIME_SCHEMA: dict[str, str] = {
    "event_id": "TEXT", "signal_id": "TEXT", "trade_id": "TEXT",
    "order_id": "TEXT", "symbol": "TEXT", "mode": "TEXT",
    "lifecycle_state": "TEXT", "state": "TEXT", "event_type": "TEXT",
    "payload": "TEXT", "decision": "TEXT", "reject_reason": "TEXT",
    "score": "REAL", "rr": "REAL", "effective_rr": "REAL",
    "expectancy_bucket": "TEXT", "execution_ctx": "TEXT",
    "execution_ctx_missing": "INTEGER", "event_ts": "TEXT",
    "created_at": "TEXT", "lifecycle_seq": "INTEGER", "cancel_reason": "TEXT",
    "lifecycle_id": "TEXT", "failure_reason": "TEXT",
    "reconciliation_reason": "TEXT", "incident_payload": "TEXT",
}
LIFECYCLE_CONFLICT_TARGETS = (
    ("event_id",),
    ("signal_id", "event_ts", "lifecycle_state"),
)


@dataclass
class SchemaReport:
    database: str
    database_exists: bool = True
    tables: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    missing_tables: list[str] = field(default_factory=list)
    missing_columns: list[dict[str, str]] = field(default_factory=list)
    type_mismatches: list[dict[str, str]] = field(default_factory=list)
    missing_unique_constraints: list[dict[str, Any]] = field(default_factory=list)
    unsupported_legacy_shapes: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    affected_rows: list[dict[str, Any]] = field(default_factory=list)
    schema_family: str = "UNKNOWN"
    alembic_revision: str | None = None
    exposure_tables: dict[str, str] = field(default_factory=dict)

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
    missing_unique_constraints: list[dict[str, Any]] = field(default_factory=list)
    unsupported_legacy_shapes: list[dict[str, Any]] = field(default_factory=list)
    affected_rows: list[dict[str, Any]] = field(default_factory=list)
    unsafe_changes_required: list[str] = field(default_factory=list)
    affected_features: list[str] = field(default_factory=list)
    next_action: str = "none"
    schema_family: str = "UNKNOWN"
    alembic_revision: str | None = None
    exposure_tables: dict[str, str] = field(default_factory=dict)

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
    logical_table = "positions" if table in {"positions", "runtime_positions"} else "orders"
    allowed = RECOGNIZED_STATES[logical_table]
    placeholders = ",".join("?" for _ in allowed)
    rows = conn.execute(
        f"""SELECT id, status FROM {table}
            WHERE status IS NULL OR TRIM(CAST(status AS TEXT)) = ''
               OR UPPER(TRIM(CAST(status AS TEXT))) NOT IN ({placeholders})
            ORDER BY id LIMIT 100""",
        tuple(sorted(allowed)),
    ).fetchall()
    return [{"table": table, "id": row[0], "status": row[1]} for row in rows]


def sqlite_type_affinity(declared_type: str) -> str:
    """Return SQLite's canonical affinity for a declared type."""
    value = str(declared_type or "").strip().upper()
    if "INT" in value:
        return "INTEGER"
    if any(token in value for token in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BLOB" in value or not value:
        return "BLOB"
    if any(token in value for token in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _affinity_compatible(expected: str, declared: str) -> bool:
    actual = sqlite_type_affinity(declared)
    expected_affinity = sqlite_type_affinity(expected)
    return actual == expected_affinity or (expected_affinity == "REAL" and actual == "NUMERIC")


def _schema_identity(conn: sqlite3.Connection, tables: dict[str, dict[str, dict[str, Any]]]) -> tuple[str, str | None, dict[str, str]]:
    revision = None
    if "alembic_version" in tables:
        row = conn.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
        revision = str(row[0]) if row else None
    pcols = set(tables.get("positions", {}))
    ocols = set(tables.get("orders", {}))
    if {"id", "qty", "status"}.issubset(pcols) and {"id", "status"}.issubset(ocols):
        return "RUNTIME_CANONICAL", revision, {"positions": "positions", "orders": "orders"}
    alembic_core = {"exchange_symbols", "order_intents", "positions", "orders"}.issubset(tables)
    alembic_shapes = {"id", "symbol_id", "side", "size"}.issubset(pcols) and {"id", "order_intent_id", "external_order_id", "status"}.issubset(ocols)
    if revision in KNOWN_ALEMBIC_HEADS and alembic_core and alembic_shapes:
        return "ALEMBIC_HEAD", revision, {"positions": "runtime_positions", "orders": "runtime_orders"}
    return "LEGACY_OR_UNKNOWN", revision, {"positions": "positions", "orders": "orders"}


def inspect_database_schema(target: Any) -> SchemaReport:
    conn, owned, database = _connection(target)
    if conn is None:
        return SchemaReport(database=database, database_exists=False, missing_tables=sorted(RUNTIME_SCHEMA), reasons=["DATABASE_IDENTITY_UNVERIFIED", "EXPOSURE_TABLES_MISSING"], schema_family="FRESH", exposure_tables={"positions": "positions", "orders": "orders"})
    try:
        report = SchemaReport(database=database)
        names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in sorted(names):
            report.tables[table] = {
                str(row[1]): {"type": str(row[2] or "").upper(), "nullable": not bool(row[3]), "default": row[4], "primary_key": bool(row[5])}
                for row in conn.execute(f'PRAGMA table_info("{table}")')
            }
        report.schema_family, report.alembic_revision, report.exposure_tables = _schema_identity(conn, report.tables)
        if report.alembic_revision and report.alembic_revision not in KNOWN_ALEMBIC_HEADS:
            report.reasons.append("DATABASE_IDENTITY_UNVERIFIED")
        for logical_table, required in RUNTIME_SCHEMA.items():
            table = report.exposure_tables[logical_table]
            if table not in names:
                report.missing_tables.append(table)
                continue
            columns = report.tables[table]
            for column, expected_type in required.items():
                if column not in columns:
                    report.missing_columns.append({"table": table, "column": column})
                    if column == "id":
                        report.reasons.append("IDENTIFIER_COLUMN_MISSING")
                elif not _affinity_compatible(expected_type, columns[column]["type"]):
                    report.type_mismatches.append({"table": table, "column": column, "expected": expected_type, "actual": columns[column]["type"]})
        for table, required in CONDITIONAL_RUNTIME_SCHEMA.items():
            if table not in names:
                continue
            columns = report.tables[table]
            for column, expected_type in required.items():
                if column not in columns:
                    report.missing_columns.append({"table": table, "column": column})
                    report.reasons.append("RUNTIME_REQUIRED_COLUMN_MISSING")
                elif not _affinity_compatible(expected_type, columns[column]["type"]):
                    report.type_mismatches.append({"table": table, "column": column, "expected": expected_type, "actual": columns[column]["type"]})
        lifecycle_table = "trade_lifecycle_events"
        if report.alembic_revision in {"0007_repair_runtime_lifecycle_schema", "0008_database_doctor_lifecycle_contract"} and lifecycle_table not in names:
            report.missing_tables.append(lifecycle_table)
            report.reasons.append("RUNTIME_LIFECYCLE_CONTRACT_INCOMPATIBLE")
        if lifecycle_table in names:
            columns = report.tables[lifecycle_table]
            for column, expected_type in LIFECYCLE_RUNTIME_SCHEMA.items():
                if column not in columns:
                    report.missing_columns.append({"table": lifecycle_table, "column": column})
                    report.reasons.append("RUNTIME_LIFECYCLE_CONTRACT_INCOMPATIBLE")
                elif not _affinity_compatible(expected_type, columns[column]["type"]):
                    report.type_mismatches.append({"table": lifecycle_table, "column": column, "expected": expected_type, "actual": columns[column]["type"]})
            unique_targets: set[tuple[str, ...]] = set()
            for index_row in conn.execute(f'PRAGMA index_list("{lifecycle_table}")'):
                if not bool(index_row[2]):
                    continue
                index_name = str(index_row[1]).replace('"', '""')
                target = tuple(str(row[2]) for row in conn.execute(f'PRAGMA index_info("{index_name}")'))
                unique_targets.add(target)
            for target in LIFECYCLE_CONFLICT_TARGETS:
                if target not in unique_targets:
                    report.missing_unique_constraints.append({"table": lifecycle_table, "columns": list(target)})
                    report.reasons.append("RUNTIME_LIFECYCLE_CONFLICT_TARGET_MISSING")
        if report.missing_tables:
            report.reasons.append("EXPOSURE_TABLES_MISSING")
        ptable = report.exposure_tables["positions"]
        otable = report.exposure_tables["orders"]
        pcols = report.tables.get(ptable, {})
        if report.schema_family != "ALEMBIC_HEAD" and pcols and "status" not in pcols and not ({"state", "closed_at", "exit_time"} & pcols.keys()):
            report.unsupported_legacy_shapes.append({"table": "positions", "reason": "no status/state/closed timestamp evidence"})
        ocols = report.tables.get(otable, {})
        if report.schema_family != "ALEMBIC_HEAD" and ocols and "status" not in ocols and "order_status" not in ocols:
            report.unsupported_legacy_shapes.append({"table": "orders", "reason": "no status/order_status evidence"})
        for table in report.exposure_tables.values():
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
        missing_unique_constraints=report.missing_unique_constraints,
        affected_rows=report.affected_rows, unsafe_changes_required=["manual schema/data migration required"],
        affected_features=["runtime_recovery", "reconciliation", "burnin_preflight"],
        next_action="verify database identity and perform a manual migration",
        schema_family=report.schema_family, alembic_revision=report.alembic_revision,
        exposure_tables=report.exposure_tables,
    )


def ensure_database_schema(target: Any, *, allow_fresh_bootstrap: bool = False) -> MigrationReport:
    before = inspect_database_schema(target)
    if not before.database_exists and not allow_fresh_bootstrap:
        return _blocked(before)
    existing_user_tables = set(before.tables) - {"sqlite_sequence", "schema_migrations"}
    trusted_alembic = before.schema_family == "ALEMBIC_HEAD"
    domain_counts: dict[str, int] = {}
    if trusted_alembic and before.missing_tables:
        conn, owned, _ = _connection(target)
        try:
            domain_counts = {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in ("positions", "orders")}
        finally:
            if owned:
                conn.close()
        if any(domain_counts.values()):
            before.affected_rows = [{"table": table, "row_count": count} for table, count in domain_counts.items() if count]
            return _blocked(before, ["ALEMBIC_DOMAIN_EXPOSURE_REQUIRES_RECONCILIATION"])
    elif before.missing_tables and existing_user_tables:
        return _blocked(before, ["DATABASE_IDENTITY_UNVERIFIED", "EXPOSURE_TABLES_MISSING"])
    if before.missing_tables and not allow_fresh_bootstrap and not trusted_alembic:
        return _blocked(before, ["EXPOSURE_TABLES_MISSING"])
    if before.unsupported_legacy_shapes or before.type_mismatches or "IDENTIFIER_COLUMN_MISSING" in before.reasons or "UNKNOWN_EXPOSURE_STATE" in before.reasons:
        return _blocked(before)

    conn, owned, database = _connection(target, create=allow_fresh_bootstrap)
    if conn is None:
        return _blocked(before)
    applied: list[str] = []
    details: dict[str, Any] = {"additive": True, "schema_family": before.schema_family, "alembic_revision": before.alembic_revision, "columns_added": [], "adapter": "dedicated_runtime_exposure" if trusted_alembic else "canonical", "domain_row_counts": domain_counts if trusted_alembic else None, "row_counts_before": {}, "row_counts_after": {}, "affected_rows": [], "semantic_validation": "PENDING"}
    try:
        integrity = _migration_integrity(conn)
        if integrity in {"MIGRATION_CHECKSUM_MISMATCH", "MIGRATION_PREVIOUSLY_FAILED"}:
            return _blocked(before, [integrity])
        conn.execute("BEGIN")
        _migration_table(conn)
        if allow_fresh_bootstrap and not trusted_alembic:
            conn.execute("CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY AUTOINCREMENT,symbol TEXT,qty REAL,status TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id TEXT,symbol TEXT,status TEXT,created_at TEXT)")
        if trusted_alembic:
            conn.execute("CREATE TABLE IF NOT EXISTS runtime_positions(id INTEGER PRIMARY KEY AUTOINCREMENT,symbol TEXT,qty REAL,status TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS runtime_orders(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id TEXT,symbol TEXT,status TEXT,created_at TEXT)")
            details["columns_added"] = ["runtime_positions.*", "runtime_orders.*"]
        for table, required in CONDITIONAL_RUNTIME_SCHEMA.items():
            if table not in before.tables:
                continue
            columns = before.tables[table]
            for column, ddl in required.items():
                if column not in columns:
                    conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}')
                    details["columns_added"].append(f"{table}.{column}")
        exposure_tables = before.exposure_tables
        for table in exposure_tables.values():
            details["row_counts_before"][table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        ptable, otable = exposure_tables["positions"], exposure_tables["orders"]
        pcols = {row[1] for row in conn.execute(f"PRAGMA table_info({ptable})")}
        for column, ddl in (("symbol", "TEXT"), ("qty", "REAL")):
            if column not in pcols:
                conn.execute(f"ALTER TABLE {ptable} ADD COLUMN {column} {ddl}")
                details["columns_added"].append(f"{ptable}.{column}")
        if "status" not in pcols:
            conn.execute(f"ALTER TABLE {ptable} ADD COLUMN status TEXT")
            details["columns_added"].append(f"{ptable}.status")
            if "state" in pcols:
                conn.execute(f"UPDATE {ptable} SET status=UPPER(TRIM(state))")
            elif "closed_at" in pcols:
                conn.execute(f"UPDATE {ptable} SET status=CASE WHEN closed_at IS NULL THEN 'OPEN' ELSE 'CLOSED' END")
            elif "exit_time" in pcols:
                conn.execute(f"UPDATE {ptable} SET status=CASE WHEN exit_time IS NULL THEN 'OPEN' ELSE 'CLOSED' END")
        ocols = {row[1] for row in conn.execute(f"PRAGMA table_info({otable})")}
        for column, ddl in (("order_id", "TEXT"), ("symbol", "TEXT"), ("created_at", "TEXT")):
            if column not in ocols:
                conn.execute(f"ALTER TABLE {otable} ADD COLUMN {column} {ddl}")
                details["columns_added"].append(f"{otable}.{column}")
        if "status" not in ocols:
            conn.execute(f"ALTER TABLE {otable} ADD COLUMN status TEXT")
            details["columns_added"].append(f"{otable}.status")
            conn.execute(f"UPDATE {otable} SET status=UPPER(TRIM(order_status))")
        for table in exposure_tables.values():
            details["row_counts_after"][table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            details["affected_rows"].extend(_unknown_state_rows(conn, table))
        if details["row_counts_before"] != details["row_counts_after"] or details["affected_rows"]:
            conn.rollback()
            after = inspect_database_schema(target)
            after.affected_rows = list(details["affected_rows"])
            return _blocked(after, ["UNKNOWN_EXPOSURE_STATE"] if details["affected_rows"] else ["MIGRATION_ROW_COUNT_MISMATCH"])
        details["semantic_validation"] = "PASS"
        conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{ptable}_status ON {ptable}(status)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{otable}_status ON {otable}(status)")
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
    blocked = bool(report.missing_tables or report.missing_columns or report.type_mismatches or report.missing_unique_constraints or report.unsupported_legacy_shapes or report.reasons)
    if blocked:
        return _blocked(report)
    return MigrationReport(database=report.database, schema_status="VALID", schema_version=SCHEMA_VERSION,
                           schema_family=report.schema_family, alembic_revision=report.alembic_revision,
                           exposure_tables=report.exposure_tables)


def exposure_count(conn: Any, table: str) -> int:
    validation = validate_required_schema(conn)
    if validation.schema_status != "VALID":
        raise ExposureStateError(validation)
    if table not in ACTIVE_STATES:
        raise ValueError(f"unsupported exposure table:{table}")
    states = ACTIVE_STATES[table]
    placeholders = ",".join("?" for _ in states)
    raw, _, _ = _connection(conn)
    physical_table = validation.exposure_tables[table]
    row = raw.execute(
        f"SELECT COUNT(*) FROM {physical_table} WHERE UPPER(TRIM(CAST(status AS TEXT))) IN ({placeholders})",
        tuple(sorted(states)),
    ).fetchone()
    return int(row[0] or 0)


def resolve_exposure_tables(conn: Any) -> dict[str, str]:
    """Resolve validated physical exposure tables, failing closed on ambiguity."""
    validation = validate_required_schema(conn)
    if validation.schema_status != "VALID":
        # Keep the stable operator-facing schema code ahead of doctor detail.
        if validation.reason == "MIGRATION_CHECKSUM_MISMATCH":
            raise ExposureStateError(validation)
        if validation.reason == "UNKNOWN_EXPOSURE_STATE":
            raise ExposureStateError(validation)
        validation.reason = "RUNTIME_EXPOSURE_SCHEMA_UNAVAILABLE"
        validation.reasons = list(dict.fromkeys([validation.reason, *validation.reasons]))
        raise ExposureStateError(validation)
    tables = validation.exposure_tables
    if set(tables) != {"positions", "orders"} or any(
        table not in {"positions", "orders", "runtime_positions", "runtime_orders"}
        for table in tables.values()
    ):
        validation.reason = "RUNTIME_EXPOSURE_SCHEMA_UNAVAILABLE"
        raise ExposureStateError(validation)
    return dict(tables)


def _load_active_exposure(conn: Any, logical_table: str, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    tables = resolve_exposure_tables(conn)
    raw, _, _ = _connection(conn)
    states = ACTIVE_STATES[logical_table]
    placeholders = ",".join("?" for _ in states)
    rows = raw.execute(
        f"SELECT {','.join(fields)} FROM {tables[logical_table]} "
        f"WHERE UPPER(TRIM(CAST(status AS TEXT))) IN ({placeholders})",
        tuple(sorted(states)),
    ).fetchall()
    return [dict(zip(fields, row)) for row in rows]


def load_active_positions(conn: Any) -> list[dict[str, Any]]:
    return _load_active_exposure(conn, "positions", ("symbol", "qty", "status"))


def load_pending_orders(conn: Any) -> list[dict[str, Any]]:
    return _load_active_exposure(conn, "orders", ("order_id", "symbol", "status", "created_at"))
