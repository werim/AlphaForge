from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from sqlalchemy import text

from alphaforge.config import (
    CROSS_SURFACE_FILTER_FIELDS,
    load_config_from_env,
    runtime_filter_config,
)
from alphaforge.config_registry import decision_filter_config
from alphaforge.live_readiness import LiveReadinessEvaluator
from alphaforge.order import LifecycleState
from alphaforge.persistence import init_db, save_decision_evidence
from alphaforge.runtime import (
    ExecutionMode,
    RuntimeConfig,
    RuntimeOrchestrator,
    _build_runtime_from_env,
    _runtime_config_from_app_config,
)


SHARED_OVERRIDE_ENV = {
    "ALPHAFORGE_EXECUTION_MODE": "PAPER",
    "ALPHAFORGE_MIN_SIGNAL_SCORE": "0.47",
    "ALPHAFORGE_MIN_RR": "1.23",
    "MIN_EFFECTIVE_RR": "1.37",
    "ALPHAFORGE_MIN_SL_PCT": "0.17",
    "ALPHAFORGE_MAX_SL_PCT": "1.41",
    "ALPHAFORGE_MAX_SPREAD_PCT": "0.031",
    "ALPHAFORGE_MAX_EXPECTED_SLIPPAGE_PCT": "0.021",
    "ALPHAFORGE_MAX_TOTAL_COST_PCT": "0.19",
    "ALPHAFORGE_MIN_LIQUIDITY_SCORE": "0.44",
    "ALPHAFORGE_MAX_VOLATILITY_PENALTY_PCT": "0.18",
    "ALPHAFORGE_REJECT_UNKNOWN_EXECUTION_CONTEXT": "false",
    "ALPHAFORGE_MIN_ATR_PCT": "0.22",
    "ALPHAFORGE_MAX_ATR_PCT": "2.8",
    "ALPHAFORGE_BLOCK_UNKNOWN_EXPECTANCY": "false",
    "ALPHAFORGE_BLOCK_CHOP_MARKET": "false",
    "ALPHAFORGE_REQUIRE_REGIME_ALIGNMENT": "false",
    "ALPHAFORGE_ENABLE_ORDERBOOK_FILTER": "true",
    "ALPHAFORGE_STOP_TOO_WIDE_HARD_REJECT": "false",
    "ALPHAFORGE_STOP_TOO_WIDE_SOFT_SCORE_MIN": "8.6",
    "ALPHAFORGE_STOP_TOO_WIDE_SOFT_EFFECTIVE_RR_MIN": "2.05",
    "ALPHAFORGE_STOP_TOO_WIDE_MAX_RISK_SCALE": "0.42",
    "ALPHAFORGE_STOP_TOO_WIDE_EXTREME_MULT": "1.7",
    "ALPHAFORGE_MAX_TRADES_SYMBOL_PER_DAY": "4",
    "ALPHAFORGE_MAX_TRADES_GLOBAL_PER_DAY": "11",
    "ALPHAFORGE_SYMBOL_LOSS_STREAK_LIMIT": "4",
    "ALPHAFORGE_GLOBAL_LOSS_STREAK_LIMIT": "6",
    "ALPHAFORGE_MAX_ABS_FUNDING_RATE_PCT": "0.0007",
    "ALPHAFORGE_MAX_LATENCY_MS": "777",
}


def test_named_invariant_map_covers_execution_and_decision_controls() -> None:
    required = {
        "MIN_TRADE_SCORE", "MIN_RR", "MIN_EFFECTIVE_RR",
        "MAX_SPREAD_PCT", "MAX_EXPECTED_SLIPPAGE_PCT",
        "MAX_TOTAL_COST_PCT", "MIN_LIQUIDITY_SCORE",
        "MAX_VOLATILITY_PENALTY_PCT", "MAX_LATENCY_MS",
        "MAX_ABS_FUNDING_RATE_PCT", "REJECT_UNKNOWN_EXECUTION_CONTEXT",
        "ENABLE_ORDERBOOK_FILTER", "STOP_TOO_WIDE_SOFT_EFFECTIVE_RR_MIN",
        "MAX_TRADES_PER_SYMBOL_PER_DAY", "MAX_TRADES_GLOBAL_PER_DAY",
        "SYMBOL_LOSS_STREAK_LIMIT", "GLOBAL_LOSS_STREAK_LIMIT",
    }
    assert required <= CROSS_SURFACE_FILTER_FIELDS.keys()


def test_registry_to_runtime_snapshot_to_filter_preserves_explicit_overrides(
    tmp_path: Path,
) -> None:
    app = load_config_from_env(env=SHARED_OVERRIDE_ENV, root=tmp_path)
    runtime = _runtime_config_from_app_config(app, ExecutionMode.PAPER)
    runtime_filter = runtime_filter_config(runtime, mode="PAPER")
    paper_filter = decision_filter_config("PAPER", env=SHARED_OVERRIDE_ENV, root=tmp_path)
    backtest_filter = decision_filter_config("BACKTEST", env=SHARED_OVERRIDE_ENV, root=tmp_path)

    for key in CROSS_SURFACE_FILTER_FIELDS:
        assert runtime_filter[key] == paper_filter[key], key
        assert paper_filter[key] == backtest_filter[key], key

    assert runtime.enable_orderbook_filter is True
    assert runtime.stop_too_wide_soft_effective_rr_min == pytest.approx(2.05)
    assert runtime.max_latency_ms == 777
    assert runtime.max_abs_funding_rate_pct == pytest.approx(0.0007)
    assert runtime.max_total_cost_pct == pytest.approx(0.19)
    assert runtime.min_liquidity_score == pytest.approx(0.44)
    assert runtime.max_volatility_penalty_pct == pytest.approx(0.18)
    assert runtime.reject_unknown_execution_context is False


def test_runtime_filter_uses_startup_snapshot_not_post_startup_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = load_config_from_env(env=SHARED_OVERRIDE_ENV, root=tmp_path)
    runtime = _runtime_config_from_app_config(app, ExecutionMode.PAPER)

    monkeypatch.setenv("MIN_EFFECTIVE_RR", "9.9")
    monkeypatch.setenv("ALPHAFORGE_ENABLE_ORDERBOOK_FILTER", "false")
    monkeypatch.setenv("ALPHAFORGE_STOP_TOO_WIDE_SOFT_EFFECTIVE_RR_MIN", "9.8")
    monkeypatch.setenv("ALPHAFORGE_MAX_LATENCY_MS", "9999")

    frozen = runtime_filter_config(runtime, mode="PAPER")
    assert frozen["MIN_EFFECTIVE_RR"] == pytest.approx(1.37)
    assert frozen["ENABLE_ORDERBOOK_FILTER"] is True
    assert frozen["STOP_TOO_WIDE_SOFT_EFFECTIVE_RR_MIN"] == pytest.approx(2.05)
    assert frozen["MAX_LATENCY_MS"] == 777


def test_execution_threshold_surface_is_complete(tmp_path: Path) -> None:
    cfg = decision_filter_config("PAPER", env=SHARED_OVERRIDE_ENV, root=tmp_path)
    required = {
        "MIN_EFFECTIVE_RR", "MAX_SPREAD_PCT", "MAX_EXPECTED_SLIPPAGE_PCT",
        "MAX_SLIPPAGE_PCT", "MAX_TOTAL_COST_PCT", "MIN_LIQUIDITY_SCORE",
        "MAX_LATENCY_MS", "MAX_VOLATILITY_PENALTY_PCT",
        "MAX_ABS_FUNDING_RATE_PCT", "REJECT_UNKNOWN_EXECUTION_CONTEXT",
    }
    assert required <= cfg.keys()
    assert cfg["MAX_LATENCY_MS"] == 777
    assert cfg["MAX_ABS_FUNDING_RATE_PCT"] == pytest.approx(0.0007)


def test_production_builder_uses_runtime_snapshot_helper() -> None:
    source = inspect.getsource(_build_runtime_from_env)
    assert "_runtime_config_from_app_config(cfg, mode)" in source
    assert "RuntimeConfig(execution_mode=mode" not in source


def test_decision_evidence_and_readiness_share_runtime_threshold_and_scope(tmp_path: Path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'cross-surface.db'}")
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER, min_effective_rr=1.37),
        ai_brain=object(),
        market_scanner=lambda: None,
        persistence_engine=engine,
    )
    runtime._burnin_run_id = "run-invariant"
    runtime._persist_burnin_decision({
        "signal_id": "sig-invariant",
        "setup_identity": "setup-invariant",
        "decision": "REJECTED",
        "decision_time": "2026-09-22T16:00:00Z",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "score": 0.40,
        "rr": 1.50,
        "executable_raw_rr": 1.42,
        "effective_rr": 1.20,
        "entry": 100.0,
        "expected_fill": 100.02,
        "sl": 99.0,
        "tp": 101.5,
        "reason": "LOW_EFFECTIVE_RR",
        "primary_reject_reason": "LOW_EFFECTIVE_RR",
        "execution_ctx": {},
    }, lifecycle_state=LifecycleState.SIGNAL_REJECTED.value)

    with engine.begin() as conn:
        save_decision_evidence(
            conn,
            evidence_id="other-run",
            run_id="run-other",
            mode="BACKTEST",
            timestamp="2026-09-22T16:00:01Z",
            symbol="ETHUSDT",
            decision="REJECT",
            signal_id="sig-other",
            min_effective_rr=9.9,
            reject_reason="LOW_EFFECTIVE_RR",
        )

    evaluator = LiveReadinessEvaluator(
        engine,
        evidence_mode="PAPER",
        burnin_run_id="run-invariant",
        min_effective_rr=runtime.config.min_effective_rr,
        require_run_scope=True,
    )
    params = evaluator._scope_params()
    assert params["readiness_min_effective_rr"] == pytest.approx(1.37)
    assert params["readiness_run_id"] == "run-invariant"
    assert params["readiness_mode"] == "PAPER"

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT run_id,mode,min_effective_rr FROM decision_evidence "
            "WHERE " + evaluator._decision_evidence_scope_sql()
        ), params).mappings().all()
    assert len(rows) == 1
    assert rows[0]["run_id"] == "run-invariant"
    assert rows[0]["mode"] == "PAPER"
    assert rows[0]["min_effective_rr"] == pytest.approx(1.37)
