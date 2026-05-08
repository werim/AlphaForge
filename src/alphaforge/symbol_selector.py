from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass
class SymbolSelectionResult:
    symbol: str
    tradable: bool
    symbol_score: float
    regime_hint: str
    liquidity_score: float
    volatility_score: float
    trend_score: float
    spread_score: float
    volume_score: float
    reject_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


DEFAULT_CONFIG: dict[str, Any] = {
    "min_volume_24h_usdt": 2_000_000.0,
    "max_spread_pct": 0.12,
    "min_liquidity_score": 0.45,
    "max_volatility_pct": 8.0,
    "max_chop_score": 0.72,
    "panic_score_reject": 0.85,
    "min_trend_strength": 0.25,
    "range_edge_bonus_chop_limit": 0.55,
    "include_rejected": False,
}


def _safe_float(data: Mapping[str, Any], key: str, default: float, diagnostics: dict[str, Any], warnings: list[str]) -> float:
    raw = data.get(key)
    if raw is None:
        diagnostics.setdefault("defaults_used", {})[key] = default
        warnings.append(f"missing_{key}")
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        diagnostics.setdefault("defaults_used", {})[key] = default
        diagnostics.setdefault("invalid_fields", {})[key] = raw
        warnings.append(f"invalid_{key}")
        return default


def select_symbol(symbol: str, market_data: dict, config: dict | None = None) -> SymbolSelectionResult:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    diagnostics: dict[str, Any] = {"inputs": dict(market_data or {})}
    warnings: list[str] = []
    reject_reasons: list[str] = []

    volume_24h_usdt = _safe_float(market_data, "volume_24h_usdt", cfg["min_volume_24h_usdt"] * 0.5, diagnostics, warnings)
    spread_pct = _safe_float(market_data, "spread_pct", cfg["max_spread_pct"] * 1.1, diagnostics, warnings)
    volatility_pct = _safe_float(market_data, "volatility_pct", cfg["max_volatility_pct"] * 0.7, diagnostics, warnings)
    trend_strength = _safe_float(market_data, "trend_strength", 0.2, diagnostics, warnings)
    liquidity_score_raw = _safe_float(market_data, "liquidity_score", cfg["min_liquidity_score"] * 0.9, diagnostics, warnings)
    recent_volume_change_pct = _safe_float(market_data, "recent_volume_change_pct", 0.0, diagnostics, warnings)
    chop_score = _safe_float(market_data, "chop_score", 0.65, diagnostics, warnings)
    panic_score = _safe_float(market_data, "panic_score", 0.0, diagnostics, warnings) if "panic_score" in market_data else 0.0

    if volume_24h_usdt < cfg["min_volume_24h_usdt"]:
        reject_reasons.append("LOW_VOLUME")
    if spread_pct > cfg["max_spread_pct"]:
        reject_reasons.append("WIDE_SPREAD")
    if liquidity_score_raw < cfg["min_liquidity_score"]:
        reject_reasons.append("LOW_LIQUIDITY")
    if volatility_pct > cfg["max_volatility_pct"]:
        reject_reasons.append("EXCESSIVE_VOLATILITY")
    if chop_score > cfg["max_chop_score"]:
        reject_reasons.append("TOO_CHOPPY")
    if panic_score >= cfg["panic_score_reject"]:
        reject_reasons.append("PANIC_CONDITIONS")

    has_clean_trend = trend_strength >= cfg["min_trend_strength"] and chop_score <= cfg["max_chop_score"]
    has_range_edge = chop_score <= cfg["range_edge_bonus_chop_limit"] and abs(recent_volume_change_pct) <= 20.0
    if not has_clean_trend and not has_range_edge:
        reject_reasons.append("WEAK_TREND_AND_NO_RANGE_EDGE")

    volume_score = max(0.0, min(10.0, (volume_24h_usdt / cfg["min_volume_24h_usdt"]) * 5.0))
    spread_score = max(0.0, min(10.0, (cfg["max_spread_pct"] / max(spread_pct, 1e-9)) * 5.0))
    liquidity_score = max(0.0, min(10.0, liquidity_score_raw * 10.0))
    volatility_score = max(0.0, min(10.0, 10.0 - max(0.0, volatility_pct - 1.0) * 1.2))
    trend_score = max(0.0, min(10.0, trend_strength * 10.0))

    if has_range_edge:
        trend_score = min(10.0, trend_score + 1.0)

    symbol_score = (
        volume_score * 0.2
        + spread_score * 0.2
        + liquidity_score * 0.25
        + volatility_score * 0.15
        + trend_score * 0.2
    )
    if "TOO_CHOPPY" in reject_reasons:
        symbol_score -= 1.0
    if "PANIC_CONDITIONS" in reject_reasons:
        symbol_score -= 1.5

    symbol_score = round(max(0.0, min(10.0, symbol_score)), 2)
    regime_hint = "TREND" if has_clean_trend else ("RANGE" if has_range_edge else "UNFAVORABLE")

    diagnostics.update(
        {
            "metrics": {
                "volume_24h_usdt": volume_24h_usdt,
                "spread_pct": spread_pct,
                "volatility_pct": volatility_pct,
                "trend_strength": trend_strength,
                "liquidity_score": liquidity_score_raw,
                "recent_volume_change_pct": recent_volume_change_pct,
                "chop_score": chop_score,
                "panic_score": panic_score,
            },
            "sub_scores": {
                "volume_score": round(volume_score, 2),
                "spread_score": round(spread_score, 2),
                "liquidity_score": round(liquidity_score, 2),
                "volatility_score": round(volatility_score, 2),
                "trend_score": round(trend_score, 2),
            },
        }
    )

    return SymbolSelectionResult(
        symbol=symbol,
        tradable=len(reject_reasons) == 0,
        symbol_score=symbol_score,
        regime_hint=regime_hint,
        liquidity_score=round(liquidity_score, 2),
        volatility_score=round(volatility_score, 2),
        trend_score=round(trend_score, 2),
        spread_score=round(spread_score, 2),
        volume_score=round(volume_score, 2),
        reject_reasons=reject_reasons,
        warnings=warnings,
        diagnostics=diagnostics,
    )


def select_symbols(candidates: list[dict], config: dict | None = None) -> list[SymbolSelectionResult]:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    results = [
        select_symbol(str(item.get("symbol", "UNKNOWN")), item, cfg)
        for item in (candidates or [])
    ]
    if not cfg.get("include_rejected", False):
        results = [r for r in results if r.tradable]
    return sorted(results, key=lambda x: x.symbol_score, reverse=True)
