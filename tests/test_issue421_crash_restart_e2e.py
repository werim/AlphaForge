from __future__ import annotations
import os, signal, sqlite3, subprocess, sys
from pathlib import Path
import pytest
from sqlalchemy import text
from alphaforge.persistence import init_db
from alphaforge.burnin import bootstrap_burnin_schema, BurnInRun, persist_burnin_run
from alphaforge.burnin_campaign import bootstrap_campaign_schema
from alphaforge.burnin_resolver import resolve_position_closure
from alphaforge.runtime_state import (
    RuntimeStateSnapshot, evaluate_runtime_recovery, save_runtime_state_snapshot,
    save_exchange_reconciliation_event, persist_verified_paper_recovery,
)

WORKER=Path(__file__).parent/"fixtures"/"p1d_crash_worker.py"

def _engine(path):
    engine=init_db(f"sqlite+pysqlite:///{path}")
    with engine.begin() as conn:
        bootstrap_burnin_schema(conn); bootstrap_campaign_schema(conn)
        conn.execute(text("""INSERT OR IGNORE INTO burnin_campaigns
            (campaign_id,release_id,execution_mode,campaign_status,created_at,schema_version)
            VALUES ('camp-p1d','rel-p1d','PAPER','RUNNING','2026-09-23T18:00:00Z','p1d')"""))
        conn.execute(text("""INSERT OR IGNORE INTO burnin_campaign_runs
            (campaign_run_id,campaign_id,burnin_run_id,continuation_sequence,run_status,created_at,schema_version)
            VALUES ('crun-p1d','camp-p1d','run-p1d',0,'RUNNING','2026-09-23T18:00:00Z','p1d')"""))
    return engine

def _kill_at(db, boundary, marker):
    env={**os.environ,"PYTHONPATH":str(Path(__file__).parents[1]/"src")}
    proc=subprocess.Popen([sys.executable,str(WORKER),str(db),boundary],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
    line=proc.stdout.readline().strip()
    if line != marker:
        err=proc.stderr.read(); proc.kill(); proc.wait()
        pytest.fail(f"worker failed before crash boundary: {line!r} {err}")
    os.kill(proc.pid, signal.SIGKILL)
    assert proc.wait(timeout=5) == -signal.SIGKILL

def _counts(db):
    conn=sqlite3.connect(db)
    try:
        return {
            "orders":conn.execute("select count(*) from order_decisions where decision_id='dec-p1d'").fetchone()[0],
            "positions":conn.execute("select count(*) from burnin_pending_position_outcomes where trade_id='trade-p1d'").fetchone()[0],
            "closed":conn.execute("select count(*) from burnin_pending_position_outcomes where trade_id='trade-p1d' and status='CLOSED'").fetchone()[0],
            "outcomes":conn.execute("select count(*) from burnin_trade_outcomes where outcome_id='tout_trade-p1d'").fetchone()[0],
            "evidence":conn.execute("select count(*) from expectancy_evidence where evidence_id='accepted:trade-p1d'").fetchone()[0],
        }
    finally: conn.close()

@pytest.mark.parametrize("boundary,marker,expected",[
    ("accept_before_fill","ACCEPT_DURABLE",{"orders":1,"positions":0,"outcomes":0,"evidence":0}),
    ("fill_before_persistence","FILL_PROCESS_LOCAL",{"orders":0,"positions":0,"outcomes":0,"evidence":0}),
    ("pending_after_persistence","PENDING_DURABLE",{"orders":0,"positions":1,"outcomes":0,"evidence":0}),
    ("resolver_mid_finalization","RESOLVER_TRANSACTION_OPEN",{"orders":0,"positions":1,"closed":0,"outcomes":0,"evidence":0}),
])
def test_real_sigkill_boundaries_never_create_false_authoritative_success(tmp_path,boundary,marker,expected):
    db=tmp_path/f"{boundary}.sqlite3"; _engine(db)
    _kill_at(db,boundary,marker)
    first=_counts(db)
    for key,value in expected.items(): assert first[key]==value
    # Cold reopen of the exact same WAL/journal DB must be deterministic.
    second=_counts(db)
    assert second==first

def test_resolver_cold_restart_is_idempotent_and_preserves_lineage(tmp_path):
    db=tmp_path/"resolver.sqlite3"; engine=_engine(db)
    _kill_at(db,"resolver_mid_finalization","RESOLVER_TRANSACTION_OPEN")
    with engine.begin() as conn:
        result=resolve_position_closure(conn,trade_id="trade-p1d",
            exit_time="2026-09-23T18:01:00Z",exit_price=102.0,exit_reason="TP_HIT",
            exit_costs={"exit_spread":.001,"exit_slippage":.001,"exit_fee":.001,
                "funding":0.0,"latency_impact_penalty":0.0})
    assert result["status"]=="CLOSED"
    with engine.begin() as conn:
        replay=resolve_position_closure(conn,trade_id="trade-p1d",
            exit_time="2026-09-23T18:01:00Z",exit_price=102.0,exit_reason="TP_HIT",
            exit_costs={"exit_spread":.001,"exit_slippage":.001,"exit_fee":.001,
                "funding":0.0,"latency_impact_penalty":0.0})
    assert replay["status"]=="IDEMPOTENT"
    counts=_counts(db)
    assert counts["positions"]==counts["closed"]==counts["outcomes"]==counts["evidence"]==1
    with engine.connect() as conn:
        row=conn.execute(text("select campaign_id,burnin_run_id,source_provenance_json from burnin_pending_position_outcomes where trade_id='trade-p1d'")).one()
    assert row.campaign_id=="camp-p1d" and row.burnin_run_id=="run-p1d"
    assert "runtime-p1d" in row.source_provenance_json

def test_unclean_restart_and_orphan_exposure_block_until_clean_reconciliation(tmp_path):
    db=tmp_path/"recovery.sqlite3"; engine=_engine(db)
    save_runtime_state_snapshot(engine, RuntimeStateSnapshot(
        instance_id="runtime-p1d",startup_id="startup-p1d",runtime_status="OPERATING",
        execution_mode="PAPER",campaign_id="camp-p1d",burnin_run_id="run-p1d",
        process_id=None,last_start_time="2026-09-23T18:00:00Z"))
    save_exchange_reconciliation_event(engine,instance_id="runtime-p1d",startup_id="startup-p1d",
        mode="PAPER",status="MISMATCH",orphan_position_count=1,exchange_read_only_status="READ_ONLY")
    blocked=evaluate_runtime_recovery(engine,mode="PAPER",campaign_id="camp-p1d",
        instance_id="runtime-restart",startup_id="startup-restart")
    assert blocked["blocked"] is True
    assert blocked["current_exposure_check"]["orphan_positions"]==1
    # A clean probe cannot erase durable orphan exposure.
    clean_probe={"evidence_status":"COMPLETE","authenticated":True,
        "input_source":"AUTHENTICATED_EXCHANGE_SNAPSHOT","orders":[],"positions":[],"errors":[]}
    still=evaluate_runtime_recovery(engine,mode="PAPER",campaign_id="camp-p1d",
        instance_id="runtime-restart",startup_id="startup-restart",
        reconciliation_probe=lambda:clean_probe)
    assert still["blocked"] is True
    # Persisted CLEAN reconciliation is required before resume.
    save_exchange_reconciliation_event(engine,instance_id="runtime-restart",startup_id="startup-restart",
        mode="PAPER",status="CLEAN",orphan_position_count=0,orphan_order_count=0,
        exchange_read_only_status="READ_ONLY")
    persist_verified_paper_recovery(engine,probe=clean_probe,prior_snapshot=blocked["latest"])
    clean=evaluate_runtime_recovery(engine,mode="PAPER",campaign_id="camp-p1d",
        instance_id="runtime-restart-2",startup_id="startup-restart-2")
    assert clean["blocked"] is False
    assert clean["reconciliation_status"]=="CLEAN"
