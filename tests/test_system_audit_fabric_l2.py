from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path

import pytest

import alphaforge.system_audit_reporting as reporting
from alphaforge.system_audit_reporting import (
    PROTECTED_CONTROLS,
    bootstrap_reporting_schema,
    build_calibration_recommendations,
    build_system_audit_report,
    recommendation_gate,
)


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn=sqlite3.connect(tmp_path/"audit-l2.db")
    conn.row_factory=sqlite3.Row
    bootstrap_reporting_schema(conn)
    return conn


def _seed_run(
    conn: sqlite3.Connection, *,
    audit_run_id: str="run-l2",
    decision_count: int=300,
    outcome_count: int=300,
    operational_count: int=0,
    dpv: float=1.0,
) -> None:
    conn.execute(
        """INSERT INTO audit_analysis_runs(
            audit_run_id,evidence_hash,decision_count,outcome_count,operational_snapshot_count,
            generated_at,schema_version
        ) VALUES (?,?,?,?,?,?,?)""",
        (audit_run_id,"hash-"+audit_run_id,decision_count,outcome_count,operational_count,
         "2026-09-22T20:00:00Z","system_audit_v1"),
    )
    conn.execute(
        """INSERT INTO audit_system_metrics(
            audit_run_id,classified_decisions,good_accept_count,false_accept_count,
            good_reject_count,false_reject_count,unresolved_count,good_accept_value,
            false_accept_cost,good_reject_protection,false_reject_opportunity_cost,
            decision_policy_net_value,created_at,schema_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (audit_run_id,decision_count,100,20,150,20,10,80.0,12.0,90.0,15.0,dpv,
         "2026-09-22T20:00:00Z","system_audit_v1"),
    )


def _cohort(
    conn: sqlite3.Connection, *,
    audit_run_id: str="run-l2",
    dimension: str,
    key: str,
    n: int,
    mean_net_r: float,
    lower: float,
    upper: float,
) -> None:
    conn.execute(
        """INSERT INTO audit_cohort_stats(
            cohort_stat_id,audit_run_id,dimension,cohort_key,sample_count,positive_count,
            negative_count,mean_net_r,total_net_r,lower_confidence_bound,upper_confidence_bound,
            mean_expected_vs_realized_r_delta,mean_abs_fill_model_error_pct,created_at,schema_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "cohort-"+dimension+"-"+key,audit_run_id,dimension,key,n,max(0,n//2),
            max(0,n-n//2),mean_net_r,mean_net_r*n,lower,upper,None,None,
            "2026-09-22T20:00:00Z","system_audit_v1",
        ),
    )


@pytest.mark.parametrize(
    ("n","expected"),
    [
        (0,"NO_CHANGE"),(29,"NO_CHANGE"),(30,"OBSERVE_ONLY"),(99,"OBSERVE_ONLY"),
        (100,"SMALL_RECOMMENDATION_ALLOWED"),(249,"SMALL_RECOMMENDATION_ALLOWED"),
        (250,"NORMAL_RECOMMENDATION_ALLOWED"),
    ],
)
def test_l2_evidence_gate_boundaries(n: int, expected: str) -> None:
    assert recommendation_gate(n)==expected


def test_l2_positive_false_reject_requires_100_and_positive_lcb(tmp_path: Path) -> None:
    conn=_conn(tmp_path)
    _seed_run(conn)
    _cohort(
        conn,dimension="reject_reason",key="LOW_EFFECTIVE_RR",n=100,
        mean_net_r=0.35,lower=0.10,upper=0.60,
    )
    result=build_calibration_recommendations(conn,audit_run_id="run-l2")
    assert result["policy_candidates_added"]==1
    candidate=conn.execute("SELECT * FROM audit_policy_candidates").fetchone()
    assert candidate["target_control"]=="MIN_EFFECTIVE_RR"
    assert candidate["action"]=="RELAX_REVIEW"
    assert candidate["recommendation_scope"]=="SMALL_RECOMMENDATION_ALLOWED"
    assert candidate["proposed_value"] is None
    assert candidate["offline_replay_required"]==1
    assert candidate["shadow_policy_required"]==1
    assert candidate["production_mutation_allowed"]==0


def test_l2_99_samples_are_observe_only_even_when_positive(tmp_path: Path) -> None:
    conn=_conn(tmp_path)
    _seed_run(conn,decision_count=99,outcome_count=99)
    _cohort(
        conn,dimension="reject_reason",key="LOW_EFFECTIVE_RR",n=99,
        mean_net_r=0.8,lower=0.5,upper=1.1,
    )
    build_calibration_recommendations(conn,audit_run_id="run-l2")
    stat=conn.execute("SELECT * FROM audit_calibration_stats").fetchone()
    assert stat["recommendation_gate"]=="OBSERVE_ONLY"
    assert stat["recommendation_state"]=="OBSERVE_ONLY"
    assert conn.execute("SELECT COUNT(*) FROM audit_policy_candidates").fetchone()[0]==0


def test_l2_protected_execution_gate_cannot_be_relaxed(tmp_path: Path) -> None:
    conn=_conn(tmp_path)
    _seed_run(conn)
    _cohort(
        conn,dimension="reject_reason",key="HIGH_SLIPPAGE",n=300,
        mean_net_r=0.9,lower=0.4,upper=1.3,
    )
    result=build_calibration_recommendations(conn,audit_run_id="run-l2")
    stat=conn.execute("SELECT * FROM audit_calibration_stats").fetchone()
    assert stat["target_control"]=="ALPHAFORGE_MAX_EXPECTED_SLIPPAGE_PCT"
    assert stat["target_control"] in PROTECTED_CONTROLS
    assert stat["protected_control"]==1
    assert stat["recommendation_state"]=="PROTECTED_NO_RELAXATION"
    assert stat["relaxation_supported"]==0
    assert result["protected_relaxations_blocked"]==1
    assert conn.execute("SELECT COUNT(*) FROM audit_policy_candidates").fetchone()[0]==0


def test_l2_negative_accepted_cohort_can_only_propose_offline_tighten_review(tmp_path: Path) -> None:
    conn=_conn(tmp_path)
    _seed_run(conn,dpv=-20.0)
    _cohort(
        conn,dimension="decision",key="ACCEPT",n=250,
        mean_net_r=-0.4,lower=-0.6,upper=-0.2,
    )
    build_calibration_recommendations(conn,audit_run_id="run-l2")
    candidate=conn.execute("SELECT * FROM audit_policy_candidates").fetchone()
    assert candidate["target_control"]=="ACCEPTANCE_POLICY"
    assert candidate["action"]=="TIGHTEN_REVIEW"
    assert candidate["recommendation_scope"]=="NORMAL_RECOMMENDATION_ALLOWED"
    assert candidate["production_mutation_allowed"]==0


def test_l2_report_exposes_separate_dimensions_and_limitations(tmp_path: Path) -> None:
    conn=_conn(tmp_path)
    _seed_run(conn,decision_count=40,outcome_count=35,operational_count=0,dpv=2.0)
    _cohort(
        conn,dimension="regime",key="TRENDING",n=35,
        mean_net_r=0.1,lower=-0.1,upper=0.3,
    )
    conn.execute(
        """INSERT INTO audit_findings(
            finding_id,audit_run_id,subsystem,finding_type,severity,envelope_id,
            cohort_dimension,cohort_key,evidence_json,explanation,created_at,schema_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "finding-data","run-l2","DATA","DECISION_EVIDENCE_INCOMPLETE","BLOCKING",None,
            None,None,json.dumps({"count":1}),"Incomplete canonical evidence.",
            "2026-09-22T20:00:00Z","system_audit_v1",
        ),
    )
    report=build_system_audit_report(conn,audit_run_id="run-l2")
    expected={
        "DATA_INTEGRITY_SCORE","REGIME_CALIBRATION_SCORE","SIGNAL_QUALITY_SCORE",
        "DECISION_QUALITY_SCORE","REJECT_QUALITY_SCORE","EXECUTION_QUALITY_SCORE",
        "RISK_QUALITY_SCORE","EXPECTANCY_SCORE","RELIABILITY_SCORE",
        "SYSTEM_EXPECTANCY_CONFIDENCE",
    }
    assert expected==set(report["quality_dimensions"])
    assert report["quality_dimensions"]["DATA_INTEGRITY_SCORE"]["status"]=="BLOCKING"
    assert report["quality_dimensions"]["SIGNAL_QUALITY_SCORE"]["status"]=="UNKNOWN"
    assert "OPERATIONAL_EVIDENCE_ABSENT" in report["limitations"]
    assert "REALIZED_REGIME_CONFUSION_LABELS_NOT_AVAILABLE" in report["limitations"]
    assert report["production_config_mutation_allowed"] is False
    assert report["level3_shadow_policy_active"] is False
    assert report["level4_auto_calibration_active"] is False
    assert "ALPHAFORGE SYSTEM AUDIT" in report["human_summary"]


def test_l2_reporting_rows_are_append_only(tmp_path: Path) -> None:
    conn=_conn(tmp_path)
    _seed_run(conn)
    _cohort(
        conn,dimension="reject_reason",key="LOW_SCORE",n=100,
        mean_net_r=0.25,lower=0.05,upper=0.45,
    )
    report=build_system_audit_report(conn,audit_run_id="run-l2")
    assert report["report_id"]
    with pytest.raises(sqlite3.IntegrityError,match="IMMUTABLE_SYSTEM_AUDIT_REPORTING"):
        conn.execute("UPDATE audit_reports SET human_summary='changed'")


def test_l2_has_no_direct_config_mutation_path_and_cli_uses_read_only_source() -> None:
    source=inspect.getsource(reporting)
    assert "write_dashboard_overrides" not in source
    assert "reset_dashboard_override" not in source
    assert "update_parental_control" not in source
    assert "?mode=ro" in source
    assert "production_mutation_allowed" in source
