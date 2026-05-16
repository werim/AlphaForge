from __future__ import annotations

from dataclasses import dataclass
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
        "spread_source": str(market_ctx.get("spread_source", "UNKNOWN") or "UNKNOWN"),
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
        "spread_source": "UNKNOWN",
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


@dataclass(frozen=True)
class ExecutionCostModel:
    spread_penalty: float
    slippage_penalty: float
    latency_penalty: float
    funding_penalty: float
    liquidity_penalty: float
    total_penalty: float
    missing_fields: tuple[str, ...]
    completeness: str


def build_execution_cost_model(execution_ctx: Mapping[str, Any], *, include_missing_penalty: bool = False) -> ExecutionCostModel:
    missing=[]
    def req_float(k:str):
        v=execution_ctx.get(k)
        if v in (None, '', 'UNKNOWN', 'UNAVAILABLE', 'UNAVAILABLE_BACKTEST'):
            missing.append(k); return None
        try:return float(v)
        except (TypeError,ValueError): missing.append(k); return None

    spread=req_float('spread_pct')
    slippage=req_float('expected_slippage_pct')
    latency=req_float('latency_ms')
    funding=req_float('funding_rate_pct')
    liquidity=req_float('liquidity_score')

    spread_penalty=max((spread or 0.0)*25.0,0.0)
    slippage_penalty=max((slippage or 0.0)*30.0,0.0)
    latency_penalty=max(((latency or 0.0)/1000.0)*0.2,0.0)
    funding_penalty=max(abs(funding or 0.0)*2.5,0.0)
    liquidity_penalty=max((1.0-max(min(liquidity if liquidity is not None else 1.0,1.0),0.0))*0.6,0.0)

    completeness='complete' if not missing else ('partial' if len(missing)<5 else 'unavailable')
    total=spread_penalty+slippage_penalty+latency_penalty+funding_penalty+liquidity_penalty
    if include_missing_penalty and missing:
        total += min(0.5, 0.1*len(missing))
    return ExecutionCostModel(spread_penalty,slippage_penalty,latency_penalty,funding_penalty,liquidity_penalty,round(total,6),tuple(missing),completeness)
