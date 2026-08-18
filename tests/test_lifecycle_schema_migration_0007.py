from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alphaforge.persistence import save_trade_lifecycle_event
from alphaforge.schema_doctor import LIFECYCLE_CONFLICT_TARGETS, LIFECYCLE_RUNTIME_SCHEMA, validate_required_schema


MIXED_COLUMNS = """
    id INTEGER PRIMARY KEY, order_intent_id INTEGER, event_type TEXT,
    event_payload TEXT, event_id TEXT, signal_id TEXT, order_id TEXT,
    symbol TEXT, mode TEXT, trade_id TEXT, state TEXT, payload TEXT,
    lifecycle_seq INTEGER, cancel_reason TEXT, lifecycle_id TEXT,
    failure_reason TEXT, reconciliation_reason TEXT, incident_payload TEXT
"""


def _alembic_config(db: Path):
    pytest.importorskip("alembic.command")
    from alembic.config import Config

    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{db}")
    return config


def _mixed_0006_database(db: Path, *, duplicate_event_id: bool = False) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE alembic_version(version_num VARCHAR(64) NOT NULL)")
        conn.execute("INSERT INTO alembic_version VALUES('0006_reject_label_identity_timeframe')")
        conn.execute("CREATE TABLE positions(id INTEGER PRIMARY KEY, symbol TEXT, qty REAL, status TEXT)")
        conn.execute("CREATE TABLE orders(id INTEGER PRIMARY KEY, order_id TEXT, symbol TEXT, status TEXT, created_at TEXT)")
        conn.execute(f"CREATE TABLE trade_lifecycle_events({MIXED_COLUMNS})")
        conn.execute(
            "INSERT INTO trade_lifecycle_events(id,event_id,signal_id,symbol,mode,state,event_payload) "
            "VALUES(1,'legacy-1','signal-1','BTCUSDT','PAPER','SIGNAL_CREATED','{\"source\":\"legacy\"}')"
        )
        conn.execute(
            "INSERT INTO trade_lifecycle_events(id,event_id,signal_id,symbol,mode,state,event_payload) "
            "VALUES(2,?,NULL,'ETHUSDT','PAPER','UNRECOGNIZED_LEGACY_STATE','legacy-two')",
            ("legacy-1" if duplicate_event_id else None,),
        )


def _unique_targets(conn: sqlite3.Connection) -> set[tuple[str, ...]]:
    targets = set()
    for row in conn.execute("PRAGMA index_list(trade_lifecycle_events)"):
        if row[2]:
            targets.add(tuple(info[2] for info in conn.execute(f'PRAGMA index_info("{row[1]}")')))
    return targets


def test_mixed_0006_upgrade_repairs_lifecycle_contract_without_inventing_evidence(tmp_path: Path) -> None:
    from alembic import command

    db = tmp_path / "mixed.db"
    _mixed_0006_database(db)
    before = validate_required_schema(db)
    assert before.schema_status == "BLOCKED"
    assert "RUNTIME_LIFECYCLE_CONTRACT_INCOMPATIBLE" in before.reasons

    config = _alembic_config(db)
    command.upgrade(config, "head")
    command.upgrade(config, "head")  # normal Alembic idempotency

    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(trade_lifecycle_events)")}
        assert set(LIFECYCLE_RUNTIME_SCHEMA).issubset(columns)
        assert set(LIFECYCLE_CONFLICT_TARGETS).issubset(_unique_targets(conn))
        assert conn.execute("SELECT COUNT(*) FROM trade_lifecycle_events").fetchone()[0] == 2
        rows = conn.execute(
            "SELECT id,event_id,state,lifecycle_state,event_ts,created_at,event_payload,payload "
            "FROM trade_lifecycle_events ORDER BY id"
        ).fetchall()
        assert rows[0] == (1, "legacy-1", "SIGNAL_CREATED", "SIGNAL_CREATED", None, None, '{"source":"legacy"}', None)
        assert rows[1] == (2, None, "UNRECOGNIZED_LEGACY_STATE", None, None, None, "legacy-two", None)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0007_repair_runtime_lifecycle_schema"

    assert validate_required_schema(db).schema_status == "VALID"
    engine = create_engine(f"sqlite+pysqlite:///{db}", future=True)
    with Session(engine) as session:
        assert save_trade_lifecycle_event(
            session, event_id="runtime-1", signal_id="runtime-signal", symbol="SOLUSDT",
            mode="PAPER", lifecycle_state="SIGNAL_CREATED", event_ts="2026-08-18T12:00:00Z",
        ) is True
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT lifecycle_state FROM trade_lifecycle_events WHERE event_id='runtime-1'").fetchone() == ("SIGNAL_CREATED",)


def test_0007_fails_closed_with_duplicate_event_identity(tmp_path: Path) -> None:
    from alembic import command

    db = tmp_path / "duplicates.db"
    _mixed_0006_database(db, duplicate_event_id=True)
    with pytest.raises(Exception, match="unsafe lifecycle uniqueness repair blocked.*event_id"):
        command.upgrade(_alembic_config(db), "head")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0006_reject_label_identity_timeframe"
        assert conn.execute("SELECT COUNT(*) FROM trade_lifecycle_events").fetchone()[0] == 2
