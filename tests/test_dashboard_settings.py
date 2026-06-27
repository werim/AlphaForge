import pytest
fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from alphaforge.dashboard.app import create_app


def test_settings_page_renders_and_exports():
    client = TestClient(create_app('sqlite+pysqlite:///:memory:'))
    r = client.get('/settings')
    assert r.status_code == 200
    assert 'Settings' in r.text
    assert 'Trade Quality Filters' in r.text
    assert 'ALPHAFORGE_MIN_EFFECTIVE_RR' in r.text
    assert '<select name="ALPHAFORGE_BLOCK_CHOP_MARKET"' in r.text
    assert 'LIVE LOCKED / NOT READY' in r.text
    e = client.get('/settings/export')
    assert e.status_code == 200
    assert any(row['env_name'] == 'ALPHAFORGE_MAX_TRADES_GLOBAL_PER_DAY' for row in e.json()['config_snapshot'])


def test_invalid_and_unknown_settings_rejected():
    client = TestClient(create_app('sqlite+pysqlite:///:memory:'))
    r = client.post('/settings/save', data={'ALPHAFORGE_MIN_EFFECTIVE_RR': 'not-float'})
    assert r.status_code == 200
    assert 'could not convert' in r.text or 'invalid' in r.text.lower()
    r = client.post('/settings/save', data={'ALPHAFORGE_ENABLE_LIVE_TRADING': 'true'})
    assert r.status_code == 200
    assert 'cannot enable LIVE' in r.text


def test_bool_save_valid_invalid_and_unknown_rejected():
    client = TestClient(create_app('sqlite+pysqlite:///:memory:'))
    r = client.post('/settings/save', data={'ALPHAFORGE_BLOCK_CHOP_MARKET': 'false'})
    assert r.status_code == 200
    assert 'Settings saved' in r.text
    r = client.post('/settings/save', data={'ALPHAFORGE_BLOCK_CHOP_MARKET': 'maybe'})
    assert r.status_code == 200
    assert 'invalid bool' in r.text
    r = client.post('/settings/save', data={'UNKNOWN_SETTING': '1'})
    assert r.status_code == 200
    assert 'Unknown managed setting' in r.text


def test_environment_locked_setting_not_editable(monkeypatch):
    monkeypatch.setenv('ALPHAFORGE_MIN_EFFECTIVE_RR', '2.2')
    client = TestClient(create_app('sqlite+pysqlite:///:memory:'))
    html = client.get('/settings').text
    assert 'env locked' in html
    r = client.post('/settings/save', data={'ALPHAFORGE_MIN_EFFECTIVE_RR': '1.6'})
    assert 'environment-locked' in r.text
