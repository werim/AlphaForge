from __future__ import annotations

import json, os, sqlite3
from pathlib import Path

from alphaforge.burnin import persist_burnin_observation, persist_burnin_trade_outcome
from alphaforge.burnin_campaign import create_campaign, start_or_resume_campaign
from alphaforge.burnin_ops import audit_payload, bootstrap_ops_schema, finalize, health_payload, preflight, watch_once


def _conn(tmp_path: Path):
    db=tmp_path/'ops.db'; conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row; bootstrap_ops_schema(conn); return db, conn


def test_phase9_preflight_rejects_non_paper(monkeypatch, tmp_path):
    monkeypatch.setenv('ALPHAFORGE_EXECUTION_MODE','LIVE')
    out=preflight(str(tmp_path/'pf.db'),'rel',['BTCUSDT'],['1h'],require_market_data=False)
    assert out['status']=='FAIL_CLOSED'
    assert 'execution_mode_paper' in out['blockers']


def test_phase9_health_detects_running_without_worker_and_sql_counters(monkeypatch, tmp_path):
    monkeypatch.setenv('ALPHAFORGE_EXECUTION_MODE','PAPER')
    db, conn=_conn(tmp_path)
    camp=create_campaign(conn,release_id='rel',duration_days=1,symbols=['BTCUSDT'],intervals=['1h'])
    run=start_or_resume_campaign(conn,camp.campaign_id)['burnin_run_id']
    persist_burnin_observation(conn, observation_id='o1', burnin_run_id=run, release_id='rel', observed_at='2026-01-01T00:00:00Z', execution_mode='PAPER', symbol='BTCUSDT', regime='TREND', decision='ACCEPTED', lifecycle_state='FILLED', metrics={}, source_provenance={'t':'x'})
    conn.commit()
    h=health_payload(conn,camp.campaign_id,max_heartbeat_age=999999)
    assert h['total_decisions']==1 and h['accepted_decisions']==1
    assert 'RUNNING_WITHOUT_LIVE_WORKER' in h['unhealthy_reasons']
    w=watch_once(conn,camp.campaign_id)
    assert w['status']=='RECOVERY_REQUIRED'


def test_phase9_audit_detects_incomplete_outcome_and_finalize_never_live(monkeypatch, tmp_path):
    monkeypatch.setenv('ALPHAFORGE_EXECUTION_MODE','PAPER')
    db, conn=_conn(tmp_path)
    camp=create_campaign(conn,release_id='rel',duration_days=1,symbols=['BTCUSDT'],intervals=['1h'])
    run=start_or_resume_campaign(conn,camp.campaign_id)['burnin_run_id']
    persist_burnin_trade_outcome(conn,outcome_id='t1',burnin_run_id=run,release_id='rel',trade_id='tr1',symbol='BTCUSDT',regime='TREND',closed_at='2026-01-01T01:00:00Z',gross_r=1,gross_pnl=1,costs={'spread_cost':None},net_r=None,net_pnl=None,hold_duration_seconds=3600,mfe=1,mae=0,exit_reason='TP',payload={})
    conn.commit()
    a=audit_payload(conn,camp.campaign_id)
    assert a['status']=='FAIL'
    assert 'no_incomplete_outcomes_counted_complete' in a['violations'] or 'no_missing_cost_fields_in_qualified_outcomes' in a['violations']
    f=finalize(conn,str(db),camp.campaign_id,tmp_path/'final')
    assert f['decision']!='LIVE_READY'
    assert json.loads((tmp_path/'final'/'release_decision.json').read_text())['decision'] in {'PAPER_BURNIN_FAILED','PAPER_BURNIN_INCOMPLETE'}
