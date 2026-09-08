from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


MAX_MESSAGE_ID_CHARS = 128


class SQLiteReplayStore:
    def __init__(self, db_path: str | Path) -> None:
        self._connection = sqlite3.connect(Path(db_path))
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS replay_message_ids (
                message_id TEXT PRIMARY KEY,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS remote_control_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transport TEXT NOT NULL,
                update_id TEXT,
                authorized_identity TEXT,
                normalized_command TEXT,
                result TEXT NOT NULL,
                rejection_reason TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._connection.commit()

    def claim_message_id(self, message_id: str) -> bool:
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValueError("message_id must be a non-empty string")
        message_id = message_id.strip()
        if len(message_id) > MAX_MESSAGE_ID_CHARS:
            raise ValueError("message_id is too long")

        try:
            self._connection.execute(
                "INSERT INTO replay_message_ids (message_id) VALUES (?)",
                (message_id,),
            )
            self._connection.commit()
        except sqlite3.IntegrityError:
            self._connection.rollback()
            return False
        return True

    def reserve(self, message_id: str) -> bool:
        return self.claim_message_id(message_id)

    def record_remote_control_audit(
        self,
        *,
        transport: str,
        update_id: str | None,
        authorized_identity: str | None,
        normalized_command: str | None,
        result: str,
        rejection_reason: str | None,
    ) -> None:
        if not isinstance(transport, str) or not transport.strip():
            raise ValueError("transport must be a non-empty string")
        if result not in {"ACCEPTED", "REJECTED"}:
            raise ValueError("remote control audit result must be ACCEPTED or REJECTED")
        self._connection.execute(
            """
            INSERT INTO remote_control_audit (
                transport,
                update_id,
                authorized_identity,
                normalized_command,
                result,
                rejection_reason
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                transport.strip(),
                _strip_optional(update_id),
                _strip_optional(authorized_identity),
                _strip_optional(normalized_command),
                result,
                _strip_optional(rejection_reason),
            ),
        )
        self._connection.commit()

    def list_remote_control_audit_rows(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """
            SELECT
                transport,
                update_id,
                authorized_identity,
                normalized_command,
                result,
                rejection_reason,
                created_at
            FROM remote_control_audit
            ORDER BY id
            """
        ).fetchall()
        columns = (
            "transport",
            "update_id",
            "authorized_identity",
            "normalized_command",
            "result",
            "rejection_reason",
            "created_at",
        )
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteReplayStore:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()


def _strip_optional(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("remote control audit value must be a string")
    stripped = value.strip()
    return stripped or None
