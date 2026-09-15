"""Conservative classification shared by read-only exchange providers."""

from __future__ import annotations

import errno
import socket
import ssl
from urllib import error
from typing import Any, Mapping

TRANSIENT_TRANSPORT = "TRANSIENT_TRANSPORT"
PERMANENT_AUTH_OR_PROTOCOL = "PERMANENT_AUTH_OR_PROTOCOL"
UNKNOWN = "UNKNOWN"

_NETWORK_ERRNOS = {errno.ECONNABORTED, errno.ECONNREFUSED, errno.ECONNRESET,
                   errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ETIMEDOUT,
                   errno.EPIPE}


def classify_provider_exception(exc: BaseException) -> str:
    """Only known transport failures receive a recovery grace period."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, error.HTTPError):
            return (TRANSIENT_TRANSPORT if current.code == 429 or 500 <= current.code < 600
                    else PERMANENT_AUTH_OR_PROTOCOL)
        if isinstance(current, ssl.SSLError):
            return PERMANENT_AUTH_OR_PROTOCOL
        if current.__class__.__name__ == "ReconciliationPayloadError" and isinstance(current.__cause__, error.HTTPError):
            current = current.__cause__
            continue
        if current.__class__.__name__ in {"ReconciliationAuthError", "ReconciliationPayloadError",
                                          "ReconciliationScopeError", "HistoricalDataError"}:
            return PERMANENT_AUTH_OR_PROTOCOL
        if isinstance(current, (socket.gaierror, TimeoutError, ConnectionError)):
            return TRANSIENT_TRANSPORT
        if isinstance(current, OSError) and current.errno in _NETWORK_ERRNOS:
            return TRANSIENT_TRANSPORT
        if isinstance(current, error.URLError):
            reason = current.reason
            if isinstance(reason, BaseException):
                current = reason
                continue
            return TRANSIENT_TRANSPORT
        current = current.__cause__ or current.__context__
    return UNKNOWN


def classify_reconciliation_snapshot(source: Mapping[str, Any]) -> str:
    declared = str(source.get("failure_class") or "").upper()
    if declared in {TRANSIENT_TRANSPORT, PERMANENT_AUTH_OR_PROTOCOL, UNKNOWN}:
        return declared
    attempts = source.get("request_attempts") or []
    if attempts and isinstance(attempts[-1], Mapping):
        last = attempts[-1]
        status = last.get("http_status")
        if isinstance(status, int):
            return (TRANSIENT_TRANSPORT if status == 429 or 500 <= status < 600
                    else PERMANENT_AUTH_OR_PROTOCOL)
        if last.get("transport_category") in {"TIMEOUT", "TRANSPORT", "URL"}:
            return TRANSIENT_TRANSPORT
        if last.get("transport_category") in {"TLS", "HTTP_PROTOCOL"}:
            return PERMANENT_AUTH_OR_PROTOCOL
    return UNKNOWN
