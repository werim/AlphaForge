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
from alphaforge.burnin_campaign import BinanceReadOnlyCandleProvider, MarketDataUnavailable


def test_canonical_provider_resolves_tp_and_persists_provenance(tmp_path):
    conn,camp,run=setup(tmp_path)
    persist_pending_reject_label(conn,campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],reject_decision_id='prov',signal_id='s',symbol='BTCUSDT',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',entry=100,stop=90,target=120,horizon_seconds=120,execution_cost_assumptions=COSTS,regime='TRENDING',reject_reason='LOW_CONFIDENCE',source_provenance={'provider':'PAPER'})
    def fetcher(url): return [[1767225660000,'100','130','99','125','10'],[1767225720000,'125','126','121','122','10']]
    provider=BinanceReadOnlyCandleProvider(interval='1m', fetcher=fetcher)
    counts=resolve_pending_rejects(conn, {'BTCUSDT': provider('BTCUSDT','2026-01-01T00:00:00Z','2026-01-01T00:02:00Z')}, now='2026-01-01T00:03:00Z')
    assert counts['resolved'] == 1
    row=conn.execute('select forward_label,payload_json from burnin_reject_outcomes where reject_outcome_id=\'rout_prov\'').fetchone()
    assert row[0] == 'TP_BEFORE_SL' and 'BINANCE_READ_ONLY_KLINES' in row[1]


def test_provider_outage_does_not_expire_pending_label(tmp_path):
    conn,camp,run=setup(tmp_path)
    persist_pending_reject_label(conn,campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],reject_decision_id='outage',signal_id='s',symbol='BTCUSDT',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',entry=100,stop=90,target=120,horizon_seconds=120,execution_cost_assumptions=COSTS,regime='TRENDING',reject_reason='LOW_CONFIDENCE',source_provenance={'provider':'PAPER'})
    provider=BinanceReadOnlyCandleProvider(interval='1m', fetcher=lambda url: (_ for _ in ()).throw(RuntimeError('down')))
    try: provider('BTCUSDT','2026-01-01T00:00:00Z','2026-01-01T00:02:00Z')
    except Exception: pass
    assert conn.execute('select status from burnin_pending_reject_labels where reject_decision_id=\'outage\'').fetchone()[0] == 'PENDING'


def test_empty_completed_horizon_becomes_expired_not_completed(tmp_path):
    conn,camp,run=setup(tmp_path)
    persist_pending_reject_label(conn,campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],reject_decision_id='empty',signal_id='s',symbol='BTCUSDT',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',entry=100,stop=90,target=120,horizon_seconds=120,execution_cost_assumptions=COSTS,regime='TRENDING',reject_reason='LOW_CONFIDENCE',source_provenance={'provider':'PAPER'})
    counts=resolve_pending_rejects(conn, {'BTCUSDT': []}, now='2026-01-01T00:03:00Z')
    row=conn.execute('select status,evidence_complete,last_error from burnin_pending_reject_labels where reject_decision_id=\'empty\'').fetchone()
    assert tuple(row) == ('EXPIRED',0,'NO_CANDLES_IN_MARKET_WINDOW') and counts['failed'] == 1
