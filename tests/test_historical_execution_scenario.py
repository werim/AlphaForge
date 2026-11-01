"""Synthetic contract regressions; these fixtures are not historical source proof."""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from alphaforge.backtest_source_evidence import digest, validate_source_packet
from alphaforge.backtest_strategy_adapter import STEP, build_strategy_input
from alphaforge.historical_execution_scenario import (ExecutionScenario, VERSION,
    OBSERVED_NATIVE, MODELED_HISTORICAL_V1, evaluate_scenario, persist_scenario, replay_scenario)

START = 1704153600000


@pytest.fixture(scope='module')
def native():
    rows = [dict(timestamp=t,open=100+i*.01-.005,high=100+i*.01+.02,
                 low=100+i*.01-.02,close=100+i*.01,volume=1000.)
            for i,t in enumerate(range(START-86400000, START+4*STEP, STEP))]
    # Deliberately synthetic content, even though the adapter envelope exercises
    # the supported provider identity. No fixture checksum asserts authenticity.
    return build_strategy_input(rows, {b['timestamp']:100000. for b in rows},
        dict(provider='BINANCE_PUBLIC_SPOT_ARCHIVE',symbol='BTCUSDT',interval_ms=STEP,
             fixture='SYNTHETIC_NOT_HISTORICAL_EVIDENCE'),
        start_ms=START,end_ms=START+4*STEP,run_id='scenario-unit')


def scenario(data, **overrides):
    values = dict(schema=VERSION,name='explicit-unit-assumptions',source_provider='BINANCE_PUBLIC_SPOT_ARCHIVE',
        venue='BINANCE',product='SPOT',instrument='BTCUSDT',input_hash=digest(data),
        native_config_hash=digest(data['strategy_evidence']['profile']),start_ms=START,end_ms=START+4*STEP,
        assumptions_effective_from_ms=START,market_data_latency_ms=200,order_latency_ms=50,
        spread_pct=.0008,expected_slippage_pct=.0001,liquidity_score=1.,fee_pct=.001)
    values.update(overrides)
    return ExecutionScenario.from_dict(values)


def test_disabled_by_default_and_native_audit_unchanged(native):
    before = deepcopy(native)
    observed = evaluate_scenario(native)
    assert observed['mode'] == OBSERVED_NATIVE and observed['modeled'] == []
    assert observed['scenario_id'] is None
    assert observed['observed_native'] == native['strategy_evidence']['records']
    with pytest.raises(ValueError): evaluate_scenario(native,scenario=scenario(native))
    with pytest.raises(ValueError): evaluate_scenario(native,mode=MODELED_HISTORICAL_V1)
    assert native == before


def test_modeled_tags_and_causality_never_promote_orders(native, tmp_path):
    s = scenario(native)
    p = persist_scenario(tmp_path/'scenario.json',native,mode=MODELED_HISTORICAL_V1,scenario=s)
    r = p['result']
    assert r['observed_native'] == native['strategy_evidence']['records']
    assert len(r['modeled']) == len(r['observed_native']) == 4
    assert r['scenario_id'] == s.scenario_id
    assert r['consumer_admission'] == r['order_admission'] == 'DISABLED'
    assert r['capital_source_status'] == 'NOT_PRODUCED'
    assert r['capital_return_pct'] is r['max_drawdown_pct'] is None
    for m, n in zip(r['modeled'],r['observed_native']):
        assert m['decision_ms'] == n['decision_ms']+200
        assert m['assumed_available_ms'] <= m['decision_ms'] < m['earliest_execution_ms']
        assert m['earliest_execution_ms'] % STEP == 0
        assert m['signal_ms']+STEP == m['source_release_ms']
        assert m['measured_market_data_latency_ms'] is m['measured_order_latency_ms'] is None
        assert m['execution_ms'] is m['mapped_intent_id'] is None
        assert m['status'] != 'ACCEPT' and m['downstream_status'] == 'NOT_EVALUATED'
        assert 'UNSUPPORTED_NATIVE_PROTECTIVE_EXITS' in m['unsupported_exit_reasons']
        if m['mtf']:
            assert m['mtf']['execution']['evidence_status'] == 'MODELED_NOT_OBSERVED'
            assert m['mtf']['execution']['market_data_latency_status'] == 'MODEL_ESTIMATE'
        else:
            assert m['status'] == 'UPSTREAM_REJECT' and m['reasons'] == [n['native_reason']]
    persisted = json.loads((tmp_path/'scenario.json').read_text())
    assert replay_scenario(persisted,native,expected_input_hash=digest(native),scenario=s) == r
    assert evaluate_scenario(native,mode=MODELED_HISTORICAL_V1,scenario=s) == r
    with pytest.raises(FileExistsError): persist_scenario(tmp_path/'scenario.json',native,mode=MODELED_HISTORICAL_V1,scenario=s)
    with pytest.raises((ValueError,KeyError)): validate_source_packet(p,expected_identity_hash='0'*64,expected_completion_hash='0'*64)


@pytest.mark.parametrize('field', ['market_data_latency_ms','order_latency_ms','spread_pct',
    'expected_slippage_pct','liquidity_score','fee_pct'])
@pytest.mark.parametrize('value', [None,True,'0',-1,float('nan'),float('inf')])
def test_invalid_assumptions_never_default_to_zero(native,field,value):
    with pytest.raises(ValueError): scenario(native,**{field:value})


@pytest.mark.parametrize('field,value', [('venue','OTHER'),('product','USDT_PERPETUAL'),
    ('instrument','ETHUSDT'),('source_provider','OTHER'),('schema','v0'),
    ('assumptions_effective_from_ms',START+1),('market_data_latency_ms',STEP),
    ('spread_pct',1.),('liquidity_score',1.1),('start_ms',True),('end_ms',START),
    ('native_config_hash','0'*64),('input_hash','0'*64)])
def test_scope_timing_and_binding_mismatch(native,field,value):
    with pytest.raises(ValueError):
        s=scenario(native,**{field:value})
        evaluate_scenario(native,mode=MODELED_HISTORICAL_V1,scenario=s)


def test_missing_and_unknown_fields_rejected(native):
    values=asdict(scenario(native)); values.pop('fee_pct')
    with pytest.raises(ValueError): ExecutionScenario.from_dict(values)
    values=asdict(scenario(native)); values['funding_rate_pct']=0
    with pytest.raises(ValueError): ExecutionScenario.from_dict(values)


@pytest.mark.parametrize('mutation', ['future','duplicate','gap','native_reason'])
def test_bad_native_audit_cannot_seed_modeled_replay(native,mutation):
    changed=deepcopy(native)
    if mutation=='future': changed['strategy_evidence']['records'][0]['source_available_ms']+=STEP
    if mutation=='duplicate': changed['strategy_evidence']['records'][1]=deepcopy(changed['strategy_evidence']['records'][0])
    if mutation=='gap': changed['candles']['BTCUSDT'].pop()
    if mutation=='native_reason': changed['strategy_evidence']['records'][0]['native_reason']='ACCEPT'
    with pytest.raises(ValueError): evaluate_scenario(changed,mode=MODELED_HISTORICAL_V1,scenario=scenario(changed))


@pytest.mark.parametrize('field', ['mode','observed_native','modeled','order_admission'])
def test_rehashed_packet_tamper_and_promotion_fail_independent_replay(native,field):
    s=scenario(native);r=evaluate_scenario(native,mode=MODELED_HISTORICAL_V1,scenario=s)
    changed=deepcopy(r);changed[field]='PROMOTED'
    packet=dict(result=changed,result_hash=digest(changed))
    with pytest.raises(ValueError): replay_scenario(packet,native,expected_input_hash=digest(native),scenario=s)
    with pytest.raises(ValueError): replay_scenario(dict(result=r,result_hash=digest(r)),native,expected_input_hash='0'*64,scenario=s)


def test_later_prices_cannot_change_an_earlier_modeled_decision(native):
    bars=deepcopy(native['strategy_evidence']['feature_bars']+native['candles']['BTCUSDT'])
    bars[-1].update(open=1000.,high=1001.,low=999.,close=1000.2)
    quotes=dict(native['strategy_evidence']['quote_volumes'])
    changed=build_strategy_input(bars,quotes,native['strategy_evidence']['provenance'],
        start_ms=START,end_ms=START+4*STEP,run_id=native['run_id'])
    before=evaluate_scenario(native,mode=MODELED_HISTORICAL_V1,scenario=scenario(native))['modeled'][0]
    after=evaluate_scenario(changed,mode=MODELED_HISTORICAL_V1,scenario=scenario(changed))['modeled'][0]
    # Dataset/scenario identifiers necessarily bind the whole artifact. Earlier
    # economic decisions and causal times must remain independent of later prices.
    for key in ['status','reasons','decision_ms','earliest_execution_ms']:
        assert before[key] == after[key]
    a=deepcopy(before['mtf']);b=deepcopy(after['mtf'])
    if a is not None:
        a['execution'].pop('market_data_latency_source');b['execution'].pop('market_data_latency_source')
    assert a == b


def test_explicit_zero_is_an_assumption_not_a_measured_zero(native):
    r=evaluate_scenario(native,mode=MODELED_HISTORICAL_V1,
        scenario=scenario(native,market_data_latency_ms=0,order_latency_ms=0,fee_pct=0.))
    for m in r['modeled']:
        assert m['decision_ms']+STEP == m['earliest_execution_ms']
        assert m['measured_market_data_latency_ms'] is None


@pytest.mark.parametrize('field,value', [('provider','OTHER'),('symbol','ETHUSDT'),('interval_ms',300000)])
def test_source_identity_must_match_the_scenario(native,field,value):
    changed=deepcopy(native);changed['strategy_evidence']['provenance'][field]=value
    with pytest.raises(ValueError): evaluate_scenario(changed,mode=MODELED_HISTORICAL_V1,scenario=scenario(changed))


def test_modeled_mtf_context_is_not_admissible_to_observed_native_gate(native):
    from alphaforge.multi_timeframe import evaluate_mtf_alignment
    r=evaluate_scenario(native,mode=MODELED_HISTORICAL_V1,scenario=scenario(native))
    contexts=[m for m in r['modeled'] if m['mtf'] is not None]
    assert contexts
    for m in contexts:
        c=m['mtf']
        checked=evaluate_mtf_alignment(c['regime'],c['setup'],c['execution'],decision_ts_ms=m['decision_ms'])
        assert not checked['aligned']
        assert 'MTF_EXECUTION_UNAVAILABLE' in checked['reasons']
    with pytest.raises(ValueError): scenario(native,spread_pct=10**1000)


def test_too_choppy_is_retained_without_running_a_modeled_mtf(native):
    bars=deepcopy(native['strategy_evidence']['feature_bars']+native['candles']['BTCUSDT'])
    for b in bars: b.update(open=100.,high=101.,low=99.,close=100.2)
    flat=build_strategy_input(bars,dict(native['strategy_evidence']['quote_volumes']),
        native['strategy_evidence']['provenance'],start_ms=START,end_ms=START+4*STEP,run_id='choppy-scenario')
    r=evaluate_scenario(flat,mode=MODELED_HISTORICAL_V1,scenario=scenario(flat))
    assert all(n['native_reason']=='TOO_CHOPPY' for n in r['observed_native'])
    assert all(m['status']=='UPSTREAM_REJECT' and m['reasons']==['TOO_CHOPPY'] and m['mtf'] is None
               for m in r['modeled'])
