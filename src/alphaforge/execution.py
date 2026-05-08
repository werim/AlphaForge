from __future__ import annotations

from typing import Any, Mapping


def build_execution_context(market_ctx: Mapping[str, Any], funding_rate_pct: float | None = None) -> dict[str, Any]:
    klines = list(market_ctx.get("recent_klines", []) or [])
    expected_slippage_pct = _expected_slippage_pct(klines, market_ctx)
    spread_pct = float(market_ctx.get("spread_pct", _spread_pct_from_prices(market_ctx)) or 0.0)
    latency_ms = float(market_ctx.get("latency_ms", 50.0) or 50.0)
    orderbook_imbalance = float(market_ctx.get("orderbook_imbalance", 0.0) or 0.0)
    liquidity_score = float(market_ctx.get("liquidity_score", 1.0) or 1.0)
    funding = funding_rate_pct if funding_rate_pct is not None else market_ctx.get("funding_rate_pct", 0.0)
    funding_rate_pct_val = float(funding or 0.0)
    volatility_regime = str(market_ctx.get("volatility_regime", _volatility_regime(klines)))

    return {
        "expected_slippage_pct": max(expected_slippage_pct, 0.0),
        "latency_ms": max(latency_ms, 0.0),
        "spread_pct": max(spread_pct, 0.0),
        "orderbook_imbalance": max(min(orderbook_imbalance, 1.0), -1.0),
        "liquidity_score": max(min(liquidity_score, 1.0), 0.0),
        "funding_rate_pct": funding_rate_pct_val,
        "volatility_regime": volatility_regime,
        "spoof_risk": float(market_ctx.get("spoof_risk", 0.0) or 0.0),
        "absorption_score": float(market_ctx.get("absorption_score", 0.0) or 0.0),
    }


def neutral_execution_context() -> dict[str, Any]:
    return {
        "expected_slippage_pct": 0.0,
        "latency_ms": 50.0,
        "spread_pct": 0.0,
        "orderbook_imbalance": 0.0,
        "liquidity_score": 1.0,
        "funding_rate_pct": 0.0,
        "volatility_regime": "normal",
        "spoof_risk": 0.0,
        "absorption_score": 0.0,
    }


def _spread_pct_from_prices(market_ctx: Mapping[str, Any]) -> float:
    bid = float(market_ctx.get("best_bid", 0.0) or 0.0)
    ask = float(market_ctx.get("best_ask", 0.0) or 0.0)
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
    if mid <= 0:
        return 0.0
    return (ask - bid) / mid


def _expected_slippage_pct(klines: list[Any], market_ctx: Mapping[str, Any]) -> float:
    if not klines:
        return float(market_ctx.get("expected_slippage_pct", 0.001) or 0.001)
    highs, lows = [], []
    for k in klines[-20:]:
        if isinstance(k, Mapping):
            highs.append(float(k.get("high", 0.0) or 0.0))
            lows.append(float(k.get("low", 0.0) or 0.0))
    if not highs or not lows:
        return float(market_ctx.get("expected_slippage_pct", 0.001) or 0.001)
    avg_high = sum(highs) / len(highs)
    avg_low = sum(lows) / len(lows)
    if avg_high <= 0:
        return 0.001
    return max((avg_high - avg_low) / avg_high * 0.05, 0.0001)


def _volatility_regime(klines: list[Any]) -> str:
    if not klines:
        return "normal"
    ranges = []
    for k in klines[-20:]:
        if isinstance(k, Mapping):
            h = float(k.get("high", 0.0) or 0.0)
            l = float(k.get("low", 0.0) or 0.0)
            if h > 0:
                ranges.append((h - l) / h)
    if not ranges:
        return "normal"
    r = sum(ranges) / len(ranges)
    if r > 0.02:
        return "high"
    if r < 0.005:
        return "low"
    return "normal"
