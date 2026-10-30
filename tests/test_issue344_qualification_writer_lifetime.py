from __future__ import annotations

import sqlite3

from sqlalchemy import text

from alphaforge.burnin import (
    BurnInRun,
    bootstrap_burnin_schema,
    config_hash,
    persist_burnin_run,
    universe_hash,
)
from alphaforge.burnin_qualification import BurnInQualificationEngine, BurnInThresholds
from alphaforge.persistence import init_db
from alphaforge.runtime_state import RuntimeStateSnapshot, save_runtime_state_snapshot


def test_qualification_compute_phase_does_not_hold_sqlite_writer(tmp_path, monkeypatch):
    db_path = tmp_path / "issue344-qualification-writer.db"
    engine = init_db(f"sqlite+pysqlite:///{db_path}")
    with engine.begin() as conn:
        bootstrap_burnin_schema(conn)
        persist_burnin_run(
            conn,
            BurnInRun(
                burnin_run_id="issue344-run",
                release_id="issue344-release",
                execution_mode="PAPER",
                git_commit="abc",
                config_hash=config_hash({"issue": 344}),
                strategy_config_hash=config_hash({"strategy": 344}),
                universe_hash=universe_hash(["BTCUSDT"], ["1m"]),
                source_provenance={"provider": "TEST"},
                symbols=["BTCUSDT"],
                intervals=["1m"],
                observed_duration_seconds=1000,
                data_completeness_status="PASS",
                evidence_completeness_status="PASS",
            ),
        )
        for index, decision in enumerate(("ACCEPTED", "REJECTED")):
            conn.execute(
                text(
                    """
                    INSERT INTO burnin_observations(
                        observation_id,burnin_run_id,release_id,observed_at,
                        execution_mode,symbol,decision,evidence_complete,
                        missing_fields_json,metrics_json,source_provenance_json,
                        schema_version
                    ) VALUES (
                        :id,'issue344-run','issue344-release',
                        '2026-10-02T18:00:00Z','PAPER','BTCUSDT',:decision,
                        1,'[]','{}','{}','test'
                    )
                    """
                ),
                {"id": f"issue344-observation-{index}", "decision": decision},
            )

    save_runtime_state_snapshot(
        engine,
        RuntimeStateSnapshot(
            mode="PAPER",
            requested_mode="PAPER",
            actual_mode="PAPER",
            runtime_status="OPERATING",
            instance_id="issue344-runtime",
            exchange_read_only_status="AVAILABLE",
            reconciliation_status="CLEAN",
            unknown_exchange_state=False,
        ),
    )

    original_compute_expectancy = BurnInQualificationEngine._compute_expectancy
    probe = {"writer_acquired": False}

    def _probe_writer_during_compute(self, trades, blockers, metrics):
        raw = sqlite3.connect(str(db_path), timeout=0.1)
        try:
            raw.execute("PRAGMA busy_timeout=100")
            assert str(raw.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
            raw.execute("BEGIN IMMEDIATE")
            probe["writer_acquired"] = True
            raw.rollback()
        finally:
            raw.close()
        return original_compute_expectancy(self, trades, blockers, metrics)

    monkeypatch.setattr(
        BurnInQualificationEngine,
        "_compute_expectancy",
        _probe_writer_during_compute,
    )

    thresholds = BurnInThresholds(
        minimum_duration_seconds=0,
        minimum_total_decisions=0,
        minimum_accepted_trades=0,
        minimum_closed_trades=0,
        minimum_rejected_forward_outcomes=0,
        minimum_regime_coverage=0,
        minimum_calibration_sample=0,
        require_operator_ack=False,
        require_phase1_6_gates=False,
    )
    snapshot = BurnInQualificationEngine(engine, thresholds).evaluate("issue344-run")

    assert probe["writer_acquired"] is True
    assert snapshot.burnin_run_id == "issue344-run"
    with engine.connect() as conn:
        run = conn.execute(
            text(
                "SELECT sample_count,accepted_count,rejected_count "
                "FROM burnin_runs WHERE burnin_run_id='issue344-run'"
            )
        ).mappings().one()
        persisted_snapshot = conn.execute(
            text(
                "SELECT qualification_id FROM burnin_qualification_snapshots "
                "WHERE qualification_id=:qid"
            ),
            {"qid": snapshot.qualification_id},
        ).scalar_one()
    assert dict(run) == {
        "sample_count": 2,
        "accepted_count": 1,
        "rejected_count": 1,
    }
    assert persisted_snapshot == snapshot.qualification_id
