"""Observational state-direction evaluation in the separate adaptive shadow store."""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from alphaforge.adaptive_decision_calibration import (
    SCHEMA_VERSION,
    bootstrap_shadow_schema,
)
from alphaforge.burnin import canonical_hash, utc_now
from alphaforge.burnin_resolver import evaluate_forward_outcome, timeframe_seconds
from alphaforge.execution import build_execution_cost_breakdown
from alphaforge.multi_timeframe import evaluate_mtf_alignment


SHADOW_DIRECTIONS = {"WOULD_LONG", "WOULD_SHORT", "WOULD_NO_TRADE"}
GEOMETRY_TYPES = {
    "SAME_SIDE_GEOMETRY",
    "MIRRORED_GEOMETRY",
    "STRUCTURE_VALID_GEOMETRY",
    "GEOMETRY_UNAVAILABLE",
}
REPORT_GROUPS = {
    "symbol", "regime", "setup_phase", "resolved_state",
    "base_exec_direction", "shadow_final_direction", "shadow_geometry_type",
}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: Any) -> datetime | None:
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _valid_geometry(side: str, entry: Any, stop: Any, target: Any) -> bool:
    e, s, t = _finite(entry), _finite(stop), _finite(target)
    if e is None or s is None or t is None or min(e, s, t) <= 0:
        return False
    if side == "LONG":
        return s < e < t
    if side == "SHORT":
        return t < e < s
    return False


def derive_shadow_geometry(
    market_ctx: Mapping[str, Any],
    shadow_final_direction: str,
    *,
    structure_geometry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return diagnostic geometry without mutating the actual market context."""
    base_side = str(market_ctx.get("side") or "").upper()
    entry = _finite(market_ctx.get("entry"))
    base_sl = _finite(market_ctx.get("sl", market_ctx.get("stop")))
    base_tp = _finite(market_ctx.get("tp", market_ctx.get("target")))
    shadow_side = {
        "WOULD_LONG": "LONG",
        "WOULD_SHORT": "SHORT",
        "WOULD_NO_TRADE": base_side,
    }.get(shadow_final_direction, "")
    unavailable = {
        "entry": entry, "base_sl": base_sl, "base_tp": base_tp,
        "shadow_sl": None, "shadow_tp": None,
        "shadow_geometry_type": "GEOMETRY_UNAVAILABLE", "geometry_valid": False,
    }
    if not _valid_geometry(base_side, entry, base_sl, base_tp):
        return unavailable

    if shadow_final_direction == "WOULD_NO_TRADE" or shadow_side == base_side:
        return {
            **unavailable, "shadow_sl": base_sl, "shadow_tp": base_tp,
            "shadow_geometry_type": "SAME_SIDE_GEOMETRY", "geometry_valid": True,
        }

    if isinstance(structure_geometry, Mapping):
        structure_side = str(structure_geometry.get("side") or "").upper()
        structure_sl = _finite(structure_geometry.get("sl", structure_geometry.get("stop")))
        structure_tp = _finite(structure_geometry.get("tp", structure_geometry.get("target")))
        structure_entry = entry
        if structure_side == shadow_side and _valid_geometry(
                shadow_side, structure_entry, structure_sl, structure_tp):
            return {
                **unavailable, "entry": structure_entry,
                "shadow_sl": structure_sl, "shadow_tp": structure_tp,
                "shadow_geometry_type": "STRUCTURE_VALID_GEOMETRY",
                "geometry_valid": True,
            }

    mirrored_sl = 2 * entry - base_sl
    mirrored_tp = 2 * entry - base_tp
    if not _valid_geometry(shadow_side, entry, mirrored_sl, mirrored_tp):
        return unavailable
    return {
        **unavailable, "shadow_sl": mirrored_sl, "shadow_tp": mirrored_tp,
        "shadow_geometry_type": "MIRRORED_GEOMETRY", "geometry_valid": True,
    }


def build_state_direction_shadow_draft(
    *,
    symbol: str,
    signal_id: str,
    market_ctx: Mapping[str, Any],
    mtf: Mapping[str, Any],
    execution_ctx: Mapping[str, Any],
    horizon_bars: int,
    observed_at: str | None = None,
) -> dict[str, Any] | None:
    regime = mtf.get("regime") if isinstance(mtf.get("regime"), Mapping) else None
    setup = mtf.get("setup") if isinstance(mtf.get("setup"), Mapping) else None
    execution = mtf.get("execution") if isinstance(mtf.get("execution"), Mapping) else None
    decision_time = _timestamp(market_ctx.get("market_ts"))
    if decision_time is None:
        return None
    decision_timestamp = _iso(decision_time)
    decision_ms = int(decision_time.timestamp() * 1000)
    alignment = evaluate_mtf_alignment(
        regime, setup, execution, decision_ts_ms=decision_ms,
        state_direction_resolution_enabled=True,
    )
    final = str(alignment.get("final_direction") or "NO_TRADE").upper()
    shadow_final = {
        "LONG": "WOULD_LONG", "SHORT": "WOULD_SHORT",
    }.get(final, "WOULD_NO_TRADE")
    structure = mtf.get("state_direction_geometry")
    geometry = derive_shadow_geometry(
        dict(market_ctx), shadow_final,
        structure_geometry=structure if isinstance(structure, Mapping) else None,
    )
    entry = geometry["entry"]
    shadow_sl = geometry["shadow_sl"]
    shadow_tp = geometry["shadow_tp"]
    raw_rr = None
    if geometry["geometry_valid"]:
        risk = abs(float(entry) - float(shadow_sl))
        raw_rr = abs(float(shadow_tp) - float(entry)) / risk if risk > 0 else None
    costs = build_execution_cost_breakdown(
        raw_rr, execution_ctx, min_effective_rr=0.0,
        include_missing_penalty=False,
    ) if raw_rr is not None else None
    timeframe = str(
        (execution or {}).get("timeframe")
        or market_ctx.get("timeframe")
        or ""
    ) or None
    seconds = timeframe_seconds(timeframe)
    due_at = (
        _iso(datetime.fromtimestamp(
            decision_time.timestamp() + seconds * int(horizon_bars), timezone.utc
        ))
        if seconds is not None and int(horizon_bars) > 0 else None
    )
    unavailable_costs = list(costs.unavailable_fields) if costs else ["geometry"]
    evidence_complete = bool(
        geometry["geometry_valid"]
        and due_at
        and not unavailable_costs
        and not any(reason in {
            "MTF_REGIME_UNAVAILABLE", "MTF_SETUP_UNAVAILABLE",
            "MTF_EXECUTION_UNAVAILABLE", "MTF_CONTEXT_STALE",
        } for reason in alignment.get("reasons", []))
    )
    shadow_decision_id = "sds_" + canonical_hash([
        symbol, signal_id, decision_timestamp,
        alignment.get("resolved_state"), shadow_final,
    ])[:32]
    reason_parts = [
        str(alignment.get("override_reason") or "STATE_DIRECTION_EVALUATED"),
        *[str(reason) for reason in alignment.get("reasons", [])],
    ]
    setup_phase = str(
        alignment.get("setup_phase")
        or (setup or {}).get("phase")
        or "UNKNOWN"
    ).upper()
    return {
        "shadow_decision_id": shadow_decision_id,
        "observed_at": observed_at or utc_now(),
        "symbol": symbol,
        "decision_timestamp": decision_timestamp,
        "signal_id": signal_id,
        "base_exec_direction": alignment.get("base_exec_direction"),
        "regime_direction": (regime or {}).get("direction"),
        "setup_direction": (setup or {}).get("direction"),
        "resolved_state": alignment.get("resolved_state") or "REGIME_UNKNOWN__SETUP_UNKNOWN",
        "shadow_final_direction": shadow_final,
        "shadow_reason": ";".join(dict.fromkeys(reason_parts)),
        "entry": entry,
        "base_sl": geometry["base_sl"],
        "base_tp": geometry["base_tp"],
        "shadow_geometry_type": geometry["shadow_geometry_type"],
        "shadow_sl": shadow_sl,
        "shadow_tp": shadow_tp,
        "geometry_valid": geometry["geometry_valid"],
        "spread": execution_ctx.get("spread_pct"),
        "expected_slippage": execution_ctx.get("expected_slippage_pct"),
        "latency": execution_ctx.get("latency_ms", execution_ctx.get("market_data_latency_ms")),
        "fee_assumption": execution_ctx.get("fee_pct"),
        "funding_assumption": execution_ctx.get("funding_rate_pct"),
        "raw_rr_if_executed": raw_rr,
        "effective_rr_if_executed": costs.effective_rr if costs else None,
        "execution_costs": costs.as_dict() if costs else {},
        "regime": (regime or {}).get("regime") or market_ctx.get("regime"),
        "setup_phase": setup_phase,
        "timeframe": timeframe,
        "horizon_bars": int(horizon_bars),
        "due_at": due_at,
        "source_provenance": {
            "provider": mtf.get("provider"),
            "source_exchange": market_ctx.get("source_exchange"),
            "signal_id": signal_id,
            "observational_only": True,
            "state_direction_resolution_enabled_for_actual": False,
            "generation_mode": alignment.get("generation_mode"),
        },
        "evidence_complete": evidence_complete,
    }


@dataclass
class StateDirectionShadowStore:
    database: str | Path | sqlite3.Connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if isinstance(self.database, sqlite3.Connection):
            self.database.row_factory = sqlite3.Row
            yield self.database
            return
        path = Path(self.database)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _bootstrap(conn: sqlite3.Connection) -> None:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name IN ('burnin_campaigns','burnin_runs') LIMIT 1"
        ).fetchone():
            raise ValueError("state-direction shadow output contains campaign tables")
        bootstrap_shadow_schema(conn)

    def record(self, draft: Mapping[str, Any], *, actual_decision: str,
               actual_side: str | None, actual_reject_reason: str | None) -> str:
        with self.connection() as conn:
            self._bootstrap(conn)
            values = {
                **dict(draft),
                "actual_decision": str(actual_decision).upper(),
                "actual_side": str(actual_side).upper() if actual_side else None,
                "actual_reject_reason": actual_reject_reason,
                "geometry_valid": int(bool(draft.get("geometry_valid"))),
                "evidence_complete": int(bool(draft.get("evidence_complete"))),
                "execution_costs_json": json.dumps(
                    draft.get("execution_costs") or {}, sort_keys=True),
                "source_provenance_json": json.dumps(
                    draft.get("source_provenance") or {}, sort_keys=True),
                "updated_at": utc_now(),
                "schema_version": SCHEMA_VERSION,
            }
            columns = (
                "shadow_decision_id observed_at symbol decision_timestamp signal_id "
                "base_exec_direction regime_direction setup_direction resolved_state "
                "shadow_final_direction shadow_reason actual_decision actual_side "
                "actual_reject_reason entry base_sl base_tp shadow_geometry_type "
                "shadow_sl shadow_tp geometry_valid spread expected_slippage latency "
                "fee_assumption funding_assumption raw_rr_if_executed "
                "effective_rr_if_executed execution_costs_json regime setup_phase "
                "timeframe horizon_bars due_at source_provenance_json evidence_complete "
                "updated_at schema_version"
            ).split()
            placeholders = ",".join(f":{column}" for column in columns)
            updates = ",".join(
                f"{column}=excluded.{column}" for column in columns
                if column not in {"shadow_decision_id", "observed_at"}
            )
            conn.execute(
                f"INSERT INTO state_direction_shadow_decisions ({','.join(columns)}) "
                f"VALUES ({placeholders}) ON CONFLICT(shadow_decision_id) DO UPDATE SET {updates}",
                values,
            )
            conn.commit()
        return str(draft["shadow_decision_id"])


def resolve_state_direction_shadow_outcomes(
    conn: sqlite3.Connection,
    candles_by_symbol: Mapping[Any, Sequence[Mapping[str, Any]]],
    *,
    now: str | None = None,
) -> dict[str, int]:
    StateDirectionShadowStore._bootstrap(conn)
    conn.row_factory = sqlite3.Row
    current = _timestamp(now or utc_now())
    counts = {"resolved": 0, "incomplete": 0, "ambiguous": 0, "pending": 0}
    rows = conn.execute(
        """SELECT d.* FROM state_direction_shadow_decisions d
        LEFT JOIN state_direction_shadow_outcomes o
          ON o.shadow_decision_id=d.shadow_decision_id
        WHERE o.shadow_decision_id IS NULL OR o.outcome_status='INCOMPLETE'
        ORDER BY d.decision_timestamp,d.shadow_decision_id"""
    ).fetchall()
    for raw in rows:
        row = dict(raw)
        due = _timestamp(row.get("due_at"))
        if due is None or current is None or current < due:
            counts["pending"] += 1
            continue
        side = {
            "WOULD_LONG": "LONG", "WOULD_SHORT": "SHORT",
            "WOULD_NO_TRADE": str(row.get("actual_side") or "").upper(),
        }.get(row["shadow_final_direction"], "")
        candles = (
            candles_by_symbol.get((row["symbol"], row.get("timeframe")))
            or candles_by_symbol.get(row["symbol"])
            or []
        )
        evaluated = evaluate_forward_outcome(
            side=side, entry=row.get("entry"), stop=row.get("shadow_sl"),
            target=row.get("shadow_tp"),
            decision_timestamp=row["decision_timestamp"], due_at=row["due_at"],
            timeframe=row.get("timeframe"), horizon_bars=row.get("horizon_bars"),
            candles=candles,
        )
        try:
            costs = json.loads(row.get("execution_costs_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            costs = {}
        cost_drag = _finite(costs.get("cost_penalty_rr"))
        gross = evaluated.get("gross_r")
        net_r = (
            float(gross) - float(cost_drag)
            if gross is not None and cost_drag is not None else None
        )
        complete = bool(
            row.get("evidence_complete")
            and evaluated.get("evidence_complete")
            and net_r is not None
        )
        ambiguous = bool(evaluated.get("ambiguous"))
        status = "AMBIGUOUS" if ambiguous else "RESOLVED" if complete else "INCOMPLETE"
        no_trade = row["shadow_final_direction"] == "WOULD_NO_TRADE"
        evidence = {
            **evaluated,
            "execution_costs": costs,
            "original_candidate_evaluated": no_trade,
        }
        outcome_id = "sdso_" + canonical_hash(row["shadow_decision_id"])[:32]
        conn.execute(
            """INSERT INTO state_direction_shadow_outcomes
            (shadow_outcome_id,shadow_decision_id,outcome_status,forward_label,mfe,mae,
             gross_r,cost_adjusted_net_r,total_cost_drag,avoided_loss,missed_profit,
             ambiguous,evidence_complete,resolved_at,evidence_json,schema_version)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(shadow_decision_id) DO UPDATE SET
             outcome_status=excluded.outcome_status,forward_label=excluded.forward_label,
             mfe=excluded.mfe,mae=excluded.mae,gross_r=excluded.gross_r,
             cost_adjusted_net_r=excluded.cost_adjusted_net_r,
             total_cost_drag=excluded.total_cost_drag,avoided_loss=excluded.avoided_loss,
             missed_profit=excluded.missed_profit,ambiguous=excluded.ambiguous,
             evidence_complete=excluded.evidence_complete,resolved_at=excluded.resolved_at,
             evidence_json=excluded.evidence_json,schema_version=excluded.schema_version""",
            (
                outcome_id, row["shadow_decision_id"], status,
                evaluated.get("forward_label"), evaluated.get("mfe"), evaluated.get("mae"),
                gross, net_r, cost_drag,
                max(0.0, -net_r) if no_trade and net_r is not None else None,
                max(0.0, net_r) if no_trade and net_r is not None else None,
                int(ambiguous), int(complete), utc_now(),
                json.dumps(evidence, sort_keys=True), SCHEMA_VERSION,
            ),
        )
        counts["ambiguous" if ambiguous else "resolved" if complete else "incomplete"] += 1
    conn.commit()
    return counts


def state_direction_shadow_report(
    conn: sqlite3.Connection, *, group_by: Sequence[str] = (),
) -> list[dict[str, Any]]:
    invalid = set(group_by) - REPORT_GROUPS
    if invalid:
        raise ValueError(f"unsupported shadow report group(s): {sorted(invalid)}")
    conn.row_factory = sqlite3.Row
    rows = [dict(row) for row in conn.execute(
        """SELECT d.*,o.outcome_status,o.forward_label,o.mfe,o.mae,
                  o.cost_adjusted_net_r,o.total_cost_drag,o.avoided_loss,
                  o.missed_profit,o.ambiguous,o.evidence_complete AS outcome_complete
           FROM state_direction_shadow_decisions d
           LEFT JOIN state_direction_shadow_outcomes o
             ON o.shadow_decision_id=d.shadow_decision_id"""
    )]
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(tuple(row.get(key) for key in group_by), []).append(row)
    report = []
    for key, samples in sorted(groups.items(), key=lambda item: str(item[0])):
        net_values = [float(r["cost_adjusted_net_r"]) for r in samples
                      if r.get("cost_adjusted_net_r") is not None]
        resolved = sum(bool(r.get("outcome_complete")) for r in samples)
        ambiguous = sum(bool(r.get("ambiguous")) for r in samples)
        overrides = sum(
            r["shadow_final_direction"] == "WOULD_NO_TRADE"
            or r["shadow_final_direction"].removeprefix("WOULD_") != r.get("base_exec_direction")
            for r in samples
        )
        result = {name: value for name, value in zip(group_by, key)}
        result.update({
            "samples": len(samples), "resolved": resolved,
            "incomplete": len(samples) - resolved,
            "WOULD_LONG": sum(r["shadow_final_direction"] == "WOULD_LONG" for r in samples),
            "WOULD_SHORT": sum(r["shadow_final_direction"] == "WOULD_SHORT" for r in samples),
            "WOULD_NO_TRADE": sum(r["shadow_final_direction"] == "WOULD_NO_TRADE" for r in samples),
            "actual_accepts": sum(r["actual_decision"] == "ACCEPTED" for r in samples),
            "actual_rejects": sum(r["actual_decision"] != "ACCEPTED" for r in samples),
            "override_count": overrides,
            "override_rate": overrides / len(samples) if samples else 0.0,
            "net_r_sum": sum(net_values),
            "mean_net_r": statistics.fmean(net_values) if net_values else None,
            "median_net_r": statistics.median(net_values) if net_values else None,
            "wins": sum(value > 0 for value in net_values),
            "losses": sum(value < 0 for value in net_values),
            "total_cost_drag": sum(float(r.get("total_cost_drag") or 0) for r in samples),
            "mean_mfe": statistics.fmean(
                float(r["mfe"]) for r in samples if r.get("mfe") is not None
            ) if any(r.get("mfe") is not None for r in samples) else None,
            "mean_mae": statistics.fmean(
                float(r["mae"]) for r in samples if r.get("mae") is not None
            ) if any(r.get("mae") is not None for r in samples) else None,
            "avoided_losses": sum(float(r.get("avoided_loss") or 0) for r in samples),
            "missed_profits": sum(float(r.get("missed_profit") or 0) for r in samples),
            "ambiguity_rate": ambiguous / len(samples) if samples else 0.0,
        })
        report.append(result)
    return report
