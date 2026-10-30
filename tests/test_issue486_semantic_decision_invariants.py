from __future__ import annotations

import pytest

from alphaforge.decision_invariant import (
    assert_pre_submit_invariant_parity,
    assert_pre_submit_semantics,
    validate_pre_submit_semantics,
)


def _accept(**overrides):
    payload = {
        "decision": "ACCEPT",
        "primary_reject_reason": "",
        "all_failed_gates": [],
        "score": 0.73,
        "candidate_rr": 2.0,
        "executable_raw_rr": 1.88,
        "remaining_execution_penalty": 0.08,
        "effective_rr": 1.80,
        "geometry_status": "COMPLETE",
        "entry": 100.0,
        "sl": 95.0,
        "tp": 110.0,
        "threshold_provenance": {
            "min_score": "CONFIG_REGISTRY:MIN_TRADE_SCORE",
            "min_effective_rr": "CONFIG_REGISTRY:MIN_EFFECTIVE_RR",
        },
        "execution_evidence_status": "COMPLETE",
        "portfolio_decision": "ACCEPT",
        "lifecycle_pre_submit_terminal_state": "ORDER_ACCEPTED",
    }
    payload.update(overrides)
    return payload


def _reject(**overrides):
    payload = _accept(
        decision="REJECT",
        primary_reject_reason="LOW_EFFECTIVE_RR",
        all_failed_gates=["LOW_EFFECTIVE_RR"],
        effective_rr=1.0,
        executable_raw_rr=1.08,
        failed_gate_evidence=[
            {"gate": "LOW_EFFECTIVE_RR", "observed": 1.0, "threshold": 1.1}
        ],
        portfolio_decision="REJECT",
        lifecycle_pre_submit_terminal_state="SIGNAL_REJECTED",
    )
    payload.update(overrides)
    return payload


def test_effective_rr_arithmetic_must_be_internally_consistent():
    bad = _accept(effective_rr=1.79)
    with pytest.raises(ValueError, match="EFFECTIVE_RR_ARITHMETIC_MISMATCH"):
        assert_pre_submit_semantics(bad)


def test_identically_wrong_surfaces_do_not_pass_parity():
    bad = _accept(effective_rr=1.79)
    with pytest.raises(ValueError, match="EFFECTIVE_RR_ARITHMETIC_MISMATCH"):
        assert_pre_submit_invariant_parity(bad, dict(bad), dict(bad))


def test_unavailable_geometry_cannot_leak_authoritative_rr():
    bad = _accept(geometry_status="GUIDED_CANDIDATE_UNAVAILABLE")
    with pytest.raises(
        ValueError, match="UNAVAILABLE_GEOMETRY_HAS_AUTHORITATIVE_VALUES"
    ):
        assert_pre_submit_semantics(bad)


def test_gate_observation_must_equal_authoritative_value():
    bad = _reject(
        failed_gate_evidence=[
            {"gate": "LOW_EFFECTIVE_RR", "observed": 0.9, "threshold": 1.1}
        ]
    )
    with pytest.raises(ValueError, match="GATE_OBSERVED_VALUE_MISMATCH"):
        assert_pre_submit_semantics(bad)


def test_gate_evidence_set_must_equal_recorded_failed_gate_set():
    bad = _reject(
        all_failed_gates=["LOW_EFFECTIVE_RR", "LOW_SCORE"],
        failed_gate_evidence=[
            {"gate": "LOW_EFFECTIVE_RR", "observed": 1.0, "threshold": 1.1}
        ],
    )
    with pytest.raises(ValueError, match="FAILED_GATE_SET_MISMATCH"):
        assert_pre_submit_semantics(bad)


def test_accept_with_failed_gate_is_impossible():
    bad = _accept(all_failed_gates=["LOW_SCORE"])
    with pytest.raises(ValueError, match="ACCEPT_WITH_FAILED_GATES"):
        assert_pre_submit_semantics(bad)


def test_reject_requires_reason_and_failed_gate():
    bad = _accept(
        decision="REJECT",
        portfolio_decision="REJECT",
        lifecycle_pre_submit_terminal_state="SIGNAL_REJECTED",
    )
    violations = validate_pre_submit_semantics(bad)
    codes = {item.code for item in violations}
    assert "REJECT_WITHOUT_PRIMARY_REASON" in codes
    assert "REJECT_WITHOUT_FAILED_GATE" in codes


def test_primary_reject_reason_must_be_one_of_failed_gates():
    bad = _reject(primary_reject_reason="LOW_SCORE")
    with pytest.raises(ValueError, match="PRIMARY_REJECT_REASON_NOT_FAILED"):
        assert_pre_submit_semantics(bad)


def test_expected_fill_runtime_parity_reject_checks_entry_and_double_counting():
    bad = _reject(
        source_provenance={
            "reject_execution_basis": "EXPECTED_FILL_RUNTIME_PARITY",
            "reject_quality_attributable": True,
            "executable_entry": 100.1,
            "hypothetical_entry": 100.0,
            "executable_raw_rr": 1.08,
            "remaining_execution_penalty": 0.08,
            "effective_rr_at_decision": 1.0,
            "entry_slippage_embedded_in_fill": True,
        },
        execution_cost_assumptions={"entry_slippage_cost": 0.02},
    )
    violations = validate_pre_submit_semantics(bad)
    codes = {item.code for item in violations}
    assert "EXPECTED_FILL_ENTRY_PARITY_MISMATCH" in codes
    assert "ENTRY_SLIPPAGE_DOUBLE_COUNT" in codes


def test_attributable_expected_fill_parity_requires_complete_authority():
    bad = _reject(
        source_provenance={
            "reject_execution_basis": "EXPECTED_FILL_RUNTIME_PARITY",
            "reject_quality_attributable": True,
            "executable_entry": 100.0,
        }
    )
    with pytest.raises(
        ValueError, match="ATTRIBUTABLE_REJECT_PARITY_EVIDENCE_INCOMPLETE"
    ):
        assert_pre_submit_semantics(bad)


def test_valid_expected_fill_runtime_parity_payload_passes():
    good = _reject(
        source_provenance={
            "reject_execution_basis": "EXPECTED_FILL_RUNTIME_PARITY",
            "reject_quality_attributable": True,
            "executable_entry": 100.0,
            "hypothetical_entry": 100.0,
            "executable_raw_rr": 1.08,
            "remaining_execution_penalty": 0.08,
            "effective_rr_at_decision": 1.0,
            "entry_slippage_embedded_in_fill": True,
        },
        execution_cost_assumptions={"entry_slippage_cost": 0.0},
    )
    assert validate_pre_submit_semantics(good) == ()
