from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
VERSIONS_DIR = REPO_ROOT / "alembic" / "versions"


def _migration_revisions() -> tuple[dict[str, Path], dict[Path, str | tuple[str, ...] | None]]:
    revisions: dict[str, Path] = {}
    down_revisions: dict[Path, str | tuple[str, ...] | None] = {}
    for migration_path in sorted(VERSIONS_DIR.glob("*.py")):
        module = ast.parse(migration_path.read_text(encoding="utf-8"), filename=str(migration_path))
        values: dict[str, object] = {}
        for node in module.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                    values[target.id] = ast.literal_eval(node.value)
        revision = values.get("revision")
        assert isinstance(revision, str), f"{migration_path.name} must define a string revision"
        assert revision not in revisions, f"duplicate Alembic revision {revision!r}"
        revisions[revision] = migration_path
        down_revision = values.get("down_revision")
        assert down_revision is None or isinstance(down_revision, (str, tuple)), (
            f"{migration_path.name} down_revision must be None, a revision string, or a tuple"
        )
        down_revisions[migration_path] = down_revision
    return revisions, down_revisions


def test_alembic_migrations_do_not_reference_missing_down_revisions() -> None:
    revisions, down_revisions = _migration_revisions()

    missing: list[str] = []
    for migration_path, down_revision in down_revisions.items():
        referenced = () if down_revision is None else ((down_revision,) if isinstance(down_revision, str) else down_revision)
        for revision_id in referenced:
            if revision_id not in revisions:
                missing.append(f"{migration_path.name} references missing down_revision {revision_id!r}")

    assert not missing, "Alembic revision graph has dangling down_revision references: " + "; ".join(missing)


def test_alembic_script_directory_loads_and_resolves_heads() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(REPO_ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["0007_repair_runtime_lifecycle_schema"]
    assert script.get_current_head() == "0007_repair_runtime_lifecycle_schema"


def test_alembic_upgrade_head_succeeds_on_temporary_sqlite_database(tmp_path: Path) -> None:
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "alembic_upgrade_head.db"
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{db_path}")

    command.upgrade(config, "head")

    import sqlite3

    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"config_snapshots", "timesfm_forecast_evidence", "timesfm_forward_outcome_labels"}.issubset(tables)
        index_row = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='index' AND name='ix_timesfm_evidence_symbol_timeframe_ts'
            """
        ).fetchone()
        assert index_row is not None
        trigger_rows = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='trigger'
              AND tbl_name='config_snapshots'
              AND name IN ('trg_config_snapshots_no_update', 'trg_config_snapshots_no_delete')
            ORDER BY name
            """
        ).fetchall()
        assert trigger_rows == [('trg_config_snapshots_no_delete',), ('trg_config_snapshots_no_update',)]


def test_alembic_upgrade_head_is_idempotent_on_partially_initialized_sqlite_database(tmp_path: Path) -> None:
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "partially_initialized.db"
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE exchange_symbols (
                id BIGINT PRIMARY KEY,
                venue VARCHAR(32) NOT NULL,
                market_type VARCHAR(6) NOT NULL,
                symbol VARCHAR(64) NOT NULL,
                pair VARCHAR(64) NOT NULL,
                contract_type VARCHAR(32) NOT NULL,
                base_asset VARCHAR(32) NOT NULL,
                quote_asset VARCHAR(32) NOT NULL,
                margin_asset VARCHAR(32) NOT NULL,
                status VARCHAR(16) NOT NULL,
                onboard_date DATETIME,
                delivery_date DATETIME,
                price_precision INTEGER NOT NULL,
                quantity_precision INTEGER NOT NULL,
                tick_size NUMERIC(20, 10) NOT NULL,
                step_size NUMERIC(20, 10) NOT NULL,
                min_qty NUMERIC(20, 10) NOT NULL,
                min_notional NUMERIC(20, 10) NOT NULL,
                contract_size NUMERIC(20, 10) NOT NULL,
                last_synced_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                raw_exchange_info_json JSON NOT NULL,
                CONSTRAINT uq_exchange_symbol UNIQUE (venue, market_type, symbol),
                CHECK (price_precision >= 0),
                CHECK (quantity_precision >= 0)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO exchange_symbols (
                id, venue, market_type, symbol, pair, contract_type,
                base_asset, quote_asset, margin_asset, status,
                price_precision, quantity_precision, tick_size, step_size,
                min_qty, min_notional, contract_size, raw_exchange_info_json
            ) VALUES (
                1, 'BINANCE', 'USDT_M', 'BTCUSDT', 'BTCUSDT', 'PERPETUAL',
                'BTC', 'USDT', 'USDT', 'TRADING', 2, 3, 0.01, 0.001,
                0.001, 5, 1, '{"sentinel": "preserve-me"}'
            )
            """
        )

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{db_path}")

    command.upgrade(config, "head")
    command.upgrade(config, "head")

    empty_db_path = tmp_path / "empty_reference.db"
    empty_config = Config(str(REPO_ROOT / "alembic.ini"))
    empty_config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    empty_config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{empty_db_path}")
    command.upgrade(empty_config, "head")

    with sqlite3.connect(db_path) as conn, sqlite3.connect(empty_db_path) as empty_conn:
        assert conn.execute("SELECT raw_exchange_info_json FROM exchange_symbols WHERE id = 1").fetchone() == (
            '{"sentinel": "preserve-me"}',
        )
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        empty_database_tables = {
            row[0] for row in empty_conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert tables == empty_database_tables
        assert {"config_snapshots", "timesfm_forecast_evidence", "timesfm_forward_outcome_labels"}.issubset(tables)
        triggers = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'trg_%_no_%'"
            )
        }
        assert triggers == {
            "trg_config_snapshots_no_update", "trg_config_snapshots_no_delete",
            "trg_rejection_audit_no_update", "trg_rejection_audit_no_delete",
            "trg_order_decision_audit_no_update", "trg_order_decision_audit_no_delete",
        }
        empty_database_triggers = {
            row[0] for row in empty_conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }
        assert triggers == empty_database_triggers
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0007_repair_runtime_lifecycle_schema",
        )


def test_alembic_upgrade_fails_closed_for_incompatible_existing_table(tmp_path: Path) -> None:
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "incompatible.db"
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE exchange_symbols (id BIGINT PRIMARY KEY, sentinel TEXT)")

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{db_path}")

    with pytest.raises(RuntimeError, match="existing table 'exchange_symbols' is incompatible"):
        command.upgrade(config, "head")

    with sqlite3.connect(db_path) as conn:
        version_table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'alembic_version'"
        ).fetchone()
        if version_table_exists:
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'config_snapshots'"
        ).fetchone() is None
