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
    assert 'MIN_EFFECTIVE_RR' in r.text
    e = client.get('/settings/export')
    assert e.status_code == 200
    assert any(row['env_name'] == 'ALPHAFORGE_MAX_TRADES_GLOBAL_PER_DAY' for row in e.json()['config_snapshot'])


def test_invalid_and_unknown_settings_rejected():
    client = TestClient(create_app('sqlite+pysqlite:///:memory:'))
    r = client.post('/settings/save', data={'MIN_EFFECTIVE_RR': 'not-float'})
    assert r.status_code == 200
    assert 'could not convert' in r.text or 'invalid' in r.text.lower()
    r = client.post('/settings/save', data={'ALPHAFORGE_ENABLE_LIVE_TRADING': 'true'})
    assert r.status_code == 200
    assert 'cannot enable LIVE' in r.text
