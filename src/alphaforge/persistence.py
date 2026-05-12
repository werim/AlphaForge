from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import create_engine, text


def save_ai_decision_features(execution_features):
    import json
    # Ensure execution_features is formatted as a JSON string
    formatted_features = json.dumps(execution_features)

    # Code to insert formatted_features into ai_decision_features table goes here
    # ...

    # Example pseudocode for insertion
    # insert_into_ai_decision_features(formatted_features)

    return formatted_features


def init_db(url: str):
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT, timeframe TEXT, payload TEXT, created_at TEXT)"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS order_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT, created_at TEXT)"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS ai_decision_features (id INTEGER PRIMARY KEY AUTOINCREMENT, features TEXT, created_at TEXT)"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS trade_lifecycle_events (id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id TEXT, state TEXT, payload TEXT, created_at TEXT)"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS closed_trade_reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id TEXT, symbol TEXT, pnl REAL, review_payload TEXT, execution_metrics TEXT, created_at TEXT)"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS setup_expectancy_stats (setup TEXT PRIMARY KEY, samples INTEGER NOT NULL DEFAULT 0, win_count INTEGER NOT NULL DEFAULT 0, total_pnl REAL NOT NULL DEFAULT 0.0, expectancy REAL, updated_at TEXT)"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS regime_expectancy_stats (regime TEXT PRIMARY KEY, samples INTEGER NOT NULL DEFAULT 0, win_count INTEGER NOT NULL DEFAULT 0, total_pnl REAL NOT NULL DEFAULT 0.0, expectancy REAL, updated_at TEXT)"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS symbol_expectancy_stats (symbol TEXT PRIMARY KEY, samples INTEGER NOT NULL DEFAULT 0, win_count INTEGER NOT NULL DEFAULT 0, total_pnl REAL NOT NULL DEFAULT 0.0, expectancy REAL, updated_at TEXT)"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS cooldown_states (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, block_until INTEGER, reason TEXT)"))
    return engine


def save_signal(session, **signal):
    now = datetime.now(timezone.utc).isoformat()
    payload = str(signal)
    result = session.execute(
        text("INSERT INTO signals(symbol, side, timeframe, payload, created_at) VALUES (:symbol,:side,:timeframe,:payload,:created_at)"),
        {
            "symbol": str(signal.get("symbol", "UNKNOWN")),
            "side": str(signal.get("side", "BUY")),
            "timeframe": str(signal.get("timeframe", "NA")),
            "payload": payload,
            "created_at": now,
        },
    )
    session.commit()
    return result.lastrowid

def save_order_decision(session, *args, **kwargs):
    return None


def save_trade_lifecycle_event(session, *args, **kwargs):
    return None


def save_closed_trade_review(session, trade_id: str, symbol: str, execution_metrics: Any, review_payload: Any | None = None, pnl: float | None = None):
    now = datetime.now(timezone.utc).isoformat()
    session.execute(
        text("INSERT INTO closed_trade_reviews(trade_id, symbol, pnl, review_payload, execution_metrics, created_at) VALUES (:trade_id,:symbol,:pnl,:review_payload,:execution_metrics,:created_at)"),
        {
            "trade_id": trade_id,
            "symbol": symbol,
            "pnl": float(pnl or 0.0),
            "review_payload": str(review_payload) if review_payload is not None else None,
            "execution_metrics": str(execution_metrics),
            "created_at": now,
        },
    )
    session.commit()

def upsert_expectancy_stats(session, table_name: str, key_column: str, key_value: str, pnl: float):
    allowed_tables = {"setup_expectancy_stats", "regime_expectancy_stats", "symbol_expectancy_stats"}
    allowed_keys = {"setup", "regime", "symbol"}
    if table_name not in allowed_tables or key_column not in allowed_keys:
        return None

    now = datetime.now(timezone.utc).isoformat()
    win = 1 if float(pnl) > 0 else 0
    sql = text(
        f"""
        INSERT INTO {table_name} ({key_column}, samples, win_count, total_pnl, expectancy, updated_at)
        VALUES (:key_value, 1, :win, :pnl, :pnl, :updated_at)
        ON CONFLICT({key_column}) DO UPDATE SET
            samples = {table_name}.samples + 1,
            win_count = {table_name}.win_count + excluded.win_count,
            total_pnl = {table_name}.total_pnl + excluded.total_pnl,
            expectancy = ({table_name}.total_pnl + excluded.total_pnl) / ({table_name}.samples + 1),
            updated_at = :updated_at
        """
    )
    session.execute(sql, {"key_value": key_value, "win": win, "pnl": float(pnl), "updated_at": now})
    session.commit()
    return True

def fetch_expectancy_stat(session, table_name: str, key_column: str, key_value: str) -> float | None:
    """Fetch expectancy from approved expectancy stat tables.

    Returns float when found, otherwise None for missing session/table/row/value
    and on safe fallback exceptions.
    """
    allowed_tables = {"setup_expectancy_stats", "regime_expectancy_stats", "symbol_expectancy_stats"}
    allowed_keys = {"setup", "regime", "symbol"}

    if session is None or table_name not in allowed_tables or key_column not in allowed_keys:
        return None

    try:
        row = session.execute(
            text(f"SELECT expectancy FROM {table_name} WHERE {key_column} = :key_value LIMIT 1"),
            {"key_value": key_value},
        ).one_or_none()
        if row is None:
            return None
        value = getattr(row, "expectancy", None)
        if value is None:
            return None
        return float(value)
    except Exception:
        return None
