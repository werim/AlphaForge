from __future__ import annotations

import pytest

from alphaforge.decision_invariant import (
    assert_pre_submit_invariant_parity,
    compare_pre_submit_invariants,
    project_pre_submit_invariant,
)


def _surface(**overrides):
    base = {
        "decision": "ACCEPTED",
        "primary_reject_reason": "",
        "all_failed_gates": [],
        "score": 8.75,
        "candidate_rr": 1.42,
        "executable_raw_rr": 1.31,
        "effective_rr": 1.24,
        "threshold_provenance": {
            "min_score": "CONFIG_REGISTRY:MIN_TRADE_SCORE",
            "min_effective_rr": "CONFIG_REGISTRY:MIN_EFFECTIVE_RR",
        },
        "execution_evidence_status": "COMPLETE",
        "portfolio_decision": {"accepted": True},
        "lifecycle_pre_submit_terminal_state": "ORDER_PLACED",
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "field,value",
    [
        ("decision", "REJECTED"),
        ("primary_reject_reason", "LOW_EFFECTIVE_RR"),
        ("all_failed_gates", ["LOW_EFFECTIVE_RR"]),
        ("score", 8.0),
        ("candidate_rr", 1.41),
        ("executable_raw_rr", 1.30),
        ("effective_rr", 1.23),
        ("threshold_provenance", {"min_effective_rr": "HARDCODED"}),
        ("execution_evidence_status", "INCOMPLETE"),
        ("portfolio_decision", {"accepted": False}),
        ("lifecycle_pre_submit_terminal_state", "SIGNAL_REJECTED"),
    ],
)
def test_each_protected_dimension_is_fail_closed(field, value):
    reference = _surface()
    drifted = _surface(**{field: value})
    expected_error = (
        "DECISION_PARITY_EVIDENCE_INCOMPLETE"
        if field == "execution_evidence_status" and value == "INCOMPLETE"
        else "DECISION_PARITY_MISMATCH"
    )
    with pytest.raises(ValueError, match=expected_error):
        assert_pre_submit_invariant_parity(reference, drifted)


def test_all_failed_gates_are_order_independent_but_complete():
    left = project_pre_submit_invariant(
        _surface(all_failed_gates=["HIGH_SPREAD", "LOW_EFFECTIVE_RR"])
    )
    right = project_pre_submit_invariant(
        _surface(all_failed_gates=["LOW_EFFECTIVE_RR", "HIGH_SPREAD"])
    )
    assert compare_pre_submit_invariants(left, right) == ()


def test_execution_and_portfolio_nested_evidence_are_projected():
    projected = project_pre_submit_invariant(
        _surface(
            all_failed_gates=[],
            execution_evidence_status="",
            execution_safety={
                "execution_evidence_status": "INCOMPLETE",
                "all_failed_gates": ["UNKNOWN_EXECUTION_CONTEXT"],
            },
            portfolio_decision={"accepted": False},
            primary_reject_reason="UNKNOWN_EXECUTION_CONTEXT",
            decision="REJECTED",
            lifecycle_pre_submit_terminal_state="SIGNAL_REJECTED",
        )
    )
    assert projected.failed_gates == ("UNKNOWN_EXECUTION_CONTEXT",)
    assert projected.execution_evidence_status == "INCOMPLETE"
    assert projected.portfolio_decision == "REJECT"


def test_backtest_paper_live_precheck_same_frozen_semantics_pass():
    frozen = _surface()
    backtest = dict(frozen)
    paper = dict(frozen)
    live_precheck = dict(frozen)
    result = assert_pre_submit_invariant_parity(backtest, paper, live_precheck)
    assert result.decision == "ACCEPT"
    assert result.lifecycle_pre_submit_terminal_state == "ORDER_PLACED"


def test_mode_specific_side_effects_are_outside_pre_submit_contract():
    # Submission/fill mechanics are intentionally excluded. BACKTEST may
    # simulate after this boundary; PAPER may persist a simulated order;
    # LIVE_PRECHECK must not submit. Those differences cannot excuse drift in
    # any protected pre-submit field.
    reference = _surface()
    paper = {**reference, "simulated_order_id": "paper-1"}
    live_precheck = {**reference, "exchange_submit_calls": 0}
    backtest = {**reference, "simulated_fill": 100.01}
    assert_pre_submit_invariant_parity(reference, paper, live_precheck, backtest)


def test_missing_protected_evidence_does_not_equal_complete_evidence():
    reference = _surface()
    incomplete = dict(reference)
    incomplete.pop("executable_raw_rr")
    with pytest.raises(ValueError, match="executable_raw_rr"):
        assert_pre_submit_invariant_parity(reference, incomplete)


def test_identically_missing_required_evidence_still_fails_closed():
    left = _surface(threshold_provenance={})
    right = _surface(threshold_provenance={})
    with pytest.raises(ValueError, match="DECISION_PARITY_EVIDENCE_INCOMPLETE"):
        assert_pre_submit_invariant_parity(left, right)


@pytest.mark.parametrize(
    "field,value",
    [
        ("score", None),
        ("candidate_rr", None),
        ("executable_raw_rr", None),
        ("effective_rr", None),
        ("execution_evidence_status", "INCOMPLETE"),
        ("portfolio_decision", None),
        ("lifecycle_pre_submit_terminal_state", ""),
    ],
)
def test_missing_required_authority_never_passes_by_symmetry(field, value):
    payload = _surface(**{field: value})
    with pytest.raises(ValueError, match="DECISION_PARITY_EVIDENCE_INCOMPLETE"):
        assert_pre_submit_invariant_parity(payload, payload)
