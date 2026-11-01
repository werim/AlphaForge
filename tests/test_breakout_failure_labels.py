"""Actual canonical geometry + research labeling on explicitly synthetic bars."""
from dataclasses import asdict, replace
from copy import deepcopy
import json
import math

import pytest

from alphaforge.backtest_source_evidence import digest
from alphaforge.breakout_failure_labels import (
    Bar, LabelPolicy, CloseBackInside3Policy, freeze_breakout, label_breakout, replay_label,
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


def strict_label(s, rows, asof=5*I):
    return label_breakout(s, CloseBackInside3Policy(s.interval_ms), rows, asof_ms=asof)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_strict_three_bar_equal_close_is_boundary_not_failure(side):
    s = seed(side)
    equal = forward(side, close=s.level, **({'low': 101.} if side == 'LONG' else {'high': 99.}))
    r = strict_label(s, [equal, forward(side, start=3*I), forward(side, start=4*I)])['result']
    assert r['status'] == 'NO_FAILURE_WITHIN_HORIZON' and r['failed_breakout'] is False
    assert r['boundary_equal_closes'] == [dict(event_interval=[2*I, 3*I], close=s.level,
                                              available_ms=3*I, status='LEVEL_EQUAL_CLOSE')]
    assert r['return_to_level']['observed'] is True


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_strict_three_bar_missing_required_candle_is_unavailable(side):
    s = seed(side)
    inside = forward(side, start=4*I, **({'close': 101., 'low': 100.} if side == 'LONG'
                                        else {'close': 99., 'high': 100.}))
    r = strict_label(s, [forward(side), inside])['result']
    assert r['status'] == 'UNAVAILABLE' and r['failed_breakout'] is None
    assert r['missing_interval'] == [3*I, 4*I]
    assert r['resolved_available_ms'] is None


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_strict_three_bar_wick_touch_is_diagnostic_and_negative_needs_three_closes(side):
    s = seed(side)
    touch = forward(side, **({'low': 101.} if side == 'LONG' else {'high': 99.}))
    rows = [touch, forward(side, start=3*I), forward(side, start=4*I)]
    for asof in (2*I, 3*I, 4*I, 5*I-1):
        r = strict_label(s, rows, asof)['result']
        assert r['status'] == 'PENDING' and r['failed_breakout'] is None
        assert r['resolved_available_ms'] is None
    packet = strict_label(s, [s.previous, s.current, *rows])
    r = packet['result']
    assert r['schema'] == 'breakout-failure-label-v2'
    assert r['window_start_ms'] == 2*I and r['due_ms'] == 5*I
    assert r['status'] == 'NO_FAILURE_WITHIN_HORIZON' and r['failed_breakout'] is False
    assert r['consumed_bars'] == 3 and r['resolved_available_ms'] == 5*I
    assert r['return_to_level'] == dict(diagnostic_only=True, observed=True,
                                      event_interval=[2*I, 3*I], available_ms=3*I)
    assert r['boundary_equal_closes'] == []


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_strict_three_bar_first_close_and_late_prefix_control_positive_availability(side):
    s = seed(side)
    inside = forward(side, start=3*I, **({'close': 101., 'low': 100.} if side == 'LONG'
                                        else {'close': 99., 'high': 100.}))
    rows = [forward(side, available_ms=4*I+100), inside]
    assert strict_label(s, rows, asof=4*I+99)['result']['status'] == 'UNAVAILABLE'
    r = strict_label(s, rows, asof=4*I+100)['result']
    assert r['status'] == 'FAILED_BREAKOUT' and r['failed_breakout'] is True
    assert r['event_interval'] == [3*I, 4*I] and r['consumed_bars'] == 2
    assert r['resolved_available_ms'] == 4*I+100 >= inside.end_ms
    assert r['return_to_level']['available_ms'] == 4*I+100


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_strict_three_bar_future_close_is_not_used_early_and_resolved_prefix_is_immutable(side):
    s = seed(side)
    inside = forward(side, **({'close': 101., 'low': 100.} if side == 'LONG'
                              else {'close': 99., 'high': 100.}))
    assert strict_label(s, [inside], asof=3*I-1) == strict_label(s, [], asof=3*I-1)
    assert strict_label(s, [inside], asof=3*I-1)['result']['status'] == 'PENDING'
    delayed = replace(inside, available_ms=3*I+50)
    assert strict_label(s, [delayed], asof=3*I+49)['result']['status'] == 'UNAVAILABLE'
    resolved = strict_label(s, [delayed], asof=3*I+50)
    assert resolved['result']['resolved_available_ms'] == 3*I+50
    later = forward(side, start=3*I, symbol='OTHER', close=math.nan)
    outside = forward(side, start=5*I, close=math.nan)
    assert strict_label(s, [delayed, later, outside]) == resolved


@pytest.mark.parametrize('missing', ['gap', 'unavailable', 'late'])
def test_strict_three_bar_complete_time_window_with_missing_evidence_is_not_negative(missing):
    last = forward(start=4*I)
    if missing == 'unavailable': last = replace(last, available_ms=None)
    if missing == 'late': last = replace(last, available_ms=6*I)
    rows = [forward(), forward(start=3*I)]
    if missing != 'gap': rows.append(last)
    r = strict_label(seed(), rows)['result']
    assert r['status'] == 'UNAVAILABLE' and r['failed_breakout'] is None
    assert r['missing_interval'] == [4*I, 5*I] and r['resolved_available_ms'] is None
    assert r['return_to_level']['observed'] is None


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_strict_three_bar_freezes_level_and_anchors_after_confirming_candle(side):
    s = seed(side, decision_ms=2*I+100)
    first = forward(side, **({'high': 10000.} if side == 'LONG' else {'low': 1.}))
    r = strict_label(s, [s.current, first, forward(side, start=3*I), forward(side, start=4*I)])['result']
    assert r['failed_breakout'] is False and r['frozen_breakout_level'] == s.level
    assert r['decision_ms'] == 2*I+100 and r['window_start_ms'] == 2*I and r['due_ms'] == 5*I
    assert r['return_to_level']['observed'] is False


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_strict_three_bar_equal_boundary_is_distinct_before_window_matures(side):
    s = seed(side)
    equal = forward(side, close=s.level, **({'low': 101.} if side == 'LONG' else {'high': 99.}))
    r = strict_label(s, [equal], asof=3*I)['result']
    assert r['status'] == 'PENDING' and r['failed_breakout'] is None
    assert r['boundary_equal_closes'][0]['status'] == 'LEVEL_EQUAL_CLOSE'
    # The legacy version remains a different, inclusive hypothesis.
    old = label(s, [equal], rule='CLOSE_BACK_INSIDE', horizon=3, asof=3*I)['result']
    assert old['schema'] == 'breakout-failure-label-v1' and old['failed_breakout'] is True
    assert old['policy'] == dict(invalidation='CLOSE_BACK_INSIDE', horizon_bars=3)
    assert old['policy_id'] != r['policy_id'] and 'boundary_equal_closes' not in old


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('inside', [False, True])
def test_strict_three_bar_has_no_implicit_equality_buffer(side, inside):
    s = seed(side)
    toward = -math.inf if (side == 'LONG') == inside else math.inf
    close = math.nextafter(s.level, toward)
    first = forward(side, close=close, **({'low': min(101., close)} if side == 'LONG'
                                         else {'high': max(99., close)}))
    r = strict_label(s, [first, forward(side, start=3*I), forward(side, start=4*I)])['result']
    assert r['failed_breakout'] is inside and r['boundary_equal_closes'] == []


def test_strict_three_bar_duplicates_and_required_source_scope_fail_closed():
    s = seed(); rows = [forward(), forward(start=3*I), forward(start=4*I)]
    assert strict_label(s, [rows[0], *rows]) == strict_label(s, rows)
    r = strict_label(s, [replace(rows[0], low=102.), *rows])['result']
    assert r['status'] == 'AMBIGUOUS_DUPLICATE_SOURCE' and r['failed_breakout'] is None
    assert r['resolved_available_ms'] is None
    with pytest.raises(ValueError, match='source scope'):
        strict_label(s, [replace(rows[0], source_id='OTHER'), *rows[1:]])
    with pytest.raises(ValueError):
        strict_label(s, [replace(rows[0], start_ms=2*I+1), *rows[1:]])


@pytest.mark.parametrize('value', [0, -1, True, I*1.0, None])
def test_strict_three_bar_timeframe_requires_explicit_integer_identity(value):
    with pytest.raises(ValueError): CloseBackInside3Policy(value)


def test_strict_three_bar_policy_timeframe_schema_and_horizon_are_pinned():
    p = CloseBackInside3Policy(I)
    assert CloseBackInside3Policy.from_dict(json.loads(json.dumps(asdict(p)))) == p
    with pytest.raises(ValueError, match='timeframe'):
        label_breakout(seed(), CloseBackInside3Policy(2*I), [], asof_ms=5*I)
    for field, value in [('horizon_bars', 3.0), ('horizon_bars', 2),
                         ('equality', 'INCLUSIVE'), ('schema', 'OTHER'), ('window_anchor', 'DECISION')]:
        bad = {**asdict(p), field: value}
        with pytest.raises(ValueError): CloseBackInside3Policy.from_dict(bad)
    with pytest.raises(ValueError): CloseBackInside3Policy.from_dict({'timeframe_ms': I})


@pytest.mark.parametrize('field,value', [('failed_breakout', 0), ('probability', 0.25),
                                        ('runtime_admission', 'ENABLED'),
                                        ('native_decision_authority', 'ENABLED'),
                                        ('order_admission', 'ENABLED')])
def test_strict_three_bar_rehashed_promotions_and_json_type_edits_fail_replay(tmp_path, field, value):
    s = seed(); p = CloseBackInside3Policy(I)
    rows = [forward(), forward(start=3*I), forward(start=4*I)]
    packet = label_breakout(s, p, rows, asof_ms=5*I)
    path = tmp_path/'new-synthetic-label.json'
    path.write_text(json.dumps(packet))
    loaded = json.loads(path.read_text())
    assert replay_label(loaded, s, p, rows, asof_ms=5*I, expected_seed_id=s.seed_id) == packet
    assert packet['result']['probability'] is None
    assert packet['result']['policy_selection_status'] == 'INITIAL_RESEARCH_ASSUMPTION_NOT_OPTIMIZED'
    loaded['result'][field] = value
    loaded['result_hash'] = digest(loaded['result'])
    with pytest.raises(ValueError, match='replay mismatch'):
        replay_label(loaded, s, p, rows, asof_ms=5*I, expected_seed_id=s.seed_id)
    with pytest.raises(ValueError, match='seed pin'):
        replay_label(packet, s, p, rows, asof_ms=5*I, expected_seed_id='0'*64)
