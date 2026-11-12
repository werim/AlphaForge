"""Carry the runtime reconciliation deadline into the synchronous reader."""
from contextlib import contextmanager
from contextvars import ContextVar
import time
from collections.abc import Iterator


_deadline: ContextVar[float | None] = ContextVar("reconciliation_read_deadline", default=None)


class ReconciliationReadBudgetExpired(RuntimeError):
    """The application budget ran out before a provider result was available."""


@contextmanager
def reconciliation_read_deadline(deadline: float) -> Iterator[None]:
    token = _deadline.set(deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining_read_budget() -> float:
    deadline = _deadline.get()
    if deadline is None:
        return float("inf")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ReconciliationReadBudgetExpired("reconciliation_read_budget_expired")
    return remaining
