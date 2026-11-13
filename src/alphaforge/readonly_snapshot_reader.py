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
import time
import uuid

from alphaforge.reconciliation_observation import READ_OPERATION, exception_types, worker_stack
from alphaforge.reconciliation_read_budget import remaining_read_budget
from typing import Any


class ReadonlySnapshotReader:
    def __init__(self) -> None:
        self._pending: Future[tuple[bool, Any]] | None = None
        self._waiting = False
        self._closed = False
        self._observation: dict[str, Any] = {}
        self._thread: threading.Thread | None = None
        self._provider: Any = None

    def diagnostics(self) -> dict[str, Any]:
        from alphaforge.binance_reconciliation_provider import BinanceReadonlyReconciliationProvider
        try:
            observation = dict(self._observation)
            thread = self._thread
            observation.update(worker_alive=bool(thread and thread.is_alive()),
                result_unaccepted=self.has_unaccepted_read, reader_closed=self.closed)
            if observation:
                observation["elapsed_sec"] = max(0.0, time.monotonic() - observation["started_monotonic"])
            if thread and thread.is_alive():
                observation["worker_stack"] = worker_stack(thread.ident)
            # No arbitrary provider hook is invoked on the timeout/event-loop path.
            if type(self._provider) is BinanceReadonlyReconciliationProvider:
                provider_observation = BinanceReadonlyReconciliationProvider.read_diagnostics(self._provider)
                observation["provider"] = provider_observation if provider_observation.get("operation_id") == observation.get("operation_id") else {"state": "NOT_STARTED_THIS_OPERATION"}
            return observation
        except Exception:
            return {"observation_status": "UNAVAILABLE"}

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
        self._provider = provider
        self._observation = {"operation_id": uuid.uuid4().hex, "started_monotonic": time.monotonic(), "state": "STARTING", "exception_types": []}

        def fetch() -> None:
            self._observation = {**self._observation, "state": "RUNNING"}
            try:
                def snapshot():
                    READ_OPERATION.set(self._observation["operation_id"])
                    return dict(provider.snapshot())
                result = (True, context.run(snapshot))
            except BaseException as exc:
                result = (False, exc if isinstance(exc, Exception) else RuntimeError(type(exc).__name__))
            try:
                self._observation = {**self._observation, "state": "COMPLETE" if result[0] else "FAILED", "exception_types": [] if result[0] else exception_types(result[1]), "completed_monotonic": time.monotonic()}
            except Exception:
                self._observation = {**self._observation, "state": "OBSERVATION_UNAVAILABLE"}
            finally:
                future.set_result(result)

        self._waiting = True
        try:
            try:
                self._thread = threading.Thread(target=fetch, name="readonly-reconciliation", daemon=True)
                self._thread.start()
            except Exception:
                self._observation = {**self._observation, "state": "START_FAILED"}
                self._pending = None
                raise
            ok, value = await asyncio.shield(asyncio.wrap_future(future))
        except asyncio.CancelledError:
            # The transport still owns its lifetime. No subsequent read overlaps it.
            raise
        else:
            # The waiter can resume after its deadline before asyncio delivers cancellation.
            # Keep an expired result unaccepted for the timeout evidence and shutdown drain.
            remaining_read_budget()
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
