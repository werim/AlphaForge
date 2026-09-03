from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from alphaforge.persistence import init_db

from alphaforge.runtime import ExecutionMode, _build_runtime_from_env


def test_explicit_persistence_dependencies_override_env_for_all_runtime_consumers(monkeypatch, tmp_path) -> None:
    env_db = tmp_path / "env.db"
    campaign_db = tmp_path / "campaign.db"
    monkeypatch.setenv("ALPHAFORGE_DATABASE_URL", f"sqlite+pysqlite:///{env_db}")
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    engine = init_db(f"sqlite+pysqlite:///{campaign_db}")
    runtime = _build_runtime_from_env(persistence_engine=engine)

    assert Path(runtime.persistence_engine.url.database).resolve() == campaign_db.resolve()
    with runtime.ai_brain.session_factory() as session:
        assert Path(session.bind.url.database).resolve() == campaign_db.resolve()
    runtime.on_lifecycle_event({"signal_id": "canonical-signal", "symbol": "BTCUSDT", "mode": "PAPER", "lifecycle_state": "SIGNAL_CREATED", "timestamp": "2026-08-17T00:00:00Z", "details": {}})
    runtime.on_reject_persist({"signal_id": "canonical-reject", "symbol": "BTCUSDT", "phase": "final", "reason": "LOW_CONFIDENCE", "confidence": .1, "score": .1, "rr": 1.0, "execution_ctx": {"evidence_status": "UNAVAILABLE"}})
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM trade_lifecycle_events WHERE signal_id='canonical-signal'")).scalar_one() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM order_decisions WHERE signal_id='canonical-reject' AND decision='REJECTED'")).scalar_one() == 1
    assert not env_db.exists()
    engine.dispose()


def test_runtime_env_prefers_canonical_execution_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "live")
    monkeypatch.setenv("EXECUTION_MODE", "paper")
    rt = _build_runtime_from_env()
    assert rt.config.execution_mode == ExecutionMode.LIVE



def test_runtime_env_strips_inline_comments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER # BACKTEST | PAPER | LIVE")
    monkeypatch.setenv("ALPHAFORGE_MAX_CONCURRENT_POSITIONS", "3 # safe cap")
    monkeypatch.setenv("ALPHAFORGE_MAX_SPREAD_PCT", "0.0025 # 0.25 percent")
    monkeypatch.setenv("ALPHAFORGE_ENABLE_SHADOW_MODE", "true # enabled")

    rt = _build_runtime_from_env()
    assert rt.config.execution_mode == ExecutionMode.PAPER
    assert rt.config.max_concurrent_positions == 3
    assert rt.config.max_spread_pct == pytest.approx(0.0025)
    assert rt.config.enable_shadow_mode is True

def test_runtime_env_aliases_for_threshold_and_positions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPHAFORGE_MIN_ACCEPT_SCORE", "0.77")
    monkeypatch.setenv("ALPHAFORGE_MAX_OPEN_POSITIONS", "7")
    rt = _build_runtime_from_env()
    assert rt.ai_brain.min_accept_score == pytest.approx(0.77)
    assert rt.config.max_concurrent_positions == 7


def test_runtime_env_prefers_canonical_names_over_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPHAFORGE_MIN_SIGNAL_SCORE", "0.81")
    monkeypatch.setenv("ALPHAFORGE_MIN_ACCEPT_SCORE", "0.75")
    monkeypatch.setenv("ALPHAFORGE_MAX_CONCURRENT_POSITIONS", "4")
    monkeypatch.setenv("ALPHAFORGE_MAX_OPEN_POSITIONS", "9")
    rt = _build_runtime_from_env()
    assert rt.ai_brain.min_accept_score == pytest.approx(0.81)
    assert rt.config.max_concurrent_positions == 4


def test_runtime_env_db_url_prefers_alphaforge_database_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    canonical = tmp_path / "canonical.sqlite3"
    legacy = tmp_path / "legacy.sqlite3"
    monkeypatch.setenv("ALPHAFORGE_DATABASE_URL", f"sqlite+pysqlite:///{canonical}")
    monkeypatch.setenv("ALPHAFORGE_DB_URL", f"sqlite+pysqlite:///{legacy}")
    rt = _build_runtime_from_env()
    db_url = str(rt.ai_brain.session_factory().get_bind().url)
    assert str(canonical.resolve()) in db_url
    assert str(legacy.resolve()) not in db_url


def test_runtime_env_loads_runtime_config_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPHAFORGE_SCAN_INTERVAL_SEC", "0.2")
    monkeypatch.setenv("ALPHAFORGE_HEARTBEAT_INTERVAL_SEC", "0.3")
    monkeypatch.setenv("ALPHAFORGE_MAX_SYMBOLS_PER_SCAN", "8")
    monkeypatch.setenv("ALPHAFORGE_MAX_REJECT_LOG_ENTRIES", "200")
    monkeypatch.setenv("ALPHAFORGE_SYMBOL_COOLDOWN_SEC", "99")
    monkeypatch.setenv("ALPHAFORGE_MAX_NOTIONAL_EXPOSURE", "120000")
    monkeypatch.setenv("ALPHAFORGE_MAX_SYMBOL_NOTIONAL", "22000")
    monkeypatch.setenv("ALPHAFORGE_STALE_MARKET_DATA_SEC", "11")
    monkeypatch.setenv("ALPHAFORGE_MAX_SPREAD_PCT", "0.0015")
    monkeypatch.setenv("ALPHAFORGE_MAX_ABS_FUNDING_RATE_PCT", "0.0008")
    monkeypatch.setenv("ALPHAFORGE_GLOBAL_KILL_SWITCH", "true")
    monkeypatch.setenv("ALPHAFORGE_REQUIRE_LIVE_QUALIFICATION", "false")
    monkeypatch.setenv("ALPHAFORGE_ENABLE_SHADOW_MODE", "1")
    monkeypatch.setenv("ALPHAFORGE_ENABLE_CANARY_MODE", "yes")
    monkeypatch.setenv("ALPHAFORGE_OPERATOR_LIVE_ACKNOWLEDGED", "on")
    monkeypatch.setenv("ALPHAFORGE_RECONCILIATION_INTERVAL_SEC", "7")
    monkeypatch.setenv("ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC", "3")
    monkeypatch.setenv("ALPHAFORGE_MTF_GUIDED_SIGNAL_GENERATION_ENABLED", "false")

    rt = _build_runtime_from_env()
    cfg = rt.config
    assert cfg.scan_interval_sec == pytest.approx(0.2)
    assert cfg.heartbeat_interval_sec == pytest.approx(0.3)
    assert cfg.max_symbols_per_scan == 8
    assert cfg.max_reject_log_entries == 200
    assert cfg.symbol_cooldown_sec == pytest.approx(99)
    assert cfg.max_notional_exposure == pytest.approx(120000)
    assert cfg.max_symbol_notional == pytest.approx(22000)
    assert cfg.stale_market_data_sec == pytest.approx(11)
    assert cfg.max_spread_pct == pytest.approx(0.0015)
    assert cfg.max_abs_funding_rate_pct == pytest.approx(0.0008)
    assert cfg.global_kill_switch is True
    assert cfg.require_live_qualification is False
    assert cfg.enable_shadow_mode is True
    assert cfg.enable_canary_mode is True
    assert cfg.operator_live_acknowledged is True
    assert cfg.reconciliation_interval_sec == pytest.approx(7)
    assert cfg.reconciliation_timeout_sec == pytest.approx(3)
    assert cfg.mtf_guided_signal_generation_enabled is False


def test_mtf_guided_signal_generation_env_controls_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "PAPER")
    monkeypatch.setenv("ALPHAFORGE_RUNTIME_SAFE_SCANNER", "false")
    monkeypatch.setenv("ALPHAFORGE_MTF_GUIDED_SIGNAL_GENERATION_ENABLED", "false")
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'mtf-env.sqlite3'}")

    rt = _build_runtime_from_env(persistence_engine=engine)

    assert rt.config.mtf_guided_signal_generation_enabled is False
    assert rt.mtf_context_provider is not None
    assert rt.mtf_context_provider.guided_signal_generation_enabled is False
    engine.dispose()


def test_live_reconciliation_enabled_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "LIVE")
    monkeypatch.setenv("ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION", "true")
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="credentials are missing"):
        _build_runtime_from_env()
