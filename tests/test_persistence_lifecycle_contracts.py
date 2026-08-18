from __future__ import annotations

from pathlib import Path
import importlib.util
import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from alphaforge.persistence import fetch_expectancy_stat, fetch_expectancy_stat_detail, init_db


_SPEC = importlib.util.spec_from_file_location("backtest_order", Path(__file__).resolve().parents[1] / "backtest_order.py")
bo = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(bo)


def test_fetch_expectancy_stat_preserves_legacy_scalar_contract() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        session.execute(
            text("INSERT INTO setup_expectancy_stats (setup, samples, expectancy) VALUES ('breakout', 5, 0.42)")
        )
        session.execute(text("CREATE TABLE nullable_expectancy_stats (setup TEXT PRIMARY KEY, samples INTEGER, expectancy REAL)"))
        session.execute(text("INSERT INTO nullable_expectancy_stats (setup, samples, expectancy) VALUES ('missing-value', 3, NULL)"))
        session.commit()

        assert fetch_expectancy_stat(session, "not_a_table", "setup", "breakout") is None
        assert fetch_expectancy_stat(session, "setup_expectancy_stats", "setup", "absent") is None
        assert fetch_expectancy_stat(session, "nullable_expectancy_stats", "setup", "missing-value") is None
        assert fetch_expectancy_stat(session, "setup_expectancy_stats", "setup", "breakout") == 0.42


def test_fetch_expectancy_stat_detail_keeps_metadata_separate_from_scalar_api() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        session.execute(
            text("INSERT INTO setup_expectancy_stats (setup, samples, expectancy) VALUES ('breakout', 7, 0.31)")
        )
        session.commit()

        detail = fetch_expectancy_stat_detail(session, "setup_expectancy_stats", "setup", "breakout")

    assert detail is not None
    assert detail["expectancy_bucket"] == "UNKNOWN"
    assert detail["sample_size"] == 7
    assert detail["expectancy"] == 0.31


def test_init_db_blocks_incompatible_legacy_lifecycle_before_bootstrap_mutation(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_runtime.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE positions(id INTEGER PRIMARY KEY AUTOINCREMENT,symbol TEXT,qty REAL,status TEXT)")
        conn.execute("CREATE TABLE orders(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id TEXT,symbol TEXT,status TEXT)")
        conn.execute("CREATE TABLE order_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT UNIQUE)")
        conn.execute("CREATE TABLE trade_lifecycle_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE)")
        conn.execute("INSERT INTO order_decisions (decision_id) VALUES ('preserve-decision')")
        conn.execute("INSERT INTO trade_lifecycle_events (event_id) VALUES ('preserve-event')")

    with pytest.raises(RuntimeError, match="RUNTIME_LIFECYCLE_CONTRACT_INCOMPATIBLE"):
        init_db(f"sqlite+pysqlite:///{db_path}")

    with sqlite3.connect(db_path) as conn:
        lifecycle_cols = {row[1] for row in conn.execute("PRAGMA table_info(trade_lifecycle_events)")}
        decision_rows = conn.execute("SELECT COUNT(*) FROM order_decisions WHERE decision_id='preserve-decision'").fetchone()[0]
        lifecycle_rows = conn.execute("SELECT COUNT(*) FROM trade_lifecycle_events WHERE event_id='preserve-event'").fetchone()[0]

    assert lifecycle_cols == {"id", "event_id"}
    assert decision_rows == 1
    assert lifecycle_rows == 1


def test_simulate_candidate_includes_waiting_entry_zone_before_trigger() -> None:
    candidate = bo.CandidateOrder(1, "S", "LONG", 10, 9, 12, 2, "BACKTEST", "R", "X", 1, "LIMIT")
    candles = [bo.Candle(1, 11, 11, 10.5, 11, 1), bo.Candle(2, 10, 10.2, 9.8, 10.1, 1)]

    states = [row.status_after for row in bo.simulate_candidate(candidate, candles, 0, 1000, 1)]

    assert states.index("WAITING_ENTRY_ZONE") < states.index("ENTRY_TRIGGERED")
