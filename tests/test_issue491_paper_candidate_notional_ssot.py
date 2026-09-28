from __future__ import annotations

import pytest

from alphaforge.burnin_campaign import build_phase8_campaign_identity
from alphaforge.config import load_config_from_env, runtime_filter_config
from alphaforge.config_registry import REGISTRY_BY_ENV
from alphaforge.runtime import ExecutionMode, _runtime_config_from_app_config


SETTING = "ALPHAFORGE_PAPER_CANDIDATE_NOTIONAL"


def _resolved(value: str | None = None):
    env = {"ALPHAFORGE_EXECUTION_MODE": "PAPER"}
    if value is not None:
        env[SETTING] = value
    app = load_config_from_env(env=env)
    runtime = _runtime_config_from_app_config(app, ExecutionMode.PAPER)
    return app, runtime


def test_paper_candidate_notional_is_canonical_typed_setting():
    setting = REGISTRY_BY_ENV[SETTING]

    assert setting.field_name == "paper_candidate_notional"
    assert setting.applies_to == ("PAPER",)
    assert setting.default == pytest.approx(10.0)
    assert setting.deprecated_aliases == ()


def test_canonical_candidate_notional_reaches_runtime_and_campaign_identity():
    low_app, low_runtime = _resolved("7.5")
    high_app, high_runtime = _resolved("12.5")

    assert low_app.runtime.paper_candidate_notional == pytest.approx(7.5)
    assert low_runtime.paper_candidate_notional == pytest.approx(7.5)
    assert high_runtime.paper_candidate_notional == pytest.approx(12.5)

    low_filters = runtime_filter_config(low_runtime, mode="PAPER")
    assert low_filters["PAPER_CANDIDATE_NOTIONAL"] == pytest.approx(7.5)

    low_identity = build_phase8_campaign_identity(
        low_runtime,
        ["BTCUSDT"],
        ["1m", "15m", "1h"],
        release_id="issue491-low",
    )
    high_identity = build_phase8_campaign_identity(
        high_runtime,
        ["BTCUSDT"],
        ["1m", "15m", "1h"],
        release_id="issue491-high",
    )

    assert low_identity["config_payload"]["PAPER_CANDIDATE_NOTIONAL"] == pytest.approx(7.5)
    assert high_identity["config_payload"]["PAPER_CANDIDATE_NOTIONAL"] == pytest.approx(12.5)
    assert low_identity["config_hash"] != high_identity["config_hash"]


def test_missing_candidate_notional_resolves_registry_default_not_hidden_runtime_fallback():
    setting = REGISTRY_BY_ENV[SETTING]
    app, runtime = _resolved()

    assert app.runtime.paper_candidate_notional == pytest.approx(setting.default)
    assert runtime.paper_candidate_notional == pytest.approx(setting.default)
    assert runtime_filter_config(runtime, mode="PAPER")[
        "PAPER_CANDIDATE_NOTIONAL"
    ] == pytest.approx(setting.default)


def test_unregistered_legacy_name_cannot_override_canonical_candidate_notional():
    app = load_config_from_env(
        env={
            "ALPHAFORGE_EXECUTION_MODE": "PAPER",
            "PAPER_CANDIDATE_NOTIONAL": "999",
        }
    )

    assert app.runtime.paper_candidate_notional == pytest.approx(
        REGISTRY_BY_ENV[SETTING].default
    )


@pytest.mark.parametrize("bad", ["nan", "inf", "-0.1", "not-a-number"])
def test_invalid_canonical_candidate_notional_fails_closed(bad):
    with pytest.raises((TypeError, ValueError)):
        _resolved(bad)
