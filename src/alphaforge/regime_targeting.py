"""Regime/setup-aware target contract without profit-amount forcing.

The existing structural target remains market-evidence authority.  This module
only classifies realization/trailing permission and verifies that the target
still clears execution-adjusted RR.  It never widens TP to satisfy PnL goals.
"""
from __future__ import annotations

import math
from typing import Any

RISK_OFF_REGIMES = {"PANIC", "LOW_LIQUIDITY", "NEWS_DRIVEN"}
CONTINUATION_REGIMES = {"TRENDING", "BREAKOUT", "SHORT_SQUEEZE", "RANGE_COMPRESSION"}
SHORT_REALIZATION_REGIMES = {"MEAN_REVERTING", "CHOPPY"}
CONTINUATION_PHASES = {"CONTINUATION", "REENTRY_READY"}


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def build_regime_target_policy(
    *,
    side: Any,
    entry: Any,
    stop: Any,
    target: Any,
    effective_rr: Any,
    min_effective_rr: Any,
    regime: Any = None,
    setup_phase: Any = None,
    target_source: Any = None,
) -> dict[str, Any]:
    """Return a fail-closed target/exit policy for an already-built candidate.

    Numeric target authority stays with structural market geometry.  Regime and
    setup phase can permit continuation/trailing behavior, but this function
    deliberately does not invent a trailing distance or a wider target.
    """
    normalized_side = str(side or "").strip().upper()
    normalized_regime = str(regime or "UNKNOWN").strip().upper()
    normalized_phase = str(setup_phase or "UNKNOWN").strip().upper()

    px = _finite(entry)
    sl = _finite(stop)
    tp = _finite(target)
    eff = _finite(effective_rr)
    min_eff = _finite(min_effective_rr)

    if normalized_side not in {"LONG", "SHORT"}:
        return {"status": "UNAVAILABLE_BLOCKING", "reason": "TARGET_POLICY_SIDE_UNAVAILABLE"}
    if None in {px, sl, tp, eff, min_eff}:
        return {"status": "UNAVAILABLE_BLOCKING", "reason": "TARGET_POLICY_EVIDENCE_UNAVAILABLE"}
    assert px is not None and sl is not None and tp is not None and eff is not None and min_eff is not None

    if normalized_side == "LONG":
        geometry_valid = sl < px < tp
    else:
        geometry_valid = tp < px < sl
    if not geometry_valid:
        return {"status": "INVALID", "reason": "TARGET_POLICY_GEOMETRY_INVALID"}
    if eff < min_eff:
        return {
            "status": "REJECTED",
            "reason": "LOW_EFFECTIVE_RR_AFTER_COSTS",
            "effective_rr": eff,
            "min_effective_rr": min_eff,
        }

    risk_off = normalized_regime in RISK_OFF_REGIMES
    continuation = (
        normalized_regime in CONTINUATION_REGIMES
        and normalized_phase in CONTINUATION_PHASES
    )
    shorter = (
        normalized_regime in SHORT_REALIZATION_REGIMES
        or normalized_phase == "PULLBACK"
    )

    if risk_off:
        realization_profile = "RISK_OFF_STRUCTURAL_TARGET_ONLY"
        trailing_allowed = False
        new_risk_posture = "NO_NEW_RISK_RECOMMENDED"
    elif continuation:
        realization_profile = "CONTINUATION_TRAILING_ELIGIBLE"
        trailing_allowed = True
        new_risk_posture = "NORMAL"
    elif shorter:
        realization_profile = "EARLY_REALIZATION_ELIGIBLE"
        trailing_allowed = False
        new_risk_posture = "NORMAL"
    else:
        realization_profile = "STRUCTURAL_TARGET_ONLY"
        trailing_allowed = False
        new_risk_posture = "NORMAL"

    return {
        "status": "COMPLETE",
        "reason": "",
        "regime": normalized_regime,
        "setup_phase": normalized_phase,
        "target": tp,
        "target_source": str(target_source or "UNKNOWN"),
        "target_action": "KEEP_STRUCTURAL_TARGET",
        "target_authority": "STRUCTURAL_MARKET_EVIDENCE",
        "realization_profile": realization_profile,
        "trailing_allowed": trailing_allowed,
        "trailing_distance": None,
        "trailing_distance_status": "UNSET_REQUIRES_CALIBRATION",
        "new_risk_posture": new_risk_posture,
        "effective_rr": eff,
        "min_effective_rr": min_eff,
        "execution_cost_gate_passed": True,
        "profit_amount_targeting_used": False,
        "monthly_return_targeting_used": False,
    }
