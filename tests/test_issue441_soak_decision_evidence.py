from __future__ import annotations

import asyncio

from sqlalchemy import text

from alphaforge.autonomous_qualification import AutonomousQualificationHarness


def _soak_context(tmp_path):
    harness = AutonomousQualificationHarness(
        mode="SOAK",
        output_root=tmp_path,
        soak_hours=6,
        sleep=lambda _seconds: None,
    )
    ctx = harness._new_context(
        "issue441-decision-evidence",
        harness._provider({"fault": None}),
        qualification_targets=True,
    )
    asyncio.run(ctx.runtime._run_reconciliation_once())
    return harness, ctx


def test_soak_decision_probe_persists_same_run_canonical_evidence_without_execution(tmp_path):
    harness, ctx = _soak_context(tmp_path)
    try:
        harness._run_soak_decision_evidence_probe(ctx)
        evidence = harness._soak_decision_evidence(ctx)
        checks = harness._soak_decision_evidence_checks(ctx, evidence)

        assert harness._soak_decision_probe_error is None
        assert evidence["canonical_decisions"] >= 1
        assert evidence["rejected_decisions"] >= 1
        assert evidence["decision_evidence"] >= evidence["canonical_decisions"]
        assert evidence["order_decisions"] >= 1
        assert evidence["rejected_signal_reviews"] >= 1
        assert evidence["pending_reject_labels"] >= 1
        assert evidence["qualification_snapshots"] >= 1
        assert evidence["complete_mtf_regime_mismatches"] == 0
        assert ctx.runtime.metrics.executions == 0
        assert all(checks.values())
    finally:
        harness._terminalize(ctx)
        harness.close()


def test_zero_decision_evidence_cannot_satisfy_full_system_soak_gate(tmp_path):
    harness, ctx = _soak_context(tmp_path)
    try:
        evidence = harness._soak_decision_evidence(ctx)
        checks = harness._soak_decision_evidence_checks(ctx, evidence)

        assert evidence["canonical_decisions"] == 0
        assert evidence["decision_evidence"] == 0
        assert evidence["order_decisions"] == 0
        assert checks["canonical_decision_evidence_nonzero"] is False
        assert checks["decision_evidence_persisted"] is False
        assert checks["order_decision_pipeline_exercised"] is False
        assert checks["decision_probe_no_submit"] is False
    finally:
        harness._terminalize(ctx)
        harness.close()


def test_soak_gate_detects_complete_mtf_regime_mismatch(tmp_path):
    harness, ctx = _soak_context(tmp_path)
    try:
        harness._run_soak_decision_evidence_probe(ctx)
        with harness.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE burnin_observations SET regime='CHOPPY' "
                    "WHERE burnin_run_id=:bid AND decision='REJECTED'"
                ),
                {"bid": ctx.burnin_run_id},
            )

        evidence = harness._soak_decision_evidence(ctx)
        checks = harness._soak_decision_evidence_checks(ctx, evidence)

        assert evidence["complete_mtf_regime_mismatches"] >= 1
        assert checks["canonical_regime_evidence_consistent"] is False
    finally:
        harness._terminalize(ctx)
        harness.close()
