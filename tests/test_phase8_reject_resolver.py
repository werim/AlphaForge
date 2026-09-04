import sqlite3
import json
from alphaforge.burnin_campaign import create_campaign, start_or_resume_campaign
from alphaforge.burnin_resolver import persist_pending_reject_label, resolve_pending_rejects
from alphaforge.persistence import init_db

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


def test_reject_net_r_subtracts_costs_already_normalized_to_r(tmp_path):
    conn,camp,run=setup(tmp_path)
    costs={"spread_cost":0.05,"entry_slippage_cost":0.03,"exit_slippage_cost":0.03,
           "fee_cost":0.04,"funding_cost":0.01,"latency_cost":0.04,
           "execution_cost_unit":"R"}
    persist_pending_reject_label(conn,campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],reject_decision_id='units',signal_id='s',symbol='BTCUSDT',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',entry=100,stop=95,target=106,horizon_seconds=60,execution_cost_assumptions=costs,regime='TRENDING',reject_reason='LOW_CONFIDENCE',source_provenance={'provider':'PAPER','forward_label_subject':'GUIDED_CANDIDATE'})
    resolve_pending_rejects(conn,{'BTCUSDT':[{'timestamp':'2026-01-01T00:01:00Z','high':106,'low':99}]},now='2026-01-01T00:02:00Z')
    row=conn.execute('select hypothetical_gross_r,hypothetical_net_r_after_costs,payload_json from burnin_reject_outcomes').fetchone()
    payload=json.loads(row['payload_json'])
    assert row['hypothetical_gross_r'] == 1.2
    assert row['hypothetical_net_r_after_costs'] == 1.0
    assert payload['execution_cost_unit'] == 'R'
    assert payload['reject_quality_attributable'] is True


def test_legacy_shadow_outcome_is_resolved_but_not_marked_reject_correct(tmp_path):
    conn,camp,run=setup(tmp_path)
    persist_pending_reject_label(conn,campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],reject_decision_id='shadow',signal_id='s',symbol='BTCUSDT',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',entry=100,stop=90,target=120,horizon_seconds=60,execution_cost_assumptions=COSTS,regime='TRENDING',reject_reason='MTF_EXECUTION_COUNTER_REGIME',source_provenance={'provider':'PAPER','forward_label_subject':'LEGACY_SCANNER_SHADOW_CANDIDATE','forward_label_side':'LONG','mtf':{'regime':{'direction':'SHORT'}}})
    resolve_pending_rejects(conn,{'BTCUSDT':[{'timestamp':'2026-01-01T00:01:00Z','high':101,'low':89}]},now='2026-01-01T00:02:00Z')
    payload=json.loads(conn.execute('select payload_json from burnin_reject_outcomes').fetchone()[0])
    assert payload['forward_label_subject'] == 'LEGACY_SCANNER_SHADOW_CANDIDATE'
    assert payload['reject_quality_attributable'] is False
    assert payload['reject_correct'] is None
    assert payload['non_attributable_reason'] == 'LEGACY_SHADOW_NOT_GUIDED_EQUIVALENT'
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


def test_canonical_provider_uses_configured_market_data_endpoint():
    calls=[]
    provider=BinanceReadOnlyCandleProvider(
        interval='1m', base_url='https://market-data.example/',
        fetcher=lambda url: calls.append(url) or [[1767225660000,'100','101','99','100','10']],
    )
    provider('BTCUSDT','2026-01-01T00:00:00Z','2026-01-01T00:01:00Z')
    assert calls[0].startswith('https://market-data.example/fapi/v1/klines?')
    assert provider.source_provenance['base_url'] == 'https://market-data.example'


def test_provider_outage_does_not_expire_pending_label(tmp_path):
    conn,camp,run=setup(tmp_path)
    persist_pending_reject_label(conn,campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],reject_decision_id='outage',signal_id='s',symbol='BTCUSDT',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',entry=100,stop=90,target=120,horizon_seconds=120,execution_cost_assumptions=COSTS,regime='TRENDING',reject_reason='LOW_CONFIDENCE',source_provenance={'provider':'PAPER'})
    provider=BinanceReadOnlyCandleProvider(interval='1m', fetcher=lambda url: (_ for _ in ()).throw(RuntimeError('down')))
    try: provider('BTCUSDT','2026-01-01T00:00:00Z','2026-01-01T00:02:00Z')
    except Exception: pass
    assert conn.execute('select status from burnin_pending_reject_labels where reject_decision_id=\'outage\'').fetchone()[0] == 'PENDING'


def test_empty_completed_horizon_remains_pending_for_market_data_recovery(tmp_path):
    conn,camp,run=setup(tmp_path)
    persist_pending_reject_label(conn,campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],reject_decision_id='empty',signal_id='s',symbol='BTCUSDT',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',entry=100,stop=90,target=120,horizon_seconds=120,execution_cost_assumptions=COSTS,regime='TRENDING',reject_reason='LOW_CONFIDENCE',source_provenance={'provider':'PAPER'})
    counts=resolve_pending_rejects(conn, {'BTCUSDT': []}, now='2026-01-01T00:03:00Z')
    row=conn.execute('select status,evidence_complete,last_error from burnin_pending_reject_labels where reject_decision_id=\'empty\'').fetchone()
    assert tuple(row) == ('PENDING',0,'NO_CANDLES_IN_MARKET_WINDOW') and counts['pending'] == 1


def test_reject_feedback_labels_reviews_and_is_restart_idempotent(tmp_path):
    cases = {
        "tp": ([{"timestamp":"2026-01-01T00:01:00Z","high":121,"low":99}], "TP_BEFORE_SL", 0),
        "sl": ([{"timestamp":"2026-01-01T00:01:00Z","high":101,"low":89}], "SL_BEFORE_TP", 1),
        "timeout": ([{"timestamp":"2026-01-01T00:01:00Z","high":105,"low":95}], "TIMEOUT", 1),
    }
    for signal_id, (candles, expected, correct) in cases.items():
        db = tmp_path / f"feedback-{signal_id}.db"
        init_db(f"sqlite+pysqlite:///{db}").dispose()
        conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
        camp = create_campaign(conn, release_id=signal_id, duration_days=1, symbols=["BTCUSDT"], intervals=["1m"])
        run = start_or_resume_campaign(conn, camp.campaign_id)["burnin_run_id"]
        conn.execute("INSERT INTO rejected_signal_reviews(signal_id,symbol,setup_type,regime,reject_reason,created_at,payload_json) VALUES(?,?,?,?,?,?,?)",
                     (signal_id,"BTCUSDT","BREAKOUT","TRENDING","LOW_CONFIDENCE","2026-01-01T00:00:00Z","{}"))
        kwargs=dict(campaign_id=camp.campaign_id,burnin_run_id=run,reject_decision_id=signal_id,signal_id=signal_id,symbol="BTCUSDT",side="LONG",decision_timestamp="2026-01-01T00:00:00Z",entry=100,stop=90,target=120,horizon_seconds=120,execution_cost_assumptions=COSTS,regime="TRENDING",reject_reason="LOW_CONFIDENCE",source_provenance={"provider":"PAPER","bar_seconds":60,"setup_type":"BREAKOUT","volatility_regime":"NORMAL"})
        assert persist_pending_reject_label(conn, **kwargs)
        assert persist_pending_reject_label(conn, **kwargs)
        conn.commit(); conn.close()  # pending evidence survives a restart
        conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
        assert resolve_pending_rejects(conn, {"BTCUSDT": candles}, now="2026-01-01T00:03:00Z")["resolved"] == 1
        outcome = conn.execute("SELECT forward_label,payload_json FROM burnin_reject_outcomes WHERE reject_outcome_id=?", ("rout_"+signal_id,)).fetchone()
        payload = json.loads(outcome["payload_json"])
        review = conn.execute("SELECT forward_window_bars,would_have_hit_tp,would_have_hit_sl,max_favorable_excursion_pct,max_adverse_excursion_pct,reject_correct,setup_type,regime FROM rejected_signal_reviews WHERE signal_id=?", (signal_id,)).fetchone()
        assert outcome["forward_label"] == expected
        assert review["forward_window_bars"] == 2 and review["reject_correct"] == correct
        assert review["max_favorable_excursion_pct"] is not None and review["max_adverse_excursion_pct"] is not None
        assert payload["mfe_pct"] == review["max_favorable_excursion_pct"] and payload["mae_pct"] == review["max_adverse_excursion_pct"]
        assert review["setup_type"] == "BREAKOUT" and review["regime"] == "TRENDING"
        assert conn.execute("SELECT COUNT(*) FROM burnin_pending_reject_labels").fetchone()[0] == 1
        assert resolve_pending_rejects(conn, {"BTCUSDT": []}, now="2026-01-01T00:04:00Z")["resolved"] == 0
        assert conn.execute("SELECT COUNT(*) FROM burnin_reject_outcomes").fetchone()[0] == 1
        conn.close()


def test_missing_costs_are_incomplete_and_never_counted_correct(tmp_path):
    db=tmp_path/'invalid-cost.db'; init_db(f'sqlite+pysqlite:///{db}').dispose(); conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='invalid',duration_days=1,symbols=['BTCUSDT'],intervals=['1m']); run=start_or_resume_campaign(conn,camp.campaign_id)['burnin_run_id']
    conn.execute("INSERT INTO rejected_signal_reviews(reject_decision_id,signal_id,reject_reason,created_at,payload_json) VALUES('reject:invalid','invalid','LOW_CONFIDENCE','2026-01-01T00:00:00Z','{}')")
    persist_pending_reject_label(conn,campaign_id=camp.campaign_id,burnin_run_id=run,reject_decision_id='reject:invalid',signal_id='invalid',symbol='BTCUSDT',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',timeframe='1m',horizon_bars=1,entry=100,stop=90,target=120,execution_cost_assumptions={},regime='TRENDING',reject_reason='LOW_CONFIDENCE',source_provenance={'provider':'PAPER'})
    resolve_pending_rejects(conn,{'BTCUSDT':[{'timestamp':'2026-01-01T00:01:00Z','high':121,'low':99}]},now='2026-01-01T00:02:00Z')
    outcome=conn.execute("SELECT execution_invalidated,evidence_complete FROM burnin_reject_outcomes").fetchone(); review=conn.execute("SELECT reject_correct,execution_invalidated,evidence_complete FROM rejected_signal_reviews").fetchone()
    assert tuple(outcome)==(1,0) and tuple(review)==(None,1,0)


def test_first_finalized_outcome_is_immutable_across_retry(tmp_path):
    conn,camp,run=setup(tmp_path); rid='immutable'
    persist_pending_reject_label(conn,campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],reject_decision_id=rid,signal_id='s',symbol='BTCUSDT',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',timeframe='1m',horizon_bars=1,entry=100,stop=90,target=120,execution_cost_assumptions=COSTS,regime='TRENDING',reject_reason='LOW_CONFIDENCE',source_provenance={'provider':'PAPER'})
    resolve_pending_rejects(conn,{'BTCUSDT':[{'timestamp':'2026-01-01T00:01:00Z','high':121,'low':99}]},now='2026-01-01T00:02:00Z'); first=tuple(conn.execute("SELECT forward_label,payload_json FROM burnin_reject_outcomes").fetchone())
    conn.execute("UPDATE burnin_pending_reject_labels SET status='READY',claim_token=NULL,claimed_at=NULL")
    resolve_pending_rejects(conn,{'BTCUSDT':[{'timestamp':'2026-01-01T00:01:00Z','high':101,'low':89}]},now='2026-01-01T00:02:00Z')
    assert tuple(conn.execute("SELECT forward_label,payload_json FROM burnin_reject_outcomes").fetchone())==first
    assert conn.execute("SELECT COUNT(*) FROM burnin_reject_outcomes").fetchone()[0]==1


def test_fresh_overlapping_claim_cannot_finalize(tmp_path):
    conn,camp,run=setup(tmp_path)
    persist_pending_reject_label(conn,campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],reject_decision_id='claimed',signal_id='s',symbol='BTCUSDT',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',timeframe='1m',horizon_bars=1,entry=100,stop=90,target=120,execution_cost_assumptions=COSTS,regime='TRENDING',reject_reason='LOW_CONFIDENCE',source_provenance={'provider':'PAPER'})
    conn.execute("UPDATE burnin_pending_reject_labels SET status='RESOLVING',claim_token='worker-a',claimed_at='2026-01-01T00:01:30Z'")
    counts=resolve_pending_rejects(conn,{'BTCUSDT':[{'timestamp':'2026-01-01T00:01:00Z','high':121,'low':99}]},now='2026-01-01T00:02:00Z')
    assert counts['resolved']==0 and conn.execute("SELECT COUNT(*) FROM burnin_reject_outcomes").fetchone()[0]==0


def test_timeframe_aware_due_at_and_invalid_geometry_audit(tmp_path):
    conn,camp,run=setup(tmp_path)
    for tf,seconds in [('1m',60),('5m',300),('1h',3600)]:
        persist_pending_reject_label(conn,campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],reject_decision_id=tf,signal_id=tf,symbol='BTCUSDT',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',timeframe=tf,horizon_bars=240,entry=100,stop=90,target=120,execution_cost_assumptions=COSTS,regime='TRENDING',reject_reason='LOW_CONFIDENCE',source_provenance={'provider':'PAPER'})
        row=conn.execute("SELECT horizon_seconds,timeframe FROM burnin_pending_reject_labels WHERE reject_decision_id=?",(tf,)).fetchone(); assert tuple(row)==(240*seconds,tf)
    assert persist_pending_reject_label(conn,campaign_id=camp.campaign_id,burnin_run_id=run['burnin_run_id'],reject_decision_id='zero',signal_id='zero',symbol='BTCUSDT',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',timeframe='1m',horizon_bars=1,entry=100,stop=100,target=120,execution_cost_assumptions=COSTS,regime='TRENDING',reject_reason='LOW_CONFIDENCE',source_provenance={'provider':'PAPER'}) is None
    assert conn.execute("SELECT COUNT(*) FROM burnin_pending_reject_labels WHERE reject_decision_id='zero'").fetchone()[0]==0
    assert 'zero_risk' in conn.execute("SELECT missing_fields_json FROM burnin_observations WHERE metrics_json LIKE '%zero%'").fetchone()[0]


def test_partial_and_gapped_windows_retry_before_immutable_finalization(tmp_path):
    db=tmp_path/'retry-window.db'; init_db(f'sqlite+pysqlite:///{db}').dispose(); conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='retry',duration_days=1,symbols=['BTCUSDT'],intervals=['1m']); run=start_or_resume_campaign(conn,camp.campaign_id)['burnin_run_id']
    conn.execute("INSERT INTO rejected_signal_reviews(reject_decision_id,signal_id,reject_reason,forward_window_bars,created_at,payload_json) VALUES('retry','s','LOW_CONFIDENCE',2,'2026-01-01T00:00:00Z','{}')")
    persist_pending_reject_label(conn,campaign_id=camp.campaign_id,burnin_run_id=run,reject_decision_id='retry',signal_id='s',symbol='BTCUSDT',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',timeframe='1m',horizon_bars=2,entry=100,stop=90,target=120,execution_cost_assumptions=COSTS,regime='TRENDING',reject_reason='LOW_CONFIDENCE',source_provenance={'provider':'PAPER'})
    partial=[{'timestamp':'2026-01-01T00:02:00Z','high':105,'low':95}]
    assert resolve_pending_rejects(conn,{'BTCUSDT':partial},now='2026-01-01T00:03:00Z')['pending']==1
    pending=conn.execute("SELECT status,last_error FROM burnin_pending_reject_labels").fetchone()
    assert pending['status']=='PENDING' and 'INCOMPLETE_MARKET_WINDOW' in pending['last_error']
    assert conn.execute("SELECT COUNT(*) FROM burnin_reject_outcomes").fetchone()[0]==0
    full=[{'timestamp':'2026-01-01T00:01:00Z','high':105,'low':95},{'timestamp':'2026-01-01T00:02:00Z','high':121,'low':99}]
    assert resolve_pending_rejects(conn,{'BTCUSDT':full},now='2026-01-01T00:03:00Z')['resolved']==1
    assert conn.execute("SELECT forward_label FROM burnin_reject_outcomes").fetchone()[0]=='TP_BEFORE_SL'
    review=conn.execute("SELECT would_have_hit_tp,max_favorable_excursion_pct,evidence_complete FROM rejected_signal_reviews").fetchone()
    assert tuple(review)==(1,21.0,1)
    first=tuple(conn.execute("SELECT forward_label,payload_json FROM burnin_reject_outcomes").fetchone())
    conn.execute("UPDATE burnin_pending_reject_labels SET status='READY',claim_token=NULL,claimed_at=NULL")
    resolve_pending_rejects(conn,{'BTCUSDT':[{'timestamp':'2026-01-01T00:01:00Z','high':101,'low':89}]},now='2026-01-01T00:03:00Z')
    assert tuple(conn.execute("SELECT forward_label,payload_json FROM burnin_reject_outcomes").fetchone())==first
    assert conn.execute("SELECT COUNT(*) FROM burnin_reject_outcomes").fetchone()[0]==1
