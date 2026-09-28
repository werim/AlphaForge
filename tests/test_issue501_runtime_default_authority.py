from __future__ import annotations

from dataclasses import MISSING, fields, replace

import pytest

from alphaforge.burnin_campaign import build_phase8_campaign_identity
from alphaforge.config import RuntimeSettings, load_config_from_env
from alphaforge.config_registry import FIELD_BY_NAME, canonical_field_default
from alphaforge.runtime import ExecutionMode, RuntimeConfig, _runtime_config_from_app_config


def _field_map(cls):
    return {item.name: item for item in fields(cls)}


def test_direct_runtime_defaults_use_registry_factories_not_literal_policy_copies():
    runtime_settings_fields = _field_map(RuntimeSettings)
    runtime_config_fields = _field_map(RuntimeConfig)

    for name, setting in FIELD_BY_NAME.items():
        if name in runtime_settings_fields:
            item = runtime_settings_fields[name]
            assert item.default is MISSING, name
            assert item.default_factory is not MISSING, name

        if name in runtime_config_fields:
            item = runtime_config_fields[name]
            assert item.default is MISSING, name
            assert item.default_factory is not MISSING, name

    settings = RuntimeSettings()
    runtime = RuntimeConfig()

    for name, setting in FIELD_BY_NAME.items():
        if hasattr(settings, name) and name != "required_live_exchanges":
            assert getattr(settings, name) == setting.default, name
        if hasattr(runtime, name) and name not in {"execution_mode", "required_live_exchanges"}:
            assert getattr(runtime, name) == setting.default, name

    assert settings.required_live_exchanges == ("binance",)
    assert runtime.required_live_exchanges == ("binance",)
    assert runtime.execution_mode is ExecutionMode.PAPER

    # Runtime representations whose names intentionally differ from registry fields.
    assert settings.market_data_base_url == canonical_field_default("binance_market_data_base_url")
    assert runtime.market_data_base_url == canonical_field_default("binance_market_data_base_url")
    assert settings.binance_reconciliation_recv_window_ms == canonical_field_default("binance_recv_window_ms")
    assert settings.paper_decision_timeframe == canonical_field_default("execution_timeframe")
    assert runtime.paper_decision_timeframe == canonical_field_default("execution_timeframe")
    assert runtime.live_trading_enabled == canonical_field_default("live_enabled")
    assert runtime.max_open_positions == canonical_field_default("max_concurrent_positions")


def test_registry_default_change_cannot_leave_direct_runtime_stale(monkeypatch):
    original = FIELD_BY_NAME["min_effective_rr"]
    monkeypatch.setitem(
        FIELD_BY_NAME,
        "min_effective_rr",
        replace(original, default=1.73),
    )

    assert RuntimeSettings().min_effective_rr == pytest.approx(1.73)
    assert RuntimeConfig().min_effective_rr == pytest.approx(1.73)


def test_direct_runtime_protected_defaults_match_normal_bootstrap():
    app = load_config_from_env(env={})
    frozen = _runtime_config_from_app_config(app, ExecutionMode.PAPER)
    direct = RuntimeConfig()

    protected = (
        "min_signal_score",
        "min_rr",
        "min_effective_rr",
        "min_sl_pct",
        "max_sl_pct",
        "max_spread_pct",
        "max_expected_slippage_pct",
        "max_total_cost_pct",
        "max_latency_ms",
        "max_notional_exposure",
        "max_symbol_notional",
        "max_correlation_group_exposure",
        "max_correlated_positions",
        "max_daily_loss_pct",
        "paper_candidate_notional",
        "reject_unknown_execution_context",
        "enable_binance_readonly_reconciliation",
    )
    for name in protected:
        assert getattr(direct, name) == getattr(frozen, name), name

    assert direct.min_effective_rr == pytest.approx(1.60)
    assert direct.max_spread_pct == pytest.approx(0.05)
    assert direct.max_expected_slippage_pct == pytest.approx(0.05)
    assert direct.enable_binance_readonly_reconciliation is True


def test_explicit_fixture_override_still_requires_explicit_callsite_value():
    fixture = RuntimeConfig(
        min_effective_rr=1.10,
        max_spread_pct=0.0025,
        max_expected_slippage_pct=0.0020,
        enable_binance_readonly_reconciliation=False,
    )

    assert fixture.min_effective_rr == pytest.approx(1.10)
    assert fixture.max_spread_pct == pytest.approx(0.0025)
    assert fixture.max_expected_slippage_pct == pytest.approx(0.0020)
    assert fixture.enable_binance_readonly_reconciliation is False


def test_default_campaign_identity_records_canonical_effective_values():
    runtime = RuntimeConfig()
    identity = build_phase8_campaign_identity(
        runtime,
        ["BTCUSDT", "ETHUSDT"],
        ["1m", "15m", "1h"],
        release_id="issue501-canonical-defaults",
    )
    payload = identity["config_payload"]

    assert payload["MIN_EFFECTIVE_RR"] == pytest.approx(
        canonical_field_default("min_effective_rr")
    )
    assert payload["MAX_SPREAD_PCT"] == pytest.approx(
        canonical_field_default("max_spread_pct")
    )
    assert payload["MAX_EXPECTED_SLIPPAGE_PCT"] == pytest.approx(
        canonical_field_default("max_expected_slippage_pct")
    )
    assert payload["MAX_CORRELATION_GROUP_EXPOSURE"] == pytest.approx(
        canonical_field_default("max_correlation_group_exposure")
    )
    assert payload["MAX_CORRELATED_POSITIONS"] == canonical_field_default(
        "max_correlated_positions"
    )
