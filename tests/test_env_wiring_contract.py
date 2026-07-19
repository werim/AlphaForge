from __future__ import annotations

import json

import pytest

from alphaforge.config import load_config_from_env
from alphaforge.config_audit import audit_config
from alphaforge.config_registry import CONFIG_REGISTRY, ENV_CONTRACT, effective_config_values
from alphaforge.exchange_market_scanner import _scan_hyperliquid


def _alternate(setting):
    if setting.value_type == "bool":
        return "false" if setting.default is True else "true"
    if setting.value_type == "int":
        candidate = int(setting.default or 0) + 1
        if setting.max_value is not None and candidate > setting.max_value:
            candidate = int(setting.default) - 1
        return str(candidate)
    if setting.value_type == "float":
        candidate = float(setting.default or 0) + 0.01
        if setting.max_value is not None and candidate > setting.max_value:
            candidate = float(setting.default) - 0.01
        return str(candidate)
    return "contract-alternate" if setting.default != "contract-alternate" else "contract-other"


@pytest.mark.parametrize("setting", CONFIG_REGISTRY, ids=lambda setting: setting.env_name)
def test_every_wired_value_has_typed_observable_resolution(setting, tmp_path):
    """Registry-wide regression: changing a WIRED input changes its typed snapshot.

    Runtime/decision tests named by ``behavioral_test`` cover each post-loader
    consumer; this test prevents a setting from becoming an inert parser entry.
    """
    alternate = _alternate(setting)
    baseline = effective_config_values(env={}, root=tmp_path)[setting.env_name]["value"]
    changed = effective_config_values(env={setting.env_name: alternate}, root=tmp_path)[setting.env_name]["value"]
    assert changed != baseline
    row = next(item for item in ENV_CONTRACT if item.name == setting.env_name)
    assert row.consumed_by and not row.consumed_by.endswith("load_config_from_env")
    assert row.behavioral_test.startswith("tests/")


def test_alias_conflicts_fail_audit_but_equal_values_are_accepted():
    conflict = audit_config(env={"ALPHAFORGE_MAX_CONCURRENT_POSITIONS": "3", "ALPHAFORGE_MAX_OPEN_POSITIONS": "9"})
    assert conflict["status"] == "FAIL"
    assert any("alias conflict" in error for error in conflict["errors"])
    equal = audit_config(env={"ALPHAFORGE_MAX_CONCURRENT_POSITIONS": "3", "ALPHAFORGE_MAX_OPEN_POSITIONS": "3"})
    assert equal["status"] != "FAIL", json.dumps(equal, indent=2)


def test_canonical_position_limit_wins_alias(monkeypatch):
    monkeypatch.setenv("ALPHAFORGE_MAX_CONCURRENT_POSITIONS", "4")
    monkeypatch.setenv("ALPHAFORGE_MAX_OPEN_POSITIONS", "9")
    assert load_config_from_env().runtime.max_concurrent_positions == 4


def test_max_daily_loss_changes_runtime_risk_configuration(monkeypatch):
    monkeypatch.setenv("ALPHAFORGE_MAX_DAILY_LOSS_PCT", "0.0125")
    assert load_config_from_env().runtime.max_daily_loss_pct == pytest.approx(0.0125)


def test_hyperliquid_enabled_controls_scanner_without_network(monkeypatch):
    monkeypatch.setenv("HYPERLIQUID_ENABLED", "false")
    cfg = load_config_from_env()
    assert _scan_hyperliquid(cfg, timeout_sec=0.01) == []


def test_remaining_reserved_entries_have_reviewable_reasons():
    allowed = {"NOT_IMPLEMENTED", "DEPRECATED_NO_EFFECT", "REMOVED", "UNSAFE", "FUTURE_SUBSYSTEM"}
    reserved = [row for row in ENV_CONTRACT if row.classification == "RESERVED"]
    assert reserved
    assert all(row.unsupported_reason in allowed for row in reserved)
