from __future__ import annotations

from alphaforge.burnin_campaign import build_phase8_campaign_identity
from alphaforge.burnin_ops import _actual_runtime_identity, _candidate_identity, preflight
from alphaforge.runtime import ExecutionMode, RuntimeConfig, _phase8_runtime_hashes


_BASE = {
    "release_id": "phase9-burnin-1",
    "strategy_config": {"threshold": 0.62, "window": 20},
    "universe": ["BTCUSDT", "ETHUSDT"],
    "execution_cost_config": {"max_spread_pct": 0.0025, "slippage_bps": 2.0},
}


def test_paper_candidate_and_runtime_config_hash_match() -> None:
    candidate = _candidate_identity(**_BASE, execution_mode="PAPER", runtime_limits_active=True, runtime_limits={"max_positions": 3})
    runtime = _phase8_runtime_hashes(**_BASE, config=RuntimeConfig(execution_mode=ExecutionMode.PAPER), runtime_limits_active=True, runtime_limits={"max_positions": 3})
    assert candidate["config_payload"] == runtime["config_payload"]
    assert candidate["config_hash"] == runtime["config_hash"]


def test_backtest_and_paper_differ_for_mode_aware_fields() -> None:
    backtest = build_phase8_campaign_identity(**_BASE, execution_mode="BACKTEST")
    paper = build_phase8_campaign_identity(**_BASE, execution_mode="PAPER", runtime_limits_active=True, runtime_limits={"max_positions": 3})
    assert backtest["config_hash"] != paper["config_hash"]
    assert backtest["strategy_config_hash"] == paper["strategy_config_hash"]
    assert backtest["universe_hash"] == paper["universe_hash"]
    assert backtest["execution_cost_config_hash"] == paper["execution_cost_config_hash"]


def test_component_hashes_are_deterministic_despite_mapping_order() -> None:
    first = build_phase8_campaign_identity(**_BASE, execution_mode="PAPER")
    second = build_phase8_campaign_identity(
        release_id=_BASE["release_id"],
        strategy_config={"window": 20, "threshold": 0.62},
        universe=_BASE["universe"],
        execution_cost_config={"slippage_bps": 2.0, "max_spread_pct": 0.0025},
        execution_mode="paper",
    )
    for key in ("strategy_config_hash", "universe_hash", "execution_cost_config_hash"):
        assert first[key] == second[key]


def test_preflight_passes_when_critical_checks_and_identities_pass() -> None:
    candidate = _candidate_identity(**_BASE, execution_mode="PAPER")
    runtime = _actual_runtime_identity(**_BASE, execution_mode="PAPER")
    result = preflight(candidate, runtime, critical_checks_pass=True)
    assert result["status"] == "PASS"
    assert result["identity_differences"] == {}


def test_real_config_drift_fails_closed() -> None:
    candidate = _candidate_identity(**_BASE, execution_mode="PAPER", runtime_limits_active=False)
    runtime = _actual_runtime_identity(**_BASE, execution_mode="PAPER", runtime_limits_active=True, runtime_limits={"max_positions": 3})
    result = preflight(candidate, runtime)
    assert result["status"] == "FAIL_CLOSED"
    assert "config_hash" in result["identity_differences"]
    assert result["identity_differences"]["config_payload"]["candidate"]["RUNTIME_LIMITS_ACTIVE"] is False
    assert result["identity_differences"]["config_payload"]["runtime"]["RUNTIME_LIMITS_ACTIVE"] is True
