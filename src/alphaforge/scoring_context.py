"""Pure normalization for AIBrain pre-submit scoring inputs."""
from __future__ import annotations

import math
from typing import Any, Mapping

from alphaforge.execution import build_execution_context


def empty_stats_context() -> dict[str, Any]:
    return {"setup": {}, "regime": {}, "symbol": {}, "sample_size": 0}


def finite_numeric(*candidates: tuple[str, Any]) -> tuple[float | None, str | None]:
    for source, value in candidates:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric = float(value)
            if math.isfinite(numeric):
                return numeric, source
    return None, None


def build_signal_payload(symbol: str, market_ctx: Mapping[str, Any], *, signal_id: str, default_mode: str) -> dict[str, Any]:
    execution_ctx = build_execution_context(market_ctx)
    raw_rr = market_ctx.get("rr")
    rr = float(raw_rr) if raw_rr is not None else None
    signal = {
        "symbol": symbol, "signal_id": signal_id,
        "mode": str(market_ctx.get("mode", default_mode)).upper(),
        "side": market_ctx.get("side", "LONG"), "timeframe": market_ctx.get("timeframe", "1m"),
        "entry_price": float(market_ctx.get("entry", 0.0) or 0.0),
        "stop_loss": market_ctx.get("sl", market_ctx.get("stop")),
        "take_profit": market_ctx.get("tp", market_ctx.get("target")),
        "setup": market_ctx.get("setup", market_ctx.get("setup_type")),
        "regime": market_ctx.get("regime"), "risk_reward": rr,
        "max_spread_bps": 12.0, "max_funding_rate": 0.0008,
        "max_expected_slippage_pct": execution_ctx.get("expected_slippage_pct", 0.002) * 1.2,
        "execution_ctx": execution_ctx,
    }
    mtf = market_ctx.get("mtf") if isinstance(market_ctx.get("mtf"), Mapping) else {}
    setup = mtf.get("setup") if isinstance(mtf.get("setup"), Mapping) else {}
    setup_quality, _ = finite_numeric(
        ("market.setup_quality", market_ctx.get("setup_quality")),
        ("mtf.setup.setup_quality", setup.get("setup_quality")),
    )
    if setup_quality is not None:
        signal["setup_quality"] = setup_quality
    return signal


def normalize_scoring_context(signal_payload: Mapping[str, Any], market_ctx: Mapping[str, Any], *, stats_ctx: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return the exact feature shape consumed by ``AIBrain.score_signal``."""
    scored_market = dict(market_ctx)
    mtf = market_ctx.get("mtf") if isinstance(market_ctx.get("mtf"), Mapping) else {}
    setup = mtf.get("setup") if isinstance(mtf.get("setup"), Mapping) else {}
    execution = mtf.get("execution") if isinstance(mtf.get("execution"), Mapping) else {}
    regime = mtf.get("regime") if isinstance(mtf.get("regime"), Mapping) else {}
    alignment = mtf.get("alignment") if isinstance(mtf.get("alignment"), Mapping) else {}
    sources: dict[str, str] = {}
    missing: list[str] = []
    setup_quality, source = finite_numeric(
        ("signal.setup_quality", signal_payload.get("setup_quality")),
        ("market.setup_quality", market_ctx.get("setup_quality")),
        ("mtf.setup.setup_quality", setup.get("setup_quality")),
    )
    if setup_quality is None:
        missing.append("setup_quality")
    else:
        sources["setup_quality"] = str(source)
    for feature, candidates in {
        "momentum_confirmation": (("market.momentum_confirmation", market_ctx.get("momentum_confirmation")), ("mtf.execution.momentum_confirmation", execution.get("momentum_confirmation"))),
        "liquidity_quality": (("market.liquidity_quality", market_ctx.get("liquidity_quality")), ("mtf.execution.liquidity_quality", execution.get("liquidity_quality")), ("market.liquidity_score", market_ctx.get("liquidity_score")), ("mtf.execution.liquidity_score", execution.get("liquidity_score"))),
        "volatility_fit": (("market.volatility_fit", market_ctx.get("volatility_fit")), ("mtf.execution.volatility_fit", execution.get("volatility_fit"))),
    }.items():
        value, source = finite_numeric(*candidates)
        if value is None:
            scored_market.pop(feature, None); missing.append(feature)
        else:
            scored_market[feature] = value; sources[feature] = str(source)
    regime_alignment, source = finite_numeric(
        ("market.regime_alignment", market_ctx.get("regime_alignment")),
        ("mtf.alignment.alignment", alignment.get("alignment")),
        ("mtf.regime.regime_alignment", regime.get("regime_alignment")),
        ("mtf.regime.alignment", regime.get("alignment")),
    )
    regime_ctx: dict[str, Any] = {"regime": regime.get("regime", signal_payload.get("regime"))}
    if regime_alignment is None:
        missing.append("regime_alignment")
    else:
        regime_ctx["alignment"] = regime_alignment; sources["regime_alignment"] = str(source)
    stats = dict(stats_ctx or empty_stats_context())
    scored_market["scoring_context_diagnostics"] = {
        "status": "SCORING_CONTEXT_INCOMPLETE" if missing else "COMPLETE",
        "missing_inputs": missing, "sources": sources,
        "sample_size": int(stats.get("sample_size", 0) or 0),
    }
    return scored_market, regime_ctx, stats
