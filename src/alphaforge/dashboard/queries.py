from __future__ import annotations

import json
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def _has_table(engine: Engine, table_name: str) -> bool:
    return bool(inspect(engine).has_table(table_name))


def _column_names(engine: Engine, table_name: str) -> set[str]:
    if not _has_table(engine, table_name):
        return set()
    return {str(column["name"]) for column in inspect(engine).get_columns(table_name)}


def fetch_latest_readiness(engine: Engine) -> dict[str, Any]:
    if not _has_table(engine, "live_readiness_reports"):
        return {"status": "NOT_AVAILABLE", "reason": "NO_READINESS_REPORT_TABLE"}
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT generated_at, qualified, deployment_state, acknowledgement_required, report_payload
            FROM live_readiness_reports
            ORDER BY id DESC
            LIMIT 1
        """)).mappings().first()
    if row is None:
        return {"status": "NOT_AVAILABLE", "reason": "NO_READINESS_REPORT"}
    try:
        payload = json.loads(row.get("report_payload") or "{}")
    except json.JSONDecodeError:
        payload = {"status": "ERROR", "reason": "INVALID_REPORT_PAYLOAD"}
    return {
        "status": "PASS" if bool(row["qualified"]) else "FAIL",
        "qualified": bool(row["qualified"]),
        "deployment_state": row["deployment_state"],
        "acknowledgement_required": bool(row["acknowledgement_required"]),
        "generated_at": row["generated_at"],
        "payload": payload,
    }


def fetch_reject_summary(engine: Engine) -> dict[str, Any]:
    columns = _column_names(engine, "order_decisions")
    required_columns = {"decision", "reject_reason", "signal_id", "symbol"}
    if not required_columns.issubset(columns):
        return {
            "status": "NOT_AVAILABLE",
            "reason": "ORDER_DECISIONS_SCHEMA_INCOMPLETE",
            "total_final_decisions": None,
            "total_rejected": None,
            "rejection_rate": None,
            "reasons": [],
            "incomplete_rejected_rows": {},
        }
    where_final = "COALESCE(phase, 'final') = 'final'" if "phase" in columns else "1 = 1"
    with engine.connect() as conn:
        total = int(conn.execute(text(f"SELECT COUNT(*) FROM order_decisions WHERE {where_final}")).scalar_one())
        rejected = int(conn.execute(text(f"SELECT COUNT(*) FROM order_decisions WHERE {where_final} AND UPPER(COALESCE(decision, '')) = 'REJECTED'")).scalar_one())
        rows = conn.execute(text(f"""
            SELECT COALESCE(NULLIF(TRIM(reject_reason), ''), 'MISSING_REASON') AS reject_reason, COUNT(*) AS count
            FROM order_decisions
            WHERE {where_final} AND UPPER(COALESCE(decision, '')) = 'REJECTED'
            GROUP BY COALESCE(NULLIF(TRIM(reject_reason), ''), 'MISSING_REASON')
            ORDER BY count DESC, reject_reason ASC
        """)).mappings().all()
        incomplete = conn.execute(text(f"""
            SELECT
                SUM(CASE WHEN signal_id IS NULL OR TRIM(signal_id) = '' THEN 1 ELSE 0 END) AS empty_signal_id_count,
                SUM(CASE WHEN symbol IS NULL OR TRIM(symbol) = '' THEN 1 ELSE 0 END) AS empty_symbol_count,
                SUM(CASE WHEN reject_reason IS NULL OR TRIM(reject_reason) = '' THEN 1 ELSE 0 END) AS empty_reject_reason_count
            FROM order_decisions
            WHERE {where_final} AND UPPER(COALESCE(decision, '')) = 'REJECTED'
        """)).mappings().one()
    return {
        "status": "AVAILABLE",
        "total_final_decisions": total,
        "total_rejected": rejected,
        "rejection_rate": (rejected / total) if total else None,
        "reasons": [
            {
                "reason": row["reject_reason"],
                "count": int(row["count"]),
                "ratio": (int(row["count"]) / rejected) if rejected else None,
            }
            for row in rows
        ],
        "incomplete_rejected_rows": {key: int(incomplete[key] or 0) for key in incomplete.keys()},
    }


def fetch_recent_lifecycle(engine: Engine, *, limit: int = 100, signal_id: str | None = None, symbol: str | None = None) -> list[dict[str, Any]]:
    columns = _column_names(engine, "trade_lifecycle_events")
    required_columns = {"signal_id", "symbol", "mode", "lifecycle_state", "reject_reason", "event_ts"}
    if not required_columns.issubset(columns):
        return []
    conditions: list[str] = []
    params: dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
    if signal_id:
        conditions.append("signal_id = :signal_id")
        params["signal_id"] = signal_id
    if symbol:
        conditions.append("symbol = :symbol")
        params["symbol"] = symbol
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT signal_id, symbol, mode, lifecycle_state, reject_reason, event_ts
            FROM trade_lifecycle_events
            {where}
            ORDER BY event_ts DESC
            LIMIT :limit
        """), params).mappings().all()
    return [dict(row) for row in rows]


def fetch_signal_timeline(engine: Engine, signal_id: str) -> dict[str, Any]:
    events = list(reversed(fetch_recent_lifecycle(engine, limit=500, signal_id=signal_id)))
    rejected_without_reason = any(
        str(row.get("lifecycle_state") or "") == "SIGNAL_REJECTED" and not str(row.get("reject_reason") or "").strip()
        for row in events
    )
    return {
        "signal_id": signal_id,
        "events": events,
        "event_count": len(events),
        "has_signal_created": any(str(row.get("lifecycle_state") or "") == "SIGNAL_CREATED" for row in events),
        "rejected_without_reason": rejected_without_reason,
    }
