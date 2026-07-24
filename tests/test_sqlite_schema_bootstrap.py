from __future__ import annotations

import sqlite3

import pytest

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from alphaforge.persistence import (
    _apply_sqlite_migrations,
    _timesfm_forecast_evidence_ddl,
    init_db,
    save_order_decision,
)



def test_timesfm_ddl_helper_orders_table_before_dependent_index() -> None:
    ddl = _timesfm_forecast_evidence_ddl()

    table_position = next(
        index
        for index, statement in enumerate(ddl)
        if "CREATE TABLE IF NOT EXISTS timesfm_forecast_evidence" in statement
    )
    index_position = next(
        index
        for index, statement in enumerate(ddl)
        if "CREATE INDEX IF NOT EXISTS ix_timesfm_evidence_symbol_timeframe_ts" in statement
    )

    assert table_position < index_position


def test_apply_sqlite_migrations_bootstraps_schema_migrations_on_partial_database(tmp_path) -> None:
    db_path = tmp_path / "partial_without_schema_migrations.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT)")
        conn.execute("INSERT INTO signals (signal_id) VALUES (NULL)")
        conn.commit()

    engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        _apply_sqlite_migrations(conn)

    with sqlite3.connect(db_path) as conn:
        migration_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        assert migration_table is not None
        migration_count = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version='2026_05_16_persistence_integrity_v1'"
        ).fetchone()[0]
        assert migration_count == 1
        backfilled_signal_id = conn.execute("SELECT signal_id FROM signals WHERE id=1").fetchone()[0]
        assert backfilled_signal_id == "legacy-signal-1"

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


def _create_verified_exposure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE positions(id INTEGER PRIMARY KEY AUTOINCREMENT,symbol TEXT,qty REAL,status TEXT)")
    conn.execute("CREATE TABLE orders(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id TEXT,symbol TEXT,status TEXT)")


def test_init_db_bootstraps_timesfm_evidence_before_indexes(tmp_path) -> None:
    db_path = tmp_path / "fresh_timesfm.db"

    init_db(f"sqlite+pysqlite:///{db_path}")

    expected_columns = {
        "forecast_id",
        "timestamp",
        "symbol",
        "timeframe",
        "side",
        "mode",
        "no_lookahead_input_end_ts",
    }
    assert expected_columns.issubset(_sqlite_columns(str(db_path), "timesfm_forecast_evidence"))
    with sqlite3.connect(db_path) as conn:
        index_row = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='index' AND name='ix_timesfm_evidence_symbol_timeframe_ts'
            """
        ).fetchone()
        assert index_row is not None


def test_init_db_creates_timesfm_forecast_evidence_table_and_index(tmp_path) -> None:
    db_path = tmp_path / "alphaforge.db"
    engine = init_db(f"sqlite+pysqlite:///{db_path}")

    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
        indexes = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='index'")
            )
        }

    assert "timesfm_forecast_evidence" in tables
    assert "ix_timesfm_evidence_symbol_timeframe_ts" in indexes


def test_init_db_is_idempotent_for_timesfm_table(tmp_path) -> None:
    db_path = tmp_path / "alphaforge.db"
    url = f"sqlite+pysqlite:///{db_path}"

    init_db(url)
    engine = init_db(url)

    with engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='timesfm_forecast_evidence'"
            )
        ).scalar_one()

    assert count == 1


def test_init_db_bootstraps_conservative_timesfm_evidence_columns(tmp_path) -> None:
    db_path = tmp_path / "fresh_timesfm_conservative_columns.db"

    init_db(f"sqlite+pysqlite:///{db_path}")

    expected_columns = {
        "id",
        "symbol",
        "timeframe",
        "timestamp",
        "forecast_timestamp",
        "horizon",
        "point_forecast",
        "quantiles_json",
        "model_name",
        "created_at",
    }
    assert expected_columns.issubset(_sqlite_columns(str(db_path), "timesfm_forecast_evidence"))


def test_init_db_preserves_existing_timesfm_evidence_rows_on_repeated_calls(tmp_path) -> None:
    db_path = tmp_path / "timesfm_idempotent.db"

    init_db(f"sqlite+pysqlite:///{db_path}")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO timesfm_forecast_evidence (
                forecast_id, timestamp, symbol, timeframe, side, mode, no_lookahead_input_end_ts
            ) VALUES ('forecast-preserved', 1, 'BTCUSDT', '1m', 'NO_TRADE', 'BACKTEST', 1)
            """
        )
        conn.commit()

    init_db(f"sqlite+pysqlite:///{db_path}")

    with sqlite3.connect(db_path) as conn:
        preserved = conn.execute(
            "SELECT COUNT(*) FROM timesfm_forecast_evidence WHERE forecast_id='forecast-preserved'"
        ).fetchone()[0]
        assert preserved == 1


def test_init_db_migrates_legacy_order_decisions_schema(tmp_path) -> None:
    db_path = tmp_path / "legacy_order_decisions.db"
    with sqlite3.connect(db_path) as conn:
        _create_verified_exposure_schema(conn)
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
        _create_verified_exposure_schema(conn)
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
        _create_verified_exposure_schema(conn)
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


REQUIRED_BASELINE_TABLES = {
    "signals",
    "order_decisions",
    "signal_id_state",
    "positions",
    "orders",
    "fills",
    "paper_events",
    "backtest_runs",
    "backtest_events",
    "symbol_snapshots",
    "timesfm_forecast_evidence",
    "runtime_control_state",
    "calibration_labels",
    "optimizer_runs",
}

REQUIRED_BASELINE_INDEXES = {"ix_timesfm_evidence_symbol_timeframe_ts"}


def _sqlite_url(path):
    return f"sqlite+pysqlite:///{path}"


def _table_names(engine):
    with engine.connect() as conn:
        return {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}


def _index_names(engine):
    with engine.connect() as conn:
        return {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))}


def _upgrade_alembic_head(db_path):
    pytest.importorskip("alembic.command")
    from alembic import command
    from alembic.config import Config

    repo_root = __import__("pathlib").Path(__file__).resolve().parents[1]
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "alembic"))
    config.set_main_option("sqlalchemy.url", _sqlite_url(db_path))
    command.upgrade(config, "head")


def _assert_required_schema(engine):
    assert REQUIRED_BASELINE_TABLES.issubset(_table_names(engine))
    assert REQUIRED_BASELINE_INDEXES.issubset(_index_names(engine))


def test_init_db_and_alembic_baseline_schema_paths_are_idempotent(tmp_path) -> None:
    init_only = tmp_path / "init_only.db"
    init_engine = init_db(_sqlite_url(init_only))
    init_db(_sqlite_url(init_only))
    _assert_required_schema(init_engine)

    alembic_only = tmp_path / "alembic_only.db"
    _upgrade_alembic_head(alembic_only)
    _upgrade_alembic_head(alembic_only)
    alembic_engine = create_engine(_sqlite_url(alembic_only), future=True)
    _assert_required_schema(alembic_engine)

    init_then_alembic = tmp_path / "init_then_alembic.db"
    init_db(_sqlite_url(init_then_alembic))
    _upgrade_alembic_head(init_then_alembic)
    init_db(_sqlite_url(init_then_alembic))
    _assert_required_schema(create_engine(_sqlite_url(init_then_alembic), future=True))

    alembic_then_init = tmp_path / "alembic_then_init.db"
    _upgrade_alembic_head(alembic_then_init)
    init_db(_sqlite_url(alembic_then_init))
    _upgrade_alembic_head(alembic_then_init)
    _assert_required_schema(create_engine(_sqlite_url(alembic_then_init), future=True))

CORE_IDENTIFIER_COLUMNS = {
    "signals": {"signal_id", "symbol", "timeframe", "mode", "created_at", "updated_at"},
    "order_decisions": {"decision_id", "signal_id", "symbol", "timeframe", "mode", "created_at", "updated_at"},
    "signal_id_state": {"signal_id", "symbol", "timeframe", "mode", "created_at", "updated_at"},
    "orders": {"order_id", "signal_id", "position_id", "symbol", "timeframe", "mode", "created_at", "updated_at"},
    "positions": {"position_id", "signal_id", "symbol", "timeframe", "mode", "created_at", "updated_at"},
    "fills": {"order_id", "position_id", "signal_id", "symbol", "created_at"},
    "paper_events": {"event_id", "signal_id", "order_id", "position_id", "symbol", "timeframe", "mode", "created_at"},
    "backtest_runs": {"run_id", "mode", "created_at", "updated_at"},
    "backtest_events": {"event_id", "run_id", "signal_id", "order_id", "position_id", "symbol", "timeframe", "mode", "created_at"},
    "symbol_snapshots": {"run_id", "symbol", "timeframe", "mode", "created_at"},
    "timesfm_forecast_evidence": {"symbol", "timeframe", "timestamp", "created_at"},
    "calibration_labels": {"signal_id", "run_id", "symbol", "timeframe", "mode", "created_at"},
    "optimizer_runs": {"run_id", "created_at", "updated_at"},
}

CORE_IDENTIFIER_INDEXES = {
    "ix_signals_signal_id",
    "ix_order_decisions_decision_id",
    "ix_order_decisions_signal_id",
    "ix_orders_order_id",
    "ix_orders_signal_id",
    "ix_orders_position_id",
    "ix_positions_position_id",
    "ix_positions_signal_id",
    "ix_fills_order_id",
    "ix_fills_position_id",
    "ix_paper_events_signal_id",
    "ix_paper_events_position_id",
    "ix_backtest_events_run_id",
    "ix_backtest_events_signal_id",
    "ix_calibration_labels_signal_id",
    "ix_optimizer_runs_run_id",
}


def _assert_core_identifier_schema(engine) -> None:
    for table_name, expected_columns in CORE_IDENTIFIER_COLUMNS.items():
        with engine.connect() as conn:
            columns = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table_name})"))}
        assert expected_columns.issubset(columns), f"{table_name} missing {expected_columns - columns}"
    assert CORE_IDENTIFIER_INDEXES.issubset(_index_names(engine))


def test_fresh_init_db_creates_core_identifier_columns_and_indexes(tmp_path) -> None:
    engine = init_db(_sqlite_url(tmp_path / "fresh_init_ids.db"))

    _assert_core_identifier_schema(engine)


def test_fresh_alembic_upgrade_creates_core_identifier_columns_and_indexes(tmp_path) -> None:
    db_path = tmp_path / "fresh_alembic_ids.db"
    _upgrade_alembic_head(db_path)

    _assert_core_identifier_schema(create_engine(_sqlite_url(db_path), future=True))


def test_mixed_init_db_and_alembic_preserve_core_identifier_schema(tmp_path) -> None:
    init_then_alembic = tmp_path / "init_then_alembic_ids.db"
    init_db(_sqlite_url(init_then_alembic))
    _upgrade_alembic_head(init_then_alembic)
    _assert_core_identifier_schema(create_engine(_sqlite_url(init_then_alembic), future=True))

    alembic_then_init = tmp_path / "alembic_then_init_ids.db"
    _upgrade_alembic_head(alembic_then_init)
    engine = init_db(_sqlite_url(alembic_then_init))
    _assert_core_identifier_schema(engine)


def test_legacy_identifier_tables_are_additively_repaired_and_insertable(tmp_path) -> None:
    db_path = tmp_path / "legacy_core_ids.db"
    with sqlite3.connect(db_path) as conn:
        _create_verified_exposure_schema(conn)
        conn.execute("INSERT INTO orders (order_id, status) VALUES ('order-preserved', 'FILLED')")
        conn.execute("CREATE TABLE paper_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE)")
        conn.commit()

    engine = init_db(_sqlite_url(db_path))

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO paper_events(event_id, signal_id, order_id, position_id, symbol, timeframe, mode, created_at)
                VALUES ('event-1', 'signal-1', 'order-1', 'position-1', 'BTCUSDT', '1m', 'PAPER', '2026-06-23T00:00:00Z')
                """
            )
        )
        preserved = conn.execute(text("SELECT COUNT(*) FROM orders WHERE order_id='order-preserved'")).scalar_one()
        event_count = conn.execute(text("SELECT COUNT(*) FROM paper_events WHERE signal_id='signal-1'")).scalar_one()

    assert preserved == 1
    assert event_count == 1
    _assert_core_identifier_schema(engine)
