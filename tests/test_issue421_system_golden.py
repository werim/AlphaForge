from __future__ import annotations

import json
from pathlib import Path

import pytest

from alphaforge.decision_invariant import project_pre_submit_invariant

ROOT = Path(__file__).parent / "fixtures" / "system_golden"
REQUIRED = ["profitable_long","profitable_short","losing_accept","correct_reject","false_reject","multi_gate_reject","low_effective_rr","high_spread","high_slippage","thin_liquidity","stale_candle","future_candle","regime_mismatch","mtf_conflict","ambiguous_tp_sl","incomplete_geometry","partial_fill","execution_context_unavailable","portfolio_overexposure","provider_outage","restart_replay","orphan_position","delayed_resolver"]
CHAIN = [
    "market_event", "decision", "failed_gates", "geometry", "expected_fill",
    "effective_rr", "portfolio_state", "persistence", "resolver", "readiness", "audit",
]


def _load(name: str) -> dict:
    return json.loads((ROOT / f"{name}.json").read_text())


def test_p2a_has_exact_canonical_scenario_pack():
    actual = sorted(p.stem for p in ROOT.glob("*.json"))
    assert actual == sorted(REQUIRED)


@pytest.mark.parametrize("name", REQUIRED)
def test_p2a_each_frozen_fixture_declares_complete_chain(name):
    fixture = _load(name)
    assert fixture["schema_version"] == "system_golden.v1"
    assert fixture["scenario"] == name
    assert fixture["mode"] == "PAPER"
    assert fixture["frozen_market_event"]["closed_candle"] is True
    assert fixture["expected"]["full_chain"] == CHAIN
    assert fixture["expected"]["decision"] in {"ACCEPT", "REJECT"}


@pytest.mark.parametrize("name", REQUIRED)
def test_p2a_fixture_decision_projects_through_production_invariant(name):
    """Golden decision semantics must be consumable by the production projector.

    This deliberately does not reimplement score/RR thresholds. P2-A freezes
    scenario identity and the required end-to-end surfaces; existing production
    regression suites remain the authority for each gate/resolver/readiness
    implementation until the deterministic full-chain replay work in P2-D.
    """
    fixture = _load(name)
    decision = fixture["expected"]["decision"]
    payload = {
        "decision": decision,
        "primary_reject_reason": "" if decision == "ACCEPT" else name.upper(),
        "failed_gates": [] if decision == "ACCEPT" else [name.upper()],
        "score": 0.5,
        "candidate_rr": 1.5,
        "executable_raw_rr": 1.4,
        "effective_rr": 1.2,
        "threshold_provenance": {"fixture_schema": fixture["schema_version"]},
        "execution_evidence_status": "COMPLETE",
        "portfolio_decision": decision,
        "lifecycle_pre_submit_terminal_state": (
            "ORDER_ACCEPTED" if decision == "ACCEPT" else "SIGNAL_REJECTED"
        ),
    }
    projected = project_pre_submit_invariant(payload)
    assert projected.decision == decision
    assert projected.execution_evidence_status == "COMPLETE"
    assert projected.lifecycle_pre_submit_terminal_state
