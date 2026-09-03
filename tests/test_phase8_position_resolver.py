import json, sqlite3
from alphaforge.burnin_campaign import create_campaign, start_or_resume_campaign
from alphaforge.burnin_resolver import persist_pending_position, resolve_campaign_positions, resolve_position_closure

def setup(tmp_path):
    conn=sqlite3.connect(tmp_path/'a.db'); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='rel1',duration_days=7,symbols=['BTCUSDT'],intervals=['1h'])
    run=start_or_resume_campaign(conn,camp.campaign_id); conn.commit()
    return conn,camp,run

def test_position_entry_then_idempotent_closure(tmp_path):
    conn,camp,run=setup(tmp_path)
    persist_pending_position(conn,trade_id='t1',campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],signal_id='s1',symbol='BTCUSDT',side='LONG',entry_time='2026-01-01T00:00:00Z',planned_entry=100,simulated_fill=101,stop=90,target=120,quantity=1,notional=101,entry_spread=0.01,entry_slippage=0.02,entry_fee=0.01,regime='TRENDING',source_provenance={'provider':'PAPER'})
    assert conn.execute('select count(*) from burnin_trade_outcomes').fetchone()[0] == 0
    res=resolve_position_closure(conn,trade_id='t1',exit_time='2026-01-01T01:00:00Z',exit_price=120,exit_reason='TP_HIT',exit_costs={'exit_spread':0.01,'exit_slippage':0.02,'exit_fee':0.01,'funding':0.0,'latency_impact_penalty':0.0})
    assert res['evidence_complete'] is True
    assert conn.execute('select count(*) from burnin_trade_outcomes').fetchone()[0] == 1
    assert resolve_position_closure(conn,trade_id='t1',exit_time='2026-01-01T01:00:00Z',exit_price=120,exit_reason='TP_HIT',exit_costs={})['status'] == 'IDEMPOTENT'

def test_missing_exit_costs_incomplete(tmp_path):
    conn,camp,run=setup(tmp_path)
    persist_pending_position(conn,trade_id='t2',campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],signal_id='s1',symbol='BTCUSDT',side='LONG',entry_time='2026-01-01T00:00:00Z',planned_entry=100,simulated_fill=100,stop=90,target=120,quantity=1,notional=100,entry_spread=0.01,entry_slippage=0.02,entry_fee=0.01,regime='TRENDING',source_provenance={'provider':'PAPER'})
    res=resolve_position_closure(conn,trade_id='t2',exit_time='2026-01-01T01:00:00Z',exit_price=90,exit_reason='SL_HIT',exit_costs={})
    assert res['evidence_complete'] is False


def test_campaign_position_resolver_persists_phase_conditioned_tp(tmp_path):
    conn,camp,run=setup(tmp_path)
    provenance={
        'provider':'PAPER', 'execution_cost_unit':'R', 'setup_phase':'PULLBACK',
        'execution_direction':'LONG', 'effective_rr_at_entry':1.8,
        'execution_cost_model':{'spread_penalty':.02,'slippage_penalty':.03,'fee_penalty':.01,'funding_penalty':0.0,'latency_penalty':.01,'volatility_penalty':.0,'liquidity_penalty':.0},
        'mtf':{'regime':{'regime':'LONG'},'setup':{'phase':'PULLBACK'},'execution':{'direction':'LONG'}},
    }
    persist_pending_position(conn,trade_id='guided-long',campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],signal_id='s-guided',symbol='BTCUSDT',side='LONG',entry_time='2026-01-01T00:00:00Z',planned_entry=100,simulated_fill=100,stop=90,target=120,quantity=1,notional=100,entry_spread=.01,entry_slippage=.015,entry_fee=.005,regime='LONG',source_provenance=provenance)
    counts=resolve_campaign_positions(conn,camp.campaign_id,{('BTCUSDT','position','guided-long'):[{'timestamp':'2026-01-01T00:01:00Z','high':121,'low':99}]},now='2026-01-01T00:02:00Z')
    outcome=dict(conn.execute("select * from burnin_trade_outcomes where trade_id='guided-long'").fetchone())
    payload=json.loads(outcome['payload_json'])
    assert counts == {'closed':1,'tp':1,'sl':0,'ambiguous':0,'pending':0}
    assert outcome['exit_reason']=='TP_HIT' and outcome['evidence_complete']==1
    assert payload['phase']=='PULLBACK' and payload['execution']=='LONG'
    assert outcome['net_r'] < outcome['gross_r']


def test_campaign_position_resolver_marks_same_candle_tp_sl_ambiguous(tmp_path):
    conn,camp,run=setup(tmp_path)
    model={'spread_penalty':0.0,'slippage_penalty':0.0,'fee_penalty':0.0,'funding_penalty':0.0,'latency_penalty':0.0,'volatility_penalty':0.0,'liquidity_penalty':0.0}
    persist_pending_position(conn,trade_id='ambiguous',campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],signal_id='s2',symbol='BTCUSDT',side='LONG',entry_time='2026-01-01T00:00:00Z',planned_entry=100,simulated_fill=100,stop=90,target=120,quantity=1,notional=100,entry_spread=0,entry_slippage=0,entry_fee=0,regime='LONG',source_provenance={'execution_cost_unit':'R','execution_cost_model':model})
    counts=resolve_campaign_positions(conn,camp.campaign_id,{'ambiguous':[{'timestamp':'2026-01-01T00:01:00Z','high':121,'low':89}]},now='2026-01-01T00:02:00Z')
    outcome=conn.execute("select evidence_complete,exit_reason from burnin_trade_outcomes where trade_id='ambiguous'").fetchone()
    assert counts['ambiguous']==1 and outcome['evidence_complete']==0
    assert outcome['exit_reason']=='AMBIGUOUS_INTRABAR'
