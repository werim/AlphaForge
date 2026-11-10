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
