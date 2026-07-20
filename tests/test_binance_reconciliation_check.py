import json
from pathlib import Path
from alphaforge.binance_reconciliation_check import sanitize_position_risk


def test_sanitizer_keeps_only_safe_exact_fields(tmp_path):
    src = tmp_path / "raw.json"; out = tmp_path / "safe.json"
    src.write_text(json.dumps([{"symbol":"BTCUSDT", "positionAmt":"0.001", "positionSide":"BOTH", "entryPrice":"1", "unRealizedProfit":"2", "apiKey":"secret"}]))
    sanitize_position_risk(src, out)
    payload = json.loads(out.read_text())
    assert payload == [{"symbol":"BTCUSDT", "positionAmt":"0.001", "positionSide":"BOTH", "entryPrice":"1", "unRealizedProfit":"2"}]
