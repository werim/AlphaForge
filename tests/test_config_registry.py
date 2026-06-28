from pathlib import Path

import pytest

from alphaforge.config_registry import CONFIG_REGISTRY, REGISTRY_BY_ENV, config_snapshot, decision_filter_config, write_dashboard_overrides


def _env_names_from_example():
    names = set()
    for line in Path('.env.example').read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        name = line.split('=', 1)[0].strip()
        if name in REGISTRY_BY_ENV:
            names.add(name)
    return names


def test_registry_fields_complete_for_managed_env_example():
    assert _env_names_from_example()
    for name in _env_names_from_example():
        s = REGISTRY_BY_ENV[name]
        assert s.value_type in {'int', 'float', 'bool', 'str'}
        assert s.category
        assert s.applies_to
        assert s.description
        assert s.default is not None or s.value_type == 'str'


def test_dashboard_managed_settings_are_registry_backed_and_secrets_not_editable():
    rows = config_snapshot(mode='PAPER')
    assert any(r['env_name'] == 'MIN_EFFECTIVE_RR' and r['dashboard_editable'] for r in rows)
    for row in rows:
        assert row['env_name'] in REGISTRY_BY_ENV
        if row['secret']:
            assert not row['dashboard_editable']


def test_deprecated_alias_parses(monkeypatch):
    monkeypatch.setenv('ALPHAFORGE_MIN_EFFECTIVE_RR', '1.9')
    assert decision_filter_config('PAPER')['MIN_EFFECTIVE_RR'] == pytest.approx(1.9)


def test_dashboard_override_validation(tmp_path):
    write_dashboard_overrides({'MIN_EFFECTIVE_RR': '1.4'}, root=tmp_path)
    assert (tmp_path / 'config/runtime_overrides.json').exists()
    with pytest.raises(ValueError):
        write_dashboard_overrides({'UNKNOWN_SETTING': '1'}, root=tmp_path)
    with pytest.raises(ValueError):
        write_dashboard_overrides({'ALPHAFORGE_ENABLE_LIVE_TRADING': 'true'}, root=tmp_path)


def test_all_dashboard_editable_managed_settings_are_documented_in_env_example():
    env_names = _env_names_from_example()
    editable = {s.env_name for s in CONFIG_REGISTRY if s.dashboard_editable and not s.secret}
    assert editable.issubset(env_names)
