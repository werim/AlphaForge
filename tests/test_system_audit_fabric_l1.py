from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import alphaforge.system_audit_diagnostics as diagnostics
from alphaforge.system_audit_diagnostics import (
    ingest_operational_evidence,
    run_system_diagnostics,
    safe_run_system_diagnostics,
)
from alphaforge.system_audit_store import bootstrap_audit_schema


def _audit(tmp_path: Path) -> sqlite3.Connection:
    conn=sqlite3.connect(tmp_path/"audit.db")
    conn.row_factory=sqlite3.Row
    bootstrap_audit_schema(conn)
    return conn


def _envelope(
    conn: sqlite3.Connection, *, envelope_id: str, decision: str,
    symbol: str="BTCUSDT", regime: str="TRENDING", setup: str="CONTINUATION",
    reject_reason: str|None=None, decision_row: dict|None=None,
    observation: dict|None=None, gates: list[str]|None=None,
    git_commit: str|None="git", config_hash: str|None="cfg",
) -> None:
    source_payload={
        "decision_evidence":{"risk_flags":[],**(decision_row or {})},
        "burnin_observation": observation if observation is not None else {
            "evidence_complete":1,"missing_fields_json":"[]"
        },
        "burnin_run":{},
    }
    conn.execute(
        """INSERT INTO audit_decision_envelopes(
            envelope_id,source_evidence_id,source_hash,source_version,campaign_id,burnin_run_id,
            release_id,git_commit,config_hash,strategy_config_hash,runtime_instance_id,mode,
            decision_time,signal_id,decision_id,symbol,side,setup_type,regime,decision,reject_reason,
            score,confidence,raw_rr,effective_rr,min_effective_rr,entry,stop,target,spread_pct,
            expected_slippage_pct,fee_pct,funding_rate_pct,latency_ms,liquidity_score,volume_24h_usdt,
            volatility_regime,gate_outputs_json,gate_thresholds_json,gate_margins_json,
            decision_payload_json,source_provenance_json,ingested_at,schema_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            envelope_id,"src-"+envelope_id,"hash-"+envelope_id,1,"camp","run","rel",
            git_commit,config_hash,"strategy","runtime-1","PAPER","2026-09-22T16:00:00Z",
            "sig-"+envelope_id,"dec-"+envelope_id,symbol,"LONG",setup,regime,decision,
            reject_reason,0.7,0.8,1.8,1.5,1.1,100.0,99.0,102.0,0.0002,0.0002,
            0.0004,0.0,50.0,0.9,10_000_000.0,"NORMAL",json.dumps(gates or []),
            json.dumps({"MIN_EFFECTIVE_RR":1.1}),json.dumps([]),
            json.dumps(source_payload,sort_keys=True),json.dumps({"provider":"PAPER_RUNTIME"}),
            "2026-09-22T16:00:01Z","system_audit_v1",
        ),
    )


def _outcome(
    conn: sqlite3.Connection, *, envelope_id: str, kind: str, net_r: float,
    expected: float|None=None, realized: float|None=None, authoritative: int=1,
    simulated_fill: float=100.0,
) -> None:
    payload={"pending_position":{"simulated_fill":simulated_fill}} if kind=="ACCEPTED_ACTUAL" else {}
    oid="out-"+envelope_id
    conn.execute(
        """INSERT INTO audit_outcomes(
            outcome_snapshot_id,envelope_id,outcome_kind,source_outcome_id,source_hash,outcome_status,
            gross_r,net_r,execution_cost,expected_effective_rr,realized_effective_rr,mfe,mae,
            hold_duration_seconds,ambiguous,evidence_complete,attributable,execution_invalidated,
            authoritative,resolved_at,outcome_payload_json,ingested_at,schema_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            oid,envelope_id,kind,"src-"+oid,"hash-"+oid,"TP_HIT" if net_r>=0 else "SL_HIT",
            net_r+0.05,net_r,0.05,expected,realized,1.2,-0.3,3600,0,1,1,0,authoritative,
            "2026-09-22T17:00:00Z",json.dumps(payload),"2026-09-22T17:00:01Z","system_audit_v1",
        ),
    )


def test_l1_observed_matrix_and_value_weighted_dpv(tmp_path: Path) -> None:
    conn=_audit(tmp_path)
    cases=[
        ("ga","ACCEPT","ACCEPTED_ACTUAL",1.2,None),
        ("fa","ACCEPT","ACCEPTED_ACTUAL",-0.8,None),
        ("gr","REJECT","REJECT_SHADOW",-0.6,"LOW_EFFECTIVE_RR"),
        ("fr","REJECT","REJECT_SHADOW",1.5,"LOW_SCORE"),
    ]
    for eid,decision,kind,net_r,reason in cases:
        _envelope(conn,envelope_id=eid,decision=decision,reject_reason=reason,
                  gates=[reason,"SECONDARY_GATE"] if reason else [])
        _outcome(conn,envelope_id=eid,kind=kind,net_r=net_r,expected=1.4,realized=net_r)
    conn.commit()
    report=run_system_diagnostics(conn)
    assert report["observed_matrix"]=={
        "GOOD_ACCEPT":1,"FALSE_ACCEPT":1,"GOOD_REJECT":1,"FALSE_REJECT":1,"UNRESOLVED":0
    }
    assert report["decision_policy_net_value"]==pytest.approx(1.2+0.6-0.8-1.5)
    rows=conn.execute(
        "SELECT envelope_id,observed_cell,attribution_json FROM audit_decision_classifications "
        "WHERE audit_run_id=?",(report["audit_run_id"],)
    ).fetchall()
    by_id={r["envelope_id"]:r for r in rows}
    attr=json.loads(by_id["fr"]["attribution_json"])
    assert attr["LOW_SCORE"]>attr["SECONDARY_GATE"]
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_findings WHERE audit_run_id=? "
        "AND finding_type LIKE '%NEGATIVE_EXPECTANCY_COHORT%'",
        (report["audit_run_id"],)
    ).fetchone()[0]==0


def test_l1_execution_delta_is_telemetry_not_single_trade_blame(tmp_path: Path) -> None:
    conn=_audit(tmp_path)
    _envelope(conn,envelope_id="exec",decision="ACCEPT")
    _outcome(conn,envelope_id="exec",kind="ACCEPTED_ACTUAL",net_r=-0.4,
             expected=1.5,realized=-0.4,simulated_fill=100.2)
    conn.commit()
    report=run_system_diagnostics(conn)
    row=conn.execute(
        "SELECT expected_vs_realized_r_delta,fill_model_error_pct,attribution_json "
        "FROM audit_decision_classifications WHERE audit_run_id=?",(report["audit_run_id"],)
    ).fetchone()
    assert row["expected_vs_realized_r_delta"]==pytest.approx(-1.9)
    assert row["fill_model_error_pct"]==pytest.approx(0.002)
    assert "EXECUTION_FILL_MODEL_ERROR" in json.loads(row["attribution_json"])
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_findings WHERE audit_run_id=? "
        "AND finding_type='SYSTEMATIC_FILL_MODEL_ERROR'",(report["audit_run_id"],)
    ).fetchone()[0]==0


def test_l1_persisted_risk_breach_is_explicit_finding(tmp_path: Path) -> None:
    conn=_audit(tmp_path)
    _envelope(conn,envelope_id="risk",decision="ACCEPT",decision_row={
        "open_position_count":4,"max_open_positions":3,
        "total_notional_exposure":1200,"max_notional_exposure":1000,
        "portfolio_risk_state":"MEASURED","risk_flags":["CORRELATION_OVEREXPOSURE"],
    })
    _outcome(conn,envelope_id="risk",kind="ACCEPTED_ACTUAL",net_r=0.2)
    conn.commit()
    report=run_system_diagnostics(conn)
    findings={r[0] for r in conn.execute(
        "SELECT finding_type FROM audit_findings WHERE audit_run_id=?",(report["audit_run_id"],)
    )}
    assert {"ACCEPTED_OVER_POSITION_LIMIT","ACCEPTED_OVER_NOTIONAL_LIMIT",
            "ACCEPTED_WITH_RISK_FLAGS"} <= findings


def test_l1_incomplete_lineage_fails_closed_and_missing_operations_is_unknown(tmp_path: Path) -> None:
    conn=_audit(tmp_path)
    _envelope(conn,envelope_id="bad",decision="REJECT",git_commit=None,config_hash=None,
              observation={"evidence_complete":0,"missing_fields_json":json.dumps(["entry"])})
    conn.commit()
    report=run_system_diagnostics(conn)
    rows=conn.execute(
        "SELECT finding_type,severity FROM audit_findings WHERE audit_run_id=?",
        (report["audit_run_id"],)
    ).fetchall()
    findings={(r["finding_type"],r["severity"]) for r in rows}
    assert ("DECISION_LINEAGE_INCOMPLETE","BLOCKING") in findings
    assert ("DECISION_EVIDENCE_INCOMPLETE","BLOCKING") in findings
    assert ("OPERATIONAL_EVIDENCE_MISSING","WATCH") in findings


def test_l1_systemic_negative_cohort_requires_thirty_samples(tmp_path: Path) -> None:
    conn=_audit(tmp_path)
    for i in range(30):
        eid=f"neg-{i}"
        _envelope(conn,envelope_id=eid,decision="ACCEPT",regime="CHOPPY")
        _outcome(conn,envelope_id=eid,kind="ACCEPTED_ACTUAL",net_r=-0.5,
                 expected=1.2,realized=-0.5)
    conn.commit()
    report=run_system_diagnostics(conn)
    row=conn.execute(
        "SELECT finding_type,severity FROM audit_findings WHERE audit_run_id=? "
        "AND cohort_dimension='regime' AND cohort_key='CHOPPY'",
        (report["audit_run_id"],)
    ).fetchone()
    assert tuple(row)==("CONFIDENT_NEGATIVE_EXPECTANCY_COHORT","WARNING")


def test_l1_operational_evidence_is_read_only_and_surfaces_stale_data(tmp_path: Path) -> None:
    audit=_audit(tmp_path)
    _envelope(audit,envelope_id="ops",decision="REJECT")
    audit.commit()
    source=sqlite3.connect(tmp_path/"source.db")
    source.execute("""CREATE TABLE runtime_state_snapshots(
        id INTEGER PRIMARY KEY,timestamp TEXT,instance_id TEXT,burnin_run_id TEXT,
        runtime_status TEXT,recovery_action_required INTEGER,fail_closed_reason TEXT,
        orphan_order_count INTEGER,orphan_position_count INTEGER,
        stale_market_data_symbols TEXT,unknown_exchange_state INTEGER)""")
    source.execute(
        "INSERT INTO runtime_state_snapshots VALUES(1,?,?,?,?,?,?,?,?,?,?)",
        ("2026-09-22T16:10:00Z","runtime-1","run","OPERATING",0,None,0,0,
         json.dumps(["BTCUSDT"]),0),
    )
    source.commit()
    before=source.total_changes
    result=ingest_operational_evidence(source,audit,burnin_run_id="run")
    assert source.total_changes==before
    assert result["operational_snapshots_added"]==1
    report=run_system_diagnostics(audit)
    finding=audit.execute(
        "SELECT severity FROM audit_findings WHERE audit_run_id=? "
        "AND finding_type='STALE_MARKET_DATA'",(report["audit_run_id"],)
    ).fetchone()
    assert finding["severity"]=="BLOCKING"


def test_l1_safe_wrapper_isolates_audit_process_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn=_audit(tmp_path)
    _envelope(conn,envelope_id="safe",decision="REJECT")
    conn.commit()
    before=conn.execute("SELECT COUNT(*) FROM audit_decision_envelopes").fetchone()[0]
    monkeypatch.setattr(
        diagnostics,"run_system_diagnostics",
        lambda _conn: (_ for _ in ()).throw(RuntimeError("audit-boom")),
    )
    result=safe_run_system_diagnostics(conn)
    assert result["status"]=="ERROR"
    assert result["error_type"]=="RuntimeError"
    after=conn.execute("SELECT COUNT(*) FROM audit_decision_envelopes").fetchone()[0]
    assert after==before
