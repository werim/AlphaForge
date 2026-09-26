from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import backtest_order
from alphaforge import burnin_ops
from alphaforge.config import load_config_from_env
from alphaforge.config_registry import (
    REGISTRY_BY_ENV,
    managed_config_value,
    resolve_backtest_database_url,
    resolve_backtest_portfolio_config,
)
from alphaforge.dashboard import backtest_control


def test_live_connectivity_settings_are_registry_managed(tmp_path: Path) -> None:
    env = {
        "ALPHAFORGE_EXECUTION_MODE": "LIVE",
        "ALPHAFORGE_REQUIRE_EXCHANGE_CONNECTIVITY_FOR_LIVE": "false",
        "ALPHAFORGE_REQUIRED_LIVE_EXCHANGES": "binance,hyperliquid",
        "ALPHAFORGE_EXCHANGE_CONNECTIVITY_TIMEOUT_SEC": "7.5",
    }
    cfg = load_config_from_env(env=env, root=tmp_path)
    assert cfg.runtime.require_exchange_connectivity_for_live is False
    assert cfg.runtime.required_live_exchanges == ("binance", "hyperliquid")
    assert cfg.runtime.exchange_connectivity_timeout_sec == pytest.approx(7.5)
    for name in env:
        if name != "ALPHAFORGE_EXECUTION_MODE":
            assert name in REGISTRY_BY_ENV


def test_backtest_portfolio_defaults_preserve_legacy_semantics(tmp_path: Path) -> None:
    resolved = resolve_backtest_portfolio_config(1000.0, env={}, root=tmp_path)
    assert resolved == {
        "max_open_positions": 3,
        "max_concurrent_positions": 3,
        "max_notional_exposure": 1000.0,
        "max_symbol_notional": 500.0,
        "max_daily_loss_pct": 0.03,
        "max_rolling_drawdown_pct": 0.08,
        "max_correlation_group_exposure": 750.0,
        "max_correlated_positions": 2,
        "max_daily_symbol_trades": 2,
        "max_daily_global_trades": 6,
        "max_same_side_exposure": 750.0,
        "max_net_exposure": 1000.0,
        "reject_unknown_portfolio_risk": True,
    }


def test_backtest_portfolio_explicit_overrides_and_generic_fallback(tmp_path: Path) -> None:
    env = {
        "ALPHAFORGE_MAX_OPEN_POSITIONS": "4",
        "ALPHAFORGE_MAX_NOTIONAL_EXPOSURE": "900",
        "ALPHAFORGE_MAX_SYMBOL_NOTIONAL": "400",
        "ALPHAFORGE_BACKTEST_MAX_NOTIONAL_EXPOSURE": "800",
        "ALPHAFORGE_BACKTEST_MAX_TRADES_GLOBAL_PER_DAY": "9",
    }
    resolved = resolve_backtest_portfolio_config(1000.0, env=env, root=tmp_path)
    assert resolved["max_open_positions"] == 4
    assert resolved["max_concurrent_positions"] == 4
    assert resolved["max_notional_exposure"] == pytest.approx(800.0)
    assert resolved["max_symbol_notional"] == pytest.approx(400.0)
    assert resolved["max_daily_global_trades"] == 9


def test_backtest_database_url_preserves_local_fallback_and_alias_resolution(tmp_path: Path) -> None:
    assert resolve_backtest_database_url("artifacts/backtest", env={}, root=tmp_path) == (
        "sqlite+pysqlite:///artifacts/backtest/alphaforge_backtest.db"
    )
    alias = resolve_backtest_database_url(
        "ignored",
        env={"ALPHAFORGE_DB_URL": "sqlite+pysqlite:///tmp/issue407.db"},
        root=tmp_path,
    )
    assert alias == "sqlite+pysqlite:///tmp/issue407.db"


def test_clock_skew_limit_uses_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALPHAFORGE_MAX_CLOCK_SKEW_MS", "2")
    monkeypatch.setattr(burnin_ops.time, "time", lambda: 1000.0)
    result = burnin_ops.clock_skew_check(
        provider=lambda: {"provider_utc_ms": 1_000_003, "provider_provenance": {"provider": "TEST"}}
    )
    assert result["status"] == "FAIL"
    assert result["configured_max_skew_ms"] == 2


def test_dashboard_safe_trade_limit_uses_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALPHAFORGE_BACKTEST_SAFE_TRADES_PER_DAY", "4.5")
    assert managed_config_value("ALPHAFORGE_BACKTEST_SAFE_TRADES_PER_DAY") == pytest.approx(4.5)
    source = inspect.getsource(backtest_control)
    assert 'os.getenv("ALPHAFORGE_BACKTEST_SAFE_TRADES_PER_DAY"' not in source


def test_behavioral_env_bypasses_removed_from_consumers() -> None:
    backtest_source = inspect.getsource(backtest_order)
    for name in (
        "ALPHAFORGE_BACKTEST_MAX_OPEN_POSITIONS",
        "ALPHAFORGE_BACKTEST_MAX_CONCURRENT_POSITIONS",
        "ALPHAFORGE_BACKTEST_MAX_NOTIONAL_EXPOSURE",
        "ALPHAFORGE_BACKTEST_MAX_SYMBOL_NOTIONAL",
        "ALPHAFORGE_BACKTEST_MAX_DAILY_LOSS_PCT",
        "ALPHAFORGE_BACKTEST_MAX_ROLLING_DRAWDOWN_PCT",
        "ALPHAFORGE_BACKTEST_MAX_CORRELATION_GROUP_EXPOSURE",
        "ALPHAFORGE_BACKTEST_MAX_CORRELATED_POSITIONS",
        "ALPHAFORGE_BACKTEST_MAX_TRADES_SYMBOL_PER_DAY",
        "ALPHAFORGE_BACKTEST_MAX_TRADES_GLOBAL_PER_DAY",
        "ALPHAFORGE_BACKTEST_MAX_SAME_SIDE_EXPOSURE",
        "ALPHAFORGE_BACKTEST_MAX_NET_EXPOSURE",
        "ALPHAFORGE_DATABASE_URL",
        "ALPHAFORGE_DB_URL",
    ):
        assert f'os.getenv("{name}"' not in backtest_source
    assert 'os.getenv("ALPHAFORGE_MAX_CLOCK_SKEW_MS"' not in inspect.getsource(burnin_ops)
