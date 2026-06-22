from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from alphaforge.paper_burnin import generate_paper_burnin_report
from alphaforge.persistence import init_db
from alphaforge.runtime_heartbeat import save_runtime_heartbeat


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "paper.db"
    init_db(f"sqlite+pysqlite:///{path}")
    return path


def _insert_decision(conn, *, signal_id="s1", decision="REJECTED", reject_reason="LOW_EFFECTIVE_RR", score=0.2, rr=2.0, effective_rr=1.5, execution_ctx='{"spread_pct":0.01}'):
    conn.execute(text("""
        INSERT INTO order_decisions(decision_id, signal_id, order_id, symbol, mode, decision, reject_reason, score, rr, effective_rr, execution_ctx, execution_ctx_missing, created_at, updated_at)
        VALUES (:decision_id, :signal_id, :order_id, 'BTCUSDT', 'PAPER', :decision, :reject_reason, :score, :rr, :effective_rr, :execution_ctx, 0, '2026-06-22T00:00:00Z', '2026-06-22T00:01:00Z')
    """), {"decision_id": f"d-{signal_id}", "signal_id": signal_id, "order_id": f"o-{signal_id}" if decision == "ACCEPTED" else None, "decision": decision, "reject_reason": reject_reason, "score": score, "rr": rr, "effective_rr": effective_rr, "execution_ctx": execution_ctx})


def _insert_lifecycle(conn, signal_id="s1", states=("SIGNAL_CREATED", "SIGNAL_REJECTED"), reject_reason="LOW_EFFECTIVE_RR"):
    for idx, state in enumerate(states):
        conn.execute(text("""
            INSERT INTO trade_lifecycle_events(event_id, signal_id, symbol, mode, lifecycle_state, event_type, reject_reason, event_ts, created_at)
            VALUES (:event_id, :signal_id, 'BTCUSDT', 'PAPER', :state, :state, :reject_reason, :ts, :ts)
        """), {"event_id": f"e-{signal_id}-{idx}", "signal_id": signal_id, "state": state, "reject_reason": reject_reason if state == "SIGNAL_REJECTED" else "", "ts": f"2026-06-22T00:0{idx}:00Z"})


def test_empty_db_is_insufficient_sample(tmp_path):
    db = _db(tmp_path)
    report = generate_paper_burnin_report(db, tmp_path / "out")
    assert "INSUFFICIENT_SAMPLE" in report["classification"]
    assert report["live_readiness"] == "NOT_LIVE_READY"


def test_missing_reject_reason_is_data_integrity_failure(tmp_path):
    db = _db(tmp_path)
    engine = init_db(f"sqlite+pysqlite:///{db}")
    with engine.begin() as conn:
        _insert_decision(conn, reject_reason="")
        _insert_lifecycle(conn, reject_reason="")
    report = generate_paper_burnin_report(db, tmp_path / "out")
    assert "DATA_INTEGRITY_FAILURE" in report["classification"]
    assert report["missing_reject_reason_count"] == 1


def test_bad_lifecycle_ordering_is_lifecycle_failure(tmp_path):
    db = _db(tmp_path)
    engine = init_db(f"sqlite+pysqlite:///{db}")
    with engine.begin() as conn:
        _insert_decision(conn)
        _insert_lifecycle(conn, states=("SIGNAL_REJECTED",))
    report = generate_paper_burnin_report(db, tmp_path / "out")
    assert "LIFECYCLE_INTEGRITY_FAILURE" in report["classification"]
    assert report["lifecycle_ordering_errors"] == 1


def test_missing_execution_context_is_execution_failure(tmp_path):
    db = _db(tmp_path)
    engine = init_db(f"sqlite+pysqlite:///{db}")
    with engine.begin() as conn:
        _insert_decision(conn, execution_ctx="{}")
        _insert_lifecycle(conn)
    report = generate_paper_burnin_report(db, tmp_path / "out")
    assert "EXECUTION_CONTEXT_FAILURE" in report["classification"]


def test_fake_zeros_are_execution_failure(tmp_path):
    db = _db(tmp_path)
    engine = init_db(f"sqlite+pysqlite:///{db}")
    with engine.begin() as conn:
        _insert_decision(conn)
        _insert_lifecycle(conn)
        conn.execute(text("""
            INSERT INTO rejected_signal_reviews(signal_id, symbol, reject_reason, score, raw_rr, effective_rr, spread_pct, expected_slippage_pct, funding_rate_pct, liquidity_score, created_at)
            VALUES ('s1', 'BTCUSDT', 'LOW_EFFECTIVE_RR', 0.2, 2.0, 1.5, 0, 0, 0, 0, '2026-06-22T00:00:00Z')
        """))
    report = generate_paper_burnin_report(db, tmp_path / "out")
    assert "EXECUTION_CONTEXT_FAILURE" in report["classification"]
    assert report["fake_zero_detection"]["fake_zero_count"] == 4


def test_healthy_synthetic_db_reports_selectivity_but_not_live_ready(tmp_path):
    db = _db(tmp_path)
    engine = init_db(f"sqlite+pysqlite:///{db}")
    with engine.begin() as conn:
        _insert_decision(conn, signal_id="s1", decision="REJECTED", reject_reason="LOW_EFFECTIVE_RR", score=0.2, rr=2.0, effective_rr=1.5)
        _insert_lifecycle(conn, "s1")
        _insert_decision(conn, signal_id="s2", decision="ACCEPTED", reject_reason="", score=0.8, rr=3.0, effective_rr=2.7)
        _insert_lifecycle(conn, "s2", states=("SIGNAL_CREATED", "WAITING_ENTRY_ZONE", "ENTRY_TRIGGERED", "ORDER_PLACED", "POSITION_OPENED", "OPEN_AT_END"), reject_reason="")
        conn.execute(text("""
            INSERT INTO rejected_signal_reviews(signal_id, symbol, reject_reason, spread_pct, expected_slippage_pct, funding_rate_pct, liquidity_score, created_at)
            VALUES ('s1', 'BTCUSDT', 'LOW_EFFECTIVE_RR', 0.01, 0.02, 0.001, 0.9, '2026-06-22T00:00:00Z')
        """))
    save_runtime_heartbeat(engine, runtime_instance_id="r1", execution_mode="PAPER", scanner_source="EXCHANGE_PUBLIC_MARKET_DATA", heartbeat_ts="2026-06-22T00:02:00Z")
    report = generate_paper_burnin_report(db, tmp_path / "out")
    assert "HEALTHY_SELECTIVITY" in report["classification"]
    assert report["live_readiness"] == "NOT_LIVE_READY"
    assert (tmp_path / "out" / "paper_burnin_summary.csv").exists()
    assert (tmp_path / "out" / "paper_burnin_report.md").exists()
    assert (tmp_path / "out" / "paper_burnin_blockers.json").exists()


def test_timesfm_absence_is_not_fatal(tmp_path):
    db = _db(tmp_path)
    engine = init_db(f"sqlite+pysqlite:///{db}")
    with engine.begin() as conn:
        _insert_decision(conn)
        _insert_lifecycle(conn)
    report = generate_paper_burnin_report(db, tmp_path / "out")
    blockers = {b["blocker"] for b in report["readiness_blockers"]}
    assert "timesfm_evidence_absent_optional_not_fatal" in blockers
    assert "SCORING_OR_REGIME_PIPELINE_FAILURE" not in report["classification"]
