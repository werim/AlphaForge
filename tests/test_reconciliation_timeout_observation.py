"""Offline production-path evidence tests; no credentials or network required."""
import asyncio
import json
import threading
import socket

from alphaforge.burnin import BurnInRun, bootstrap_burnin_schema, persist_burnin_run
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text

from alphaforge.binance_reconciliation_provider import BinanceReadonlyReconciliationConfig, BinanceReadonlyReconciliationProvider
from alphaforge.readonly_snapshot_reader import ReadonlySnapshotReader
from test_issue662_reconciliation_read_lifetime import runtime

SECRET = 'DO_NOT_PERSIST_KEY_SIGNATURE_ACCOUNT'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('network forbidden')
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)



def provider(http):
    return BinanceReadonlyReconciliationProvider(config=BinanceReadonlyReconciliationConfig(
        base_url='https://offline.invalid', api_key=SECRET, api_secret=SECRET), http_get_json=http)


def latest(orchestrator):
    with orchestrator._resolve_persistence_engine().connect() as c:
        return json.loads(c.execute(text('SELECT diagnostics_json FROM exchange_reconciliation_events ORDER BY id DESC LIMIT 1')).scalar_one())


def test_deadline_persists_inflight_endpoint_worker_stack_and_run_reference(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def http(url, headers, timeout):
        path = urlsplit(url).path
        calls.append(path)
        if path.endswith('openOrders'):
            entered.set()
            assert release.wait(1)
        return []
    p = provider(http)
    o = runtime(p)
    o._burnin_run_id = 'canonical-run-ref'
    engine = o._resolve_persistence_engine()
    with engine.begin() as c:
        raw = c.connection.driver_connection
        bootstrap_burnin_schema(raw)
        persist_burnin_run(raw, BurnInRun(burnin_run_id=o._burnin_run_id, release_id='test-release',
            git_commit='a'*40, config_hash='b'*64, strategy_config_hash='c'*64, universe_hash='d'*64,
            source_provenance={'provider':'BINANCE','evidence_kind':'OFFLINE_FIXTURE'}))
    async def check():
        try:
            await o._run_reconciliation_once()
            assert entered.is_set()
            d = latest(o)
            assert d['failure_class'] == 'UNKNOWN' and d['timeout_phase'] == 'PROVIDER_READ'
            assert d['deadline_observation']['origin'] == 'APPLICATION_DEADLINE'
            assert d['deadline_observation']['budget_sec'] == .03
            obs = d['read_observation']
            assert obs['state'] == 'RUNNING' and obs['worker_alive'] and obs['result_unaccepted']
            assert obs['worker_stack'] and len(obs['worker_stack']) <= 8
            requests = obs['provider']['requests']
            assert [r['endpoint_class'] for r in requests] == ['positionRisk', 'openOrders']
            assert requests[-1]['state'] == 'IN_FLIGHT' and requests[-1]['elapsed_sec'] >= 0
            assert len({r['request_id'] for r in requests}) == 2
            assert obs['operation_id'] == obs['provider']['operation_id']
            assert d['provenance']['burnin_run_id'] == 'canonical-run-ref'
            assert 'burnin_runs.git_commit/config_hash' in d['provenance']['source_config_authority']
            with engine.connect() as c:
                # SHA/config remain owned by canonical run SQL, not copied defaults.
                bound = c.execute(text("SELECT git_commit,config_hash FROM burnin_runs WHERE burnin_run_id=:run"),
                    {'run':d['provenance']['burnin_run_id']}).one()
            assert tuple(bound) == ('a'*40, 'b'*64)
            serialized = json.dumps(d)
            assert SECRET not in serialized and 'signature=' not in serialized and 'X-MBX' not in serialized
            assert o._unknown_exchange_state and o._reconciliation_reader.has_unaccepted_read
            # A delayed result must not turn this cycle into an accepted CLEAN read.
            release.set()
            await o._shutdown_tasks()
            assert o._unknown_exchange_state
        finally:
            release.set()
            await o._reconciliation_reader.close(.5)
    asyncio.run(check())
    assert calls == ['/fapi/v3/positionRisk', '/fapi/v1/openOrders']


def test_completed_transport_exception_records_types_without_messages_or_urls():
    calls = []
    def http(url, headers, timeout):
        calls.append(1)
        try:
            raise TimeoutError(SECRET + url)
        except TimeoutError as cause:
            raise ConnectionResetError(SECRET) from cause
    p = provider(http)
    o = runtime(p)
    o.config.reconciliation_timeout_sec = 1
    async def check():
        try:
            await o._run_reconciliation_once()
            d = latest(o)
            assert d['failure_class'] == 'TRANSIENT_TRANSPORT'
            assert 'deadline_observation' not in d
            rows = d['provider_observation']['requests']
            assert len(rows) == 2  # existing retry count is unchanged
            assert all(r['state'] == 'FAILED' for r in rows)
            assert rows[-1]['exception_types'] == ['ConnectionResetError', 'TimeoutError']
            assert SECRET not in json.dumps(d)
            assert 'signature=' not in json.dumps(d['provider_observation'])
        finally:
            await o._shutdown_tasks()
    asyncio.run(check())
    assert len(calls) == 2


def test_observation_never_calls_custom_provider_hooks():
    class Custom:
        def read_diagnostics(self):
            raise AssertionError('arbitrary hooks must not run')
        def snapshot(self):
            return {'evidence_status': 'COMPLETE'}
    async def check():
        r = ReadonlySnapshotReader()
        await r.read(Custom())
        d = r.diagnostics()
        assert d['state'] == 'COMPLETE' and not d['result_unaccepted']
        assert 'provider' not in d
        assert await r.close(.1)
    asyncio.run(check())


def test_worker_exception_chain_is_bounded_and_redacted():
    class Custom:
        def snapshot(self):
            try: raise ValueError(SECRET)
            except ValueError as cause: raise RuntimeError(SECRET) from cause
    async def check():
        r = ReadonlySnapshotReader()
        with pytest.raises(RuntimeError): await r.read(Custom())
        d = r.diagnostics()
        assert d['exception_types'] == ['RuntimeError', 'ValueError']
        assert SECRET not in json.dumps(d)
        assert await r.close(.1)
    asyncio.run(check())


def test_diagnostic_failure_cannot_strand_provider_future(monkeypatch):
    import alphaforge.readonly_snapshot_reader as module
    class Custom:
        def snapshot(self): raise ValueError('original provider error')
    def broken(exc): raise RuntimeError('diagnostic failure')
    monkeypatch.setattr(module, 'exception_types', broken)
    async def check():
        r = ReadonlySnapshotReader()
        with pytest.raises(ValueError, match='original provider error'):
            await asyncio.wait_for(r.read(Custom()), .5)
        assert r.diagnostics()['state'] == 'OBSERVATION_UNAVAILABLE'
        assert await r.close(.1)
    asyncio.run(check())


def test_custom_exception_hooks_cannot_break_exception_metadata():
    from alphaforge.reconciliation_observation import exception_types
    class Hostile(RuntimeError):
        def __getattribute__(self, name):
            if name.startswith('__'): raise AssertionError(SECRET)
            return super().__getattribute__(name)
    assert exception_types(Hostile(SECRET)) == ['OTHER']


def test_exception_cause_truthiness_cannot_delay_original_provider_error():
    bool_called, release = threading.Event(), threading.Event()
    class BlockingCause(Exception):
        def __bool__(self):
            bool_called.set()
            release.wait(1)
            return True
    class Provider:
        def snapshot(self):
            raise RuntimeError('original provider failure') from BlockingCause(SECRET)
    async def check():
        r = ReadonlySnapshotReader()
        try:
            with pytest.raises(RuntimeError, match='original provider failure'):
                await asyncio.wait_for(r.read(Provider()), .15)
            assert not bool_called.is_set()
            assert r.diagnostics()['exception_types'] == ['RuntimeError', 'OTHER']
            assert SECRET not in json.dumps(r.diagnostics())
        finally:
            release.set()
            assert await r.close(.5)
    asyncio.run(check())


@pytest.mark.parametrize('attribute', ['__cause__', '__context__', '__suppress_context__'])
def test_exception_chain_metadata_never_evaluates_subclass_descriptors(attribute):
    from alphaforge.reconciliation_observation import exception_types
    touched = []
    def descriptor(self):
        touched.append(attribute)
        return False if attribute == '__suppress_context__' else None
    Hostile = type('Hostile', (RuntimeError,), {attribute: property(descriptor)})
    assert exception_types(Hostile(SECRET)) == ['OTHER']
    assert touched == []


def test_exception_type_metadata_never_evaluates_metaclass_descriptors():
    from alphaforge.reconciliation_observation import exception_types
    touched = []
    class Meta(type):
        @property
        def __name__(cls):
            touched.append('name')
            return SECRET
    class Hostile(RuntimeError, metaclass=Meta):
        pass
    assert exception_types(Hostile(SECRET)) == ['OTHER']
    assert touched == []
