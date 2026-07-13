import sqlite3
from alphaforge.burnin_campaign import create_campaign, start_or_resume_campaign
from alphaforge.burnin_resolver import persist_pending_position, resolve_position_closure

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
