from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alphaforge.contracts import validate_transition

CLASS_LIVE_BLOCKED = "LIVE_BLOCKED"
TERMINAL_LIVE_READINESS = "NOT_LIVE_READY"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _count(conn: sqlite3.Connection, table: str, where: str = "1=1") -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0] or 0)


def _dist(conn: sqlite3.Connection, table: str, col: str, where: str = "1=1") -> dict[str, int]:
    if not _table_exists(conn, table) or col not in _columns(conn, table):
        return {}
    return {str(k or "<NULL>"): int(v) for k, v in conn.execute(f"SELECT COALESCE(NULLIF(TRIM({col}), ''), '<EMPTY>'), COUNT(*) FROM {table} WHERE {where} GROUP BY 1 ORDER BY 2 DESC, 1").fetchall()}


def _numeric_summary(conn: sqlite3.Connection, table: str, col: str, where: str = "1=1") -> dict[str, Any]:
    if not _table_exists(conn, table) or col not in _columns(conn, table):
        return {"available": False, "count": 0}
    row = conn.execute(f"SELECT COUNT({col}), MIN({col}), AVG({col}), MAX({col}) FROM {table} WHERE {where}").fetchone()
    distinct = conn.execute(f"SELECT COUNT(DISTINCT {col}) FROM {table} WHERE {where} AND {col} IS NOT NULL").fetchone()[0]
    return {"available": True, "count": int(row[0] or 0), "min": row[1], "avg": row[2], "max": row[3], "distinct": int(distinct or 0)}


def _where_mode(table_cols: set[str], mode: str = "PAPER") -> str:
    return f"UPPER(mode)='{mode}'" if "mode" in table_cols else "1=1"


def _lifecycle_errors(conn: sqlite3.Connection) -> tuple[int, list[dict[str, Any]]]:
    if not _table_exists(conn, "trade_lifecycle_events"):
        return 0, []
    cols = _columns(conn, "trade_lifecycle_events")
    state_col = "lifecycle_state" if "lifecycle_state" in cols else "state"
    mode_filter = _where_mode(cols)
    order_col = "event_ts" if "event_ts" in cols else "created_at"
    rows = conn.execute(f"SELECT signal_id, {state_col} AS state, {order_col} AS ts FROM trade_lifecycle_events WHERE {mode_filter} ORDER BY signal_id, {order_col}, id").fetchall()
    by_signal: dict[str, list[tuple[str, Any]]] = defaultdict(list)
    for sid, state, ts in rows:
        by_signal[str(sid or "<NULL>")].append((str(state or ""), ts))
    errors: list[dict[str, Any]] = []
    for sid, events in by_signal.items():
        prev: str | None = None
        for state, ts in events:
            if not validate_transition(prev, state):
                errors.append({"signal_id": sid, "previous": prev, "next": state, "event_ts": ts})
            prev = state
    return len(errors), errors[:25]


def generate_paper_burnin_report(db_path: str | Path, output_dir: str | Path) -> dict[str, Any]:
    db_path = Path(db_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        blockers: list[dict[str, str]] = []
        classifications: set[str] = {CLASS_LIVE_BLOCKED}
        required = ["order_decisions", "trade_lifecycle_events"]
        for table in required:
            if not _table_exists(conn, table):
                blockers.append({"classification": "DATA_INTEGRITY_FAILURE", "blocker": f"missing_table:{table}"})
                classifications.add("DATA_INTEGRITY_FAILURE")

        od_cols = _columns(conn, "order_decisions")
        tl_cols = _columns(conn, "trade_lifecycle_events")
        od_where = _where_mode(od_cols)
        tl_where = _where_mode(tl_cols)
        total = _count(conn, "order_decisions", od_where)
        accepted = _count(conn, "order_decisions", f"{od_where} AND UPPER(COALESCE(decision,''))='ACCEPTED'")
        rejected = _count(conn, "order_decisions", f"{od_where} AND UPPER(COALESCE(decision,''))='REJECTED'")
        reject_rate = rejected / total if total else None
        if total == 0:
            classifications.add("INSUFFICIENT_SAMPLE")
            blockers.append({"classification": "INSUFFICIENT_SAMPLE", "blocker": "no_paper_order_decisions"})
        missing_reject = _count(conn, "order_decisions", f"{od_where} AND UPPER(COALESCE(decision,''))='REJECTED' AND COALESCE(NULLIF(TRIM(reject_reason),''),'')=''" ) if "reject_reason" in od_cols else rejected
        if missing_reject:
            classifications.add("DATA_INTEGRITY_FAILURE")
            blockers.append({"classification": "DATA_INTEGRITY_FAILURE", "blocker": f"missing_reject_reason_count:{missing_reject}"})
        lifecycle_error_count, lifecycle_error_examples = _lifecycle_errors(conn)
        if lifecycle_error_count:
            classifications.add("LIFECYCLE_INTEGRITY_FAILURE")
            blockers.append({"classification": "LIFECYCLE_INTEGRITY_FAILURE", "blocker": f"lifecycle_ordering_errors:{lifecycle_error_count}"})
        dup_signal_rows = conn.execute(f"SELECT COUNT(*) FROM (SELECT signal_id FROM order_decisions WHERE {od_where} AND signal_id IS NOT NULL GROUP BY signal_id HAVING COUNT(*) > 1)").fetchone()[0] if _table_exists(conn,"order_decisions") and "signal_id" in od_cols else 0
        dup_order_rows = conn.execute(f"SELECT COUNT(*) FROM (SELECT order_id FROM order_decisions WHERE {od_where} AND order_id IS NOT NULL AND TRIM(order_id)<>'' GROUP BY order_id HAVING COUNT(*) > 1)").fetchone()[0] if _table_exists(conn,"order_decisions") and "order_id" in od_cols else 0
        if dup_signal_rows or dup_order_rows:
            classifications.add("DATA_INTEGRITY_FAILURE")
            blockers.append({"classification":"DATA_INTEGRITY_FAILURE","blocker":f"duplicate_signal_ids:{dup_signal_rows},duplicate_order_ids:{dup_order_rows}"})

        exec_cols = ["execution_ctx", "execution_ctx_missing"]
        missing_exec = 0
        if "execution_ctx_missing" in od_cols:
            missing_exec += _count(conn, "order_decisions", f"{od_where} AND COALESCE(execution_ctx_missing,0)<>0")
        if "execution_ctx" in od_cols:
            missing_exec += _count(conn, "order_decisions", f"{od_where} AND (execution_ctx IS NULL OR TRIM(execution_ctx)='' OR TRIM(execution_ctx)='{{}}')")
        else:
            missing_exec = total
        if missing_exec:
            classifications.add("EXECUTION_CONTEXT_FAILURE")
            blockers.append({"classification":"EXECUTION_CONTEXT_FAILURE","blocker":f"missing_execution_context_evidence:{missing_exec}"})

        availability = {}
        fake_zero_count = 0
        for col in ["spread_pct", "expected_slippage_pct", "funding_rate_pct", "liquidity_score"]:
            source_table = "rejected_signal_reviews" if _table_exists(conn, "rejected_signal_reviews") and col in _columns(conn, "rejected_signal_reviews") else "order_decisions"
            available = _table_exists(conn, source_table) and col in _columns(conn, source_table)
            cnt = _count(conn, source_table, f"{col} IS NOT NULL") if available else 0
            zeros = _count(conn, source_table, f"{col}=0") if available else 0
            availability[col] = {"column_available": available, "non_null_count": cnt, "zero_count": zeros}
            fake_zero_count += zeros if cnt and zeros == cnt else 0
        if fake_zero_count:
            classifications.add("EXECUTION_CONTEXT_FAILURE")
            blockers.append({"classification":"EXECUTION_CONTEXT_FAILURE","blocker":f"fake_zero_execution_fields:{fake_zero_count}"})

        if total and not any(c.endswith("FAILURE") for c in classifications):
            classifications.add("HEALTHY_SELECTIVITY")
        # Optional evidence blockers for live only.
        for blocker in ["readiness_evidence_incomplete", "live_reconciliation_not_proven", "operator_ack_not_part_of_burnin"]:
            blockers.append({"classification": CLASS_LIVE_BLOCKED, "blocker": blocker})

        timesfm_count = _count(conn, "timesfm_forecast_evidence", "UPPER(mode)='PAPER'" if "mode" in _columns(conn,"timesfm_forecast_evidence") else "1=1")
        timesfm_outcomes = _dist(conn, "timesfm_forward_outcome_labels", "outcome")
        if timesfm_count == 0:
            blockers.append({"classification":"LIVE_BLOCKED", "blocker":"timesfm_evidence_absent_optional_not_fatal"})

        heartbeat_count = _count(conn, "runtime_heartbeats", "UPPER(execution_mode)='PAPER'" if "execution_mode" in _columns(conn,"runtime_heartbeats") else "1=1")
        if heartbeat_count == 0:
            classifications.add("OBSERVABILITY_FAILURE")
            blockers.append({"classification":"OBSERVABILITY_FAILURE","blocker":"missing_paper_heartbeat_evidence"})

        incidents = _count(conn, "trade_lifecycle_events", f"{tl_where} AND (UPPER(COALESCE(lifecycle_state,'')) IN ('ERROR','EXECUTION_ERROR','EXCHANGE_REJECT') OR UPPER(COALESCE(event_type,'')) LIKE '%ERROR%')")
        kill_switch = _count(conn, "trade_lifecycle_events", f"{tl_where} AND (UPPER(COALESCE(reject_reason,'')) LIKE '%KILL_SWITCH%' OR UPPER(COALESCE(payload,'')) LIKE '%KILL_SWITCH%')")
        control_table = "runtime_control_audit_events" if _table_exists(conn, "runtime_control_audit_events") else "runtime_control_events"
        control_cols = _columns(conn, control_table)
        switch_terms = []
        if "event_type" in control_cols:
            switch_terms.append("UPPER(COALESCE(event_type,'')) LIKE '%SWITCH%'")
        if "action" in control_cols:
            switch_terms.append("UPPER(COALESCE(action,'')) LIKE '%SWITCH%'")
        dashboard_switch_attempts = _count(conn, control_table, " OR ".join(switch_terms)) if switch_terms else 0
        reconciliation_count = _count(conn, "trade_lifecycle_events", f"{tl_where} AND UPPER(COALESCE(lifecycle_state,''))='RECONCILIATION_REPAIR'")
        if reconciliation_count == 0:
            classifications.add("RECONCILIATION_FAILURE")
            blockers.append({"classification":"RECONCILIATION_FAILURE","blocker":"reconciliation_evidence_missing"})

        ts_values = []
        for table, cols, where in [("order_decisions", od_cols, od_where), ("trade_lifecycle_events", tl_cols, tl_where)]:
            for col in ("created_at", "event_ts", "updated_at"):
                if _table_exists(conn, table) and col in cols:
                    for (v,) in conn.execute(f"SELECT {col} FROM {table} WHERE {where} AND {col} IS NOT NULL"):
                        parsed = _parse_ts(v)
                        if parsed: ts_values.append(parsed)
        duration_seconds = (max(ts_values) - min(ts_values)).total_seconds() if len(ts_values) >= 2 else None

        report = {
            "generated_at": _utc_now(), "database": str(db_path), "runtime_mode": "PAPER", "runtime_duration_seconds": duration_seconds,
            "total_signals": total, "accepted_count": accepted, "rejected_count": rejected, "reject_rate": reject_rate,
            "reject_reason_distribution": _dist(conn, "order_decisions", "reject_reason", f"{od_where} AND UPPER(COALESCE(decision,''))='REJECTED'"),
            "missing_reject_reason_count": missing_reject, "lifecycle_state_distribution": _dist(conn, "trade_lifecycle_events", "lifecycle_state", tl_where),
            "lifecycle_ordering_errors": lifecycle_error_count, "lifecycle_ordering_error_examples": lifecycle_error_examples,
            "duplicate_signal_id_issues": int(dup_signal_rows or 0), "duplicate_order_id_issues": int(dup_order_rows or 0),
            "score_distribution": _numeric_summary(conn, "order_decisions", "score", od_where), "raw_rr_distribution": _numeric_summary(conn, "order_decisions", "rr", od_where), "effective_rr_distribution": _numeric_summary(conn, "order_decisions", "effective_rr", od_where),
            "effective_rr_differs_from_raw_rr_count": _count(conn, "order_decisions", f"{od_where} AND rr IS NOT NULL AND effective_rr IS NOT NULL AND ABS(effective_rr-rr) > 0.0000001") if {"rr","effective_rr"}.issubset(od_cols) else 0,
            "execution_context_completeness": {"missing_or_flagged_count": missing_exec, "total_decisions": total}, "fake_zero_detection": {"fake_zero_count": fake_zero_count},
            "execution_field_availability": availability, "timesfm_forecast_evidence_count": timesfm_count, "timesfm_calibration_outcome_summary": timesfm_outcomes,
            "heartbeat_uptime_gaps": {"paper_heartbeat_count": heartbeat_count, "status": "PRESENT" if heartbeat_count else "MISSING"}, "incident_count": incidents, "kill_switch_events": kill_switch, "dashboard_switch_attempts": dashboard_switch_attempts,
            "reconciliation_evidence_status": "PRESENT" if reconciliation_count else "MISSING", "readiness_blockers": blockers,
            "classification": sorted(classifications), "live_readiness": TERMINAL_LIVE_READINESS,
        }
        _write_outputs(report, output_dir)
        return report
    finally:
        conn.close()


def _write_outputs(report: dict[str, Any], output_dir: Path) -> None:
    with (output_dir / "paper_burnin_summary.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["metric", "value"])
        for key in ["generated_at","database","runtime_duration_seconds","total_signals","accepted_count","rejected_count","reject_rate","missing_reject_reason_count","lifecycle_ordering_errors","duplicate_signal_id_issues","duplicate_order_id_issues","effective_rr_differs_from_raw_rr_count","incident_count","kill_switch_events","dashboard_switch_attempts","reconciliation_evidence_status","classification","live_readiness"]:
            writer.writerow([key, json.dumps(report[key]) if isinstance(report[key], (list, dict)) else report[key]])
    (output_dir / "paper_burnin_blockers.json").write_text(json.dumps({"classification": report["classification"], "live_readiness": report["live_readiness"], "blockers": report["readiness_blockers"]}, indent=2, sort_keys=True), encoding="utf-8")
    lines = ["# PAPER Burn-In Report", "", f"Generated: {report['generated_at']}", "", "## Verdict", "", f"- Classification: {', '.join(report['classification'])}", f"- Live readiness: {report['live_readiness']} (burn-in reports never promote LIVE readiness)", "", "## Summary", ""]
    for key in ["runtime_duration_seconds","total_signals","accepted_count","rejected_count","reject_rate","missing_reject_reason_count","lifecycle_ordering_errors","duplicate_signal_id_issues","duplicate_order_id_issues","effective_rr_differs_from_raw_rr_count","timesfm_forecast_evidence_count","incident_count","kill_switch_events","dashboard_switch_attempts","reconciliation_evidence_status"]:
        lines.append(f"- {key}: {report[key]}")
    lines += ["", "## Readiness Blockers", ""]
    lines += [f"- {b['classification']}: {b['blocker']}" for b in report["readiness_blockers"]]
    lines += ["", "## Distributions", "", "```json", json.dumps({k: report[k] for k in ["reject_reason_distribution","lifecycle_state_distribution","score_distribution","raw_rr_distribution","effective_rr_distribution","execution_field_availability","timesfm_calibration_outcome_summary"]}, indent=2, sort_keys=True), "```", ""]
    (output_dir / "paper_burnin_report.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic SQL-first PAPER burn-in diagnostics.")
    parser.add_argument("--db", required=True, help="Path to the PAPER runtime SQLite database")
    parser.add_argument("--out", default="reports/paper_burnin", help="Output directory for CSV/Markdown/JSON report artifacts")
    args = parser.parse_args(argv)
    report = generate_paper_burnin_report(args.db, args.out)
    print(json.dumps({"classification": report["classification"], "live_readiness": report["live_readiness"], "output_dir": args.out}, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
