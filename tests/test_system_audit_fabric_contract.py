from __future__ import annotations

import inspect
from pathlib import Path

import alphaforge.order as order
import alphaforge.runtime as runtime
import alphaforge.system_audit_diagnostics as diagnostics
import alphaforge.system_audit_reporting as reporting
import alphaforge.system_audit_store as store


AUDIT_MODULES=(store,diagnostics,reporting)


def test_system_audit_fabric_is_not_imported_by_runtime_or_order_path() -> None:
    for module in (runtime,order):
        source=inspect.getsource(module)
        assert "alphaforge.system_audit" not in source
        assert "system_audit_store" not in source
        assert "system_audit_diagnostics" not in source
        assert "system_audit_reporting" not in source


def test_system_audit_modules_have_no_runtime_mutation_or_order_submission_dependency() -> None:
    forbidden=(
        "from alphaforge.runtime import",
        "import alphaforge.runtime",
        "from alphaforge.order import",
        "import alphaforge.order",
        "write_dashboard_overrides",
        "reset_dashboard_override",
        "allow_live_orders=True",
        "live_trading_enabled=True",
    )
    for module in AUDIT_MODULES:
        source=inspect.getsource(module)
        for token in forbidden:
            assert token not in source, (module.__name__,token)


def test_system_audit_cli_source_is_read_only_and_output_isolated() -> None:
    source=inspect.getsource(reporting)
    assert "?mode=ro" in source
    assert "--source-db" in source
    assert "--audit-db" in source
    assert "--audit-db must be different from --source-db" in source
    assert '"production_config_mutation_allowed": False' in source


def test_system_audit_ci_gate_covers_l0_l1_l2_and_umbrella_contract() -> None:
    root=Path(__file__).resolve().parents[1]
    workflow=(root/".github/workflows/test.yml").read_text(encoding="utf-8")
    assert "System audit fabric gate" in workflow
    for filename in (
        "tests/test_system_audit_fabric_l0.py",
        "tests/test_system_audit_fabric_l1.py",
        "tests/test_system_audit_fabric_l2.py",
        "tests/test_system_audit_fabric_contract.py",
    ):
        assert filename in workflow


def test_system_audit_architecture_document_records_level_boundary_and_authority() -> None:
    root=Path(__file__).resolve().parents[1]
    document=(root/"docs/system_audit_fabric.md").read_text(encoding="utf-8")
    for phrase in (
        "Levels 0–2",
        "read-only",
        "separate audit database",
        "EXECUTABLE_SHADOW",
        "production_mutation_allowed = 0",
        "Level 3",
        "Level 4",
    ):
        assert phrase in document
