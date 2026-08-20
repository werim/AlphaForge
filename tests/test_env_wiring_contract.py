from __future__ import annotations

import json

import pytest

from alphaforge.config import load_config_from_env
from alphaforge.config_audit import audit_config
from alphaforge.config_registry import CONFIG_REGISTRY, ENV_CONTRACT, effective_config_values
from alphaforge.exchange_market_scanner import _scan_binance, _scan_hyperliquid
from alphaforge.burnin_campaign import build_phase8_campaign_identity


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
    """Parsing regression only; this is not behavioral-wiring evidence."""
    alternate = _alternate(setting)
    baseline = effective_config_values(env={}, root=tmp_path)[setting.env_name]["value"]
    changed = effective_config_values(env={setting.env_name: alternate}, root=tmp_path)[setting.env_name]["value"]
    assert changed != baseline


def test_wired_metadata_resolves_to_specific_consumers_and_pytest_nodes():
    report = audit_config(env={})
    assert report["status"] != "FAIL", json.dumps(report, indent=2)
    wired = [row for row in ENV_CONTRACT if row.classification == "WIRED"]
    assert all("::" in row.behavioral_test for row in wired)
    assert all(row.consumed_by and not row.consumed_by.endswith("load_config_from_env") for row in wired)


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


def test_paper_decision_timeframe_controls_scanner_and_reject_evidence(monkeypatch):
    """The env setting gates production candidates and hashes reject evaluation semantics."""
    calls = []

    def fetch(*args, **kwargs):
        calls.append(args[0])
        return None

    monkeypatch.setenv("ALPHAFORGE_PAPER_DECISION_TIMEFRAME", "5m")
    unsupported = load_config_from_env()
    monkeypatch.setattr("alphaforge.exchange_market_scanner._fetch_json", fetch)
    assert _scan_binance(unsupported, timeout_sec=0.01) == []
    assert calls == []  # unsupported geometry cannot silently fall back to 1m
    unsupported_identity = build_phase8_campaign_identity(
        unsupported.runtime, ["BTCUSDT"], ["1h"]
    )
    assert unsupported_identity["config_payload"]["decision_setup_timeframe"] == "5m"
    assert unsupported_identity["config_payload"]["reject_evaluation_timeframe"] == "5m"

    responses = iter([
        {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]},
        [{"symbol": "BTCUSDT", "lastPrice": "100", "quoteVolume": "90000000", "priceChangePercent": "1"}],
        [{"symbol": "BTCUSDT", "bidPrice": "99.9", "askPrice": "100.1"}],
        [],
    ])
    monkeypatch.setenv("ALPHAFORGE_PAPER_DECISION_TIMEFRAME", "1m")
    supported = load_config_from_env()
    monkeypatch.setattr("alphaforge.exchange_market_scanner._fetch_json", lambda *a, **k: next(responses))
    monkeypatch.setattr(
        "alphaforge.exchange_market_scanner._fetch_json_with_latency",
        lambda *a, **k: (next(responses), 1.0),
    )
    candidates = _scan_binance(supported, timeout_sec=0.01)
    assert candidates[0]["timeframe"] == "1m"
    supported_identity = build_phase8_campaign_identity(supported.runtime, ["BTCUSDT"], ["1h"])
    assert supported_identity["config_payload"]["reject_evaluation_timeframe"] == "1m"
    assert unsupported_identity["config_hash"] != supported_identity["config_hash"]


def test_remaining_reserved_entries_have_reviewable_reasons():
    allowed = {"NOT_IMPLEMENTED", "DEPRECATED_NO_EFFECT", "REMOVED", "UNSAFE", "FUTURE_SUBSYSTEM"}
    reserved = [row for row in ENV_CONTRACT if row.classification == "RESERVED"]
    assert reserved
    assert all(row.unsupported_reason in allowed for row in reserved)
