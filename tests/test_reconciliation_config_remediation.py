import ast
import json
from pathlib import Path

import pytest


def _reconciliation_env() -> dict[str, str]:
    return {
        "BINANCE_ENVIRONMENT": "demo",
        "BINANCE_BASE_URL": "https://demo.example",
        "BINANCE_API_KEY": "key-value-never-print",
        "BINANCE_API_SECRET": "secret-value-never-print",
        "ALPHAFORGE_BINANCE_RECV_WINDOW_MS": "7000",
        "ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC": "3.5",
        "ALPHAFORGE_BINANCE_RECONCILIATION_TRADE_LOOKBACK_MS": "123456",
        "ALPHAFORGE_RECONCILIATION_POSITION_EPSILON": "0.000000010",
        "ALPHAFORGE_RECONCILIATION_MAX_FILL_SYMBOLS": "7",
    }


def test_exactly_one_canonical_reconciliation_loader_exists():
    definitions = []
    for path in Path("src/alphaforge").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        definitions.extend((path, node.name) for node in ast.walk(tree)
                           if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                           and node.name == "load_reconciliation_settings")
    assert definitions == [(Path("src/alphaforge/config/__init__.py"), "load_reconciliation_settings")]


def test_demo_rest_reconciliation_does_not_require_websocket():
    from alphaforge.config import load_reconciliation_settings
    settings = load_reconciliation_settings(env=_reconciliation_env())
    assert settings.environment == "demo"
    assert settings.base_url == "https://demo.example"


def test_demo_runtime_remains_strict_without_websocket(monkeypatch):
    from alphaforge.config import load_config_from_env
    for name, value in _reconciliation_env().items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("BINANCE_WS_URL", raising=False)
    with pytest.raises(ValueError, match="runtime requires explicit BINANCE_WS_URL"):
        load_config_from_env()


def test_runtime_burnin_and_cli_use_canonical_loader():
    sources = {
        name: Path(path).read_text(encoding="utf-8")
        for name, path in {
            "runtime": "src/alphaforge/runtime.py",
            "burnin": "src/alphaforge/burnin_ops.py",
            "cli": "src/alphaforge/binance_reconciliation_check.py",
        }.items()
    }
    assert all("load_reconciliation_settings()" in source for source in sources.values())
    assert "os.getenv(\"BINANCE_RECV_WINDOW_MS\"" not in "".join(sources.values())


def test_alias_conflict_fails_closed_and_process_mapping_is_deterministic():
    from alphaforge.config import load_reconciliation_settings
    env = _reconciliation_env()
    env["BINANCE_RECV_WINDOW_MS"] = "6000"
    with pytest.raises(ValueError, match="alias conflict"):
        load_reconciliation_settings(env=env)
    env["BINANCE_RECV_WINDOW_MS"] = "7000"
    first = load_reconciliation_settings(env=env)
    second = load_reconciliation_settings(env=dict(reversed(tuple(env.items()))))
    assert first == second
    assert first.recv_window_ms == 7000


def test_config_fix_is_safe_redacted_atomic_and_idempotent(tmp_path, capsys):
    from alphaforge.config_fix import main
    path = tmp_path / ".env"
    secret = "secret-value-never-print"
    original = (
        "BINANCE_RECV_WINDOW_MS=7000\n"
        f"BINANCE_API_SECRET={secret}\n"
        "ALPHAFORGE_ENABLE_LIVE_TRADING=false\n"
        "ALPHAFORGE_ALLOW_LIVE_ORDERS=false\n"
        "ALPHAFORGE_MAX_DAILY_LOSS_PCT=2.0\n"
    )
    path.write_text(original, encoding="utf-8")
    assert main(["--json", "--path", str(path)]) == 0
    dry_output = capsys.readouterr().out
    assert secret not in dry_output and path.read_text() == original
    assert main(["--json", "--apply", "--path", str(path)]) == 0
    applied_output = capsys.readouterr().out
    assert secret not in applied_output
    applied = path.read_text()
    assert "ALPHAFORGE_BINANCE_RECV_WINDOW_MS=7000" in applied
    assert f"BINANCE_API_SECRET={secret}" in applied
    assert "ALPHAFORGE_ENABLE_LIVE_TRADING=false" in applied
    assert "ALPHAFORGE_ALLOW_LIVE_ORDERS=false" in applied
    assert "ALPHAFORGE_MAX_DAILY_LOSS_PCT=2.0" in applied
    assert (tmp_path / ".env.bak").read_text() == original
    assert main(["--json", "--apply", "--path", str(path)]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["changed"] is False


def test_config_fix_never_guesses_conflicting_alias(tmp_path):
    from alphaforge.config_fix import remediate
    path = tmp_path / ".env"
    original = "ALPHAFORGE_BINANCE_RECV_WINDOW_MS=7000\nBINANCE_RECV_WINDOW_MS=6000\n"
    path.write_text(original)
    result = remediate(path, apply=True)
    assert result["status"] == "BLOCKED"
    assert path.read_text() == original
    assert not (tmp_path / ".env.bak").exists()
