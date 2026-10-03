from __future__ import annotations

import json
import sqlite3
from typing import Any

from sqlalchemy import text

from alphaforge.portfolio_risk import PortfolioAllocationDecision


PORTFOLIO_ALLOCATION_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS portfolio_allocation_cycles (
        allocation_cycle_id TEXT PRIMARY KEY,
        timestamp TEXT NOT NULL,
        mode TEXT NOT NULL,
        action TEXT NOT NULL,
        candidate_set_hash TEXT NOT NULL,
        portfolio_snapshot_hash TEXT NOT NULL,
        config_hash TEXT NOT NULL,
        universe_hash TEXT NOT NULL,
        evidence_hash TEXT NOT NULL,
        git_sha TEXT NOT NULL,
        release_id TEXT NOT NULL,
        runtime_identity TEXT NOT NULL,
        allocator_version TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS portfolio_allocation_candidates (
        allocation_cycle_id TEXT NOT NULL,
        candidate_index INTEGER NOT NULL,
        candidate_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        action TEXT NOT NULL,
        reason_codes_json TEXT NOT NULL,
        requested_notional REAL,
        allocated_notional REAL NOT NULL,
        requested_risk REAL,
        allocated_risk REAL,
        requested_quantity REAL,
        allocated_quantity REAL,
        correlation_group TEXT NOT NULL,
        correlation_contribution REAL NOT NULL,
        concentration_contribution REAL NOT NULL,
        hard_gate_accepted INTEGER NOT NULL,
        hard_gate_reason TEXT NOT NULL,
        candidate_inputs_json TEXT NOT NULL,
        preference_components_json TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (allocation_cycle_id, candidate_index),
        UNIQUE (allocation_cycle_id, candidate_id),
        FOREIGN KEY (allocation_cycle_id) REFERENCES portfolio_allocation_cycles(allocation_cycle_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_portfolio_allocation_cycles_time ON portfolio_allocation_cycles(timestamp DESC, allocation_cycle_id)",
    "CREATE INDEX IF NOT EXISTS ix_portfolio_allocation_candidates_symbol ON portfolio_allocation_candidates(symbol, allocation_cycle_id)",
    """
    CREATE TRIGGER IF NOT EXISTS trg_portfolio_allocation_cycles_no_update
    BEFORE UPDATE ON portfolio_allocation_cycles
    BEGIN SELECT RAISE(ABORT, 'portfolio allocation evidence is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_portfolio_allocation_cycles_no_delete
    BEFORE DELETE ON portfolio_allocation_cycles
    BEGIN SELECT RAISE(ABORT, 'portfolio allocation evidence is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_portfolio_allocation_candidates_no_update
    BEFORE UPDATE ON portfolio_allocation_candidates
    BEGIN SELECT RAISE(ABORT, 'portfolio allocation evidence is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_portfolio_allocation_candidates_no_delete
    BEFORE DELETE ON portfolio_allocation_candidates
    BEGIN SELECT RAISE(ABORT, 'portfolio allocation evidence is immutable'); END
    """,
)


def _execute(conn: Any, statement: str, params: dict[str, Any] | None = None) -> Any:
    return conn.execute(
        statement if isinstance(conn, sqlite3.Connection) else text(statement),
        params or {},
    )


def _execute_many(
    conn: Any,
    statement: str,
    params: list[dict[str, Any]],
) -> Any:
    if not params:
        return None
    if isinstance(conn, sqlite3.Connection):
        return conn.executemany(statement, params)
    return conn.execute(text(statement), params)


def persist_portfolio_allocation(
    conn: Any,
    allocation: PortfolioAllocationDecision,
) -> bool:
    """Persist immutable allocation evidence; exact retries are idempotent."""
    payload_json = json.dumps(
        allocation.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    params = {
        "allocation_cycle_id": allocation.allocation_cycle_id,
        "timestamp": allocation.timestamp,
        "mode": allocation.mode,
        "action": allocation.action,
        "candidate_set_hash": allocation.candidate_set_hash,
        "portfolio_snapshot_hash": allocation.portfolio_snapshot_hash,
        "config_hash": allocation.config_hash,
        "universe_hash": allocation.universe_hash,
        "evidence_hash": allocation.evidence_hash,
        "git_sha": allocation.git_sha,
        "release_id": allocation.release_id,
        "runtime_identity": allocation.runtime_identity,
        "allocator_version": allocation.allocator_version,
        "schema_version": allocation.schema_version,
        "payload_json": payload_json,
    }
    _execute(conn, """
        INSERT INTO portfolio_allocation_cycles (
            allocation_cycle_id, timestamp, mode, action, candidate_set_hash,
            portfolio_snapshot_hash, config_hash, universe_hash, evidence_hash,
            git_sha, release_id, runtime_identity, allocator_version,
            schema_version, payload_json
        ) VALUES (
            :allocation_cycle_id, :timestamp, :mode, :action, :candidate_set_hash,
            :portfolio_snapshot_hash, :config_hash, :universe_hash, :evidence_hash,
            :git_sha, :release_id, :runtime_identity, :allocator_version,
            :schema_version, :payload_json
        ) ON CONFLICT(allocation_cycle_id) DO NOTHING
    """, params)
    stored = _execute(conn, """
        SELECT evidence_hash, payload_json
        FROM portfolio_allocation_cycles
        WHERE allocation_cycle_id=:allocation_cycle_id
    """, params).fetchone()
    if stored is None or str(stored[0]) != allocation.evidence_hash or str(stored[1]) != payload_json:
        raise RuntimeError(
            f"PORTFOLIO_ALLOCATION_IDEMPOTENCY_CONFLICT:{allocation.allocation_cycle_id}"
        )

    candidate_params: list[dict[str, Any]] = []
    for index, candidate in enumerate(allocation.candidates):
        candidate_payload = candidate.to_dict()
        candidate_params.append({
            "allocation_cycle_id": allocation.allocation_cycle_id,
            "candidate_index": index,
            "candidate_id": candidate.candidate_id,
            "symbol": candidate.symbol,
            "side": candidate.side,
            "action": candidate.action,
            "reason_codes_json": json.dumps(list(candidate.reason_codes), separators=(",", ":")),
            "requested_notional": candidate.requested_notional,
            "allocated_notional": candidate.allocated_notional,
            "requested_risk": candidate.requested_risk,
            "allocated_risk": candidate.allocated_risk,
            "requested_quantity": candidate.requested_quantity,
            "allocated_quantity": candidate.allocated_quantity,
            "correlation_group": candidate.correlation_group,
            "correlation_contribution": candidate.correlation_contribution,
            "concentration_contribution": candidate.concentration_contribution,
            "hard_gate_accepted": int(candidate.hard_gate_accepted),
            "hard_gate_reason": candidate.hard_gate_reason,
            "candidate_inputs_json": json.dumps(
                candidate.candidate_inputs,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            "preference_components_json": json.dumps(
                candidate.preference_components,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            "payload_json": json.dumps(
                candidate_payload,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        })

    _execute_many(conn, """
        INSERT INTO portfolio_allocation_candidates (
            allocation_cycle_id, candidate_index, candidate_id, symbol, side,
            action, reason_codes_json, requested_notional, allocated_notional,
            requested_risk, allocated_risk, requested_quantity,
            allocated_quantity, correlation_group, correlation_contribution,
            concentration_contribution, hard_gate_accepted, hard_gate_reason,
            candidate_inputs_json, preference_components_json, payload_json
        ) VALUES (
            :allocation_cycle_id, :candidate_index, :candidate_id, :symbol, :side,
            :action, :reason_codes_json, :requested_notional, :allocated_notional,
            :requested_risk, :allocated_risk, :requested_quantity,
            :allocated_quantity, :correlation_group, :correlation_contribution,
            :concentration_contribution, :hard_gate_accepted, :hard_gate_reason,
            :candidate_inputs_json, :preference_components_json, :payload_json
        ) ON CONFLICT(allocation_cycle_id, candidate_index) DO NOTHING
    """, candidate_params)

    stored_rows = _execute(conn, """
        SELECT candidate_index, candidate_id, payload_json
        FROM portfolio_allocation_candidates
        WHERE allocation_cycle_id=:allocation_cycle_id
        ORDER BY candidate_index
    """, {"allocation_cycle_id": allocation.allocation_cycle_id}).fetchall()
    if len(stored_rows) != len(candidate_params):
        raise RuntimeError(
            "PORTFOLIO_ALLOCATION_CANDIDATE_COUNT_CONFLICT:"
            f"{allocation.allocation_cycle_id}:{len(stored_rows)}!={len(candidate_params)}"
        )
    for stored, params in zip(stored_rows, candidate_params):
        if (
            int(stored[0]) != int(params["candidate_index"])
            or str(stored[1]) != str(params["candidate_id"])
            or str(stored[2]) != str(params["payload_json"])
        ):
            raise RuntimeError(
                "PORTFOLIO_ALLOCATION_CANDIDATE_IDEMPOTENCY_CONFLICT:"
                f"{allocation.allocation_cycle_id}:{params['candidate_index']}"
            )
    return True
