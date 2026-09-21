"""Shared deterministic signal geometry for runtime and backtest candidates."""
from __future__ import annotations

import math
from typing import Any, Mapping


def build_breakout_geometry(
    current: Mapping[str, Any], previous: Mapping[str, Any]
) -> dict[str, Any]:
    """Build AlphaForge's candle-structure breakout geometry, or fail closed.

    The calculation is extracted from the accepted backtest signal path: direction
    follows the current versus previous close, the stop spans both setup candles,
    and reward scales with breakout/body strength on that same timeframe.
    """
    return build_breakout_geometry_with_diagnostics(current, previous)[0]


def build_breakout_geometry_with_diagnostics(
    current: Mapping[str, Any], previous: Mapping[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """Return canonical geometry and a stable fail-closed diagnostic reason."""
    try:
        now = {name: float(current[name]) for name in ("open", "high", "low", "close")}
        prev = {name: float(previous[name]) for name in ("open", "high", "low", "close")}
    except (KeyError, TypeError, ValueError):
        return {}, "KLINE_MALFORMED_PAYLOAD"
    values = (*now.values(), *prev.values())
    if not all(math.isfinite(value) and value > 0.0 for value in values):
        return {}, "OHLC_INVALID"
    # Preserve the shared historical API's tolerance of synthetic/backtest bars
    # whose open/close lie outside the recorded range; an inverted range itself
    # is unambiguously invalid provider evidence.
    if any(candle["low"] > candle["high"] for candle in (now, prev)):
        return {}, "OHLC_INVALID"
    side = "LONG" if now["close"] >= prev["close"] else "SHORT"
    entry = now["close"]
    stop = min(now["low"], prev["low"]) if side == "LONG" else max(now["high"], prev["high"])
    risk = entry - stop if side == "LONG" else stop - entry
    if risk <= 0.0:
        return {}, "ZERO_RISK_GEOMETRY"
    body = abs(now["close"] - now["open"])
    breakout_strength = (
        max(0.0, (now["close"] - prev["high"]) / prev["high"])
        if side == "LONG"
        else max(0.0, (prev["low"] - now["close"]) / prev["low"])
    )
    rr = max(1.1, min(3.5, 1.2 + breakout_strength * 25.0 + body / now["open"] * 8.0))
    target = entry + rr * risk if side == "LONG" else entry - rr * risk
    if target <= 0.0:
        return {}, "INVALID_TARGET"
    return {
        "entry": entry,
        "side": side,
        "sl": stop,
        "tp": target,
        "rr": rr,
        "setup_type": "BREAKOUT_UP" if side == "LONG" else "BREAKDOWN_DOWN",
        "setup_reason": "CLOSE_ABOVE_PREV_HIGH" if side == "LONG" else "CLOSE_BELOW_PREV_LOW",
        "breakout_strength": breakout_strength,
    }, None


def build_regime_guided_geometry_with_diagnostics(
    current: Mapping[str, Any],
    previous: Mapping[str, Any],
    *,
    side: str,
    setup_type: str,
    setup_phase: str,
) -> tuple[dict[str, Any], str | None]:
    """Build executable geometry after the higher-timeframe side is selected.

    Unlike ``build_breakout_geometry_with_diagnostics``, candle-to-candle direction
    is evidence for timing and reward strength, not an independent trade-side
    decision.  Stop placement remains structural and therefore fail-closed.
    """
    normalized_side = str(side or "").upper()
    if normalized_side not in {"LONG", "SHORT"}:
        return {}, "REGIME_SIDE_INVALID"
    try:
        now = {name: float(current[name]) for name in ("open", "high", "low", "close")}
        prev = {name: float(previous[name]) for name in ("open", "high", "low", "close")}
    except (KeyError, TypeError, ValueError):
        return {}, "KLINE_MALFORMED_PAYLOAD"
    values = (*now.values(), *prev.values())
    if not all(math.isfinite(value) and value > 0.0 for value in values):
        return {}, "OHLC_INVALID"
    if any(candle["low"] > candle["high"] for candle in (now, prev)):
        return {}, "OHLC_INVALID"

    entry = now["close"]
    stop = (min(now["low"], prev["low"]) if normalized_side == "LONG"
            else max(now["high"], prev["high"]))
    risk = entry - stop if normalized_side == "LONG" else stop - entry
    if risk <= 0.0:
        return {}, "ZERO_RISK_GEOMETRY"
    body = abs(now["close"] - now["open"])
    breakout_strength = (
        max(0.0, (now["close"] - prev["high"]) / prev["high"])
        if normalized_side == "LONG"
        else max(0.0, (prev["low"] - now["close"]) / prev["low"])
    )
    rr = max(1.1, min(3.5, 1.2 + breakout_strength * 25.0 + body / now["open"] * 8.0))
    target = entry + rr * risk if normalized_side == "LONG" else entry - rr * risk
    if target <= 0.0:
        return {}, "INVALID_TARGET"
    return {
        "entry": entry,
        "side": normalized_side,
        "sl": stop,
        "tp": target,
        "rr": rr,
        "setup_type": setup_type,
        "setup_phase": setup_phase,
        "setup_reason": f"REGIME_GUIDED_{setup_phase}",
        "breakout_strength": breakout_strength,
        "geometry_status": "COMPLETE",
        "geometry_reason": None,
        "geometry_source": "MTF_REGIME_GUIDED_CLOSED_KLINES",
    }, None


def derive_setup_structure_with_diagnostics(
    candles: list[Mapping[str, Any]], *, side: str, lookback: int = 12,
) -> tuple[dict[str, Any], str | None]:
    """Derive setup-timeframe support/resistance levels without an RR target.

    This intentionally reuses the closed setup candle window already required by
    the MTF setup layer.  Its extrema are evidence, not a target multiplier:
    later candidate construction compares the independently chosen execution
    entry with these levels and fails closed if they do not form valid geometry.
    """
    normalized_side = str(side or "").strip().upper()
    if normalized_side not in {"LONG", "SHORT"}:
        return {}, "REGIME_SIDE_INVALID"
    if len(candles) < lookback:
        return {}, "KLINE_INSUFFICIENT_ROWS"
    try:
        window = [
            {name: float(candle[name]) for name in ("high", "low", "close")}
            for candle in candles[-lookback:]
        ]
    except (KeyError, TypeError, ValueError):
        return {}, "KLINE_MALFORMED_PAYLOAD"
    if any(
        not all(math.isfinite(value) and value > 0.0 for value in candle.values())
        or candle["low"] > candle["high"]
        for candle in window
    ):
        return {}, "OHLC_INVALID"
    if normalized_side == "LONG":
        return {
            "structural_stop": min(candle["low"] for candle in window),
            "structural_target": max(candle["high"] for candle in window),
            "stop_source": "setup_window_support",
            "target_source": "setup_window_resistance",
        }, None
    return {
        "structural_stop": max(candle["high"] for candle in window),
        "structural_target": min(candle["low"] for candle in window),
        "stop_source": "setup_window_resistance",
        "target_source": "setup_window_support",
    }, None


def build_structural_geometry_with_diagnostics(
    *, entry: Any, side: str, setup_type: str, setup_phase: str,
    structure: Mapping[str, Any], setup_timeframe: str, execution_timeframe: str,
    entry_source: str,
) -> tuple[dict[str, Any], str | None]:
    """Calculate RR from independent setup structure and execution entry.

    ``MIN_RR`` is deliberately absent from this function.  It is a downstream
    decision filter, while candidate RR is strictly reward/risk from the three
    observed price levels.
    """
    normalized_side = str(side or "").strip().upper()
    if normalized_side not in {"LONG", "SHORT"}:
        return {}, "REGIME_SIDE_INVALID"
    try:
        entry_price = float(entry)
        stop = float(structure["structural_stop"])
        target = float(structure["structural_target"])
    except (KeyError, TypeError, ValueError):
        return {}, "NO_STRUCTURAL_GEOMETRY"
    if not all(math.isfinite(value) and value > 0.0 for value in (entry_price, stop, target)):
        return {}, "NO_STRUCTURAL_GEOMETRY"
    if normalized_side == "LONG":
        risk, reward = entry_price - stop, target - entry_price
    else:
        risk, reward = stop - entry_price, entry_price - target
    if risk <= 0.0:
        return {}, "NO_STRUCTURAL_GEOMETRY"
    if reward <= 0.0:
        return {}, "INSUFFICIENT_STRUCTURAL_REWARD"
    rr = reward / risk
    return {
        "entry": entry_price,
        "side": normalized_side,
        "sl": stop,
        "tp": target,
        "rr": rr,
        "setup_type": setup_type,
        "setup_phase": setup_phase,
        "setup_reason": f"REGIME_GUIDED_{setup_phase}",
        "geometry_status": "COMPLETE",
        "geometry_reason": None,
        "geometry_source": "MTF_SETUP_STRUCTURE",
        "entry_source": entry_source,
        "stop_source": structure.get("stop_source"),
        "target_source": structure.get("target_source"),
        "setup_timeframe": setup_timeframe,
        "execution_timeframe": execution_timeframe,
        "structural_stop": stop,
        "structural_target": target,
        "candidate_rr": rr,
    }, None
