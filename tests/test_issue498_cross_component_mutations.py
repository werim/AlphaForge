from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_safety_mutations.py"
SPEC = importlib.util.spec_from_file_location("run_safety_mutations", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
mutations = MODULE.mutations


def test_cross_component_safety_mutations_are_ci_protected() -> None:
    cases = {case.name: case for case in mutations()}
    required = {
        "risk_scale_not_applied": (
            "src/alphaforge/runtime.py",
            "tests/test_issue489_risk_scale_sizing.py::test_paper_softened_wide_stop_executes_scaled_notional_and_quantity",
        ),
        "planned_entry_stop_basis": (
            "src/alphaforge/order.py",
            "tests/test_issue492_executable_stop_risk.py::test_trade_quality_uses_expected_fill_not_planned_entry_for_stop_gate",
        ),
        "missing_execution_metrics_pass": (
            "src/alphaforge/burnin_qualification.py",
            "tests/test_phase7_qualification.py::test_execution_metrics_one_missing_mandatory_field_blocks_canary",
        ),
        "missing_drawdown_coalesced_resolved": (
            "src/alphaforge/burnin_qualification.py",
            "tests/test_phase7_qualification.py::test_synthetic_zero_drawdown_placeholder_is_insufficient",
        ),
        "rr_stage_writer_swap": (
            "src/alphaforge/persistence.py",
            "tests/test_issue495_rr_stage_evidence.py::test_raw_rr_is_candidate_alias_and_rr_stages_round_trip",
        ),
        "sizing_removed_from_parity_projection": (
            "src/alphaforge/decision_invariant.py",
            "tests/test_issue490_protected_mode_parity.py::test_mode_parity_fails_on_capital_sizing_drift",
        ),
        "semantic_validation_bypass": (
            "src/alphaforge/decision_invariant.py",
            "tests/test_issue486_semantic_decision_invariants.py::test_identically_wrong_surfaces_do_not_pass_parity",
        ),
    }

    assert len(cases) == len(mutations())
    for name, (production_file, production_test) in required.items():
        case = cases[name]
        assert case.file == production_file
        assert case.test == production_test
