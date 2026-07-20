import json
import os
from pathlib import Path

import pytest

from alphaforge.config import load_config_from_env
from alphaforge.config_audit import TEMPLATES, audit_config
from alphaforge.config_registry import CONTRACT_BY_NAME
from alphaforge.env_contract import (
    PRODUCTION_REST_URL, TESTNET_REST_URL, bootstrap_environment, parse_dotenv,
    resolve_binance_environment,
)


def _keys(path: Path) -> list[str]:
    return [line.split("=", 1)[0].strip() for line in path.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#") and "=" in line]


def test_every_template_variable_is_classified_once_and_has_no_duplicates():
    for filename in TEMPLATES:
        keys = _keys(Path(filename))
        assert len(keys) == len(set(keys))
        assert all(key in CONTRACT_BY_NAME for key in keys)
        assert all(CONTRACT_BY_NAME[key].classification in {"WIRED", "ALIAS", "RESERVED"} for key in keys)


def test_repository_templates_pass_config_audit():
    report = audit_config(env={})
    assert report["status"] == "PASS", json.dumps(report, indent=2)
    assert not report["duplicate_template_variables"]


def test_dotenv_process_precedence_comments_quotes_and_worker_inheritance(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "src" / "alphaforge").mkdir(parents=True)
    path = tmp_path / ".env"
    path.write_text('PLAIN=value # comment\nQUOTED="value#inside" # comment\nSINGLE=\'x#y\'\n')
    assert parse_dotenv(path) == {"PLAIN": "value", "QUOTED": "value#inside", "SINGLE": "x#y"}
    child_environment = {"PLAIN": "process"}
    result = bootstrap_environment(tmp_path, environ=child_environment)
    assert result.loaded
    assert child_environment == {"PLAIN": "process", "QUOTED": "value#inside", "SINGLE": "x#y"}


def test_binance_explicit_override_and_testnet_backward_compatibility():
    selected = resolve_binance_environment({"BINANCE_TESTNET": "true"})
    assert selected.environment == "testnet"
    assert selected.rest_base_url == TESTNET_REST_URL
    assert selected.rest_base_url != PRODUCTION_REST_URL
    explicit = resolve_binance_environment({"BINANCE_ENVIRONMENT": "testnet", "BINANCE_BASE_URL": "https://proxy.example", "BINANCE_WS_URL": "wss://proxy.example"})
    assert explicit.rest_base_url == "https://proxy.example"


@pytest.mark.parametrize("env", [
    {"BINANCE_ENVIRONMENT": "production", "BINANCE_TESTNET": "true"},
    {"BINANCE_ENVIRONMENT": "testnet", "BINANCE_BASE_URL": PRODUCTION_REST_URL},
    {"BINANCE_ENVIRONMENT": "demo"},
])
def test_binance_contradictions_fail_closed(env):
    with pytest.raises(ValueError):
        resolve_binance_environment(env)


def test_reconciliation_and_scanner_share_resolved_binance_url(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.delenv("BINANCE_ENVIRONMENT", raising=False)
    monkeypatch.delenv("BINANCE_BASE_URL", raising=False)
    monkeypatch.delenv("BINANCE_WS_URL", raising=False)
    cfg = load_config_from_env()
    assert cfg.binance.base_url == TESTNET_REST_URL
    assert cfg.exchange.binance is cfg.binance


def test_secret_audit_never_emits_secret_and_placeholder_fails():
    secret = "replace_with_real_secret"
    report = audit_config(env={"ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION": "true", "BINANCE_API_KEY": "replace_key", "BINANCE_API_SECRET": secret})
    assert report["status"] == "FAIL"
    assert secret not in json.dumps(report)
    row = report["resolved_non_secret_configuration"]["BINANCE_API_SECRET"]
    assert row["present"] and row["placeholder_detected"]
