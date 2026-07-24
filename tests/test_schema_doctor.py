from __future__ import annotations

import json
import sqlite3
import re
import ast
from pathlib import Path

import pytest

from alphaforge.burnin_ops import main
from alphaforge.persistence import init_db
from alphaforge.schema_doctor import (
    MIGRATION_CHECKSUM,
    PREVIOUS_MIGRATION_CHECKSUM,
    PREVIOUS_SCHEMA_VERSION,
    SCHEMA_VERSION,
    ensure_database_schema,
    exposure_count,
    inspect_database_schema,
    normalize_database_path,
    validate_required_schema,
)


def test_empty_database_is_additively_created_and_idempotent(tmp_path):
    db = tmp_path / "empty.db"
    first = ensure_database_schema(db, allow_fresh_bootstrap=True)
    second = ensure_database_schema(db)
    assert first.schema_status == "MIGRATED"
    assert first.applied_migrations == [SCHEMA_VERSION]
    assert second.schema_status == "VALID"
    assert second.applied_migrations == []
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=?", (SCHEMA_VERSION,)).fetchone()[0] == 1


@pytest.mark.parametrize(
    "position_columns,values,expected",
    [
        ("state TEXT", ("ACTIVE",), 1),
        ("closed_at TEXT", (None,), 1),
        ("exit_time TEXT", ("2026-01-01",), 0),
    ],
)
def test_legacy_position_shapes_migrate_without_data_loss(tmp_path, position_columns, values, expected):
    db = tmp_path / (position_columns.split()[0] + ".db")
    legacy = position_columns.split()[0]
    with sqlite3.connect(db) as conn:
        conn.execute(f"CREATE TABLE positions(id INTEGER PRIMARY KEY, symbol TEXT, qty REAL, {position_columns})")
        conn.execute(f"INSERT INTO positions(id,symbol,qty,{legacy}) VALUES(1,'BTCUSDT',2,?)", values)
        conn.execute("CREATE TABLE orders(id INTEGER PRIMARY KEY,order_id TEXT,symbol TEXT,order_status TEXT)")
        conn.execute("INSERT INTO orders VALUES(1,'o1','BTCUSDT','PENDING')")
    result = ensure_database_schema(db)
    assert result.schema_status == "MIGRATED"
    with sqlite3.connect(db) as conn:
        assert exposure_count(conn, "positions") == expected
        assert exposure_count(conn, "orders") == 1
        assert conn.execute("SELECT symbol,qty FROM positions WHERE id=1").fetchone() == ("BTCUSDT", 2.0)


@pytest.mark.parametrize("table,ddl", [("positions", "id INTEGER PRIMARY KEY, mystery TEXT"), ("orders", "id INTEGER PRIMARY KEY, mystery TEXT")])
def test_unknown_exposure_schema_fails_closed_and_is_not_mutated(tmp_path, table, ddl):
    db = tmp_path / f"unknown-{table}.db"
    with sqlite3.connect(db) as conn:
        conn.execute(f"CREATE TABLE {table}({ddl})")
    report = ensure_database_schema(db)
    assert report.schema_status == "BLOCKED"
    assert report.unsupported_legacy_shapes
    with sqlite3.connect(db) as conn:
        with pytest.raises(RuntimeError, match="SCHEMA_UNSUPPORTED"):
            exposure_count(conn, table)


def test_wrong_type_is_reported_not_rewritten(tmp_path):
    db = tmp_path / "wrong-type.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE positions(id INTEGER PRIMARY KEY,symbol TEXT,qty TEXT,status TEXT)")
        conn.execute("CREATE TABLE orders(id INTEGER PRIMARY KEY,order_id TEXT,symbol TEXT,status TEXT)")
    report = ensure_database_schema(db)
    assert report.schema_status == "BLOCKED"
    assert report.type_mismatches == [{"table": "positions", "column": "qty", "expected": "REAL", "actual": "TEXT"}]


def test_migration_rolls_back_on_failure(monkeypatch, tmp_path):
    db = tmp_path / "rollback.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE positions(id INTEGER PRIMARY KEY,symbol TEXT,qty REAL,state TEXT)")
        conn.execute("CREATE TABLE orders(id INTEGER PRIMARY KEY,order_id TEXT,symbol TEXT,order_status TEXT)")
    real_connect = sqlite3.connect

    class Broken:
        def __init__(self, wrapped): self.wrapped = wrapped
        def __getattr__(self, name): return getattr(self.wrapped, name)
        def execute(self, sql, params=()):
            if "CREATE INDEX IF NOT EXISTS ix_orders_status" in sql: raise sqlite3.OperationalError("injected")
            return self.wrapped.execute(sql, params)

    monkeypatch.setattr("alphaforge.schema_doctor.sqlite3.connect", lambda *a, **k: Broken(real_connect(*a, **k)))
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        ensure_database_schema(db)
    monkeypatch.undo()
    with sqlite3.connect(db) as conn:
        assert "status" not in {r[1] for r in conn.execute("PRAGMA table_info(positions)")}


def test_db_doctor_cli_check_and_apply_reports_canonical_path(tmp_path, capsys):
    db = tmp_path / "cli.db"
    assert main(["--db", str(db), "db-doctor", "--check-only"]) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["schema_status"] == "BLOCKED"
    assert not db.exists()
    assert main(["--db", str(db), "db-doctor", "--apply"]) == 2
    blocked_apply = json.loads(capsys.readouterr().out)
    assert blocked_apply["reason"] == "DATABASE_IDENTITY_UNVERIFIED"
    assert not db.exists()


def test_confirmed_fresh_persistence_bootstrap_can_create_exposure_tables(tmp_path):
    db = tmp_path / "canonical-new.db"
    init_db(f"sqlite+pysqlite:///{db}").dispose()
    assert validate_required_schema(db).schema_status == "VALID"


def test_existing_unrelated_database_is_not_made_clean(tmp_path):
    db = tmp_path / "wrong-path.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE unrelated(id INTEGER PRIMARY KEY)")
    report = ensure_database_schema(db)
    assert report.schema_status == "BLOCKED"
    assert report.reason == "DATABASE_IDENTITY_UNVERIFIED"
    with sqlite3.connect(db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "positions" not in tables and "orders" not in tables


@pytest.mark.parametrize("table,value", [
    ("positions", None), ("positions", ""), ("positions", "MYSTERY"),
    ("orders", None), ("orders", "MYSTERY"),
])
def test_unknown_exposure_states_block_with_affected_rows(tmp_path, table, value):
    db = tmp_path / f"unknown-state-{table}-{value}.db"
    ensure_database_schema(db, allow_fresh_bootstrap=True)
    with sqlite3.connect(db) as conn:
        if table == "positions":
            conn.execute("INSERT INTO positions(id,symbol,qty,status) VALUES(1,'BTCUSDT',1,?)", (value,))
        else:
            conn.execute("INSERT INTO orders(id,order_id,symbol,status) VALUES(1,'o1','BTCUSDT',?)", (value,))
    report = validate_required_schema(db)
    assert report.schema_status == "BLOCKED"
    assert report.reason == "UNKNOWN_EXPOSURE_STATE"
    assert report.affected_rows == [{"table": table, "id": 1, "status": value}]


def test_recognized_terminal_states_are_authoritative_and_clean(tmp_path):
    db = tmp_path / "terminal.db"
    ensure_database_schema(db, allow_fresh_bootstrap=True)
    with sqlite3.connect(db) as conn:
        conn.executemany("INSERT INTO positions(id,status) VALUES(?,?)", [(1, "CLOSED"), (2, "POSITION_CLOSED"), (3, "EXITED"), (4, "CANCELLED")])
        conn.executemany("INSERT INTO orders(id,status) VALUES(?,?)", [(1, "FILLED"), (2, "CANCELLED"), (3, "REJECTED"), (4, "EXPIRED"), (5, "CLOSED")])
        assert exposure_count(conn, "positions") == 0
        assert exposure_count(conn, "orders") == 0


def test_recognized_active_states_are_counted_as_exposure(tmp_path):
    db = tmp_path / "active.db"
    ensure_database_schema(db, allow_fresh_bootstrap=True)
    with sqlite3.connect(db) as conn:
        conn.executemany("INSERT INTO positions(id,status) VALUES(?,?)", [(1, "OPEN"), (2, "POSITION_OPENED"), (3, "ACTIVE")])
        conn.executemany("INSERT INTO orders(id,status) VALUES(?,?)", [(1, "PENDING"), (2, "OPEN"), (3, "ORDER_PLACED"), (4, "ENTRY_SUBMITTED")])
        assert exposure_count(conn, "positions") == 3
        assert exposure_count(conn, "orders") == 4


@pytest.mark.parametrize("success,checksum,reason", [
    (1, "tampered", "MIGRATION_CHECKSUM_MISMATCH"),
    (0, MIGRATION_CHECKSUM, "MIGRATION_PREVIOUSLY_FAILED"),
])
def test_existing_migration_integrity_is_verified(tmp_path, success, checksum, reason):
    db = tmp_path / f"integrity-{reason}.db"
    ensure_database_schema(db, allow_fresh_bootstrap=True)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE schema_migrations SET checksum=?,success=? WHERE version=?", (checksum, success, SCHEMA_VERSION))
    report = validate_required_schema(db)
    assert report.schema_status == "BLOCKED"
    assert report.reason == reason


def test_deployed_v1_checksum_remains_valid_and_v2_is_added(tmp_path):
    db = tmp_path / "upgrade-from-v1.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE positions(id INTEGER PRIMARY KEY,symbol TEXT,qty REAL,status TEXT)")
        conn.execute("CREATE TABLE orders(id INTEGER PRIMARY KEY,order_id TEXT,symbol TEXT,status TEXT)")
        conn.execute("CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,name TEXT,applied_at TEXT NOT NULL,checksum TEXT,success INTEGER,details_json TEXT,notes TEXT)")
        conn.execute("INSERT INTO schema_migrations(version,applied_at,checksum,success) VALUES(?,?,?,1)", (PREVIOUS_SCHEMA_VERSION, "2026-07-24", PREVIOUS_MIGRATION_CHECKSUM))
    report = ensure_database_schema(db)
    assert report.schema_status == "MIGRATED"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version IN (?,?)", (PREVIOUS_SCHEMA_VERSION, SCHEMA_VERSION)).fetchone()[0] == 2


@pytest.mark.parametrize("missing_from", ["positions", "orders"])
def test_missing_identifier_is_explicitly_unsupported(tmp_path, missing_from):
    db = tmp_path / f"missing-id-{missing_from}.db"
    with sqlite3.connect(db) as conn:
        positions_id = "" if missing_from == "positions" else "id INTEGER PRIMARY KEY,"
        orders_id = "" if missing_from == "orders" else "id INTEGER PRIMARY KEY,"
        conn.execute(f"CREATE TABLE positions({positions_id} symbol TEXT,qty REAL,status TEXT)")
        conn.execute(f"CREATE TABLE orders({orders_id} order_id TEXT,symbol TEXT,status TEXT)")
    report = ensure_database_schema(db)
    assert report.schema_status == "BLOCKED"
    assert report.reason == "IDENTIFIER_COLUMN_MISSING"
    assert "manual" in report.next_action


def test_legacy_unknown_source_rolls_back_and_does_not_record_success(tmp_path):
    db = tmp_path / "legacy-unknown.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE positions(id INTEGER PRIMARY KEY,symbol TEXT,qty REAL,state TEXT)")
        conn.execute("INSERT INTO positions VALUES(1,'BTCUSDT',1,'UNMAPPED')")
        conn.execute("CREATE TABLE orders(id INTEGER PRIMARY KEY,order_id TEXT,symbol TEXT,order_status TEXT)")
        conn.execute("INSERT INTO orders VALUES(1,'o1','BTCUSDT','FILLED')")
    report = ensure_database_schema(db)
    assert report.schema_status == "BLOCKED"
    assert report.reason == "UNKNOWN_EXPOSURE_STATE"
    assert report.affected_rows[0]["id"] == 1
    with sqlite3.connect(db) as conn:
        assert "status" not in {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
        assert "schema_migrations" not in {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_path_normalization_relative_absolute_and_windows_spelling(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert normalize_database_path("data/runtime.db") == (tmp_path / "data/runtime.db").resolve()
    assert normalize_database_path(tmp_path / "absolute.db") == (tmp_path / "absolute.db").resolve()
    assert normalize_database_path(r"C:\\Alpha Forge\\runtime.db").as_posix().startswith("C:/")


def test_schema_inventory_exposes_nullability_defaults_and_primary_key(tmp_path):
    db = tmp_path / "inventory.db"
    ensure_database_schema(db, allow_fresh_bootstrap=True)
    report = inspect_database_schema(db)
    assert report.tables["positions"]["id"]["primary_key"] is True
    assert "nullable" in report.tables["positions"]["status"]


def test_runtime_exposure_raw_sql_uses_registered_columns():
    """CI regression guard for the failure class that prompted this audit."""
    root = Path(__file__).parents[1] / "src" / "alphaforge"
    allowed = {
        "positions": {"id", "position_id", "signal_id", "symbol", "timeframe", "mode", "side", "qty", "entry_price", "status", "state", "closed_at", "exit_time", "created_at", "updated_at"},
        "orders": {"id", "order_id", "signal_id", "position_id", "symbol", "timeframe", "mode", "side", "status", "order_status", "created_at", "updated_at"},
    }
    unknown = []
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        strings = [node.value for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        for table in allowed:
            for fragment in strings:
                if not re.search(rf"(?is)(?:FROM|UPDATE|INTO)\s+{table}\b", fragment):
                    continue
                for name in re.findall(r"COALESCE\(\s*([A-Za-z_]\w*)", fragment, re.I):
                    if name.lower() not in allowed[table]:
                        unknown.append((path.name, table, name))
    assert unknown == []
