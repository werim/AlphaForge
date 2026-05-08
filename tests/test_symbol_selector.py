from alphaforge.symbol_selector import select_symbol, select_symbols


def test_high_quality_symbol_tradable_high_score():
    result = select_symbol(
        "BTCUSDT",
        {
            "volume_24h_usdt": 25_000_000,
            "spread_pct": 0.03,
            "volatility_pct": 3.0,
            "trend_strength": 0.8,
            "liquidity_score": 0.9,
            "recent_volume_change_pct": 8.0,
            "chop_score": 0.3,
        },
    )
    assert result.tradable is True
    assert result.symbol_score >= 7.0


def test_low_volume_rejected():
    result = select_symbol("ALTUSDT", {"volume_24h_usdt": 100_000})
    assert "LOW_VOLUME" in result.reject_reasons
    assert result.tradable is False


def test_wide_spread_rejected():
    result = select_symbol("ALTUSDT", {"spread_pct": 0.4})
    assert "WIDE_SPREAD" in result.reject_reasons


def test_excessive_volatility_rejected():
    result = select_symbol("ALTUSDT", {"volatility_pct": 20})
    assert "EXCESSIVE_VOLATILITY" in result.reject_reasons


def test_too_choppy_rejected_or_penalized():
    result = select_symbol(
        "ALTUSDT",
        {
            "volume_24h_usdt": 5_000_000,
            "spread_pct": 0.05,
            "volatility_pct": 2.0,
            "trend_strength": 0.2,
            "liquidity_score": 0.75,
            "recent_volume_change_pct": 5.0,
            "chop_score": 0.9,
        },
    )
    assert "TOO_CHOPPY" in result.reject_reasons
    assert result.symbol_score <= 7.0


def test_missing_fields_safe_defaults_and_diagnostics():
    result = select_symbol("NEWUSDT", {})
    assert isinstance(result.diagnostics, dict)
    assert "defaults_used" in result.diagnostics
    assert len(result.warnings) > 0


def test_select_symbols_sorted_by_score():
    candidates = [
        {
            "symbol": "MIDUSDT",
            "volume_24h_usdt": 5_000_000,
            "spread_pct": 0.06,
            "volatility_pct": 4.0,
            "trend_strength": 0.5,
            "liquidity_score": 0.7,
            "recent_volume_change_pct": 2.0,
            "chop_score": 0.4,
        },
        {
            "symbol": "TOPUSDT",
            "volume_24h_usdt": 20_000_000,
            "spread_pct": 0.03,
            "volatility_pct": 3.0,
            "trend_strength": 0.9,
            "liquidity_score": 0.95,
            "recent_volume_change_pct": 5.0,
            "chop_score": 0.25,
        },
    ]
    results = select_symbols(candidates)
    assert len(results) == 2
    assert results[0].symbol == "TOPUSDT"
    assert results[0].symbol_score >= results[1].symbol_score
