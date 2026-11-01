"""Test real replay comparison boundaries with deterministic evaluator fixtures.

The evaluator stub isolates artifact identity; this is not producer/market evidence.
"""
from copy import deepcopy
import json

import pytest
from alphaforge.backtest_source_evidence import digest
from alphaforge import historical_execution_scenario as scenario
from alphaforge import historical_downstream_scenario as downstream


@pytest.fixture(params=[scenario, downstream], ids=['scenario', 'downstream'])
def replay_boundary(request, monkeypatch):
    module = request.param
    expected = dict(schema=module.VERSION, order_admission='DISABLED',
                    scenario=dict(fee_pct=0.0, liquidity_score=1.0),
                    source_window=dict(feature_only_rows=1))
    evaluator = 'evaluate_scenario' if module is scenario else 'evaluate_downstream'
    monkeypatch.setattr(module, evaluator, lambda *a, **kw: deepcopy(expected))
    replay = module.replay_scenario if module is scenario else module.replay_downstream
    data = dict(fixture='REPLAY_BOUNDARY_ONLY_NOT_PRODUCER_EVIDENCE')
    packet = dict(result=expected, result_hash=digest(expected))
    return replay, data, packet


@pytest.mark.parametrize('field,value', [
    ('fee_pct', False), ('liquidity_score', True), ('feature_only_rows', 1.0),
])
def test_equal_python_values_with_different_json_types_are_refused(replay_boundary, field, value):
    replay, data, packet = replay_boundary
    forged = deepcopy(packet)
    section = 'source_window' if field == 'feature_only_rows' else 'scenario'
    forged['result'][section][field] = value
    # Demonstrates why Python equality alone is not an artifact identity check.
    assert forged == packet
    assert digest(forged) != digest(packet)
    with pytest.raises(ValueError, match='replay mismatch/promotion'):
        replay(forged, data, expected_input_hash=digest(data))


def test_valid_json_roundtrip_and_key_reordering_remain_valid(replay_boundary):
    replay, data, packet = replay_boundary
    loaded = json.loads(json.dumps(packet, sort_keys=True))
    assert replay(loaded, data, expected_input_hash=digest(data)) == packet['result']


@pytest.mark.parametrize('rehash', [False, True])
def test_changed_values_or_rehashed_type_promotion_stay_refused(replay_boundary, rehash):
    replay, data, packet = replay_boundary
    changed = deepcopy(packet)
    changed['result']['scenario']['fee_pct'] = False if rehash else 0.1
    if rehash:
        changed['result_hash'] = digest(changed['result'])
    with pytest.raises(ValueError, match='replay mismatch/promotion'):
        replay(changed, data, expected_input_hash=digest(data))


def test_independent_input_pin_is_still_required(replay_boundary):
    replay, data, packet = replay_boundary
    with pytest.raises(ValueError, match='pin mismatch'):
        replay(packet, data, expected_input_hash='0' * 64)


@pytest.mark.parametrize('kind', ['scenario', 'downstream'])
def test_real_producer_replay_refuses_boolean_promotion(kind, tmp_path):
    # Requires a real, correctly based Git checkout. Never run this producer
    # under an export that could inherit a different parent repository identity.
    from test_historical_downstream_scenario import make_native, scenario as model

    data = make_native()
    policy = model(data)
    if kind == 'scenario':
        packet = scenario.persist_scenario(tmp_path / 'scenario.json', data,
            mode=scenario.MODELED_HISTORICAL_V1, scenario=policy)
        replay = scenario.replay_scenario
        modeled = packet['result']['scenario']
    else:
        packet = downstream.persist_downstream(tmp_path / 'downstream.json', data,
            mode=scenario.MODELED_HISTORICAL_V1, scenario=policy)
        replay = downstream.replay_downstream
        modeled = packet['result']['mtf_evaluation']['scenario']
    assert modeled['liquidity_score'] == 1.0
    modeled['liquidity_score'] = True
    assert packet['result_hash'] != digest(packet['result'])
    with pytest.raises(ValueError, match='replay mismatch/promotion'):
        replay(packet, data, expected_input_hash=digest(data), scenario=policy)
