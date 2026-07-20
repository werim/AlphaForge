import json
import pytest
import sys
from pathlib import Path
from alphaforge.binance_reconciliation_check import sanitize_position_risk


def test_sanitizer_keeps_only_safe_exact_fields(tmp_path):
    src = tmp_path / "raw.json"; out = tmp_path / "safe.json"
    src.write_text(json.dumps([{"symbol":"BTCUSDT", "positionAmt":"0.001", "positionSide":"BOTH", "entryPrice":"1", "unRealizedProfit":"2", "apiKey":"secret"}]))
    sanitize_position_risk(src, out)
    payload = json.loads(out.read_text())
    assert payload == [{"symbol":"BTCUSDT", "positionAmt":"0.001", "positionSide":"BOTH", "entryPrice":"1", "unRealizedProfit":"2"}]

from types import SimpleNamespace
import alphaforge.binance_reconciliation_check as check


def test_diagnostic_visibility_and_safe_distribution_write(monkeypatch, tmp_path):
    cfg = SimpleNamespace(
        binance=SimpleNamespace(base_url="https://demo-fapi.binance.com", environment="demo", recv_window_ms=7000),
        runtime=SimpleNamespace(reconciliation_timeout_sec=3, binance_reconciliation_trade_lookback_ms=1000,
                                reconciliation_position_epsilon="0.00000001", reconciliation_max_fill_symbols=10))
    snapshot = {
        "positions": [
            {"symbol":"BTCUSDT", "qty_exact":"1", "position_side":"BOTH", "entry_price":1, "unrealized_pnl":0, "symbol_valid":True, "exact_zero":False, "epsilon_filtered":False, "active":True},
            {"symbol":"INVALID_SYMBOL_SHA256_abc", "qty_exact":"0", "position_side":"BOTH", "entry_price":0, "unrealized_pnl":0, "symbol_valid":False, "exact_zero":True, "epsilon_filtered":False, "active":False},
        ],
        "orders": [], "selected_symbols":["BTCUSDT"], "symbol_sources":{"BTCUSDT":["active_position"]},
        "coverage":{"positionRisk":True,"openOrders":True,"userTrades":["BTCUSDT"]}, "selected_count":1,
        "request_count":3, "request_evidence":[], "failed_endpoint":None, "failed_symbol":None,
        "unknown_unreconciled_symbols":[], "position_warnings":[{"category":"zero_exposure_invalid_symbol","symbol":"INVALID_SYMBOL_SHA256_abc"}],
        "evidence_status":"COMPLETE", "errors":[],
    }
    class Provider:
        def __init__(self, **kwargs): pass
        def snapshot(self): return snapshot
    monkeypatch.setattr(check, "load_config_from_env", lambda: cfg)
    monkeypatch.setattr(check, "BinanceReadonlyReconciliationProvider", Provider)
    monkeypatch.setenv("BINANCE_API_KEY", "key"); monkeypatch.setenv("BINANCE_API_SECRET", "secret")
    destination = tmp_path / "artifacts" / "safe.json"
    result = check.run(write_sanitized_position_risk=destination)
    assert result["invalid_zero_exposure_symbol_count"] == 1
    assert result["invalid_nonzero_symbol_count"] == 0
    assert result["failed_endpoint"] is None and result["unknown_unreconciled_symbols"] == []
    blob = json.dumps(result) + destination.read_text()
    assert "key" not in blob and "secret" not in blob and "signature=" not in blob


def _cli_cfg():
    return SimpleNamespace(
        binance=SimpleNamespace(base_url="https://demo-fapi.binance.com", environment="demo", recv_window_ms=7000),
        runtime=SimpleNamespace(reconciliation_timeout_sec=3, binance_reconciliation_trade_lookback_ms=1000,
                                reconciliation_position_epsilon="0.00000001", reconciliation_max_fill_symbols=10))


def test_cli_tracked_symbols_exercise_campaign_fill_scope(monkeypatch):
    calls = []
    def http(self, url, headers, timeout):
        calls.append(url)
        return []
    monkeypatch.setattr(check, "load_config_from_env", _cli_cfg)
    monkeypatch.setattr(check.BinanceHttpTransport if hasattr(check, "BinanceHttpTransport") else __import__('alphaforge.binance_reconciliation_provider', fromlist=['BinanceHttpTransport']).BinanceHttpTransport, "get_json", http)
    monkeypatch.setenv("BINANCE_API_KEY", "k"); monkeypatch.setenv("BINANCE_API_SECRET", "s")
    result = check.run(symbols=check.parse_symbols("btcusdt,BTCUSDT,ethusdt"))
    assert result["requested_symbols"] == ["BTCUSDT", "BTCUSDT", "ETHUSDT"]
    assert result["tracked_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert result["tracked_scope_source"] == "CLI" and result["campaign_scope_validated"] is True
    assert result["selected_fill_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert sum("userTrades" in url for url in calls) == 2
    assert result["http_request_count"] == 4


def test_cli_no_symbol_mode_is_not_campaign_equivalent(monkeypatch):
    monkeypatch.setattr(check, "load_config_from_env", _cli_cfg)
    monkeypatch.setattr(check.BinanceReadonlyReconciliationProvider, "snapshot", lambda self: {"positions":[], "orders":[], "coverage":{"positionRisk":True,"openOrders":True,"userTrades":[]}, "selected_count":0, "evidence_status":"COMPLETE"})
    monkeypatch.setenv("BINANCE_API_KEY", "k"); monkeypatch.setenv("BINANCE_API_SECRET", "s")
    result = check.run()
    assert result["tracked_scope_source"] == "NONE" and result["campaign_scope_validated"] is False
    assert result["requested_symbols"] == result["tracked_symbols"] == []


@pytest.mark.parametrize("symbols", [["BTC/USDT"], ["éUSDT"]])
def test_cli_malformed_symbols_fail_before_network(monkeypatch, symbols):
    monkeypatch.setattr(check, "load_config_from_env", _cli_cfg)
    with pytest.raises(Exception, match="invalid_symbol"):
        check.run(symbols=symbols)


def test_cli_explicit_empty_symbols_exits_nonzero_safe_json(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["binance_reconciliation_check", "--symbols", ""])
    assert check.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["evidence_status"] == "INCOMPLETE"
