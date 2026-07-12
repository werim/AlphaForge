import json, sqlite3
from sqlalchemy import create_engine
from alphaforge.burnin_campaign import create_campaign, start_or_resume_campaign, pause_campaign, get_campaign, aggregate_campaign, export_campaign_bundle
from alphaforge.burnin import persist_burnin_observation


def test_campaign_create_start_resume_pause_and_export(tmp_path):
    db=tmp_path/'a.db'; conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='rel1',duration_days=7,symbols=['BTCUSDT'],intervals=['1h'])
    conn.commit()
    first=start_or_resume_campaign(conn,camp.campaign_id); conn.commit()
    persist_burnin_observation(conn,observation_id='o1',burnin_run_id=first['burnin_run_id'],release_id='rel1',execution_mode='PAPER',decision='REJECTED',source_provenance={'provider':'PAPER'})
    second=start_or_resume_campaign(conn,camp.campaign_id,resume=True); conn.commit()
    assert first['burnin_run_id'] != second['burnin_run_id']
    assert get_campaign(conn,camp.campaign_id)['restart_count'] == 1
    pause_campaign(conn,camp.campaign_id); conn.commit()
    assert get_campaign(conn,camp.campaign_id)['campaign_status'] == 'PAUSED'
    agg=aggregate_campaign(conn,camp.campaign_id)
    assert agg['metrics']['sample_count'] == 1
    conn.close()
    out=export_campaign_bundle(db,tmp_path/'out',camp.campaign_id)
    assert (tmp_path/'out'/f"burnin_campaign_{camp.campaign_id}"/'manifest.json').exists()
    assert out['manifest']['source_run_ids'] == [first['burnin_run_id'], second['burnin_run_id']]


def test_config_drift_pauses_campaign(tmp_path):
    conn=sqlite3.connect(tmp_path/'a.db'); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='rel1',duration_days=7,symbols=['BTCUSDT'],intervals=['1h'])
    conn.commit()
    try:
        start_or_resume_campaign(conn,camp.campaign_id,config_hash='different')
    except ValueError as exc:
        assert 'CONFIG_DRIFT' in str(exc)
    else:
        raise AssertionError('expected drift')
    assert get_campaign(conn,camp.campaign_id)['campaign_status'] == 'PAUSED'

from sqlalchemy import create_engine
from alphaforge.burnin import persist_burnin_trade_outcome, persist_burnin_observation
from alphaforge.burnin_campaign import qualify_campaign, mark_worker_started, update_campaign_heartbeat, detect_stale_worker, check_campaign_completion
from alphaforge.burnin_qualification import BurnInThresholds

COSTS={"spread_cost":0.0,"entry_slippage_cost":0.0,"exit_slippage_cost":0.0,"fee_cost":0.0,"funding_cost":0.0,"latency_cost":0.0}

def _two_run_campaign(tmp_path):
    db=tmp_path/'q.db'; conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='relq',duration_days=0,symbols=['BTCUSDT'],intervals=['1h'],target_decisions=2,target_closed_trades=2,target_reject_forward_outcomes=0)
    r1=start_or_resume_campaign(conn,camp.campaign_id)['burnin_run_id']
    r2=start_or_resume_campaign(conn,camp.campaign_id,resume=True)['burnin_run_id']
    for i,rid in enumerate([r1,r2]):
        persist_burnin_observation(conn,observation_id=f'o{i}',burnin_run_id=rid,release_id='relq',execution_mode='PAPER',decision='ACCEPTED',source_provenance={'provider':'PAPER'})
    return db,conn,camp,r1,r2

def test_campaign_qualification_uses_all_runs_and_early_loss_cannot_be_hidden(tmp_path):
    db,conn,camp,r1,r2=_two_run_campaign(tmp_path)
    persist_burnin_trade_outcome(conn,outcome_id='loss',burnin_run_id=r1,release_id='relq',symbol='BTCUSDT',gross_r=-2.0,gross_pnl=-2.0,costs=COSTS,net_r=-2.0,net_pnl=-2.0)
    persist_burnin_trade_outcome(conn,outcome_id='win',burnin_run_id=r2,release_id='relq',symbol='BTCUSDT',gross_r=1.0,gross_pnl=1.0,costs=COSTS,net_r=1.0,net_pnl=1.0)
    conn.commit(); engine=create_engine(f'sqlite+pysqlite:///{db}',future=True)
    res=qualify_campaign(engine,camp.campaign_id,BurnInThresholds(minimum_duration_seconds=0,minimum_total_decisions=2,minimum_closed_trades=2,minimum_accepted_trades=0,minimum_rejected_forward_outcomes=0,minimum_regime_coverage=0,minimum_calibration_sample=0,require_operator_ack=False,require_phase1_6_gates=False))
    assert res['source_run_ids'] == [r1,r2]
    assert res['metrics']['closed_trade_count'] == 2
    assert res['metrics']['mean_net_r'] < 0
    assert res['verdict'] != 'CANARY_QUALIFIED'
    # Mutating earlier evidence changes aggregate hash/verdict inputs, proving latest run did not hide it.
    old_hash=res['aggregate_evidence_hash']
    conn.execute("UPDATE burnin_trade_outcomes SET net_r=3.0,gross_r=3.0 WHERE outcome_id='loss'"); conn.commit()
    res2=qualify_campaign(engine,camp.campaign_id,BurnInThresholds(minimum_duration_seconds=0,minimum_total_decisions=2,minimum_closed_trades=2,minimum_accepted_trades=0,minimum_rejected_forward_outcomes=0,minimum_regime_coverage=0,minimum_calibration_sample=0,require_operator_ack=False,require_phase1_6_gates=False,min_lower_confidence_bound_expectancy=-10))
    assert res2['aggregate_evidence_hash'] != old_hash
    engine.dispose(); conn.close()

def test_campaign_qualification_blocks_incompatible_run(tmp_path):
    db,conn,camp,r1,r2=_two_run_campaign(tmp_path)
    conn.execute("UPDATE burnin_runs SET universe_hash='different' WHERE burnin_run_id=?",(r2,)); conn.commit()
    engine=create_engine(f'sqlite+pysqlite:///{db}',future=True)
    res=qualify_campaign(engine,camp.campaign_id,BurnInThresholds(minimum_duration_seconds=0,minimum_total_decisions=0,minimum_closed_trades=0,minimum_rejected_forward_outcomes=0,require_operator_ack=False,require_phase1_6_gates=False))
    assert 'CAMPAIGN_UNIVERSE_DRIFT' in res['blockers']
    assert res['verdict'] == 'BURN_IN_FAILED'
    engine.dispose(); conn.close()

def test_worker_heartbeat_and_stale_recovery(tmp_path):
    conn=sqlite3.connect(tmp_path/'h.db'); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='relh',duration_days=0,symbols=['BTCUSDT'],intervals=['1h'])
    start_or_resume_campaign(conn,camp.campaign_id); mark_worker_started(conn,camp.campaign_id,pid=1234); update_campaign_heartbeat(conn,camp.campaign_id,runtime_status='OPERATING')
    conn.execute("UPDATE burnin_campaigns SET stale_worker_timeout_seconds=1,last_heartbeat_at='2000-01-01T00:00:00Z' WHERE campaign_id=?",(camp.campaign_id,)); conn.commit()
    res=detect_stale_worker(conn,camp.campaign_id)
    assert res['status'] == 'RECOVERY_REQUIRED'
    assert get_campaign(conn,camp.campaign_id)['campaign_status'] == 'RECOVERY_REQUIRED'

def test_completion_rules_require_targets_and_final_qualification(tmp_path):
    conn=sqlite3.connect(tmp_path/'c.db'); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='relc',duration_days=0,symbols=['BTCUSDT'],intervals=['1h'],target_decisions=0,target_closed_trades=0,target_reject_forward_outcomes=0)
    assert 'FINAL_QUALIFICATION_MISSING' in check_campaign_completion(conn,camp.campaign_id)['blockers']
