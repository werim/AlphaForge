"""Bounded metadata only; never exception messages, locals, URLs or credentials."""
from __future__ import annotations

import sys
from contextvars import ContextVar

READ_OPERATION: ContextVar[str | None] = ContextVar("reconciliation_read_operation", default=None)

_EXCEPTION_TYPES = frozenset({"TimeoutError", "CancelledError", "OSError", "ConnectionError", "ConnectionResetError", "ConnectionRefusedError", "SSLError", "HTTPError", "URLError", "JSONDecodeError", "RuntimeError", "ValueError"})
_MODULES = frozenset({"ssl", "socket", "http.client", "threading", "alphaforge.binance_reconciliation_provider", "alphaforge.readonly_snapshot_reader"})
_FUNCTIONS = frozenset({"snapshot", "_http_operation", "_signed_get", "get_json", "getresponse", "_read_status", "read", "readinto", "recv_into", "getaddrinfo", "connect", "do_handshake", "wait", "fetch"})


def exception_types(exc: BaseException) -> list[str]:
    result = []
    seen = set()
    try:
        while exc is not None and len(result) < 4 and id(exc) not in seen:
            seen.add(id(exc))
            name = type.__dict__["__name__"].__get__(type(exc), type)
            result.append(name if name in _EXCEPTION_TYPES else "OTHER")
            # Read base storage directly: generic __getattribute__ still dispatches
            # subclass descriptors, and truthiness may execute __bool__.
            cause = BaseException.__cause__.__get__(exc)
            suppressed = BaseException.__suppress_context__.__get__(exc)
            exc = cause if cause is not None else (None if suppressed else BaseException.__context__.__get__(exc))
        return result
    except Exception:
        return ["UNAVAILABLE"]


def worker_stack(thread_id: int | None) -> list[dict]:
    frame = sys._current_frames().get(thread_id)
    result = []
    while frame is not None and len(result) < 8:
        module = frame.f_globals.get("__name__")
        function = frame.f_code.co_name
        result.append({"module": module if module in _MODULES else "OTHER", "function": function if function in _FUNCTIONS else "OTHER", "line": frame.f_lineno})
        frame = frame.f_back
    return result
