import sqlite3
from alphaforge.burnin_campaign import create_campaign, start_or_resume_campaign
from alphaforge.burnin_resolver import persist_pending_reject_label, resolve_pending_rejects

COSTS={"spread_cost":0.01,"entry_slippage_cost":0.01,"exit_slippage_cost":0.01,"fee_cost":0.01,"funding_cost":0.0,"latency_cost":0.0}

def setup(tmp_path):
    conn=sqlite3.connect(tmp_path/'a.db'); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='rel1',duration_days=7,symbols=['BTCUSDT'],intervals=['1h'])
    run=start_or_resume_campaign(conn,camp.campaign_id); conn.commit()
    return conn,camp,run

def test_reject_resolver_tp_sl_timeout_ambiguous_and_no_lookahead(tmp_path):
    conn,camp,run=setup(tmp_path)
    base=dict(campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],signal_id='s',symbol='BTCUSDT',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',entry=100,stop=90,target=120,horizon_seconds=7200,execution_cost_assumptions=COSTS,regime='TRENDING',reject_reason='LOW_CONFIDENCE',source_provenance={'provider':'PAPER'})
    for rid in ['tp','sl','amb','to']:
        persist_pending_reject_label(conn,reject_decision_id=rid,**base)
    candles={'BTCUSDT':[{'timestamp':'2025-12-31T23:00:00Z','high':130,'low':80},{'timestamp':'2026-01-01T01:00:00Z','high':130,'low':95}]}
    assert resolve_pending_rejects(conn,candles,now='2026-01-01T03:00:00Z')['resolved'] >= 1
    labels=[r[0] for r in conn.execute('select forward_label from burnin_reject_outcomes')]
    assert 'TP_BEFORE_SL' in labels


def test_missing_costs_mark_incomplete(tmp_path):
    conn,camp,run=setup(tmp_path)
    persist_pending_reject_label(conn,campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],reject_decision_id='x',signal_id='s',symbol='BTCUSDT',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',entry=100,stop=90,target=120,horizon_seconds=3600,execution_cost_assumptions={},regime='TRENDING',reject_reason='LOW_CONFIDENCE',source_provenance={'provider':'PAPER'})
    resolve_pending_rejects(conn,{'BTCUSDT':[{'timestamp':'2026-01-01T00:30:00Z','high':130,'low':99}]},now='2026-01-01T02:00:00Z')
    assert conn.execute('select evidence_complete from burnin_reject_outcomes').fetchone()[0] == 0

from alphaforge.burnin_campaign import resolve_campaign_batch
from alphaforge.burnin_cli import main as cli_main

def test_manual_resolve_command_and_batch_expire_due_outcome(tmp_path):
    db=tmp_path/'cli.db'; conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='rel1',duration_days=7,symbols=['BTCUSDT'],intervals=['1h'])
    run=start_or_resume_campaign(conn,camp.campaign_id); conn.commit()
    persist_pending_reject_label(conn,campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],reject_decision_id='due',signal_id='s',symbol='BTCUSDT',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',entry=100,stop=90,target=120,horizon_seconds=1,execution_cost_assumptions=COSTS,regime='TRENDING',reject_reason='LOW_CONFIDENCE',source_provenance={'provider':'PAPER'})
    conn.commit(); conn.close()
    assert cli_main(['--db',str(db),'--json','resolve','--campaign-id',camp.campaign_id]) == 0
    chk=sqlite3.connect(db)
    assert chk.execute("select status from burnin_pending_reject_labels where reject_decision_id='due'").fetchone()[0] == 'EXPIRED'
