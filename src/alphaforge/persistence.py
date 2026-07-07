from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
import logging
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from alphaforge.contracts import canonical_reject_reason, canonical_utc_timestamp, validate_transition
from alphaforge.lifecycle_contract import normalize_lifecycle_event



LOGGER = logging.getLogger(__name__)

__all__ = [
    "init_db",
    "fetch_expectancy_stat",
    "fetch_expectancy_stat_detail",
    "save_ai_decision_features",
    "save_signal",
    "save_order_decision",
    "save_rejected_decision_artifact",
    "save_trade_lifecycle_event",
    "save_closed_trade_review",
    "save_timesfm_forecast_evidence",
    "upsert_expectancy_stats",
]


def _utc_now_iso() -> str:
    return canonical_utc_timestamp()


def _ensure_sqlite_parent_dir(database_url: str) -> None:
    """Create the parent directory for file-backed SQLite URLs.

    SQLAlchemy/SQLite will create the database file, but it will not create
    missing parent directories. Runtime can pass either an explicit DB URL or
    call init_db() with no arguments, so this helper keeps both paths safe.
    """
    url = make_url(database_url)
    if not url.get_backend_name().startswith("sqlite"):
        return

    database = url.database
    if not database or database == ":memory:":
        return

    db_path = Path(database).expanduser()
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path

    db_path.parent.mkdir(parents=True, exist_ok=True)


def _timesfm_forecast_evidence_ddl() -> list[str]:
    """Return TimesFM evidence DDL with tables before dependent indexes.

    SQLite validates the target table when creating an index, even with
    ``IF NOT EXISTS`` on the index. Keep the canonical evidence table first so
    fresh databases and partial legacy databases can bootstrap idempotently
    without dropping or rewriting existing research evidence rows.
    """
    return [
        """
        CREATE TABLE IF NOT EXISTS timesfm_forecast_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            forecast_id TEXT NOT NULL UNIQUE,
            timestamp INTEGER NOT NULL,
            forecast_timestamp TEXT,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            horizon INTEGER,
            point_forecast REAL,
            quantiles_json TEXT,
            current_price REAL,
            forecast_p10 REAL,
            forecast_p50 REAL,
            forecast_p90 REAL,
            side TEXT NOT NULL,
            expected_rr REAL,
            rejection_reason TEXT,
            mode TEXT NOT NULL,
            model_provider TEXT,
            model_name TEXT,
            model_version TEXT,
            no_lookahead_input_end_ts INTEGER NOT NULL,
            payload_json TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS timesfm_forward_outcome_labels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            forecast_id TEXT NOT NULL UNIQUE,
            outcome TEXT NOT NULL,
            mfe REAL,
            mae REAL,
            expected_r REAL,
            realized_r REAL,
            labeled_at TEXT,
            payload_json TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_timesfm_evidence_symbol_timeframe_ts ON timesfm_forecast_evidence(symbol, timeframe, timestamp)",
    ]


def init_db(database_url: str | None = None) -> Engine:
    resolved_database_url = (
        database_url
        or os.getenv("ALPHAFORGE_DATABASE_URL")
        or os.getenv("ALPHAFORGE_DB_URL")
        or "sqlite+pysqlite:///:memory:"
    )
    _ensure_sqlite_parent_dir(resolved_database_url)
    engine = create_engine(resolved_database_url, future=True)
    ddl = [
        """
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT UNIQUE,
            symbol TEXT,
            side TEXT,
            timeframe TEXT,
            mode TEXT,
            score REAL,
            rr REAL,
            effective_rr REAL,
            expectancy_bucket TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS order_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id TEXT UNIQUE,
            signal_id TEXT,
            order_id TEXT,
            symbol TEXT,
            timeframe TEXT,
            mode TEXT,
            decision TEXT,
            reject_reason TEXT,
            score REAL,
            rr REAL,
            effective_rr REAL,
            expectancy_bucket TEXT,
            payload TEXT,
            execution_ctx TEXT,
            execution_ctx_missing INTEGER,
            input_snapshot_hash TEXT,
            no_submit_verified INTEGER,
            parity_result TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """,
        "CREATE TABLE IF NOT EXISTS ai_decision_features (id INTEGER PRIMARY KEY AUTOINCREMENT)",
        """
        CREATE TABLE IF NOT EXISTS trade_lifecycle_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT UNIQUE,
            signal_id TEXT,
            order_id TEXT,
            symbol TEXT,
            mode TEXT,
            trade_id TEXT,
            lifecycle_state TEXT,
            state TEXT,
            event_type TEXT,
            payload TEXT,
            decision TEXT,
            reject_reason TEXT,
            score REAL,
            rr REAL,
            effective_rr REAL,
            expectancy_bucket TEXT,
            execution_ctx TEXT,
            execution_ctx_missing INTEGER,
            event_ts TEXT,
            created_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS decision_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evidence_id TEXT UNIQUE,
            run_id TEXT,
            profile_id TEXT,
            profile_name TEXT,
            mode TEXT,
            timestamp TEXT,
            symbol TEXT,
            side TEXT,
            setup_type TEXT,
            setup_reason TEXT,
            regime TEXT,
            lifecycle_state_before TEXT,
            lifecycle_state_after TEXT,
            decision TEXT,
            score REAL,
            raw_rr REAL,
            effective_rr REAL,
            expectancy REAL,
            expectancy_bucket TEXT,
            reject_reason TEXT,
            cancel_reason TEXT,
            close_reason TEXT,
            entry REAL,
            sl REAL,
            tp REAL,
            trigger_price REAL,
            close_price REAL,
            net_pnl_pct REAL,
            net_pnl_usdt REAL,
            hold_minutes REAL,
            volume_24h_usdt REAL,
            spread_pct REAL,
            funding_rate_pct REAL,
            expected_slippage_pct REAL,
            liquidity_score REAL,
            volatility_regime TEXT,
            cost_penalty REAL,
            total_cost_pct REAL,
            spread_source TEXT,
            slippage_source TEXT,
            fee_pct REAL,
            fee_source TEXT,
            funding_source TEXT,
            latency_ms REAL,
            latency_source TEXT,
            liquidity_status TEXT,
            volatility_penalty_pct REAL,
            volatility_source TEXT,
            reject_flags TEXT,
            unavailable_fields TEXT,
            diagnostics_json TEXT,
            signal_id TEXT,
            order_id TEXT,
            position_id TEXT,
            lifecycle_id TEXT,
            lifecycle_seq INTEGER,
            created_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS closed_trade_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT, symbol TEXT, setup_type TEXT, regime TEXT, side TEXT, entry_price REAL, exit_price REAL,
            raw_rr REAL, effective_rr REAL, score REAL, net_pnl_pct REAL, fee_pct REAL, spread_pct REAL,
            expected_slippage_pct REAL, actual_slippage_pct REAL, liquidity_score REAL, volatility_regime TEXT,
            close_reason TEXT, tp_hit INTEGER, sl_hit INTEGER, hold_minutes REAL, created_at TEXT, payload_json TEXT,
            review_payload TEXT, execution_metrics TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS rejected_signal_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT, symbol TEXT, setup_type TEXT, regime TEXT, side TEXT, reject_reason TEXT, score REAL,
            raw_rr REAL, effective_rr REAL, expectancy_bucket TEXT, volume_24h_usdt REAL, spread_pct REAL,
            expected_slippage_pct REAL, funding_rate_pct REAL, liquidity_score REAL, volatility_regime TEXT,
            forward_window_bars INTEGER, would_have_hit_tp INTEGER, would_have_hit_sl INTEGER,
            max_favorable_excursion_pct REAL, max_adverse_excursion_pct REAL, reject_correct INTEGER,
            created_at TEXT, payload_json TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS adaptive_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_type TEXT NOT NULL, scope_key TEXT NOT NULL, sample_size INTEGER, win_rate REAL, avg_net_pnl_pct REAL,
            avg_effective_rr REAL, avg_spread_pct REAL, avg_slippage_pct REAL, reject_accuracy REAL, expectancy REAL,
            confidence REAL, updated_at TEXT, payload_json TEXT,
            UNIQUE(scope_type, scope_key)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS adaptive_threshold_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_type TEXT, scope_key TEXT, min_score REAL, min_effective_rr REAL, max_spread_pct REAL,
            max_expected_slippage_pct REAL, min_liquidity_score REAL, reason TEXT, source TEXT, created_at TEXT, payload_json TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS calibration_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT NOT NULL,
            predicted_quality REAL,
            realized_outcome TEXT,
            score REAL,
            rr REAL,
            effective_rr REAL,
            regime TEXT,
            setup_type TEXT,
            rejection_reason TEXT,
            forward_window_minutes INTEGER NOT NULL,
            mfe_pct REAL,
            mae_pct REAL,
            would_have_hit_tp INTEGER,
            would_have_hit_sl INTEGER,
            reject_correct INTEGER,
            created_at TEXT,
            UNIQUE(signal_id, forward_window_minutes)
        )
        """,
        "CREATE TABLE IF NOT EXISTS setup_expectancy_stats (setup TEXT PRIMARY KEY, samples INTEGER NOT NULL DEFAULT 0, win_count INTEGER NOT NULL DEFAULT 0, total_pnl REAL NOT NULL DEFAULT 0, expectancy REAL NOT NULL DEFAULT 0, updated_at TEXT)",
        "CREATE TABLE IF NOT EXISTS regime_expectancy_stats (regime TEXT PRIMARY KEY, samples INTEGER NOT NULL DEFAULT 0, win_count INTEGER NOT NULL DEFAULT 0, total_pnl REAL NOT NULL DEFAULT 0, expectancy REAL NOT NULL DEFAULT 0, updated_at TEXT)",
        "CREATE TABLE IF NOT EXISTS symbol_expectancy_stats (symbol TEXT PRIMARY KEY, samples INTEGER NOT NULL DEFAULT 0, win_count INTEGER NOT NULL DEFAULT 0, total_pnl REAL NOT NULL DEFAULT 0, expectancy REAL NOT NULL DEFAULT 0, updated_at TEXT)",
        *_timesfm_forecast_evidence_ddl(),

        "CREATE TABLE IF NOT EXISTS signal_id_state (id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL UNIQUE, last_signal_id TEXT, signal_id TEXT, symbol TEXT, timeframe TEXT, mode TEXT, created_at TEXT, updated_at TEXT)",
        "CREATE TABLE IF NOT EXISTS positions (id INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT UNIQUE, signal_id TEXT, symbol TEXT, timeframe TEXT, mode TEXT, side TEXT, qty REAL, entry_price REAL, status TEXT, created_at TEXT, updated_at TEXT)",
        "CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT UNIQUE, signal_id TEXT, position_id TEXT, symbol TEXT, timeframe TEXT, mode TEXT, side TEXT, status TEXT, created_at TEXT, updated_at TEXT)",
        "CREATE TABLE IF NOT EXISTS fills (id INTEGER PRIMARY KEY AUTOINCREMENT, fill_id TEXT UNIQUE, order_id TEXT, position_id TEXT, signal_id TEXT, symbol TEXT, side TEXT, qty REAL, price REAL, fee REAL, filled_at TEXT, created_at TEXT)",
        "CREATE TABLE IF NOT EXISTS paper_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE, signal_id TEXT, order_id TEXT, position_id TEXT, event_type TEXT, symbol TEXT, timeframe TEXT, mode TEXT, payload_json TEXT, created_at TEXT)",
        "CREATE TABLE IF NOT EXISTS backtest_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT UNIQUE, mode TEXT, started_at TEXT, completed_at TEXT, payload_json TEXT, created_at TEXT, updated_at TEXT)",
        "CREATE TABLE IF NOT EXISTS backtest_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE, run_id TEXT, signal_id TEXT, order_id TEXT, position_id TEXT, event_type TEXT, symbol TEXT, timeframe TEXT, mode TEXT, payload_json TEXT, created_at TEXT)",
        "CREATE TABLE IF NOT EXISTS symbol_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, symbol TEXT NOT NULL, timeframe TEXT, mode TEXT, snapshot_ts TEXT, payload_json TEXT, created_at TEXT)",
        "CREATE TABLE IF NOT EXISTS runtime_control_state (id INTEGER PRIMARY KEY CHECK (id = 1), mode_requested TEXT NOT NULL, mode_running TEXT, kill_switch_active INTEGER NOT NULL DEFAULT 0, kill_switch_source TEXT, kill_switch_updated_at TEXT, runtime_status TEXT NOT NULL, last_error TEXT, created_at TEXT, updated_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS calibration_labels (id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT, run_id TEXT, symbol TEXT, timeframe TEXT, mode TEXT, label TEXT, payload_json TEXT, created_at TEXT)",
        "CREATE TABLE IF NOT EXISTS optimizer_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT UNIQUE, status TEXT, payload_json TEXT, created_at TEXT, updated_at TEXT)",
        "CREATE TABLE IF NOT EXISTS cooldown_states (symbol TEXT PRIMARY KEY, cooldown_remaining_sec INTEGER NOT NULL DEFAULT 0)",
    ]
    with engine.begin() as conn:
        for statement in ddl:
            conn.execute(text(statement))
        _apply_sqlite_migrations(conn)
    return engine


def _sqlite_table_exists(conn: Any, table_name: str) -> bool:
    row = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name = :table_name"),
        {"table_name": table_name},
    ).first()
    return row is not None


def _sqlite_columns(conn: Any, table_name: str) -> set[str]:
    if not _sqlite_table_exists(conn, table_name):
        return set()
    rows = conn.execute(text(f"PRAGMA table_info({table_name})")).mappings().all()
    return {str(r.get("name")) for r in rows}


def _add_column_if_missing(conn: Any, table_name: str, column_name: str, ddl: str) -> None:
    cols = _sqlite_columns(conn, table_name)
    if column_name in cols:
        return
    conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {ddl}"))
    LOGGER.info("sqlite_schema_migration added column table=%s column=%s", table_name, column_name)


CORE_IDENTIFIER_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "signals": [("signal_id", "signal_id TEXT"), ("symbol", "symbol TEXT"), ("timeframe", "timeframe TEXT"), ("mode", "mode TEXT"), ("created_at", "created_at TEXT"), ("updated_at", "updated_at TEXT")],
    "order_decisions": [("decision_id", "decision_id TEXT"), ("signal_id", "signal_id TEXT"), ("symbol", "symbol TEXT"), ("timeframe", "timeframe TEXT"), ("mode", "mode TEXT"), ("created_at", "created_at TEXT"), ("updated_at", "updated_at TEXT")],
    "signal_id_state": [("signal_id", "signal_id TEXT"), ("symbol", "symbol TEXT"), ("timeframe", "timeframe TEXT"), ("mode", "mode TEXT"), ("created_at", "created_at TEXT"), ("updated_at", "updated_at TEXT")],
    "orders": [("order_id", "order_id TEXT"), ("signal_id", "signal_id TEXT"), ("position_id", "position_id TEXT"), ("symbol", "symbol TEXT"), ("timeframe", "timeframe TEXT"), ("mode", "mode TEXT"), ("created_at", "created_at TEXT"), ("updated_at", "updated_at TEXT")],
    "positions": [("position_id", "position_id TEXT"), ("signal_id", "signal_id TEXT"), ("symbol", "symbol TEXT"), ("timeframe", "timeframe TEXT"), ("mode", "mode TEXT"), ("created_at", "created_at TEXT"), ("updated_at", "updated_at TEXT")],
    "fills": [("order_id", "order_id TEXT"), ("position_id", "position_id TEXT"), ("signal_id", "signal_id TEXT"), ("symbol", "symbol TEXT"), ("created_at", "created_at TEXT")],
    "paper_events": [("event_id", "event_id TEXT"), ("signal_id", "signal_id TEXT"), ("order_id", "order_id TEXT"), ("position_id", "position_id TEXT"), ("symbol", "symbol TEXT"), ("timeframe", "timeframe TEXT"), ("mode", "mode TEXT"), ("created_at", "created_at TEXT")],
    "backtest_runs": [("run_id", "run_id TEXT"), ("mode", "mode TEXT"), ("created_at", "created_at TEXT"), ("updated_at", "updated_at TEXT")],
    "backtest_events": [("event_id", "event_id TEXT"), ("run_id", "run_id TEXT"), ("signal_id", "signal_id TEXT"), ("order_id", "order_id TEXT"), ("position_id", "position_id TEXT"), ("symbol", "symbol TEXT"), ("timeframe", "timeframe TEXT"), ("mode", "mode TEXT"), ("created_at", "created_at TEXT")],
    "symbol_snapshots": [("run_id", "run_id TEXT"), ("symbol", "symbol TEXT"), ("timeframe", "timeframe TEXT"), ("mode", "mode TEXT"), ("created_at", "created_at TEXT")],
    "timesfm_forecast_evidence": [("symbol", "symbol TEXT"), ("timeframe", "timeframe TEXT"), ("timestamp", "timestamp INTEGER"), ("created_at", "created_at TEXT")],
    "calibration_labels": [("signal_id", "signal_id TEXT"), ("run_id", "run_id TEXT"), ("symbol", "symbol TEXT"), ("timeframe", "timeframe TEXT"), ("mode", "mode TEXT"), ("created_at", "created_at TEXT")],
    "optimizer_runs": [("run_id", "run_id TEXT"), ("created_at", "created_at TEXT"), ("updated_at", "updated_at TEXT")],
}

CORE_IDENTIFIER_INDEXES = [
    ("ix_signals_signal_id", "signals", "signal_id"),
    ("ix_order_decisions_decision_id", "order_decisions", "decision_id"),
    ("ix_order_decisions_signal_id", "order_decisions", "signal_id"),
    ("ix_orders_order_id", "orders", "order_id"),
    ("ix_orders_signal_id", "orders", "signal_id"),
    ("ix_orders_position_id", "orders", "position_id"),
    ("ix_positions_position_id", "positions", "position_id"),
    ("ix_positions_signal_id", "positions", "signal_id"),
    ("ix_fills_order_id", "fills", "order_id"),
    ("ix_fills_position_id", "fills", "position_id"),
    ("ix_paper_events_signal_id", "paper_events", "signal_id"),
    ("ix_paper_events_position_id", "paper_events", "position_id"),
    ("ix_backtest_events_run_id", "backtest_events", "run_id"),
    ("ix_backtest_events_signal_id", "backtest_events", "signal_id"),
    ("ix_calibration_labels_signal_id", "calibration_labels", "signal_id"),
    ("ix_optimizer_runs_run_id", "optimizer_runs", "run_id"),
]


def _ensure_core_identifier_schema(conn: Any) -> None:
    for table_name, columns in CORE_IDENTIFIER_COLUMNS.items():
        if not _sqlite_table_exists(conn, table_name):
            continue
        for column_name, ddl in columns:
            _add_column_if_missing(conn, table_name, column_name, ddl)
    for index_name, table_name, column_name in CORE_IDENTIFIER_INDEXES:
        if table_name in CORE_IDENTIFIER_COLUMNS and column_name in _sqlite_columns(conn, table_name):
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name}({column_name})"))


def _ensure_sqlite_runtime_schema(conn: Any) -> None:
    required_columns: dict[str, list[tuple[str, str]]] = {
        "order_decisions": [
            ("decision_id", "decision_id TEXT"),
            ("signal_id", "signal_id TEXT"),
            ("order_id", "order_id TEXT"),
            ("symbol", "symbol TEXT"),
            ("mode", "mode TEXT"),
            ("phase", "phase TEXT"),
            ("decision", "decision TEXT"),
            ("order_type", "order_type TEXT"),
            ("confidence", "confidence REAL"),
            ("explanation", "explanation TEXT"),
            ("reject_reason", "reject_reason TEXT"),
            ("score", "score REAL"),
            ("rr", "rr REAL"),
            ("effective_rr", "effective_rr REAL DEFAULT 0.0"),
            ("expectancy_bucket", "expectancy_bucket TEXT"),
            ("payload", "payload TEXT"),
            ("order_payload", "order_payload TEXT"),
            ("execution_ctx", "execution_ctx TEXT"),
            ("execution_ctx_missing", "execution_ctx_missing INTEGER"),
            ("expected_slippage_pct", "expected_slippage_pct REAL DEFAULT 0.0"),
            ("spread_pct", "spread_pct REAL DEFAULT 0.0"),
            ("latency_ms", "latency_ms REAL DEFAULT 0.0"),
            ("orderbook_imbalance", "orderbook_imbalance REAL DEFAULT 0.0"),
            ("funding_rate_pct", "funding_rate_pct REAL DEFAULT 0.0"),
            ("execution_regime", "execution_regime TEXT"),
            ("volatility_regime", "volatility_regime TEXT"),
            ("input_snapshot_hash", "input_snapshot_hash TEXT"),
            ("no_submit_verified", "no_submit_verified INTEGER"),
            ("parity_result", "parity_result TEXT"),
            ("created_at", "created_at TEXT"),
            ("updated_at", "updated_at TEXT"),
        ],
        "ai_decision_features": [
            ("decision_id", "decision_id TEXT"),
            ("features", "features TEXT"),
            ("penalties", "penalties TEXT"),
            ("reason_flags", "reason_flags TEXT"),
            ("execution_features", "execution_features TEXT"),
            ("created_at", "created_at TEXT"),
        ],
        "trade_lifecycle_events": [
            ("event_id", "event_id TEXT"),
            ("signal_id", "signal_id TEXT"),
            ("order_id", "order_id TEXT"),
            ("symbol", "symbol TEXT"),
            ("mode", "mode TEXT"),
            ("trade_id", "trade_id TEXT"),
            ("state", "state TEXT"),
            ("event_type", "event_type TEXT"),
            ("payload", "payload TEXT"),
            ("lifecycle_seq", "lifecycle_seq INTEGER"),
            ("cancel_reason", "cancel_reason TEXT"),
            ("lifecycle_id", "lifecycle_id TEXT"),
            ("failure_reason", "failure_reason TEXT"),
            ("reconciliation_reason", "reconciliation_reason TEXT"),
            ("incident_payload", "incident_payload TEXT"),
        ],
        "closed_trade_reviews": [
            ("execution_metrics", "execution_metrics TEXT"),
            ("review_payload", "review_payload TEXT"),
        ],
        "timesfm_forecast_evidence": [
            ("forecast_timestamp", "forecast_timestamp TEXT"),
            ("point_forecast", "point_forecast REAL"),
            ("quantiles_json", "quantiles_json TEXT"),
        ],
        "decision_evidence": [
            ("evidence_id", "evidence_id TEXT"), ("run_id", "run_id TEXT"), ("profile_id", "profile_id TEXT"),
            ("profile_name", "profile_name TEXT"), ("mode", "mode TEXT"), ("timestamp", "timestamp TEXT"),
            ("symbol", "symbol TEXT"), ("side", "side TEXT"), ("setup_type", "setup_type TEXT"),
            ("setup_reason", "setup_reason TEXT"), ("regime", "regime TEXT"),
            ("lifecycle_state_before", "lifecycle_state_before TEXT"), ("lifecycle_state_after", "lifecycle_state_after TEXT"),
            ("decision", "decision TEXT"), ("score", "score REAL"), ("raw_rr", "raw_rr REAL"),
            ("effective_rr", "effective_rr REAL"), ("expectancy", "expectancy REAL"), ("expectancy_bucket", "expectancy_bucket TEXT"),
            ("reject_reason", "reject_reason TEXT"), ("cancel_reason", "cancel_reason TEXT"), ("close_reason", "close_reason TEXT"),
            ("entry", "entry REAL"), ("sl", "sl REAL"), ("tp", "tp REAL"), ("trigger_price", "trigger_price REAL"),
            ("close_price", "close_price REAL"), ("net_pnl_pct", "net_pnl_pct REAL"), ("net_pnl_usdt", "net_pnl_usdt REAL"),
            ("hold_minutes", "hold_minutes REAL"), ("volume_24h_usdt", "volume_24h_usdt REAL"), ("spread_pct", "spread_pct REAL"),
            ("funding_rate_pct", "funding_rate_pct REAL"), ("expected_slippage_pct", "expected_slippage_pct REAL"),
            ("liquidity_score", "liquidity_score REAL"), ("volatility_regime", "volatility_regime TEXT"),
            ("cost_penalty", "cost_penalty REAL"), ("total_cost_pct", "total_cost_pct REAL"),
            ("spread_source", "spread_source TEXT"), ("slippage_source", "slippage_source TEXT"),
            ("fee_pct", "fee_pct REAL"), ("fee_source", "fee_source TEXT"),
            ("funding_source", "funding_source TEXT"), ("latency_ms", "latency_ms REAL"),
            ("latency_source", "latency_source TEXT"), ("liquidity_status", "liquidity_status TEXT"),
            ("volatility_penalty_pct", "volatility_penalty_pct REAL"), ("volatility_source", "volatility_source TEXT"),
            ("reject_flags", "reject_flags TEXT"), ("unavailable_fields", "unavailable_fields TEXT"),
            ("diagnostics_json", "diagnostics_json TEXT"),
            ("signal_id", "signal_id TEXT"), ("order_id", "order_id TEXT"), ("position_id", "position_id TEXT"),
            ("lifecycle_id", "lifecycle_id TEXT"), ("lifecycle_seq", "lifecycle_seq INTEGER"), ("created_at", "created_at TEXT"),
        ],
    }
    for table_name, columns in required_columns.items():
        if not _sqlite_table_exists(conn, table_name):
            continue
        for column_name, ddl in columns:
            _add_column_if_missing(conn, table_name, column_name, ddl)


def _ensure_sqlite_schema_migrations_table(conn: Any) -> None:
    """Bootstrap SQLite migration bookkeeping before reading applied versions.

    This must stay ahead of any SELECT from schema_migrations so fresh SQLite
    databases and partial legacy databases can enter the normal idempotent
    migration path without dropping or recreating runtime/audit tables.
    """
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL,
                notes TEXT
            )
            """
        )
    )


def _ensure_sqlite_rollback_evidence_schema(conn: Any) -> None:
    """Create the rollback validation evidence table for fresh SQLite DBs.

    The canonical rollback evidence surface is
    ``live_rollback_validation_evidence`` (see ``alphaforge.rollback_evidence``).
    Keep this idempotent and additive so runtime/audit rows are preserved across
    repeated bootstraps and legacy database repairs.
    """
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS live_rollback_validation_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                validation_id TEXT NOT NULL UNIQUE,
                recorded_at TEXT NOT NULL,
                evidence_status TEXT NOT NULL,
                rollback_evidence_source TEXT NOT NULL,
                kill_switch_block_verified INTEGER NOT NULL,
                no_submit_on_kill_switch_verified INTEGER NOT NULL,
                fail_closed_reconciliation_verified INTEGER NOT NULL,
                repair_actions_non_mutating_verified INTEGER NOT NULL,
                execution_mutation_attempt_count INTEGER NOT NULL,
                blocking_reasons TEXT NOT NULL,
                evidence_payload TEXT NOT NULL
            )
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_live_rollback_validation_recorded_at
            ON live_rollback_validation_evidence(recorded_at DESC, id DESC)
            """
        )
    )


def _apply_sqlite_migrations(conn: Any) -> None:
    _ensure_sqlite_schema_migrations_table(conn)
    existing = {str(r[0]) for r in conn.execute(text("SELECT version FROM schema_migrations")).all()}
    migrations: list[tuple[str, str]] = [
        ("2026_05_16_persistence_integrity_v1", "Backfill missing persistence columns and normalize legacy execution_ctx_missing semantics."),
        ("2026_06_19_rollback_evidence_bootstrap", "Ensure fresh SQLite bootstrap creates canonical live rollback validation evidence table."),
        ("2026_06_21_timesfm_canonical_evidence", "Add canonical TimesFM forecast evidence and optional forward outcome labels tables."),
        ("2026_06_23_core_identifier_normalization", "Add normalized lifecycle identifier columns and safe join indexes."),
        ("2026_07_06_phase2_decision_evidence", "Add SQL-backed decision evidence export surface for lifecycle/dashboard reconciliation."),
    ]
    _ensure_sqlite_rollback_evidence_schema(conn)
    _ensure_core_identifier_schema(conn)
    _ensure_sqlite_runtime_schema(conn)
    signal_cols = _sqlite_columns(conn, "signals")
    if "signal_id" in signal_cols and "uq_signals_signal_id_not_null" not in existing:
        conn.execute(text("UPDATE signals SET signal_id = 'legacy-signal-' || id WHERE signal_id IS NULL OR TRIM(signal_id) = ''"))
    decision_cols = _sqlite_columns(conn, "order_decisions")
    if "execution_ctx_missing" in decision_cols:
        conn.execute(text("""
            UPDATE order_decisions
            SET execution_ctx_missing =
                CASE
                    WHEN LOWER(TRIM(CAST(execution_ctx_missing AS TEXT))) IN ('1','true','t','yes','y') THEN 1
                    ELSE 0
                END
            WHERE execution_ctx_missing IS NOT NULL
        """))
    lifecycle_cols = _sqlite_columns(conn, "trade_lifecycle_events")
    if "execution_ctx_missing" in lifecycle_cols:
        conn.execute(text("""
            UPDATE trade_lifecycle_events
            SET execution_ctx_missing =
                CASE
                    WHEN LOWER(TRIM(CAST(execution_ctx_missing AS TEXT))) IN ('1','true','t','yes','y') THEN 1
                    ELSE 0
                END
            WHERE execution_ctx_missing IS NOT NULL
        """))
    if lifecycle_cols and "event_type" not in lifecycle_cols:
        conn.execute(text("ALTER TABLE trade_lifecycle_events ADD COLUMN event_type TEXT"))
    if lifecycle_cols and "payload" not in lifecycle_cols:
        conn.execute(text("ALTER TABLE trade_lifecycle_events ADD COLUMN payload TEXT"))
    if lifecycle_cols and "lifecycle_seq" not in lifecycle_cols:
        conn.execute(text("ALTER TABLE trade_lifecycle_events ADD COLUMN lifecycle_seq INTEGER"))
    if lifecycle_cols and "cancel_reason" not in lifecycle_cols:
        conn.execute(text("ALTER TABLE trade_lifecycle_events ADD COLUMN cancel_reason TEXT"))
    if lifecycle_cols and "lifecycle_id" not in lifecycle_cols:
        conn.execute(text("ALTER TABLE trade_lifecycle_events ADD COLUMN lifecycle_id TEXT"))
    if lifecycle_cols and "failure_reason" not in lifecycle_cols:
        conn.execute(text("ALTER TABLE trade_lifecycle_events ADD COLUMN failure_reason TEXT"))
    if lifecycle_cols and "reconciliation_reason" not in lifecycle_cols:
        conn.execute(text("ALTER TABLE trade_lifecycle_events ADD COLUMN reconciliation_reason TEXT"))
    if lifecycle_cols and "incident_payload" not in lifecycle_cols:
        conn.execute(text("ALTER TABLE trade_lifecycle_events ADD COLUMN incident_payload TEXT"))
    closed_trade_cols = _sqlite_columns(conn, "closed_trade_reviews")
    if closed_trade_cols and "execution_metrics" not in closed_trade_cols:
        conn.execute(text("ALTER TABLE closed_trade_reviews ADD COLUMN execution_metrics TEXT"))
    lifecycle_cols = _sqlite_columns(conn, "trade_lifecycle_events")
    if {"signal_id", "event_ts", "lifecycle_state"}.issubset(lifecycle_cols):
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_lifecycle_signal_event_ts_state ON trade_lifecycle_events(signal_id, event_ts, lifecycle_state)"))
    for version, notes in migrations:
        if version not in existing:
            conn.execute(text("INSERT INTO schema_migrations(version, applied_at, notes) VALUES (:v, :at, :n)"), {"v": version, "at": _utc_now_iso(), "n": notes})


def fetch_expectancy_stat_detail(session: Any, table_name: str, key_column: str, key_value: str) -> dict[str, Any] | None:
    default = {"expectancy_bucket": "UNKNOWN", "sample_size": 0, "win_rate": None, "avg_rr": None, "expectancy": None}
    if session is None:
        return None
    try:
        row = session.execute(text(f"SELECT * FROM {table_name} WHERE {key_column} = :key_value LIMIT 1"), {"key_value": key_value}).fetchone()
    except Exception:
        return None
    if not row:
        return None
    row_data = dict(row) if isinstance(row, Mapping) else dict(row._mapping)
    detail = {
        "expectancy_bucket": row_data.get("expectancy_bucket") or "UNKNOWN",
        "sample_size": int(row_data.get("sample_size") or row_data.get("samples") or 0),
        "win_rate": row_data.get("win_rate"),
        "avg_rr": row_data.get("avg_rr"),
        "expectancy": row_data.get("expectancy") if row_data.get("expectancy") is not None else row_data.get("total_pnl"),
    }
    detail["expectancy"] = row_data.get("expectancy")
    return detail


def fetch_expectancy_stat(session: Any, table_name: str, key_column: str, key_value: str) -> float | None:
    detail = fetch_expectancy_stat_detail(session, table_name, key_column, key_value)
    if detail is None:
        return None
    expectancy = detail.get("expectancy")
    if expectancy is None:
        return None
    try:
        return float(expectancy)
    except (TypeError, ValueError):
        return None



def save_timesfm_forecast_evidence(session: Any, **evidence: Any) -> str | None:
    """Persist one TimesFM forecast evidence row without creating an order path.

    TimesFM rows are research evidence only. Accepted-looking LONG/SHORT sides are
    not submitted to order_decisions and invalid forecasts remain NO_TRADE with
    INVALID_FORECAST or another explicit rejection reason.
    """
    if session is None:
        return None
    now = _utc_now_iso()
    forecast_id = evidence.get("forecast_id")
    if not forecast_id:
        return None
    payload = dict(evidence)
    try:
        session.execute(text("""
            INSERT INTO timesfm_forecast_evidence (
                forecast_id, timestamp, symbol, timeframe, horizon, current_price, forecast_p10, forecast_p50, forecast_p90,
                side, expected_rr, rejection_reason, mode, model_provider, model_name, model_version, no_lookahead_input_end_ts,
                payload_json, created_at, updated_at
            ) VALUES (
                :forecast_id, :timestamp, :symbol, :timeframe, :horizon, :current_price, :forecast_p10, :forecast_p50, :forecast_p90,
                :side, :expected_rr, :rejection_reason, :mode, :model_provider, :model_name, :model_version, :no_lookahead_input_end_ts,
                :payload_json, :created_at, :updated_at
            )
            ON CONFLICT(forecast_id) DO UPDATE SET
                timestamp=excluded.timestamp, symbol=excluded.symbol, timeframe=excluded.timeframe, horizon=excluded.horizon,
                current_price=excluded.current_price, forecast_p10=excluded.forecast_p10, forecast_p50=excluded.forecast_p50, forecast_p90=excluded.forecast_p90,
                side=excluded.side, expected_rr=excluded.expected_rr, rejection_reason=excluded.rejection_reason, mode=excluded.mode,
                model_provider=excluded.model_provider, model_name=excluded.model_name, model_version=excluded.model_version,
                no_lookahead_input_end_ts=excluded.no_lookahead_input_end_ts, payload_json=excluded.payload_json, updated_at=excluded.updated_at
        """), {
            "forecast_id": forecast_id, "timestamp": evidence.get("timestamp"), "symbol": evidence.get("symbol"),
            "timeframe": evidence.get("timeframe"), "horizon": evidence.get("horizon"), "current_price": evidence.get("current_price"),
            "forecast_p10": evidence.get("forecast_p10"), "forecast_p50": evidence.get("forecast_p50"), "forecast_p90": evidence.get("forecast_p90"),
            "side": evidence.get("side"), "expected_rr": evidence.get("expected_rr"), "rejection_reason": evidence.get("rejection_reason"),
            "mode": evidence.get("mode"), "model_provider": evidence.get("model_provider"), "model_name": evidence.get("model_name"),
            "model_version": evidence.get("model_version"), "no_lookahead_input_end_ts": evidence.get("no_lookahead_input_end_ts"),
            "payload_json": json.dumps(payload), "created_at": now, "updated_at": now,
        })
        if hasattr(session, "commit"):
            session.commit()
        return str(forecast_id)
    except Exception:
        return None


def save_ai_decision_features(*args, execution_features=None, **kwargs):
    payload = execution_features
    if payload is None and kwargs:
        payload = kwargs.get("execution_features", kwargs)
    try:
        return json.dumps(payload)
    except Exception:
        return None


def save_signal(session: Any, **signal: Any) -> Any:
    if session is None:
        return None
    now = _utc_now_iso()
    signal_id = signal.get("signal_id") or signal.get("id") or f"{signal.get('symbol', 'UNKNOWN')}:{now}"
    try:
        row = session.execute(text("""
            INSERT INTO signals (signal_id, symbol, side, timeframe, mode, score, rr, effective_rr, expectancy_bucket, created_at, updated_at)
            VALUES (:signal_id, :symbol, :side, :timeframe, :mode, :score, :rr, :effective_rr, :expectancy_bucket, :created_at, :updated_at)
            ON CONFLICT(signal_id) DO UPDATE SET
              symbol=excluded.symbol, side=excluded.side, timeframe=excluded.timeframe, mode=excluded.mode,
              score=excluded.score, rr=excluded.rr, effective_rr=excluded.effective_rr, expectancy_bucket=excluded.expectancy_bucket,
              updated_at=excluded.updated_at
        """), {
            "signal_id": signal_id, "symbol": signal.get("symbol"), "side": signal.get("side"), "timeframe": signal.get("timeframe"),
            "mode": signal.get("mode"), "score": signal.get("score"), "rr": signal.get("rr"), "effective_rr": signal.get("effective_rr"),
            "expectancy_bucket": signal.get("expectancy_bucket"), "created_at": now, "updated_at": now,
        })
        if hasattr(session, "commit"):
            session.commit()
        return signal_id or row.lastrowid
    except Exception:
        return signal.get("id")


def save_order_decision(session: Any, **decision: Any) -> Any:
    if session is None:
        return None
    now = _utc_now_iso()
    decision_id = decision.get("decision_id") or decision.get("id") or f"{decision.get('signal_id', 'UNKNOWN')}:{now}:{decision.get('decision', 'UNKNOWN')}"
    execution_ctx = decision.get("execution_ctx", {}) or {}
    payload = {
        "decision_id": decision_id, "signal_id": decision.get("signal_id"), "order_id": decision.get("order_id"),
        "symbol": decision.get("symbol"), "mode": decision.get("mode"), "decision": decision.get("decision"),
        "reject_reason": canonical_reject_reason(decision.get("reject_reason")) if str(decision.get("decision", "")).upper() == "REJECTED" else decision.get("reject_reason"), "score": decision.get("score"), "rr": decision.get("rr"),
        "effective_rr": decision.get("effective_rr"), "expectancy_bucket": decision.get("expectancy_bucket"),
        "execution_ctx": json.dumps(execution_ctx),
        "execution_ctx_missing": 1 if bool(decision.get("execution_ctx_missing", execution_ctx.get("evidence_status") in {"UNAVAILABLE", None})) else 0,
        "created_at": now, "updated_at": now,
    }
    payload_obj = decision.get("order_payload")
    if payload_obj is None:
        payload_obj = {"reject_reason": payload["reject_reason"]} if str(decision.get("decision", "")).upper() == "REJECTED" else {}
    try:
        row = session.execute(text("""
        INSERT INTO order_decisions (
            decision_id, signal_id, order_id, symbol, mode, phase, decision, order_type, confidence, explanation,
            reject_reason, score, rr, effective_rr, expectancy_bucket, order_payload, payload, execution_ctx,
            execution_ctx_missing, expected_slippage_pct, spread_pct, latency_ms, orderbook_imbalance,
            funding_rate_pct, execution_regime, volatility_regime, input_snapshot_hash, no_submit_verified, parity_result, created_at, updated_at
        ) VALUES (
            :decision_id, :signal_id, :order_id, :symbol, :mode, :phase, :decision, :order_type, :confidence, :explanation,
            :reject_reason, :score, :rr, :effective_rr, :expectancy_bucket, :order_payload, :payload, :execution_ctx,
            :execution_ctx_missing, :expected_slippage_pct, :spread_pct, :latency_ms, :orderbook_imbalance,
            :funding_rate_pct, :execution_regime, :volatility_regime, :input_snapshot_hash, :no_submit_verified, :parity_result, :created_at, :updated_at
        )
        ON CONFLICT(decision_id) DO UPDATE SET
            signal_id=excluded.signal_id, order_id=excluded.order_id, symbol=excluded.symbol, mode=excluded.mode,
            phase=excluded.phase, decision=excluded.decision, order_type=excluded.order_type, confidence=excluded.confidence,
            explanation=excluded.explanation, reject_reason=excluded.reject_reason, score=excluded.score, rr=excluded.rr,
            effective_rr=excluded.effective_rr, expectancy_bucket=excluded.expectancy_bucket, order_payload=excluded.order_payload, payload=excluded.payload,
            execution_ctx=excluded.execution_ctx, execution_ctx_missing=excluded.execution_ctx_missing,
            expected_slippage_pct=excluded.expected_slippage_pct, spread_pct=excluded.spread_pct, latency_ms=excluded.latency_ms,
            orderbook_imbalance=excluded.orderbook_imbalance, funding_rate_pct=excluded.funding_rate_pct,
            execution_regime=excluded.execution_regime, volatility_regime=excluded.volatility_regime,
            input_snapshot_hash=excluded.input_snapshot_hash, no_submit_verified=excluded.no_submit_verified,
            parity_result=excluded.parity_result, updated_at=excluded.updated_at
    """), {
        "decision_id": decision_id, "signal_id": decision.get("signal_id"), "order_id": decision.get("order_id"),
        "symbol": decision.get("symbol"), "mode": decision.get("mode"), "decision": decision.get("decision"),
        "reject_reason": canonical_reject_reason(decision.get("reject_reason")) if str(decision.get("decision", "")).upper() == "REJECTED" else decision.get("reject_reason"), "score": decision.get("score"), "rr": decision.get("rr"),
        "effective_rr": decision.get("effective_rr"), "expectancy_bucket": decision.get("expectancy_bucket"),
        "phase": decision.get("phase"), "order_type": decision.get("order_type"), "confidence": decision.get("confidence") or decision.get("score"),
        "explanation": decision.get("explanation"), "order_payload": json.dumps(payload_obj), "payload": json.dumps(payload_obj),
        "execution_ctx": json.dumps(execution_ctx),
        "expected_slippage_pct": decision.get("expected_slippage_pct"), "spread_pct": decision.get("spread_pct"),
        "latency_ms": decision.get("latency_ms"), "orderbook_imbalance": decision.get("orderbook_imbalance"),
        "funding_rate_pct": decision.get("funding_rate_pct"), "execution_regime": decision.get("execution_regime"),
        "volatility_regime": decision.get("volatility_regime"),
        "input_snapshot_hash": decision.get("input_snapshot_hash"),
        "no_submit_verified": 1 if bool(decision.get("no_submit_verified", False)) else 0,
        "parity_result": decision.get("parity_result"),
        "execution_ctx_missing": 1 if bool(decision.get("execution_ctx_missing", execution_ctx.get("evidence_status") in {"UNAVAILABLE", None})) else 0,
        "created_at": now, "updated_at": now,
    })
        if hasattr(session, "commit"):
            session.commit()
        return decision_id or row.lastrowid
    except Exception:
        return None


def save_rejected_decision_artifact(session: Any, **artifact: Any) -> dict[str, Any] | None:
    """Persist a rejected signal/order as one auditable SQL artifact.

    The helper intentionally writes the signal, order_decision, and lifecycle
    row together with the same stable signal_id and canonical reject reason so
    BACKTEST/PAPER callers do not accidentally return early with only partial
    rejection evidence.
    """
    if session is None:
        return None
    reason = canonical_reject_reason(artifact.get("reject_reason") or artifact.get("reason"))
    if reason == "UNKNOWN":
        return None
    now = canonical_utc_timestamp(artifact.get("event_ts"))
    signal_id = str(
        artifact.get("signal_id")
        or f"{artifact.get('mode', 'UNKNOWN')}:{artifact.get('symbol', 'UNKNOWN')}:{now}:{reason}"
    )
    raw_rr = artifact.get("raw_rr", artifact.get("rr"))
    effective_rr = artifact.get("effective_rr")
    if effective_rr is None:
        effective_rr = raw_rr
    execution_ctx = dict(artifact.get("execution_ctx") or {})
    execution_ctx_missing = bool(
        artifact.get(
            "execution_ctx_missing",
            execution_ctx.get("evidence_status") in {"UNAVAILABLE", "UNKNOWN", None},
        )
    )
    signal_payload = {
        "signal_id": signal_id,
        "symbol": artifact.get("symbol"),
        "side": artifact.get("side"),
        "timeframe": artifact.get("timeframe"),
        "mode": artifact.get("mode"),
        "score": artifact.get("score"),
        "rr": raw_rr,
        "effective_rr": effective_rr,
        "expectancy_bucket": artifact.get("expectancy_bucket"),
    }
    save_signal(session, **signal_payload)
    decision_id = save_order_decision(
        session,
        decision_id=artifact.get("decision_id") or f"{signal_id}:REJECTED",
        signal_id=signal_id,
        order_id=artifact.get("order_id"),
        symbol=artifact.get("symbol"),
        mode=artifact.get("mode"),
        phase=artifact.get("phase", "final"),
        decision="REJECTED",
        reject_reason=reason,
        score=artifact.get("score"),
        rr=raw_rr,
        effective_rr=effective_rr,
        expectancy_bucket=artifact.get("expectancy_bucket"),
        order_payload=artifact.get("order_payload"),
        explanation=artifact.get("explanation"),
        execution_ctx=execution_ctx,
        execution_ctx_missing=execution_ctx_missing,
        expected_slippage_pct=artifact.get("expected_slippage_pct", execution_ctx.get("expected_slippage_pct")),
        spread_pct=artifact.get("spread_pct", execution_ctx.get("spread_pct")),
        latency_ms=artifact.get("latency_ms", execution_ctx.get("market_data_latency_ms") or execution_ctx.get("latency_ms")),
        orderbook_imbalance=artifact.get("orderbook_imbalance", execution_ctx.get("orderbook_imbalance")),
        funding_rate_pct=artifact.get("funding_rate_pct", execution_ctx.get("funding_rate_pct")),
        volatility_regime=artifact.get("volatility_regime", execution_ctx.get("volatility_regime")),
    )
    lifecycle_ok = save_trade_lifecycle_event(
        session,
        event_id=artifact.get("event_id") or f"{signal_id}:SIGNAL_REJECTED",
        signal_id=signal_id,
        order_id=artifact.get("order_id"),
        symbol=artifact.get("symbol"),
        mode=artifact.get("mode"),
        lifecycle_state=artifact.get("lifecycle_state", "SIGNAL_REJECTED"),
        decision="REJECTED",
        reject_reason=reason,
        score=artifact.get("score"),
        rr=raw_rr,
        effective_rr=effective_rr,
        expectancy_bucket=artifact.get("expectancy_bucket"),
        execution_ctx=execution_ctx,
        execution_ctx_missing=execution_ctx_missing,
        event_ts=now,
        lifecycle_seq=artifact.get("lifecycle_seq"),
        lifecycle_id=artifact.get("lifecycle_id") or f"{signal_id}:reject",
        payload={"reject_reason": reason, **dict(artifact.get("payload") or {})},
    )
    if not decision_id or not lifecycle_ok:
        return None
    return {"signal_id": signal_id, "decision_id": decision_id, "reject_reason": reason}


def save_trade_lifecycle_event(session: Any, **event: Any) -> Any:
    if session is None:
        return False
    now = _utc_now_iso()
    event_id = event.get("event_id") or event.get("id") or f"{event.get('symbol', 'UNKNOWN')}:{canonical_utc_timestamp(event.get('event_ts'))}:{event.get('lifecycle_state') or event.get('state') or 'UNKNOWN'}"
    signal_id = event.get("signal_id") or f"UNKNOWN_SIGNAL:{event.get('symbol', 'UNKNOWN')}:{canonical_utc_timestamp(event.get('event_ts'))}"
    raw_lifecycle_state = event.get("lifecycle_state") or event.get("state")
    try:
        lifecycle_state = normalize_lifecycle_event(raw_lifecycle_state)
    except ValueError:
        return None
    prev_state = event.get("previous_lifecycle_state")
    is_valid = validate_transition(prev_state, lifecycle_state) if lifecycle_state else False
    if not is_valid and prev_state is not None:
        return None
    payload = {
        "event_id": event_id, "signal_id": signal_id, "order_id": event.get("order_id"), "symbol": event.get("symbol"),
        "mode": event.get("mode"), "lifecycle_state": lifecycle_state, "decision": event.get("decision"),
        "reject_reason": canonical_reject_reason(event.get("reject_reason")), "score": event.get("score"), "rr": event.get("rr"), "effective_rr": event.get("effective_rr"),
        "expectancy_bucket": event.get("expectancy_bucket"), "execution_ctx": json.dumps(event.get("execution_ctx", {})),
        "execution_ctx_missing": 1 if bool(event.get("execution_ctx_missing", (event.get("execution_ctx") or {}).get("evidence_status") in {"UNAVAILABLE", "UNKNOWN", None})) else 0, "event_ts": canonical_utc_timestamp(event.get("event_ts")), "created_at": now,
        "lifecycle_seq": event.get("lifecycle_seq"),
        "cancel_reason": event.get("cancel_reason"),
        "lifecycle_id": event.get("lifecycle_id") or f"{signal_id}:{canonical_utc_timestamp(event.get('event_ts'))}:{lifecycle_state}",
        "failure_reason": event.get("failure_reason"),
        "reconciliation_reason": event.get("reconciliation_reason"),
        "incident_payload": json.dumps(event.get("incident_payload", {})),
        "trade_id": event.get("trade_id") or signal_id,
        "state": lifecycle_state or event.get("event_type"),
        "event_type": event.get("event_type") or lifecycle_state,
        "payload": json.dumps(event.get("payload", {})),
    }
    statement_by_event_id = text("""
        INSERT INTO trade_lifecycle_events (
            event_id, signal_id, trade_id, order_id, symbol, mode, lifecycle_state, state, event_type, payload, decision, reject_reason, score, rr,
            effective_rr, expectancy_bucket, execution_ctx, execution_ctx_missing, event_ts, created_at, lifecycle_seq, cancel_reason, lifecycle_id, failure_reason, reconciliation_reason, incident_payload
        ) VALUES (
            :event_id, :signal_id, :trade_id, :order_id, :symbol, :mode, :lifecycle_state, :state, :event_type, :payload, :decision, :reject_reason, :score, :rr,
            :effective_rr, :expectancy_bucket, :execution_ctx, :execution_ctx_missing, :event_ts, :created_at, :lifecycle_seq, :cancel_reason, :lifecycle_id, :failure_reason, :reconciliation_reason, :incident_payload
        )
        ON CONFLICT(event_id) DO UPDATE SET
            signal_id=excluded.signal_id, order_id=excluded.order_id, symbol=excluded.symbol, mode=excluded.mode,
            lifecycle_state=excluded.lifecycle_state, state=excluded.state, event_type=excluded.event_type, payload=excluded.payload, decision=excluded.decision, reject_reason=excluded.reject_reason,
            score=excluded.score, rr=excluded.rr, effective_rr=excluded.effective_rr, expectancy_bucket=excluded.expectancy_bucket,
            execution_ctx=excluded.execution_ctx, execution_ctx_missing=excluded.execution_ctx_missing, event_ts=excluded.event_ts,
            lifecycle_seq=excluded.lifecycle_seq, cancel_reason=excluded.cancel_reason, lifecycle_id=excluded.lifecycle_id, failure_reason=excluded.failure_reason, reconciliation_reason=excluded.reconciliation_reason, incident_payload=excluded.incident_payload
    """)
    statement_by_lifecycle_key = text("""
        INSERT INTO trade_lifecycle_events (
            event_id, signal_id, trade_id, order_id, symbol, mode, lifecycle_state, state, event_type, payload, decision, reject_reason, score, rr,
            effective_rr, expectancy_bucket, execution_ctx, execution_ctx_missing, event_ts, created_at, lifecycle_seq, cancel_reason, lifecycle_id, failure_reason, reconciliation_reason, incident_payload
        ) VALUES (
            :event_id, :signal_id, :trade_id, :order_id, :symbol, :mode, :lifecycle_state, :state, :event_type, :payload, :decision, :reject_reason, :score, :rr,
            :effective_rr, :expectancy_bucket, :execution_ctx, :execution_ctx_missing, :event_ts, :created_at, :lifecycle_seq, :cancel_reason, :lifecycle_id, :failure_reason, :reconciliation_reason, :incident_payload
        )
        ON CONFLICT(signal_id, event_ts, lifecycle_state) DO UPDATE SET
            event_id=excluded.event_id, order_id=excluded.order_id, symbol=excluded.symbol, mode=excluded.mode,
            state=excluded.state, event_type=excluded.event_type, payload=excluded.payload, decision=excluded.decision, reject_reason=excluded.reject_reason, score=excluded.score, rr=excluded.rr,
            effective_rr=excluded.effective_rr, expectancy_bucket=excluded.expectancy_bucket, execution_ctx=excluded.execution_ctx,
            execution_ctx_missing=excluded.execution_ctx_missing, lifecycle_seq=excluded.lifecycle_seq, cancel_reason=excluded.cancel_reason,
            lifecycle_id=excluded.lifecycle_id, failure_reason=excluded.failure_reason, reconciliation_reason=excluded.reconciliation_reason,
            incident_payload=excluded.incident_payload
    """)
    try:
        session.execute(statement_by_lifecycle_key, payload)
    except Exception:
        try:
            session.execute(statement_by_event_id, payload)
        except Exception:
            return None

    if hasattr(session, "commit"):
        try:
            session.commit()
        except Exception:
            return None

    return True

# keep remaining functions as-is

def save_closed_trade_review(session: Any, trade_id: str, symbol: str, review_payload: Mapping[str, Any] | None = None, execution_metrics: Mapping[str, Any] | None = None) -> bool:
    if session is None:
        return False
    try:
        if hasattr(session, "execute"):
            session.execute(text("""
                INSERT INTO closed_trade_reviews (trade_id, symbol, review_payload, execution_metrics)
                VALUES (:trade_id, :symbol, :review_payload, :execution_metrics)
                """), {
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "review_payload": json.dumps(dict(review_payload or {})),
                    "execution_metrics": json.dumps(dict(execution_metrics or {})),
                })
            if hasattr(session, "commit"):
                session.commit()
            return True
    except Exception:
        return False
    return False


def upsert_expectancy_stats(session: Any, table_name: str, key_column: str, key_value: str, pnl: float) -> bool:
    if session is None:
        return False
    try:
        session.execute(text(f"""
                INSERT INTO {table_name} ({key_column}, samples, total_pnl)
                VALUES (:key_value, 1, :pnl)
                ON CONFLICT({key_column}) DO UPDATE SET
                  samples = samples + 1,
                  total_pnl = total_pnl + :pnl
                """), {"key_value": key_value, "pnl": float(pnl)})
        if hasattr(session, "commit"):
            session.commit()
        return True
    except Exception:
        return False
