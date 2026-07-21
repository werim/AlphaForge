import json

from alphaforge import binance_reconciliation_check as check


def test_check_reaches_demo_provider_without_websocket_and_redacts_failure(monkeypatch):
    monkeypatch.setenv("BINANCE_ENVIRONMENT", "demo")
    monkeypatch.setenv("BINANCE_BASE_URL", "https://demo-fapi.binance.com")
    monkeypatch.setenv("BINANCE_WS_URL", "")
    monkeypatch.setenv("BINANCE_API_KEY", "key-never-print")
    monkeypatch.setenv("BINANCE_API_SECRET", "secret-never-print")
    monkeypatch.setattr(check.BinanceReadonlyReconciliationProvider, "snapshot", lambda self: {
        "evidence_status": "COMPLETE", "orders": [], "positions": [], "fills": []
    })
    report = check.run(["ETHUSDT", "BTCUSDT"])
    assert report["status"] == "COMPLETE"
    assert report["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert "never-print" not in json.dumps(report)


def test_check_fails_closed_without_credentials(monkeypatch):
    monkeypatch.setenv("BINANCE_ENVIRONMENT", "demo")
    monkeypatch.setenv("BINANCE_BASE_URL", "https://demo-fapi.binance.com")
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    report = check.run(["BTCUSDT"])
    assert report == {"status": "INCOMPLETE", "errors": ["ReconciliationAuthError:configuration_or_authentication_failed_redacted"]}
