from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from alphaforge.burnin_campaign import build_phase8_campaign_identity
from alphaforge.config import load_config_from_env, runtime_filter_config
from alphaforge.config_registry import (
    REGISTRY_BY_ENV,
    RESERVED_VARIABLES,
    resolve_backtest_portfolio_config,
)
from alphaforge.runtime import (
    ExecutionMode,
    RuntimeOrchestrator,
    _runtime_config_from_app_config,
)


GROUP_EXPOSURE = "ALPHAFORGE_MAX_CORRELATION_GROUP_EXPOSURE"
GROUP_COUNT = "ALPHAFORGE_MAX_CORRELATED_POSITIONS"


class _AcceptBrain:
    def score_signal(self, signal_payload, market_ctx, regime_ctx, stats_ctx):
        return SimpleNamespace(total_score=9.0, components={}, probabilistic={})

    def choose_order_plan(self, signal_payload, market_ctx, score_ctx):
        return SimpleNamespace(
            decision="ACCEPTED",
            reason="",
            confidence=0.9,
            order_type="LIMIT",
            limit_price=signal_payload.get("entry_price"),
            stop_price=None,
        )

    def explain_decision(self, signal_payload, score_ctx, order_plan):
        return "issue500-fixture"


def _runtime(*, max_group_exposure: float, max_group_positions: int):
    app = load_config_from_env(
        env={
            "ALPHAFORGE_EXECUTION_MODE": "PAPER",
            GROUP_EXPOSURE: str(max_group_exposure),
            GROUP_COUNT: str(max_group_positions),
        }
    )
    config = _runtime_config_from_app_config(app, ExecutionMode.PAPER)
    runtime = RuntimeOrchestrator(
        config=config,
        ai_brain=_AcceptBrain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        paper_slippage_bps=2.0,
    )
    return app, config, runtime


def _portfolio_projection(runtime: RuntimeOrchestrator):
    sample = dict(runtime._qualification_samples[0])
    sample["open_positions"] = {
        "ETHUSDT": {"notional": 10.0, "side": "LONG"},
    }
    signal = {
        "signal_id": "issue500-btc-long",
        "symbol": sample["symbol"],
        "side": sample["side"],
        "timeframe": sample["timeframe"],
        "entry_price": sample["entry"],
        "stop_loss": sample["sl"],
        "take_profit": sample["tp"],
        "risk_reward": sample["rr"],
        "setup": sample["setup_type"],
        "setup_type": sample["setup_type"],
        "setup_reason": sample["setup_reason"],
        "regime": sample["regime"],
        "expectancy": sample["expectancy"],
        "setup_quality": 0.9,
        "mode": "PAPER",
    }
    market = {**sample, "mode": "PAPER"}
    regime = {"alignment": 0.8, "regime": "TRENDING"}
    stats = {"sample_size": 100, "setup": {}, "regime": {}, "symbol": {}}
    return runtime._evaluate_pre_submit(signal, market, regime, stats)


def test_canonical_correlation_limits_change_runtime_portfolio_decision():
    _, tight_config, tight_runtime = _runtime(
        max_group_exposure=15.0,
        max_group_positions=1,
    )
    _, loose_config, loose_runtime = _runtime(
        max_group_exposure=25.0,
        max_group_positions=2,
    )

    assert tight_config.max_correlation_group_exposure == pytest.approx(15.0)
    assert tight_config.max_correlated_positions == 1
    assert loose_config.max_correlation_group_exposure == pytest.approx(25.0)
    assert loose_config.max_correlated_positions == 2

    tight = _portfolio_projection(tight_runtime)
    loose = _portfolio_projection(loose_runtime)

    assert tight["portfolio_decision"]["accepted"] is False
    assert tight["primary_reject_reason"] == "CORRELATION_OVEREXPOSURE"
    assert loose["portfolio_decision"]["accepted"] is True


def test_correlation_limits_are_typed_and_campaign_identity_bound():
    app_low, runtime_low, _ = _runtime(
        max_group_exposure=20.0,
        max_group_positions=1,
    )
    app_high, runtime_high, _ = _runtime(
        max_group_exposure=30.0,
        max_group_positions=3,
    )

    assert app_low.runtime.max_correlation_group_exposure == pytest.approx(20.0)
    assert app_low.runtime.max_correlated_positions == 1
    assert REGISTRY_BY_ENV[GROUP_EXPOSURE].default == pytest.approx(75_000.0)
    assert REGISTRY_BY_ENV[GROUP_COUNT].default == 2

    low_filter = runtime_filter_config(runtime_low, mode="PAPER")
    assert low_filter["MAX_CORRELATION_GROUP_EXPOSURE"] == pytest.approx(20.0)
    assert low_filter["MAX_CORRELATED_POSITIONS"] == 1

    low = build_phase8_campaign_identity(
        runtime_low,
        ["BTCUSDT", "ETHUSDT"],
        ["1m", "15m", "1h"],
        release_id="issue500-low",
    )
    high = build_phase8_campaign_identity(
        runtime_high,
        ["BTCUSDT", "ETHUSDT"],
        ["1m", "15m", "1h"],
        release_id="issue500-high",
    )

    assert low["config_payload"]["MAX_CORRELATION_GROUP_EXPOSURE"] == pytest.approx(20.0)
    assert low["config_payload"]["MAX_CORRELATED_POSITIONS"] == 1
    assert high["config_payload"]["MAX_CORRELATION_GROUP_EXPOSURE"] == pytest.approx(30.0)
    assert high["config_payload"]["MAX_CORRELATED_POSITIONS"] == 3
    assert low["config_hash"] != high["config_hash"]


def test_stale_bare_correlation_alias_remains_non_authoritative():
    assert "MAX_CORRELATED_POSITIONS" in RESERVED_VARIABLES

    app = load_config_from_env(
        env={
            "ALPHAFORGE_EXECUTION_MODE": "PAPER",
            "MAX_CORRELATED_POSITIONS": "99",
        }
    )

    assert app.runtime.max_correlated_positions == REGISTRY_BY_ENV[GROUP_COUNT].default


def test_backtest_correlation_overrides_remain_explicitly_separate(tmp_path):
    runtime_names_only = resolve_backtest_portfolio_config(
        1000.0,
        env={
            GROUP_EXPOSURE: "1",
            GROUP_COUNT: "9",
        },
        root=tmp_path,
    )
    assert runtime_names_only["max_correlation_group_exposure"] == pytest.approx(750.0)
    assert runtime_names_only["max_correlated_positions"] == 2

    explicit_backtest = resolve_backtest_portfolio_config(
        1000.0,
        env={
            GROUP_EXPOSURE: "1",
            GROUP_COUNT: "9",
            "ALPHAFORGE_BACKTEST_MAX_CORRELATION_GROUP_EXPOSURE": "600",
            "ALPHAFORGE_BACKTEST_MAX_CORRELATED_POSITIONS": "4",
        },
        root=tmp_path,
    )
    assert explicit_backtest["max_correlation_group_exposure"] == pytest.approx(600.0)
    assert explicit_backtest["max_correlated_positions"] == 4


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (GROUP_EXPOSURE, "-0.01"),
        (GROUP_EXPOSURE, "nan"),
        (GROUP_EXPOSURE, "inf"),
        (GROUP_COUNT, "-1"),
        (GROUP_COUNT, "not-an-int"),
    ],
)
def test_invalid_canonical_correlation_limits_fail_closed(name, value):
    with pytest.raises((TypeError, ValueError)):
        load_config_from_env(
            env={
                "ALPHAFORGE_EXECUTION_MODE": "PAPER",
                name: value,
            }
        )
