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
