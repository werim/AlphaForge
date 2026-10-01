from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from alphaforge.sqlite_safety import run_sqlite_write_with_retry


DDL = """
CREATE TABLE IF NOT EXISTS execution_account_leases (
    account_scope TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    owner_instance_id TEXT NOT NULL,
    owner_startup_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    lease_expires_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class ExecutionOwnership:
    account_scope: str
    mode: str
    owner_instance_id: str
    owner_startup_id: str
    fencing_token: int | None
    lease_expires_at: float | None
    acquired: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def ensure_execution_ownership_schema(engine: Engine) -> None:
    run_sqlite_write_with_retry(
        engine,
        lambda conn: conn.execute(text(DDL)),
        operation_name="execution_ownership_schema",
    )


def _validate_inputs(
    *,
    account_scope: str,
    owner_instance_id: str,
    owner_startup_id: str,
    lease_ttl_sec: float,
) -> tuple[str, float]:
    scope = str(account_scope or "").strip()
    if not scope:
        raise ValueError("execution account scope is required")
    if not str(owner_instance_id or "").strip():
        raise ValueError("owner_instance_id is required")
    if not str(owner_startup_id or "").strip():
        raise ValueError("owner_startup_id is required")
    ttl = float(lease_ttl_sec)
    if not math.isfinite(ttl) or ttl <= 0.0:
        raise ValueError("lease_ttl_sec must be finite and > 0")
    return scope, ttl


def acquire_execution_ownership(
    engine: Engine,
    *,
    account_scope: str,
    mode: str,
    owner_instance_id: str,
    owner_startup_id: str,
    lease_ttl_sec: float,
    now: float | None = None,
) -> ExecutionOwnership:
    """Acquire/renew the durable single-owner lease for one account scope.

    A different owner may take over only after expiry. Takeover increments the
    fencing token; renewal by the same instance/startup preserves it.
    """
    scope, ttl = _validate_inputs(
        account_scope=account_scope,
        owner_instance_id=owner_instance_id,
        owner_startup_id=owner_startup_id,
        lease_ttl_sec=lease_ttl_sec,
    )
    ts = float(time.time() if now is None else now)
    expires = ts + ttl
    ensure_execution_ownership_schema(engine)

    def _persist(conn):
        inserted = conn.execute(
            text(
                """
                INSERT INTO execution_account_leases (
                    account_scope, mode, owner_instance_id, owner_startup_id,
                    fencing_token, lease_expires_at, updated_at
                ) VALUES (
                    :scope, :mode, :instance, :startup, 1, :expires, :now
                )
                ON CONFLICT(account_scope) DO NOTHING
                """
            ),
            {
                "scope": scope,
                "mode": str(mode or "").upper(),
                "instance": owner_instance_id,
                "startup": owner_startup_id,
                "expires": expires,
                "now": ts,
            },
        )
        if int(inserted.rowcount or 0) != 1:
            conn.execute(
                text(
                    """
                    UPDATE execution_account_leases
                    SET
                        mode = :mode,
                        owner_instance_id = :instance,
                        owner_startup_id = :startup,
                        fencing_token = CASE
                            WHEN owner_instance_id = :instance
                             AND owner_startup_id = :startup
                            THEN fencing_token
                            ELSE fencing_token + 1
                        END,
                        lease_expires_at = :expires,
                        updated_at = :now
                    WHERE account_scope = :scope
                      AND (
                            (owner_instance_id = :instance
                             AND owner_startup_id = :startup)
                            OR lease_expires_at <= :now
                      )
                    """
                ),
                {
                    "scope": scope,
                    "mode": str(mode or "").upper(),
                    "instance": owner_instance_id,
                    "startup": owner_startup_id,
                    "expires": expires,
                    "now": ts,
                },
            )

        return conn.execute(
            text(
                """
                SELECT account_scope, mode, owner_instance_id, owner_startup_id,
                       fencing_token, lease_expires_at
                FROM execution_account_leases
                WHERE account_scope = :scope
                """
            ),
            {"scope": scope},
        ).mappings().one()

    row = run_sqlite_write_with_retry(
        engine,
        _persist,
        operation_name="execution_ownership_acquire",
    )

    acquired = (
        str(row["owner_instance_id"]) == str(owner_instance_id)
        and str(row["owner_startup_id"]) == str(owner_startup_id)
        and float(row["lease_expires_at"]) > ts
    )
    return ExecutionOwnership(
        account_scope=str(row["account_scope"]),
        mode=str(row["mode"]),
        owner_instance_id=str(row["owner_instance_id"]),
        owner_startup_id=str(row["owner_startup_id"]),
        fencing_token=int(row["fencing_token"]),
        lease_expires_at=float(row["lease_expires_at"]),
        acquired=acquired,
        reason="" if acquired else "EXECUTION_ACCOUNT_OWNED_BY_ANOTHER_RUNTIME",
    )


def validate_execution_ownership(
    engine: Engine,
    *,
    account_scope: str,
    owner_instance_id: str,
    owner_startup_id: str,
    fencing_token: int | None,
    min_validity_sec: float = 0.0,
    now: float | None = None,
) -> ExecutionOwnership:
    scope = str(account_scope or "").strip()
    ts = float(time.time() if now is None else now)
    min_validity = float(min_validity_sec)
    if not math.isfinite(min_validity) or min_validity < 0.0:
        raise ValueError("min_validity_sec must be finite and >= 0")
    ensure_execution_ownership_schema(engine)

    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT account_scope, mode, owner_instance_id, owner_startup_id,
                       fencing_token, lease_expires_at
                FROM execution_account_leases
                WHERE account_scope = :scope
                """
            ),
            {"scope": scope},
        ).mappings().one_or_none()

    if row is None:
        return ExecutionOwnership(
            account_scope=scope,
            mode="",
            owner_instance_id="",
            owner_startup_id="",
            fencing_token=None,
            lease_expires_at=None,
            acquired=False,
            reason="EXECUTION_OWNERSHIP_MISSING",
        )

    reason = ""
    if str(row["owner_instance_id"]) != str(owner_instance_id):
        reason = "EXECUTION_OWNER_INSTANCE_MISMATCH"
    elif str(row["owner_startup_id"]) != str(owner_startup_id):
        reason = "EXECUTION_OWNER_STARTUP_MISMATCH"
    elif fencing_token is None or int(row["fencing_token"]) != int(fencing_token):
        reason = "EXECUTION_FENCING_TOKEN_MISMATCH"
    elif float(row["lease_expires_at"]) <= ts + min_validity:
        reason = "EXECUTION_OWNERSHIP_LEASE_TOO_CLOSE_TO_EXPIRY"

    return ExecutionOwnership(
        account_scope=str(row["account_scope"]),
        mode=str(row["mode"]),
        owner_instance_id=str(row["owner_instance_id"]),
        owner_startup_id=str(row["owner_startup_id"]),
        fencing_token=int(row["fencing_token"]),
        lease_expires_at=float(row["lease_expires_at"]),
        acquired=not reason,
        reason=reason,
    )


def release_execution_ownership(
    engine: Engine,
    *,
    account_scope: str,
    owner_instance_id: str,
    owner_startup_id: str,
    fencing_token: int | None,
    now: float | None = None,
) -> bool:
    if fencing_token is None:
        return False
    ts = float(time.time() if now is None else now)
    ensure_execution_ownership_schema(engine)

    def _release(conn):
        return conn.execute(
            text(
                """
                UPDATE execution_account_leases
                SET lease_expires_at = :now, updated_at = :now
                WHERE account_scope = :scope
                  AND owner_instance_id = :instance
                  AND owner_startup_id = :startup
                  AND fencing_token = :token
                """
            ),
            {
                "scope": str(account_scope or "").strip(),
                "instance": owner_instance_id,
                "startup": owner_startup_id,
                "token": int(fencing_token),
                "now": ts,
            },
        ).rowcount

    rowcount = run_sqlite_write_with_retry(
        engine,
        _release,
        operation_name="execution_ownership_release",
    )
    return int(rowcount or 0) == 1
