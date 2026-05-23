from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


_UNAVAILABLE = "UNAVAILABLE"
_MEASURED = "MEASURED"
_MODEL_ESTIMATE = "MODEL_ESTIMATE"


def build_execution_context(market_ctx: Mapping[str, Any], funding_rate_pct: float | None = None) -> dict[str, Any]:
    """Build audit-grade execution evidence without representing absence as zero.

    This function is evidence construction only.  It deliberately preserves the
    existing conservative slippage fallback as a MODEL_ESTIMATE so current
    decision thresholds are not changed by this observability increment.
    """
    klines = list(market_ctx.get("recent_klines", []) or [])

    raw_spread = market_ctx.get("spread_pct")
    if raw_spread is None:
        derived_spread = _spread_pct_from_prices(market_ctx)
        spread_value = derived_spread if derived_spread > 0.0 else None
        spread_status = _MEASURED if spread_value is not None else _UNAVAILABLE
        spread_source = "BEST_BID_ASK_DERIVED" if spread_value is not None else str(market_ctx.get("spread_source", _UNAVAILABLE) or _UNAVAILABLE)
        spread_unit_assumed = "FRACTIONAL_RATE" if spread_value is not None else _UNAVAILABLE
    else:
        spread_value, spread_unit_assumed = normalize_pct_input(raw_spread, field="spread_pct")
        spread_status = str(market_ctx.get("spread_status", _MEASURED) or _MEASURED)
        spread_source = str(market_ctx.get("spread_source", "UPSTREAM_MARKET_CONTEXT") or "UPSTREAM_MARKET_CONTEXT")

    raw_slippage = market_ctx.get("expected_slippage_pct")
    if raw_slippage is None:
        slippage_value, slippage_unit_assumed = normalize_pct_input(_expected_slippage_pct(klines, market_ctx), field="expected_slippage_pct")
        slippage_status = _MODEL_ESTIMATE
        slippage_source = "CONSERVATIVE_VOLATILITY_FALLBACK" if not klines else "RECENT_KLINE_RANGE_MODEL"
    else:
        slippage_value, slippage_unit_assumed = normalize_pct_input(raw_slippage, field="expected_slippage_pct")
        slippage_status = str(market_ctx.get("slippage_status", _MODEL_ESTIMATE) or _MODEL_ESTIMATE)
        slippage_source = str(market_ctx.get("slippage_source", "UPSTREAM_MODEL_ESTIMATE") or "UPSTREAM_MODEL_ESTIMATE")

    raw_market_latency = market_ctx.get("market_data_latency_ms")
    if raw_market_latency is not None:
        market_data_latency_ms: float | None = max(float(raw_market_latency), 0.0)
        market_data_latency_status = str(market_ctx.get("market_data_latency_status", _MEASURED) or _MEASURED)
        market_data_latency_source = str(market_ctx.get("market_data_latency_source", "UPSTREAM_HTTP_RTT") or "UPSTREAM_HTTP_RTT")
    else:
        market_data_latency_ms = None
        market_data_latency_status = _UNAVAILABLE
        market_data_latency_source = _UNAVAILABLE

    raw_submit_latency = market_ctx.get("submit_ack_latency_ms")
    submit_ack_latency_ms = max(float(raw_submit_latency), 0.0) if raw_submit_latency is not None else None
    submit_ack_latency_status = str(market_ctx.get("submit_ack_latency_status", _UNAVAILABLE) or _UNAVAILABLE)
    submit_ack_latency_source = str(market_ctx.get("submit_ack_latency_source", _UNAVAILABLE) or _UNAVAILABLE)

    orderbook_raw = market_ctx.get("orderbook_imbalance")
    orderbook_imbalance = max(min(float(orderbook_raw), 1.0), -1.0) if orderbook_raw is not None else None
    orderbook_status = str(market_ctx.get("orderbook_status", _MEASURED if orderbook_raw is not None else _UNAVAILABLE) or _UNAVAILABLE)
    orderbook_source = str(market_ctx.get("orderbook_source", "UPSTREAM_ORDERBOOK" if orderbook_raw is not None else _UNAVAILABLE) or _UNAVAILABLE)

    liquidity_score = float(market_ctx.get("liquidity_score", 1.0) or 1.0)
    funding = funding_rate_pct if funding_rate_pct is not None else market_ctx.get("funding_rate_pct")
    funding_rate_pct_val = float(funding) if funding is not None else None
    funding_status = str(market_ctx.get("funding_status", _MEASURED if funding is not None else _UNAVAILABLE) or _UNAVAILABLE)
    funding_source = str(market_ctx.get("funding_source", "UPSTREAM_FUNDING" if funding is not None else _UNAVAILABLE) or _UNAVAILABLE)
    volatility_regime = str(market_ctx.get("volatility_regime", _volatility_regime(klines)))

    required_statuses = (spread_status, slippage_status, market_data_latency_status, funding_status)
    if all(status == _MEASURED for status in required_statuses):
        evidence_status = "COMPLETE_MEASURED"
    elif any(status != _UNAVAILABLE for status in required_statuses):
        evidence_status = "PARTIAL"
    else:
        evidence_status = _UNAVAILABLE

    return {
        "expected_slippage_pct": max(slippage_value, 0.0),
        "slippage_status": slippage_status,
        "slippage_source": slippage_source,
        "slippage_unit_assumed": slippage_unit_assumed,
        "spread_pct": max(spread_value, 0.0) if spread_value is not None else None,
        "spread_status": spread_status,
        "spread_source": spread_source,
        "spread_unit_assumed": spread_unit_assumed,
        "market_data_latency_ms": market_data_latency_ms,
        "market_data_latency_status": market_data_latency_status,
        "market_data_latency_source": market_data_latency_source,
        "submit_ack_latency_ms": submit_ack_latency_ms,
        "submit_ack_latency_status": submit_ack_latency_status,
        "submit_ack_latency_source": submit_ack_latency_source,
        "latency_ms": market_data_latency_ms,
        "latency_status": market_data_latency_status,
        "latency_source": market_data_latency_source,
        "orderbook_imbalance": orderbook_imbalance,
        "orderbook_status": orderbook_status,
        "orderbook_source": orderbook_source,
        "liquidity_score": max(min(liquidity_score, 1.0), 0.0),
        "funding_rate_pct": funding_rate_pct_val,
        "funding_status": funding_status,
        "funding_source": funding_source,
        "volatility_regime": volatility_regime,
        "spoof_risk": float(market_ctx.get("spoof_risk", 0.0) or 0.0),
        "absorption_score": float(market_ctx.get("absorption_score", 0.0) or 0.0),
        "evidence_status": evidence_status,
    }


def neutral_execution_context() -> dict[str, Any]:
    return {
        "expected_slippage_pct": None,
        "slippage_status": _UNAVAILABLE,
        "slippage_source": _UNAVAILABLE,
        "latency_ms": None,
        "latency_status": _UNAVAILABLE,
        "latency_source": _UNAVAILABLE,
        "market_data_latency_ms": None,
        "market_data_latency_status": _UNAVAILABLE,
        "market_data_latency_source": _UNAVAILABLE,
        "spread_pct": None,
        "spread_status": _UNAVAILABLE,
        "spread_source": _UNAVAILABLE,
        "orderbook_imbalance": None,
        "orderbook_status": _UNAVAILABLE,
        "orderbook_source": _UNAVAILABLE,
        "liquidity_score": 1.0,
        "funding_rate_pct": None,
        "funding_status": _UNAVAILABLE,
        "funding_source": _UNAVAILABLE,
        "volatility_regime": "normal",
        "spoof_risk": 0.0,
        "absorption_score": 0.0,
        "evidence_status": _UNAVAILABLE,
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
    missing: list[str] = []

    def req_float(key: str) -> float | None:
        value = execution_ctx.get(key)
        if value in (None, "", "UNKNOWN", "UNAVAILABLE", "UNAVAILABLE_BACKTEST"):
            missing.append(key)
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            missing.append(key)
            return None

    spread = req_float("spread_pct")
    slippage = req_float("expected_slippage_pct")
    latency = req_float("latency_ms")
    funding = req_float("funding_rate_pct")
    liquidity = req_float("liquidity_score")

    spread_penalty = max((spread or 0.0) * 25.0, 0.0)
    slippage_penalty = max((slippage or 0.0) * 30.0, 0.0)
    latency_penalty = max(((latency or 0.0) / 1000.0) * 0.2, 0.0)
    funding_penalty = max(abs(funding or 0.0) * 2.5, 0.0)
    liquidity_penalty = max((1.0 - max(min(liquidity if liquidity is not None else 1.0, 1.0), 0.0)) * 0.6, 0.0)

    completeness = "complete" if not missing else ("partial" if len(missing) < 5 else "unavailable")
    total = spread_penalty + slippage_penalty + latency_penalty + funding_penalty + liquidity_penalty
    if include_missing_penalty and missing:
        total += min(0.5, 0.1 * len(missing))
    return ExecutionCostModel(spread_penalty, slippage_penalty, latency_penalty, funding_penalty, liquidity_penalty, round(total, 6), tuple(missing), completeness)


def normalize_pct_input(value: Any, *, field: str) -> tuple[float, str]:
    """Normalize percentage inputs into fractional rate units."""
    try:
        raw = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0, _UNAVAILABLE
    v = abs(raw)
    if v > 0.05:
        return v / 100.0, "PERCENT_POINT_NORMALIZED"
    return v, "FRACTIONAL_RATE"
