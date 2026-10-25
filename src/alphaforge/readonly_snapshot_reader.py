"""Owned, single-flight transport boundary for synchronous read-only providers.

Cancellation abandons a result, never the underlying read. A daemon thread keeps
an unresponsive transport from making event-loop/process shutdown unbounded;
callers must retain recovery/containment until close reports quiescence.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
import contextvars
import threading
from typing import Any


class ReadonlySnapshotReader:
    def __init__(self) -> None:
        self._pending: Future[tuple[bool, Any]] | None = None
        self._waiting = False
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def has_unaccepted_read(self) -> bool:
        return self._pending is not None

    async def read(self, provider: Any) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("reconciliation_reader_closed")
        if self._waiting or (self._pending is not None and not self._pending.done()):
            raise RuntimeError("reconciliation_read_in_flight")
        # Any completed abandoned read is discarded, including COMPLETE results.
        self._pending = future = Future()
        context = contextvars.copy_context()

        def fetch() -> None:
            try:
                result = (True, context.run(lambda: dict(provider.snapshot())))
            except BaseException as exc:
                result = (False, exc if isinstance(exc, Exception) else RuntimeError(type(exc).__name__))
            future.set_result(result)

        self._waiting = True
        try:
            try:
                threading.Thread(target=fetch, name="readonly-reconciliation", daemon=True).start()
            except Exception:
                self._pending = None
                raise
            ok, value = await asyncio.shield(asyncio.wrap_future(future))
        except asyncio.CancelledError:
            # The transport still owns its lifetime. No subsequent read overlaps it.
            raise
        else:
            self._pending = None
            if self._closed:
                raise RuntimeError("reconciliation_reader_closed")
            if not ok:
                raise value
            return value
        finally:
            self._waiting = False

    async def close(self, timeout: float) -> bool:
        self._closed = True
        pending = self._pending
        if pending is None:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(pending)), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        self._pending = None
        return True
