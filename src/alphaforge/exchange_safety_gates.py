from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from alphaforge.exchange_connectivity import ExchangeHealth


@dataclass(frozen=True)
class ExchangeSafetyDecision:
    allowed: bool
    reject_reasons: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reject_reasons": list(self.reject_reasons),
            "warnings": list(self.warnings),
            "diagnostics": self.diagnostics,
        }


def evaluate_exchange_safety(
    health: Sequence[ExchangeHealth],
    market_ctx: Mapping[str, Any],
    *,
    max_latency_ms: float = 750.0,
    max_spread_pct: float = 0.0025,
    max_abs_funding_rate_pct: float = 0.0010,
    require_orderbook: bool = True,
    require_public_market_data: bool = True,
) -> ExchangeSafetyDecision:
    """Fail-closed exchange safety gate.

    This function does not submit orders. It only evaluates whether current
    exchange/market conditions are safe enough for the execution layer to be
    allowed to continue.
    """
    reject_reasons: list[str] = []
    warnings: list[str] = []
    diagnostics: dict[str, Any] = {
        "health": [h.to_dict() for h in health],
        "market_ctx": dict(market_ctx or {}),
    }

    if not health:
        reject_reasons.append("EXCHANGE_HEALTH_MISSING")

    for item in health:
        if not item.connected:
            reject_reasons.append(f"EXCHANGE_UNAVAILABLE:{item.exchange}")
        if require_public_market_data and not item.public_market_data_ok:
            reject_reasons.append(f"PUBLIC_MARKET_DATA_UNAVAILABLE:{item.exchange}")
        if require_orderbook and not item.orderbook_ok:
            reject_reasons.append(f"ORDERBOOK_UNAVAILABLE:{item.exchange}")
        if item.funding_ok is False:
            reject_reasons.append(f"FUNDING_UNAVAILABLE:{item.exchange}")
        if item.latency_ms is None:
            warnings.append(f"LATENCY_UNKNOWN:{item.exchange}")
        elif item.latency_ms > max_latency_ms:
            reject_reasons.append(f"LATENCY_SPIKE:{item.exchange}")

    spread_pct = _safe_float(market_ctx.get("spread_pct"), None)
    if spread_pct is None:
        reject_reasons.append("SPREAD_UNKNOWN")
    elif spread_pct > max_spread_pct:
        reject_reasons.append("SPREAD_EXPANSION")

    funding_rate_pct = _safe_float(market_ctx.get("funding_rate_pct"), None)
    if funding_rate_pct is None:
        warnings.append("FUNDING_UNKNOWN")
    elif abs(funding_rate_pct) > max_abs_funding_rate_pct:
        reject_reasons.append("FUNDING_ANOMALY")

    orderbook_stale = bool(market_ctx.get("orderbook_stale", False))
    if orderbook_stale:
        reject_reasons.append("ORDERBOOK_STALE")

    websocket_stale = bool(market_ctx.get("websocket_stale", False))
    if websocket_stale:
        reject_reasons.append("WEBSOCKET_STALE")

    api_error_cluster = bool(market_ctx.get("api_error_cluster", False))
    if api_error_cluster:
        reject_reasons.append("API_ERROR_CLUSTER")

    reasons = tuple(sorted(set(reject_reasons)))
    return ExchangeSafetyDecision(
        allowed=not reasons,
        reject_reasons=reasons,
        warnings=tuple(sorted(set(warnings))),
        diagnostics=diagnostics,
    )


def _safe_float(value: Any, default: float | None) -> float | None:
    try:
        if value in (None, "", "UNKNOWN", "UNAVAILABLE", "UNAVAILABLE_BACKTEST"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
