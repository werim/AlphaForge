from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import textwrap

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


def test_storage_authority_routes_both_retention_regressions() -> None:
    plan = _plan("src/alphaforge/storage_policy.py")
    assert "tests/test_issue620_storage_policy.py" in plan.selected_tests
    assert "tests/test_issue622_stats_only_retention.py" in plan.selected_tests


@pytest.mark.parametrize("module", ["storage_calibration", "storage_workload"])
def test_storage_source_only_change_selects_existing_regressions(module) -> None:
    root = Path(__file__).resolve().parents[1]
    source = f"src/alphaforge/{module}.py"
    assert (root / source).is_file()
    plan = _plan(source)
    assert plan.risk_level == RiskLevel.HIGH
    assert plan.required_tiers == ("T0", "T1", "T2", "T3")
    assert "UNKNOWN_PRODUCTION_CHANGE" not in plan.affected_invariants
    assert {"STORAGE_RETENTION", "PERSISTENCE_ATOMICITY", "RESTART_RECOVERY"} <= set(plan.affected_invariants)
    assert "tests/test_storage_calibration.py" in plan.selected_tests
    assert all((root / path).is_file() for path in plan.selected_tests)
    # These tests belong to unmerged637; dev must not depend on them.
    assert not {"tests/test_storage_measurement.py", "tests/test_storage_host_identity.py",
                "tests/test_storage_admission_contract.py"} & set(plan.selected_tests)


def test_storage_plan_deduplicates_source_and_explicit_test_changes() -> None:
    paths = ["src/alphaforge/storage_workload.py", "src/alphaforge/storage_calibration.py",
             "tests/test_storage_calibration.py", "src/alphaforge/storage_workload.py"]
    first = _plan(*paths)
    assert first == _plan(*reversed(paths))
    assert first.selected_tests == tuple(sorted(set(first.selected_tests)))
    assert first.selected_tests.count("tests/test_storage_calibration.py") == 1
    assert first.plan_id == _plan(*sorted(set(paths))).plan_id


@pytest.mark.parametrize(("module", "suites", "invariants"), [
    ("burnin_ops", {"test_phase9_burnin_ops.py", "test_issue631_reject_recovery.py"},
     {"BURNIN_OPERATIONS", "RECONCILIATION_INTEGRITY", "RESTART_RECOVERY"}),
    ("burnin_campaign", {"test_phase8_burnin_campaign.py", "test_phase9_burnin_ops.py",
                         "test_issue581_burnin_dynamic_universe.py", "test_issue631_reject_recovery.py"},
     {"BURNIN_CAMPAIGN", "BURNIN_OPERATIONS", "PERSISTENCE_ATOMICITY",
      "RESTART_RECOVERY", "RESOLVER_INTEGRITY"}),
])
def test_burnin_source_only_selects_actual_regressions(module, suites, invariants):
    root = Path(__file__).resolve().parents[1]
    source = f"src/alphaforge/{module}.py"
    assert (root / source).is_file()
    plan = _plan(source)
    assert plan.changed_files == (source,)
    assert plan.risk_level == RiskLevel.HIGH
    assert plan.required_tiers == ("T0", "T1", "T2", "T3")
    assert set(plan.affected_invariants) == invariants
    assert suites <= {Path(path).name for path in plan.selected_tests}
    assert all((root / path).is_file() for path in plan.selected_tests)


def test_burnin_source_only_union_is_deduplicated_and_order_independent():
    paths = ("src/alphaforge/burnin_ops.py", "src/alphaforge/burnin_campaign.py")
    first = _plan(*paths)
    assert first == _plan(*reversed(paths)) == _plan(*paths, paths[0])
    assert first.plan_id == _plan(*reversed(paths)).plan_id
    assert first.selected_tests == tuple(sorted(set(_plan(paths[0]).selected_tests)
                                                | set(_plan(paths[1]).selected_tests)))
    assert first.selected_tests.count("tests/test_issue631_reject_recovery.py") == 1


def test_qualification_source_only_selects_evidence_and_promotion_regressions():
    root = Path(__file__).resolve().parents[1]
    source = "src/alphaforge/burnin_qualification.py"
    plan = _plan(source)
    assert plan.changed_files == (source,)
    assert plan.risk_level == RiskLevel.HIGH
    assert plan.required_tiers == ("T0", "T1", "T2", "T3")
    assert set(plan.affected_invariants) == {"READINESS_EVIDENCE", "FAIL_CLOSED_PROMOTION"}
    assert set(plan.selected_tests) == {
        "tests/test_burnin_qualification_evidence_integrity.py",
        "tests/test_issue547_qualification_evidence.py",
        "tests/test_issue344_qualification_writer_lifetime.py",
        "tests/test_phase7_qualification.py",
        "tests/test_live_readiness.py",
    }
    assert all((root / path).is_file() for path in plan.selected_tests)


def test_qualification_combines_existing_source_routes_without_order_or_duplicates():
    paths = ("src/alphaforge/burnin_qualification.py", "src/alphaforge/burnin_campaign.py",
             "src/alphaforge/burnin_ops.py", "src/alphaforge/storage_policy.py")
    plan = _plan(*paths)
    assert plan == _plan(*reversed(paths), paths[0])
    assert plan.plan_id == _plan(*reversed(paths)).plan_id
    expected = {test for source in paths for test in _plan(source).selected_tests}
    assert plan.selected_tests == tuple(sorted(expected))
    assert plan.selected_tests.count("tests/test_live_readiness.py") == 1


@pytest.mark.parametrize(("source", "risk", "invariants", "suites"), [
    ("storage_policy.py", RiskLevel.HIGH,
     {"STORAGE_RETENTION", "PERSISTENCE_ATOMICITY", "RESTART_RECOVERY"},
     {"test_issue620_storage_policy.py", "test_issue622_stats_only_retention.py",
      "test_issue550_atomic_reject_persistence.py", "test_issue450_sqlite_concurrency.py",
      "test_issue421_full_chain_replay.py"}),
    ("runtime.py", RiskLevel.HIGH, {"RUNTIME_AUTHORITY", "RESTART_RECOVERY"},
     {"test_runtime.py", "test_issue421_full_chain_replay.py"}),
    ("execution.py", RiskLevel.HIGH, {"EXECUTION_SAFETY", "DECISION_PARITY"},
     {"test_issue421_execution_safety.py", "test_execution_cost_semantics.py",
      "test_issue490_protected_mode_parity.py", "test_issue421_decision_invariant.py"}),
    ("order.py", RiskLevel.CRITICAL,
     {"EXECUTION_SAFETY", "KILL_SWITCH_AUTHORIZATION", "LIVE_ORDER_BOUNDARY"},
     {"test_issue421_execution_safety.py", "test_execution_cost_semantics.py",
      "test_runtime_live_authorization.py", "test_trading_modes.py", "test_phase6_release_gates.py"}),
])
def test_storage_mapping_preserves_existing_authority_routes(source, risk, invariants, suites):
    plan = _plan(f"src/alphaforge/{source}")
    assert plan.risk_level == risk
    assert set(plan.affected_invariants) == invariants
    assert {Path(path).name for path in plan.selected_tests} == suites


@pytest.mark.parametrize("module", [
    "storage_calibration", "burnin_ops", "burnin_campaign", "burnin_qualification",
])
@pytest.mark.parametrize(("result", "reason"), [
    ("FAIL", "STORAGE_ADMISSION_EVIDENCE_REJECTED"),
    ("ERROR", "MEASUREMENT_EVIDENCE_MISSING"),
    ("BLOCKED", "PHYSICAL_CAPACITY_UNVERIFIED"),
])
def test_routing_cannot_promote_absent_failed_or_unverified_evidence(module, result, reason):
    plan = _plan(f"src/alphaforge/{module}.py")
    manifest = build_verification_manifest(plan, deterministic_result=result,
        tests_executed=0, invariants_passed=0, invariants_total=len(plan.affected_invariants))
    packet = build_failure_packet({"deterministic_result": result, "risk_level": plan.risk_level,
        "affected_invariants": plan.affected_invariants, "failed_invariant": reason})
    assert manifest["deterministic_result"] == packet["routing"]["deterministic_result"] == result
    assert packet["failed_invariant"] == reason
    for authority in (manifest, packet["routing"]):
        assert authority["llm_authoritative"] is False
        assert authority["may_relax_safety"] is False
        assert authority["may_authorize_live"] is False
    assert "admission_eligible" not in manifest


def _routed_ci_script():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/test.yml").read_text()
    step = workflow.split("      - name: Routed PR verification\n", 1)[1].split("      - name:", 1)[0]
    script = textwrap.dedent(step.split("python - <<'PY'\n", 1)[1].split("          PY", 1)[0])
    return compile(script, ".github/workflows/test.yml:Routed PR verification", "exec")


@pytest.mark.parametrize("sources", [
    ("storage_calibration", "storage_workload"),
    ("burnin_ops",),
    ("burnin_campaign",),
    ("burnin_ops", "burnin_campaign"),
    ("burnin_qualification",),
])
def test_real_ci_step_consumes_source_only_selection(tmp_path, monkeypatch, sources):
    # Execute the actual workflow's embedded Python, intercepting only pytest
    # invocation. No second CI command/test mapping is maintained here.
    script = _routed_ci_script()
    plan = _plan(*(f"src/alphaforge/{module}.py" for module in sources))
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts/system-verification-plan.json").write_text(json.dumps(plan.to_dict()))
    commands = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: commands.append((argv, kwargs)))
    exec(script, {})
    assert len(commands) == 1
    argv, kwargs = commands[0]
    assert argv[:4] == [sys.executable, "-m", "pytest", "-q"]
    always_covered = {"tests/test_ci_branch_contract.py",
                      "tests/test_issue421_decision_invariant.py",
                      "tests/test_issue498_cross_component_mutations.py"}
    assert argv[4:] == [path for path in plan.selected_tests if path not in always_covered]
    assert kwargs == {"check": True}


def test_real_ci_missing_selected_test_path_fails_instead_of_passing(tmp_path, monkeypatch):
    script = _routed_ci_script()
    payload = _plan("src/alphaforge/burnin_qualification.py").to_dict()
    payload["selected_tests"] = ["tests/missing_qualification_evidence.py"]
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts/system-verification-plan.json").write_text(json.dumps(payload))
    monkeypatch.chdir(tmp_path)
    # Exercise actual pytest and workflow check=True, not a mocked exit status.
    with pytest.raises(subprocess.CalledProcessError) as error:
        exec(script, {})
    assert error.value.returncode == 4  # pytest usage error: selected file not found


@pytest.mark.parametrize("result", [None, "", "UNKNOWN", "SKIPPED"])
def test_qualification_missing_or_skipped_verification_cannot_be_pass(result):
    plan = _plan("src/alphaforge/burnin_qualification.py")
    with pytest.raises(ValueError, match="deterministic_result"):
        build_verification_manifest(plan, deterministic_result=result,
            tests_executed=0, invariants_passed=0, invariants_total=len(plan.affected_invariants))
    route = route_failure({"deterministic_result": result, "risk_level": plan.risk_level})
    assert route.deterministic_result != "PASS"
    assert route.recommended_model != ModelAlias.NO_LLM
    assert route.llm_authoritative is route.may_relax_safety is route.may_authorize_live is False


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


@pytest.mark.parametrize("source", ["backtest_order.py", "src/alphaforge/realized_accounting.py",
    "src/alphaforge/burnin_resolver.py", "src/alphaforge/portfolio_risk.py",
    "src/alphaforge/dashboard/backtest_control.py", "src/alphaforge/historical_validation.py",
    "src/alphaforge/walk_forward.py"])
def test_realized_accounting_source_only_selects_sql_cash_regressions(source):
    plan = _plan(source)
    assert plan.risk_level == RiskLevel.HIGH
    assert plan.required_tiers == ("T0", "T1", "T2", "T3")
    assert "tests/test_issue646_legacy_realized_accounting.py" in plan.selected_tests


def test_quality_gate_source_only_selects_unit_and_authority_contract():
    plan = _plan("backtest_order.py")
    assert plan.risk_level == RiskLevel.HIGH
    assert "tests/test_quality_gate_accounting_integrity.py" in plan.selected_tests


@pytest.mark.parametrize("source", ["src/alphaforge/historical_validation.py", "src/alphaforge/walk_forward.py"])
def test_walk_forward_source_only_selects_cash_and_holdout_regressions(source):
    plan = _plan(source)
    assert plan.risk_level == RiskLevel.HIGH
    assert "tests/test_walk_forward_cash_integrity.py" in plan.selected_tests
    assert "tests/test_issue506_walk_forward.py" in plan.selected_tests


@pytest.mark.parametrize("extra_sources", [[], ["storage_measurement", "storage_volume"]])
def test_storage_plan_deduplicates_with_real_measurement_sources(extra_sources) -> None:
    paths = ["src/alphaforge/storage_workload.py", "src/alphaforge/storage_calibration.py",
             "tests/test_storage_calibration.py", "src/alphaforge/storage_workload.py"]
    paths.extend(f"src/alphaforge/{module}.py" for module in extra_sources)
    first = _plan(*paths)
    assert first == _plan(*reversed(paths))
    assert first.selected_tests == tuple(sorted(set(first.selected_tests)))
    assert first.selected_tests.count("tests/test_storage_calibration.py") == 1
    assert first.plan_id == _plan(*sorted(set(paths))).plan_id


@pytest.mark.parametrize("module", ["storage_measurement", "storage_volume"])
def test_real_storage_source_only_change_selects_capacity_identity_and_admission_suites(module):
    root = Path(__file__).resolve().parents[1]
    source = f"src/alphaforge/{module}.py"
    assert (root / source).is_file()
    plan = _plan(source)
    assert plan.risk_level == RiskLevel.HIGH
    assert plan.required_tiers == ("T0", "T1", "T2", "T3")
    assert "UNKNOWN_PRODUCTION_CHANGE" not in plan.affected_invariants
    assert {"STORAGE_RETENTION", "PERSISTENCE_ATOMICITY", "RESTART_RECOVERY"} <= set(plan.affected_invariants)
    assert {"tests/test_storage_measurement.py", "tests/test_storage_host_identity.py",
            "tests/test_storage_admission_contract.py"} <= set(plan.selected_tests)
    assert all((root / path).is_file() for path in plan.selected_tests)


@pytest.mark.parametrize(("result", "reason"), [
    ("FAIL", "STORAGE_ADMISSION_EVIDENCE_REJECTED"),
    ("ERROR", "MEASUREMENT_EVIDENCE_MISSING"),
    ("BLOCKED", "PHYSICAL_CAPACITY_UNVERIFIED"),
])
@pytest.mark.parametrize("module", ["storage_calibration", "storage_measurement", "storage_volume"])
def test_storage_routing_cannot_promote_absent_failed_or_unverified_evidence(result, reason, module):
    plan = _plan(f"src/alphaforge/{module}.py")
    manifest = build_verification_manifest(plan, deterministic_result=result,
        tests_executed=0, invariants_passed=0, invariants_total=len(plan.affected_invariants))
    packet = build_failure_packet({"deterministic_result": result, "risk_level": plan.risk_level,
        "affected_invariants": plan.affected_invariants, "failed_invariant": reason})
    assert manifest["deterministic_result"] == packet["routing"]["deterministic_result"] == result
    assert packet["failed_invariant"] == reason
    for authority in (manifest, packet["routing"]):
        assert authority["llm_authoritative"] is False
        assert authority["may_relax_safety"] is False
        assert authority["may_authorize_live"] is False
    assert "admission_eligible" not in manifest


@pytest.mark.parametrize(("sources", "required_suites"), [
    (["storage_calibration", "storage_workload"], {"tests/test_storage_calibration.py"}),
    (["storage_measurement", "storage_volume"], {"tests/test_storage_measurement.py",
       "tests/test_storage_host_identity.py", "tests/test_storage_admission_contract.py"}),
])
def test_real_ci_step_consumes_storage_source_only_selection(tmp_path, monkeypatch, sources, required_suites):
    # Execute the actual workflow's embedded Python, intercepting only pytest
    # invocation. No second CI command/test mapping is maintained here.
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/test.yml").read_text()
    step = workflow.split("      - name: Routed PR verification\n", 1)[1].split("      - name:", 1)[0]
    script = textwrap.dedent(step.split("python - <<'PY'\n", 1)[1].split("          PY", 1)[0])
    plan = _plan(*(f"src/alphaforge/{module}.py" for module in sources))
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts/system-verification-plan.json").write_text(json.dumps(plan.to_dict()))
    commands = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: commands.append((argv, kwargs)))
    exec(compile(script, ".github/workflows/test.yml:Routed PR verification", "exec"), {})
    assert len(commands) == 1
    argv, kwargs = commands[0]
    assert argv[:4] == [sys.executable, "-m", "pytest", "-q"]
    assert required_suites <= set(argv[4:])
    assert set(argv[4:]) <= set(plan.selected_tests)
    assert kwargs == {"check": True}
