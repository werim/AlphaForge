from __future__ import annotations

import sqlite3
import threading
import time

from sqlalchemy import text

from alphaforge.burnin import persist_burnin_observation
from alphaforge.burnin_campaign import create_campaign, start_or_resume_campaign
from alphaforge.burnin_resolver import persist_pending_reject_label, resolve_campaign_batch
from alphaforge.live_readiness import LiveReadinessEvaluator
from alphaforge.persistence import init_db, save_decision_evidence
from alphaforge.runtime_state import RuntimeStateSnapshot, persist_reconciliation_cycle
from alphaforge.system_audit_store import ingest_audit_evidence


LOAD_ROWS = 64
LOAD_DEADLINE_SECONDS = 15.0
COSTS = {
    "spread_cost": 0.01,
    "entry_slippage_cost": 0.01,
    "exit_slippage_cost": 0.01,
    "fee_cost": 0.01,
    "funding_cost": 0.0,
    "latency_cost": 0.0,
    "execution_cost_unit": "R",
}


def _campaign(tmp_path):
    path = tmp_path / "bounded-load.sqlite3"
    engine = init_db(f"sqlite+pysqlite:///{path}")
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    campaign = create_campaign(
        conn,
        release_id="issue421-p2e",
        duration_days=1,
        symbols=["BTCUSDT"],
        intervals=["1m"],
    )
    run = start_or_resume_campaign(conn, campaign.campaign_id)
    conn.commit()
    return path, engine, conn, campaign.campaign_id, run["burnin_run_id"]


def _persist_reject(conn, *, campaign_id: str, run_id: str, index: int) -> None:
    reject_id = f"load-reject-{index:04d}"
    observation_id = f"load-observation-{index:04d}"
    signal_id = f"load-signal-{index:04d}"
    persist_burnin_observation(
        conn,
        observation_id=observation_id,
        burnin_run_id=run_id,
        release_id="issue421-p2e",
        execution_mode="PAPER",
        observed_at="2026-01-01T00:00:00Z",
        symbol="BTCUSDT",
        interval="1m",
        regime="TRENDING",
        decision="REJECTED",
        lifecycle_state="SIGNAL_REJECTED",
        metrics={
            "reject_decision_id": reject_id,
            "signal_id": signal_id,
            "campaign_id": campaign_id,
        },
        source_provenance={"provider": "PAPER_RUNTIME"},
    )
    assert save_decision_evidence(
        conn,
        evidence_id=f"load-evidence-{index:04d}",
        run_id=run_id,
        mode="PAPER",
        timestamp="2026-01-01T00:00:00Z",
        symbol="BTCUSDT",
        side="LONG",
        regime="TRENDING",
        decision="REJECT",
        signal_id=signal_id,
        reject_reason="LOW_EFFECTIVE_RR",
        score=0.4 + index / 1000,
        raw_rr=1.2,
        effective_rr=0.9,
        min_effective_rr=1.1,
        entry=100.0,
        sl=90.0,
        tp=120.0,
        spread_pct=0.0002,
        expected_slippage_pct=0.0002,
        fee_pct=0.0004,
        funding_rate_pct=0.0,
        latency_ms=50.0,
        liquidity_score=0.9,
        diagnostics_json={"observation_id": observation_id, "campaign_id": campaign_id},
        reject_flags=["LOW_EFFECTIVE_RR"],
    ) == f"load-evidence-{index:04d}"
    assert persist_pending_reject_label(
        conn,
        campaign_id=campaign_id,
        burnin_run_id=run_id,
        reject_decision_id=reject_id,
        signal_id=signal_id,
        symbol="BTCUSDT",
        side="LONG",
        decision_timestamp="2026-01-01T00:00:00Z",
        entry=100.0,
        stop=90.0,
        target=120.0,
        timeframe="1m",
        horizon_bars=1,
        execution_cost_assumptions=COSTS,
        regime="TRENDING",
        reject_reason="LOW_EFFECTIVE_RR",
        source_provenance={
            "provider": "PAPER_RUNTIME",
            "forward_label_subject": "GUIDED_CANDIDATE",
            "reject_execution_basis": "EXPECTED_FILL_RUNTIME_PARITY",
            "reject_quality_attributable": True,
        },
    ) == f"prej_{reject_id}"


def test_bounded_reject_backlog_and_audit_ingestion_are_lossless_and_idempotent(tmp_path):
    _path, engine, source, campaign_id, run_id = _campaign(tmp_path)
    started = time.monotonic()
    for index in range(LOAD_ROWS):
        _persist_reject(
            source, campaign_id=campaign_id, run_id=run_id, index=index
        )
    source.commit()

    candles = {
        "BTCUSDT": [
            {"timestamp": "2026-01-01T00:01:00Z", "high": 121.0, "low": 99.0}
        ]
    }
    first = resolve_campaign_batch(
        source, campaign_id, candles, now="2026-01-01T00:02:00Z"
    )
    second = resolve_campaign_batch(
        source, campaign_id, candles, now="2026-01-01T00:02:00Z"
    )
    source.commit()

    audit = sqlite3.connect(tmp_path / "bounded-audit.sqlite3")
    ingested = ingest_audit_evidence(source, audit, burnin_run_id=run_id)
    replayed = ingest_audit_evidence(source, audit, burnin_run_id=run_id)

    assert first["resolved"] == LOAD_ROWS
    assert second["resolved"] == second["canonical"] == 0
    assert source.execute("SELECT COUNT(*) FROM decision_evidence").fetchone()[0] == LOAD_ROWS
    assert source.execute("SELECT COUNT(*) FROM burnin_reject_outcomes").fetchone()[0] == LOAD_ROWS
    assert source.execute(
        "SELECT COUNT(*) FROM burnin_pending_reject_labels WHERE status='RESOLVED'"
    ).fetchone()[0] == LOAD_ROWS
    assert ingested["source_decisions"] == ingested["envelopes_added"] == LOAD_ROWS
    assert ingested["shadow_decisions_added"] == LOAD_ROWS
    assert ingested["shadow_outcomes_added"] == LOAD_ROWS
    assert replayed["envelopes_added"] == replayed["outcomes_added"] == 0
    assert replayed["shadow_decisions_added"] == replayed["shadow_outcomes_added"] == 0
    assert time.monotonic() - started < LOAD_DEADLINE_SECONDS

    audit.close()
    source.close()
    engine.dispose()


def test_bounded_reconciliation_volume_is_atomic_and_idempotent(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'reconciliation.sqlite3'}")
    snapshot = RuntimeStateSnapshot(
        mode="PAPER",
        requested_mode="PAPER",
        actual_mode="PAPER",
        runtime_status="RECONCILED",
        instance_id="issue421-load",
        startup_id="issue421-load-startup",
        unknown_exchange_state=False,
        exchange_connectivity_status="HEALTHY",
        exchange_read_only_status="COMPLETE",
        reconciliation_status="CLEAN",
    )
    started = time.monotonic()
    for index in range(LOAD_ROWS):
        cycle_id = f"recon:v1:issue421-load:{index:04d}"
        assert persist_reconciliation_cycle(
            engine,
            cycle_id=cycle_id,
            findings=[],
            snapshot=snapshot,
            diagnostics={"load_sequence": index},
        )
        assert not persist_reconciliation_cycle(
            engine,
            cycle_id=cycle_id,
            findings=[],
            snapshot=snapshot,
            diagnostics={"load_sequence": index, "replay": True},
        )

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM exchange_reconciliation_events")).scalar_one() == LOAD_ROWS
        assert conn.execute(text("SELECT COUNT(*) FROM runtime_state_snapshots")).scalar_one() == LOAD_ROWS
        assert conn.execute(text("SELECT COUNT(*) FROM reconciliation_incidents")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(DISTINCT cycle_id) FROM exchange_reconciliation_events")).scalar_one() == LOAD_ROWS
    assert time.monotonic() - started < LOAD_DEADLINE_SECONDS
    engine.dispose()


def test_active_paper_writes_do_not_starve_read_only_readiness_or_audit(tmp_path):
    path, engine, setup, campaign_id, run_id = _campaign(tmp_path)
    setup.close()
    writer_done = threading.Event()
    errors: list[BaseException] = []
    reads = {"readiness": 0, "audit": 0}

    def writer() -> None:
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            for index in range(LOAD_ROWS):
                _persist_reject(
                    conn, campaign_id=campaign_id, run_id=run_id, index=index
                )
                if index % 8 == 7:
                    conn.commit()
            conn.commit()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            conn.close()
            writer_done.set()

    def reader() -> None:
        source = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        audit = sqlite3.connect(tmp_path / "concurrent-audit.sqlite3", timeout=5)
        try:
            while not writer_done.is_set() or reads["readiness"] < 4:
                report = LiveReadinessEvaluator(
                    engine,
                    campaign_id=campaign_id,
                    burnin_run_id=run_id,
                    evidence_mode="PAPER",
                ).evaluate(
                    mode_parity={},
                    reconciliation_snapshot={},
                    observability_snapshot={},
                    canary_enabled=False,
                    shadow_mode_enabled=False,
                    operator_ack=False,
                )
                assert report.qualified is False
                reads["readiness"] += 1
                ingest_audit_evidence(source, audit, burnin_run_id=run_id)
                reads["audit"] += 1
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            source.close()
            audit.close()

    started = time.monotonic()
    writer_thread = threading.Thread(target=writer, name="issue421-load-writer")
    reader_thread = threading.Thread(target=reader, name="issue421-load-reader")
    reader_thread.start()
    writer_thread.start()
    writer_thread.join(LOAD_DEADLINE_SECONDS)
    reader_thread.join(LOAD_DEADLINE_SECONDS)

    assert not writer_thread.is_alive()
    assert not reader_thread.is_alive()
    assert not errors
    assert reads["readiness"] >= 4
    assert reads["audit"] >= 4
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM decision_evidence")).scalar_one() == LOAD_ROWS
    audit = sqlite3.connect(tmp_path / "concurrent-audit.sqlite3")
    assert audit.execute("SELECT COUNT(*) FROM audit_decision_envelopes").fetchone()[0] == LOAD_ROWS
    assert time.monotonic() - started < LOAD_DEADLINE_SECONDS
    audit.close()
    engine.dispose()
