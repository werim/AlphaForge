from __future__ import annotations

import json
import sqlite3
from typing import Any

from sqlalchemy import text

from alphaforge.symbol_selector import SelectedUniverse


UNIVERSE_SELECTION_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS universe_selection_cycles (
        cycle_id TEXT PRIMARY KEY,
        decision_timestamp REAL NOT NULL,
        execution_mode TEXT NOT NULL,
        selected_symbols_json TEXT NOT NULL,
        candidate_count INTEGER NOT NULL,
        config_hash TEXT NOT NULL,
        strategy_config_hash TEXT,
        universe_hash TEXT NOT NULL,
        evidence_hash TEXT NOT NULL,
        git_sha TEXT NOT NULL,
        source_provenance_json TEXT NOT NULL,
        ranking_version TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS universe_selection_candidates (
        cycle_id TEXT NOT NULL,
        candidate_index INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        eligibility_state TEXT NOT NULL,
        exclusion_reasons_json TEXT NOT NULL,
        observed_inputs_json TEXT NOT NULL,
        ranking_components_json TEXT NOT NULL,
        ranking_score REAL,
        ranking_order INTEGER,
        selected INTEGER NOT NULL,
        evidence_availability_json TEXT NOT NULL,
        PRIMARY KEY (cycle_id, candidate_index),
        FOREIGN KEY (cycle_id) REFERENCES universe_selection_cycles(cycle_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_universe_selection_cycles_time ON universe_selection_cycles(decision_timestamp DESC, cycle_id)",
    "CREATE INDEX IF NOT EXISTS ix_universe_selection_candidates_symbol ON universe_selection_candidates(symbol, cycle_id)",
    """
    CREATE TRIGGER IF NOT EXISTS trg_universe_selection_cycles_no_update
    BEFORE UPDATE ON universe_selection_cycles
    BEGIN SELECT RAISE(ABORT, 'universe selection evidence is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_universe_selection_cycles_no_delete
    BEFORE DELETE ON universe_selection_cycles
    BEGIN SELECT RAISE(ABORT, 'universe selection evidence is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_universe_selection_candidates_no_update
    BEFORE UPDATE ON universe_selection_candidates
    BEGIN SELECT RAISE(ABORT, 'universe selection evidence is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_universe_selection_candidates_no_delete
    BEFORE DELETE ON universe_selection_candidates
    BEGIN SELECT RAISE(ABORT, 'universe selection evidence is immutable'); END
    """,
)


def _execute(conn: Any, statement: str, params: dict[str, Any] | None = None) -> Any:
    return conn.execute(statement if isinstance(conn, sqlite3.Connection) else text(statement), params or {})


def persist_universe_selection(conn: Any, selection: SelectedUniverse) -> bool:
    """Insert immutable selection evidence, accepting exact replay idempotently."""
    payload_json = json.dumps(selection.as_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)
    cycle_params = {
        "cycle_id": selection.cycle_id,
        "decision_timestamp": selection.decision_timestamp,
        "execution_mode": selection.execution_mode,
        "selected_symbols_json": json.dumps(list(selection.selected_symbols), sort_keys=True),
        "candidate_count": len(selection.candidates),
        "config_hash": selection.config_hash,
        "strategy_config_hash": selection.strategy_config_hash,
        "universe_hash": selection.universe_hash,
        "evidence_hash": selection.evidence_hash,
        "git_sha": selection.git_sha,
        "source_provenance_json": json.dumps(list(selection.source_provenance), sort_keys=True),
        "ranking_version": selection.ranking_version,
        "schema_version": selection.schema_version,
        "payload_json": payload_json,
    }
    _execute(conn, """
        INSERT INTO universe_selection_cycles (
            cycle_id, decision_timestamp, execution_mode, selected_symbols_json,
            candidate_count, config_hash, strategy_config_hash, universe_hash,
            evidence_hash, git_sha, source_provenance_json, ranking_version,
            schema_version, payload_json
        ) VALUES (
            :cycle_id, :decision_timestamp, :execution_mode, :selected_symbols_json,
            :candidate_count, :config_hash, :strategy_config_hash, :universe_hash,
            :evidence_hash, :git_sha, :source_provenance_json, :ranking_version,
            :schema_version, :payload_json
        ) ON CONFLICT(cycle_id) DO NOTHING
    """, cycle_params)
    existing = _execute(
        conn,
        "SELECT evidence_hash, payload_json FROM universe_selection_cycles WHERE cycle_id=:cycle_id",
        {"cycle_id": selection.cycle_id},
    ).fetchone()
    if existing is None or str(existing[0]) != selection.evidence_hash or str(existing[1]) != payload_json:
        raise RuntimeError(f"UNIVERSE_SELECTION_IDEMPOTENCY_CONFLICT:{selection.cycle_id}")

    for index, candidate in enumerate(selection.candidates):
        params = {
            "cycle_id": selection.cycle_id,
            "candidate_index": index,
            "symbol": candidate.symbol,
            "eligibility_state": candidate.state.value,
            "exclusion_reasons_json": json.dumps(list(candidate.reasons), sort_keys=True),
            "observed_inputs_json": json.dumps(candidate.observed_inputs, sort_keys=True, separators=(",", ":"), allow_nan=False),
            "ranking_components_json": json.dumps(candidate.ranking_components, sort_keys=True, separators=(",", ":"), allow_nan=False),
            "ranking_score": candidate.score,
            "ranking_order": candidate.rank,
            "selected": int(candidate.selected),
            "evidence_availability_json": json.dumps(candidate.evidence_availability, sort_keys=True),
        }
        _execute(conn, """
            INSERT INTO universe_selection_candidates (
                cycle_id, candidate_index, symbol, eligibility_state,
                exclusion_reasons_json, observed_inputs_json, ranking_components_json,
                ranking_score, ranking_order, selected, evidence_availability_json
            ) VALUES (
                :cycle_id, :candidate_index, :symbol, :eligibility_state,
                :exclusion_reasons_json, :observed_inputs_json, :ranking_components_json,
                :ranking_score, :ranking_order, :selected, :evidence_availability_json
            ) ON CONFLICT(cycle_id, candidate_index) DO NOTHING
        """, params)
        stored = _execute(conn, """
            SELECT symbol, eligibility_state, exclusion_reasons_json, observed_inputs_json,
                   ranking_components_json, ranking_score, ranking_order, selected,
                   evidence_availability_json
            FROM universe_selection_candidates
            WHERE cycle_id=:cycle_id AND candidate_index=:candidate_index
        """, params).fetchone()
        expected = (
            params["symbol"], params["eligibility_state"], params["exclusion_reasons_json"],
            params["observed_inputs_json"], params["ranking_components_json"],
            params["ranking_score"], params["ranking_order"], params["selected"],
            params["evidence_availability_json"],
        )
        if stored is None or tuple(stored) != expected:
            raise RuntimeError(f"UNIVERSE_SELECTION_CANDIDATE_IDEMPOTENCY_CONFLICT:{selection.cycle_id}:{index}")
    return True
