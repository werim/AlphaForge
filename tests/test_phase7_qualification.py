from __future__ import annotations

import json
from sqlalchemy import create_engine, text

from alphaforge.burnin import BurnInRun, bootstrap_burnin_schema, config_hash, persist_burnin_run, universe_hash
from alphaforge.burnin_qualification import BurnInQualificationEngine, BurnInThresholds


def _engine():
    e=create_engine("sqlite+pysqlite:///:memory:", future=True)
    with e.begin() as c: bootstrap_burnin_schema(c)
    return e

def _run(e):
    with e.begin() as c:
        persist_burnin_run(c, BurnInRun(burnin_run_id="r",release_id="rel",execution_mode="PAPER",git_commit="abc",config_hash=config_hash({"a":1}),strategy_config_hash=config_hash({"s":1}),universe_hash=universe_hash(["BTCUSDT","ETHUSDT","SOLUSDT"],["5m"]),source_provenance={"provider":"BINANCE_READONLY"},symbols=["BTCUSDT","ETHUSDT","SOLUSDT"],intervals=["5m"],observed_duration_seconds=1000,sample_count=10,accepted_count=4,rejected_count=6,closed_trade_count=4,data_completeness_status="PASS",evidence_completeness_status="PASS"))

def test_missing_costs_block_qualification():
    e=_engine(); _run(e)
    with e.begin() as c:
        c.execute(text("INSERT INTO burnin_trade_outcomes(outcome_id,burnin_run_id,release_id,symbol,regime,gross_r,net_r,evidence_complete,missing_cost_fields_json,payload_json,schema_version) VALUES ('o','r','rel','BTCUSDT','TRENDING',1.0,1.0,0,'[\"fee_cost\"]','{}','v')"))
    snap=BurnInQualificationEngine(e, BurnInThresholds(minimum_duration_seconds=1,minimum_total_decisions=1,minimum_accepted_trades=1,minimum_closed_trades=1,minimum_rejected_forward_outcomes=0,minimum_regime_coverage=0,minimum_calibration_sample=0)).evaluate("r")
    assert snap.status != "CANARY_QUALIFIED"
    assert any("INCOMPLETE_COST_EVIDENCE" in b for b in snap.blockers)


def test_positive_lcb_can_qualify_but_live_not_enabled():
    e=_engine(); _run(e)
    with e.begin() as c:
        for i,sym in enumerate(["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT"]):
            c.execute(text("INSERT INTO burnin_trade_outcomes(outcome_id,burnin_run_id,release_id,symbol,regime,gross_r,gross_pnl,spread_cost,entry_slippage_cost,exit_slippage_cost,fee_cost,funding_cost,latency_cost,volatility_penalty,liquidity_penalty,total_execution_cost,net_r,net_pnl,evidence_complete,missing_cost_fields_json,payload_json,schema_version) VALUES (:id,'r','rel',:sym,'TRENDING',1,1,.01,.01,.01,.01,.01,.01,0,0,.06,.6,.6,1,'[]','{}','v')"), {"id":f"o{i}","sym":sym})
        for i in range(3):
            c.execute(text("INSERT INTO burnin_reject_outcomes(reject_outcome_id,burnin_run_id,release_id,reject_reason,symbol,regime,avoided_loss,missed_profit,payload_json,schema_version) VALUES (:id,'r','rel','LOW_EFFECTIVE_RR','X','TRENDING',1,0,'{}','v')"), {"id":f"rej{i}"})
        c.execute(text("INSERT INTO burnin_regime_metrics(burnin_run_id,release_id,regime,sample_count,accepted_count,rejected_count,mean_net_r,lower_confidence_bound_expectancy,status,generated_at,schema_version) VALUES ('r','rel','TRENDING',4,4,3,.6,.5,'PASS','now','v')"))
        c.execute(text("INSERT INTO burnin_calibration_metrics(burnin_run_id,release_id,scope,sample_count,calibration_error,status,generated_at,schema_version) VALUES ('r','rel','GLOBAL',3,.01,'PASS','now','v')"))
        c.execute(text("INSERT INTO burnin_execution_metrics(burnin_run_id,release_id,metric_window,status,generated_at,schema_version) VALUES ('r','rel','CURRENT','STABLE','now','v')"))
    th=BurnInThresholds(minimum_duration_seconds=1,minimum_total_decisions=1,minimum_accepted_trades=1,minimum_closed_trades=1,minimum_rejected_forward_outcomes=1,minimum_regime_coverage=1,minimum_regime_sample=1,minimum_calibration_sample=1,max_symbol_concentration=.99,max_trade_contribution=.99,max_regime_concentration=1.0,min_lower_confidence_bound_expectancy=.01)
    snap=BurnInQualificationEngine(e, th).evaluate("r")
    assert snap.status == "CANARY_QUALIFIED"
    assert snap.status not in {"LIVE_REAL_ORDERS_READY","LIVE_ENABLED","PROMOTED_TO_LIVE"}


def test_unknown_regime_cannot_pass():
    e=_engine(); _run(e)
    with e.begin() as c:
        c.execute(text("INSERT INTO burnin_regime_metrics(burnin_run_id,release_id,regime,sample_count,accepted_count,rejected_count,mean_net_r,lower_confidence_bound_expectancy,status,generated_at,schema_version) VALUES ('r','rel','UNKNOWN',99,1,98,1,1,'PASS','now','v')"))
    snap=BurnInQualificationEngine(e, BurnInThresholds(minimum_duration_seconds=1,minimum_total_decisions=1,minimum_accepted_trades=1,minimum_closed_trades=1,minimum_rejected_forward_outcomes=0,minimum_regime_coverage=1,minimum_regime_sample=1,minimum_calibration_sample=0)).evaluate("r")
    assert "UNKNOWN_REGIME_CANNOT_PASS" in snap.blockers
