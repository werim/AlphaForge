from datetime import datetime, timezone
import sqlite3
from pathlib import Path
from alphaforge.live_readiness_agent import BLOCKED, NEEDS_FIX, NOT_OBSERVABLE, PASS, LiveReadinessAgent

NOW=datetime(2026,9,20,18,14,tzinfo=timezone.utc)
def meta(_): return {"commit":"abc","branch":"feat/live-readiness-agent-v1","dirty":False}
def make_db(path:Path):
    c=sqlite3.connect(path); c.executescript("""
    CREATE TABLE schema_migrations(x); CREATE TABLE burnin_campaigns(campaign_id,release_id,campaign_status,active_run_id,git_commit,config_hash,strategy_config_hash,last_heartbeat_at,last_error,symbols_json);
    CREATE TABLE burnin_runs(burnin_run_id,status,git_commit,config_hash,strategy_config_hash); CREATE TABLE burnin_campaign_runs(campaign_id,burnin_run_id,status);
    CREATE TABLE burnin_preflight_reports(campaign_id,status,generated_at); CREATE TABLE runtime_state_snapshots(campaign_id,burnin_run_id,reconciliation_status,reconciliation_mismatch_count,recovery_action_required,last_error,timestamp);
    CREATE TABLE order_decisions(decision_id,signal_id,decision,mode,effective_rr,execution_ctx_missing,spread_pct,expected_slippage_pct,latency_ms,funding_rate_pct); CREATE TABLE trade_lifecycle_events(signal_id,lifecycle_state,mode);
    CREATE TABLE burnin_reject_outcomes(burnin_run_id,evidence_complete,forward_label,hypothetical_net_r_after_costs,payload_json); CREATE TABLE expectancy_evidence(campaign_id,run_id,evidence_complete,net_r,decision_time,resolved_at,source_decision_id,evidence_type);
    CREATE TABLE burnin_qualification_snapshots(burnin_run_id,status,sample_status,evidence_completeness_status,generated_at); CREATE TABLE burnin_recovery_drills(campaign_id,status,generated_at);
    CREATE TABLE live_rollback_validation_evidence(recorded_at,evidence_status,execution_mutation_attempt_count); CREATE TABLE rollback_verification_events(release_id,status,verified_at); CREATE TABLE runbook_evidence(release_id,status,recorded_at);
    CREATE TABLE runtime_control_state(mode_requested,mode_running,updated_at); CREATE TABLE runtime_control_audit_events(action,success,event_ts,requested_mode); CREATE TABLE burnin_observations(burnin_run_id,symbol,decision,execution_mode,metrics_json);
    """)
    c.execute("INSERT INTO burnin_campaigns VALUES(?,?,?,?,?,?,?,?,?,?)",("camp","rel","RUNNING","run","abc","cfg","str","2026-09-20T18:13:59Z",None,'["BTCUSDT"]'))
    c.execute("INSERT INTO burnin_runs VALUES(?,?,?,?,?)",("run","RUNNING","abc","cfg","str")); c.execute("INSERT INTO burnin_campaign_runs VALUES(?,?,?)",("camp","run","RUNNING")); c.execute("INSERT INTO burnin_preflight_reports VALUES(?,?,?)",("camp","PASS","2026-09-20T18:00:00Z")); c.execute("INSERT INTO runtime_state_snapshots VALUES(?,?,?,?,?,?,?)",("camp","run","CLEAN",0,0,None,"2026-09-20T18:00:00Z")); c.execute("INSERT INTO runtime_control_state VALUES(?,?,?)",("PAPER","PAPER","2026-09-20T18:00:00Z")); c.execute("INSERT INTO burnin_observations VALUES(?,?,?,?,?)",("run","BTCUSDT",None,"PAPER","{}")); c.commit(); c.close()
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
    c.execute("ALTER TABLE burnin_reject_outcomes ADD COLUMN payload_json TEXT")
    c.execute("ALTER TABLE expectancy_evidence ADD COLUMN source_decision_id TEXT")
    c.execute("ALTER TABLE expectancy_evidence ADD COLUMN evidence_type TEXT")
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
