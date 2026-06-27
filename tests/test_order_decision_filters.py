from pathlib import Path


def test_order_has_no_local_production_threshold_fallback_dictionary():
    text = Path('src/alphaforge/order.py').read_text()
    assert '"MAX_TRADES_GLOBAL_PER_DAY": 10' not in text
    assert '"MAX_EXPECTED_SLIPPAGE_PCT": 0.05' not in text
    assert 'decision_filter_config' in text
