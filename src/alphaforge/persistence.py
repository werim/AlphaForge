from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import create_engine, text

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


def init_db(database_url: str = "sqlite+pysqlite:///:memory:"):
    """Backward-compatible DB initializer; returns a SQLAlchemy engine."""
    engine = create_engine(database_url, future=True)
    ddl = [
        "CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT, timeframe TEXT)",
        """
        CREATE TABLE IF NOT EXISTS order_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT,
            symbol TEXT,
            mode TEXT,
            decision TEXT,
            reject_reason TEXT,
            score REAL,
            rr REAL,
            effective_rr REAL,
            expectancy_bucket TEXT,
            volume_24h_usdt TEXT,
            spread_pct TEXT,
            funding_rate_pct TEXT,
            expected_slippage_pct TEXT,
            volatility_regime TEXT,
            liquidity_score TEXT,
            decision_ts TEXT,
            created_at TEXT,
            UNIQUE(signal_id, decision, mode)
        )
        """,
        "CREATE TABLE IF NOT EXISTS ai_decision_features (id INTEGER PRIMARY KEY AUTOINCREMENT)",
        """
        CREATE TABLE IF NOT EXISTS trade_lifecycle_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT,
            symbol TEXT,
            mode TEXT,
            state TEXT,
            reject_reason TEXT,
            details TEXT,
            event_ts TEXT,
            created_at TEXT,
            UNIQUE(signal_id, symbol, mode, state, event_ts)
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
    return engine


def fetch_expectancy_stat(
    session: Any,
    table_name: str,
    key_column: str,
    key_value: str,
) -> dict[str, Any]:
    """Fetch one expectancy stat row, returning safe defaults on any failure.

    This is intentionally defensive: missing tables, missing rows, or database
    errors all return a consistent contract-safe payload so import/runtime
    callers never crash.
    """
    default = {
        "expectancy_bucket": "UNKNOWN",
        "sample_size": 0,
        "win_rate": None,
        "avg_rr": None,
        "expectancy": None,
    }

    if session is None:
        return dict(default)

    try:
        query = f"SELECT * FROM {table_name} WHERE {key_column} = :key_value LIMIT 1"
        row = session.execute(query, {"key_value": key_value}).fetchone()
    except Exception:
        return dict(default)

    if not row:
        return dict(default)

    try:
        row_data = dict(row) if isinstance(row, Mapping) else dict(row._mapping)
    except Exception:
        return dict(default)

    return {
        "expectancy_bucket": row_data.get("expectancy_bucket") or "UNKNOWN",
        "sample_size": int(row_data.get("sample_size") or 0),
        "win_rate": row_data.get("win_rate"),
        "avg_rr": row_data.get("avg_rr"),
        "expectancy": row_data.get("expectancy"),
    }


def save_ai_decision_features(execution_features=None, *args, **kwargs):
    import json

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
    try:
        row = session.execute(
            text(
                """
                INSERT INTO signals (symbol, side, timeframe)
                VALUES (:symbol, :side, :timeframe)
                """
            ),
            {
                "symbol": signal.get("symbol"),
                "side": signal.get("side"),
                "timeframe": signal.get("timeframe"),
            },
        )
        if hasattr(session, "commit"):
            session.commit()
        return row.lastrowid
    except Exception:
        return signal.get("id")


def save_order_decision(session: Any, **decision: Any) -> Any:
    if session is None:
        return None
    try:
        payload = {
            "signal_id": decision.get("signal_id") or decision.get("order_id") or decision.get("id"),
            "symbol": decision.get("symbol"),
            "mode": decision.get("mode", "BACKTEST"),
            "decision": decision.get("decision", "REJECTED"),
            "reject_reason": decision.get("reject_reason") or decision.get("reason"),
            "score": decision.get("score"),
            "rr": decision.get("rr"),
            "effective_rr": decision.get("effective_rr"),
            "expectancy_bucket": decision.get("expectancy_bucket", "UNKNOWN"),
            "volume_24h_usdt": decision.get("volume_24h_usdt", "UNAVAILABLE_BACKTEST"),
            "spread_pct": decision.get("spread_pct", "UNAVAILABLE_BACKTEST"),
            "funding_rate_pct": decision.get("funding_rate_pct", "UNAVAILABLE_BACKTEST"),
            "expected_slippage_pct": decision.get("expected_slippage_pct", "UNAVAILABLE_BACKTEST"),
            "volatility_regime": decision.get("volatility_regime", "UNAVAILABLE_BACKTEST"),
            "liquidity_score": decision.get("liquidity_score", "UNAVAILABLE_BACKTEST"),
            "decision_ts": decision.get("decision_ts"),
            "created_at": decision.get("created_at"),
        }
        row = session.execute(text("""
            INSERT INTO order_decisions (
                signal_id,symbol,mode,decision,reject_reason,score,rr,effective_rr,expectancy_bucket,
                volume_24h_usdt,spread_pct,funding_rate_pct,expected_slippage_pct,volatility_regime,
                liquidity_score,decision_ts,created_at
            ) VALUES (
                :signal_id,:symbol,:mode,:decision,:reject_reason,:score,:rr,:effective_rr,:expectancy_bucket,
                :volume_24h_usdt,:spread_pct,:funding_rate_pct,:expected_slippage_pct,:volatility_regime,
                :liquidity_score,:decision_ts,:created_at
            )
            ON CONFLICT(signal_id, decision, mode) DO UPDATE SET
                reject_reason=excluded.reject_reason,
                score=excluded.score,
                rr=excluded.rr,
                effective_rr=excluded.effective_rr,
                expectancy_bucket=excluded.expectancy_bucket
        """), payload)
        if hasattr(session, "commit"):
            session.commit()
        return getattr(row, "lastrowid", None) or payload["signal_id"]
    except Exception:
        return decision.get("id")


def save_trade_lifecycle_event(session: Any, **event: Any) -> bool:
    if session is None:
        return False
    try:
        import json
        payload = {
            "signal_id": event.get("signal_id") or event.get("order_id") or event.get("id"),
            "symbol": event.get("symbol"),
            "mode": event.get("mode", "BACKTEST"),
            "state": event.get("state") or event.get("status_after") or event.get("event"),
            "reject_reason": event.get("reject_reason") or event.get("reason"),
            "details": json.dumps(dict(event.get("details") or {})),
            "event_ts": event.get("event_ts") or event.get("timestamp"),
            "created_at": event.get("created_at"),
        }
        session.execute(text("""
            INSERT INTO trade_lifecycle_events (signal_id, symbol, mode, state, reject_reason, details, event_ts, created_at)
            VALUES (:signal_id, :symbol, :mode, :state, :reject_reason, :details, :event_ts, :created_at)
            ON CONFLICT(signal_id, symbol, mode, state, event_ts) DO NOTHING
        """), payload)
        if hasattr(session, "commit"):
            session.commit()
        return True
    except Exception:
        return False


def save_closed_trade_review(
    session: Any,
    trade_id: str,
    symbol: str,
    review_payload: Mapping[str, Any] | None = None,
    execution_metrics: Mapping[str, Any] | None = None,
) -> bool:
    """Best-effort persistence for closed-trade review; never raises."""
    if session is None:
        return False

    try:
        import json

        if hasattr(session, "execute"):
            session.execute(
                """
                INSERT INTO closed_trade_reviews (trade_id, symbol, review_payload, execution_metrics)
                VALUES (:trade_id, :symbol, :review_payload, :execution_metrics)
                """,
                {
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "review_payload": json.dumps(dict(review_payload or {})),
                    "execution_metrics": json.dumps(dict(execution_metrics or {})),
                },
            )
            if hasattr(session, "commit"):
                session.commit()
            return True
    except Exception:
        return False

    return False


def upsert_expectancy_stats(
    session: Any,
    table_name: str,
    key_column: str,
    key_value: str,
    pnl: float,
) -> bool:
    if session is None:
        return False
    try:
        session.execute(
            text(
                f"""
                INSERT INTO {table_name} ({key_column}, samples, total_pnl)
                VALUES (:key_value, 1, :pnl)
                ON CONFLICT({key_column}) DO UPDATE SET
                  samples = samples + 1,
                  total_pnl = total_pnl + :pnl
                """
            ),
            {"key_value": key_value, "pnl": float(pnl)},
        )
        if hasattr(session, "commit"):
            session.commit()
        return True
    except Exception:
        return False
