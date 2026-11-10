"""Exception graph semantics, entirely offline; no provider requests."""
from urllib.error import HTTPError
import socket

import pytest

from alphaforge.provider_failures import classify_provider_exception, PERMANENT_AUTH_OR_PROTOCOL, TRANSIENT_TRANSPORT, UNKNOWN


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs): raise AssertionError('network forbidden')
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)


def test_false_explicit_auth_cause_wins_over_unrelated_timeout_context():
    calls = []
    class FalseAuth(HTTPError):
        def __bool__(self):
            calls.append('bool')
            return False
    outer = RuntimeError('wrapper')
    outer.__cause__ = FalseAuth('https://offline.invalid', 401, 'denied', {}, None)
    outer.__context__ = TimeoutError('unrelated')
    assert classify_provider_exception(outer) == PERMANENT_AUTH_OR_PROTOCOL
    assert calls == []


def test_false_explicit_transport_cause_wins_over_unrelated_auth_context():
    class FalseTimeout(TimeoutError):
        def __bool__(self):
            raise AssertionError('classification must not call bool')
    outer = RuntimeError('wrapper')
    outer.__cause__ = FalseTimeout('timeout')
    outer.__context__ = HTTPError('https://offline.invalid', 401, 'denied', {}, None)
    assert classify_provider_exception(outer) == TRANSIENT_TRANSPORT


def test_context_fallback_and_cycles_retain_existing_policy():
    outer = RuntimeError('wrapper')
    outer.__context__ = TimeoutError('timeout')
    assert classify_provider_exception(outer) == TRANSIENT_TRANSPORT
    outer.__context__ = outer
    assert classify_provider_exception(outer) == UNKNOWN


@pytest.mark.parametrize('status,code', [(401,None), (418,None), (418,-1003), (429,-1003), (500,None)])
def test_http_exception_and_snapshot_evidence_have_one_classification(status, code):
    from alphaforge.provider_failures import classify_reconciliation_snapshot
    exc = HTTPError('https://offline.invalid', status, 'offline', {}, None)
    exc._alphaforge_binance_code = code
    source = {'request_attempts':[{'http_status':status,'binance_code':code,'transport_category':'HTTP'}]}
    assert classify_reconciliation_snapshot(source) == classify_provider_exception(exc)


def test_declared_unknown_is_not_upgraded_by_rate_limit_snapshot():
    from alphaforge.provider_failures import classify_reconciliation_snapshot
    source = {'failure_class':UNKNOWN,'request_attempts':[{'http_status':418,'binance_code':-1003}]}
    assert classify_reconciliation_snapshot(source) == UNKNOWN
