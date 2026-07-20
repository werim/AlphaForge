import json

from alphaforge.config_check import audit_settings, main


def test_multi_error_audit_is_structured_and_deterministic():
    report = audit_settings(env={"ALPHAFORGE_MAX_DAILY_LOSS_PCT":"2.0", "BINANCE_DEFAULT_MARKET_TYPE":"SPOT"})
    relevant = [row for row in report["errors"] if row["setting"] in {"ALPHAFORGE_MAX_DAILY_LOSS_PCT", "BINANCE_DEFAULT_MARKET_TYPE"}]
    assert relevant == [
        {"type":"ValueError", "reason":"setting_above_maximum", "setting":"ALPHAFORGE_MAX_DAILY_LOSS_PCT",
         "allowed_range":{"minimum":0.0, "maximum":1.0}, "expected_unit":"fraction",
         "migration":"use 0.02 for two percent; 2.0 is intentionally invalid", "provided_value":"2.0"},
        {"type":"ValueError", "reason":"unsupported_market_type", "setting":"BINANCE_DEFAULT_MARKET_TYPE", "provided_value":"REDACTED"},
    ]


def test_config_check_never_emits_secret_values(monkeypatch, capsys):
    monkeypatch.setenv("BINANCE_API_KEY", "operator-key-secret")
    monkeypatch.setenv("BINANCE_API_SECRET", "operator-api-secret")
    code = main()
    payload = capsys.readouterr().out
    assert code in {0, 2}
    assert "operator-key-secret" not in payload and "operator-api-secret" not in payload
    parsed = json.loads(payload)
    assert parsed["settings"]["BINANCE_API_KEY"]["is_set"] is True


def test_config_provenance_process_over_dotenv_and_dotenv_quotes(tmp_path):
    from alphaforge.config_registry import effective_config_subset
    (tmp_path / ".env").write_text('ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC="3.5" # safe comment\n')
    name = "ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC"
    dotenv = effective_config_subset((name,), env={}, root=tmp_path)[name]
    process = effective_config_subset((name,), env={name:"4.5"}, root=tmp_path)[name]
    assert dotenv["value"] == 3.5 and dotenv["source"] == "dotenv"
    assert process["value"] == 4.5 and process["source"] == "process_env"


def _temporary_repo(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='temporary'\nversion='0'\n")
    (tmp_path / "src" / "alphaforge").mkdir(parents=True)
    return tmp_path


def test_config_check_cli_bootstraps_dotenv_once_and_redacts_secrets(monkeypatch, tmp_path, capsys):
    import alphaforge.config_check as module
    root = _temporary_repo(tmp_path)
    (root / ".env").write_text('BINANCE_API_KEY="dotenv-key" # comment\nBINANCE_API_SECRET="dotenv-secret"\nALPHAFORGE_RECONCILIATION_TIMEOUT_SEC="3.5" # seconds\n')
    monkeypatch.chdir(root)
    for key in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC"):
        monkeypatch.delenv(key, raising=False)
    real = module.bootstrap_environment; calls = []
    monkeypatch.setattr(module, "bootstrap_environment", lambda: (calls.append(1), real())[1])
    assert module.main() == 0
    payload_text = capsys.readouterr().out; payload = json.loads(payload_text)
    assert calls == [1]
    assert "dotenv-key" not in payload_text and "dotenv-secret" not in payload_text
    assert payload["settings"]["BINANCE_API_KEY"] == {"source":"DOTENV", "is_set":True}
    assert payload["settings"]["ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC"] == {"source":"DOTENV", "value":3.5}


def test_process_environment_overrides_dotenv_with_visible_source(monkeypatch, tmp_path, capsys):
    import alphaforge.config_check as module
    root = _temporary_repo(tmp_path)
    (root / ".env").write_text("ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC=3.5\n")
    monkeypatch.chdir(root); monkeypatch.setenv("ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC", "4.5")
    assert module.main() == 0
    row = json.loads(capsys.readouterr().out)["settings"]["ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC"]
    assert row == {"source":"PROCESS_ENV", "value":4.5}


def test_explicit_mappings_are_isolated_from_host_and_dotenv(monkeypatch, tmp_path):
    from alphaforge.config import load_reconciliation_settings
    root = _temporary_repo(tmp_path)
    (root / ".env").write_text("ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC=9.0\nBINANCE_API_KEY=host-key\n")
    monkeypatch.chdir(root); monkeypatch.setenv("ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC", "8.0")
    explicit = {"ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC":"2.5", "BINANCE_API_KEY":"explicit-key", "BINANCE_API_SECRET":"explicit-secret"}
    settings = load_reconciliation_settings(env=explicit)
    report = audit_settings(env=explicit)
    assert settings.timeout_sec == 2.5 and settings.api_key == "explicit-key"
    assert settings.sources["ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC"] == "PROCESS_ENV"
    assert report["settings"]["ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC"]["value"] == 2.5
