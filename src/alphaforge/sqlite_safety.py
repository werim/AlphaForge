from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError


_T = TypeVar("_T")

SQLITE_BUSY_RETRY_ATTEMPTS = 4
SQLITE_BUSY_RETRY_BASE_SECONDS = 0.05
SQLITE_BUSY_PROBE_TIMEOUT_MS = 50

_SQLITE_WRITER_REGISTRY_LOCK = threading.Lock()
_SQLITE_WRITER_LOCKS: dict[str, threading.RLock] = {}


def _sqlite_writer_key(engine: Engine) -> str | None:
    """Return one process-local arbitration key per SQLite database file."""
    if engine.dialect.name != "sqlite":
        return None
    database = engine.url.database
    if not database or database == ":memory:":
        return f"sqlite-memory-engine:{id(engine)}"
    try:
        return f"sqlite-file:{Path(str(database)).expanduser().resolve()}"
    except (OSError, RuntimeError, ValueError):
        return f"sqlite-url:{engine.url.render_as_string(hide_password=True)}"


@contextmanager
def sqlite_writer_guard(engine: Engine) -> Iterator[None]:
    """Serialize known in-process SQLite writers for one canonical database.

    SQLite permits only one writer. AlphaForge has several legitimate writer
    loops (scanner/allocation, resolver, maintenance, heartbeat,
    reconciliation and qualification) sharing the same campaign database.
    Their own BUSY retry loops protect against external contention, but letting
    those in-process loops race each other can consume the retry budget without
    any external fault.

    This guard only arbitrates writers in this Python process. Cross-process
    contention is intentionally untouched and still reaches the bounded,
    fail-closed SQLITE_BUSY path.
    """
    key = _sqlite_writer_key(engine)
    if key is None:
        yield
        return
    with _SQLITE_WRITER_REGISTRY_LOCK:
        lock = _SQLITE_WRITER_LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield


class SQLiteBusyExhausted(RuntimeError):
    """Bounded transient SQLite writer contention exhausted its retry budget."""

    def __init__(
        self,
        *,
        operation_name: str,
        attempts: int,
        original: BaseException,
    ) -> None:
        super().__init__(
            f"SQLITE_BUSY {operation_name} after {attempts} attempts: {original}"
        )
        self.operation_name = operation_name
        self.attempts = attempts
        self.original = original


def is_sqlite_busy_error(exc: BaseException) -> bool:
    """Return True only for SQLite BUSY/LOCKED contention."""
    original = getattr(exc, "orig", exc)
    code = getattr(original, "sqlite_errorcode", None)
    if isinstance(code, int):
        primary = code & 0xFF
        if primary in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            return True
    message = str(original).lower()
    return (
        "database is locked" in message
        or "database table is locked" in message
        or "sqlite_busy" in message
        or "sqlite_locked" in message
    )


def is_sqlite_full_error(exc: BaseException) -> bool:
    """Classify physical capacity exhaustion separately from schema failures."""
    original = getattr(exc, "orig", exc)
    code = getattr(original, "sqlite_errorcode", None)
    if isinstance(code, int):
        return (code & 0xFF) == sqlite3.SQLITE_FULL
    return "database or disk is full" in str(original).lower()


def run_sqlite_write_with_retry(
    engine: Engine,
    operation: Callable[[Connection], _T],
    *,
    operation_name: str,
    attempts: int = SQLITE_BUSY_RETRY_ATTEMPTS,
    base_seconds: float = SQLITE_BUSY_RETRY_BASE_SECONDS,
    probe_timeout_ms: int = SQLITE_BUSY_PROBE_TIMEOUT_MS,
) -> _T:
    """Run one write transaction with bounded fresh-connection BUSY retries.

    Non-SQLite engines execute once with normal transaction semantics.
    Non-transient database errors are re-raised unchanged.

    On SQLite, every failed BUSY/LOCKED transaction is rolled back and its
    connection invalidated before the next attempt. The helper temporarily uses
    a short busy timeout so runtime control can enter an explicit fail-closed
    state instead of blocking behind the canonical 30s connection timeout.
    """
    total_attempts = max(1, int(attempts))
    with sqlite_writer_guard(engine):
        for attempt in range(total_attempts):
            conn = engine.connect()
            transaction = None
            old_timeout: int | None = None
            try:
                if engine.dialect.name == "sqlite":
                    old_timeout = int(
                        conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
                    )
                    conn.exec_driver_sql(
                        f"PRAGMA busy_timeout={max(0, int(probe_timeout_ms))}"
                    )
                    conn.commit()

                transaction = conn.begin()
                result = operation(conn)
                transaction.commit()
                return result
            except OperationalError as exc:
                if transaction is not None and transaction.is_active:
                    transaction.rollback()
                if engine.dialect.name != "sqlite" or not is_sqlite_busy_error(exc):
                    raise
                conn.invalidate()
                if attempt + 1 >= total_attempts:
                    raise SQLiteBusyExhausted(
                        operation_name=operation_name,
                        attempts=total_attempts,
                        original=exc,
                    ) from exc
                time.sleep(max(0.0, float(base_seconds)) * (2 ** attempt))
            except BaseException:
                if transaction is not None and transaction.is_active:
                    transaction.rollback()
                raise
            finally:
                if (
                    old_timeout is not None
                    and not conn.invalidated
                    and engine.dialect.name == "sqlite"
                ):
                    try:
                        conn.exec_driver_sql(f"PRAGMA busy_timeout={old_timeout}")
                        conn.commit()
                    except Exception:
                        conn.invalidate()
                conn.close()

    raise AssertionError("unreachable sqlite retry state")
