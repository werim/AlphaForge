"""Level 2 evidence-gated recommendations and reporting for System Audit Fabric.

This module is deliberately downstream of the immutable audit store and Level 1
diagnostics. It may recommend offline/shadow investigation, but it never writes
runtime configuration, never submits orders, and never mutates source evidence.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

from alphaforge.burnin import canonical_hash, utc_now
from alphaforge.system_audit_diagnostics import (
    bootstrap_diagnostics_schema,
    ingest_operational_evidence,
    run_system_diagnostics,
)
from alphaforge.system_audit_store import (
    SCHEMA_VERSION,
    ingest_audit_evidence,
)


PROTECTED_CONTROLS = frozenset({
    "DATA_FRESHNESS",
    "PROVIDER_INTEGRITY",
    "RECONCILIATION_INTEGRITY",
    "HARD_POSITION_LIMIT",
    "PORTFOLIO_RISK_LIMIT",
    "CORRELATION_RISK_LIMIT",
    "ALPHAFORGE_MAX_CONCURRENT_POSITIONS",
    "ALPHAFORGE_MAX_NOTIONAL_EXPOSURE",
    "ALPHAFORGE_MAX_SYMBOL_NOTIONAL",
    "ALPHAFORGE_MAX_SPREAD_PCT",
    "ALPHAFORGE_MAX_EXPECTED_SLIPPAGE_PCT",
    "ALPHAFORGE_MAX_TOTAL_COST_PCT",
    "ALPHAFORGE_MIN_LIQUIDITY_SCORE",
})

REJECT_REASON_CONTROLS: dict[str, str] = {
    "LOW_EFFECTIVE_RR": "MIN_EFFECTIVE_RR",
    "RR_TOO_LOW": "MIN_EFFECTIVE_RR",
    "LOW_SCORE": "ALPHAFORGE_MIN_SIGNAL_SCORE",
    "LOW_CONFIDENCE": "ALPHAFORGE_MIN_SIGNAL_SCORE",
    "REGIME_MISMATCH": "ALPHAFORGE_REQUIRE_REGIME_ALIGNMENT",
    "MTF_EXECUTION_NOT_CONFIRMED": "MTF_EXECUTION_CONFIRMATION_POLICY",
    "STOP_TOO_WIDE": "STOP_TOO_WIDE_POLICY",
    "HIGH_SPREAD": "ALPHAFORGE_MAX_SPREAD_PCT",
    "HIGH_SLIPPAGE": "ALPHAFORGE_MAX_EXPECTED_SLIPPAGE_PCT",
    "BAD_EXECUTION": "EXECUTION_MODEL",
    "THIN_LIQUIDITY": "ALPHAFORGE_MIN_LIQUIDITY_SCORE",
    "LOW_LIQUIDITY": "ALPHAFORGE_MIN_LIQUIDITY_SCORE",
    "EXCESSIVE_VOLATILITY": "ALPHAFORGE_MAX_VOLATILITY_PENALTY_PCT",
    "CORRELATION_OVEREXPOSURE": "CORRELATION_RISK_LIMIT",
    "STALE_MARKET_DATA": "DATA_FRESHNESS",
    "PROVIDER_FAILURE": "PROVIDER_INTEGRITY",
    "RECONCILIATION_FAILURE": "RECONCILIATION_INTEGRITY",
}

CONTROL_THRESHOLD_KEYS: dict[str, tuple[str, ...]] = {
    "MIN_EFFECTIVE_RR": ("MIN_EFFECTIVE_RR", "LOW_EFFECTIVE_RR"),
    "ALPHAFORGE_MIN_SIGNAL_SCORE": ("MIN_TRADE_SCORE", "LOW_SCORE", "LOW_CONFIDENCE"),
    "ALPHAFORGE_MAX_SPREAD_PCT": ("HIGH_SPREAD", "MAX_SPREAD_PCT"),
    "ALPHAFORGE_MAX_EXPECTED_SLIPPAGE_PCT": ("HIGH_SLIPPAGE", "MAX_EXPECTED_SLIPPAGE_PCT"),
    "ALPHAFORGE_MIN_LIQUIDITY_SCORE": ("THIN_LIQUIDITY", "LOW_LIQUIDITY", "MIN_LIQUIDITY_SCORE"),
    "ALPHAFORGE_MAX_VOLATILITY_PENALTY_PCT": ("EXCESSIVE_VOLATILITY", "MAX_VOLATILITY_PENALTY_PCT"),
}

SEVERITY_RANK = {"INFO": 0, "WATCH": 1, "WARNING": 2, "CRITICAL": 3, "BLOCKING": 4}

DDL = (
    """CREATE TABLE IF NOT EXISTS audit_calibration_stats(
        calibration_stat_id TEXT PRIMARY KEY,
        audit_run_id TEXT NOT NULL,
        dimension TEXT NOT NULL,
        cohort_key TEXT NOT NULL,
        sample_count INTEGER NOT NULL,
        mean_net_r REAL,
        lower_confidence_bound REAL,
        upper_confidence_bound REAL,
        recommendation_gate TEXT NOT NULL,
        recommendation_state TEXT NOT NULL,
        target_control TEXT,
        current_value REAL,
        protected_control INTEGER NOT NULL,
        relaxation_supported INTEGER NOT NULL,
        tightening_supported INTEGER NOT NULL,
        evidence_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        UNIQUE(audit_run_id,dimension,cohort_key)
    )""",
    """CREATE TABLE IF NOT EXISTS audit_policy_candidates(
        candidate_id TEXT PRIMARY KEY,
        audit_run_id TEXT NOT NULL,
        calibration_stat_id TEXT NOT NULL,
        target_control TEXT NOT NULL,
        action TEXT NOT NULL,
        recommendation_scope TEXT NOT NULL,
        current_value REAL,
        proposed_value REAL,
        protected_control INTEGER NOT NULL,
        offline_replay_required INTEGER NOT NULL,
        shadow_policy_required INTEGER NOT NULL,
        production_mutation_allowed INTEGER NOT NULL,
        rationale_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        UNIQUE(audit_run_id,calibration_stat_id,action)
    )""",
    """CREATE TABLE IF NOT EXISTS audit_reports(
        report_id TEXT PRIMARY KEY,
        audit_run_id TEXT NOT NULL,
        evidence_hash TEXT NOT NULL,
        report_json TEXT NOT NULL,
        human_summary TEXT NOT NULL,
        created_at TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        UNIQUE(audit_run_id,evidence_hash)
    )""",
)

REPORTING_TABLES = (
    "audit_calibration_stats",
    "audit_policy_candidates",
    "audit_reports",
)


def _trigger(table: str, operation: str) -> str:
    return f"""CREATE TRIGGER IF NOT EXISTS trg_{table}_no_{operation.lower()}
        BEFORE {operation} ON {table}
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_SYSTEM_AUDIT_REPORTING'); END"""


def bootstrap_reporting_schema(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    bootstrap_diagnostics_schema(conn)
    for statement in DDL:
        conn.execute(statement)
    for table in REPORTING_TABLES:
        conn.execute(_trigger(table, "UPDATE"))
        conn.execute(_trigger(table, "DELETE"))


def recommendation_gate(sample_count: int) -> str:
    count = max(0, int(sample_count))
    if count < 30:
        return "NO_CHANGE"
    if count < 100:
        return "OBSERVE_ONLY"
    if count < 250:
        return "SMALL_RECOMMENDATION_ALLOWED"
    return "NORMAL_RECOMMENDATION_ALLOWED"


def _json(raw: Any, default: Any) -> Any:
    if isinstance(raw, type(default)):
        return raw
    try:
        parsed = json.loads(raw if raw not in (None, "") else json.dumps(default))
    except (TypeError, json.JSONDecodeError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _latest_audit_run(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT audit_run_id FROM audit_analysis_runs ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if row is None:
        result = run_system_diagnostics(conn)
        return str(result["audit_run_id"])
    return str(row["audit_run_id"])


def _target_control(dimension: str, cohort_key: str) -> str | None:
    dimension_u = str(dimension or "").strip().lower()
    key_u = str(cohort_key or "").strip().upper()
    if dimension_u == "reject_reason":
        return REJECT_REASON_CONTROLS.get(key_u)
    if dimension_u == "decision" and key_u == "ACCEPT":
        return "ACCEPTANCE_POLICY"
    return None


def _current_control_value(conn: sqlite3.Connection, target_control: str | None) -> float | None:
    if not target_control:
        return None
    keys = CONTROL_THRESHOLD_KEYS.get(target_control, (target_control,))
    for row in conn.execute(
        "SELECT gate_thresholds_json FROM audit_decision_envelopes "
        "ORDER BY decision_time DESC,rowid DESC"
    ):
        thresholds = _json(row["gate_thresholds_json"], {})
        for key in keys:
            value = _finite(thresholds.get(key))
            if value is not None:
                return value
    return None


def _recommendation_state(
    row: Mapping[str, Any],
    *,
    target_control: str | None,
    protected: bool,
) -> tuple[str, bool, bool]:
    sample_count = int(row.get("sample_count") or 0)
    gate = recommendation_gate(sample_count)
    if gate == "NO_CHANGE":
        return "NO_CHANGE_INSUFFICIENT_EVIDENCE", False, False
    if gate == "OBSERVE_ONLY":
        return "OBSERVE_ONLY", False, False

    lower = _finite(row.get("lower_confidence_bound"))
    upper = _finite(row.get("upper_confidence_bound"))
    dimension = str(row.get("dimension") or "").lower()
    key = str(row.get("cohort_key") or "").upper()

    if target_control is None:
        return "NO_ACTIONABLE_CONTROL", False, False

    if dimension == "reject_reason":
        if lower is not None and lower > 0:
            if protected:
                return "PROTECTED_NO_RELAXATION", False, False
            return "RELAX_REVIEW", True, False
        if upper is not None and upper < 0:
            return "KEEP_PROTECTIVE_GATE", False, False
        return "NO_CHANGE_UNCERTAIN", False, False

    if dimension == "decision" and key == "ACCEPT":
        if upper is not None and upper < 0:
            return "TIGHTEN_REVIEW", False, True
        if lower is not None and lower > 0:
            return "KEEP_ACCEPTANCE_POLICY", False, False
        return "NO_CHANGE_UNCERTAIN", False, False

    return "NO_CHANGE_UNCERTAIN", False, False


def build_calibration_recommendations(
    conn: sqlite3.Connection,
    *,
    audit_run_id: str | None = None,
) -> dict[str, Any]:
    """Persist evidence-gated Level 2 calibration stats and offline-only candidates."""
    bootstrap_reporting_schema(conn)
    run_id = str(audit_run_id or _latest_audit_run(conn))
    rows = [
        dict(row) for row in conn.execute(
            "SELECT * FROM audit_cohort_stats WHERE audit_run_id=? "
            "ORDER BY dimension,cohort_key",
            (run_id,),
        )
    ]
    stats_added = 0
    candidates_added = 0
    protected_blocks = 0

    for row in rows:
        target = _target_control(str(row["dimension"]), str(row["cohort_key"]))
        protected = bool(target in PROTECTED_CONTROLS)
        state, relax_supported, tighten_supported = _recommendation_state(
            row, target_control=target, protected=protected
        )
        gate = recommendation_gate(int(row["sample_count"]))
        current_value = _current_control_value(conn, target)
        evidence = {
            "sample_count": int(row["sample_count"]),
            "mean_net_r": row["mean_net_r"],
            "lower_confidence_bound": row["lower_confidence_bound"],
            "upper_confidence_bound": row["upper_confidence_bound"],
            "positive_count": int(row["positive_count"]),
            "negative_count": int(row["negative_count"]),
            "mean_expected_vs_realized_r_delta": row["mean_expected_vs_realized_r_delta"],
            "mean_abs_fill_model_error_pct": row["mean_abs_fill_model_error_pct"],
            "recommendation_rule": "POST_COST_EXPECTANCY_WITH_CONFIDENCE_BOUNDS",
        }
        stat_id = "acal_" + canonical_hash([
            run_id, row["dimension"], row["cohort_key"], evidence, state, target
        ])[:28]
        before = conn.total_changes
        conn.execute(
            """INSERT OR IGNORE INTO audit_calibration_stats(
                calibration_stat_id,audit_run_id,dimension,cohort_key,sample_count,mean_net_r,
                lower_confidence_bound,upper_confidence_bound,recommendation_gate,
                recommendation_state,target_control,current_value,protected_control,
                relaxation_supported,tightening_supported,evidence_json,created_at,schema_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                stat_id,run_id,row["dimension"],row["cohort_key"],int(row["sample_count"]),
                row["mean_net_r"],row["lower_confidence_bound"],row["upper_confidence_bound"],
                gate,state,target,current_value,int(protected),int(relax_supported),
                int(tighten_supported),json.dumps(evidence,sort_keys=True),utc_now(),SCHEMA_VERSION,
            ),
        )
        if conn.total_changes > before:
            stats_added += 1

        if state == "PROTECTED_NO_RELAXATION":
            protected_blocks += 1

        action = None
        if relax_supported:
            action = "RELAX_REVIEW"
        elif tighten_supported:
            action = "TIGHTEN_REVIEW"
        if action is None or target is None:
            continue

        rationale = {
            "evidence_gate": gate,
            "state": state,
            "target_control": target,
            "current_value": current_value,
            "proposed_value": None,
            "numeric_change_deferred_to_offline_replay": True,
            "production_config_mutation": False,
            "required_path": [
                "AUDIT_FINDING",
                "POLICY_CANDIDATE",
                "OFFLINE_REPLAY",
                "SHADOW_POLICY",
                "QUALIFICATION",
                "OPERATOR_APPROVAL",
                "PROMOTION",
            ],
            "evidence": evidence,
        }
        candidate_id = "apol_" + canonical_hash([stat_id, action, rationale])[:28]
        before = conn.total_changes
        conn.execute(
            """INSERT OR IGNORE INTO audit_policy_candidates(
                candidate_id,audit_run_id,calibration_stat_id,target_control,action,
                recommendation_scope,current_value,proposed_value,protected_control,
                offline_replay_required,shadow_policy_required,production_mutation_allowed,
                rationale_json,created_at,schema_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                candidate_id,run_id,stat_id,target,action,gate,current_value,None,
                int(protected),1,1,0,json.dumps(rationale,sort_keys=True),utc_now(),SCHEMA_VERSION,
            ),
        )
        if conn.total_changes > before:
            candidates_added += 1

    conn.commit()
    return {
        "audit_run_id": run_id,
        "calibration_stats_added": stats_added,
        "policy_candidates_added": candidates_added,
        "protected_relaxations_blocked": protected_blocks,
        "cohort_count": len(rows),
    }


def _findings(conn: sqlite3.Connection, audit_run_id: str) -> list[dict[str, Any]]:
    return [
        dict(row) for row in conn.execute(
            "SELECT * FROM audit_findings WHERE audit_run_id=? "
            "ORDER BY CASE severity "
            "WHEN 'BLOCKING' THEN 5 WHEN 'CRITICAL' THEN 4 WHEN 'WARNING' THEN 3 "
            "WHEN 'WATCH' THEN 2 ELSE 1 END DESC, subsystem, finding_type, finding_id",
            (audit_run_id,),
        )
    ]


def _worst_status(findings: Sequence[Mapping[str, Any]], subsystems: set[str]) -> str | None:
    relevant = [
        str(row.get("severity") or "INFO").upper()
        for row in findings
        if str(row.get("subsystem") or "").upper() in subsystems
    ]
    if not relevant:
        return None
    return max(relevant, key=lambda value: SEVERITY_RANK.get(value, -1))


def _quality_dimensions(
    conn: sqlite3.Connection,
    audit_run_id: str,
    findings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    analysis = conn.execute(
        "SELECT * FROM audit_analysis_runs WHERE audit_run_id=?", (audit_run_id,)
    ).fetchone()
    metrics = conn.execute(
        "SELECT * FROM audit_system_metrics WHERE audit_run_id=?", (audit_run_id,)
    ).fetchone()
    analysis_row = dict(analysis) if analysis else {}
    metric_row = dict(metrics) if metrics else {}

    def status_for(subsystems: set[str], evidence_count: int) -> dict[str, Any]:
        worst = _worst_status(findings, subsystems)
        if evidence_count <= 0:
            return {"status": "UNKNOWN", "evidence_count": 0, "worst_finding": worst}
        if worst is None:
            return {"status": "PASS", "evidence_count": evidence_count, "worst_finding": None}
        if worst == "INFO":
            return {"status": "PASS_WITH_INFO", "evidence_count": evidence_count, "worst_finding": worst}
        return {"status": worst, "evidence_count": evidence_count, "worst_finding": worst}

    decision_count = int(analysis_row.get("decision_count") or 0)
    outcome_count = int(analysis_row.get("outcome_count") or 0)
    operational_count = int(analysis_row.get("operational_snapshot_count") or 0)
    resolved_count = (
        int(metric_row.get("good_accept_count") or 0)
        + int(metric_row.get("false_accept_count") or 0)
        + int(metric_row.get("good_reject_count") or 0)
        + int(metric_row.get("false_reject_count") or 0)
    )
    dpv = _finite(metric_row.get("decision_policy_net_value"))

    regime_row = conn.execute(
        "SELECT COALESCE(SUM(sample_count),0) FROM audit_cohort_stats "
        "WHERE audit_run_id=? AND dimension='regime'", (audit_run_id,)
    ).fetchone()
    regime_samples = int(regime_row[0] or 0)
    reject_row = conn.execute(
        "SELECT COALESCE(SUM(sample_count),0) FROM audit_cohort_stats "
        "WHERE audit_run_id=? AND dimension='reject_reason' AND UPPER(cohort_key)!='NONE'",
        (audit_run_id,),
    ).fetchone()
    reject_samples = int(reject_row[0] or 0)

    decision_gate = recommendation_gate(resolved_count)
    if resolved_count == 0:
        decision_status = "UNKNOWN"
    elif decision_gate in {"NO_CHANGE", "OBSERVE_ONLY"}:
        decision_status = "INSUFFICIENT_EVIDENCE"
    elif dpv is not None and dpv < 0:
        decision_status = "WATCH"
    else:
        decision_status = "OBSERVED_POSITIVE"

    reject_candidates = int(conn.execute(
        "SELECT COUNT(*) FROM audit_policy_candidates WHERE audit_run_id=? "
        "AND action='RELAX_REVIEW'", (audit_run_id,)
    ).fetchone()[0])
    protected_blocks = int(conn.execute(
        "SELECT COUNT(*) FROM audit_calibration_stats WHERE audit_run_id=? "
        "AND recommendation_state='PROTECTED_NO_RELAXATION'", (audit_run_id,)
    ).fetchone()[0])

    return {
        "DATA_INTEGRITY_SCORE": status_for({"DATA"}, decision_count),
        "REGIME_CALIBRATION_SCORE": {
            **status_for({"EXPECTANCY"}, regime_samples),
            "limitation": "REALIZED_REGIME_CONFUSION_LABELS_NOT_AVAILABLE",
        },
        "SIGNAL_QUALITY_SCORE": {
            "status": "UNKNOWN",
            "evidence_count": 0,
            "limitation": "MISSED_CANDIDATE_COUNTERFACTUALS_NOT_AVAILABLE",
        },
        "DECISION_QUALITY_SCORE": {
            "status": decision_status,
            "evidence_count": resolved_count,
            "decision_policy_net_value": dpv,
            "evidence_gate": decision_gate,
        },
        "REJECT_QUALITY_SCORE": {
            "status": (
                "UNKNOWN" if reject_samples == 0
                else "WATCH" if reject_candidates > 0
                else "OBSERVED"
            ),
            "evidence_count": reject_samples,
            "relax_review_candidates": reject_candidates,
            "protected_relaxations_blocked": protected_blocks,
        },
        "EXECUTION_QUALITY_SCORE": status_for({"EXECUTION"}, outcome_count),
        "RISK_QUALITY_SCORE": status_for({"RISK"}, decision_count),
        "EXPECTANCY_SCORE": status_for({"EXPECTANCY"}, outcome_count),
        "RELIABILITY_SCORE": status_for({"RELIABILITY"}, operational_count),
        "SYSTEM_EXPECTANCY_CONFIDENCE": {
            "status": recommendation_gate(outcome_count),
            "evidence_count": outcome_count,
        },
    }


def _limitations(
    conn: sqlite3.Connection,
    audit_run_id: str,
    findings: Sequence[Mapping[str, Any]],
) -> list[str]:
    limitations = {
        "NO_DIRECT_AUDIT_TO_PRODUCTION_CONFIG_PATH",
        "POLICY_CANDIDATES_REQUIRE_OFFLINE_REPLAY_AND_SHADOW_QUALIFICATION",
        "MISSED_CANDIDATE_COUNTERFACTUALS_NOT_AVAILABLE",
        "REALIZED_REGIME_CONFUSION_LABELS_NOT_AVAILABLE",
    }
    analysis = conn.execute(
        "SELECT * FROM audit_analysis_runs WHERE audit_run_id=?", (audit_run_id,)
    ).fetchone()
    metrics = conn.execute(
        "SELECT * FROM audit_system_metrics WHERE audit_run_id=?", (audit_run_id,)
    ).fetchone()
    if analysis and int(analysis["operational_snapshot_count"] or 0) == 0:
        limitations.add("OPERATIONAL_EVIDENCE_ABSENT")
    if metrics and int(metrics["unresolved_count"] or 0) > 0:
        limitations.add("UNRESOLVED_DECISION_OUTCOMES_PRESENT")
    if any(str(row.get("subsystem") or "") == "POSITION_MANAGEMENT" for row in findings):
        limitations.add("POSITION_MANAGEMENT_COUNTERFACTUALS_NOT_AVAILABLE")
    return sorted(limitations)


def render_human_report(report: Mapping[str, Any]) -> str:
    matrix = report.get("observed_matrix") or {}
    quality = report.get("quality_dimensions") or {}
    lines = [
        "ALPHAFORGE SYSTEM AUDIT",
        "",
        f"Audit run                  {report.get('audit_run_id')}",
        f"Status                     {report.get('system_status')}",
        f"Decisions                  {report.get('decision_count', 0)}",
        f"Resolved outcomes          {report.get('outcome_count', 0)}",
        f"Decision Policy Net Value  {report.get('decision_policy_net_value')}",
        "",
        "Observed decision matrix",
        f"  GOOD_ACCEPT              {matrix.get('GOOD_ACCEPT', 0)}",
        f"  FALSE_ACCEPT             {matrix.get('FALSE_ACCEPT', 0)}",
        f"  GOOD_REJECT              {matrix.get('GOOD_REJECT', 0)}",
        f"  FALSE_REJECT             {matrix.get('FALSE_REJECT', 0)}",
        f"  UNRESOLVED               {matrix.get('UNRESOLVED', 0)}",
        "",
        "Quality dimensions",
    ]
    for name in (
        "DATA_INTEGRITY_SCORE","REGIME_CALIBRATION_SCORE","SIGNAL_QUALITY_SCORE",
        "DECISION_QUALITY_SCORE","REJECT_QUALITY_SCORE","EXECUTION_QUALITY_SCORE",
        "RISK_QUALITY_SCORE","EXPECTANCY_SCORE","RELIABILITY_SCORE",
        "SYSTEM_EXPECTANCY_CONFIDENCE",
    ):
        item = quality.get(name) or {}
        lines.append(f"  {name:<29} {item.get('status', 'UNKNOWN')}")
    lines.extend([
        "",
        f"Policy candidates          {len(report.get('policy_candidates') or [])}",
        f"Protected relax blocked    {report.get('protected_relaxations_blocked', 0)}",
        "",
        "Evidence limitations",
    ])
    for item in report.get("limitations") or []:
        lines.append(f"  - {item}")
    return "\n".join(lines) + "\n"


def build_system_audit_report(
    conn: sqlite3.Connection,
    *,
    audit_run_id: str | None = None,
) -> dict[str, Any]:
    """Build and persist a Level 2 report without changing any trading policy."""
    bootstrap_reporting_schema(conn)
    run_id = str(audit_run_id or _latest_audit_run(conn))
    build_calibration_recommendations(conn, audit_run_id=run_id)

    analysis = conn.execute(
        "SELECT * FROM audit_analysis_runs WHERE audit_run_id=?", (run_id,)
    ).fetchone()
    metrics = conn.execute(
        "SELECT * FROM audit_system_metrics WHERE audit_run_id=?", (run_id,)
    ).fetchone()
    if analysis is None or metrics is None:
        raise ValueError("Level 1 diagnostics are required before Level 2 reporting")
    analysis_row = dict(analysis)
    metric_row = dict(metrics)
    findings = _findings(conn, run_id)
    candidates = [
        dict(row) for row in conn.execute(
            "SELECT * FROM audit_policy_candidates WHERE audit_run_id=? "
            "ORDER BY action,target_control,candidate_id", (run_id,)
        )
    ]
    calibration = [
        dict(row) for row in conn.execute(
            "SELECT * FROM audit_calibration_stats WHERE audit_run_id=? "
            "ORDER BY dimension,cohort_key", (run_id,)
        )
    ]

    worst = max(
        (str(row.get("severity") or "INFO").upper() for row in findings),
        key=lambda value: SEVERITY_RANK.get(value, -1),
        default=None,
    )
    if worst == "BLOCKING":
        system_status = "BLOCKING_FINDINGS"
    elif worst == "CRITICAL":
        system_status = "CRITICAL_FINDINGS"
    elif worst == "WARNING":
        system_status = "AUDIT_COMPLETE_WITH_WARNINGS"
    elif worst == "WATCH":
        system_status = "AUDIT_COMPLETE_WITH_WATCH_ITEMS"
    else:
        system_status = "AUDIT_COMPLETE"

    quality = _quality_dimensions(conn, run_id, findings)
    limitations = _limitations(conn, run_id, findings)
    report_core = {
        "schema_version": SCHEMA_VERSION,
        "report_level": 2,
        "audit_run_id": run_id,
        "system_status": system_status,
        "decision_count": int(analysis_row["decision_count"]),
        "outcome_count": int(analysis_row["outcome_count"]),
        "operational_snapshot_count": int(analysis_row["operational_snapshot_count"]),
        "observed_matrix": {
            "GOOD_ACCEPT": int(metric_row["good_accept_count"]),
            "FALSE_ACCEPT": int(metric_row["false_accept_count"]),
            "GOOD_REJECT": int(metric_row["good_reject_count"]),
            "FALSE_REJECT": int(metric_row["false_reject_count"]),
            "UNRESOLVED": int(metric_row["unresolved_count"]),
        },
        "decision_policy_net_value": float(metric_row["decision_policy_net_value"]),
        "quality_dimensions": quality,
        "finding_count": len(findings),
        "findings": findings,
        "calibration_stats": calibration,
        "policy_candidates": candidates,
        "protected_relaxations_blocked": sum(
            1 for row in calibration
            if row["recommendation_state"] == "PROTECTED_NO_RELAXATION"
        ),
        "limitations": limitations,
        "production_config_mutation_allowed": False,
        "level3_shadow_policy_active": False,
        "level4_auto_calibration_active": False,
    }
    evidence_hash = canonical_hash(report_core)
    report_id = "arep_" + canonical_hash([run_id, evidence_hash])[:28]
    report = {**report_core, "report_id": report_id, "evidence_hash": evidence_hash}
    human = render_human_report(report)
    conn.execute(
        """INSERT OR IGNORE INTO audit_reports(
            report_id,audit_run_id,evidence_hash,report_json,human_summary,created_at,schema_version
        ) VALUES (?,?,?,?,?,?,?)""",
        (
            report_id,run_id,evidence_hash,json.dumps(report,sort_keys=True,default=str),
            human,utc_now(),SCHEMA_VERSION,
        ),
    )
    conn.commit()
    return {**report, "human_summary": human}


def _readonly_sqlite(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run isolated AlphaForge System Audit Fabric Levels 0-2")
    parser.add_argument("--source-db", required=True, help="Runtime/campaign SQLite database; opened read-only")
    parser.add_argument("--audit-db", required=True, help="Separate audit SQLite database")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--campaign-id")
    scope.add_argument("--burnin-run-id")
    parser.add_argument("--json-out")
    parser.add_argument("--text-out")
    args = parser.parse_args(argv)

    source_path = Path(args.source_db).expanduser().resolve()
    audit_path = Path(args.audit_db).expanduser().resolve()
    if source_path == audit_path:
        parser.error("--audit-db must be different from --source-db")
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    source = _readonly_sqlite(source_path)
    audit = sqlite3.connect(audit_path)
    try:
        ingest_audit_evidence(
            source,audit,campaign_id=args.campaign_id,burnin_run_id=args.burnin_run_id
        )
        if args.burnin_run_id:
            run_ids = [str(args.burnin_run_id)]
        else:
            run_ids = [
                str(row[0]) for row in audit.execute(
                    "SELECT DISTINCT burnin_run_id FROM audit_decision_envelopes "
                    "WHERE campaign_id=? ORDER BY burnin_run_id",
                    (args.campaign_id,),
                )
            ]
        for run_id in run_ids:
            try:
                ingest_operational_evidence(source,audit,burnin_run_id=run_id)
            except Exception:
                # Operational evidence is optional; the report records its absence.
                pass
        diagnostics = run_system_diagnostics(audit)
        report = build_system_audit_report(audit,audit_run_id=diagnostics["audit_run_id"])
        if args.json_out:
            path = Path(args.json_out)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report,indent=2,sort_keys=True,default=str)+"\n",encoding="utf-8")
        if args.text_out:
            path = Path(args.text_out)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(report["human_summary"],encoding="utf-8")
        print(json.dumps({
            "status":"PASS",
            "audit_run_id":report["audit_run_id"],
            "report_id":report["report_id"],
            "system_status":report["system_status"],
            "policy_candidate_count":len(report["policy_candidates"]),
        },sort_keys=True))
        return 0
    finally:
        source.close()
        audit.close()


if __name__ == "__main__":
    raise SystemExit(main())
