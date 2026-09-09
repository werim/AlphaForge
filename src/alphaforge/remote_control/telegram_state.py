from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


MAX_TELEGRAM_ID_CHARS = 64
MAX_TELEGRAM_RESPONSE_CHARS = 3900
_OFFSET_KEY = "next_offset"
_ALLOWED_FAILURE_REASONS = {None, "SEND_MESSAGE_FAILED", "DELIVERY_ACK_FAILED"}


@dataclass(frozen=True, slots=True)
class StoredTelegramResponseDelivery:
    update_id: str
    chat_id: str
    text: str
    failure_reason: str | None
    retry_count: int


class SQLiteTelegramTransportStateStore:
    """Dedicated Telegram transport state at an explicit caller-supplied path."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        before_commit: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(db_path, (str, Path)) or not str(db_path):
            raise ValueError("Telegram transport state path is required")
        self._connection = sqlite3.connect(Path(db_path))
        self._before_commit = before_commit
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_transport_state (
                state_key TEXT PRIMARY KEY,
                state_value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_pending_deliveries (
                update_id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                response_text TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status = 'PENDING'),
                failure_reason TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._connection.commit()

    def load_offset(self) -> int | None:
        row = self._connection.execute(
            "SELECT state_value FROM telegram_transport_state WHERE state_key = ?",
            (_OFFSET_KEY,),
        ).fetchone()
        if row is None:
            return None
        return _validate_stored_offset(row[0])

    def acknowledge_offset(self, next_offset: int) -> int:
        next_offset = _validate_offset(next_offset)
        self._begin()
        try:
            durable_offset = self._write_monotonic_offset(next_offset)
            self._commit("ACKNOWLEDGE_OFFSET")
        except Exception:
            self._connection.rollback()
            raise
        return durable_offset

    def persist_pending_with_offset(
        self,
        *,
        update_id: str,
        chat_id: str,
        text: str,
        next_offset: int,
    ) -> int:
        update_id = _validate_normalized_id(update_id, "update_id")
        chat_id = _validate_normalized_id(chat_id, "chat_id")
        text = _validate_response_text(text)
        next_offset = _validate_offset(next_offset)
        self._begin()
        try:
            self._connection.execute(
                """
                INSERT INTO telegram_pending_deliveries (
                    update_id, chat_id, response_text, status, failure_reason, retry_count
                ) VALUES (?, ?, ?, 'PENDING', NULL, 0)
                ON CONFLICT(update_id) DO UPDATE SET
                    chat_id = excluded.chat_id,
                    response_text = excluded.response_text,
                    status = 'PENDING',
                    updated_at = CURRENT_TIMESTAMP
                """,
                (update_id, chat_id, text),
            )
            durable_offset = self._write_monotonic_offset(next_offset)
            self._commit("PERSIST_PENDING_WITH_OFFSET")
        except Exception:
            self._connection.rollback()
            raise
        return durable_offset

    def load_pending(self) -> tuple[StoredTelegramResponseDelivery, ...]:
        rows = self._connection.execute(
            """
            SELECT update_id, chat_id, response_text, failure_reason, retry_count
            FROM telegram_pending_deliveries
            WHERE status = 'PENDING'
            ORDER BY created_at, update_id
            """
        ).fetchall()
        return tuple(_validate_pending_row(row) for row in rows)

    def record_delivery_failure(self, update_id: str) -> None:
        update_id = _validate_normalized_id(update_id, "update_id")
        self._begin()
        try:
            cursor = self._connection.execute(
                """
                UPDATE telegram_pending_deliveries
                SET failure_reason = 'SEND_MESSAGE_FAILED',
                    retry_count = retry_count + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE update_id = ? AND status = 'PENDING'
                """,
                (update_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("pending Telegram delivery not found")
            self._commit("RECORD_DELIVERY_FAILURE")
        except Exception:
            self._connection.rollback()
            raise

    def mark_delivered(self, update_id: str) -> None:
        update_id = _validate_normalized_id(update_id, "update_id")
        self._begin()
        try:
            cursor = self._connection.execute(
                "DELETE FROM telegram_pending_deliveries WHERE update_id = ? AND status = 'PENDING'",
                (update_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("pending Telegram delivery not found")
            self._commit("MARK_DELIVERED")
        except Exception:
            self._connection.rollback()
            raise

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteTelegramTransportStateStore:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def _begin(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    def _commit(self, operation: str) -> None:
        if self._before_commit is not None:
            self._before_commit(operation)
        self._connection.commit()

    def _write_monotonic_offset(self, next_offset: int) -> int:
        current = self.load_offset()
        durable_offset = next_offset if current is None else max(current, next_offset)
        self._connection.execute(
            """
            INSERT INTO telegram_transport_state (state_key, state_value)
            VALUES (?, ?)
            ON CONFLICT(state_key) DO UPDATE SET
                state_value = excluded.state_value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (_OFFSET_KEY, str(durable_offset)),
        )
        return durable_offset


def _validate_offset(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Telegram next_offset must be an integer")
    return value


def _validate_stored_offset(value: object) -> int:
    if not isinstance(value, str) or not value or len(value) > MAX_TELEGRAM_ID_CHARS + 1:
        raise ValueError("invalid persisted Telegram offset")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError("invalid persisted Telegram offset") from exc
    if str(parsed) != value:
        raise ValueError("invalid persisted Telegram offset")
    return parsed


def _validate_normalized_id(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_TELEGRAM_ID_CHARS:
        raise ValueError(f"invalid persisted Telegram {name}")
    if not value.lstrip("-").isdigit() or str(int(value)) != value:
        raise ValueError(f"invalid persisted Telegram {name}")
    return value


def _validate_response_text(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_TELEGRAM_RESPONSE_CHARS:
        raise ValueError("invalid persisted Telegram response text")
    if "\r" in value or "\n" in value:
        raise ValueError("invalid persisted Telegram response text")
    return value


def _validate_pending_row(row: tuple[object, ...]) -> StoredTelegramResponseDelivery:
    if len(row) != 5:
        raise ValueError("invalid persisted Telegram delivery")
    update_id = _validate_normalized_id(row[0], "update_id")
    chat_id = _validate_normalized_id(row[1], "chat_id")
    text = _validate_response_text(row[2])
    failure_reason = row[3]
    if failure_reason not in _ALLOWED_FAILURE_REASONS:
        raise ValueError("invalid persisted Telegram failure reason")
    retry_count = row[4]
    if isinstance(retry_count, bool) or not isinstance(retry_count, int) or retry_count < 0:
        raise ValueError("invalid persisted Telegram retry count")
    return StoredTelegramResponseDelivery(
        update_id=update_id,
        chat_id=chat_id,
        text=text,
        failure_reason=failure_reason,
        retry_count=retry_count,
    )
