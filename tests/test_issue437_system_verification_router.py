from __future__ import annotations

import json

import pytest

from alphaforge.system_verification import (
    ModelAlias,
    RiskLevel,
    build_failure_packet,
    build_verification_manifest,
    plan_changed_files,
    route_failure,
)


def _plan(*paths: str):
    return plan_changed_files(
        paths,
        base_sha="a" * 40,
        head_sha="b" * 40,
    )


def test_docs_only_is_low_t0_and_no_llm() -> None:
    plan = _plan("docs/verification.md")
    assert plan.risk_level == RiskLevel.LOW
    assert plan.required_tiers == ("T0",)
    assert plan.recommended_model == ModelAlias.NO_LLM
    assert plan.llm_required_during_execution is False


@pytest.mark.parametrize(
    ("path", "required"),
    [
        ("src/alphaforge/execution.py", {"EXECUTION_SAFETY", "DECISION_PARITY"}),
        ("src/alphaforge/burnin_resolver.py", {"RESOLVER_INTEGRITY", "TIME_INTEGRITY"}),
        ("src/alphaforge/persistence.py", {"PERSISTENCE_ATOMICITY", "RESTART_RECOVERY"}),
    ],
)
def test_protected_high_risk_paths_require_t0_through_t3(
    path: str, required: set[str]
) -> None:
    plan = _plan(path)
    assert plan.risk_level == RiskLevel.HIGH
    assert plan.required_tiers == ("T0", "T1", "T2", "T3")
    assert required.issubset(set(plan.affected_invariants))
    assert plan.selected_tests


def test_live_order_boundary_is_critical_and_requires_release_tier() -> None:
    plan = _plan("src/alphaforge/order.py")
    assert plan.risk_level == RiskLevel.CRITICAL
    assert plan.required_tiers == ("T0", "T1", "T2", "T3", "T4")
    assert "LIVE_ORDER_BOUNDARY" in plan.affected_invariants
    assert plan.recommended_model == ModelAlias.HIGH_REASONING
    assert "tests/test_runtime_live_authorization.py" in plan.selected_tests


def test_unknown_production_code_escalates_fail_closed() -> None:
    plan = _plan("src/alphaforge/new_unmapped_engine.py")
    assert plan.risk_level == RiskLevel.HIGH
    assert plan.affected_invariants == ("UNKNOWN_PRODUCTION_CHANGE",)
    assert "tests/test_issue498_cross_component_mutations.py" in plan.selected_tests


def test_protected_verification_files_cannot_route_low() -> None:
    workflow = _plan(".github/workflows/test.yml")
    mutation = _plan("scripts/run_safety_mutations.py")
    assert workflow.risk_level == mutation.risk_level == RiskLevel.HIGH
    assert workflow.affected_invariants == mutation.affected_invariants == (
        "VERIFICATION_GATE_INTEGRITY",
    )


def test_generic_scanner_change_is_medium_not_suite_size_driven() -> None:
    plan = _plan("src/alphaforge/exchange_market_scanner.py")
    assert plan.risk_level == RiskLevel.MEDIUM
    assert plan.required_tiers == ("T0", "T1", "T2")
    assert plan.recommended_model == ModelAlias.MEDIUM_REASONING


def test_plan_identity_is_order_independent_and_deterministic() -> None:
    first = _plan("src/alphaforge/runtime.py", "tests/test_runtime.py")
    second = _plan("tests/test_runtime.py", "src/alphaforge/runtime.py")
    assert first == second
    assert first.plan_id == second.plan_id
    assert first.changed_files == tuple(sorted(first.changed_files))


def test_changed_test_is_in_selected_test_set() -> None:
    plan = _plan("tests/test_some_local_helper.py")
    assert "tests/test_some_local_helper.py" in plan.selected_tests


def test_path_escape_is_rejected() -> None:
    with pytest.raises(ValueError, match="outside repository"):
        _plan("../runtime.py")


def test_pass_path_routes_to_no_llm_and_never_gains_authority() -> None:
    route = route_failure({
        "deterministic_result": "PASS",
        "risk_level": "CRITICAL",
        "affected_invariants": ["LIVE_ORDER_BOUNDARY"],
        "failing_tests": ["x"] * 2000,
    })
    assert route.recommended_model == ModelAlias.NO_LLM
    assert route.llm_authoritative is False
    assert route.may_relax_safety is False
    assert route.may_authorize_live is False


def test_collection_failure_uses_cheap_triage() -> None:
    route = route_failure({
        "deterministic_result": "ERROR",
        "failure_class": "COLLECTION",
        "risk_level": "HIGH",
    })
    assert route.recommended_model == ModelAlias.CHEAP_TRIAGE


def test_local_semantic_failure_uses_medium_reasoning() -> None:
    route = route_failure({
        "deterministic_result": "FAIL",
        "risk_level": "MEDIUM",
        "affected_invariants": ["DECISION_SEMANTICS"],
    })
    assert route.recommended_model == ModelAlias.MEDIUM_REASONING


def test_cross_boundary_failure_uses_high_reasoning() -> None:
    route = route_failure({
        "deterministic_result": "FAIL",
        "risk_level": "HIGH",
        "affected_invariants": ["PERSISTENCE_ATOMICITY", "RESTART_RECOVERY"],
    })
    assert route.recommended_model == ModelAlias.HIGH_REASONING


def test_nondeterministic_crash_is_max_reasoning_eligible() -> None:
    route = route_failure({
        "deterministic_result": "FAIL",
        "risk_level": "HIGH",
        "failure_class": "SIGKILL_RESTART_RACE",
        "nondeterministic": True,
    })
    assert route.recommended_model == ModelAlias.MAX_REASONING


def test_failure_packet_is_bounded_and_keeps_identity() -> None:
    packet = build_failure_packet({
        "deterministic_result": "FAIL",
        "risk_level": "HIGH",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "config_hash": "c" * 64,
        "failed_invariant": "PERSISTENCE_ATOMICITY",
        "failing_tests": ["z", "a"],
        "changed_files": ["src/alphaforge/runtime.py"],
        "expected": {"status": "PENDING"},
        "actual": {"status": "DUPLICATE"},
        "traceback": "T" * 9000,
        "persisted_evidence": {"rows": "E" * 9000},
    })
    assert packet["base_sha"] == "a" * 40
    assert packet["head_sha"] == "b" * 40
    assert packet["failing_tests"] == ("a", "z")
    assert len(packet["traceback"]) <= 4000
    assert len(packet["persisted_evidence"]) <= 2000
    assert packet["routing"]["llm_authoritative"] is False


def test_manifest_keeps_deterministic_gate_authoritative() -> None:
    plan = _plan("src/alphaforge/execution.py")
    manifest = build_verification_manifest(
        plan,
        deterministic_result="FAIL",
        tests_executed=3,
        invariants_passed=1,
        invariants_total=2,
        config_hash="c" * 64,
    )
    assert manifest["deterministic_result"] == "FAIL"
    assert manifest["llm_required_during_execution"] is False
    assert manifest["llm_authoritative"] is False
    assert manifest["may_relax_safety"] is False
    assert manifest["may_authorize_live"] is False


def test_manifest_rejects_impossible_counts() -> None:
    plan = _plan("src/alphaforge/execution.py")
    with pytest.raises(ValueError, match="cannot exceed"):
        build_verification_manifest(
            plan,
            deterministic_result="PASS",
            tests_executed=1,
            invariants_passed=2,
            invariants_total=1,
        )


def test_plan_is_json_serializable() -> None:
    payload = _plan("src/alphaforge/runtime.py").to_dict()
    rendered = json.dumps(payload, sort_keys=True)
    assert '"risk_level": "HIGH"' in rendered
    assert '"recommended_model": "HIGH_REASONING"' in rendered
