"""Actual canonical geometry + research labeling on explicitly synthetic bars."""
from dataclasses import asdict, replace
from copy import deepcopy
import json
import math

import pytest

from alphaforge.backtest_source_evidence import digest
from alphaforge.breakout_failure_labels import (
    Bar, LabelPolicy, freeze_breakout, label_breakout, replay_label,
)

I = 1000  # synthetic clock interval; not a suggested research horizon/timeframe


def seed(side='LONG', **overrides):
    previous = Bar(0, I, I, 100., 102., 98., 100., 'BTCUSDT', 'SYNTHETIC_UNIT_FIXTURE')
    current = (Bar(I, 2*I, 2*I, 101., 106., 100., 104., 'BTCUSDT', 'SYNTHETIC_UNIT_FIXTURE') if side == 'LONG'
               else Bar(I, 2*I, 2*I, 99., 100., 94., 96., 'BTCUSDT', 'SYNTHETIC_UNIT_FIXTURE'))
    args = dict(candidate_id='synthetic-candidate', symbol='BTCUSDT',
        setup_type='BREAKOUT_UP' if side == 'LONG' else 'BREAKDOWN_DOWN',
        side=side, decision_ms=2*I, interval_ms=I, previous=previous, current=current,
        context=dict(source_id='SYNTHETIC_UNIT_FIXTURE',
            source_hash=digest([asdict(previous), asdict(current)]),
            config_hash=digest({'test': True}), venue='SYNTHETIC', product='SYNTHETIC',
            availability_basis='SYNTHETIC_TEST'))
    args.update(overrides)
    return freeze_breakout(**args)


def forward(side='LONG', start=2*I, **overrides):
    b = (Bar(start, start+I, start+I, 104., 105., 103., 104., 'BTCUSDT', 'SYNTHETIC_UNIT_FIXTURE') if side == 'LONG'
         else Bar(start, start+I, start+I, 96., 97., 95., 96., 'BTCUSDT', 'SYNTHETIC_UNIT_FIXTURE'))
    return replace(b, **overrides)


def label(s, rows, rule='RETURN_TO_LEVEL', horizon=2, asof=4*I):
    return label_breakout(s, LabelPolicy(rule, horizon), rows, asof_ms=asof)


@pytest.mark.parametrize('side,level', [('LONG', 102.), ('SHORT', 98.)])
def test_freeze_uses_known_canonical_level_and_actual_crossing(side, level):
    s = seed(side)
    assert s.level == level and s.side == side
    assert json.loads(s.context_json)['availability_basis'] == 'SYNTHETIC_TEST'
    assert s == seed(side)


@pytest.mark.parametrize('defect', ['nonbreakout', 'wrong_side', 'wrong_setup', 'unavailable', 'future', 'prefix_gap', 'stale'])
def test_candidate_snapshot_refuses_invalid_scope_or_information(defect):
    changes = {}
    if defect == 'nonbreakout': changes['current'] = replace(seed().current, open=100., high=102., low=99., close=101.)
    if defect == 'wrong_side': changes.update(side='SHORT', setup_type='BREAKOUT_UP', current=seed().current)
    if defect == 'wrong_setup': changes['setup_type'] = 'LONG_CONTINUATION'
    if defect == 'unavailable': changes['current'] = replace(seed().current, available_ms=None)
    if defect == 'future': changes['current'] = replace(seed().current, available_ms=2*I+1)
    if defect == 'prefix_gap': changes['previous'] = replace(seed().previous, start_ms=I, end_ms=2*I, available_ms=2*I)
    if defect == 'stale': changes['decision_ms'] = 3*I
    with pytest.raises(ValueError): seed(**changes)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('rule', ['RETURN_TO_LEVEL', 'CLOSE_BACK_INSIDE'])
def test_definitive_return_and_equality_are_failures(side, rule):
    s = seed(side)
    b = (forward(side, low=101., close=102.) if side == 'LONG'
         else forward(side, high=99., close=98.))
    packet = label(s, [b], rule=rule, asof=3*I)
    r = packet['result']
    assert r['status'] == 'FAILED_BREAKOUT' and r['failed_breakout'] is True
    assert r['event_interval'] == [2*I, 3*I] and r['consumed_bars'] == 1
    assert r['probability'] is None and r['probability_status'] == 'UNAVAILABLE'
    assert r['dependent_evaluation'] == 'NOT_EVALUABLE' and r['runtime_admission'] == 'DISABLED'


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_wick_and_recovery_are_distinct_explicit_hypotheses(side):
    s = seed(side)
    wick = forward(side, **({'low': 101.} if side == 'LONG' else {'high': 99.}))
    rows = [wick, forward(side, start=3*I)]
    assert label(s, rows, rule='RETURN_TO_LEVEL')['result']['failed_breakout'] is True
    close = label(s, rows, rule='CLOSE_BACK_INSIDE')['result']
    assert close['status'] == 'NO_FAILURE_WITHIN_HORIZON' and close['failed_breakout'] is False
    # No competing success/TP barrier exists; do not invent intrabar path order.


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_expiry_is_observed_survival_not_trade_success(side):
    rows = [forward(side), forward(side, start=3*I)]
    r = label(seed(side), rows)['result']
    assert r['status'] == 'NO_FAILURE_WITHIN_HORIZON'
    assert r['failed_breakout'] is False and r['event_interval'] is None
    assert 'pnl' not in r and 'p_win' not in r and r['probability'] is None


@pytest.mark.parametrize('missing', ['gap', 'unavailable', 'late'])
def test_missing_or_unavailable_coverage_never_becomes_survival(missing):
    b = forward(start=3*I)
    if missing == 'unavailable': b = replace(b, available_ms=None)
    if missing == 'late': b = replace(b, available_ms=5*I)
    rows = [forward(), b] if missing != 'gap' else [b]
    r = label(seed(), rows)['result']
    assert r['status'] == 'INCOMPLETE_WINDOW' and r['failed_breakout'] is None


def test_earlier_asof_does_not_use_future_outcomes():
    s = seed(); safe = forward(); failure = forward(start=3*I, low=101., close=102.)
    assert label(s, [safe, failure], asof=3*I) == label(s, [safe], asof=3*I)
    assert label(s, [safe], asof=3*I)['result']['status'] == 'PENDING'
    assert label(s, [safe, failure])['result']['status'] == 'FAILED_BREAKOUT'


def test_prediction_bar_is_excluded_and_future_append_cannot_change_final_label():
    s = seed(); failure = forward(low=101., close=102.)
    expected = label(s, [failure])
    later = forward(start=20*I, low=1., close=2.)
    assert label(s, [s.previous, s.current, failure, later]) == expected
    # Even a conflicting later row cannot reopen an already decisive prefix.
    assert label(s, [failure, forward(start=3*I), forward(start=3*I, low=102.)]) == expected


def test_missing_earlier_bar_blocks_later_failure_and_conflicts_remain_ambiguous():
    s = seed(); late = forward(start=3*I, low=101., close=102.)
    assert label(s, [late])['result']['status'] == 'INCOMPLETE_WINDOW'
    a = forward(); b = forward(low=102.)
    r = label(s, [a, b, late])['result']
    assert r['status'] == 'AMBIGUOUS_DUPLICATE_SOURCE' and r['failed_breakout'] is None
    assert label(s, [a, a, forward(start=3*I)]) == label(s, [a, forward(start=3*I)])


def test_partial_prediction_candle_and_unset_horizon_are_not_forced_labels():
    s = seed(decision_ms=2*I+100)
    r = label(s, [forward(low=101.)])['result']
    assert r['status'] == 'AMBIGUOUS_PREDICTION_BOUNDARY' and r['failed_breakout'] is None
    assert label(seed(), [], horizon=None)['result']['status'] == 'HORIZON_UNSET'


@pytest.mark.parametrize('horizon', [0, -1, True, 1.5, 10001])
def test_horizon_is_explicit_strict_and_bounded(horizon):
    with pytest.raises(ValueError): LabelPolicy('RETURN_TO_LEVEL', horizon)


@pytest.mark.parametrize('field,value', [('low', math.nan), ('high', math.inf), ('close', True), ('low', 200.), ('available_ms', 1)])
def test_malformed_visible_evidence_fails_closed(field, value):
    with pytest.raises(ValueError): label(seed(), [replace(forward(), **{field: value})])


def test_seed_tampering_and_unknown_policies_are_refused():
    with pytest.raises(ValueError): label(replace(seed(), level=999.), [forward()])
    with pytest.raises(ValueError): LabelPolicy('DEFAULT', 2)
    with pytest.raises(ValueError): label(seed(), [], asof=I)
    with pytest.raises(ValueError): seed(context={})


def test_serialized_replay_requires_independent_pin_and_preserves_json_types():
    s = seed(); p = LabelPolicy('RETURN_TO_LEVEL', 2); rows = [forward(), forward(start=3*I)]
    packet = label_breakout(s, p, rows, asof_ms=4*I)
    loaded = json.loads(json.dumps(packet))
    assert replay_label(loaded, s, p, rows, asof_ms=4*I, expected_seed_id=s.seed_id) == packet
    bad = deepcopy(packet); bad['result']['failed_breakout'] = 0
    assert bad == packet and digest(bad) != digest(packet)
    with pytest.raises(ValueError): replay_label(bad, s, p, rows, asof_ms=4*I, expected_seed_id=s.seed_id)
    with pytest.raises(ValueError): replay_label(packet, s, p, rows, asof_ms=4*I, expected_seed_id='0'*64)


def test_horizon_and_policy_ids_are_separate_research_targets():
    s = seed(); rows = [forward(), forward(start=3*I, low=101., close=102.)]
    short = label(s, rows, horizon=1)['result']
    long = label(s, rows, horizon=2)['result']
    assert short['failed_breakout'] is False and long['failed_breakout'] is True
    assert short['policy_id'] != long['policy_id']


@pytest.mark.parametrize('change', [{'symbol': 'ETHUSDT'}, {'source_id': 'OTHER'}, {'start_ms': 2500, 'end_ms': 3500, 'available_ms': 3500}])
def test_forward_scope_and_time_grid_are_enforced(change):
    with pytest.raises(ValueError): label(seed(), [replace(forward(), **change)])


def test_candidate_scope_mismatch_is_refused():
    with pytest.raises(ValueError): seed(current=replace(seed().current, symbol='ETHUSDT'))
