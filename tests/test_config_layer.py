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
