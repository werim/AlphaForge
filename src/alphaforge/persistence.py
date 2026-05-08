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


def save_ai_decision_features(execution_features):
    import json
    # Ensure execution_features is formatted as a JSON string
    formatted_features = json.dumps(execution_features)
    
    # Code to insert formatted_features into ai_decision_features table goes here
    # ... 
    
    # Example pseudocode for insertion
    # insert_into_ai_decision_features(formatted_features)
    
    return formatted_features
