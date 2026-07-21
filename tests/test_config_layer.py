from pathlib import Path

from alphaforge.config import load_config_from_env
from alphaforge.runtime import _build_runtime_from_env


def test_config_defaults_load():
    cfg = load_config_from_env()
    assert cfg.runtime.execution_mode in {"PAPER", "BACKTEST", "LIVE"}
    assert cfg.backtest.top_n > 0


def test_env_aliases_work(monkeypatch):
    monkeypatch.setenv("EXECUTION_MODE", "BACKTEST")
    monkeypatch.setenv("ALPHAFORGE_MIN_ACCEPT_SCORE", "0.73")
    monkeypatch.setenv("ALPHAFORGE_MAX_OPEN_POSITIONS", "9")
    cfg = load_config_from_env()
    assert cfg.runtime.execution_mode == "BACKTEST"
    assert cfg.runtime.min_signal_score == 0.73
    assert cfg.runtime.max_concurrent_positions == 9


def test_runtime_receives_config_values(monkeypatch):
    monkeypatch.setenv("ALPHAFORGE_SCAN_INTERVAL_SEC", "0.4")
    rt = _build_runtime_from_env()
    assert rt.config.scan_interval_sec == 0.4


def test_env_example_keys_are_wired_or_reserved():
    content = Path('.env.example').read_text().splitlines()
    keys = [line.split('=',1)[0].strip() for line in content if line and not line.startswith('#') and '=' in line]
    known = set(Path('src/alphaforge/config.py').read_text().split('"'))
    for key in keys:
        assert key in known or 'RESERVED_NOT_WIRED' in '\n'.join(content)


def test_default_binance_base_url_is_futures_host(monkeypatch):
    monkeypatch.delenv("BINANCE_BASE_URL", raising=False)
    cfg = load_config_from_env()
    assert cfg.exchange.binance.base_url == "https://fapi.binance.com"


def test_daily_loss_unit_is_fraction_and_two_percent_is_point_zero_two(monkeypatch):
    import pytest
    from alphaforge.config import load_config_from_env
    monkeypatch.setenv("ALPHAFORGE_MAX_DAILY_LOSS_PCT", "0.02")
    assert load_config_from_env().runtime.max_daily_loss_pct == pytest.approx(0.02)
    monkeypatch.setenv("ALPHAFORGE_MAX_DAILY_LOSS_PCT", "2.0")
    with pytest.raises(ValueError, match="above maximum 1.0"):
        load_config_from_env()


def test_daily_loss_fraction_boundaries_and_real_guard():
    import pytest
    from alphaforge.config_registry import effective_config_subset
    from alphaforge.portfolio_risk import PortfolioRiskSnapshot, evaluate_portfolio_risk
    for value in ("0", "1"):
        assert effective_config_subset(("ALPHAFORGE_MAX_DAILY_LOSS_PCT",), env={"ALPHAFORGE_MAX_DAILY_LOSS_PCT":value})
    for value in ("-0.01", "1.01"):
        with pytest.raises(ValueError):
            effective_config_subset(("ALPHAFORGE_MAX_DAILY_LOSS_PCT",), env={"ALPHAFORGE_MAX_DAILY_LOSS_PCT":value})
    snapshot = PortfolioRiskSnapshot(mode="PAPER", timestamp=0.0, equity=1000, open_position_count=0,
                                     total_notional_exposure=0, symbol_notional_exposure=0,
                                     daily_loss_pct=0.02, max_daily_loss_pct=0.02)
    decision = evaluate_portfolio_risk({"symbol":"BTCUSDT", "notional":10}, snapshot, {}, mode="PAPER")
    assert not decision.accepted and decision.reject_reason == "MAX_DAILY_LOSS"


def test_narrow_reconciliation_settings_match_runtime(monkeypatch):
    from alphaforge.config import load_config_from_env, load_reconciliation_settings
    monkeypatch.setenv("ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC", "3.5")
    monkeypatch.setenv("ALPHAFORGE_BINANCE_RECV_WINDOW_MS", "7000")
    monkeypatch.setenv("ALPHAFORGE_BINANCE_RECONCILIATION_TRADE_LOOKBACK_MS", "123456")
    monkeypatch.setenv("ALPHAFORGE_RECONCILIATION_POSITION_EPSILON", "0.000001")
    monkeypatch.setenv("ALPHAFORGE_RECONCILIATION_MAX_FILL_SYMBOLS", "7")
    full = load_config_from_env(); narrow = load_reconciliation_settings()
    assert narrow.base_url == full.binance.base_url
    assert narrow.timeout_sec == full.runtime.reconciliation_timeout_sec
    assert narrow.recv_window_ms == full.binance.recv_window_ms
    assert narrow.trade_lookback_ms == full.runtime.binance_reconciliation_trade_lookback_ms
    assert narrow.position_epsilon == full.runtime.reconciliation_position_epsilon
    assert narrow.max_fill_symbols == full.runtime.reconciliation_max_fill_symbols


def test_narrow_reconciliation_settings_ignore_unrelated_invalid_risk(monkeypatch):
    import pytest
    from alphaforge.config import load_config_from_env, load_reconciliation_settings
    monkeypatch.setenv("ALPHAFORGE_MAX_DAILY_LOSS_PCT", "2.0")
    narrow = load_reconciliation_settings()
    assert narrow.timeout_sec > 0
    with pytest.raises(ValueError, match="ALPHAFORGE_MAX_DAILY_LOSS_PCT above maximum"):
        load_config_from_env()


def test_full_loader_normalizes_safe_binance_market_aliases(monkeypatch):
    from alphaforge.config import load_config_from_env
    for value in ("USD_M", "USDT_M", "USD-M", "USDT-M"):
        monkeypatch.setenv("BINANCE_DEFAULT_MARKET_TYPE", value)
        assert load_config_from_env().binance.default_market_type == "USD_M"
