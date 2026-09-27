from __future__ import annotations

import sqlite3

from sqlalchemy import text

from alphaforge.persistence import init_db, save_decision_evidence


def _engine(tmp_path):
    return init_db(f"sqlite+pysqlite:///{tmp_path / 'rr-stages.db'}")


def test_decision_evidence_schema_has_explicit_rr_stages(tmp_path):
    engine = _engine(tmp_path)
    with engine.connect() as conn:
        cols = {
            row["name"]
            for row in conn.execute(text("PRAGMA table_info(decision_evidence)")).mappings()
        }
    assert {
        "raw_rr",
        "candidate_raw_rr",
        "executable_raw_rr",
        "remaining_execution_penalty",
        "rr_basis",
        "execution_cost_semantics",
        "effective_rr",
    } <= cols


def test_raw_rr_is_candidate_alias_and_rr_stages_round_trip(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        evidence_id = save_decision_evidence(
            conn,
            evidence_id="rr-stage-1",
            run_id="run-1",
            mode="PAPER",
            timestamp="2026-09-27T20:00:00Z",
            symbol="BTCUSDT",
            decision="ACCEPTED",
            raw_rr=9.9,  # must not override the canonical candidate alias
            candidate_raw_rr=2.0,
            executable_raw_rr=1.8,
            remaining_execution_penalty=0.2,
            rr_basis="EXPECTED_FILL_RUNTIME_PARITY",
            execution_cost_semantics="EXPECTED_FILL_PLUS_REMAINING_PENALTY",
            effective_rr=1.6,
        )
        assert evidence_id == "rr-stage-1"

    with engine.connect() as conn:
        row = conn.execute(text(
            """SELECT raw_rr,candidate_raw_rr,executable_raw_rr,
                      remaining_execution_penalty,rr_basis,
                      execution_cost_semantics,effective_rr
               FROM decision_evidence WHERE evidence_id='rr-stage-1'"""
        )).mappings().one()

    assert row["raw_rr"] == 2.0
    assert row["candidate_raw_rr"] == 2.0
    assert row["executable_raw_rr"] == 1.8
    assert row["remaining_execution_penalty"] == 0.2
    assert row["effective_rr"] == 1.6
    assert row["rr_basis"] == "EXPECTED_FILL_RUNTIME_PARITY"
    assert row["execution_cost_semantics"] == "EXPECTED_FILL_PLUS_REMAINING_PENALTY"


def test_legacy_raw_rr_is_marked_ambiguous_not_backfilled(tmp_path):
    db = tmp_path / "rr-stages.db"
    engine = init_db(f"sqlite+pysqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(text(
            """INSERT INTO decision_evidence(
                   evidence_id,mode,timestamp,symbol,decision,raw_rr,created_at
               ) VALUES (
                   'legacy-rr','PAPER','2026-09-27T19:00:00Z',
                   'BTCUSDT','REJECT',1.25,'2026-09-27T19:00:00Z'
               )"""
        ))
    engine.dispose()

    # Restart/bootstrap must label historical ambiguity without inventing
    # candidate or executable stage values from the overloaded legacy column.
    engine = init_db(f"sqlite+pysqlite:///{db}")
    with engine.connect() as conn:
        row = conn.execute(text(
            """SELECT raw_rr,candidate_raw_rr,executable_raw_rr,
                      remaining_execution_penalty,rr_basis
               FROM decision_evidence WHERE evidence_id='legacy-rr'"""
        )).mappings().one()

    assert row["raw_rr"] == 1.25
    assert row["candidate_raw_rr"] is None
    assert row["executable_raw_rr"] is None
    assert row["remaining_execution_penalty"] is None
    assert row["rr_basis"] == "LEGACY_AMBIGUOUS_RAW_RR"
