"""Timestamp-bounded expectancy evidence and its pure as-of reader.

Only resolved, first-class evidence belongs here.  ``created_at`` is audit
metadata; it is intentionally never a reader predicate.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import math
import sqlite3
from typing import Any

from sqlalchemy import text

from alphaforge.contracts import canonical_utc_timestamp
from alphaforge.scoring_context import empty_stats_context


EXPECTANCY_EVIDENCE_DDL = """
CREATE TABLE IF NOT EXISTS expectancy_evidence (
    evidence_id TEXT PRIMARY KEY,
    source_decision_id TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    decision_time TEXT NOT NULL,
    resolved_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT,
    setup_type TEXT,
    regime TEXT,
    reject_reason TEXT,
    net_r REAL NOT NULL,
    run_id TEXT,
    campaign_id TEXT,
    release_id TEXT,
    evidence_complete INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
)
"""

EXPECTANCY_EVIDENCE_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS ix_expectancy_evidence_as_of "
    "ON expectancy_evidence(resolved_at, decision_time)",
    "CREATE INDEX IF NOT EXISTS ix_expectancy_evidence_symbol_as_of "
    "ON expectancy_evidence(symbol, resolved_at, decision_time)",
)


def _execute(bind: Any, sql: str, params: dict[str, Any] | None = None) -> Any:
    return bind.execute(sql if isinstance(bind, sqlite3.Connection) else text(sql), params or {})


def ensure_expectancy_evidence_schema(bind: Any) -> None:
    _execute(bind, EXPECTANCY_EVIDENCE_DDL)
    for ddl in EXPECTANCY_EVIDENCE_INDEX_DDL:
        _execute(bind, ddl)


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value) / 1000.0 if abs(float(value)) >= 100_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return canonical_utc_timestamp(str(value))


def record_expectancy_evidence(bind: Any, *, evidence_id: str, source_decision_id: str | None,
                               evidence_type: str, decision_time: Any, resolved_at: Any,
                               symbol: str | None, side: str | None, setup_type: str | None,
                               regime: str | None, reject_reason: str | None, net_r: Any,
                               run_id: str | None, campaign_id: str | None,
                               release_id: str | None, evidence_complete: bool = True) -> bool:
    """Append one complete resolved result; incomplete/unknown evidence is not guessed."""
    try:
        value = float(net_r)
    except (TypeError, ValueError):
        return False
    if (not evidence_complete or not source_decision_id or decision_time is None or resolved_at is None
            or not symbol or not math.isfinite(value)):
        return False
    ensure_expectancy_evidence_schema(bind)
    _execute(bind, """
        INSERT INTO expectancy_evidence(
            evidence_id,source_decision_id,evidence_type,decision_time,resolved_at,
            symbol,side,setup_type,regime,reject_reason,net_r,run_id,campaign_id,
            release_id,evidence_complete,created_at
        ) VALUES (
            :evidence_id,:source_decision_id,:evidence_type,:decision_time,:resolved_at,
            :symbol,:side,:setup_type,:regime,:reject_reason,:net_r,:run_id,:campaign_id,
            :release_id,1,:created_at
        ) ON CONFLICT(evidence_id) DO NOTHING
    """, {
        "evidence_id": str(evidence_id), "source_decision_id": str(source_decision_id),
        "evidence_type": str(evidence_type), "decision_time": _timestamp(decision_time),
        "resolved_at": _timestamp(resolved_at), "symbol": str(symbol).upper(),
        "side": str(side).upper() if side else None,
        "setup_type": str(setup_type) if setup_type else None,
        "regime": str(regime) if regime else None,
        "reject_reason": str(reject_reason) if reject_reason else None,
        "net_r": value, "run_id": run_id, "campaign_id": campaign_id,
        "release_id": release_id, "created_at": canonical_utc_timestamp(),
    })
    return True


def fetch_expectancy_as_of(bind: Any, *, as_of: Any, symbol: str, setup_type: str | None,
                            regime: str | None, run_id: str | None = None,
                            campaign_id: str | None = None,
                            release_id: str | None = None) -> dict[str, Any]:
    """Return AIBrain's existing stats shape from evidence knowable at ``as_of``.

    Scope IDs are optional.  When supplied, each is an exact isolation filter.
    """
    timestamp = _timestamp(as_of)
    rows = _execute(bind, """
        SELECT symbol, setup_type, regime, net_r
        FROM expectancy_evidence
        WHERE evidence_complete=1
          AND decision_time <= :as_of
          AND resolved_at IS NOT NULL
          AND resolved_at <= :as_of
          AND (:run_id IS NULL OR run_id=:run_id)
          AND (:campaign_id IS NULL OR campaign_id=:campaign_id)
          AND (:release_id IS NULL OR release_id=:release_id)
          AND (symbol=:symbol OR setup_type=:setup_type OR regime=:regime)
        ORDER BY resolved_at, evidence_id
    """, {
        "as_of": timestamp, "symbol": str(symbol).upper(), "setup_type": setup_type,
        "regime": regime, "run_id": run_id, "campaign_id": campaign_id,
        "release_id": release_id,
    }).fetchall()
    if not rows:
        return empty_stats_context()
    buckets: dict[str, dict[str, list[float]]] = {
        "setup": defaultdict(list), "regime": defaultdict(list), "symbol": defaultdict(list),
    }
    for row in rows:
        values = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
        net_r = float(values["net_r"])
        if setup_type and values.get("setup_type") == setup_type:
            buckets["setup"][setup_type].append(net_r)
        if regime and values.get("regime") == regime:
            buckets["regime"][regime].append(net_r)
        if values.get("symbol") == str(symbol).upper():
            buckets["symbol"][str(symbol).upper()].append(net_r)
    stats = {
        kind: {key: sum(values) / len(values) for key, values in dimensions.items() if values}
        for kind, dimensions in buckets.items()
    }
    return {**stats, "sample_size": len(rows)}
