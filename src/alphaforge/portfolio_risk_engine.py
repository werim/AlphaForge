from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PortfolioRiskSnapshot:
    total_open_positions: int
    portfolio_heat_pct: float
    correlated_exposure_pct: float
    directional_long_exposure_pct: float
    directional_short_exposure_pct: float
    largest_position_pct: float
    drawdown_pct: float
    volatility_state: str
    liquidity_state: str
    risk_state: str
    warnings: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass
class PositionSizingDecision:
    approved: bool
    recommended_size_pct: float
    max_allowed_size_pct: float
    risk_multiplier: float
    correlation_penalty: float
    liquidity_penalty: float
    volatility_penalty: float
    drawdown_penalty: float
    execution_penalty: float
    reject_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


DEFAULT_CONFIG: dict[str, Any] = {
    "max_portfolio_heat_pct": 12.0,
    "max_correlated_exposure_pct": 45.0,
    "max_position_pct": 4.0,
    "max_directional_imbalance_pct": 70.0,
    "drawdown_defensive_pct": 8.0,
    "drawdown_lockdown_pct": 15.0,
    "execution_lockdown_threshold": 0.75,
    "execution_elevated_threshold": 0.45,
    "volatility_expansion_threshold": 1.25,
    "volatility_unstable_threshold": 1.6,
    "liquidity_thin_threshold": 0.45,
    "liquidity_stressed_threshold": 0.3,
    "base_size_pct": 1.0,
    "max_size_pct": 2.5,
    "min_size_pct": 0.1,
}

CLUSTERS = {
    "L1": {"BTC", "ETH", "SOL", "BNB", "AVAX", "ADA", "XRP"},
    "MEME": {"DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI"},
    "AI": {"FET", "AGIX", "RNDR", "TAO"},
}


def evaluate_portfolio_risk(
    open_positions: list[dict],
    account_state: dict,
    market_state: dict,
    config: dict | None = None,
) -> PortfolioRiskSnapshot:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    equity = max(float(account_state.get("equity", 1.0) or 1.0), 1e-9)

    heat_pct = 0.0
    long_exposure = 0.0
    short_exposure = 0.0
    largest = 0.0
    exposures: list[tuple[str, float, str]] = []

    for p in open_positions:
        notional = abs(float(p.get("notional", p.get("value", 0.0)) or 0.0))
        risk_pct = float(p.get("risk_pct", (notional / equity) * 100.0) or 0.0)
        side = str(p.get("side", "LONG")).upper()
        symbol = str(p.get("symbol", "UNKNOWN")).upper()
        heat_pct += max(risk_pct, 0.0)
        largest = max(largest, max(risk_pct, 0.0))
        exposures.append((symbol, max(risk_pct, 0.0), side))
        if side == "SHORT":
            short_exposure += max(risk_pct, 0.0)
        else:
            long_exposure += max(risk_pct, 0.0)

    correlated = _correlated_exposure_pct(exposures)
    drawdown_pct = float(account_state.get("drawdown_pct", 0.0) or 0.0)
    vol_ratio = float(market_state.get("volatility_ratio", 1.0) or 1.0)
    liquidity_score = float(market_state.get("liquidity_score", 1.0) or 1.0)
    execution_risk = float(market_state.get("execution_risk", 0.0) or 0.0)
    slippage_anomaly = bool(market_state.get("slippage_anomaly", False))

    volatility_state = "UNSTABLE" if vol_ratio >= cfg["volatility_unstable_threshold"] else "EXPANDED" if vol_ratio >= cfg["volatility_expansion_threshold"] else "NORMAL"
    liquidity_state = "STRESSED" if liquidity_score <= cfg["liquidity_stressed_threshold"] else "THIN" if liquidity_score <= cfg["liquidity_thin_threshold"] else "NORMAL"

    risk_state = "NORMAL"
    warnings: list[str] = []

    if heat_pct >= cfg["max_portfolio_heat_pct"] * 1.15 or correlated >= cfg["max_correlated_exposure_pct"] * 1.2:
        risk_state = "ELEVATED"
    if drawdown_pct >= cfg["drawdown_defensive_pct"] or volatility_state != "NORMAL" or liquidity_state != "NORMAL":
        risk_state = "DEFENSIVE" if risk_state == "NORMAL" else risk_state
    if drawdown_pct >= cfg["drawdown_lockdown_pct"] or execution_risk >= cfg["execution_lockdown_threshold"] or slippage_anomaly or volatility_state == "UNSTABLE":
        risk_state = "LOCKDOWN"

    if heat_pct > cfg["max_portfolio_heat_pct"]:
        warnings.append("PORTFOLIO_HEAT_TOO_HIGH")
    if correlated > cfg["max_correlated_exposure_pct"]:
        warnings.append("CORRELATION_EXPOSURE")
    if largest > cfg["max_position_pct"]:
        warnings.append("POSITION_TOO_LARGE")
    if drawdown_pct >= cfg["drawdown_defensive_pct"]:
        warnings.append("EXCESSIVE_DRAWDOWN")
    if liquidity_state != "NORMAL":
        warnings.append("LIQUIDITY_TOO_THIN")
    if volatility_state != "NORMAL":
        warnings.append("VOLATILITY_EXPANSION")
    if execution_risk >= cfg["execution_elevated_threshold"]:
        warnings.append("EXECUTION_RISK_TOO_HIGH")

    return PortfolioRiskSnapshot(
        total_open_positions=len(open_positions),
        portfolio_heat_pct=round(heat_pct, 6),
        correlated_exposure_pct=round(correlated, 6),
        directional_long_exposure_pct=round(long_exposure, 6),
        directional_short_exposure_pct=round(short_exposure, 6),
        largest_position_pct=round(largest, 6),
        drawdown_pct=round(drawdown_pct, 6),
        volatility_state=volatility_state,
        liquidity_state=liquidity_state,
        risk_state=risk_state,
        warnings=sorted(set(warnings)),
        diagnostics={
            "volatility_ratio": vol_ratio,
            "liquidity_score": liquidity_score,
            "execution_risk": execution_risk,
            "slippage_anomaly": slippage_anomaly,
            "config": cfg,
        },
    )


def calculate_position_size(
    candidate_trade: dict,
    portfolio_snapshot: PortfolioRiskSnapshot,
    market_state: dict,
    config: dict | None = None,
) -> PositionSizingDecision:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    confidence = _clamp(float(candidate_trade.get("setup_confidence", candidate_trade.get("score", 0.0)) or 0.0) / 10.0, 0.0, 1.0)
    liquidity_score = _clamp(float(market_state.get("liquidity_score", 1.0) or 1.0), 0.0, 1.0)
    vol_ratio = max(float(market_state.get("volatility_ratio", 1.0) or 1.0), 0.01)
    spread_pct = max(float(market_state.get("spread_pct", 0.0) or 0.0), 0.0)
    slippage_pct = max(float(market_state.get("expected_slippage_pct", 0.0) or 0.0), 0.0)
    execution_risk = _clamp(float(market_state.get("execution_risk", 0.0) or 0.0), 0.0, 1.0)
    effective_rr = max(float(candidate_trade.get("effective_rr", candidate_trade.get("rr", 1.0)) or 1.0), 0.0)

    correlation_penalty = _clamp(portfolio_snapshot.correlated_exposure_pct / max(cfg["max_correlated_exposure_pct"], 1e-9), 0.0, 1.5)
    liquidity_penalty = _clamp(1.0 - liquidity_score, 0.0, 1.0)
    volatility_penalty = _clamp((vol_ratio - 1.0) / 1.2, 0.0, 1.2)
    drawdown_penalty = _clamp(portfolio_snapshot.drawdown_pct / max(cfg["drawdown_lockdown_pct"], 1e-9), 0.0, 1.2)
    execution_penalty = _clamp(execution_risk + (spread_pct * 4.0) + (slippage_pct * 5.0), 0.0, 1.2)

    state_multiplier = {"NORMAL": 1.0, "ELEVATED": 0.8, "DEFENSIVE": 0.55, "LOCKDOWN": 0.15}.get(portfolio_snapshot.risk_state, 0.5)
    edge_multiplier = _clamp((confidence * 0.7) + (_clamp(effective_rr / 3.0, 0.0, 1.0) * 0.3), 0.1, 1.2)

    aggregate_penalty = (correlation_penalty * 0.25) + (liquidity_penalty * 0.25) + (volatility_penalty * 0.2) + (drawdown_penalty * 0.15) + (execution_penalty * 0.15)
    risk_multiplier = _clamp((1.0 - aggregate_penalty) * state_multiplier * edge_multiplier, 0.0, 1.5)

    base_size = float(candidate_trade.get("base_size_pct", cfg["base_size_pct"]))
    max_allowed = min(cfg["max_size_pct"], max(cfg["min_size_pct"], cfg["max_size_pct"] * state_multiplier))
    recommended = _clamp(base_size * risk_multiplier, 0.0, max_allowed)

    reject_reasons: list[str] = []
    warnings: list[str] = []

    if portfolio_snapshot.portfolio_heat_pct >= cfg["max_portfolio_heat_pct"]:
        reject_reasons.append("PORTFOLIO_HEAT_TOO_HIGH")
    if portfolio_snapshot.correlated_exposure_pct >= cfg["max_correlated_exposure_pct"] * 1.15:
        reject_reasons.append("CORRELATION_EXPOSURE")
    if portfolio_snapshot.drawdown_pct >= cfg["drawdown_lockdown_pct"]:
        reject_reasons.append("EXCESSIVE_DRAWDOWN")
    if liquidity_score <= cfg["liquidity_stressed_threshold"]:
        reject_reasons.append("LIQUIDITY_TOO_THIN")
    if execution_risk >= cfg["execution_lockdown_threshold"]:
        reject_reasons.append("EXECUTION_RISK_TOO_HIGH")
    if vol_ratio >= cfg["volatility_unstable_threshold"]:
        reject_reasons.append("VOLATILITY_EXPANSION")
    if candidate_trade.get("requested_size_pct", recommended) and float(candidate_trade.get("requested_size_pct", recommended)) > max_allowed:
        reject_reasons.append("POSITION_TOO_LARGE")
    if portfolio_snapshot.risk_state == "LOCKDOWN":
        reject_reasons.append("REGIME_UNSTABLE")

    if recommended < cfg["min_size_pct"] and not reject_reasons:
        warnings.append("SIZE_AT_MINIMUM")

    approved = not reject_reasons
    if not approved:
        recommended = 0.0

    return PositionSizingDecision(
        approved=approved,
        recommended_size_pct=round(recommended, 6),
        max_allowed_size_pct=round(max_allowed, 6),
        risk_multiplier=round(risk_multiplier, 6),
        correlation_penalty=round(correlation_penalty, 6),
        liquidity_penalty=round(liquidity_penalty, 6),
        volatility_penalty=round(volatility_penalty, 6),
        drawdown_penalty=round(drawdown_penalty, 6),
        execution_penalty=round(execution_penalty, 6),
        reject_reasons=sorted(set(reject_reasons)),
        warnings=warnings,
        diagnostics={
            "confidence": confidence,
            "effective_rr": effective_rr,
            "spread_pct": spread_pct,
            "slippage_pct": slippage_pct,
            "aggregate_penalty": aggregate_penalty,
            "state_multiplier": state_multiplier,
            "edge_multiplier": edge_multiplier,
            "base_size_pct": base_size,
        },
    )


def _correlated_exposure_pct(exposures: list[tuple[str, float, str]]) -> float:
    by_cluster: dict[str, float] = {}
    for symbol, risk_pct, side in exposures:
        base = symbol.replace("USDT", "").replace("USD", "")
        cluster = _cluster_for_symbol(base)
        key = f"{cluster}:{side}"
        by_cluster[key] = by_cluster.get(key, 0.0) + risk_pct
    return max(by_cluster.values(), default=0.0)


def _cluster_for_symbol(symbol: str) -> str:
    for k, names in CLUSTERS.items():
        if symbol in names:
            return k
    return symbol


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))
