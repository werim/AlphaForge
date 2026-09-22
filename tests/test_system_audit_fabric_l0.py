from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from alphaforge.burnin import (
    BurnInRun,
    persist_burnin_observation,
    persist_burnin_reject_outcome,
    persist_burnin_run,
    persist_burnin_trade_outcome,
)
from alphaforge.burnin_campaign import bootstrap_campaign_schema
from alphaforge.burnin_resolver import persist_pending_position, persist_pending_reject_label
from alphaforge.persistence import init_db, save_decision_evidence
from alphaforge.system_audit_store import bootstrap_audit_schema, ingest_audit_evidence


RUN_ID = "audit-run"
RELEASE_ID = "audit-release"
CAMPAIGN_ID = "audit-campaign"


def _source(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    path = tmp_path / "source.db"
    engine = init_db(f"sqlite+pysqlite:///{path}")
    with engine.begin() as conn:
        bootstrap_campaign_schema(conn)
        persist_burnin_run(
            conn,
            BurnInRun(
                burnin_run_id=RUN_ID,
                release_id=RELEASE_ID,
                git_commit="deadbeef",
                config_hash="cfg-hash",
                strategy_config_hash="strategy-hash",
                universe_hash="universe-hash",
                source_provenance={"provider": "PAPER_RUNTIME"},
                symbols=["BTCUSDT", "ETHUSDT"],
                intervals=["1m"],
            ),
        )
    engine.dispose()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return path, conn


def _decision(
    source: sqlite3.Connection,
    *,
    evidence_id: str,
    observation_id: str,
    signal_id: str,
    decision: str,
    symbol: str,
    side: str | None,
    entry: float | None,
    stop: float | None,
    target: float | None,
    reject_reason: str | None = None,
    diagnostics: dict | None = None,
) -> None:
    persist_burnin_observation(
        source,
        observation_id=observation_id,
        burnin_run_id=RUN_ID,
        release_id=RELEASE_ID,
        execution_mode="PAPER",
        observed_at="2026-09-22T16:00:00Z",
        symbol=symbol,
        interval="1m",
        regime="TRENDING",
        decision="ACCEPTED" if decision == "ACCEPT" else "REJECTED",
        lifecycle_state="POSITION_OPENED" if decision == "ACCEPT" else "SIGNAL_REJECTED",
        metrics={"signal_id": signal_id},
        source_provenance={"provider": "PAPER_RUNTIME", "runtime_instance_id": "runtime-audit"},
    )
    diag = {
        "observation_id": observation_id,
        "campaign_id": CAMPAIGN_ID,
        **(diagnostics or {}),
    }
    save_decision_evidence(
        source,
        evidence_id=evidence_id,
        run_id=RUN_ID,
        mode="PAPER",
        timestamp="2026-09-22T16:00:00Z",
        symbol=symbol,
        side=side,
        setup_type="TREND_CONTINUATION",
        regime="TRENDING",
        decision=decision,
        signal_id=signal_id,
        reject_reason=reject_reason,
        score=0.71,
        raw_rr=1.8,
        effective_rr=1.5,
        min_effective_rr=1.1,
        entry=entry,
        sl=stop,
        tp=target,
        spread_pct=0.0002,
        expected_slippage_pct=0.0002,
        fee_pct=0.0004,
        funding_rate_pct=0.0,
        latency_ms=50.0,
        liquidity_score=0.9,
        diagnostics_json=diag,
        reject_flags=(["LOW_SCORE", "LOW_EFFECTIVE_RR"] if decision == "REJECT" else []),
    )
    source.commit()


def _accepted(source: sqlite3.Connection) -> None:
    _decision(
        source,
        evidence_id="e-accept",
        observation_id="obs-accept",
        signal_id="sig-accept",
        decision="ACCEPT",
        symbol="BTCUSDT",
        side="LONG",
        entry=100.1,
        stop=99.0,
        target=102.0,
    )
    pending_id = persist_pending_position(
        source,
        trade_id="trade-accept",
        campaign_id=CAMPAIGN_ID,
        burnin_run_id=RUN_ID,
        signal_id="sig-accept",
        source_decision_id="sig-accept",
        decision_time="2026-09-22T16:00:00Z",
        symbol="BTCUSDT",
        side="LONG",
        setup_type="TREND_CONTINUATION",
        entry_time="2026-09-22T16:00:00Z",
        planned_entry=100.0,
        simulated_fill=100.1,
        stop=99.0,
        target=102.0,
        quantity=1.0,
        notional=100.1,
        entry_spread=0.01,
        entry_slippage=0.01,
        entry_fee=0.01,
        regime="TRENDING",
        source_provenance={"effective_rr_at_entry": 1.5},
    )
    persist_burnin_trade_outcome(
        source,
        outcome_id="tout_trade-accept",
        burnin_run_id=RUN_ID,
        release_id=RELEASE_ID,
        trade_id="trade-accept",
        symbol="BTCUSDT",
        regime="TRENDING",
        closed_at="2026-09-22T17:00:00Z",
        gross_r=1.2,
        gross_pnl=1.2,
        costs={
            "spread_cost": 0.01,
            "entry_slippage_cost": 0.01,
            "exit_slippage_cost": 0.01,
            "fee_cost": 0.01,
            "funding_cost": 0.0,
            "latency_cost": 0.0,
        },
        net_r=1.16,
        net_pnl=1.16,
        effective_rr_at_entry=1.5,
        realized_effective_rr=1.16,
        hold_duration_seconds=3600,
        mfe=1.4,
        mae=-0.2,
        exit_reason="TP_HIT",
        payload={
            "signal_id": "sig-accept",
            "pending_position_id": pending_id,
            "ambiguous_intrabar_sequence": False,
        },
    )
    source.commit()


def _reject(
    source: sqlite3.Connection,
    *,
    signal_id: str,
    reject_id: str,
    attributable: bool = True,
    legacy: bool = False,
    ambiguous: bool = False,
) -> None:
    subject = "LEGACY_SCANNER_SHADOW_CANDIDATE" if legacy else "CANONICAL_REJECT_CANDIDATE"
    diagnostics = {
        "geometry_status": "COMPLETE",
        "reject_execution_basis": "EXPECTED_FILL_RUNTIME_PARITY",
        "reject_quality_attributable": attributable,
        "all_failed_gates": ["LOW_SCORE", "LOW_EFFECTIVE_RR"],
        "failed_gate_evidence": [
            {"gate": "LOW_SCORE", "observed": 0.4, "threshold": 0.5},
            {"gate": "LOW_EFFECTIVE_RR", "observed": 0.9, "threshold": 1.1},
        ],
    }
    _decision(
        source,
        evidence_id="e-" + signal_id,
        observation_id="obs-" + signal_id,
        signal_id=signal_id,
        decision="REJECT",
        symbol="ETHUSDT",
        side="SHORT",
        entry=2500.0,
        stop=2520.0,
        target=2470.0,
        reject_reason="LOW_SCORE",
        diagnostics=diagnostics,
    )
    pending_id = persist_pending_reject_label(
        source,
        campaign_id=CAMPAIGN_ID,
        burnin_run_id=RUN_ID,
        reject_decision_id=reject_id,
        signal_id=signal_id,
        symbol="ETHUSDT",
        side="SHORT",
        decision_timestamp="2026-09-22T16:00:00Z",
        entry=2500.0,
        stop=2520.0,
        target=2470.0,
        horizon_seconds=60.0,
        horizon_bars=1,
        timeframe="1m",
        execution_cost_assumptions={
            "spread_cost": 0.01,
            "entry_slippage_cost": 0.0,
            "exit_slippage_cost": 0.01,
            "fee_cost": 0.01,
            "funding_cost": 0.0,
            "latency_cost": 0.0,
        },
        regime="TRENDING",
        reject_reason="LOW_SCORE",
        source_provenance={
            "reject_quality_attributable": attributable,
            "forward_label_subject": subject,
            "reject_execution_basis": "EXPECTED_FILL_RUNTIME_PARITY",
        },
    )
    assert pending_id
    persist_burnin_reject_outcome(
        source,
        reject_outcome_id="rout_" + reject_id,
        burnin_run_id=RUN_ID,
        release_id=RELEASE_ID,
        reject_reason="LOW_SCORE",
        symbol="ETHUSDT",
        regime="TRENDING",
        decision_time="2026-09-22T16:00:00Z",
        hypothetical_entry=2500.0,
        hypothetical_stop=2520.0,
        hypothetical_target=2470.0,
        forward_label="AMBIGUOUS" if ambiguous else "TP_BEFORE_SL",
        would_tp=False if ambiguous else True,
        would_sl=False,
        timeout=False,
        ambiguous=ambiguous,
        hypothetical_gross_r=1.5,
        hypothetical_net_r_after_costs=1.45,
        avoided_loss=0.0,
        missed_profit=1.45,
        execution_invalidated=False,
        evidence_horizon="2026-09-22T16:01:00Z",
        evidence_complete=not ambiguous,
        payload={
            "campaign_id": CAMPAIGN_ID,
            "burnin_run_id": RUN_ID,
            "reject_decision_id": reject_id,
            "pending_label_id": pending_id,
            "reject_quality_attributable": attributable,
            "forward_label_subject": subject,
            "reject_execution_basis": "EXPECTED_FILL_RUNTIME_PARITY",
            "execution_aligned": True,
            "effective_rr_at_decision": 0.9,
        },
    )
    source.commit()


def test_l0_ingests_accept_and_execution_aligned_reject_idempotently(tmp_path: Path) -> None:
    _path, source = _source(tmp_path)
    _accepted(source)
    _reject(source, signal_id="sig-reject", reject_id="reject-1")

    audit = sqlite3.connect(tmp_path / "audit.db")
    before_source_changes = source.total_changes
    first = ingest_audit_evidence(source, audit, burnin_run_id=RUN_ID)
    assert source.total_changes == before_source_changes
    second = ingest_audit_evidence(source, audit, burnin_run_id=RUN_ID)

    assert first["source_decisions"] == 2
    assert first["envelopes_added"] == 2
    assert first["outcomes_added"] == 2
    assert first["shadow_decisions_added"] == 1
    assert first["shadow_outcomes_added"] == 1
    assert second["envelopes_added"] == 0
    assert second["outcomes_added"] == 0
    assert second["shadow_decisions_added"] == 0
    assert second["shadow_outcomes_added"] == 0

    reject = audit.execute(
        "SELECT eligibility,authoritative,reject_reasons_json FROM shadow_decisions"
    ).fetchone()
    assert reject[0] == "EXECUTABLE_SHADOW"
    assert reject[1] == 1
    assert set(json.loads(reject[2])) == {"LOW_SCORE", "LOW_EFFECTIVE_RR"}

    outcomes = audit.execute(
        "SELECT outcome_kind,authoritative,net_r FROM audit_outcomes ORDER BY outcome_kind"
    ).fetchall()
    assert outcomes == [
        ("ACCEPTED_ACTUAL", 1, pytest.approx(1.16)),
        ("REJECT_SHADOW", 1, pytest.approx(1.45)),
    ]


def test_l0_non_attributable_legacy_shadow_is_diagnostic_not_authoritative(tmp_path: Path) -> None:
    _path, source = _source(tmp_path)
    _reject(
        source,
        signal_id="sig-legacy",
        reject_id="reject-legacy",
        attributable=False,
        legacy=True,
    )
    audit = sqlite3.connect(tmp_path / "audit.db")
    ingest_audit_evidence(source, audit, burnin_run_id=RUN_ID)

    decision = audit.execute(
        "SELECT eligibility,eligibility_reason,attributable,authoritative FROM shadow_decisions"
    ).fetchone()
    assert decision == (
        "DIAGNOSTIC_SHADOW",
        "NON_ATTRIBUTABLE_OR_LEGACY_SHADOW",
        0,
        0,
    )
    outcome = audit.execute(
        "SELECT attributable,authoritative FROM shadow_outcomes"
    ).fetchone()
    assert outcome == (0, 0)


def test_l0_missing_geometry_is_non_simulatable_and_no_trade_is_invented(tmp_path: Path) -> None:
    _path, source = _source(tmp_path)
    _decision(
        source,
        evidence_id="e-missing",
        observation_id="obs-missing",
        signal_id="sig-missing",
        decision="REJECT",
        symbol="BTCUSDT",
        side=None,
        entry=None,
        stop=None,
        target=None,
        reject_reason="MTF_NO_VALID_SETUP",
        diagnostics={"geometry_status": "UNAVAILABLE"},
    )
    audit = sqlite3.connect(tmp_path / "audit.db")
    ingest_audit_evidence(source, audit, burnin_run_id=RUN_ID)

    row = audit.execute(
        "SELECT eligibility,eligibility_reason FROM shadow_decisions"
    ).fetchone()
    assert row == ("NON_SIMULATABLE", "CANONICAL_GEOMETRY_UNAVAILABLE")
    assert audit.execute("SELECT COUNT(*) FROM shadow_outcomes").fetchone()[0] == 0


def test_l0_ambiguous_shadow_outcome_is_explicit_and_not_authoritative(tmp_path: Path) -> None:
    _path, source = _source(tmp_path)
    _reject(
        source,
        signal_id="sig-ambiguous",
        reject_id="reject-ambiguous",
        ambiguous=True,
    )
    audit = sqlite3.connect(tmp_path / "audit.db")
    ingest_audit_evidence(source, audit, burnin_run_id=RUN_ID)

    row = audit.execute(
        "SELECT ambiguous,evidence_complete,authoritative,forward_label FROM shadow_outcomes"
    ).fetchone()
    assert row == (1, 0, 0, "AMBIGUOUS")


def test_l0_immutable_tables_reject_updates_and_source_output_aliasing(tmp_path: Path) -> None:
    _path, source = _source(tmp_path)
    _accepted(source)
    audit = sqlite3.connect(tmp_path / "audit.db")
    ingest_audit_evidence(source, audit, burnin_run_id=RUN_ID)

    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_SYSTEM_AUDIT_EVIDENCE"):
        audit.execute(
            "UPDATE audit_decision_envelopes SET score=999 WHERE source_evidence_id='e-accept'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_SYSTEM_AUDIT_EVIDENCE"):
        audit.execute("DELETE FROM audit_outcomes")

    with pytest.raises(ValueError, match="separate"):
        ingest_audit_evidence(source, source, burnin_run_id=RUN_ID)


def test_l0_schema_bootstrap_never_adds_campaign_tables(tmp_path: Path) -> None:
    audit = sqlite3.connect(tmp_path / "audit-only.db")
    bootstrap_audit_schema(audit)
    tables = {row[0] for row in audit.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert {
        "audit_decision_envelopes",
        "audit_outcomes",
        "shadow_decisions",
        "shadow_outcomes",
    } <= tables
    assert "burnin_campaigns" not in tables
    assert "burnin_runs" not in tables
