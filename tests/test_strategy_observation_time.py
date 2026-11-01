"""Source-time regressions; synthetic decisions are not historical acceptance."""
from copy import deepcopy
import pytest

from alphaforge.backtest_strategy_adapter import validate_strategy_evidence
from test_backtest_strategy_adapter import data


@pytest.fixture(scope='module')
def native():
    return data()


@pytest.mark.parametrize('recompute', [False, True])
@pytest.mark.parametrize('defect', ['negative', 'before_availability', 'bool', 'float'])
def test_invalid_observation_time_is_refused(native, recompute, defect):
    changed = deepcopy(native)
    record = changed['strategy_evidence']['records'][0]
    record['observed_ms'] = {
        'negative': -1,
        'before_availability': record['source_available_ms'] - 1,
        'bool': True,
        'float': float(record['source_available_ms']),
    }[defect]
    with pytest.raises(ValueError):
        validate_strategy_evidence(changed, recompute=recompute)


@pytest.mark.parametrize('recompute', [False, True])
@pytest.mark.parametrize('offset', [0, 1, 86400000])
def test_available_observation_time_remains_valid(native, recompute, offset):
    changed = deepcopy(native)
    for record in changed['strategy_evidence']['records']:
        record['observed_ms'] = record['source_available_ms'] + offset
    validate_strategy_evidence(changed, recompute=recompute)
    assert changed['intents'] == []
    assert changed['strategy_evidence']['execution_admission'] == 'BLOCKED_NATIVE_EXIT_SEMANTICS'


@pytest.mark.parametrize('observed', [-1, 1704153599999])
def test_bad_observation_is_refused_before_output_creation(native, tmp_path, observed):
    from alphaforge.backtest_chronology import run_boundary_backtest

    changed = deepcopy(native)
    changed['strategy_evidence']['records'][0]['observed_ms'] = observed
    output = tmp_path / 'refused'
    with pytest.raises(ValueError, match='observation precedes source availability'):
        run_boundary_backtest(changed, output)
    assert not output.exists()
