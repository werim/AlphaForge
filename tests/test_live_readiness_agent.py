from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path
from alphaforge.live_readiness_agent import BLOCKED, NEEDS_FIX, NOT_OBSERVABLE, PASS, LiveReadinessAgent
from alphaforge.release_gates import ROLLBACK_VERIFICATION_CONTRACT, RUNBOOK_REQUIRED_MARKERS, RUNBOOK_VERIFICATION_CONTRACT

NOW=datetime(2026,9,20,18,14,tzinfo=timezone.utc)
def meta(_): return {"commit":"abc","branch":"feat/live-readiness-agent-v1","dirty":False}
def make_db(path:Path):
    c=sqlite3.connect(path); c.executescript("""
    CREATE TABLE schema_migrations(x); CREATE TABLE burnin_campaigns(campaign_id,release_id,campaign_status,active_run_id,git_commit,config_hash,strategy_config_hash,last_heartbeat_at,last_error,symbols_json,latest_qualification_id,qualification_status);
    CREATE TABLE burnin_runs(burnin_run_id,status,git_commit,config_hash,strategy_config_hash); CREATE TABLE burnin_campaign_runs(campaign_id,burnin_run_id,status);
    CREATE TABLE burnin_preflight_reports(campaign_id,status,generated_at); CREATE TABLE runtime_state_snapshots(campaign_id,burnin_run_id,reconciliation_status,reconciliation_mismatch_count,recovery_action_required,last_error,timestamp);
    CREATE TABLE order_decisions(decision_id,signal_id,decision,mode,effective_rr,execution_ctx_missing,spread_pct,expected_slippage_pct,latency_ms,funding_rate_pct); CREATE TABLE trade_lifecycle_events(signal_id,lifecycle_state,mode);
    CREATE TABLE burnin_reject_outcomes(burnin_run_id,evidence_complete,forward_label,hypothetical_net_r_after_costs,payload_json); CREATE TABLE expectancy_evidence(campaign_id,run_id,evidence_complete,net_r,decision_time,resolved_at,source_decision_id,evidence_type);
    CREATE TABLE burnin_qualification_snapshots(qualification_id,burnin_run_id,release_id,campaign_id,source_run_ids_json,aggregate_evidence_hash,status,sample_status,evidence_completeness_status,generated_at); CREATE TABLE burnin_recovery_drills(campaign_id,status,generated_at);
    CREATE TABLE live_rollback_validation_evidence(validation_id,recorded_at,evidence_status,rollback_evidence_source,kill_switch_block_verified,no_submit_on_kill_switch_verified,fail_closed_reconciliation_verified,repair_actions_non_mutating_verified,execution_mutation_attempt_count,blocking_reasons);
    CREATE TABLE rollback_verification_events(release_id,status,verified_at,evidence_json);
    CREATE TABLE runbook_evidence(release_id,status,recorded_at,evidence_json);
    CREATE TABLE runtime_control_state(mode_requested,mode_running,updated_at); CREATE TABLE runtime_control_audit_events(action,success,event_ts,requested_mode); CREATE TABLE burnin_observations(burnin_run_id,symbol,decision,execution_mode,metrics_json);
    """)
    c.execute("INSERT INTO burnin_campaigns VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",("camp","rel","RUNNING","run","abc","cfg","str","2026-09-20T18:13:59Z",None,'["BTCUSDT"]',None,None))
    c.execute("INSERT INTO burnin_runs VALUES(?,?,?,?,?)",("run","RUNNING","abc","cfg","str")); c.execute("INSERT INTO burnin_campaign_runs VALUES(?,?,?)",("camp","run","RUNNING")); c.execute("INSERT INTO burnin_preflight_reports VALUES(?,?,?)",("camp","PASS","2026-09-20T18:00:00Z")); c.execute("INSERT INTO runtime_state_snapshots VALUES(?,?,?,?,?,?,?)",("camp","run","CLEAN",0,0,None,"2026-09-20T18:00:00Z")); c.execute("INSERT INTO runtime_control_state VALUES(?,?,?)",("PAPER","PAPER","2026-09-20T18:00:00Z")); c.execute("INSERT INTO burnin_observations VALUES(?,?,?,?,?)",("run","BTCUSDT",None,"PAPER","{}")); c.commit(); c.close()
def seed_release_safety_evidence(path:Path, *, validation_id="rb-source", release_id="rel"):
    rollback_evidence={
        "verification_contract":ROLLBACK_VERIFICATION_CONTRACT,
        "git_commit":"abc",
        "source":"DETERMINISTIC_VALIDATION",
        "validation_id":validation_id,
        "recorded_at":"2026-09-20T18:00:00Z",
        "age_sec":1.0,
        "kill_switch_block_verified":True,
        "no_submit_on_kill_switch_verified":True,
        "fail_closed_reconciliation_verified":True,
        "repair_actions_non_mutating_verified":True,
        "execution_mutation_attempt_count":0,
        "blocking_reasons":[],
    }
    runbook_evidence={
        "verification_contract":RUNBOOK_VERIFICATION_CONTRACT,
        "git_commit":"abc",
        "file_name":"RUNBOOK.md",
        "sha256":"a"*64,
        "size_bytes":128,
        "required_markers":list(RUNBOOK_REQUIRED_MARKERS),
        "missing_markers":[],
        "read_error":None,
    }
    c=sqlite3.connect(path)
    c.execute("INSERT INTO live_rollback_validation_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",(
        validation_id,"2026-09-20T18:00:00Z","COMPLETE","DETERMINISTIC_VALIDATION",1,1,1,1,0,"[]",
    ))
    c.execute("INSERT INTO rollback_verification_events VALUES(?,?,?,?)",(
        release_id,"PASS","2026-09-20T18:00:01Z",json.dumps(rollback_evidence),
    ))
    c.execute("INSERT INTO runbook_evidence VALUES(?,?,?,?)",(
        release_id,"PASS","2026-09-20T18:00:01Z",json.dumps(runbook_evidence),
    ))
    c.commit(); c.close()

def seed_soak_snapshot(path:Path, *, status="CANARY_QUALIFIED", sample_status="PASS", evidence_status="PASS", campaign_id="camp", release_id="rel", source_runs=None, qualification_id="q1", aggregate_hash="agg"):
    source_runs=["run"] if source_runs is None else source_runs
    c=sqlite3.connect(path)
    c.execute("INSERT INTO burnin_qualification_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",(
        qualification_id,"camp__aggregate",release_id,campaign_id,json.dumps(source_runs),aggregate_hash,
        status,sample_status,evidence_status,"2026-09-20T18:10:00Z",
    ))
    c.execute("UPDATE burnin_campaigns SET latest_qualification_id=?, qualification_status=? WHERE campaign_id='camp'",(
        qualification_id,status,
    ))
    c.commit(); c.close()

def report(path): return LiveReadinessAgent(path,"camp",now=NOW,git_metadata=meta).evaluate()
def gate(r,id): return next(x for x in r["gates"] if x["gate_id"]==id)
def test_target_is_read_only_and_report_is_deterministic(tmp_path):
    db=tmp_path/"campaign.db"; make_db(db); before=db.read_bytes(); env=tmp_path/".env"; env.write_text("X=1\n")
    one,two=report(db),report(db)
    assert one==two and db.read_bytes()==before and env.read_text()=="X=1\n"
    assert gate(one,"EXPECTANCY_TEMPORAL_EVIDENCE")["status"]==NOT_OBSERVABLE
    assert one["overall_status"]=="READINESS_INCOMPLETE"
def test_blocks_duplicates_contamination_and_config_drift(tmp_path):
    db=tmp_path/"campaign.db"; make_db(db); c=sqlite3.connect(db)
    c.executemany("INSERT INTO order_decisions VALUES(?,?,?,?,?,?,?,?,?,?)",[("d","s","ACCEPTED","PAPER",1.2,0,.1,.1,1,.1),("d","s2","ACCEPTED","PAPER",1.2,0,.1,.1,1,.1)])
    c.executemany("INSERT INTO burnin_observations VALUES(?,?,?,?,?)",[
        ("run","BTCUSDT","ACCEPTED","PAPER",'{"signal_id":"s"}'),
        ("run","BTCUSDT","ACCEPTED","PAPER",'{"signal_id":"s2"}'),
        ("run","ETHUSDT",None,"PAPER","{}"),
    ])
    c.execute("UPDATE burnin_runs SET config_hash='drift'"); c.commit(); c.close(); r=report(db)
    assert gate(r,"DUPLICATE_EXECUTION")["status"]==BLOCKED; assert gate(r,"CONTAMINATION")["status"]==BLOCKED; assert gate(r,"CONFIG_DRIFT")["status"]==BLOCKED; assert r["overall_status"]==BLOCKED
def test_pass_and_missing_cost_source_fails_safe(tmp_path):
    db=tmp_path/"campaign.db"; make_db(db); c=sqlite3.connect(db)
    c.execute("INSERT INTO burnin_observations VALUES(?,?,?,?,?)",("run","BTCUSDT","ACCEPTED","PAPER",'{"signal_id":"s"}'))
    c.execute("INSERT INTO order_decisions VALUES(?,?,?,?,?,?,?,?,?,?)",("d","s","ACCEPTED","PAPER",1.2,0,.1,.1,1,.1))
    c.execute("INSERT INTO trade_lifecycle_events VALUES(?,?,?)",("s","FILLED","PAPER"))
    c.execute("""INSERT INTO expectancy_evidence
        (campaign_id,run_id,evidence_complete,net_r,decision_time,resolved_at)
        VALUES(?,?,?,?,?,?)""",("camp","run",1,.2,"2026-09-20T17:00:00Z","2026-09-20T18:00:00Z"))
    c.commit(); c.close(); r=report(db)
    assert gate(r,"ACCEPTED_LIFECYCLE_EVIDENCE")["status"]==PASS; assert gate(r,"EXECUTION_COST_EVIDENCE")["status"]==PASS
    c=sqlite3.connect(db); c.execute("UPDATE order_decisions SET spread_pct=NULL WHERE signal_id='s'"); c.commit(); c.close(); assert gate(report(db),"EXECUTION_COST_EVIDENCE")["status"]==NOT_OBSERVABLE


def test_non_attributable_shadow_reject_cannot_satisfy_readiness_evidence(tmp_path):
    db=tmp_path/"campaign.db"; make_db(db); c=sqlite3.connect(db)
    shadow_payload='{"reject_decision_id":"shadow","forward_label_subject":"LEGACY_SCANNER_SHADOW_CANDIDATE","reject_quality_attributable":false}'
    c.execute("""INSERT INTO burnin_reject_outcomes
        (burnin_run_id,evidence_complete,forward_label,hypothetical_net_r_after_costs,payload_json)
        VALUES(?,?,?,?,?)""",("run",1,"SL_BEFORE_TP",-0.8,shadow_payload))
    c.execute("""INSERT INTO expectancy_evidence
        (campaign_id,run_id,evidence_complete,net_r,decision_time,resolved_at,source_decision_id,evidence_type)
        VALUES(?,?,?,?,?,?,?,?)""",("camp","run",1,-0.8,"2026-09-20T17:00:00Z","2026-09-20T18:00:00Z","shadow","REJECT_FORWARD"))
    c.commit(); c.close()
    r=report(db)
    assert gate(r,"REJECT_FORWARD_OUTCOME_EVIDENCE")["status"]==NOT_OBSERVABLE
    assert gate(r,"EXPECTANCY_TEMPORAL_EVIDENCE")["status"]==NOT_OBSERVABLE


def test_attributable_reject_can_satisfy_readiness_evidence(tmp_path):
    db=tmp_path/"campaign.db"; make_db(db); c=sqlite3.connect(db)
    payload='{"reject_decision_id":"guided","forward_label_subject":"GUIDED_CANDIDATE","reject_quality_attributable":true}'
    c.execute("""INSERT INTO burnin_reject_outcomes
        (burnin_run_id,evidence_complete,forward_label,hypothetical_net_r_after_costs,payload_json)
        VALUES(?,?,?,?,?)""",("run",1,"SL_BEFORE_TP",-0.8,payload))
    c.execute("""INSERT INTO expectancy_evidence
        (campaign_id,run_id,evidence_complete,net_r,decision_time,resolved_at,source_decision_id,evidence_type)
        VALUES(?,?,?,?,?,?,?,?)""",("camp","run",1,-0.8,"2026-09-20T17:00:00Z","2026-09-20T18:00:00Z","guided","REJECT_FORWARD"))
    c.commit(); c.close()
    r=report(db)
    assert gate(r,"REJECT_FORWARD_OUTCOME_EVIDENCE")["status"]==PASS
    assert gate(r,"EXPECTANCY_TEMPORAL_EVIDENCE")["status"]==PASS


def test_unrelated_backtest_and_historical_paper_rows_do_not_change_active_run_readiness(tmp_path):
    db=tmp_path/"campaign.db"; make_db(db); c=sqlite3.connect(db)
    c.execute("INSERT INTO burnin_observations VALUES(?,?,?,?,?)",("run","BTCUSDT","ACCEPTED","PAPER",'{"signal_id":"current"}'))
    c.execute("INSERT INTO order_decisions VALUES(?,?,?,?,?,?,?,?,?,?)",("current-d","current","ACCEPTED","PAPER",1.25,0,.1,.1,1,.1))
    c.execute("INSERT INTO trade_lifecycle_events VALUES(?,?,?)",("current","FILLED","PAPER"))

    c.execute("INSERT INTO burnin_observations VALUES(?,?,?,?,?)",("old-run","BTCUSDT","ACCEPTED","PAPER",'{"signal_id":"old"}'))
    c.executemany("INSERT INTO order_decisions VALUES(?,?,?,?,?,?,?,?,?,?)",[
        ("old-duplicate","old","ACCEPTED","PAPER",1.2,0,None,.1,1,.1),
        ("old-duplicate","old","ACCEPTED","PAPER",1.2,0,None,.1,1,.1),
        ("backtest-only","bt","ACCEPTED","BACKTEST",9.0,0,None,None,None,None),
    ])
    c.execute("INSERT INTO trade_lifecycle_events VALUES(?,?,?)",("old","ERROR","PAPER"))
    c.execute("INSERT INTO trade_lifecycle_events VALUES(?,?,?)",("bt","ERROR","BACKTEST"))
    c.commit(); c.close()

    r=report(db)
    assert gate(r,"DUPLICATE_EXECUTION")["status"]==PASS
    assert gate(r,"RUNTIME_ERRORS")["status"]==PASS
    assert gate(r,"ACCEPTED_LIFECYCLE_EVIDENCE")["status"]==PASS
    assert gate(r,"EXECUTION_COST_EVIDENCE")["status"]==PASS


def test_unrelated_lifecycle_cannot_satisfy_current_accepted_signal(tmp_path):
    db=tmp_path/"campaign.db"; make_db(db); c=sqlite3.connect(db)
    c.execute("INSERT INTO burnin_observations VALUES(?,?,?,?,?)",("run","BTCUSDT","ACCEPTED","PAPER",'{"signal_id":"current"}'))
    c.execute("INSERT INTO order_decisions VALUES(?,?,?,?,?,?,?,?,?,?)",("current-d","current","ACCEPTED","PAPER",1.25,0,.1,.1,1,.1))
    c.execute("INSERT INTO trade_lifecycle_events VALUES(?,?,?)",("other","FILLED","PAPER"))
    c.commit(); c.close()

    assert gate(report(db),"ACCEPTED_LIFECYCLE_EVIDENCE")["status"]==NEEDS_FIX


def test_release_safety_gates_require_contract_verified_evidence(tmp_path):
    db=tmp_path/"campaign.db"; make_db(db); seed_release_safety_evidence(db)
    r=report(db)
    assert gate(r,"ROLLBACK_EVIDENCE")["status"]==PASS
    assert gate(r,"RUNBOOK_EVIDENCE")["status"]==PASS


def test_newer_unrelated_rollback_validation_cannot_contaminate_release_scope(tmp_path):
    db=tmp_path/"campaign.db"; make_db(db); seed_release_safety_evidence(db,validation_id="linked")
    c=sqlite3.connect(db)
    c.execute("INSERT INTO live_rollback_validation_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",(
        "unrelated","2026-09-20T18:13:00Z","INCOMPLETE","DETERMINISTIC_VALIDATION",0,0,0,0,4,'["UNRELATED_FAILURE"]',
    ))
    c.commit(); c.close()
    assert gate(report(db),"ROLLBACK_EVIDENCE")["status"]==PASS


def test_status_only_release_safety_pass_rows_are_blocked(tmp_path):
    db=tmp_path/"campaign.db"; make_db(db); c=sqlite3.connect(db)
    c.execute("INSERT INTO rollback_verification_events VALUES(?,?,?,?)",(
        "rel","PASS","2026-09-20T18:00:01Z","{}",
    ))
    c.execute("INSERT INTO runbook_evidence VALUES(?,?,?,?)",(
        "rel","PASS","2026-09-20T18:00:01Z","{}",
    ))
    c.commit(); c.close()
    r=report(db)
    assert gate(r,"ROLLBACK_EVIDENCE")["status"]==BLOCKED
    assert gate(r,"RUNBOOK_EVIDENCE")["status"]==BLOCKED


def test_soak_gate_accepts_canonical_canary_qualified_aggregate_snapshot(tmp_path):
    db=tmp_path/"campaign.db"; make_db(db)
    seed_soak_snapshot(db)
    g=gate(report(db),"SOAK_EVIDENCE")
    assert g["status"]==PASS
    assert g["reason"]=="SOAK_QUALIFICATION_PASS"


def test_soak_gate_rejects_nonqualified_verdicts(tmp_path):
    for verdict in ("BURN_IN_INSUFFICIENT","BURN_IN_FAILED","CANARY_SUSPENDED"):
        db=tmp_path/f"{verdict}.db"; make_db(db)
        seed_soak_snapshot(db,status=verdict)
        g=gate(report(db),"SOAK_EVIDENCE")
        assert g["status"]==BLOCKED
        assert g["reason"]=="SOAK_QUALIFICATION_INCOMPLETE"


def test_soak_gate_rejects_scope_mismatch_and_stale_source_run_set(tmp_path):
    db=tmp_path/"campaign.db"; make_db(db)
    seed_soak_snapshot(db,source_runs=["older-run"])
    g=gate(report(db),"SOAK_EVIDENCE")
    assert g["status"]==BLOCKED
    assert g["reason"]=="SOAK_QUALIFICATION_SCOPE_MISMATCH"


def test_unrelated_qualification_snapshot_cannot_satisfy_campaign_soak_gate(tmp_path):
    db=tmp_path/"campaign.db"; make_db(db)
    seed_soak_snapshot(db,campaign_id="other-campaign",release_id="other-release")
    g=gate(report(db),"SOAK_EVIDENCE")
    assert g["status"]==NOT_OBSERVABLE


def test_release_safety_evidence_from_different_commit_is_blocked(tmp_path):
    db=tmp_path/"campaign.db"; make_db(db); seed_release_safety_evidence(db)
    c=sqlite3.connect(db)
    rb=json.loads(c.execute("SELECT evidence_json FROM rollback_verification_events").fetchone()[0])
    rb["git_commit"]="different"
    c.execute("UPDATE rollback_verification_events SET evidence_json=?",(json.dumps(rb),))
    run=json.loads(c.execute("SELECT evidence_json FROM runbook_evidence").fetchone()[0])
    run["git_commit"]="different"
    c.execute("UPDATE runbook_evidence SET evidence_json=?",(json.dumps(run),))
    c.commit(); c.close()
    r=report(db)
    assert gate(r,"ROLLBACK_EVIDENCE")["status"]==BLOCKED
    assert gate(r,"RUNBOOK_EVIDENCE")["status"]==BLOCKED
