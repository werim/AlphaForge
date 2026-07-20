from __future__ import annotations

import json

from alphaforge.binance_reconciliation_check import main


def test_check_missing_credentials_is_sanitized_and_nonzero(monkeypatch, capsys) -> None:
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.setenv("BINANCE_API_SECRET", "must-not-appear")
    assert main([]) == 2
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["evidence_status"] == "INCOMPLETE"
    assert payload["errors"] == [{"reason": "missing_binance_credentials"}]
    assert "must-not-appear" not in output
    assert "signature" not in output.lower()
