"""Synthetic fixtures exercise real authorities, not historical evidence claims."""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from alphaforge.backtest_source_evidence import digest
from alphaforge.backtest_strategy_adapter import STEP,build_strategy_input
from alphaforge.historical_execution_scenario import ExecutionScenario,VERSION as MODEL_VERSION,MODELED_HISTORICAL_V1,evaluate_scenario
import alphaforge.historical_downstream_scenario as downstream

START=1704153600000


def make_native(side='LONG'):
    # SHORT step/terminal price lies between the momentum and overextension
    # boundaries; exercise supported geometry without bypassing either gate.
    bars=[]
    for i,t in enumerate(range(START-86400000,START+4*STEP,STEP)):
        c=100+i*.1 if side=='LONG' else 1000-i*.25
        bars.append(dict(timestamp=t,open=c-.005,high=c+.02,low=c-.02,close=c,volume=1000.))
    return build_strategy_input(bars,{b['timestamp']:100000. for b in bars},
        dict(provider='BINANCE_PUBLIC_SPOT_ARCHIVE',symbol='BTCUSDT',interval_ms=STEP,fixture='SYNTHETIC'),
        start_ms=START,end_ms=START+4*STEP,run_id='downstream-'+side)


@pytest.fixture(scope='module')
def native(): return make_native()


def scenario(data,**overrides):
    v=dict(schema=MODEL_VERSION,name='explicit-unit-scenario',source_provider='BINANCE_PUBLIC_SPOT_ARCHIVE',
        venue='BINANCE',product='SPOT',instrument='BTCUSDT',input_hash=digest(data),
        native_config_hash=digest(data['strategy_evidence']['profile']),start_ms=data['start_ms'],end_ms=data['end_ms'],
        assumptions_effective_from_ms=data['start_ms'],market_data_latency_ms=200,order_latency_ms=50,
        spread_pct=.0008,expected_slippage_pct=.0001,liquidity_score=1.,fee_pct=.001)
    v.update(overrides);return ExecutionScenario.from_dict(v)


def evaluated(data,s=None):
    return downstream.evaluate_downstream(data,mode=MODELED_HISTORICAL_V1,scenario=s or scenario(data))


def first(result): return next(r for r in result['downstream'] if r['gates'])


def test_default_off_native_and_mtf_records_unchanged(native):
    original=deepcopy(native)
    off=downstream.evaluate_downstream(native)
    assert off['downstream']==[]
    with pytest.raises(ValueError): downstream.evaluate_downstream(native,scenario=scenario(native))
    with pytest.raises(ValueError): downstream.evaluate_downstream(native,mode=MODELED_HISTORICAL_V1)
    r=evaluated(native)
    assert r['mtf_evaluation']==evaluate_scenario(native,mode=MODELED_HISTORICAL_V1,scenario=scenario(native))
    assert native==original
    assert len(r['downstream'])==len(r['mtf_evaluation']['modeled'])==4
    assert first(r)['status']=='NOT_EVALUABLE'
    for a,b in zip(r['downstream'],r['mtf_evaluation']['modeled']):
        if b['status']!='MTF_ALIGNED':
            assert a['status']==b['status'] and a['reasons']==b['reasons'] and a['gates']=={}


def test_real_authorities_in_order_only_for_aligned_source_and_no_order_cycle(native,monkeypatch):
    calls=[];snapshots=[]
    sim=downstream._simulation_module()
    monkeypatch.setattr(downstream,'_simulation_module',lambda:sim)
    def spy(obj,name,label):
        fn=getattr(obj,name)
        def wrapper(*args,**kwargs):
            calls.append(label)
            if label=='RISK': snapshots.append(args[1])
            return fn(*args,**kwargs)
        monkeypatch.setattr(obj,name,wrapper)
    spy(sim,'_historical_authoritative_score','SCORE')
    spy(downstream,'evaluate_signal_decision','PRE_SUBMIT')
    spy(downstream,'evaluate_portfolio_risk','RISK')
    spy(downstream,'scale_candidate_exposure','SIZE')
    def forbidden(*a,**kw): raise AssertionError('scenario attempted execution')
    monkeypatch.setattr(sim,'simulate_candidate',forbidden)
    runtime=sim._order_runtime()
    monkeypatch.setattr(sim,'_order_runtime',lambda:(runtime[0],runtime[1],forbidden))
    r=evaluated(native)
    aligned=sum(m['status']=='MTF_ALIGNED' for m in r['mtf_evaluation']['modeled'])
    assert aligned>0 and calls==['SCORE','PRE_SUBMIT','RISK','SIZE']*aligned
    assert all(s.equity is None and s.open_position_count is None and s.risk_state_complete is False for s in snapshots)


def test_missing_fields_do_not_become_optimistic_scores_costs_or_flat_sizing(native):
    r=evaluated(native);a=first(r);g=a['gates']
    assert a['gate_order']==downstream.GATE_ORDER
    assert all(x['status']=='NOT_EVALUABLE' for x in g.values())
    assert {'funding_rate','funding_rate_pct','fakeout_risk'}<=set(g['SCORING']['reasons'])
    assert g['SCORING']['score'] is g['SCORING']['expectancy_after_costs'] is None
    cost=g['SHARED_QUALITY_AND_EXECUTION_COST']
    assert cost['effective_rr'] is cost['total_penalty'] is cost['remaining_execution_penalty'] is None
    assert cost['estimated_components']['funding_penalty'] is None
    assert g['PORTFOLIO_RISK']['canonical_reject_reason']=='UNKNOWN_PORTFOLIO_RISK'
    assert g['PORTFOLIO_RISK']['open_position_count'] is None
    assert g['SIZING']['quantity'] is g['SIZING']['effective_notional'] is None
    assert g['SIZING']['canonical_projection']['original_notional'] is None
    assert native['initial_capital_usdt']>0  # must never be used as current balance
    assert a['measured_market_data_latency_ms'] is a['measured_order_latency_ms'] is None
    assert a['executed_intent'] is None and a['native_authorization']=='DISABLED'
    assert 'UNSUPPORTED_NATIVE_PROTECTIVE_EXITS' in a['unsupported_exit_reasons']
    assert r['consumer_admission']==r['order_admission']=='DISABLED'
    assert r['capital_return_pct'] is r['max_drawdown_pct'] is None


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_cost_estimates_use_canonical_components_no_fee_booking_or_slippage_duplication(side):
    from alphaforge.execution import adverse_expected_fill_price,build_execution_cost_model
    data=make_native(side);s=scenario(data);r=evaluated(data,s);a=first(r)
    mtf=next(m for m in r['mtf_evaluation']['modeled'] if m['status']=='MTF_ALIGNED')
    geom=mtf['mtf']['generation']['candidate'];c=a['gates']['SHARED_QUALITY_AND_EXECUTION_COST']
    assert c['expected_fill_estimate']==round(adverse_expected_fill_price(entry=geom['entry'],side=geom['side'],slippage_pct=s.expected_slippage_pct),8)
    model=build_execution_cost_model(dict(fee_pct=s.fee_pct,expected_slippage_pct=s.expected_slippage_pct))
    assert c['estimated_components']['fee_penalty']==model.fee_penalty
    assert c['estimated_components']['slippage_penalty']==model.slippage_penalty
    assert c['actual_fill'] is c['booked_fee'] is None
    higher=first(evaluated(data,scenario(data,fee_pct=.002)))['gates']['SHARED_QUALITY_AND_EXECUTION_COST']
    assert higher['estimated_components']['fee_penalty']==pytest.approx(2*c['estimated_components']['fee_penalty'])
    assert higher['expected_fill_estimate']==c['expected_fill_estimate']
    assert higher['effective_rr'] is None


def test_later_prices_never_change_earlier_downstream_gates(native):
    bars=deepcopy(native['strategy_evidence']['feature_bars']+native['candles']['BTCUSDT'])
    bars[-1].update(open=500.,close=500.,low=499.,high=501.)
    changed=build_strategy_input(bars,dict(native['strategy_evidence']['quote_volumes']),native['strategy_evidence']['provenance'],
        start_ms=START,end_ms=START+4*STEP,run_id=native['run_id'])
    assert first(evaluated(changed))['gates']==first(evaluated(native))['gates']


@pytest.mark.parametrize('kind',['future','missing','portfolio','config'])
def test_bad_source_or_counterfactual_flat_state_is_rejected(native,kind):
    d=deepcopy(native)
    if kind=='future': d['strategy_evidence']['records'][0]['source_available_ms']+=STEP
    if kind=='missing': d['candles']['BTCUSDT'].pop()
    if kind=='portfolio': d['strategy_evidence']['records'][0]['portfolio']['cash']=1e12
    if kind=='config': d['strategy_evidence']['profile']['risk_pct']=99.
    with pytest.raises(ValueError): evaluated(d)


def test_replay_and_rehashed_promotion_rejected(native,tmp_path):
    s=scenario(native);p=downstream.persist_downstream(tmp_path/'model.json',native,mode=MODELED_HISTORICAL_V1,scenario=s)
    loaded=json.loads((tmp_path/'model.json').read_text())
    assert downstream.replay_downstream(loaded,native,expected_input_hash=digest(native),scenario=s)==p['result']
    changed=deepcopy(p['result']);first(changed)['status']='SCENARIO_DOWNSTREAM_ALIGNED'
    with pytest.raises(ValueError): downstream.replay_downstream(dict(result=changed,result_hash=digest(changed)),native,expected_input_hash=digest(native),scenario=s)
    with pytest.raises(ValueError): downstream.replay_downstream(p,native,expected_input_hash='0'*64,scenario=s)
    with pytest.raises(FileExistsError): downstream.persist_downstream(tmp_path/'model.json',native,mode=MODELED_HISTORICAL_V1,scenario=s)


def test_outcomes_require_every_gate_and_follow_canonical_order():
    g={k:dict(status='PASS') for k in downstream.GATE_ORDER}
    assert downstream._outcome(g)=='SCENARIO_DOWNSTREAM_ALIGNED'
    g['SCORING']['status']='REJECT'
    assert downstream._outcome(g)=='DOWNSTREAM_REJECT'
    g['SCORING']['status']='NOT_EVALUABLE'
    assert downstream._outcome(g)=='NOT_EVALUABLE'
    with pytest.raises(ValueError): downstream._outcome({})


def test_diagnostic_risk_fail_open_never_supplies_missing_state(native,monkeypatch):
    from alphaforge.portfolio_risk import PortfolioRiskDecision
    monkeypatch.setattr(downstream,'evaluate_portfolio_risk',lambda *a,**k:PortfolioRiskDecision(True))
    r=evaluated(native);a=first(r)
    assert a['status']=='NOT_EVALUABLE'
    assert a['gates']['PORTFOLIO_RISK']['status']=='NOT_EVALUABLE'
    assert a['gates']['PORTFOLIO_RISK']['equity'] is None


def test_upstream_choppy_decisions_never_enter_downstream(native):
    bars=deepcopy(native['strategy_evidence']['feature_bars']+native['candles']['BTCUSDT'])
    for b in bars: b.update(open=100.,high=101.,low=99.,close=100.2)
    d=build_strategy_input(bars,dict(native['strategy_evidence']['quote_volumes']),native['strategy_evidence']['provenance'],
        start_ms=START,end_ms=START+4*STEP,run_id='downstream-choppy')
    r=evaluated(d)
    assert len(r['downstream'])==4
    assert all(x['status']=='UPSTREAM_REJECT' and x['gates']=={} and x['observed_native_reason']=='TOO_CHOPPY' for x in r['downstream'])


def test_missing_scoring_feature_never_exposes_a_favorable_native_fallback(native,monkeypatch):
    sim=downstream._simulation_module();original=sim._historical_authoritative_score
    def incomplete(*a,**k):
        r=original(*a,**k)
        r['diagnostics']['historical_scoring_context']['missing_inputs'].append('setup_quality')
        r['diagnostics']['authoritative_components']['setup_quality']=.5
        return r
    monkeypatch.setattr(sim,'_historical_authoritative_score',incomplete)
    monkeypatch.setattr(downstream,'_simulation_module',lambda:sim)
    scoring=first(evaluated(native))['gates']['SCORING']
    assert scoring['status']=='NOT_EVALUABLE' and scoring['features']['setup_quality'] is None
