from __future__ import annotations

import json
from sqlalchemy import create_engine, text

from alphaforge.burnin import BurnInRun, bootstrap_burnin_schema, config_hash, persist_burnin_run, universe_hash
from alphaforge.burnin_qualification import BurnInQualificationEngine, BurnInThresholds
from alphaforge.runtime_state import RuntimeStateSnapshot, save_runtime_state_snapshot


def _engine():
    e=create_engine("sqlite+pysqlite:///:memory:", future=True)
    with e.begin() as c: bootstrap_burnin_schema(c)
    save_runtime_state_snapshot(e, RuntimeStateSnapshot(mode="PAPER", requested_mode="PAPER", actual_mode="PAPER", runtime_status="OPERATING", instance_id="i", exchange_read_only_status="AVAILABLE", reconciliation_status="CLEAN", unknown_exchange_state=False))
    return e

def _run(e):
    with e.begin() as c:
        persist_burnin_run(c, BurnInRun(burnin_run_id="r",release_id="rel",execution_mode="PAPER",git_commit="abc",config_hash=config_hash({"a":1}),strategy_config_hash=config_hash({"s":1}),universe_hash=universe_hash(["BTCUSDT","ETHUSDT","SOLUSDT"],["5m"]),source_provenance={"provider":"BINANCE_READONLY"},symbols=["BTCUSDT","ETHUSDT","SOLUSDT"],intervals=["5m"],observed_duration_seconds=1000,sample_count=10,accepted_count=4,rejected_count=6,closed_trade_count=4,data_completeness_status="PASS",evidence_completeness_status="PASS"))
        for i in range(4):
            c.execute(text("INSERT INTO burnin_observations(observation_id,burnin_run_id,release_id,observed_at,execution_mode,symbol,decision,evidence_complete,missing_fields_json,metrics_json,source_provenance_json,schema_version) VALUES (:id,'r','rel','2026-01-01T00:00:00Z','PAPER','BTCUSDT','ACCEPTED',1,'[]','{}','{}','v')"), {"id": f"obs-a-{i}"})
        for i in range(6):
            c.execute(text("INSERT INTO burnin_observations(observation_id,burnin_run_id,release_id,observed_at,execution_mode,symbol,decision,evidence_complete,missing_fields_json,metrics_json,source_provenance_json,schema_version) VALUES (:id,'r','rel','2026-01-01T00:00:00Z','PAPER','BTCUSDT','REJECTED',1,'[]','{}','{}','v')"), {"id": f"obs-r-{i}"})

def test_missing_costs_block_qualification():
    e=_engine(); _run(e)
    with e.begin() as c:
        c.execute(text("INSERT INTO burnin_trade_outcomes(outcome_id,burnin_run_id,release_id,symbol,regime,gross_r,net_r,evidence_complete,missing_cost_fields_json,payload_json,schema_version) VALUES ('o','r','rel','BTCUSDT','TRENDING',1.0,1.0,0,'[\"fee_cost\"]','{}','v')"))
    snap=BurnInQualificationEngine(e, BurnInThresholds(minimum_duration_seconds=1,minimum_total_decisions=1,minimum_accepted_trades=1,minimum_closed_trades=1,minimum_rejected_forward_outcomes=0,minimum_regime_coverage=0,minimum_calibration_sample=0,require_operator_ack=False,require_phase1_6_gates=False)).evaluate("r")
    assert snap.status != "CANARY_QUALIFIED"
    assert any("INCOMPLETE_COST_EVIDENCE" in b for b in snap.blockers)


def test_positive_lcb_can_qualify_but_live_not_enabled():
    e=_engine(); _run(e)
    with e.begin() as c:
        for i,sym in enumerate(["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT"]):
            c.execute(text("INSERT INTO burnin_trade_outcomes(outcome_id,burnin_run_id,release_id,symbol,regime,gross_r,gross_pnl,spread_cost,entry_slippage_cost,exit_slippage_cost,fee_cost,funding_cost,latency_cost,volatility_penalty,liquidity_penalty,total_execution_cost,net_r,net_pnl,evidence_complete,missing_cost_fields_json,payload_json,schema_version) VALUES (:id,'r','rel',:sym,'TRENDING',1,1,.01,.01,.01,.01,.01,.01,0,0,.06,.6,.6,1,'[]','{}','v')"), {"id":f"o{i}","sym":sym})
        for i in range(3):
            c.execute(text("INSERT INTO burnin_reject_outcomes(reject_outcome_id,burnin_run_id,release_id,reject_reason,symbol,regime,forward_label,avoided_loss,missed_profit,hypothetical_net_r_after_costs,evidence_complete,payload_json,schema_version) VALUES (:id,'r','rel','LOW_EFFECTIVE_RR','X','TRENDING','SL_BEFORE_TP',1,0,-1,1,'{}','v')"), {"id":f"rej{i}"})
        c.execute(text("INSERT INTO burnin_regime_metrics(burnin_run_id,release_id,regime,sample_count,accepted_count,rejected_count,mean_net_r,lower_confidence_bound_expectancy,status,generated_at,schema_version) VALUES ('r','rel','TRENDING',4,4,3,.6,.5,'PASS','now','v')"))
        c.execute(text("INSERT INTO burnin_calibration_metrics(burnin_run_id,release_id,scope,sample_count,calibration_error,status,generated_at,schema_version) VALUES ('r','rel','GLOBAL',3,.01,'PASS','now','v')"))
        c.execute(text("INSERT INTO burnin_execution_metrics(burnin_run_id,release_id,metric_window,status,generated_at,schema_version) VALUES ('r','rel','CURRENT','STABLE','now','v')"))
    th=BurnInThresholds(minimum_duration_seconds=1,minimum_total_decisions=1,minimum_accepted_trades=1,minimum_closed_trades=1,minimum_rejected_forward_outcomes=1,minimum_regime_coverage=1,minimum_regime_sample=1,minimum_calibration_sample=1,max_symbol_concentration=.99,max_trade_contribution=.99,max_regime_concentration=1.0,min_lower_confidence_bound_expectancy=.01,require_operator_ack=False,require_phase1_6_gates=False)
    snap=BurnInQualificationEngine(e, th).evaluate("r")
    assert snap.status == "CANARY_QUALIFIED"
    assert snap.status not in {"LIVE_REAL_ORDERS_READY","LIVE_ENABLED","PROMOTED_TO_LIVE"}


def test_unknown_regime_cannot_pass():
    e=_engine(); _run(e)
    with e.begin() as c:
        c.execute(text("INSERT INTO burnin_regime_metrics(burnin_run_id,release_id,regime,sample_count,accepted_count,rejected_count,mean_net_r,lower_confidence_bound_expectancy,status,generated_at,schema_version) VALUES ('r','rel','UNKNOWN',99,1,98,1,1,'PASS','now','v')"))
    snap=BurnInQualificationEngine(e, BurnInThresholds(minimum_duration_seconds=1,minimum_total_decisions=1,minimum_accepted_trades=1,minimum_closed_trades=1,minimum_rejected_forward_outcomes=0,minimum_regime_coverage=1,minimum_regime_sample=1,minimum_calibration_sample=0,require_operator_ack=False,require_phase1_6_gates=False)).evaluate("r")
    assert "UNKNOWN_REGIME_CANNOT_PASS" in snap.blockers


def test_evaluate_missing_schema_does_not_create_tables():
    e=create_engine("sqlite+pysqlite:///:memory:", future=True)
    snap=BurnInQualificationEngine(e).evaluate("missing")
    assert snap.status == "BURN_IN_INSUFFICIENT"
    assert "BURNIN_SCHEMA_OR_EVIDENCE_MISSING" in snap.blockers
    with e.connect() as c:
        tables={r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert "burnin_runs" not in tables


def test_missing_phase6_and_operator_ack_block_qualification():
    e=_engine(); _run(e)
    snap=BurnInQualificationEngine(e, BurnInThresholds(minimum_duration_seconds=1,minimum_total_decisions=1,minimum_accepted_trades=1,minimum_closed_trades=1,minimum_rejected_forward_outcomes=0,minimum_regime_coverage=0,minimum_calibration_sample=0)).evaluate("r")
    assert "OPERATOR_ACK_MISSING_OR_EXPIRED" in snap.blockers
    assert "PHASE1_6_GATES_NOT_PASSING" in snap.blockers
    assert "RELEASE_GATE_NOT_READY" in snap.blockers
    assert "FULL_TEST_EVIDENCE_MISSING" in snap.blockers


def test_suspension_reasons_are_persisted_separately():
    e=_engine(); _run(e)
    th=BurnInThresholds(require_operator_ack=False, require_phase1_6_gates=False)
    engine=BurnInQualificationEngine(e, th)
    snap=engine._snapshot("r","rel","CANARY_SUSPENDED",["SPREAD_DEGRADATION","MUTATION_ATTEMPT_DETECTED","RUNBOOK_NOT_VERIFIED"],[],{}, {"max_drawdown_pct": .2, "lower_confidence_bound_expectancy": -1, "symbol_concentration": 1})
    with e.begin() as c:
        engine.persist_suspension(c, snap, engine.suspension_reasons(snap))
    with e.connect() as c:
        rows=c.execute(text("SELECT reason_codes_json FROM burnin_suspension_events WHERE burnin_run_id='r'")).fetchall()
    reasons=[json.loads(r[0])[0] for r in rows]
    assert {"SPREAD_DEGRADATION","MUTATION_ATTEMPT","RUNBOOK_INVALIDATION","DRAWDOWN_BREACH","ROLLING_EXPECTANCY_BREACH"}.issubset(set(reasons))


def test_all_required_phase7_and_phase6_evidence_canary_qualified():
    from alphaforge.release_gates import ensure_release_gate_schema, persist_operator_ack, persist_canary_event, persist_release_snapshot, ReleaseGateSnapshot
    e=_engine(); _run(e)
    ensure_release_gate_schema(e)
    persist_operator_ack(e, release_id="rel", phase="PHASE6", valid_until="2099-01-01T00:00:00Z")
    persist_canary_event(e, release_id="rel", phase="PHASE6", mutation_attempted=False)
    with e.begin() as c:
        c.execute(text("INSERT INTO rollback_verification_events(verification_id,release_id,phase,verified_at,status,evidence_json) VALUES ('rb','rel','PHASE6','now','PASS','{}')"))
        c.execute(text("INSERT INTO runbook_evidence(evidence_id,release_id,phase,recorded_at,status,evidence_json) VALUES ('run','rel','PHASE6','now','PASS','{}')"))
    persist_release_snapshot(e, ReleaseGateSnapshot(release_id="rel", phase="PHASE6", status="CANARY_READY", generated_at="now", canary_ready=True, rollback_verified=True, runbook_verified=True, operator_acknowledged=True, mutation_attempt_count=0, blocking_reasons=[], evidence={"full_tests":{"status":"PASS"}}))
    with e.begin() as c:
        for i,sym in enumerate(["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT"]):
            c.execute(text("INSERT INTO burnin_trade_outcomes(outcome_id,burnin_run_id,release_id,symbol,regime,gross_r,gross_pnl,spread_cost,entry_slippage_cost,exit_slippage_cost,fee_cost,funding_cost,latency_cost,volatility_penalty,liquidity_penalty,total_execution_cost,net_r,net_pnl,evidence_complete,missing_cost_fields_json,payload_json,schema_version) VALUES (:id,'r','rel',:sym,'TRENDING',1,1,.01,.01,.01,.01,.01,.01,0,0,.06,.6,.6,1,'[]','{}','v')"), {"id":f"all-o{i}","sym":sym})
        for i in range(3):
            c.execute(text("INSERT INTO burnin_reject_outcomes(reject_outcome_id,burnin_run_id,release_id,reject_reason,symbol,regime,forward_label,avoided_loss,missed_profit,hypothetical_net_r_after_costs,evidence_complete,payload_json,schema_version) VALUES (:id,'r','rel','LOW_EFFECTIVE_RR','X','TRENDING','SL_BEFORE_TP',1,0,-1,1,'{}','v')"), {"id":f"all-r{i}"})
        c.execute(text("INSERT INTO burnin_regime_metrics(burnin_run_id,release_id,regime,sample_count,accepted_count,rejected_count,mean_net_r,lower_confidence_bound_expectancy,status,generated_at,schema_version) VALUES ('r','rel','TRENDING',4,4,3,.6,.5,'PASS','now','v')"))
        c.execute(text("INSERT INTO burnin_calibration_metrics(burnin_run_id,release_id,scope,sample_count,calibration_error,status,generated_at,schema_version) VALUES ('r','rel','GLOBAL',3,.01,'PASS','now','v')"))
        c.execute(text("INSERT INTO burnin_execution_metrics(burnin_run_id,release_id,metric_window,status,spread_baseline,spread_current,slippage_baseline,slippage_current,latency_baseline,latency_current,fill_probability_baseline,fill_probability_current,stale_data_count,execution_rejects,generated_at,schema_version) VALUES ('r','rel','CURRENT','STABLE',1,1,1,1,1,1,.9,.9,0,0,'now','v')"))
    th=BurnInThresholds(minimum_duration_seconds=1,minimum_total_decisions=1,minimum_accepted_trades=1,minimum_closed_trades=1,minimum_rejected_forward_outcomes=1,minimum_regime_coverage=1,minimum_regime_sample=1,minimum_calibration_sample=1,max_symbol_concentration=.99,max_trade_contribution=.99,max_regime_concentration=1.0,min_lower_confidence_bound_expectancy=.01)
    snap=BurnInQualificationEngine(e, th).evaluate("r")
    assert snap.status == "CANARY_QUALIFIED"


def test_pending_reject_does_not_count_as_forward_outcome():
    e=_engine(); _run(e)
    snap=BurnInQualificationEngine(e, BurnInThresholds(minimum_duration_seconds=1,minimum_total_decisions=1,minimum_accepted_trades=1,minimum_closed_trades=0,minimum_rejected_forward_outcomes=1,minimum_regime_coverage=0,minimum_calibration_sample=0,require_operator_ack=False,require_phase1_6_gates=False)).evaluate("r")
    assert snap.metrics["completed_rejected_forward_outcomes"] == 0
    assert snap.metrics["pending_rejected_forward_outcomes"] == 6
    assert any("MINIMUM_REJECTED_FORWARD_OUTCOMES" in b for b in snap.blockers)


def test_non_attributable_shadow_and_infrastructure_outcomes_are_diagnostic_only():
    e=_engine(); _run(e)
    rows = [
        ("shadow", "MTF_EXECUTION_COUNTER_REGIME", 0, 50,
         {"forward_label_subject":"LEGACY_SCANNER_SHADOW_CANDIDATE",
          "forward_label_side":"LONG", "authoritative_mtf_side":"SHORT"}),
        ("guided", "LOW_EFFECTIVE_RR", 1, 0,
         {"forward_label_subject":"GUIDED_CANDIDATE"}),
        ("infra", "EXCHANGE_STATE_UNKNOWN", 50, 0,
         {"forward_label_subject":"GUIDED_CANDIDATE"}),
    ]
    with e.begin() as c:
        for oid, reason, avoided, missed, payload in rows:
            c.execute(text("INSERT INTO burnin_reject_outcomes(reject_outcome_id,burnin_run_id,release_id,reject_reason,symbol,regime,forward_label,avoided_loss,missed_profit,hypothetical_net_r_after_costs,evidence_complete,payload_json,schema_version) VALUES (:id,'r','rel',:reason,'BTCUSDT','TRENDING','SL_BEFORE_TP',:avoided,:missed,:net,1,:payload,'v')"), {"id":oid,"reason":reason,"avoided":avoided,"missed":missed,"net":avoided-missed,"payload":json.dumps(payload)})
    th=BurnInThresholds(minimum_duration_seconds=1,minimum_total_decisions=1,minimum_accepted_trades=0,minimum_closed_trades=0,minimum_rejected_forward_outcomes=1,minimum_regime_coverage=0,minimum_calibration_sample=0,require_operator_ack=False,require_phase1_6_gates=False)
    snap=BurnInQualificationEngine(e, th).evaluate("r")

    assert snap.metrics["completed_rejected_forward_outcomes"] == 3
    assert snap.metrics["attributable_rejected_forward_outcomes"] == 1
    assert snap.metrics["non_attributable_rejected_forward_outcomes"] == 2
    assert snap.metrics["reject_precision"] == 1.0
    assert snap.metrics["false_reject_rate"] == 0.0
    assert snap.metrics["net_reject_value"] == 1.0
    assert snap.metrics["reject_value_by_reason"] == {"LOW_EFFECTIVE_RR": 1.0}
