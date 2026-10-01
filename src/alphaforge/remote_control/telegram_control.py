from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from alphaforge.remote_control.commands import RemoteControlResult


CONFIRMABLE_OPERATIONS = frozenset({"PAUSE", "RESUME", "RECOVERY"})
_CONFIRMATION_ID = re.compile(r"^[A-Za-z0-9_-]{16,96}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class TelegramControlError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TelegramConfirmation:
    confirmation_id: str
    operation: str
    campaign_id: str
    run_id: str
    expires_at: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_token(value: object, *, code: str) -> str:
    text = str(value or "").strip()
    if not _TOKEN.fullmatch(text):
        raise TelegramControlError(code)
    return text


def _confirmation_id(value: object) -> str:
    text = str(value or "").strip()
    if not _CONFIRMATION_ID.fullmatch(text):
        raise TelegramControlError("INVALID_CONFIRMATION_ID")
    return text


class SQLiteTelegramControlStore:
    """Isolated confirmation/notification state. Never points at the campaign DB."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        path = Path(db_path).expanduser()
        if not str(path):
            raise ValueError("Telegram control state path is required")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, timeout=30.0)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._clock = clock
        self._id_factory = id_factory or (lambda: secrets.token_urlsafe(24))
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS telegram_confirmations (
                confirmation_id TEXT PRIMARY KEY,
                operation TEXT NOT NULL CHECK(operation IN ('PAUSE','RESUME','RECOVERY')),
                campaign_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('PENDING','CONSUMED','CANCELLED','EXPIRED')),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                result_code TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_telegram_confirmations_status_expires
                ON telegram_confirmations(status, expires_at);
            CREATE TABLE IF NOT EXISTS telegram_notification_state (
                notification_key TEXT PRIMARY KEY,
                last_digest TEXT,
                last_sent_at TEXT NOT NULL
            );
            """
        )
        self._connection.commit()

    def create_confirmation(
        self,
        *,
        operation: str,
        campaign_id: str,
        run_id: str,
        ttl_seconds: float = 120.0,
    ) -> TelegramConfirmation:
        operation = str(operation or "").upper()
        if operation not in CONFIRMABLE_OPERATIONS:
            raise TelegramControlError("UNSUPPORTED_OPERATION")
        campaign_id = _safe_token(campaign_id, code="INVALID_CAMPAIGN_ID")
        run_id = _safe_token(run_id, code="INVALID_RUN_ID")
        ttl = min(max(float(ttl_seconds), 15.0), 600.0)
        now = self._clock().astimezone(timezone.utc)
        expires = now + timedelta(seconds=ttl)
        confirmation_id = _confirmation_id(self._id_factory())
        try:
            self._connection.execute(
                """
                INSERT INTO telegram_confirmations(
                    confirmation_id,operation,campaign_id,run_id,status,created_at,expires_at
                ) VALUES(?,?,?,?, 'PENDING', ?, ?)
                """,
                (confirmation_id, operation, campaign_id, run_id, _iso(now), _iso(expires)),
            )
            self._connection.commit()
        except sqlite3.IntegrityError as exc:
            self._connection.rollback()
            raise TelegramControlError("CONFIRMATION_ID_COLLISION") from exc
        return TelegramConfirmation(confirmation_id, operation, campaign_id, run_id, _iso(expires))

    def consume_confirmation(
        self,
        confirmation_id: str,
        *,
        campaign_id: str,
        run_id: str,
    ) -> TelegramConfirmation:
        confirmation_id = _confirmation_id(confirmation_id)
        campaign_id = _safe_token(campaign_id, code="INVALID_CAMPAIGN_ID")
        run_id = _safe_token(run_id, code="INVALID_RUN_ID")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT * FROM telegram_confirmations WHERE confirmation_id=?",
                (confirmation_id,),
            ).fetchone()
            if row is None:
                raise TelegramControlError("CONFIRMATION_NOT_FOUND")
            if row["status"] != "PENDING":
                raise TelegramControlError("CONFIRMATION_NOT_PENDING")
            now = self._clock().astimezone(timezone.utc)
            if _parse_time(str(row["expires_at"])) <= now:
                self._connection.execute(
                    "UPDATE telegram_confirmations SET status='EXPIRED' WHERE confirmation_id=?",
                    (confirmation_id,),
                )
                self._connection.commit()
                raise TelegramControlError("CONFIRMATION_EXPIRED")
            if row["campaign_id"] != campaign_id or row["run_id"] != run_id:
                self._connection.execute(
                    "UPDATE telegram_confirmations SET status='CANCELLED', result_code='IDENTITY_MISMATCH' "
                    "WHERE confirmation_id=?",
                    (confirmation_id,),
                )
                self._connection.commit()
                raise TelegramControlError("CONFIRMATION_IDENTITY_MISMATCH")
            self._connection.execute(
                "UPDATE telegram_confirmations SET status='CONSUMED', consumed_at=? WHERE confirmation_id=?",
                (_iso(now), confirmation_id),
            )
            self._connection.commit()
            return TelegramConfirmation(
                confirmation_id=str(row["confirmation_id"]),
                operation=str(row["operation"]),
                campaign_id=str(row["campaign_id"]),
                run_id=str(row["run_id"]),
                expires_at=str(row["expires_at"]),
            )
        except TelegramControlError:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        except Exception:
            self._connection.rollback()
            raise

    def cancel_confirmation(
        self,
        confirmation_id: str,
        *,
        campaign_id: str,
        run_id: str,
    ) -> str:
        confirmation_id = _confirmation_id(confirmation_id)
        campaign_id = _safe_token(campaign_id, code="INVALID_CAMPAIGN_ID")
        run_id = _safe_token(run_id, code="INVALID_RUN_ID")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT * FROM telegram_confirmations WHERE confirmation_id=?",
                (confirmation_id,),
            ).fetchone()
            if row is None:
                raise TelegramControlError("CONFIRMATION_NOT_FOUND")
            if row["campaign_id"] != campaign_id or row["run_id"] != run_id:
                raise TelegramControlError("CONFIRMATION_IDENTITY_MISMATCH")
            if row["status"] != "PENDING":
                raise TelegramControlError("CONFIRMATION_NOT_PENDING")
            self._connection.execute(
                "UPDATE telegram_confirmations SET status='CANCELLED', result_code='OPERATOR_CANCELLED' "
                "WHERE confirmation_id=?",
                (confirmation_id,),
            )
            self._connection.commit()
            return str(row["operation"])
        except TelegramControlError:
            self._connection.rollback()
            raise
        except Exception:
            self._connection.rollback()
            raise

    def record_confirmation_result(self, confirmation_id: str, result_code: str) -> None:
        confirmation_id = _confirmation_id(confirmation_id)
        result_code = _safe_token(result_code, code="INVALID_RESULT_CODE")
        cursor = self._connection.execute(
            "UPDATE telegram_confirmations SET result_code=? "
            "WHERE confirmation_id=? AND status='CONSUMED'",
            (result_code, confirmation_id),
        )
        if cursor.rowcount != 1:
            self._connection.rollback()
            raise TelegramControlError("CONFIRMATION_RESULT_NOT_RECORDABLE")
        self._connection.commit()

    def notification_state(self, notification_key: str) -> tuple[str | None, datetime | None]:
        key = _safe_token(notification_key, code="INVALID_NOTIFICATION_KEY")
        row = self._connection.execute(
            "SELECT last_digest,last_sent_at FROM telegram_notification_state WHERE notification_key=?",
            (key,),
        ).fetchone()
        if row is None:
            return None, None
        return (str(row["last_digest"]) if row["last_digest"] else None, _parse_time(str(row["last_sent_at"])))

    def record_notification(self, notification_key: str, payload: str) -> str:
        key = _safe_token(notification_key, code="INVALID_NOTIFICATION_KEY")
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        now = _iso(self._clock().astimezone(timezone.utc))
        self._connection.execute(
            """
            INSERT INTO telegram_notification_state(notification_key,last_digest,last_sent_at)
            VALUES(?,?,?)
            ON CONFLICT(notification_key) DO UPDATE SET
                last_digest=excluded.last_digest,
                last_sent_at=excluded.last_sent_at
            """,
            (key, digest, now),
        )
        self._connection.commit()
        return digest

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SQLiteTelegramControlStore":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()


def execute_paper_action(
    operation: str,
    *,
    config: Mapping[str, str],
    timeout: float = 30.0,
    max_output_chars: int = 3900,
) -> RemoteControlResult:
    """Execute one confirmed PAPER action through existing canonical authorities."""
    operation = str(operation or "").upper()
    if operation not in CONFIRMABLE_OPERATIONS:
        return RemoteControlResult(operation or "UNKNOWN", 1, "", "UNSUPPORTED_OPERATION")
    db_path = str(config.get("remote_control_db_path") or "").strip()
    campaign_id = str(config.get("remote_control_campaign_id") or "").strip()
    run_id = str(config.get("remote_control_run_id") or "").strip()
    try:
        _safe_token(campaign_id, code="INVALID_CAMPAIGN_ID")
        _safe_token(run_id, code="INVALID_RUN_ID")
        if not db_path:
            raise TelegramControlError("TRUSTED_CONFIG_UNAVAILABLE")

        from alphaforge.dashboard.control_center import ControlCenterService, ControlError

        service = ControlCenterService.from_environment()
        configured_db = Path(db_path).expanduser().resolve()
        if service.db_path.resolve() != configured_db:
            raise TelegramControlError("TRUSTED_DATABASE_MISMATCH")
        service.validate(controls=True)
        if service.execution_mode() != "PAPER":
            raise TelegramControlError("PAPER_ONLY")
        before = service.status(campaign_id)
        active_run = str((before.get("campaign") or {}).get("active_run_id") or "")
        if active_run != run_id:
            raise TelegramControlError("STALE_TRUSTED_RUN_ID")

        if operation in {"PAUSE", "RESUME"}:
            try:
                outcome = service.control(campaign_id, operation.lower(), service.token)
            except ControlError as exc:
                return RemoteControlResult(operation, 1, "", str(exc.code)[:max_output_chars])
            op = outcome.get("operation") or {}
            verified = str(op.get("verified_campaign_status") or "UNKNOWN")
            return RemoteControlResult(operation, 0, f"{operation} verified_status={verified}"[:max_output_chars], "")

        if not before.get("recovery_required"):
            raise TelegramControlError("RECOVERY_NOT_REQUIRED")
        cmd = [
            str(service.python),
            "-m",
            "alphaforge.burnin_ops",
            "--db",
            str(service.db_path),
            "--json",
            "recovery-drill",
            "--campaign-id",
            campaign_id,
        ]
        completed = subprocess.run(
            cmd,
            cwd=service.project_root,
            capture_output=True,
            text=True,
            timeout=min(max(float(timeout), 1.0), 120.0),
            shell=False,
            check=False,
        )
        if completed.returncode != 0:
            return RemoteControlResult(operation, 1, "", "RECOVERY_COMMAND_FAILED")
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError:
            return RemoteControlResult(operation, 1, "", "RECOVERY_RESULT_MALFORMED")
        if not isinstance(payload, dict) or payload.get("status") != "PASS":
            return RemoteControlResult(operation, 1, "", "RECOVERY_NOT_VERIFIED")
        after = service.status(campaign_id)
        after_status = str((after.get("campaign") or {}).get("campaign_status") or "UNKNOWN")
        return RemoteControlResult(operation, 0, f"RECOVERY verified_status={after_status}"[:max_output_chars], "")
    except TelegramControlError as exc:
        return RemoteControlResult(operation, 1, "", str(exc)[:max_output_chars])
    except subprocess.TimeoutExpired:
        return RemoteControlResult(operation, 1, "", "RECOVERY_COMMAND_TIMEOUT")
    except Exception:
        return RemoteControlResult(operation, 1, "", "CONTROL_ACTION_FAILED_SAFELY")
