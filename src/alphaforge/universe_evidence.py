from __future__ import annotations

import json
import sqlite3
from typing import Any

from sqlalchemy import text

from alphaforge.symbol_selector import SelectedUniverse, _canonical_hash


# Storage representation version is intentionally separate from the canonical
# strategy/economic schema_version and evidence_hash. Legacy full JSON stays readable.
UNIVERSE_CYCLE_STORAGE_CODEC = "candidate-rows-v2"


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
    """
    CREATE TABLE IF NOT EXISTS burnin_universe_selection_links (
        link_id TEXT PRIMARY KEY,
        campaign_id TEXT NOT NULL,
        burnin_run_id TEXT NOT NULL,
        cycle_id TEXT NOT NULL,
        decision_timestamp REAL NOT NULL,
        schema_version TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(campaign_id, burnin_run_id, cycle_id),
        FOREIGN KEY (cycle_id) REFERENCES universe_selection_cycles(cycle_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_burnin_universe_selection_links_scope ON burnin_universe_selection_links(campaign_id, burnin_run_id, decision_timestamp)",
    """
    CREATE TRIGGER IF NOT EXISTS trg_burnin_universe_selection_links_no_update
    BEFORE UPDATE ON burnin_universe_selection_links
    BEGIN SELECT RAISE(ABORT, 'burn-in universe selection link is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_burnin_universe_selection_links_no_delete
    BEFORE DELETE ON burnin_universe_selection_links
    BEGIN SELECT RAISE(ABORT, 'burn-in universe selection link is immutable'); END
    """,
)


def _execute(conn: Any, statement: str, params: dict[str, Any] | None = None) -> Any:
    return conn.execute(statement if isinstance(conn, sqlite3.Connection) else text(statement), params or {})


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


def persist_universe_selection(conn: Any, selection: SelectedUniverse) -> bool:
    """Insert immutable selection evidence, accepting exact replay idempotently."""
    # The canonical evidence hash still binds the *complete* decision-time
    # selection. Candidate details already have immutable, verified rows below;
    # keeping another copy in the cycle JSON doubled database growth (#622).
    # Serialize metadata only on the common write path. Only an exact replay
    # colliding with a pre-#622 full-payload row needs legacy serialization.
    payload_json = json.dumps({
        "_storage_codec": UNIVERSE_CYCLE_STORAGE_CODEC,
        "cycle_id": selection.cycle_id,
        "decision_timestamp": selection.decision_timestamp,
        "execution_mode": selection.execution_mode,
        "constraints": selection.constraints.as_dict(),
        "selected_symbols": list(selection.selected_symbols),
        "config_hash": selection.config_hash,
        "strategy_config_hash": selection.strategy_config_hash,
        "universe_hash": selection.universe_hash,
        "evidence_hash": selection.evidence_hash,
        "git_sha": selection.git_sha,
        "source_provenance": list(selection.source_provenance),
        "ranking_version": selection.ranking_version,
        "schema_version": selection.schema_version,
    }, sort_keys=True, separators=(",", ":"), allow_nan=False)
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
    cycle_insert = _execute(conn, """
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
    if cycle_insert.rowcount not in (0, 1):
        # Without a reliable write outcome we cannot safely distinguish an
        # initially new cycle from a damaged existing record.
        raise RuntimeError(f"UNIVERSE_SELECTION_CYCLE_WRITE_OUTCOME_UNKNOWN:{selection.cycle_id}")
    existing = _execute(
        conn,
        "SELECT evidence_hash, payload_json FROM universe_selection_cycles WHERE cycle_id=:cycle_id",
        {"cycle_id": selection.cycle_id},
    ).fetchone()
    # Exact legacy retries on pre-#622 files must remain idempotent, without
    # serializing every candidate twice on the usual new-cycle write path.
    if existing is None or str(existing[0]) != selection.evidence_hash:
        raise RuntimeError(f"UNIVERSE_SELECTION_IDEMPOTENCY_CONFLICT:{selection.cycle_id}")
    if str(existing[1]) != payload_json:
        legacy_json = json.dumps(selection.as_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)
        if str(existing[1]) != legacy_json:
            raise RuntimeError(f"UNIVERSE_SELECTION_IDEMPOTENCY_CONFLICT:{selection.cycle_id}")

    candidate_params: list[dict[str, Any]] = []
    for index, candidate in enumerate(selection.candidates):
        candidate_params.append({
            "cycle_id": selection.cycle_id,
            "candidate_index": index,
            "symbol": candidate.symbol,
            "eligibility_state": candidate.state.value,
            "exclusion_reasons_json": json.dumps(list(candidate.reasons), sort_keys=True),
            "observed_inputs_json": json.dumps(
                candidate.observed_inputs,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            "ranking_components_json": json.dumps(
                candidate.ranking_components,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            "ranking_score": candidate.score,
            "ranking_order": candidate.rank,
            "selected": int(candidate.selected),
            "evidence_availability_json": json.dumps(
                candidate.evidence_availability, sort_keys=True
            ),
        })

    # A replay must NEVER refill missing immutable candidate evidence. A cycle
    # and all its candidates are one atomic production writer transaction. If
    # historical candidates went missing, validation below fails closed.
    if cycle_insert.rowcount == 1:
        _execute_many(conn, """
            INSERT INTO universe_selection_candidates (
            cycle_id, candidate_index, symbol, eligibility_state,
            exclusion_reasons_json, observed_inputs_json, ranking_components_json,
            ranking_score, ranking_order, selected, evidence_availability_json
        ) VALUES (
            :cycle_id, :candidate_index, :symbol, :eligibility_state,
            :exclusion_reasons_json, :observed_inputs_json, :ranking_components_json,
            :ranking_score, :ranking_order, :selected, :evidence_availability_json
        ) ON CONFLICT(cycle_id, candidate_index) DO NOTHING
        """, candidate_params)

    stored_rows = _execute(
        conn,
        """
        SELECT candidate_index, symbol, eligibility_state, exclusion_reasons_json,
               observed_inputs_json, ranking_components_json, ranking_score,
               ranking_order, selected, evidence_availability_json
        FROM universe_selection_candidates
        WHERE cycle_id=:cycle_id
        ORDER BY candidate_index
        """,
        {"cycle_id": selection.cycle_id},
    ).fetchall()
    if len(stored_rows) != len(candidate_params):
        raise RuntimeError(
            f"UNIVERSE_SELECTION_CANDIDATE_COUNT_CONFLICT:{selection.cycle_id}:"
            f"{len(stored_rows)}!={len(candidate_params)}"
        )

    for stored, params in zip(stored_rows, candidate_params):
        expected = (
            params["candidate_index"],
            params["symbol"],
            params["eligibility_state"],
            params["exclusion_reasons_json"],
            params["observed_inputs_json"],
            params["ranking_components_json"],
            params["ranking_score"],
            params["ranking_order"],
            params["selected"],
            params["evidence_availability_json"],
        )
        if tuple(stored) != expected:
            raise RuntimeError(
                "UNIVERSE_SELECTION_CANDIDATE_IDEMPOTENCY_CONFLICT:"
                f"{selection.cycle_id}:{params['candidate_index']}"
            )
    return True



def load_persisted_universe_selection(conn: Any, cycle_id: str) -> dict[str, Any]:
    """Losslessly reconstruct and verify one immutable decision-time universe.

    The v2 cycle JSON contains metadata and constraints; candidate rows are its
    only candidate-data authority. Historical full v1 payloads remain readable.
    Missing, malformed, altered, or wrong-cycle data fail closed: no fallback to
    the latest universe or another campaign/run is permitted.
    """
    row = _execute(conn, """
        SELECT cycle_id, decision_timestamp, execution_mode, selected_symbols_json,
               candidate_count, config_hash, strategy_config_hash, universe_hash,
               evidence_hash, git_sha, source_provenance_json, ranking_version,
               schema_version, payload_json
        FROM universe_selection_cycles WHERE cycle_id=:cycle_id
    """, {"cycle_id": cycle_id}).fetchone()
    if row is None:
        raise ValueError(f"UNIVERSE_SELECTION_EVIDENCE_MISSING:{cycle_id}")
    (stored_id, decision_ts, mode, selected_json, count, config_hash, strategy_hash,
     universe_hash, evidence_hash, git_sha, providers_json, rank_version,
     schema_version, payload_json) = tuple(row)

    try:
        payload = json.loads(payload_json)
        if not isinstance(payload, dict):
            raise ValueError("cycle payload is not an object")
        codec = payload.get("_storage_codec")
        if codec not in (None, UNIVERSE_CYCLE_STORAGE_CODEC):
            raise ValueError("unknown storage codec")
        stored = _execute(conn, """
            SELECT candidate_index, symbol, eligibility_state, exclusion_reasons_json,
                   observed_inputs_json, ranking_components_json, ranking_score,
                   ranking_order, selected, evidence_availability_json
            FROM universe_selection_candidates
            WHERE cycle_id=:cycle_id ORDER BY candidate_index
        """, {"cycle_id": cycle_id}).fetchall()
        if len(stored) != count:
            raise ValueError("candidate count mismatch")

        candidates = []
        for index, record in enumerate(stored):
            (stored_index, symbol, state, reasons, observed, components,
             score, rank, selected, availability) = tuple(record)
            if stored_index != index or selected not in (0, 1):
                raise ValueError("candidate index or boolean mismatch")
            candidate = {
                "symbol": symbol, "state": state, "reasons": json.loads(reasons),
                "observed_inputs": json.loads(observed),
                "ranking_components": json.loads(components), "score": score,
                "rank": rank, "selected": bool(selected),
                "evidence_availability": json.loads(availability),
            }
            if not isinstance(candidate["reasons"], list) or not all(
                isinstance(candidate[key], dict)
                for key in ("observed_inputs", "ranking_components", "evidence_availability")
            ):
                raise ValueError("candidate JSON structure invalid")
            candidates.append(candidate)

        if codec == UNIVERSE_CYCLE_STORAGE_CODEC:
            if "candidates" in payload:
                raise ValueError("normalized cycle contains duplicate candidates")
            payload.pop("_storage_codec")
            payload["candidates"] = candidates
        elif codec is None:
            if payload.get("candidates") != candidates:
                raise ValueError("legacy candidate rows diverge from cycle payload")

        expected = {
            "cycle_id": stored_id, "decision_timestamp": decision_ts,
            "execution_mode": mode, "selected_symbols": json.loads(selected_json),
            "config_hash": config_hash, "strategy_config_hash": strategy_hash,
            "universe_hash": universe_hash, "evidence_hash": evidence_hash,
            "git_sha": git_sha, "source_provenance": json.loads(providers_json),
            "ranking_version": rank_version, "schema_version": schema_version,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise ValueError("cycle metadata mismatch")
        if not isinstance(payload.get("constraints"), dict):
            raise ValueError("constraints unavailable")
        if _canonical_hash(payload["constraints"]) != config_hash:
            raise ValueError("constraints hash mismatch")
        canonical = {
            "decision_timestamp": decision_ts, "constraints": payload["constraints"],
            "candidates": candidates, "git_sha": git_sha,
            "strategy_config_hash": strategy_hash, "ranking_version": rank_version,
            "schema_version": schema_version,
        }
        if _canonical_hash(canonical) != evidence_hash:
            raise ValueError("canonical decision evidence hash mismatch")
        if stored_id != f"universe:{mode}:{evidence_hash}":
            raise ValueError("cycle ID / evidence hash mismatch")
        actual_selected = [candidate["symbol"] for candidate in candidates if candidate["selected"]]
        if actual_selected != payload["selected_symbols"]:
            raise ValueError("selected symbol projection mismatch")
        if _canonical_hash({"symbols": actual_selected, "evidence_hash": evidence_hash}) != universe_hash:
            raise ValueError("universe hash mismatch")
        return payload
    except (TypeError, KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"UNIVERSE_SELECTION_EVIDENCE_INVALID:{cycle_id}:{exc}") from exc


def persist_burnin_universe_selection_link(
    conn: Any,
    selection: SelectedUniverse,
    *,
    campaign_id: str,
    burnin_run_id: str,
) -> bool:
    """Bind immutable selector evidence to one burn-in continuation."""
    campaign_id = str(campaign_id or "").strip()
    burnin_run_id = str(burnin_run_id or "").strip()
    if not campaign_id or not burnin_run_id:
        raise ValueError("campaign_id and burnin_run_id are required")
    from alphaforge.burnin_campaign import require_operational_campaign_evidence
    require_operational_campaign_evidence(conn, campaign_id)
    link_id = f"burnin-universe:{campaign_id}:{burnin_run_id}:{selection.cycle_id}"
    params = {
        "link_id": link_id,
        "campaign_id": campaign_id,
        "burnin_run_id": burnin_run_id,
        "cycle_id": selection.cycle_id,
        "decision_timestamp": float(selection.decision_timestamp),
        "schema_version": selection.schema_version,
    }
    _execute(conn, """
        INSERT INTO burnin_universe_selection_links (
            link_id, campaign_id, burnin_run_id, cycle_id,
            decision_timestamp, schema_version
        ) VALUES (
            :link_id, :campaign_id, :burnin_run_id, :cycle_id,
            :decision_timestamp, :schema_version
        ) ON CONFLICT(campaign_id, burnin_run_id, cycle_id) DO NOTHING
    """, params)
    stored = _execute(
        conn,
        """
        SELECT link_id, campaign_id, burnin_run_id, cycle_id,
               decision_timestamp, schema_version
        FROM burnin_universe_selection_links
        WHERE campaign_id=:campaign_id AND burnin_run_id=:burnin_run_id
          AND cycle_id=:cycle_id
        """,
        params,
    ).fetchone()
    expected = (
        params["link_id"], params["campaign_id"], params["burnin_run_id"],
        params["cycle_id"], params["decision_timestamp"], params["schema_version"],
    )
    if stored is None or tuple(stored) != expected:
        raise RuntimeError(
            f"BURNIN_UNIVERSE_SELECTION_LINK_IDEMPOTENCY_CONFLICT:"
            f"{campaign_id}:{burnin_run_id}:{selection.cycle_id}"
        )
    return True
