"""SQL-backed PAPER account-performance reporting for #365.

This module is diagnostic/reporting authority only.  Return objectives are never
fed back into sizing, participation, leverage, or target geometry.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import text


def _dt(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _execute(conn: Any, sql: str, params: Mapping[str, Any]):
    if isinstance(conn, sqlite3.Connection):
        return conn.execute(sql, dict(params))
    return conn.execute(text(sql), dict(params))


def build_account_performance_report(
    conn: Any,
    *,
    burnin_run_id: str,
    as_of: str | datetime | None = None,
    window_days: int = 30,
    monthly_return_target_fraction: float | None = None,
) -> dict[str, Any]:
    """Summarize realized PAPER economics from canonical closed-trade evidence.

    Missing sizing/equity provenance never becomes zero.  A monthly return target
    is accepted only as a reporting comparator and does not alter any economic
    metric or trading decision.
    """
    if int(window_days) <= 0:
        raise ValueError("window_days must be positive")
    rows = _execute(
        conn,
        """SELECT closed_at,symbol,regime,net_r,net_pnl,payload_json,evidence_complete
           FROM burnin_trade_outcomes
           WHERE burnin_run_id=:bid AND closed_at IS NOT NULL
           ORDER BY closed_at,id""",
        {"bid": burnin_run_id},
    ).fetchall()
    if not rows:
        return {
            "status": "UNAVAILABLE",
            "reason": "NO_CLOSED_TRADE_EVIDENCE",
            "burnin_run_id": burnin_run_id,
        }

    records: list[dict[str, Any]] = []
    for raw in rows:
        mapping = raw if isinstance(raw, sqlite3.Row) else getattr(raw, "_mapping", raw)
        row = dict(mapping)
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        provenance = payload.get("source_provenance")
        if not isinstance(provenance, Mapping):
            provenance = {}
        records.append({**row, "payload": payload, "provenance": dict(provenance)})

    if as_of is None:
        as_of_dt = max(_dt(row["closed_at"]) for row in records)
    elif isinstance(as_of, datetime):
        as_of_dt = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
    else:
        as_of_dt = _dt(as_of)
    window_start = as_of_dt - timedelta(days=int(window_days))
    selected = [
        row for row in records
        if window_start < _dt(row["closed_at"]) <= as_of_dt
    ]
    if not selected:
        return {
            "status": "UNAVAILABLE",
            "reason": "NO_CLOSED_TRADES_IN_WINDOW",
            "burnin_run_id": burnin_run_id,
            "window_days": int(window_days),
        }

    missing_fields: set[str] = set()
    complete_rows: list[dict[str, Any]] = []
    for row in selected:
        if int(row.get("evidence_complete") or 0) != 1:
            missing_fields.add("trade_evidence_complete")
            continue
        prov = row["provenance"]
        required = {
            "portfolio_equity": _finite(prov.get("portfolio_equity")),
            "risk_at_stop_usdt": _finite(prov.get("risk_at_stop_usdt")),
            "risk_at_stop_pct_equity": _finite(prov.get("risk_at_stop_pct_equity")),
            "selected_notional": _finite(prov.get("selected_notional", row["payload"].get("notional"))),
            "net_r": _finite(row.get("net_r")),
            "net_pnl": _finite(row.get("net_pnl")),
        }
        for name, value in required.items():
            if value is None:
                missing_fields.add(name)
        complete_rows.append({**row, **required})

    expectancy_values = [
        _finite(row.get("net_r")) for row in selected
        if int(row.get("evidence_complete") or 0) == 1
        and _finite(row.get("net_r")) is not None
    ]
    expectancy_after_costs = (
        sum(expectancy_values) / len(expectancy_values)
        if expectancy_values else None
    )

    contributions: dict[str, dict[str, dict[str, float | int]]] = {
        "regime": {},
        "symbol": {},
        "setup_phase": {},
    }
    buckets: dict[str, defaultdict[str, dict[str, float | int]]] = {
        "regime": defaultdict(lambda: {"trades": 0, "net_pnl": 0.0, "net_r": 0.0}),
        "symbol": defaultdict(lambda: {"trades": 0, "net_pnl": 0.0, "net_r": 0.0}),
        "setup_phase": defaultdict(lambda: {"trades": 0, "net_pnl": 0.0, "net_r": 0.0}),
    }
    for row in selected:
        nr = _finite(row.get("net_r"))
        np = _finite(row.get("net_pnl"))
        if int(row.get("evidence_complete") or 0) != 1 or nr is None or np is None:
            continue
        keys = {
            "regime": str(row.get("regime") or "UNKNOWN"),
            "symbol": str(row.get("symbol") or "UNKNOWN"),
            "setup_phase": str(row["provenance"].get("setup_phase") or "UNKNOWN"),
        }
        for dimension, key in keys.items():
            bucket = buckets[dimension][key]
            bucket["trades"] = int(bucket["trades"]) + 1
            bucket["net_pnl"] = float(bucket["net_pnl"]) + np
            bucket["net_r"] = float(bucket["net_r"]) + nr
    for dimension, values in buckets.items():
        contributions[dimension] = {key: dict(value) for key, value in values.items()}

    target = _finite(monthly_return_target_fraction)
    if monthly_return_target_fraction is not None and (target is None or target < 0):
        raise ValueError("monthly_return_target_fraction must be finite and >= 0")

    core_complete = bool(complete_rows) and not missing_fields and len(complete_rows) == len(selected)
    if core_complete:
        start_equity = float(complete_rows[0]["portfolio_equity"])
        if start_equity <= 0:
            missing_fields.add("portfolio_equity")
            core_complete = False

    if core_complete:
        realized_returns = [
            {
                "closed_at": row["closed_at"],
                "return_fraction": float(row["net_pnl"]) / float(row["portfolio_equity"]),
                "risk_fraction": float(row["risk_at_stop_pct_equity"]),
                "risk_usdt": float(row["risk_at_stop_usdt"]),
                "selected_notional": float(row["selected_notional"]),
            }
            for row in complete_rows
        ]
        total_net_pnl = sum(float(row["net_pnl"]) for row in complete_rows)
        rolling_return = total_net_pnl / start_equity

        equity = start_equity
        peak = equity
        max_drawdown = 0.0
        for row in complete_rows:
            equity += float(row["net_pnl"])
            peak = max(peak, equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - equity) / peak)
        return_drawdown_ratio = (
            None if max_drawdown <= 0 else rolling_return / max_drawdown
        )
        avg_risk_usdt = sum(float(row["risk_at_stop_usdt"]) for row in complete_rows) / len(complete_rows)
        avg_risk_fraction = sum(float(row["risk_at_stop_pct_equity"]) for row in complete_rows) / len(complete_rows)
        avg_notional = sum(float(row["selected_notional"]) for row in complete_rows) / len(complete_rows)
    else:
        realized_returns = None
        total_net_pnl = None
        rolling_return = None
        max_drawdown = None
        return_drawdown_ratio = None
        avg_risk_usdt = None
        avg_risk_fraction = None
        avg_notional = None

    return {
        "status": "COMPLETE" if core_complete else "INCOMPLETE",
        "reason": "" if core_complete else "ACCOUNT_PERFORMANCE_EVIDENCE_INCOMPLETE",
        "burnin_run_id": burnin_run_id,
        "window_days": int(window_days),
        "window_start": window_start.isoformat(),
        "as_of": as_of_dt.isoformat(),
        "trade_count": len(selected),
        "missing_fields": sorted(missing_fields),
        "risk_per_trade": realized_returns,
        "average_risk_usdt": avg_risk_usdt,
        "average_risk_fraction": avg_risk_fraction,
        "average_selected_notional": avg_notional,
        "total_net_pnl": total_net_pnl,
        "rolling_monthly_return_fraction": rolling_return,
        "rolling_realized_drawdown_fraction": max_drawdown,
        "expectancy_after_costs_net_r": expectancy_after_costs,
        "return_drawdown_ratio": return_drawdown_ratio,
        "contribution": contributions,
        "monthly_return_target_fraction": target,
        "monthly_return_target_gap_fraction": (
            None if rolling_return is None or target is None else rolling_return - target
        ),
        "monthly_return_target_role": "REPORTING_ONLY",
    }
