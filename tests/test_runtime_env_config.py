from __future__ import annotations

import pytest

from alphaforge.runtime import ExecutionMode, _build_runtime_from_env


_RUNTIME_ENV_KEYS = [
    "ALPHAFORGE_EXECUTION_MODE",
    "EXECUTION_MODE",
    "ALPHAFORGE_DATABASE_URL",
    "ALPHAFORGE_DB_URL",
    "ALPHAFORGE_MIN_SIGNAL_SCORE",
    "ALPHAFORGE_MIN_ACCEPT_SCORE",
    "ALPHAFORGE_SCAN_INTERVAL_SEC",
    "ALPHAFORGE_HEARTBEAT_INTERVAL_SEC",
    "ALPHAFORGE_MAX_SYMBOLS_PER_SCAN",
    "ALPHAFORGE_MAX_REJECT_LOG_ENTRIES",
    "ALPHAFORGE_MAX_CONCURRENT_POSITIONS",
    "ALPHAFORGE_MAX_OPEN_POSITIONS",
    "ALPHAFORGE_SYMBOL_COOLDOWN_SEC",
    "ALPHAFORGE_MAX_NOTIONAL_EXPOSURE",
    "ALPHAFORGE_MAX_SYMBOL_NOTIONAL",
    "ALPHAFORGE_STALE_MARKET_DATA_SEC",
    "ALPHAFORGE_MAX_SPREAD_PCT",
    "ALPHAFORGE_MAX_ABS_FUNDING_RATE_PCT",
    "ALPHAFORGE_GLOBAL_KILL_SWITCH",
    "ALPHAFORGE_REQUIRE_LIVE_QUALIFICATION",
    "ALPHAFORGE_ENABLE_SHADOW_MODE",
    "ALPHAFORGE_ENABLE_CANARY_MODE",
    "ALPHAFORGE_OPERATOR_LIVE_ACKNOWLEDGED",
    "ALPHAFORGE_RECONCILIATION_INTERVAL_SEC",
    "ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC",
]


def _clear_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _RUNTIME_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_runtime_env_loads_canonical_runtime_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "paper")
    monkeypatch.setenv("EXECUTION_MODE", "live")
    monkeypatch.setenv("ALPHAFORGE_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("ALPHAFORGE_SCAN_INTERVAL_SEC", "0.25")
    monkeypatch.setenv("ALPHAFORGE_HEARTBEAT_INTERVAL_SEC", "12.5")
    monkeypatch.setenv("ALPHAFORGE_MAX_SYMBOLS_PER_SCAN", "9")
    monkeypatch.setenv("ALPHAFORGE_MAX_REJECT_LOG_ENTRIES", "77")
    monkeypatch.setenv("ALPHAFORGE_MAX_CONCURRENT_POSITIONS", "4")
    monkeypatch.setenv("ALPHAFORGE_SYMBOL_COOLDOWN_SEC", "900")
    monkeypatch.setenv("ALPHAFORGE_MAX_NOTIONAL_EXPOSURE", "123456")
    monkeypatch.setenv("ALPHAFORGE_MAX_SYMBOL_NOTIONAL", "34567")
    monkeypatch.setenv("ALPHAFORGE_STALE_MARKET_DATA_SEC", "8")
    monkeypatch.setenv("ALPHAFORGE_MAX_SPREAD_PCT", "0.0012")
    monkeypatch.setenv("ALPHAFORGE_MAX_ABS_FUNDING_RATE_PCT", "0.0007")
    monkeypatch.setenv("ALPHAFORGE_GLOBAL_KILL_SWITCH", "true")
    monkeypatch.setenv("ALPHAFORGE_REQUIRE_LIVE_QUALIFICATION", "false")
    monkeypatch.setenv("ALPHAFORGE_ENABLE_SHADOW_MODE", "true")
    monkeypatch.setenv("ALPHAFORGE_ENABLE_CANARY_MODE", "true")
    monkeypatch.setenv("ALPHAFORGE_OPERATOR_LIVE_ACKNOWLEDGED", "true")
    monkeypatch.setenv("ALPHAFORGE_RECONCILIATION_INTERVAL_SEC", "3")
    monkeypatch.setenv("ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC", "1.5")

    runtime = _build_runtime_from_env()
    cfg = runtime.config

    assert cfg.execution_mode == ExecutionMode.PAPER
    assert cfg.scan_interval_sec == pytest.approx(0.25)
    assert cfg.heartbeat_interval_sec == pytest.approx(12.5)
    assert cfg.max_symbols_per_scan == 9
    assert cfg.max_reject_log_entries == 77
    assert cfg.max_concurrent_positions == 4
    assert cfg.symbol_cooldown_sec == pytest.approx(900)
    assert cfg.max_notional_exposure == pytest.approx(123456)
    assert cfg.max_symbol_notional == pytest.approx(34567)
    assert cfg.stale_market_data_sec == pytest.approx(8)
    assert cfg.max_spread_pct == pytest.approx(0.0012)
    assert cfg.max_abs_funding_rate_pct == pytest.approx(0.0007)
    assert cfg.global_kill_switch is True
    assert cfg.require_live_qualification is False
    assert cfg.enable_shadow_mode is True
    assert cfg.enable_canary_mode is True
    assert cfg.operator_live_acknowledged is True
    assert cfg.reconciliation_interval_sec == pytest.approx(3)
    assert cfg.reconciliation_timeout_sec == pytest.approx(1.5)


def test_runtime_env_supports_position_and_db_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("EXECUTION_MODE", "backtest")
    monkeypatch.setenv("ALPHAFORGE_DB_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("ALPHAFORGE_MAX_OPEN_POSITIONS", "6")
    monkeypatch.setenv("ALPHAFORGE_MIN_ACCEPT_SCORE", "0.74")

    runtime = _build_runtime_from_env()

    assert runtime.config.execution_mode == ExecutionMode.BACKTEST
    assert runtime.config.max_concurrent_positions == 6
    assert runtime.ai_brain.min_accept_score == pytest.approx(0.74)


def test_runtime_env_canonical_values_override_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "paper")
    monkeypatch.setenv("EXECUTION_MODE", "live")
    monkeypatch.setenv("ALPHAFORGE_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("ALPHAFORGE_DB_URL", "sqlite+pysqlite:///ignored.sqlite3")
    monkeypatch.setenv("ALPHAFORGE_MAX_CONCURRENT_POSITIONS", "2")
    monkeypatch.setenv("ALPHAFORGE_MAX_OPEN_POSITIONS", "9")
    monkeypatch.setenv("ALPHAFORGE_MIN_SIGNAL_SCORE", "0.81")
    monkeypatch.setenv("ALPHAFORGE_MIN_ACCEPT_SCORE", "0.51")

    runtime = _build_runtime_from_env()

    assert runtime.config.execution_mode == ExecutionMode.PAPER
    assert runtime.config.max_concurrent_positions == 2
    assert runtime.ai_brain.min_accept_score == pytest.approx(0.81)


def test_runtime_env_fractional_percentage_values_are_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("ALPHAFORGE_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("ALPHAFORGE_MAX_SPREAD_PCT", "0.0025")
    monkeypatch.setenv("ALPHAFORGE_MAX_ABS_FUNDING_RATE_PCT", "0.0010")

    runtime = _build_runtime_from_env()

    assert runtime.config.max_spread_pct == pytest.approx(0.0025)
    assert runtime.config.max_abs_funding_rate_pct == pytest.approx(0.0010)
