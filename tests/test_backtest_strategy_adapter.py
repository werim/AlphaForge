"""Native causality without replacing protective exits by a synthetic timeout."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import json

import pytest

from alphaforge.backtest_strategy_adapter import (build_strategy_input,evaluate_prefix,
    representation_reasons,validate_strategy_evidence,_profile,STEP)
from alphaforge.backtest_chronology import _simulation_module,run_boundary_backtest
from alphaforge.portfolio_risk import BacktestPortfolioState
from alphaforge.backtest_source_evidence import digest,validate_source_packet

START = 1704153600000


def source():
    rows = [dict(timestamp=t,open=100.,high=101.,low=99.,close=100.2,volume=1000.)
            for t in range(START-86400000,START+4*STEP,STEP)]
    return rows,{b['timestamp']:100000. for b in rows},dict(provider='SYNTHETIC_UNIT_FIXTURE',sha256='a'*64)


def data():
    rows,quotes,provenance=source()
    return build_strategy_input(rows,quotes,provenance,start_ms=START,end_ms=START+4*STEP,run_id='causal-test')


def test_native_decisions_replay_and_source_remains_incomplete_without_fabricated_trades(tmp_path):
    d=data()
    validate_strategy_evidence(d,recompute=True)
    p=run_boundary_backtest(d,tmp_path/'run')
    assert p['status']=='INCOMPLETE'
    assert p['completion']['closure_population']==[]
    assert 'NO_SUPPORTED_CAUSAL_STRATEGY_EXECUTION' in p['completion']['gaps']
    assert all(s['marked_equity_usdt'] is None for s in p['completion']['valuations'])
    decisions=[e['payload'] for e in p['events'] if e['kind']=='STRATEGY_DECISION']
    assert len(decisions)==4
    for r in decisions:
        assert r['signal_ms']+STEP==r['decision_ms']
        assert r['earliest_execution_ms']==r['decision_ms']+STEP
        assert r['execution_ms'] is None and r['mapped_intent_id'] is None
    validate_source_packet(p,expected_identity_hash=p['identity_hash'],expected_completion_hash=p['completion_hash'])
    assert p['completion']['capital_return_pct'] is None and p['completion']['max_drawdown_pct'] is None


def test_future_rows_never_reach_native_selector_or_scan(monkeypatch):
    rows,quotes,_=source()
    sim=_simulation_module()
    calls=[]
    def selector(symbol,market,context):
        return SimpleNamespace(tradable=True)
    def scan(symbol,candles,idx,ctx):
        calls.append(([b.timestamp for b in candles],ctx['mtf_source_candles']))
        ctx['last_result']=dict(accepted=False,reject_reason='TEST_NATIVE_REJECTION')
    monkeypatch.setattr(sim,'select_symbol',selector)
    monkeypatch.setattr(sim,'scan_symbol_backtest',scan)
    profile=_profile(sim)
    state=BacktestPortfolioState(initial_equity=1000.)
    first=evaluate_prefix(sim,rows,quotes,START,state,profile,'lookahead')
    changed=deepcopy(rows)
    for b in changed:
        if b['timestamp']>=START: b.update(close=9999.,high=10000.)
    second=evaluate_prefix(sim,changed,quotes,START,state,profile,'lookahead')
    assert {k:v for k,v in first.items() if k!='observed_ms'}=={k:v for k,v in second.items() if k!='observed_ms'}
    assert calls and all(max(labels)+STEP<=START for labels,_ in calls)
    assert all(max(b.timestamp for b in mtf)+STEP<=START for _,mtf in calls)


def test_overlap_risk_and_current_cash_are_passed_to_canonical_authority(monkeypatch):
    rows,quotes,_=source()
    sim=_simulation_module()
    monkeypatch.setattr(sim,'select_symbol',lambda *a:SimpleNamespace(tradable=True))
    state=BacktestPortfolioState(initial_equity=1000.)
    state.open_position(position_id='existing',symbol='BTCUSDT',side='LONG',notional=300.,entry_price=100.,timestamp=START-STEP)
    state.current_equity=970.
    def scan(symbol,candles,idx,ctx):
        assert ctx['portfolio_state'] is state
        assert ctx['balance']==970.
        assert set(ctx['portfolio_state'].open_positions)=={'existing'}
        ctx['last_result']=dict(accepted=False,reject_reason='EXISTING_RISK_GATE')
    monkeypatch.setattr(sim,'scan_symbol_backtest',scan)
    r=evaluate_prefix(sim,rows,quotes,START,state,_profile(sim),'risk')
    assert r['portfolio']['cash']==970.
    assert set(r['portfolio']['open_positions'])=={'existing'}
    assert r['native_reason']=='EXISTING_RISK_GATE'


@pytest.mark.parametrize('kind',['MARKET','LIMIT','STOP','BREAKOUT'])
def test_native_protective_exits_are_not_coerced_to_fixed_horizon(kind):
    sim=_simulation_module()
    candidate=sim.CandidateOrder(START,'BTCUSDT','LONG',100.,99.,103.,3.,'BREAKOUT','SOURCE','TREND',.8,kind)
    reasons=representation_reasons(candidate)
    assert 'UNSUPPORTED_NATIVE_PROTECTIVE_EXITS' in reasons
    assert 'MISSING_NATIVE_SCHEDULED_EXIT_CONTRACT' in reasons
    assert ('UNSUPPORTED_NATIVE_ORDER_TYPE' in reasons)==(kind!='MARKET')


@pytest.mark.parametrize('defect',['future_signal','early_execution','duplicate','missing_record','admitted_intent','foreign_window','invalid_timestamp','missing_warmup'])
def test_bad_lineage_cannot_reach_executor(defect,tmp_path):
    d=data()
    e=d['strategy_evidence'];r=e['records'][0]
    if defect=='future_signal': r['signal_ms']=START
    if defect=='early_execution': r['earliest_execution_ms']=START-STEP
    if defect=='duplicate': e['records'].insert(1,deepcopy(r))
    if defect=='missing_record': e['records'].pop()
    if defect=='admitted_intent': d['intents']=[{}]
    if defect=='foreign_window': e['feature_bars'].append(deepcopy(d['candles']['BTCUSDT'][0]))
    if defect=='invalid_timestamp': r['decision_ms']=str(START)
    if defect=='missing_warmup': e['feature_bars'].pop()
    with pytest.raises((ValueError,TypeError)):
        run_boundary_backtest(d,tmp_path/'bad')
    assert not (tmp_path/'bad/backtest.sqlite').exists()


@pytest.mark.parametrize('defect',['missing','duplicate','invalid_time'])
def test_source_population_errors_refused_before_native_execution(defect):
    rows,quotes,provenance=source()
    if defect=='missing': rows.pop(20)
    if defect=='duplicate': rows.insert(20,deepcopy(rows[20]))
    if defect=='invalid_time': rows[20]['timestamp']=True
    with pytest.raises(ValueError):
        build_strategy_input(rows,quotes,provenance,start_ms=START,end_ms=START+4*STEP,run_id='bad')


def test_native_accept_still_rejected_at_unsupported_exit_and_end_boundary(monkeypatch):
    rows,quotes,_=source()
    sim=_simulation_module()
    monkeypatch.setattr(sim,'select_symbol',lambda *a:SimpleNamespace(tradable=True))
    candidate=sim.CandidateOrder(START,'BTCUSDT','LONG',100.,99.,103.,3.,'BREAKOUT','SOURCE','TREND',.8,'MARKET')
    def scan(symbol,candles,idx,ctx):
        ctx['last_result']=dict(accepted=True,candidate=candidate,diagnostics=dict(risk_scale=.25))
    monkeypatch.setattr(sim,'scan_symbol_backtest',scan)
    r=evaluate_prefix(sim,rows,quotes,START,BacktestPortfolioState(initial_equity=1000.),_profile(sim),'last',end_ms=START+STEP)
    assert r['native_status']=='ACCEPT'
    assert r['execution_ms'] is None
    assert 'EARLIEST_EXECUTION_OUTSIDE_WINDOW' in r['adapter_reasons']
    assert 'UNSUPPORTED_NATIVE_PROTECTIVE_EXITS' in r['adapter_reasons']
    assert r['proposed_notional_usdt']==pytest.approx(2.5)


def test_native_risk_clock_is_candle_release_while_signal_label_stays_unchanged(monkeypatch):
    sim=_simulation_module()
    Context,Mode,cycle=sim._order_runtime()
    captured=[]
    def context(**kw):
        captured.append(kw['timestamp'])
        return Context(**kw)
    monkeypatch.setattr(sim,'_order_runtime',lambda:(context,Mode,cycle))
    candles=[sim.Candle(START+i*STEP,100.,101.,99.,100.2,1000.) for i in range(3)]
    scan=dict(balance=1000.,risk_pct=1.,simulation_timeframe='1m',causal_decision_ms=START+3*STEP)
    sim.scan_symbol_backtest('BTCUSDT',candles,2,scan)
    assert captured==[START+3*STEP]
    assert candles[2].timestamp==START+2*STEP
    with pytest.raises(ValueError,match='release boundary'):
        sim.scan_symbol_backtest('BTCUSDT',candles,2,{**scan,'causal_decision_ms':START+2*STEP})
    with pytest.raises(ValueError,match='release boundary'):
        sim.scan_symbol_backtest('BTCUSDT',candles,2,{**scan,'causal_decision_ms':True})


def _archive_fixture(tmp_path):
    import csv,hashlib,io,zipfile
    rows=[]
    for at in range(START-86400000,START,STEP):
        rows.append([str(at),'100','101','99','100.2','1000',str(at+STEP-1),'100000','1','0','0','0'])
    text=io.StringIO();csv.writer(text).writerows(rows)
    name='BTCUSDT-1m-2024-01-01.zip'
    with zipfile.ZipFile(tmp_path/name,'w') as z: z.writestr(name[:-4]+'.csv',text.getvalue())
    sha=hashlib.sha256((tmp_path/name).read_bytes()).hexdigest()
    url='https://data.binance.vision/data/spot/daily/klines/BTCUSDT/1m/'+name
    receipt=dict(filename=name,sha256=sha,url=url,checksum_url=url+'.CHECKSUM')
    (tmp_path/(name+'.CHECKSUM')).write_text(sha+'  '+name)
    (tmp_path/'receipts.json').write_text(json.dumps([receipt]))
    return name


def test_archive_checksum_scope_and_coverage_are_required(tmp_path):
    from alphaforge.backtest_strategy_adapter import read_historical_archives
    name=_archive_fixture(tmp_path)
    bars,quotes,provenance=read_historical_archives(tmp_path)
    assert len(bars)==len(quotes)==1440
    assert provenance['coverage_end_ms']==START
    assert provenance['remote_retrieval_observed_ms'] is None
    with (tmp_path/name).open('ab') as f: f.write(b'tamper')
    with pytest.raises(ValueError,match='checksum'):
        read_historical_archives(tmp_path)


def test_source_decision_event_cannot_be_omitted_or_substituted(tmp_path):
    from alphaforge.backtest_chronology import replay_boundary_evidence
    from alphaforge.backtest_source_evidence import SourceIdentity
    p=run_boundary_backtest(data(),tmp_path/'source')
    events=deepcopy(p['events'])
    missing=[e for e in events if e['kind']!='STRATEGY_DECISION']
    with pytest.raises(ValueError,match='causal sequence'):
        replay_boundary_evidence(SourceIdentity(**p['identity']),p['initialization'],missing)
    record=next(e for e in events if e['kind']=='STRATEGY_DECISION')
    record['payload']['native_status']='ACCEPT'
    with pytest.raises(ValueError,match='strategy source decision'):
        replay_boundary_evidence(SourceIdentity(**p['identity']),p['initialization'],events)
