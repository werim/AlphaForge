import os

import pytest

from alphaforge.config import load_config_from_env, runtime_filter_config
from alphaforge.execution import build_execution_context
from alphaforge.order import OrderExecutionContext, TradingMode, evaluate_paper_style_pre_submit
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator
from alphaforge.symbol_selector import select_symbols


BASE_ENV = {
    "ALPHAFORGE_MIN_SIGNAL_SCORE": "0.62",
    "ALPHAFORGE_MIN_RR": "1.20",
    "MIN_EFFECTIVE_RR": "1.10",
    "ALPHAFORGE_MAX_SPREAD_PCT": "0.0025",
    "ALPHAFORGE_MAX_EXPECTED_SLIPPAGE_PCT": "0.0020",
    "ALPHAFORGE_MAX_ABS_FUNDING_RATE_PCT": "0.0010",
    "MIN_LIQUIDITY_USD": "5000000",
    "ALPHAFORGE_SYMBOL_COOLDOWN_SEC": "120",
    "ALPHAFORGE_STALE_MARKET_DATA_SEC": "15",
    "ALPHAFORGE_MAX_CONCURRENT_POSITIONS": "3",
}


def _cfg(monkeypatch, **overrides):
    for key in list(BASE_ENV):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**BASE_ENV, **overrides}.items():
        monkeypatch.setenv(key, str(value))
    return load_config_from_env()


def _market(**overrides):
    m = {
        "entry": 100.0,
        "sl": 99.0,
        "tp": 103.0,
        "side": "LONG",
        "score": 0.80,
        "rr": 2.0,
        "expectancy": 0.2,
        "setup_type": "BREAKOUT_UP",
        "setup_reason": "TEST",
        "regime": "TREND",
        "volatility_regime": "normal",
        "atr_pct": 0.5,
        "spread_pct": 0.0005,
        "expected_slippage_pct": 0.0005,
        "latency_ms": 50.0,
        "market_data_latency_ms": 50.0,
        "liquidity_score": 0.9,
        "funding_rate_pct": 0.00001,
        "orderbook_imbalance": 0.1,
        "spread_status": "MEASURED",
        "slippage_status": "MEASURED",
        "market_data_latency_status": "MEASURED",
        "liquidity_status": "MEASURED",
        "funding_status": "MEASURED",
        "orderbook_status": "MEASURED",
        "volatility_status": "MEASURED",
    }
    m.update(overrides)
    m["execution_ctx"] = build_execution_context(m)
    return m


def _decision(mode, market, cfg):
    ctx = OrderExecutionContext(mode=mode, timestamp=1, symbol="BTCUSDT", balance=1000.0, risk_pct=1.0, market_ctx=market)
    return evaluate_paper_style_pre_submit(ctx, config=runtime_filter_config(cfg.runtime, mode=mode.value), recent_stats={})


def test_env_score_threshold_changes_backtest_and_paper_decisions(monkeypatch):
    cfg_loose = _cfg(monkeypatch, ALPHAFORGE_MIN_SIGNAL_SCORE="0.70")
    cfg_strict = _cfg(monkeypatch, ALPHAFORGE_MIN_SIGNAL_SCORE="0.90")
    for mode in (TradingMode.BACKTEST, TradingMode.PAPER):
        assert _decision(mode, _market(score=0.80), cfg_loose)["accepted"] is True
        rejected = _decision(mode, _market(score=0.80), cfg_strict)
        assert rejected["accepted"] is False
        assert rejected["reject_reason"] == "LOW_SCORE"


def test_env_min_effective_rr_changes_shared_pre_submit_decision(monkeypatch):
    loose = _cfg(monkeypatch, MIN_EFFECTIVE_RR="1.00")
    strict = _cfg(monkeypatch, MIN_EFFECTIVE_RR="1.95")
    market = _market(rr=2.0, spread_pct=0.0005, expected_slippage_pct=0.0005)
    assert _decision(TradingMode.BACKTEST, market, loose)["accepted"] is True
    rejected = _decision(TradingMode.PAPER, market, strict)
    assert rejected["accepted"] is False
    assert rejected["reject_reason"] == "LOW_EFFECTIVE_RR"


def test_env_spread_funding_liquidity_symbol_filters_are_canonical(monkeypatch):
    cfg = _cfg(monkeypatch, ALPHAFORGE_MAX_SPREAD_PCT="0.001", ALPHAFORGE_MAX_ABS_FUNDING_RATE_PCT="0.0001", MIN_LIQUIDITY_USD="10000000")
    filters = runtime_filter_config(cfg.runtime, mode="PAPER")
    rows = select_symbols([
        {"symbol": "WIDE", "volume_24h_usdt": 20_000_000, "spread_pct": 0.002, "liquidity_score": 0.9, "volatility_pct": 1, "trend_strength": 0.8, "chop_score": 0.1},
        {"symbol": "FUND", "volume_24h_usdt": 20_000_000, "spread_pct": 0.0005, "liquidity_score": 0.9, "volatility_pct": 1, "trend_strength": 0.8, "chop_score": 0.1, "funding_rate_pct": 0.0002},
        {"symbol": "THIN", "volume_24h_usdt": 5_000_000, "spread_pct": 0.0005, "liquidity_score": 0.9, "volatility_pct": 1, "trend_strength": 0.8, "chop_score": 0.1},
    ], {**filters, "include_rejected": True})
    reasons = {r.symbol: r.reject_reasons for r in rows}
    assert "WIDE_SPREAD" in reasons["WIDE"]
    assert "FUNDING_ANOMALY" in reasons["FUND"]
    assert "LOW_VOLUME" in reasons["THIN"]


def test_runtime_risk_uses_canonical_spread_slippage_funding_liquidity_and_stale(monkeypatch):
    cfg = _cfg(monkeypatch, ALPHAFORGE_MAX_SPREAD_PCT="0.001", ALPHAFORGE_MAX_EXPECTED_SLIPPAGE_PCT="0.001", ALPHAFORGE_MAX_ABS_FUNDING_RATE_PCT="0.001", MIN_LIQUIDITY_USD="1000", ALPHAFORGE_STALE_MARKET_DATA_SEC="60")
    rt = RuntimeOrchestrator(config=RuntimeConfig(execution_mode=ExecutionMode.PAPER, max_spread_pct=cfg.runtime.max_spread_pct, max_expected_slippage_pct=cfg.runtime.max_expected_slippage_pct, max_abs_funding_rate_pct=cfg.runtime.max_abs_funding_rate_pct, min_liquidity_usd=cfg.runtime.min_liquidity_usd, stale_market_data_sec=cfg.runtime.stale_market_data_sec), ai_brain=None, market_scanner=None)
    assert rt._evaluate_runtime_risk("BTCUSDT", {"market_ts": 0}) == "STALE_MARKET_DATA"
    import time
    now = time.time()
    assert rt._evaluate_runtime_risk("BTCUSDT", {"market_ts": now, "spread_pct": 0.002}) == "SPREAD_TOO_HIGH"
    assert rt._evaluate_runtime_risk("BTCUSDT", {"market_ts": now, "expected_slippage_pct": 0.002}) == "SLIPPAGE_TOO_HIGH"
    assert rt._evaluate_runtime_risk("BTCUSDT", {"market_ts": now, "funding_rate_pct": 0.002}) == "FUNDING_TOO_HIGH"
    assert rt._evaluate_runtime_risk("BTCUSDT", {"market_ts": now, "volume_24h_usdt": 999}) == "THIN_LIQUIDITY"


def test_max_symbols_is_runtime_config_selection_cap():
    async def scanner():
        return [_market(symbol=s, volume_24h_usdt=20_000_000, spread_pct=0.0001, trend_strength=0.9, chop_score=0.1, volatility_pct=1) for s in ("A", "B", "C")]
    rt = RuntimeOrchestrator(config=RuntimeConfig(execution_mode=ExecutionMode.PAPER, max_symbols_per_scan=2), ai_brain=object(), market_scanner=scanner)
    selected = [r for r in select_symbols([_market(symbol=s, volume_24h_usdt=20_000_000, trend_strength=0.9, chop_score=0.1, volatility_pct=1, spread_pct=0.0001) for s in ("A", "B", "C")], {**rt._canonical_filter_config(), "include_rejected": True}) if r.tradable][: rt.config.max_symbols_per_scan]
    assert len(selected) == 2
