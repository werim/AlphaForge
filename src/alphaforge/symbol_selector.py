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
    "max_spread_pct": 0.0025,
    "min_liquidity_score": 0.45,
    "max_volatility_pct": 8.0,
    "max_chop_score": 0.72,
    "panic_score_reject": 0.85,
    "min_trend_strength": 0.25,
    "range_edge_bonus_chop_limit": 0.55,
    "max_spoof_risk": 0.70,
    "max_fakeout_risk": 0.65,
    "max_abs_funding_rate_pct": 0.0010,
    "max_correlation_exposure": 0.80,
    "min_abs_orderbook_imbalance": 0.0,
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

    disabled = {str(r).upper() for r in cfg.get("disabled_backtest_filters", [])}
    bypassed_reject_reasons: list[str] = []

    def _append_reject(reason: str) -> None:
        normalized = str(reason).upper()
        if normalized in disabled:
            bypassed_reject_reasons.append(normalized)
        else:
            reject_reasons.append(normalized)

    volume_24h_usdt = _safe_float(market_data, "volume_24h_usdt", cfg["min_volume_24h_usdt"] * 0.5, diagnostics, warnings)
    spread_pct = _safe_float(market_data, "spread_pct", cfg["max_spread_pct"] * 1.1, diagnostics, warnings)
    if spread_pct > max(cfg["max_spread_pct"] * 2.0, 0.005):
        spread_pct = spread_pct / 100.0
        diagnostics["spread_normalized_from_percent_points"] = True
    volatility_pct = _safe_float(market_data, "volatility_pct", cfg["max_volatility_pct"] * 0.7, diagnostics, warnings)
    trend_strength = _safe_float(market_data, "trend_strength", 0.2, diagnostics, warnings)
    liquidity_score_raw = _safe_float(market_data, "liquidity_score", cfg["min_liquidity_score"] * 0.9, diagnostics, warnings)
    if liquidity_score_raw > 1.0 and cfg["min_liquidity_score"] <= 1.0:
        liquidity_score_raw = liquidity_score_raw / 10.0
        diagnostics["liquidity_score_normalized_from_0_10"] = True
    recent_volume_change_pct = _safe_float(market_data, "recent_volume_change_pct", 0.0, diagnostics, warnings)
    chop_score = _safe_float(market_data, "chop_score", 0.65, diagnostics, warnings)
    panic_score = _safe_float(market_data, "panic_score", 0.0, diagnostics, warnings) if "panic_score" in market_data else 0.0
    spoof_risk = _safe_float(market_data, "spoof_risk", 0.0, diagnostics, warnings) if "spoof_risk" in market_data else 0.0
    fakeout_risk = _safe_float(market_data, "fakeout_risk", 0.25, diagnostics, warnings) if "fakeout_risk" in market_data else 0.25
    funding_rate_pct = _safe_float(market_data, "funding_rate_pct", 0.0, diagnostics, warnings) if "funding_rate_pct" in market_data else 0.0
    correlation_exposure = _safe_float(market_data, "correlation_exposure", 0.0, diagnostics, warnings) if "correlation_exposure" in market_data else 0.0
    orderbook_imbalance = _safe_float(market_data, "orderbook_imbalance", 0.0, diagnostics, warnings) if "orderbook_imbalance" in market_data else 0.0

    if volume_24h_usdt < cfg["min_volume_24h_usdt"]:
        _append_reject("LOW_VOLUME")
    if spread_pct > cfg["max_spread_pct"]:
        _append_reject("WIDE_SPREAD")
    if liquidity_score_raw < cfg["min_liquidity_score"]:
        _append_reject("LOW_LIQUIDITY")
    if volatility_pct > cfg["max_volatility_pct"]:
        _append_reject("EXCESSIVE_VOLATILITY")
    if chop_score > cfg["max_chop_score"]:
        _append_reject("TOO_CHOPPY")
    if panic_score >= cfg["panic_score_reject"]:
        _append_reject("PANIC_CONDITIONS")
    if spoof_risk > cfg["max_spoof_risk"]:
        _append_reject("SPOOF_RISK")
    if fakeout_risk > cfg["max_fakeout_risk"]:
        _append_reject("FAKEOUT_RISK")
    if abs(funding_rate_pct) > cfg["max_abs_funding_rate_pct"]:
        _append_reject("FUNDING_ANOMALY")
    if correlation_exposure > cfg["max_correlation_exposure"]:
        _append_reject("CORRELATION_OVEREXPOSURE")
    if abs(orderbook_imbalance) < cfg["min_abs_orderbook_imbalance"]:
        _append_reject("LOW_ORDERBOOK_ALIGNMENT")

    has_clean_trend = trend_strength >= cfg["min_trend_strength"] and chop_score <= cfg["max_chop_score"]
    has_range_edge = chop_score <= cfg["range_edge_bonus_chop_limit"] and abs(recent_volume_change_pct) <= 20.0
    if not has_clean_trend and not has_range_edge:
        _append_reject("WEAK_TREND_AND_NO_RANGE_EDGE")

    volume_score = max(0.0, min(10.0, (volume_24h_usdt / cfg["min_volume_24h_usdt"]) * 5.0))
    spread_ratio = spread_pct / max(cfg["max_spread_pct"], 1e-9)
    spread_score = max(0.0, min(10.0, 10.0 * (1.0 - spread_ratio)))
    liquidity_score = max(0.0, min(10.0, liquidity_score_raw * 10.0))
    volatility_score = max(0.0, min(10.0, 10.0 - max(0.0, volatility_pct - 1.0) * 1.2))
    trend_score = max(0.0, min(10.0, trend_strength * 10.0))

    if has_range_edge:
        trend_score = min(10.0, trend_score + 1.0)

    microstructure_penalty = 0.0
    if "SPOOF_RISK" in reject_reasons:
        microstructure_penalty += 1.5
    if "FAKEOUT_RISK" in reject_reasons:
        microstructure_penalty += 1.2
    if "FUNDING_ANOMALY" in reject_reasons:
        microstructure_penalty += 0.8
    if "CORRELATION_OVEREXPOSURE" in reject_reasons:
        microstructure_penalty += 0.8

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
    symbol_score -= microstructure_penalty

    symbol_score = round(max(0.0, min(10.0, symbol_score)), 2)
    if panic_score >= cfg["panic_score_reject"]:
        regime_hint = "PANIC"
    elif has_clean_trend:
        regime_hint = "TREND"
    elif has_range_edge:
        regime_hint = "RANGE"
    else:
        regime_hint = "UNFAVORABLE"

    diagnostics.update(
        {
            "disabled_filters": sorted(disabled),
            "bypassed_reject_reasons": bypassed_reject_reasons,
            "disabled_filter_bypass_count": len(bypassed_reject_reasons),
            "filter_switch_experiment_active": bool(disabled),
            "metrics": {
                "volume_24h_usdt": volume_24h_usdt,
                "spread_pct": spread_pct,
                "volatility_pct": volatility_pct,
                "trend_strength": trend_strength,
                "liquidity_score": liquidity_score_raw,
                "recent_volume_change_pct": recent_volume_change_pct,
                "chop_score": chop_score,
                "panic_score": panic_score,
                "spoof_risk": spoof_risk,
                "fakeout_risk": fakeout_risk,
                "funding_rate_pct": funding_rate_pct,
                "correlation_exposure": correlation_exposure,
                "orderbook_imbalance": orderbook_imbalance,
            },
            "sub_scores": {
                "volume_score": round(volume_score, 2),
                "spread_score": round(spread_score, 2),
                "liquidity_score": round(liquidity_score, 2),
                "volatility_score": round(volatility_score, 2),
                "trend_score": round(trend_score, 2),
                "microstructure_penalty": round(microstructure_penalty, 2),
            },
        }
    )

    return SymbolSelectionResult(
        symbol=symbol,
        tradable=len(reject_reasons) == 0,
        symbol_score=symbol_score,
        regime_hint=regime_hint,
        liquidity_score=round(max(0.0, min(1.0, liquidity_score_raw)), 6),
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
