import json

from alphaforge.config_check import audit_config


def test_config_check_keeps_multi_error_and_redacts_secrets():
    secret = "do-not-print-this-secret"
    report = audit_config(env={
        "BINANCE_ENVIRONMENT": "demo",
        "BINANCE_BASE_URL": "https://fapi.binance.com",
        "BINANCE_API_SECRET": secret,
        "UNKNOWN": "ignored",
    })
    assert report["status"] == "FAIL"
    assert secret not in json.dumps(report)
    assert any("endpoint is production" in error for error in report["errors"])
