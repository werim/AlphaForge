from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
from typing import Any

from sqlalchemy import create_engine, text
from alphaforge.contracts import canonical_reject_reason, canonical_utc_timestamp, validate_transition

__all__ = [
    "init_db",
    "fetch_expectancy_stat",
    "save_ai_decision_features",
    "save_signal",
    "save_order_decision",
    "save_trade_lifecycle_event",
    "save_closed_trade_review",
    "upsert_expectancy_stats",
]


def _utc_now_iso() -> str:
    return canonical_utc_timestamp()


def init_db(database_url: str = "sqlite+pysqlite:///:memory:"):
    engine = create_engine(database_url, future=True)
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
            mode TEXT,
            decision TEXT,
            reject_reason TEXT,
            score REAL,
            rr REAL,
            effective_rr REAL,
            expectancy_bucket TEXT,
            execution_ctx TEXT,
            execution_ctx_missing INTEGER,
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
            lifecycle_state TEXT,
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
        "CREATE TABLE IF NOT EXISTS closed_trade_reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id TEXT, symbol TEXT, review_payload TEXT, execution_metrics TEXT, created_at TEXT)",
        "CREATE TABLE IF NOT EXISTS setup_expectancy_stats (setup TEXT PRIMARY KEY, samples INTEGER NOT NULL DEFAULT 0, win_count INTEGER NOT NULL DEFAULT 0, total_pnl REAL NOT NULL DEFAULT 0, expectancy REAL NOT NULL DEFAULT 0, updated_at TEXT)",
        "CREATE TABLE IF NOT EXISTS regime_expectancy_stats (regime TEXT PRIMARY KEY, samples INTEGER NOT NULL DEFAULT 0, win_count INTEGER NOT NULL DEFAULT 0, total_pnl REAL NOT NULL DEFAULT 0, expectancy REAL NOT NULL DEFAULT 0, updated_at TEXT)",
        "CREATE TABLE IF NOT EXISTS symbol_expectancy_stats (symbol TEXT PRIMARY KEY, samples INTEGER NOT NULL DEFAULT 0, win_count INTEGER NOT NULL DEFAULT 0, total_pnl REAL NOT NULL DEFAULT 0, expectancy REAL NOT NULL DEFAULT 0, updated_at TEXT)",
        "CREATE TABLE IF NOT EXISTS cooldown_states (symbol TEXT PRIMARY KEY, cooldown_remaining_sec INTEGER NOT NULL DEFAULT 0)",
    ]
    with engine.begin() as conn:
        for statement in ddl:
            conn.execute(text(statement))
        _apply_sqlite_migrations(conn)
    return engine


def _table_columns(conn: Any, table_name: str) -> set[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table_name})")).mappings().all()
    return {str(r.get("name")) for r in rows}


def _apply_sqlite_migrations(conn: Any) -> None:
    conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL, notes TEXT)"))
    existing = {str(r[0]) for r in conn.execute(text("SELECT version FROM schema_migrations")).all()}
    migrations: list[tuple[str, str]] = [
        ("2026_05_16_persistence_integrity_v1", "Backfill missing persistence columns and normalize legacy execution_ctx_missing semantics."),
    ]
    signal_cols = _table_columns(conn, "signals")
    if "signal_id" in signal_cols and "uq_signals_signal_id_not_null" not in existing:
        conn.execute(text("UPDATE signals SET signal_id = 'legacy-signal-' || id WHERE signal_id IS NULL OR TRIM(signal_id) = ''"))
    decision_cols = _table_columns(conn, "order_decisions")
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
    lifecycle_cols = _table_columns(conn, "trade_lifecycle_events")
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
    if "lifecycle_seq" not in lifecycle_cols:
        conn.execute(text("ALTER TABLE trade_lifecycle_events ADD COLUMN lifecycle_seq INTEGER"))
    if "cancel_reason" not in lifecycle_cols:
        conn.execute(text("ALTER TABLE trade_lifecycle_events ADD COLUMN cancel_reason TEXT"))
    if "lifecycle_id" not in lifecycle_cols:
        conn.execute(text("ALTER TABLE trade_lifecycle_events ADD COLUMN lifecycle_id TEXT"))
    conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_lifecycle_signal_event_ts_state ON trade_lifecycle_events(signal_id, event_ts, lifecycle_state)"))
    for version, notes in migrations:
        if version not in existing:
            conn.execute(text("INSERT INTO schema_migrations(version, applied_at, notes) VALUES (:v, :at, :n)"), {"v": version, "at": _utc_now_iso(), "n": notes})


def fetch_expectancy_stat(session: Any, table_name: str, key_column: str, key_value: str) -> dict[str, Any]:
    default = {"expectancy_bucket": "UNKNOWN", "sample_size": 0, "win_rate": None, "avg_rr": None, "expectancy": None}
    if session is None:
        return dict(default)
    try:
        row = session.execute(f"SELECT * FROM {table_name} WHERE {key_column} = :key_value LIMIT 1", {"key_value": key_value}).fetchone()
    except Exception:
        return dict(default)
    if not row:
        return dict(default)
    row_data = dict(row) if isinstance(row, Mapping) else dict(row._mapping)
    return {
        "expectancy_bucket": row_data.get("expectancy_bucket") or "UNKNOWN",
        "sample_size": int(row_data.get("sample_size") or 0),
        "win_rate": row_data.get("win_rate"),
        "avg_rr": row_data.get("avg_rr"),
        "expectancy": row_data.get("expectancy"),
    }


def save_ai_decision_features(execution_features=None, *args, **kwargs):
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
    execution_ctx = decision.get("execution_ctx", {})
    row = session.execute(text("""
        INSERT INTO order_decisions (
            decision_id, signal_id, order_id, symbol, mode, decision, reject_reason, score, rr, effective_rr,
            expectancy_bucket, execution_ctx, execution_ctx_missing, created_at, updated_at
        ) VALUES (
            :decision_id, :signal_id, :order_id, :symbol, :mode, :decision, :reject_reason, :score, :rr, :effective_rr,
            :expectancy_bucket, :execution_ctx, :execution_ctx_missing, :created_at, :updated_at
        )
        ON CONFLICT(decision_id) DO UPDATE SET
            signal_id=excluded.signal_id, order_id=excluded.order_id, symbol=excluded.symbol, mode=excluded.mode,
            decision=excluded.decision, reject_reason=excluded.reject_reason, score=excluded.score, rr=excluded.rr,
            effective_rr=excluded.effective_rr, expectancy_bucket=excluded.expectancy_bucket,
            execution_ctx=excluded.execution_ctx, execution_ctx_missing=excluded.execution_ctx_missing, updated_at=excluded.updated_at
    """), {
        "decision_id": decision_id, "signal_id": decision.get("signal_id"), "order_id": decision.get("order_id"),
        "symbol": decision.get("symbol"), "mode": decision.get("mode"), "decision": decision.get("decision"),
        "reject_reason": canonical_reject_reason(decision.get("reject_reason")) if str(decision.get("decision", "")).upper() == "REJECTED" else decision.get("reject_reason"), "score": decision.get("score"), "rr": decision.get("rr"),
        "effective_rr": decision.get("effective_rr"), "expectancy_bucket": decision.get("expectancy_bucket"),
        "execution_ctx": json.dumps(execution_ctx),
        "execution_ctx_missing": 1 if bool(decision.get("execution_ctx_missing", False)) else 0,
        "created_at": now, "updated_at": now,
    })
    if hasattr(session, "commit"):
        session.commit()
    return decision_id or row.lastrowid


def save_trade_lifecycle_event(session: Any, **event: Any) -> bool:
    if session is None:
        return False
    now = _utc_now_iso()
    event_id = event.get("event_id") or event.get("id") or f"{event.get('symbol', 'UNKNOWN')}:{canonical_utc_timestamp(event.get('event_ts'))}:{event.get('lifecycle_state') or event.get('state') or 'UNKNOWN'}"
    signal_id = event.get("signal_id") or f"UNKNOWN_SIGNAL:{event.get('symbol', 'UNKNOWN')}:{canonical_utc_timestamp(event.get('event_ts'))}"
    lifecycle_state = event.get("lifecycle_state") or event.get("state")
    prev_state = event.get("previous_lifecycle_state")
    is_valid = validate_transition(prev_state, lifecycle_state) if lifecycle_state else False
    if not is_valid and prev_state is not None:
        lifecycle_state = "ERROR"
    session.execute(text("""
        INSERT INTO trade_lifecycle_events (
            event_id, signal_id, order_id, symbol, mode, lifecycle_state, decision, reject_reason, score, rr,
            effective_rr, expectancy_bucket, execution_ctx, execution_ctx_missing, event_ts, created_at, lifecycle_seq, cancel_reason, lifecycle_id
        ) VALUES (
            :event_id, :signal_id, :order_id, :symbol, :mode, :lifecycle_state, :decision, :reject_reason, :score, :rr,
            :effective_rr, :expectancy_bucket, :execution_ctx, :execution_ctx_missing, :event_ts, :created_at, :lifecycle_seq, :cancel_reason, :lifecycle_id
        )
        ON CONFLICT(event_id) DO UPDATE SET
            signal_id=excluded.signal_id, order_id=excluded.order_id, symbol=excluded.symbol, mode=excluded.mode,
            lifecycle_state=excluded.lifecycle_state, decision=excluded.decision, reject_reason=excluded.reject_reason,
            score=excluded.score, rr=excluded.rr, effective_rr=excluded.effective_rr, expectancy_bucket=excluded.expectancy_bucket,
            execution_ctx=excluded.execution_ctx, execution_ctx_missing=excluded.execution_ctx_missing, event_ts=excluded.event_ts,
            lifecycle_seq=excluded.lifecycle_seq, cancel_reason=excluded.cancel_reason, lifecycle_id=excluded.lifecycle_id
    """), {
        "event_id": event_id, "signal_id": signal_id, "order_id": event.get("order_id"), "symbol": event.get("symbol"),
        "mode": event.get("mode"), "lifecycle_state": lifecycle_state, "decision": event.get("decision"),
        "reject_reason": canonical_reject_reason(event.get("reject_reason")), "score": event.get("score"), "rr": event.get("rr"), "effective_rr": event.get("effective_rr"),
        "expectancy_bucket": event.get("expectancy_bucket"), "execution_ctx": json.dumps(event.get("execution_ctx", {})),
        "execution_ctx_missing": 1 if bool(event.get("execution_ctx_missing", False)) else 0, "event_ts": canonical_utc_timestamp(event.get("event_ts")), "created_at": now,
        "lifecycle_seq": event.get("lifecycle_seq"),
        "cancel_reason": event.get("cancel_reason"),
        "lifecycle_id": event.get("lifecycle_id") or f"{signal_id}:{canonical_utc_timestamp(event.get('event_ts'))}:{lifecycle_state}",
    })
    if hasattr(session, "commit"):
        session.commit()
    return True

# keep remaining functions as-is

def save_closed_trade_review(session: Any, trade_id: str, symbol: str, review_payload: Mapping[str, Any] | None = None, execution_metrics: Mapping[str, Any] | None = None) -> bool:
    if session is None:
        return False
    try:
        if hasattr(session, "execute"):
            session.execute("""
                INSERT INTO closed_trade_reviews (trade_id, symbol, review_payload, execution_metrics)
                VALUES (:trade_id, :symbol, :review_payload, :execution_metrics)
                """, {
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
