from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from alphaforge.burnin_campaign import _with_fresh_lock_retry, bootstrap_campaign_schema
from alphaforge.persistence import init_db


FAMILIES = {
    "final_order_decision": (
        "order_decisions",
        "INSERT OR IGNORE INTO order_decisions(decision_id,signal_id,mode,decision,created_at) VALUES ('p1c-decision','p1c-signal','PAPER','REJECTED','2026-01-01T00:00:00Z')",
        "decision_id='p1c-decision'",
    ),
    "decision_evidence": (
        "decision_evidence",
        "INSERT OR IGNORE INTO decision_evidence(evidence_id,mode,timestamp,symbol,decision,created_at) VALUES ('p1c-evidence','PAPER','2026-01-01T00:00:00Z','BTCUSDT','REJECTED','2026-01-01T00:00:00Z')",
        "evidence_id='p1c-evidence'",
    ),
    "pending_position": (
        "burnin_pending_position_outcomes",
        "INSERT OR IGNORE INTO burnin_pending_position_outcomes(pending_position_id,trade_id,campaign_id,burnin_run_id,symbol,side,entry_time,source_provenance_json,status,created_at,schema_version) VALUES ('p1c-pending','p1c-trade','p1c-campaign','p1c-run','BTCUSDT','LONG','2026-01-01T00:00:00Z','{}','OPEN','2026-01-01T00:00:00Z','p1c')",
        "pending_position_id='p1c-pending'",
    ),
    "accepted_outcome_finalization": (
        "burnin_trade_outcomes",
        "INSERT OR IGNORE INTO burnin_trade_outcomes(outcome_id,burnin_run_id,release_id,symbol,regime,evidence_complete,missing_cost_fields_json,payload_json,schema_version) VALUES ('p1c-trade-outcome','p1c-run','p1c-release','BTCUSDT','TRENDING',0,'[\"SQLITE_WRITE_INCOMPLETE\"]','{}','p1c')",
        "outcome_id='p1c-trade-outcome'",
    ),
    "reject_outcome_finalization": (
        "burnin_reject_outcomes",
        "INSERT OR IGNORE INTO burnin_reject_outcomes(reject_outcome_id,burnin_run_id,release_id,reject_reason,symbol,regime,evidence_complete,payload_json,schema_version) VALUES ('p1c-reject-outcome','p1c-run','p1c-release','LOW_EFFECTIVE_RR','BTCUSDT','TRENDING',0,'{}','p1c')",
        "reject_outcome_id='p1c-reject-outcome'",
    ),
    "lifecycle_persistence": (
        "trade_lifecycle_events",
        "INSERT OR IGNORE INTO trade_lifecycle_events(event_id,signal_id,symbol,mode,lifecycle_state,event_ts,created_at) VALUES ('p1c-life','p1c-signal','BTCUSDT','PAPER','SIGNAL_REJECTED','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
        "event_id='p1c-life'",
    ),
    "qualification_snapshot": (
        "burnin_qualification_snapshots",
        "INSERT OR IGNORE INTO burnin_qualification_snapshots(qualification_id,burnin_run_id,release_id,generated_at,status,sample_status,expectancy_status,execution_status,regime_status,reject_quality_status,calibration_status,drawdown_status,concentration_status,reconciliation_status,evidence_completeness_status,blockers_json,warnings_json,thresholds_json,metrics_json,evidence_hash,schema_version) VALUES ('p1c-q','p1c-run','p1c-release','2026-01-01T00:00:00Z','BURN_IN_INSUFFICIENT','INSUFFICIENT','UNKNOWN','UNKNOWN','UNKNOWN','UNKNOWN','UNKNOWN','UNKNOWN','UNKNOWN','UNKNOWN','INCOMPLETE','[\"SQLITE_WRITE_INCOMPLETE\"]','[]','{}','{}','p1c-hash','p1c')",
        "qualification_id='p1c-q'",
    ),
}


def _engine(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'p1c.sqlite3'}")
    # init_db owns the core persistence schema; Phase 8/9 authoritative
    # campaign families have a separate canonical bootstrap contract.
    with engine.begin() as conn:
        bootstrap_campaign_schema(conn)
    return engine


def _count(engine, table, where):
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {where}")).scalar_one())


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_transient_sqlite_busy_retries_each_authoritative_write_family(tmp_path, family):
    engine = _engine(tmp_path)
    table, sql, where = FAMILIES[family]
    attempts = {"n": 0}

    def operation(conn):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OperationalError("authoritative write", {}, Exception("database is locked"))
        conn.execute(text(sql))
        return family

    assert _with_fresh_lock_retry(engine, operation, attempts=4) == family
    assert attempts["n"] == 3
    assert _count(engine, table, where) == 1


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_persistent_sqlite_busy_fails_closed_without_partial_row_set(tmp_path, family):
    engine = _engine(tmp_path)
    table, sql, where = FAMILIES[family]
    attempts = {"n": 0}

    def operation(conn):
        attempts["n"] += 1
        conn.execute(text(sql))
        # Fault after mutation but before commit: every attempt must roll back.
        raise OperationalError("authoritative write", {}, Exception("database is locked"))

    with pytest.raises(OperationalError):
        _with_fresh_lock_retry(engine, operation, attempts=3)

    assert attempts["n"] == 3
    assert _count(engine, table, where) == 0


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_replay_after_contention_is_deterministic_and_not_duplicated(tmp_path, family):
    engine = _engine(tmp_path)
    table, sql, where = FAMILIES[family]
    state = {"busy": True}

    def operation(conn):
        if state["busy"]:
            state["busy"] = False
            raise OperationalError("authoritative write", {}, Exception("database table is locked"))
        conn.execute(text(sql))

    _with_fresh_lock_retry(engine, operation, attempts=2)
    _with_fresh_lock_retry(engine, lambda conn: conn.execute(text(sql)), attempts=2)
    assert _count(engine, table, where) == 1


def test_multi_surface_authoritative_write_rolls_back_as_one_unit_and_recovers(tmp_path):
    engine = _engine(tmp_path)
    decision = FAMILIES["final_order_decision"]
    evidence = FAMILIES["decision_evidence"]
    lifecycle = FAMILIES["lifecycle_persistence"]
    attempts = {"n": 0}

    def atomic_boundary(conn):
        attempts["n"] += 1
        conn.execute(text(decision[1]))
        conn.execute(text(evidence[1]))
        if attempts["n"] == 1:
            raise OperationalError("authoritative bundle", {}, Exception("database is locked"))
        conn.execute(text(lifecycle[1]))

    _with_fresh_lock_retry(engine, atomic_boundary, attempts=3)
    assert attempts["n"] == 2
    for table, _sql, where in (decision, evidence, lifecycle):
        assert _count(engine, table, where) == 1


def test_persistent_lock_leaves_explicit_incomplete_recovery_evidence_possible(tmp_path):
    engine = _engine(tmp_path)

    def locked(_conn):
        raise OperationalError("qualification snapshot", {}, Exception("database is locked"))

    with pytest.raises(OperationalError):
        _with_fresh_lock_retry(engine, locked, attempts=2)

    # Once the lock clears, recovery is deterministic and records INCOMPLETE
    # authority rather than fabricating successful qualification evidence.
    table, sql, where = FAMILIES["qualification_snapshot"]
    _with_fresh_lock_retry(engine, lambda conn: conn.execute(text(sql)), attempts=2)
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT status,evidence_completeness_status,blockers_json "
            "FROM burnin_qualification_snapshots WHERE qualification_id='p1c-q'"
        )).mappings().one()
    assert row["status"] == "BURN_IN_INSUFFICIENT"
    assert row["evidence_completeness_status"] == "INCOMPLETE"
    assert "SQLITE_WRITE_INCOMPLETE" in json.loads(row["blockers_json"])
    assert _count(engine, table, where) == 1
