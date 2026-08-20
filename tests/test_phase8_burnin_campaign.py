import json, sqlite3
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
import alphaforge.burnin_campaign as campaign_module
from alphaforge.burnin_campaign import create_campaign, start_or_resume_campaign, pause_campaign, get_campaign, aggregate_campaign, export_campaign_bundle, build_phase8_campaign_identity
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
    persist_pending_reject_label(conn,campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],reject_decision_id='rx',signal_id='s',symbol='BTCUSDT',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',entry=100,stop=90,target=120,horizon_seconds=120,execution_cost_assumptions=COSTS,regime='TRENDING',reject_reason='LOW_CONFIDENCE',source_provenance={'provider':'PAPER'})
    conn.commit(); conn.close()
    e=_engine(db)
    runner=BurnInCampaignRunner(e,camp.campaign_id,lambda symbol,start,end:[{'timestamp':'2026-01-01T00:00:02Z','high':130,'low':99}],resolver_failure_threshold=1,thresholds=BurnInThresholds(minimum_duration_seconds=0,minimum_total_decisions=0,minimum_accepted_trades=0,minimum_closed_trades=0,minimum_rejected_forward_outcomes=1,minimum_regime_sample=1,minimum_regime_coverage=1,minimum_calibration_sample=1,require_operator_ack=False,require_phase1_6_gates=False))
    res=runner.resolver_tick(); e.dispose()
    assert res['resolver_counts']['resolved'] == 1
    conn=sqlite3.connect(db)
    assert conn.execute("select count(*) from burnin_campaign_events where event_type='RESOLVER_BATCH'").fetchone()[0] == 1
    assert conn.execute("select count(*) from burnin_qualification_snapshots").fetchone()[0] >= 1
    conn.close()


def test_materialize_lock_retries_with_fresh_connection(tmp_path, monkeypatch):
    db, cid = _seed_campaign_for_qualification(tmp_path)
    engine = _engine(db)
    original = campaign_module.materialize_campaign_aggregate
    seen = []

    def flaky(conn, campaign_id):
        if not isinstance(conn, campaign_module.Engine):
            seen.append(conn.connection.driver_connection)
            if len(seen) == 1:
                raise OperationalError("DELETE", {}, sqlite3.OperationalError("database is locked"))
        return original(conn, campaign_id)

    monkeypatch.setattr(campaign_module, "materialize_campaign_aggregate", flaky)
    try:
        assert flaky(engine, cid).endswith("__aggregate")
    finally:
        engine.dispose()
    assert len(seen) == 2
    assert seen[0] is not seen[1]


def test_resolver_lock_exhaustion_and_failure_event_lock_do_not_escape(tmp_path, monkeypatch, capsys):
    db, cid = _seed_campaign_for_qualification(tmp_path)
    engine = _engine(db)
    runner = BurnInCampaignRunner(engine, cid, lambda *_: [])
    locked = OperationalError("DELETE", {}, sqlite3.OperationalError("database is locked"))
    monkeypatch.setattr(runner, "_qualify_if_due", lambda: (_ for _ in ()).throw(locked))
    monkeypatch.setattr(campaign_module, "_with_fresh_lock_retry", lambda *_args, **_kwargs: (_ for _ in ()).throw(locked))
    try:
        result = runner.resolver_tick()
    finally:
        engine.dispose()
    assert result["status"] == "LOCK_RETRY_EXHAUSTED"
    assert runner.resolver_failure_count == 1
    assert "original=" in capsys.readouterr().err


def test_non_lock_operational_error_remains_fail_closed(tmp_path, monkeypatch):
    db, cid = _seed_campaign_for_qualification(tmp_path)
    engine = _engine(db)
    runner = BurnInCampaignRunner(engine, cid, lambda *_: [])
    schema_error = OperationalError("DELETE", {}, sqlite3.OperationalError("no such table: required_evidence"))
    monkeypatch.setattr(runner, "_qualify_if_due", lambda: (_ for _ in ()).throw(schema_error))
    try:
        with pytest.raises(OperationalError, match="no such table"):
            runner.resolver_tick()
    finally:
        engine.dispose()


def test_qualification_is_observation_gated_after_initial_snapshot(tmp_path):
    db, cid = _seed_campaign_for_qualification(tmp_path)
    engine = _engine(db)
    runner = BurnInCampaignRunner(engine, cid, lambda *_: [], qualification_interval_seconds=0, qualification_observation_threshold=25)
    runner._last_qualification_observation_count = 3
    runner._last_qualification_monotonic = 0
    with engine.begin() as conn:
        conn.execute(text("UPDATE burnin_campaigns SET latest_qualification_id='existing', target_decisions=500, target_closed_trades=30, target_reject_forward_outcomes=50 WHERE campaign_id=:cid"), {"cid": cid})
    try:
        assert runner._qualification_due() is False
    finally:
        engine.dispose()


def test_resolver_lock_wait_runs_off_event_loop_and_runtime_heartbeat_stays_fresh(tmp_path, monkeypatch):
    from alphaforge.runtime_heartbeat import evaluate_runtime_heartbeat_freshness, save_runtime_heartbeat
    import time as wall_time
    db, cid = _seed_campaign_for_qualification(tmp_path)
    engine = _engine(db)
    runner = BurnInCampaignRunner(engine, cid, lambda *_: [], resolver_interval_seconds=0)
    ticks = 0

    def exhausted():
        wall_time.sleep(0.2)
        return {"status": "LOCK_RETRY_EXHAUSTED"}

    monkeypatch.setattr(runner, "resolver_tick", exhausted)

    async def exercise():
        nonlocal ticks
        runner._stop_event = asyncio.Event()
        resolver = asyncio.create_task(runner._resolver_loop())
        deadline = asyncio.get_running_loop().time() + 0.12
        while asyncio.get_running_loop().time() < deadline:
            save_runtime_heartbeat(engine, runtime_instance_id="runtime", execution_mode="PAPER", scanner_source="test")
            ticks += 1
            await asyncio.sleep(0.02)
        runner._stop_event.set()
        await resolver

    try:
        asyncio.run(exercise())
        assert ticks >= 4
        # Heartbeats are persisted at whole-second precision. Allow one second
        # of quantization in addition to the one-second freshness assertion so
        # a boundary crossing cannot misclassify a heartbeat written by this
        # exercise; production freshness policy is unchanged.
        assert evaluate_runtime_heartbeat_freshness(engine, required_mode="PAPER", max_age_sec=2).is_fresh
    finally:
        engine.dispose()


def test_multiple_locked_maintenance_cycles_do_not_stop_or_stale_campaign(tmp_path, monkeypatch):
    db, cid = _seed_campaign_for_qualification(tmp_path)
    engine = _engine(db)
    runner = BurnInCampaignRunner(engine, cid, lambda *_: [], maintenance_interval_seconds=0.005)
    locked = OperationalError("UPDATE", {}, sqlite3.OperationalError("database is locked"))
    calls = 0

    def locked_tick():
        nonlocal calls
        calls += 1
        raise locked

    monkeypatch.setattr(runner, "_maintenance_tick", locked_tick)

    async def exercise():
        runner._stop_event = asyncio.Event()
        task = asyncio.create_task(runner._maintenance_loop())
        await asyncio.sleep(0.04)
        assert runner._stop_event.is_set() is False
        runner._stop_event.set()
        await task

    try:
        asyncio.run(exercise())
        assert calls >= 2
        with engine.connect() as conn:
            assert get_campaign(conn, cid)["campaign_status"] == "RUNNING"
    finally:
        engine.dispose()


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
import os, asyncio
from alphaforge.runtime import RuntimeOrchestrator, RuntimeConfig, ExecutionMode
from alphaforge.ai_brain import AIBrain
from sqlalchemy.orm import Session
from alphaforge.persistence import init_db

class _FakeRuntime:
    def __init__(self): self.persistence_engine=None; self.shutdown_called=False; self.attached=None
    def _attach_phase8_campaign(self, cid): self.attached=cid
    async def start(self): await asyncio.sleep(0.03); raise RuntimeError('stop fake runtime')
    def shutdown(self): self.shutdown_called=True

def _runtime_for_campaign(db, *, mode=ExecutionMode.PAPER):
    engine=init_db(f"sqlite+pysqlite:///{db}")
    brain=AIBrain(Session(engine), min_accept_score=0.62)
    rt=RuntimeOrchestrator(config=RuntimeConfig(execution_mode=mode), ai_brain=brain, market_scanner=lambda: asyncio.sleep(0,result=[]), persistence_engine=engine)
    return rt, engine

def _campaign_matching_runtime(db, rt):
    h=rt._phase8_runtime_hashes(["BTCUSDT"], ["1m"])
    conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id=h['release_id'],duration_days=1,symbols=["BTCUSDT"],intervals=["1m"])
    conn.execute("UPDATE burnin_campaigns SET config_hash=?, strategy_config_hash=?, universe_hash=?, execution_cost_config_hash=? WHERE campaign_id=?",(h['config_hash'],h['strategy_config_hash'],h['universe_hash'],h['execution_cost_config_hash'],camp.campaign_id))
    start_or_resume_campaign(conn,camp.campaign_id)
    conn.commit(); conn.close(); return camp.campaign_id,h

def test_runtime_attach_blocks_config_strategy_universe_execution_cost_release_and_mode_mismatches(tmp_path):
    for key, reason in [('config_hash','PHASE8_CAMPAIGN_RUN_IDENTITY_MISMATCH'),('strategy_config_hash','PHASE8_CAMPAIGN_RUN_IDENTITY_MISMATCH'),('universe_hash','PHASE8_CAMPAIGN_RUN_IDENTITY_MISMATCH'),('execution_cost_config_hash','PHASE8_CAMPAIGN_EXECUTION_COST_DRIFT'),('release_id','PHASE8_CAMPAIGN_RUN_IDENTITY_MISMATCH')]:
        db=tmp_path/f'{key}.db'; rt, engine=_runtime_for_campaign(db); cid,h=_campaign_matching_runtime(db,rt)
        col='release_id' if key=='release_id' else key
        with engine.begin() as conn: conn.execute(text(f"UPDATE burnin_campaigns SET {col}='mismatch' WHERE campaign_id=:cid"), {'cid':cid})
        try:
            rt._attach_phase8_campaign(cid)
        except RuntimeError as exc:
            assert reason in str(exc)
        else: raise AssertionError('expected mismatch')
        with engine.connect() as conn:
            row=conn.execute(text('select campaign_status,last_error from burnin_campaigns where campaign_id=:cid'), {'cid':cid}).fetchone()
        assert row[0]=='PAUSED' and row[1]==reason
        engine.dispose()
    db=tmp_path/'mode.db'; rt, engine=_runtime_for_campaign(db, mode=ExecutionMode.BACKTEST); cid,_=_campaign_matching_runtime(db,rt)
    try: rt._attach_phase8_campaign(cid)
    except RuntimeError as exc: assert 'PHASE8_CAMPAIGN_EXECUTION_MODE_INVALID' in str(exc)
    else: raise AssertionError('expected execution mode block')
    engine.dispose()


def test_runtime_attach_accepts_matching_campaign_and_same_database(tmp_path):
    db=tmp_path/'same.db'; rt, engine=_runtime_for_campaign(db); cid,_=_campaign_matching_runtime(db,rt)
    rt._attach_phase8_campaign(cid)
    with engine.connect() as conn:
        assert conn.execute(text("select count(*) from burnin_campaign_events where campaign_id=:cid and event_type='PHASE8_CAMPAIGN_ATTACHED'"), {'cid':cid}).scalar_one() == 1
    engine.dispose()


def test_runtime_attachment_records_full_release_mismatch_and_terminalizes_run(monkeypatch, tmp_path):
    monkeypatch.delenv("ALPHAFORGE_RELEASE_ID", raising=False)
    db=tmp_path/'release_source.db'; rt, engine=_runtime_for_campaign(db)
    conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    h=rt._phase8_runtime_hashes([], [])
    camp=create_campaign(conn, release_id='phase9_trial', duration_days=1, symbols=[], intervals=[])
    conn.execute("UPDATE burnin_campaigns SET config_hash=?, strategy_config_hash=?, universe_hash=?, execution_cost_config_hash=? WHERE campaign_id=?", (h['config_hash'],h['strategy_config_hash'],h['universe_hash'],h['execution_cost_config_hash'],camp.campaign_id))
    run=start_or_resume_campaign(conn,camp.campaign_id)['burnin_run_id']; conn.commit(); conn.close()
    with pytest.raises(RuntimeError, match='PHASE8_CAMPAIGN_RELEASE_MISMATCH'):
        rt._attach_phase8_campaign(camp.campaign_id)
    with engine.connect() as connection:
        event_row=connection.execute(text("SELECT details_json FROM burnin_campaign_events WHERE campaign_id=:cid AND event_type='PHASE8_CAMPAIGN_ATTACH_FAILED' ORDER BY id DESC LIMIT 1"), {'cid': camp.campaign_id}).scalar_one()
        details=json.loads(event_row)
        state=connection.execute(text("SELECT status, ended_at FROM burnin_campaign_runs WHERE burnin_run_id=:bid"), {'bid':run}).one()
    assert details['campaign_identity']['release_id'] == details['run_identity']['release_id'] == 'phase9_trial'
    assert details['runtime_identity']['release_id'] == 'default'
    assert details['identity_sources']['runtime_release'] == 'runtime_config:phase7_burnin_release_id'
    assert state.status == 'FAILED' and state.ended_at
    engine.dispose()


def test_resolver_loop_runs_automatically_without_manual_cli(tmp_path):
    db=tmp_path/'auto.db'; conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='rela',duration_days=1,symbols=['BTCUSDT'],intervals=['1h']); run=start_or_resume_campaign(conn,camp.campaign_id)
    persist_pending_reject_label(conn,campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],reject_decision_id='auto',signal_id='s',symbol='BTCUSDT',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',entry=100,stop=90,target=120,horizon_seconds=120,execution_cost_assumptions=COSTS,regime='TRENDING',reject_reason='LOW_CONFIDENCE',source_provenance={'provider':'PAPER'})
    conn.commit(); conn.close(); e=_engine(db)
    async def go():
        runner=BurnInCampaignRunner(e,camp.campaign_id,lambda s,a,b:[{'timestamp':'2026-01-01T00:00:02Z','high':130,'low':99}],resolver_interval_seconds=0.01,qualification_interval_seconds=999999,maintenance_interval_seconds=999999,thresholds=BurnInThresholds(minimum_duration_seconds=0,minimum_total_decisions=0,minimum_accepted_trades=0,minimum_closed_trades=0,minimum_rejected_forward_outcomes=1,minimum_regime_sample=1,minimum_regime_coverage=1,minimum_calibration_sample=1,require_operator_ack=False,require_phase1_6_gates=False))
        runner._stop_event=asyncio.Event(); task=asyncio.create_task(runner._resolver_loop()); await asyncio.sleep(0.05); runner._stop_event.set(); task.cancel(); await asyncio.gather(task, return_exceptions=True)
    asyncio.run(go()); e.dispose()
    conn=sqlite3.connect(db); assert conn.execute("select count(*) from burnin_reject_outcomes where burnin_run_id not like '%__aggregate'").fetchone()[0] == 1; assert conn.execute('select count(*) from burnin_qualification_snapshots').fetchone()[0] >= 1; conn.close()


def test_maintenance_loop_updates_duration_and_completion(tmp_path):
    db=tmp_path/'maint.db'; conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='relm',duration_days=0,symbols=[],intervals=[],target_decisions=0,target_closed_trades=0,target_reject_forward_outcomes=0)
    start_or_resume_campaign(conn,camp.campaign_id); conn.execute("UPDATE burnin_campaigns SET evidence_completeness_status='PASS', latest_qualification_id='q-final' WHERE campaign_id=?",(camp.campaign_id,)); conn.commit(); conn.close(); e=_engine(db)
    async def go():
        runner=BurnInCampaignRunner(e,camp.campaign_id,lambda s,a,b:[],maintenance_interval_seconds=0.01,qualification_interval_seconds=999999)
        runner._stop_event=asyncio.Event(); await asyncio.wait_for(runner._maintenance_loop(), timeout=0.2)
    asyncio.run(go()); e.dispose()
    conn=sqlite3.connect(db); row=conn.execute('select campaign_status, observed_duration_seconds from burnin_campaigns where campaign_id=?',(camp.campaign_id,)).fetchone(); conn.close()
    assert row[0]=='COMPLETED' and row[1] is not None


def test_resolver_threshold_pauses_campaign(tmp_path):
    db=tmp_path/'fail.db'; conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='relf',duration_days=1,symbols=['BTCUSDT'],intervals=['1h']); run=start_or_resume_campaign(conn,camp.campaign_id)
    persist_pending_reject_label(conn,campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],reject_decision_id='fail',signal_id='s',symbol='BTCUSDT',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',entry=100,stop=90,target=120,horizon_seconds=1,execution_cost_assumptions=COSTS,regime='TRENDING',reject_reason='LOW_CONFIDENCE',source_provenance={'provider':'PAPER'}); conn.commit(); conn.close(); e=_engine(db)
    runner=BurnInCampaignRunner(e,camp.campaign_id,lambda s,a,b: (_ for _ in ()).throw(RuntimeError('candle failure')),resolver_failure_threshold=1)
    assert runner.resolver_tick()['status']=='PAUSED'; e.dispose()
    conn=sqlite3.connect(db); assert conn.execute('select campaign_status,last_error from burnin_campaigns').fetchone()==('PAUSED','RESOLVER_FAILURE_THRESHOLD'); conn.close()


def test_run_foreground_sets_same_database_and_restores_environment(tmp_path):
    db=tmp_path/'fg.db'; conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='relfg',duration_days=1,symbols=[],intervals=[]); conn.commit(); conn.close(); e=_engine(db)
    old=(os.environ.get('ALPHAFORGE_BURNIN_CAMPAIGN_ID'),os.environ.get('ALPHAFORGE_EXECUTION_MODE'),os.environ.get('EXECUTION_MODE'))
    fake=_FakeRuntime()
    async def go():
        runner=BurnInCampaignRunner(e,camp.campaign_id,lambda s,a,b:[],runtime_factory=lambda: fake,resolver_interval_seconds=999,maintenance_interval_seconds=999)
        try: await runner.run_foreground()
        except RuntimeError as exc: assert 'stop fake runtime' in str(exc)
    asyncio.run(go())
    assert fake.persistence_engine is e and fake.attached == camp.campaign_id and fake.shutdown_called
    assert (os.environ.get('ALPHAFORGE_BURNIN_CAMPAIGN_ID'),os.environ.get('ALPHAFORGE_EXECUTION_MODE'),os.environ.get('EXECUTION_MODE')) == old
    e.dispose()


def test_cli_worker_db_writes_to_exact_database(tmp_path):
    from alphaforge.burnin_cli import main
    db=tmp_path/'cli.db'; init_db(f"sqlite+pysqlite:///{db}").dispose(); conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='relcli',duration_days=1,symbols=[],intervals=[]); start_or_resume_campaign(conn,camp.campaign_id); conn.commit(); conn.close()
    assert main(['--db', str(db), 'worker', '--campaign-id', camp.campaign_id, '--once']) == 0
    conn=sqlite3.connect(db); assert conn.execute("select count(*) from burnin_campaign_events where campaign_id=? and event_type='RESOLVER_BATCH'",(camp.campaign_id,)).fetchone()[0] == 1; conn.close()

def test_cli_start_requires_worker_and_does_not_leave_running(tmp_path):
    from alphaforge.burnin_cli import main
    db=tmp_path/'start_required.db'; conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='relreq',duration_days=1,symbols=[],intervals=[]); conn.commit(); conn.close()
    assert main(['--db', str(db), 'start', '--campaign-id', camp.campaign_id]) == 3
    conn=sqlite3.connect(db); assert conn.execute('select campaign_status from burnin_campaigns where campaign_id=?',(camp.campaign_id,)).fetchone()[0] == 'CREATED'; assert conn.execute('select count(*) from burnin_campaign_runs').fetchone()[0] == 0; conn.close()


def test_cli_foreground_start_invokes_worker_runtime(monkeypatch, tmp_path):
    from alphaforge import burnin_cli
    db=tmp_path/'fg_cli.db'; init_db(f"sqlite+pysqlite:///{db}").dispose(); conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='relfgcli',duration_days=1,symbols=[],intervals=[]); conn.commit(); conn.close()
    called={}
    async def fake_run(self): called['cid']=self.campaign_id; return {'status':'STOPPED'}
    monkeypatch.setattr(burnin_cli.BurnInCampaignRunner, 'run_foreground', fake_run)
    assert burnin_cli.main(['--db', str(db), 'start', '--campaign-id', camp.campaign_id, '--foreground']) == 0
    assert called['cid'] == camp.campaign_id
    conn=sqlite3.connect(db); assert conn.execute('select count(*) from burnin_campaign_runs').fetchone()[0] == 1; conn.close()


def test_cli_detached_start_launches_live_subprocess_and_persists_pid(monkeypatch, tmp_path):
    from alphaforge import burnin_cli
    db=tmp_path/'detach_cli.db'; conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='reldet',duration_days=1,symbols=[],intervals=[]); conn.commit(); conn.close()
    launched={}
    class Proc:
        pid=43210
        def poll(self): return None
    def fake_popen(cmd, **kw): launched['cmd']=cmd; return Proc()
    monkeypatch.setattr(burnin_cli.subprocess, 'Popen', fake_popen)
    assert burnin_cli.main(['--db', str(db), 'start', '--campaign-id', camp.campaign_id, '--detach']) == 0
    assert str(db) in launched['cmd'] and 'worker' in launched['cmd']
    conn=sqlite3.connect(db); row=conn.execute('select worker_pid,campaign_status from burnin_campaigns where campaign_id=?',(camp.campaign_id,)).fetchone(); runs=conn.execute('select count(*) from burnin_campaign_runs').fetchone()[0]; conn.close()
    assert row == (43210,'RUNNING') and runs == 1


def test_worker_default_runtime_factory_starts_real_builder(monkeypatch, tmp_path):
    import alphaforge.runtime as runtime_mod
    db=tmp_path/'default_builder.db'; conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='relbuilder',duration_days=1,symbols=[],intervals=[]); conn.commit(); conn.close(); e=_engine(db)
    fake=_FakeRuntime(); called={}
    def builder(): called['built']=True; return fake
    monkeypatch.setattr(runtime_mod, '_build_runtime_from_env', builder)
    async def go():
        runner=BurnInCampaignRunner(e,camp.campaign_id,lambda s,a,b:[],resolver_interval_seconds=999,maintenance_interval_seconds=999)
        try: await runner.run_foreground()
        except RuntimeError: pass
    asyncio.run(go()); e.dispose()
    assert called['built'] is True and fake.shutdown_called


def test_resolver_and_maintenance_loops_run_concurrently_with_runtime(tmp_path):
    db=tmp_path/'concurrent.db'; conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='relconc',duration_days=1,symbols=['BTCUSDT'],intervals=['1h']); run=start_or_resume_campaign(conn,camp.campaign_id)
    persist_pending_reject_label(conn,campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],reject_decision_id='conc',signal_id='s',symbol='BTCUSDT',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',entry=100,stop=90,target=120,horizon_seconds=1,execution_cost_assumptions=COSTS,regime='TRENDING',reject_reason='LOW_CONFIDENCE',source_provenance={'provider':'PAPER'}); conn.commit(); conn.close(); e=_engine(db)
    class SlowRuntime(_FakeRuntime):
        async def start(self): await asyncio.sleep(0.05); raise RuntimeError('done')
    fake=SlowRuntime()
    async def go():
        runner=BurnInCampaignRunner(e,camp.campaign_id,lambda s,a,b:[{'timestamp':'2026-01-01T00:00:02Z','high':130,'low':99}],runtime_factory=lambda: fake,resolver_interval_seconds=0.01,maintenance_interval_seconds=0.01,qualification_interval_seconds=999999,thresholds=BurnInThresholds(minimum_duration_seconds=0,minimum_total_decisions=0,minimum_accepted_trades=0,minimum_closed_trades=0,minimum_rejected_forward_outcomes=1,minimum_regime_sample=1,minimum_regime_coverage=1,minimum_calibration_sample=1,require_operator_ack=False,require_phase1_6_gates=False))
        try: await runner.run_foreground()
        except RuntimeError: pass
    asyncio.run(go()); e.dispose()
    conn=sqlite3.connect(db); events=[r[0] for r in conn.execute('select event_type from burnin_campaign_events')]; conn.close()
    assert 'RESOLVER_BATCH' in events and 'CAMPAIGN_HEARTBEAT' in events

def test_cli_created_campaign_attaches_without_false_config_drift(tmp_path):
    from alphaforge.burnin_cli import main
    db=tmp_path/'cli_create_attach.db'
    assert main(['--db', str(db), '--json', 'create', '--release-id', 'default', '--duration-days', '1', '--symbols', 'BTCUSDT', '--intervals', '1m']) == 0
    rt, engine=_runtime_for_campaign(db)
    from alphaforge.config import load_config_from_env
    env_runtime=load_config_from_env().runtime
    rt.config.min_effective_rr=env_runtime.min_effective_rr; rt.config.max_spread_pct=env_runtime.max_spread_pct; rt.config.max_expected_slippage_pct=env_runtime.max_expected_slippage_pct
    with engine.begin() as conn:
        cid=conn.execute(text('select campaign_id from burnin_campaigns limit 1')).scalar_one()
        start_or_resume_campaign(conn,cid)
    rt._attach_phase8_campaign(cid)
    engine.dispose()


def test_canonical_identity_drift_reasons_for_filter_strategy_universe_and_cost(tmp_path):
    db=tmp_path/'identity_drift.db'; rt, engine=_runtime_for_campaign(db)
    cases=[('config_hash','PHASE8_CAMPAIGN_RUN_IDENTITY_MISMATCH'),('strategy_config_hash','PHASE8_CAMPAIGN_RUN_IDENTITY_MISMATCH'),('universe_hash','PHASE8_CAMPAIGN_RUN_IDENTITY_MISMATCH'),('execution_cost_config_hash','PHASE8_CAMPAIGN_EXECUTION_COST_DRIFT')]
    for col,reason in cases:
        cid,_=_campaign_matching_runtime(db,rt)
        with engine.begin() as conn:
            conn.execute(text('update burnin_campaigns set campaign_status=\'RUNNING\', last_error=NULL where campaign_id=:cid'), {'cid':cid})
            conn.execute(text(f'update burnin_campaigns set {col}=\'changed\' where campaign_id=:cid'), {'cid':cid})
        try: rt._attach_phase8_campaign(cid)
        except RuntimeError as exc: assert reason in str(exc)
        else: raise AssertionError('expected drift')
        # restore for next case
        h=rt._phase8_runtime_hashes(['BTCUSDT'] if False else [], [])
        with engine.begin() as conn:
            conn.execute(text('update burnin_campaigns set config_hash=:c,strategy_config_hash=:s,universe_hash=:u,execution_cost_config_hash=:e where campaign_id=:cid'), {'cid':cid,'c':h['config_hash'],'s':h['strategy_config_hash'],'u':h['universe_hash'],'e':h['execution_cost_config_hash']})
    engine.dispose()


def test_effective_paper_slippage_identity_attaches_and_drifts(tmp_path):
    db=tmp_path/'effective_slippage.db'; rt, engine=_runtime_for_campaign(db)
    rt.paper_slippage_bps = 7.5
    identity = build_phase8_campaign_identity(rt.config, ["BTCUSDT"], ["1m"], release_id=rt.config.phase7_burnin_release_id, paper_slippage_bps=rt.paper_slippage_bps)
    assert identity['execution_cost_payload']['paper_slippage_bps'] == 7.5
    assert identity['execution_cost_payload']['paper_expected_slippage_pct'] == 0.00075
    conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id=identity['release_id'],duration_days=1,symbols=["BTCUSDT"],intervals=["1m"],runtime_config=rt.config,paper_slippage_bps=rt.paper_slippage_bps)
    start_or_resume_campaign(conn,camp.campaign_id); conn.commit(); conn.close()
    rt._attach_phase8_campaign(camp.campaign_id)
    rt.paper_slippage_bps = 8.5
    try:
        rt._attach_phase8_campaign(camp.campaign_id)
    except RuntimeError as exc:
        assert 'PHASE8_CAMPAIGN_EXECUTION_COST_DRIFT' in str(exc)
    else:
        raise AssertionError('expected execution-cost drift')
    engine.dispose()


def test_provider_scope_participates_in_config_identity():
    rt = RuntimeConfig(execution_mode=ExecutionMode.PAPER)
    binance = build_phase8_campaign_identity(rt, ["BTCUSDT"], ["1m"],
        paper_source_exchanges=["binance"])
    hyperliquid = build_phase8_campaign_identity(rt, ["BTCUSDT"], ["1m"],
        paper_source_exchanges=["hyperliquid"])
    assert binance["config_payload"]["paper_source_exchanges"] == ["binance"]
    assert hyperliquid["config_payload"]["paper_source_exchanges"] == ["hyperliquid"]
    assert binance["config_hash"] != hyperliquid["config_hash"]


def test_campaign_creation_rejects_provider_identity_provenance_disagreement(tmp_path):
    conn = sqlite3.connect(tmp_path / "provider_creation.db")
    with pytest.raises(ValueError, match="PHASE8_CAMPAIGN_PROVIDER_IDENTITY_MISMATCH"):
        create_campaign(conn, release_id="rel-provider", duration_days=1,
            symbols=["BTCUSDT"], intervals=["1m"],
            source_provenance={"provider": "BINANCE_READ_ONLY_KLINES", "exchange": "BINANCE"},
            paper_source_exchanges=["hyperliquid"])
    conn.close()


def test_runtime_attachment_fails_closed_on_mutated_provider_provenance(tmp_path):
    db = tmp_path / "provider_drift.db"
    runtime, engine = _runtime_for_campaign(db)
    campaign_id, _ = _campaign_matching_runtime(db, runtime)
    with engine.begin() as conn:
        conn.execute(text("UPDATE burnin_campaigns SET source_provenance_json=:p WHERE campaign_id=:cid"),
                     {"cid": campaign_id, "p": json.dumps({"provider": "HYPERLIQUID", "exchange": "HYPERLIQUID"})})
    with pytest.raises(RuntimeError, match="PHASE8_CAMPAIGN_PROVIDER_DRIFT"):
        runtime._attach_phase8_campaign(campaign_id)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT campaign_status,last_error FROM burnin_campaigns WHERE campaign_id=:cid"),
                           {"cid": campaign_id}).one()
    assert row == ("PAUSED", "PHASE8_CAMPAIGN_PROVIDER_DRIFT")
    engine.dispose()


def test_hyperliquid_identity_cannot_attach_to_binance_paper_runtime(tmp_path):
    db = tmp_path / "provider_identity_drift.db"
    runtime, engine = _runtime_for_campaign(db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    campaign = create_campaign(conn, release_id=runtime.config.phase7_burnin_release_id,
        duration_days=1, symbols=["BTCUSDT"], intervals=["1m"],
        runtime_config=runtime.config,
        source_provenance={"provider": "HYPERLIQUID", "exchange": "HYPERLIQUID",
                           "order_submission": "DISABLED"},
        paper_source_exchanges=["hyperliquid"])
    start_or_resume_campaign(conn, campaign.campaign_id)
    conn.commit()
    conn.close()

    binance_identity = runtime._phase8_runtime_hashes(["BTCUSDT"], ["1m"])
    assert campaign.config_hash != binance_identity["config_hash"]
    with pytest.raises(RuntimeError, match="PHASE8_CAMPAIGN_PROVIDER_DRIFT"):
        runtime._attach_phase8_campaign(campaign.campaign_id)
    with engine.connect() as sql:
        row = sql.execute(text(
            "SELECT campaign_status,last_error FROM burnin_campaigns WHERE campaign_id=:cid"),
            {"cid": campaign.campaign_id}).one()
    assert row == ("PAUSED", "PHASE8_CAMPAIGN_PROVIDER_DRIFT")
    engine.dispose()


def test_execution_cost_hash_changes_with_effective_slippage(tmp_path):
    rt, engine=_runtime_for_campaign(tmp_path/'slip_hash.db')
    a=build_phase8_campaign_identity(rt.config, [], [], release_id='rel', paper_slippage_bps=2.0)
    b=build_phase8_campaign_identity(rt.config, [], [], release_id='rel', paper_slippage_bps=3.0)
    assert a['execution_cost_payload']['paper_slippage_bps'] == 2.0
    assert b['execution_cost_payload']['paper_slippage_bps'] == 3.0
    assert a['execution_cost_config_hash'] != b['execution_cost_config_hash']
    engine.dispose()


def test_maintenance_terminal_completion_skips_post_terminal_qualification(tmp_path, monkeypatch):
    db=tmp_path/'maintenance_terminal.db'; conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='rel-maint-terminal',duration_days=0,symbols=[],intervals=[],target_decisions=0,target_closed_trades=0,target_reject_forward_outcomes=0)
    start_or_resume_campaign(conn,camp.campaign_id)
    conn.execute("UPDATE burnin_campaigns SET evidence_completeness_status='PASS', latest_qualification_id='q-final' WHERE campaign_id=?",(camp.campaign_id,)); conn.commit(); conn.close(); engine=_engine(db)
    runner=BurnInCampaignRunner(engine,camp.campaign_id,lambda *_: [],maintenance_interval_seconds=0)
    monkeypatch.setattr(runner, "_qualify_if_due", lambda: (_ for _ in ()).throw(AssertionError("terminal campaign must not requalify")))
    runner._stop_event=asyncio.Event()
    asyncio.run(runner._maintenance_loop())
    with engine.connect() as sql:
        row=get_campaign(sql,camp.campaign_id)
    assert row["campaign_status"] == "COMPLETED"
    assert runner._stop_event.is_set()
    engine.dispose()


def test_maintenance_does_not_exit_while_campaign_active(tmp_path, monkeypatch):
    db=tmp_path/'maintenance_active.db'; conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='rel-maint-active',duration_days=1,symbols=[],intervals=[]); start_or_resume_campaign(conn,camp.campaign_id); conn.commit(); conn.close(); engine=_engine(db)
    runner=BurnInCampaignRunner(engine,camp.campaign_id,lambda *_: [],maintenance_interval_seconds=0)
    ticks=0
    def active_tick():
        nonlocal ticks
        ticks += 1
        return {"complete": False}, "RUNNING"
    monkeypatch.setattr(runner, "_maintenance_tick", active_tick)
    async def exercise():
        runner._stop_event=asyncio.Event(); task=asyncio.create_task(runner._maintenance_loop())
        while ticks < 3: await asyncio.sleep(0)
        assert not task.done()
        runner._stop_event.set(); await task
    asyncio.run(exercise())
    assert ticks >= 3
    engine.dispose()


def test_normal_resolver_exit_cannot_cancel_active_runtime_silently(tmp_path, monkeypatch):
    db=tmp_path/'resolver_exit.db'; conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='rel-resolver-exit',duration_days=1,symbols=[],intervals=[]); start_or_resume_campaign(conn,camp.campaign_id); conn.commit(); conn.close(); engine=_engine(db)
    class ActiveRuntime(_FakeRuntime):
        def __init__(self): super().__init__(); self.cancelled=False
        async def start(self):
            try: await asyncio.Event().wait()
            finally: self.cancelled=True
    runtime=ActiveRuntime(); runner=BurnInCampaignRunner(engine,camp.campaign_id,lambda *_: [],runtime_factory=lambda: runtime)
    async def resolver_returns(): return None
    monkeypatch.setattr(runner, "_resolver_loop", resolver_returns)
    with pytest.raises(RuntimeError, match="SUPERVISOR_EXITED_WHILE_CAMPAIGN_RUNNING:phase8_resolver_loop"):
        asyncio.run(runner.run_foreground())
    assert runtime.cancelled and runtime.shutdown_called
    with engine.connect() as sql: assert get_campaign(sql,camp.campaign_id)["campaign_status"] == "FAILED"
    engine.dispose()


def test_unexpected_normal_runtime_exit_fails_campaign_and_stops_siblings(tmp_path, monkeypatch):
    db=tmp_path/'runtime_normal_exit.db'; conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='rel-runtime-exit',duration_days=1,symbols=[],intervals=[]); start_or_resume_campaign(conn,camp.campaign_id); conn.commit(); conn.close(); engine=_engine(db)
    class NormalRuntime(_FakeRuntime):
        async def start(self): return None
    runtime=NormalRuntime(); runner=BurnInCampaignRunner(engine,camp.campaign_id,lambda *_: [],runtime_factory=lambda: runtime)
    sibling_stopped={"resolver":False,"maintenance":False}
    async def sibling(name):
        try: await asyncio.Event().wait()
        finally: sibling_stopped[name]=True
    monkeypatch.setattr(runner,"_resolver_loop",lambda: sibling("resolver")); monkeypatch.setattr(runner,"_maintenance_loop",lambda: sibling("maintenance"))
    with pytest.raises(RuntimeError, match="RUNTIME_EXITED_WHILE_CAMPAIGN_RUNNING"):
        asyncio.run(runner.run_foreground())
    assert sibling_stopped == {"resolver":True,"maintenance":True}
    assert runtime.shutdown_called
    with engine.connect() as sql: assert get_campaign(sql,camp.campaign_id)["campaign_status"] == "FAILED"
    engine.dispose()


def test_campaign_runtime_database_identity_mismatch_remains_fail_closed(tmp_path):
    campaign_db=tmp_path/'canonical.db'; wrong_db=tmp_path/'wrong.db'
    conn=sqlite3.connect(campaign_db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='rel-db-mismatch',duration_days=1,symbols=[],intervals=[]); start_or_resume_campaign(conn,camp.campaign_id); conn.commit(); conn.close()
    campaign_engine=_engine(campaign_db); wrong_engine=_engine(wrong_db); runtime=_FakeRuntime(); runtime.persistence_engine=wrong_engine
    runner=BurnInCampaignRunner(campaign_engine,camp.campaign_id,lambda *_: [],runtime_factory=lambda: runtime)
    with pytest.raises(RuntimeError, match="PERSISTENCE_DB_IDENTITY_MISMATCH"):
        asyncio.run(runner.run_foreground())
    with campaign_engine.connect() as sql:
        campaign=get_campaign(sql,camp.campaign_id)
        details=json.loads(sql.execute(text("SELECT details_json FROM burnin_campaign_events WHERE campaign_id=:cid AND event_type='PHASE8_CAMPAIGN_ATTACH_FAILED' ORDER BY id DESC LIMIT 1"),{"cid":camp.campaign_id}).scalar_one())
    assert campaign["campaign_status"] == "PAUSED"
    assert details["expected_canonical_path"] == str(campaign_db.resolve())
    assert details["observed_canonical_path"] == str(wrong_db.resolve())
    campaign_engine.dispose(); wrong_engine.dispose()


def test_paper_fee_assumption_changes_execution_cost_identity():
    from alphaforge.burnin_campaign import build_phase8_campaign_identity
    from alphaforge.runtime import RuntimeConfig

    first = build_phase8_campaign_identity(RuntimeConfig(paper_fee_bps=4.0), ["BTCUSDT"], ["1m"])
    second = build_phase8_campaign_identity(RuntimeConfig(paper_fee_bps=5.0), ["BTCUSDT"], ["1m"])
    assert first["execution_cost_payload"]["paper_fee_bps"] == 4.0
    assert first["execution_cost_config_hash"] != second["execution_cost_config_hash"]


def test_campaign_interval_and_reject_timeframe_semantics_are_explicit_and_hashed():
    from alphaforge.burnin_campaign import build_phase8_campaign_identity
    from alphaforge.runtime import RuntimeConfig

    one_hour_campaign = build_phase8_campaign_identity(
        RuntimeConfig(paper_decision_timeframe="1m", reject_forward_horizon_bars=240),
        ["BTCUSDT"], ["1h"],
    )
    payload = one_hour_campaign["config_payload"]
    assert payload["campaign_intervals"] == ["1h"]
    assert payload["decision_setup_timeframe"] == "1m"
    assert payload["reject_evaluation_timeframe"] == "1m"
    assert payload["reject_forward_horizon_bars"] == 240

    changed = build_phase8_campaign_identity(
        RuntimeConfig(paper_decision_timeframe="5m", reject_forward_horizon_bars=240),
        ["BTCUSDT"], ["1h"],
    )
    assert one_hour_campaign["config_hash"] != changed["config_hash"]
