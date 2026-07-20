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
