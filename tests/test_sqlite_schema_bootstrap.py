from __future__ import annotations

import sqlite3

from sqlalchemy import text
from sqlalchemy.orm import Session

from alphaforge.persistence import init_db, save_order_decision


def test_init_db_bootstraps_schema_migrations_before_selecting_versions(tmp_path) -> None:
    db_path = tmp_path / "fresh_bootstrap.db"

    init_db(f"sqlite+pysqlite:///{db_path}")

    with sqlite3.connect(db_path) as conn:
        migration_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        assert migration_table is not None
        applied_versions = conn.execute("SELECT version FROM schema_migrations").fetchall()
        assert ("2026_05_16_persistence_integrity_v1",) in applied_versions
        assert ("2026_06_19_rollback_evidence_bootstrap",) in applied_versions


def test_init_db_bootstraps_live_rollback_validation_evidence_schema(tmp_path) -> None:
    db_path = tmp_path / "fresh_rollback_evidence.db"

    init_db(f"sqlite+pysqlite:///{db_path}")

    expected_columns = {
        "id",
        "validation_id",
        "recorded_at",
        "evidence_status",
        "rollback_evidence_source",
        "kill_switch_block_verified",
        "no_submit_on_kill_switch_verified",
        "fail_closed_reconciliation_verified",
        "repair_actions_non_mutating_verified",
        "execution_mutation_attempt_count",
        "blocking_reasons",
        "evidence_payload",
    }
    assert expected_columns.issubset(_sqlite_columns(str(db_path), "live_rollback_validation_evidence"))

    with sqlite3.connect(db_path) as conn:
        index_row = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='index' AND name='ix_live_rollback_validation_recorded_at'
            """
        ).fetchone()
        assert index_row is not None


def _sqlite_columns(db_path: str, table_name: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def test_init_db_migrates_legacy_order_decisions_schema(tmp_path) -> None:
    db_path = tmp_path / "legacy_order_decisions.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE order_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT UNIQUE, signal_id TEXT, decision TEXT)")
        conn.commit()

    engine = init_db(f"sqlite+pysqlite:///{db_path}")
    cols = _sqlite_columns(str(db_path), "order_decisions")
    assert {"phase", "execution_regime", "volatility_regime"}.issubset(cols)

    with Session(engine) as session:
        save_order_decision(session, decision_id="legacy-dec-1", signal_id="sig-1", phase="real", decision="REJECTED", order_type="LIMIT")
        row = session.execute(text("SELECT decision_id, phase, order_type FROM order_decisions WHERE decision_id='legacy-dec-1'"))
        saved = row.one()
        assert saved.phase == "real"
        assert saved.order_type == "LIMIT"


def test_init_db_migrates_legacy_ai_decision_features_schema(tmp_path) -> None:
    db_path = tmp_path / "legacy_ai_features.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE ai_decision_features (id INTEGER PRIMARY KEY AUTOINCREMENT)")
        conn.commit()

    engine = init_db(f"sqlite+pysqlite:///{db_path}")
    cols = _sqlite_columns(str(db_path), "ai_decision_features")
    assert "decision_id" in cols

    with Session(engine) as session:
        session.execute(
            text(
                """
                INSERT INTO ai_decision_features
                (decision_id, features, penalties, reason_flags, execution_features, created_at)
                VALUES (:decision_id, :features, :penalties, :reason_flags, :execution_features, :created_at)
                """
            ),
            {
                "decision_id": "dec-1",
                "features": "{}",
                "penalties": "{}",
                "reason_flags": "[]",
                "execution_features": "{}",
                "created_at": "2026-05-20T00:00:00Z",
            },
        )
        session.commit()
        count = session.execute(text("SELECT COUNT(*) FROM ai_decision_features WHERE decision_id='dec-1'"))
        assert count.scalar_one() == 1


def test_init_db_migrations_are_idempotent_and_preserve_data(tmp_path) -> None:
    db_path = tmp_path / "legacy_idempotent.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE order_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT UNIQUE)")
        conn.execute("INSERT INTO order_decisions (decision_id) VALUES ('preserved-row')")
        conn.commit()

    init_db(f"sqlite+pysqlite:///{db_path}")
    init_db(f"sqlite+pysqlite:///{db_path}")

    with sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(order_decisions)").fetchall()}
        assert "phase" in cols
        preserved = conn.execute("SELECT COUNT(*) FROM order_decisions WHERE decision_id='preserved-row'").fetchone()[0]
        assert preserved == 1
        migration_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        assert migration_table is not None
        migration_count = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version='2026_05_16_persistence_integrity_v1'"
        ).fetchone()[0]
        assert migration_count == 1
