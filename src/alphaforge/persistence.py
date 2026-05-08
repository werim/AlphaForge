from __future__ import annotations

from collections.abc import Mapping
from typing import Any


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
    return signal.get("id")


def save_order_decision(session: Any, **decision: Any) -> Any:
    if session is None:
        return None
    return decision.get("id")


def save_trade_lifecycle_event(session: Any, **event: Any) -> bool:
    if session is None:
        return False
    return True


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
    return True
