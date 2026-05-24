from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


def build_execution_context(market_ctx: Mapping[str, Any], funding_rate_pct: float | None = None) -> dict[str, Any]:
    klines = list(market_ctx.get("recent_klines", []) or [])
    expected_slippage_pct, slippage_unit_assumed = normalize_pct_input(
        market_ctx.get("expected_slippage_pct", _expected_slippage_pct(klines, market_ctx)),
        field="expected_slippage_pct",
    )
    raw_spread = market_ctx.get("spread_pct", _spread_pct_from_prices(market_ctx))
    spread_pct, spread_unit_assumed = normalize_pct_input(raw_spread, field="spread_pct")

    def _to_float(v: Any) -> float | None:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    measured_spread = _to_float(raw_spread) is not None
    spread_status = str(market_ctx.get("spread_status", "MEASURED" if measured_spread else "UNAVAILABLE"))
    spread_source = str(market_ctx.get("spread_source", "BOOK_TICKER" if measured_spread else "UNAVAILABLE"))

    slippage_status = str(market_ctx.get("slippage_status", "MODEL_ESTIMATE"))
    slippage_source = str(market_ctx.get("slippage_source", "KLINE_RANGE_MODEL"))

    md_latency = _to_float(market_ctx.get("market_data_latency_ms"))
    md_latency_status = str(market_ctx.get("market_data_latency_status", "MEASURED" if md_latency is not None else "UNAVAILABLE"))
    md_latency_source = str(market_ctx.get("market_data_latency_source", "UNKNOWN" if md_latency is not None else "UNAVAILABLE"))

    submit_ack = _to_float(market_ctx.get("submit_ack_latency_ms"))
    submit_ack_status = str(market_ctx.get("submit_ack_latency_status", "UNAVAILABLE" if submit_ack is None else "MEASURED"))
    submit_ack_source = str(market_ctx.get("submit_ack_latency_source", "UNAVAILABLE" if submit_ack is None else "RUNTIME_ACK"))

    funding = funding_rate_pct if funding_rate_pct is not None else market_ctx.get("funding_rate_pct")
    funding_val = _to_float(funding)
    funding_status = str(market_ctx.get("funding_status", "MEASURED" if funding_val is not None else "UNAVAILABLE"))
    funding_source = str(market_ctx.get("funding_source", "UNKNOWN" if funding_val is not None else "UNAVAILABLE"))

    orderbook = _to_float(market_ctx.get("orderbook_imbalance"))
    orderbook_status = str(market_ctx.get("orderbook_status", "MEASURED" if orderbook is not None else "UNAVAILABLE"))
    orderbook_source = str(market_ctx.get("orderbook_source", "UNKNOWN" if orderbook is not None else "UNAVAILABLE"))

    liquidity_score = float(market_ctx.get("liquidity_score", 1.0) or 1.0)
    volatility_regime = str(market_ctx.get("volatility_regime", _volatility_regime(klines)))

    required = [spread_status, slippage_status, md_latency_status, funding_status]
    if all(s == "MEASURED" for s in required):
        evidence_status = "COMPLETE_MEASURED"
    elif any(s in {"MEASURED", "MODEL_ESTIMATE"} for s in required):
        evidence_status = "PARTIAL"
    else:
        evidence_status = "UNAVAILABLE"

    return {
        "expected_slippage_pct": max(expected_slippage_pct, 0.0) if slippage_status != "UNAVAILABLE" else None,
        "expected_slippage_legacy_pct": max(expected_slippage_pct, 0.0),
        "slippage_status": slippage_status,
        "slippage_source": slippage_source,
        "market_data_latency_ms": max(md_latency, 0.0) if md_latency is not None else None,
        "market_data_latency_status": md_latency_status,
        "market_data_latency_source": md_latency_source,
        "submit_ack_latency_ms": max(submit_ack, 0.0) if submit_ack is not None else None,
        "submit_ack_latency_status": submit_ack_status,
        "submit_ack_latency_source": submit_ack_source,
        "latency_ms": max(md_latency, 0.0) if md_latency is not None else None,
        "spread_pct": max(spread_pct, 0.0) if spread_status != "UNAVAILABLE" else None,
        "spread_status": spread_status,
        "spread_source": spread_source,
        "spread_unit_assumed": spread_unit_assumed,
        "slippage_unit_assumed": slippage_unit_assumed,
        "orderbook_imbalance": max(min(orderbook, 1.0), -1.0) if orderbook is not None else None,
        "orderbook_status": orderbook_status,
        "orderbook_source": orderbook_source,
        "liquidity_score": max(min(liquidity_score, 1.0), 0.0),
        "funding_rate_pct": funding_val,
        "funding_status": funding_status,
        "funding_source": funding_source,
        "volatility_regime": volatility_regime,
        "evidence_status": evidence_status,
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
def normalize_pct_input(value: Any, *, field: str) -> tuple[float, str]:
    """
    Normalize spread/slippage inputs into fractional rate units.
    Contract:
      - 0.001 means 0.1%
      - 0.1 is treated as percent-point 0.1% and normalized to 0.001
    """
    try:
        raw = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0, "UNAVAILABLE"
    v = abs(raw)
    if v > 0.05:
        return v / 100.0, "PERCENT_POINT_NORMALIZED"
    return v, "FRACTIONAL_RATE"
