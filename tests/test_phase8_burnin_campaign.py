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
from alphaforge.burnin import persist_burnin_trade_outcome, persist_burnin_reject_outcome, persist_burnin_observation
from alphaforge.burnin_qualification import BurnInThresholds
from alphaforge.burnin_campaign import qualify_campaign, BurnInCampaignRunner
from alphaforge.burnin_resolver import persist_pending_reject_label

COSTS={"spread_cost":0.01,"entry_slippage_cost":0.01,"exit_slippage_cost":0.01,"fee_cost":0.01,"funding_cost":0.0,"latency_cost":0.0}

def _engine(path):
    return create_engine(f"sqlite+pysqlite:///{path}", future=True)

def _seed_campaign_for_qualification(tmp_path):
    db=tmp_path/'q.db'; conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='relq',duration_days=1,symbols=['BTCUSDT'],intervals=['1h'])
    run=start_or_resume_campaign(conn,camp.campaign_id)
    for i in range(2):
        persist_burnin_observation(conn,observation_id=f'o{i}',burnin_run_id=run['burnin_run_id'],release_id='relq',execution_mode='PAPER',decision='ACCEPTED',symbol='BTCUSDT',source_provenance={'provider':'PAPER'})
        persist_burnin_trade_outcome(conn,outcome_id=f't{i}',burnin_run_id=run['burnin_run_id'],release_id='relq',trade_id=f'trade{i}',symbol='BTCUSDT',regime='TRENDING',gross_r=1.0,gross_pnl=1.0,costs=COSTS,net_r=0.96,net_pnl=0.96,exit_reason='TP_HIT')
    persist_burnin_observation(conn,observation_id='or',burnin_run_id=run['burnin_run_id'],release_id='relq',execution_mode='PAPER',decision='REJECTED',symbol='BTCUSDT',source_provenance={'provider':'PAPER'})
    persist_burnin_reject_outcome(conn,reject_outcome_id='r1',burnin_run_id=run['burnin_run_id'],release_id='relq',reject_reason='LOW_CONFIDENCE',symbol='BTCUSDT',regime='TRENDING',forward_label='SL_BEFORE_TP',hypothetical_gross_r=-1.0,hypothetical_net_r_after_costs=-1.04,avoided_loss=1.04,missed_profit=0.0)
    conn.execute("INSERT INTO burnin_regime_metrics(burnin_run_id,release_id,regime,sample_count,accepted_count,rejected_count,mean_net_r,lower_confidence_bound_expectancy,max_drawdown,cost_drag,slippage_distribution_json,reject_accuracy,execution_failure_count,status,generated_at,schema_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(run['burnin_run_id'],'relq','TRENDING',3,2,1,0.5,0.1,0.01,0.01,'{}',1.0,0,'PASS','2026-01-01T00:00:00Z','test'))
    conn.execute("INSERT INTO burnin_execution_metrics(burnin_run_id,release_id,metric_window,spread_baseline,spread_current,slippage_baseline,slippage_current,latency_baseline,latency_current,fill_probability_baseline,fill_probability_current,liquidity_depth_baseline,liquidity_depth_current,timeout_rate,execution_rejects,stale_data_count,reconciliation_quality,funding_cost,price_impact_proxy,status,generated_at,schema_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(run['burnin_run_id'],'relq','campaign',1,1,1,1,1,1,1,1,1,1,0,0,0,'CLEAN',0,0,'PASS','2026-01-01T00:00:00Z','test'))
    conn.execute("INSERT INTO burnin_calibration_metrics(burnin_run_id,release_id,scope,sample_count,brier_score,log_loss,calibration_error,expected_calibration_error,reliability_buckets_json,observed_vs_predicted_json,status,generated_at,schema_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",(run['burnin_run_id'],'relq','all',10,0.1,0.1,0.01,0.01,'{}','{}','PASS','2026-01-01T00:00:00Z','test'))
    conn.execute("INSERT INTO burnin_drawdown_events(drawdown_event_id,burnin_run_id,release_id,peak_equity,trough_equity,drawdown_start,drawdown_end,drawdown_pct,drawdown_duration_seconds,recovery_duration_seconds,consecutive_losses,rolling_loss_cluster_json,rolling_expectancy,rolling_cost_drag,rolling_slippage,rolling_reject_accuracy,resolved,payload_json,schema_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",('dd1',run['burnin_run_id'],'relq',1,0.99,'2026-01-01T00:00:00Z','2026-01-01T01:00:00Z',0.01,3600,3600,1,'{}',0.1,0.01,0.01,1.0,1,'{}','test'))
    conn.commit(); conn.close()
    return db,camp.campaign_id

def _qualify(db,cid,**overrides):
    th=BurnInThresholds(minimum_duration_seconds=0,minimum_total_decisions=1,minimum_accepted_trades=1,minimum_closed_trades=1,minimum_rejected_forward_outcomes=1,minimum_regime_sample=1,minimum_regime_coverage=1,minimum_calibration_sample=1,require_operator_ack=False,require_phase1_6_gates=False,**overrides)
    e=_engine(db)
    try: return qualify_campaign(e,cid,th)
    finally: e.dispose()

def _latest_blockers(db):
    conn=sqlite3.connect(db); row=conn.execute('select blockers_json from burnin_qualification_snapshots order by id desc limit 1').fetchone(); conn.close(); return json.loads(row[0])

def test_campaign_negative_material_regime_blocks(tmp_path):
    db,cid=_seed_campaign_for_qualification(tmp_path); conn=sqlite3.connect(db)
    conn.execute("UPDATE burnin_regime_metrics SET lower_confidence_bound_expectancy=-0.2 WHERE regime='TRENDING'"); conn.commit(); conn.close()
    _qualify(db,cid)
    assert any(b.startswith('NEGATIVE_MATERIAL_REGIME') for b in _latest_blockers(db))

def test_campaign_bad_calibration_and_metric_rows_alone_block(tmp_path):
    db,cid=_seed_campaign_for_qualification(tmp_path); conn=sqlite3.connect(db)
    conn.execute("UPDATE burnin_calibration_metrics SET calibration_error=0.9"); conn.commit(); conn.close()
    _qualify(db,cid)
    assert 'CALIBRATION_QUALITY_INSUFFICIENT' in _latest_blockers(db)

def test_campaign_excessive_drawdown_blocks(tmp_path):
    db,cid=_seed_campaign_for_qualification(tmp_path); conn=sqlite3.connect(db)
    conn.execute("UPDATE burnin_drawdown_events SET drawdown_pct=0.5"); conn.commit(); conn.close()
    _qualify(db,cid)
    assert 'DRAWDOWN_OR_LOSS_CLUSTER_BLOCKER' in _latest_blockers(db)

def test_campaign_concentration_breach_blocks(tmp_path):
    db,cid=_seed_campaign_for_qualification(tmp_path)
    _qualify(db,cid,max_symbol_concentration=0.1)
    assert 'SYMBOL_CONCENTRATION_BREACH' in _latest_blockers(db)

def test_campaign_dirty_reconciliation_and_missing_operator_ack_block(tmp_path):
    from alphaforge.runtime_state import RuntimeStateSnapshot, save_runtime_state_snapshot
    db,cid=_seed_campaign_for_qualification(tmp_path); e=_engine(db)
    save_runtime_state_snapshot(e, RuntimeStateSnapshot(mode='PAPER',requested_mode='PAPER',actual_mode='PAPER',runtime_status='OPERATING',instance_id='i',exchange_read_only_status='AVAILABLE',reconciliation_status='DIRTY',unknown_exchange_state=False))
    qualify_campaign(e,cid,BurnInThresholds(minimum_duration_seconds=0,minimum_total_decisions=1,minimum_accepted_trades=1,minimum_closed_trades=1,minimum_rejected_forward_outcomes=1,minimum_regime_sample=1,minimum_regime_coverage=1,minimum_calibration_sample=1,require_operator_ack=True,require_phase1_6_gates=False))
    e.dispose(); blockers=_latest_blockers(db)
    assert 'RECONCILIATION_NOT_CLEAN' in blockers
    assert 'OPERATOR_ACK_MISSING_OR_EXPIRED' in blockers

def test_campaign_missing_phase_gate_evidence_blocks_mutation_rollback_runbook_full_tests(tmp_path):
    db,cid=_seed_campaign_for_qualification(tmp_path); e=_engine(db)
    qualify_campaign(e,cid,BurnInThresholds(minimum_duration_seconds=0,minimum_total_decisions=1,minimum_accepted_trades=1,minimum_closed_trades=1,minimum_rejected_forward_outcomes=1,minimum_regime_sample=1,minimum_regime_coverage=1,minimum_calibration_sample=1,require_operator_ack=False,require_phase1_6_gates=True))
    e.dispose(); blockers=_latest_blockers(db)
    assert 'PHASE1_6_GATES_NOT_PASSING' in blockers
    assert 'ROLLBACK_NOT_VERIFIED' in blockers
    assert 'RUNBOOK_NOT_VERIFIED' in blockers
    assert 'FULL_TEST_EVIDENCE_MISSING' in blockers


def test_campaign_worker_runs_resolver_and_triggers_qualification(tmp_path):
    db=tmp_path/'worker.db'; conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='relw',duration_days=1,symbols=['BTCUSDT'],intervals=['1h'])
    run=start_or_resume_campaign(conn,camp.campaign_id)
    persist_pending_reject_label(conn,campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],reject_decision_id='rx',signal_id='s',symbol='BTCUSDT',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',entry=100,stop=90,target=120,horizon_seconds=1,execution_cost_assumptions=COSTS,regime='TRENDING',reject_reason='LOW_CONFIDENCE',source_provenance={'provider':'PAPER'})
    conn.commit(); conn.close()
    e=_engine(db)
    runner=BurnInCampaignRunner(e,camp.campaign_id,lambda symbol,start,end:[{'timestamp':'2026-01-01T00:00:01Z','high':130,'low':99}],resolver_failure_threshold=1,thresholds=BurnInThresholds(minimum_duration_seconds=0,minimum_total_decisions=0,minimum_accepted_trades=0,minimum_closed_trades=0,minimum_rejected_forward_outcomes=1,minimum_regime_sample=1,minimum_regime_coverage=1,minimum_calibration_sample=1,require_operator_ack=False,require_phase1_6_gates=False))
    res=runner.resolver_tick(); e.dispose()
    assert res['resolver_counts']['resolved'] == 1
    conn=sqlite3.connect(db)
    assert conn.execute("select count(*) from burnin_campaign_events where event_type='RESOLVER_BATCH'").fetchone()[0] == 1
    assert conn.execute("select count(*) from burnin_qualification_snapshots").fetchone()[0] >= 1
    conn.close()


def test_missing_reject_geometry_no_fabrication_and_not_counted(tmp_path):
    db=tmp_path/'geom.db'; conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='relg',duration_days=1,symbols=['BTCUSDT'],intervals=['1h'])
    run=start_or_resume_campaign(conn,camp.campaign_id)
    pid=persist_pending_reject_label(conn,campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],reject_decision_id='bad',signal_id='s',symbol='BTCUSDT',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',entry=100,stop=None,target=None,horizon_seconds=3600,execution_cost_assumptions=COSTS,regime='TRENDING',reject_reason='LOW_CONFIDENCE',source_provenance={'provider':'PAPER'})
    conn.commit()
    assert pid is None
    assert conn.execute('select count(*) from burnin_pending_reject_labels').fetchone()[0] == 0
    obs=conn.execute('select evidence_complete, missing_fields_json from burnin_observations').fetchone()
    assert obs[0] == 0 and 'stop' in obs[1] and 'target' in obs[1]
    conn.close()
