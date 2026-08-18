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
    try:
        now = {name: float(current[name]) for name in ("open", "high", "low", "close")}
        prev = {name: float(previous[name]) for name in ("open", "high", "low", "close")}
    except (KeyError, TypeError, ValueError):
        return {}
    values = (*now.values(), *prev.values())
    if not all(math.isfinite(value) and value > 0.0 for value in values):
        return {}
    side = "LONG" if now["close"] >= prev["close"] else "SHORT"
    entry = now["close"]
    stop = min(now["low"], prev["low"]) if side == "LONG" else max(now["high"], prev["high"])
    risk = entry - stop if side == "LONG" else stop - entry
    if risk <= 0.0:
        return {}
    body = abs(now["close"] - now["open"])
    breakout_strength = (
        max(0.0, (now["close"] - prev["high"]) / prev["high"])
        if side == "LONG"
        else max(0.0, (prev["low"] - now["close"]) / prev["low"])
    )
    rr = max(1.1, min(3.5, 1.2 + breakout_strength * 25.0 + body / now["open"] * 8.0))
    target = entry + rr * risk if side == "LONG" else entry - rr * risk
    if target <= 0.0:
        return {}
    return {
        "entry": entry,
        "side": side,
        "sl": stop,
        "tp": target,
        "rr": rr,
        "setup_type": "BREAKOUT_UP" if side == "LONG" else "BREAKDOWN_DOWN",
        "setup_reason": "CLOSE_ABOVE_PREV_HIGH" if side == "LONG" else "CLOSE_BELOW_PREV_LOW",
        "breakout_strength": breakout_strength,
    }
