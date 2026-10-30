"""Explicit assumptions exercise authorities; fixtures are not market evidence."""
from copy import deepcopy
from dataclasses import asdict
import math
import pytest

from alphaforge.backtest_source_evidence import digest
from alphaforge.historical_execution_scenario import MODELED_HISTORICAL_V1
from alphaforge.historical_scoring_evidence import ScoringAssumptions, VERSION, input_evidence
import alphaforge.historical_downstream_scenario as downstream
from test_historical_downstream_scenario import make_native, scenario, first


@pytest.fixture(scope='module')
def native(): return make_native()


def policy(s, **overrides):
    values=dict(schema=VERSION,scenario_id=s.scenario_id,effective_from_ms=s.start_ms,
        funding_rate_fraction=0.,fakeout_risk=.25,rationale='Explicit canonical-fallback reference, not measured or calibrated')
    values.update(overrides)
    return ScoringAssumptions.from_dict(values)


def evaluate(data, s, p):
    return downstream.evaluate_downstream(data,mode=MODELED_HISTORICAL_V1,scenario=s,scoring_assumptions=p)


@pytest.mark.parametrize('field,value', [
    ('funding_rate_fraction',math.nan),('funding_rate_fraction',math.inf),
    ('funding_rate_fraction',1),('funding_rate_fraction',-1),('funding_rate_fraction',True),
    ('fakeout_risk',-.01),('fakeout_risk',1.01),('fakeout_risk',math.nan),
    ('fakeout_risk','0.25'),('effective_from_ms',True),('rationale',''),('schema','OBSERVED'),
    ('scenario_id','x'*64)])
def test_invalid_parameters(native,field,value):
    with pytest.raises(ValueError): policy(scenario(native),**{field:value})


def test_no_implicit_zero_no_native_promotion(native):
    s=scenario(native); original=deepcopy(native)
    baseline=first(evaluate(native,s,None))
    assert {r['kind'] for r in baseline['scoring_input_evidence'].values()}=={'UNAVAILABLE'}
    assert all(r['value'] is None for r in baseline['scoring_input_evidence'].values())
    assert baseline['gates']['SCORING']['score'] is None
    p=policy(s)
    with pytest.raises(ValueError): downstream.evaluate_downstream(native,scoring_assumptions=p)
    r=evaluate(native,s,p); row=first(r)
    assert native==original
    assert row['gates']['SCORING']['score'] is not None
    assert row['gates']['SHARED_QUALITY_AND_EXECUTION_COST']['effective_rr'] is not None
    assert row['inputs']['funding_rate_pct']==0.
    assert row['measured_order_latency_ms'] is None
    assert row['native_authorization']==r['order_admission']==r['consumer_admission']=='DISABLED'
    assert row['gates']['PORTFOLIO_RISK']['status']=='NOT_EVALUABLE'
    assert row['gates']['SIZING']['quantity'] is None
    assert row['executed_intent'] is None and row['unsupported_exit_reasons']
    assert r['mtf_evaluation']==evaluate(native,s,None)['mtf_evaluation']


def test_partial_inputs_remain_unknown(native):
    s=scenario(native)
    for missing in ('funding_rate_fraction','fakeout_risk'):
        row=first(evaluate(native,s,policy(s,**{missing:None})))
        assert row['gates']['SCORING']['status']=='NOT_EVALUABLE'
        assert row['gates']['SCORING']['score'] is None
        if missing=='funding_rate_fraction':
            assert row['gates']['SHARED_QUALITY_AND_EXECUTION_COST']['effective_rr'] is None
    with pytest.raises(ValueError): policy(s,funding_rate_fraction=None,fakeout_risk=None)
    values=asdict(policy(s)); del values['rationale']
    with pytest.raises(ValueError): ScoringAssumptions.from_dict(values)
    for invalid in ('venue','kind','observed_at_ms','funding_rate_pct'):
        with pytest.raises(ValueError): ScoringAssumptions.from_dict({**asdict(policy(s)),invalid:'OBSERVED'})


def test_causal_scope_and_replay(native,tmp_path):
    s=scenario(native); p=policy(s)
    with pytest.raises(ValueError): evaluate(native,s,policy(s,effective_from_ms=s.start_ms+1))
    with pytest.raises(ValueError): evaluate(native,s,policy(s,scenario_id='0'*64))
    with pytest.raises(ValueError): scenario(native,product='USD_M')
    with pytest.raises(ValueError): scenario(native,venue='OTHER')
    path=tmp_path/'research.json'
    packet=downstream.persist_downstream(path,native,mode=MODELED_HISTORICAL_V1,scenario=s,scoring_assumptions=p)
    result=downstream.replay_downstream(packet,native,expected_input_hash=digest(native),scenario=s,scoring_assumptions=p)
    assert result['scoring_policy_id']==p.policy_id
    with pytest.raises(ValueError): downstream.replay_downstream(packet,native,expected_input_hash=digest(native),scenario=s)
    tampered=deepcopy(packet)
    first(tampered['result'])['scoring_input_evidence']['fakeout_risk']['kind']='OBSERVED'
    tampered['result_hash']=digest(tampered['result'])
    with pytest.raises(ValueError): downstream.replay_downstream(tampered,native,expected_input_hash=digest(native),scenario=s,scoring_assumptions=p)


def test_signed_fraction_once_through_canonical_cost_and_score(native):
    s=scenario(native)
    plus=first(evaluate(native,s,policy(s,funding_rate_fraction=.0001)))
    minus=first(evaluate(native,s,policy(s,funding_rate_fraction=-.0001)))
    zero=first(evaluate(native,s,policy(s)))
    for row, sign in ((plus,1),(minus,-1)):
        e=row['scoring_input_evidence']
        assert e['funding_rate']['value']==e['funding_rate_pct']['value']==sign*.0001
        assert e['funding_rate']['units']=='SIGNED_FRACTION'
        assert e['funding_rate']['observed_at_ms'] is None
        cost=row['gates']['SHARED_QUALITY_AND_EXECUTION_COST']
        assert cost['estimated_components']['funding_penalty']==pytest.approx(.0001*2.5)
        assert cost['total_penalty']-zero['gates']['SHARED_QUALITY_AND_EXECUTION_COST']['total_penalty']==pytest.approx(.0001*2.5)
        assert cost['booked_fee'] is None and cost['actual_fill'] is None
    assert plus['gates']['SCORING']['score']==minus['gates']['SCORING']['score']
    assert plus['gates']['SCORING']['score']<zero['gates']['SCORING']['score']
    for r in input_evidence(s,policy(s)).values():
        assert r['kind']=='MODELED' and r['source']==policy(s).policy_id
        assert (r['venue'],r['product'],r['instrument'])==('BINANCE','SPOT','BTCUSDT')
