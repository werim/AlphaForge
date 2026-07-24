from __future__ import annotations

import json
import sqlite3
import re
import ast
from pathlib import Path

import pytest

from alphaforge.burnin_ops import main
from alphaforge.schema_doctor import (
    SCHEMA_VERSION,
    ensure_database_schema,
    exposure_count,
    inspect_database_schema,
    normalize_database_path,
    validate_required_schema,
)


def test_empty_database_is_additively_created_and_idempotent(tmp_path):
    db = tmp_path / "empty.db"
    first = ensure_database_schema(db)
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
    assert main(["--db", str(db), "db-doctor", "--apply"]) == 0
    migrated = json.loads(capsys.readouterr().out)
    assert migrated["schema_status"] == "MIGRATED"
    assert migrated["database"] == str(db.resolve())


def test_path_normalization_relative_absolute_and_windows_spelling(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert normalize_database_path("data/runtime.db") == (tmp_path / "data/runtime.db").resolve()
    assert normalize_database_path(tmp_path / "absolute.db") == (tmp_path / "absolute.db").resolve()
    assert normalize_database_path(r"C:\\Alpha Forge\\runtime.db").as_posix().startswith("C:/")


def test_schema_inventory_exposes_nullability_defaults_and_primary_key(tmp_path):
    db = tmp_path / "inventory.db"
    ensure_database_schema(db)
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
