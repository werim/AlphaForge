from __future__ import annotations

import sqlite3
from pathlib import Path


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

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteReplayStore:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()
