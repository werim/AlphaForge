"""Synthetic transport; real producer/enricher/runtime adapter/SQLite readback."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from alphaforge import exchange_market_scanner as scanner
from alphaforge.breakout_provenance import associate_breakout_receipt, bind_breakout_provenance
from alphaforge.persistence import init_db
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator

START = 1788196800000
OBSERVED = START+120010
DECISION = '2026-08-31T17:22:00.020000Z'


def source(monkeypatch, *, short=False, close_times=True, crossed=True):
    values = [[100,102,99,101],[101,105,100,104],[104,106,103,105]]
    if short:
        values = [[104,105,101,103],[103,104,98,99],[99,100,97,98]]
    if not crossed:
        values[0][1]=110
    rows = [[START+i*60000,*v,10,START+(i+1)*60000-1] for i,v in enumerate(values)]
    if not close_times:
        rows = [r[:5] for r in rows]
    monkeypatch.setattr(scanner,'_fetch_json',lambda *a,**k:deepcopy(rows))
    monkeypatch.setattr(scanner.time,'time_ns',lambda:OBSERVED*1000000)
    return rows


def candidate(monkeypatch, **kwargs):
    rows=source(monkeypatch,**kwargs)
    cfg=SimpleNamespace(binance=SimpleNamespace(market_data_base_url='https://synthetic.invalid'),runtime=SimpleNamespace(execution_mode='BACKTEST'))
    enriched=asyncio.run(scanner.enrich_selected_market_geometry([
        dict(symbol='BTCUSDT',source_exchange='binance',timeframe='1m')],cfg))[0]
    enriched.update(signal_id=RuntimeOrchestrator._resolve_signal_id('BTCUSDT',enriched),
                    setup_identity='SYNTHETIC-setup',decision='ACCEPTED',decision_time=DECISION,execution_ctx={})
    enriched['breakout_input_receipt']=associate_breakout_receipt(enriched['breakout_input_receipt'],enriched['signal_id'])
    return rows,enriched


def persist(tmp_path,payload):
    engine=init_db('sqlite+pysqlite:///'+str(tmp_path/'isolated.db'))
    rt=RuntimeOrchestrator(config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),ai_brain=object(),
                           market_scanner=lambda:None,persistence_engine=engine)
    rt._burnin_run_id='SYNTHETIC-receipt-test'
    rt._persist_burnin_decision(payload,lifecycle_state='SIGNAL_REJECTED' if payload['decision']=='REJECTED' else 'ORDER_PLACED')
    assert not rt._burnin_evidence_incomplete
    with engine.connect() as c:
        metrics=json.loads(c.execute(text('select metrics_json from burnin_observations')).scalar_one())
        ev=dict(c.execute(text('select * from decision_evidence')).mappings().one())
    engine.dispose()
    envelope=metrics['breakout_provenance']
    assert envelope==json.loads(ev['diagnostics_json'])['breakout_provenance']
    assert envelope['research_eligibility']=='NOT_EVALUABLE'
    assert envelope['probability_status']=='UNAVAILABLE'
    return envelope,ev


@pytest.mark.parametrize('short',[False,True])
@pytest.mark.parametrize('decision',['ACCEPTED','REJECTED'])
def test_actual_producer_adapter_SQLite_roundtrip(tmp_path,monkeypatch,short,decision):
    rows,payload=candidate(monkeypatch,short=short)
    payload.update(decision=decision,primary_reject_reason='LOW_SCORE',reject_decision_id='SYNTHETIC-reject')
    original=deepcopy(payload)
    bound,ev=persist(tmp_path,payload)
    assert payload==original
    assert bound['status']=='BOUND_LOCAL_OBSERVATION'
    assert bound['candidate_id']==payload['signal_id']
    assert bound['subject']=='NATIVE_BREAKOUT_CANDIDATE'
    receipt=bound['receipt']
    assert receipt==payload['breakout_input_receipt']
    assert receipt['raw_candles']==rows[:2]
    assert receipt['breakout_level']==float(rows[0][3 if short else 2])
    assert receipt['local_read_observed_ms']==OBSERVED
    assert receipt['remote_retrieval_observed_ms'] is None
    assert receipt['provenance_status']=='LOCAL_OBSERVATION_NOT_PUBLISHER_AUTHENTICATED'
    assert ev['side']==payload['side'] and ev['setup_type']==payload['setup_type']
    for k in ('entry','sl','tp'):assert ev[k]==payload[k]
    assert ev['decision']==('ACCEPT' if decision=='ACCEPTED' else 'REJECT')


@pytest.mark.parametrize('change,reason',[
    ('symbol','SOURCE_SCOPE_MISMATCH'),('side','CANDIDATE_GEOMETRY_MISMATCH'),
    ('entry','CANDIDATE_GEOMETRY_MISMATCH'),('decision_time','INPUT_UNAVAILABLE_AT_DECISION'),
    ('signal_id','CANDIDATE_ID_UNAVAILABLE'),('receipt','SOURCE_RECEIPT_INTEGRITY_MISMATCH'),
    ('different_id','SOURCE_CANDIDATE_ID_MISMATCH')])
def test_wrong_candidate_or_late_or_edited_receipt_cannot_bind(tmp_path,monkeypatch,change,reason):
    _,p=candidate(monkeypatch)
    if change=='symbol':p['symbol']='ETHUSDT'
    if change=='side':p['side']='SHORT'
    if change=='entry':p['entry']+=1
    if change=='decision_time':p['decision_time']='2026-08-31T17:22:00Z'
    if change=='signal_id':p['signal_id']=None
    if change=='different_id':p['signal_id']='ANOTHER-CANDIDATE'
    if change=='receipt':p['breakout_input_receipt']['raw_candles'][0][2]=103
    # Missing identity deliberately uses standalone binding: the real writer
    # rightly requires canonical decision identity, not a fake test fallback.
    bound=bind_breakout_provenance(p) if change=='signal_id' else persist(tmp_path,p)[0]
    assert bound['status']=='UNAVAILABLE' and bound['reason']==reason
    assert bound['research_eligibility']=='NOT_EVALUABLE'


@pytest.mark.parametrize('legacy',[False,True])
def test_missing_receipt_or_original_close_time_stays_unavailable(tmp_path,monkeypatch,legacy):
    _,p=candidate(monkeypatch,close_times=False)
    if legacy:p.pop('breakout_input_receipt')
    bound,_=persist(tmp_path,p)
    assert bound['status']=='UNAVAILABLE' and bound['reason']=='SOURCE_RECEIPT_UNAVAILABLE'
    assert bound['receipt'] is None if legacy else bound['receipt']['status']=='UNAVAILABLE'
    if legacy:assert bound['subject']=='LEGACY_CANDIDATE_WITHOUT_RECEIPT'


def test_guided_shadow_retains_own_geometry_and_receipt(tmp_path,monkeypatch):
    _,p=candidate(monkeypatch)
    legacy=deepcopy(p)
    p.update(side='SHORT',setup_type='SHORT_CONTINUATION',entry=104,sl=106,tp=100,
        mtf={'generation':{'mode':'REGIME_GUIDED'},'shadow_evidence':{'candidate':legacy}})
    bound,ev=persist(tmp_path,p)
    assert bound['status']=='BOUND_LOCAL_OBSERVATION'
    assert bound['subject']=='LEGACY_SCANNER_SHADOW_CANDIDATE'
    assert bound['receipt']['geometry']['side']=='LONG'
    assert ev['side']=='SHORT' and ev['setup_type']=='SHORT_CONTINUATION'


def test_guided_missing_shadow_cannot_inherit_legacy_receipt(tmp_path,monkeypatch):
    _,p=candidate(monkeypatch)
    p['mtf']={'generation':{'mode':'REGIME_GUIDED','candidate':{'setup_type':'LONG_CONTINUATION'}}}
    bound,_=persist(tmp_path,p)
    assert bound['status']=='UNAVAILABLE' and bound['reason']=='CANDIDATE_GEOMETRY_MISMATCH'


def test_receipt_addition_does_not_change_persisted_economics(tmp_path,monkeypatch):
    _,p=candidate(monkeypatch)
    p.update(score=0.73,rr=1.9,effective_rr=1.5)
    a=tmp_path/'a';b=tmp_path/'b';a.mkdir();b.mkdir()
    _,with_receipt=persist(a,p)
    legacy=deepcopy(p);legacy.pop('breakout_input_receipt')
    _,without=persist(b,legacy)
    ignored={'id','created_at','diagnostics_json'}
    assert {k:v for k,v in with_receipt.items() if k not in ignored}=={k:v for k,v in without.items() if k not in ignored}
    wa=json.loads(with_receipt['diagnostics_json']);wb=json.loads(without['diagnostics_json'])
    wa.pop('breakout_provenance');wb.pop('breakout_provenance')
    assert wa==wb


def test_real_runtime_shadow_projection_before_SQLite(tmp_path,monkeypatch):
    _,p=candidate(monkeypatch)
    p.update(spread_pct=.02,market_data_latency_ms=20.,liquidity_score=.9,
             volume_24h_usdt=100_000_000,market_ts=scanner.time.time())
    class GuidedProvider:
        async def build(self,*a,**k):
            return {'provider':'BINANCE_FUTURES_CLOSED_KLINES',
                'regime':{'regime':'TRENDING','direction':'SHORT'},
                'setup':{'phase':'PULLBACK','trade_side':'SHORT','direction':'LONG'},
                'execution':{'direction':'SHORT','confirmed_for_side':True},
                'alignment':{'aligned':True,'direction':'SHORT','reasons':[],
                    'generation_mode':'REGIME_GUIDED','setup_phase':'PULLBACK',
                    'timeframes':{'regime':'1h','setup':'15m','execution':'1m'}},
                'generation':{'mode':'REGIME_GUIDED','evidence_status':'COMPLETE',
                    'candidate':{'side':'SHORT','entry':100.,'sl':101.,'tp':98.,'rr':2.,
                        'setup_type':'SHORT_PULLBACK','setup_phase':'PULLBACK',
                        'geometry_status':'COMPLETE','geometry_reason':None,
                        'geometry_source':'MTF_REGIME_GUIDED_CLOSED_KLINES'}}}
    received=[]
    rt=RuntimeOrchestrator(RuntimeConfig(execution_mode=ExecutionMode.PAPER,
        require_mtf_alignment=True,max_spread_pct=.001),SimpleNamespace(),lambda:None,
        mtf_context_provider=GuidedProvider(),on_reject_persist=lambda row:received.append(row))
    selection=SimpleNamespace(symbol='BTCUSDT',regime_hint='TREND',diagnostics={'inputs':p})
    asyncio.run(rt._process_symbol(selection))
    assert received[-1]['reason']=='SPREAD_TOO_HIGH'
    assert received[-1]['side']=='SHORT'
    assert received[-1]['mtf']['shadow_evidence']['candidate']['breakout_input_receipt']==p['breakout_input_receipt']
    bound,ev=persist(tmp_path,received[-1])
    assert bound['status']=='BOUND_LOCAL_OBSERVATION'
    assert bound['subject']=='LEGACY_SCANNER_SHADOW_CANDIDATE'
    assert bound['receipt']==p['breakout_input_receipt']
    assert ev['decision']=='REJECT'


def test_cached_source_not_mutated_or_reassigned(monkeypatch):
    _,p=candidate(monkeypatch)
    receipt=p['breakout_input_receipt'];before=deepcopy(receipt)
    conflicting=associate_breakout_receipt(receipt,'ANOTHER-CANDIDATE')
    assert receipt==before and conflicting['candidate_id']==p['signal_id']
    p.update(signal_id='ANOTHER-CANDIDATE',breakout_input_receipt=conflicting)
    assert bind_breakout_provenance(p)['reason']=='SOURCE_CANDIDATE_ID_MISMATCH'


def test_setup_name_without_crossing_never_binds_for_labels(tmp_path,monkeypatch):
    _,p=candidate(monkeypatch,crossed=False)
    assert p['setup_type']=='BREAKOUT_UP'
    before=(p['side'],p['entry'],p['sl'],p['tp'],p['rr'])
    bound,ev=persist(tmp_path,p)
    assert bound['status']=='UNAVAILABLE' and bound['reason']=='NO_ACTUAL_BREAKOUT_CROSSING'
    assert (p['side'],p['entry'],p['sl'],p['tp'],p['rr'])==before
    assert ev['decision']=='ACCEPT'  # provenance remains observational only
