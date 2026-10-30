"""Canonical regime authority shared by runtime classification and qualification.

Only empirically validated states may contribute qualification coverage or enable
strategy behavior. The wider vocabulary remains explicit, but dormant states
cannot become authority merely because a producer writes their label.
"""
from __future__ import annotations

from typing import Any, Final

REGIME_AUTHORITY_VERSION: Final = "canonical-regime-v1"
REGIME_STATES: Final = (
    "TRENDING",
    "MEAN_REVERTING",
    "CHOPPY",
    "PANIC",
    "LOW_LIQUIDITY",
    "BREAKOUT",
    "SHORT_SQUEEZE",
    "RANGE_COMPRESSION",
    "NEWS_DRIVEN",
    "UNKNOWN",
)
ACTIVE_REGIME_STATES: Final = frozenset({"TRENDING", "CHOPPY"})
QUALIFIABLE_REGIME_STATES: Final = ACTIVE_REGIME_STATES

REGIME_TRANSITION_POLICY: Final = {
    "clock": "CLOSED_CANDLE_ONLY",
    "future_evidence": "REJECT",
    "hysteresis": "LEGACY_SINGLE_DIRECTION_THRESHOLD_NO_SECONDARY_BAND",
    "unvalidated_transitions": "BLOCK",
}

REGIME_DEFINITIONS: Final = {
    "TRENDING": {
        "activation_status": "ACTIVE",
        "required_evidence": ("20_closed_ohlc", "8_20_ma_direction"),
        "trade_policy": "REGIME_GUIDED_LEGACY",
        "allowed_strategy_phases": ("CONTINUATION", "PULLBACK", "REENTRY_READY"),
    },
    "CHOPPY": {
        "activation_status": "ACTIVE",
        "required_evidence": ("20_closed_ohlc", "8_20_ma_neutral"),
        "trade_policy": "NO_TRADE",
        "allowed_strategy_phases": (),
    },
    "MEAN_REVERTING": {
        "activation_status": "UNVALIDATED",
        "required_evidence": ("validated_mean_reversion_classifier",),
        "trade_policy": "NO_TRADE_UNTIL_VALIDATED",
        "allowed_strategy_phases": (),
    },
    "PANIC": {
        "activation_status": "UNVALIDATED",
        "required_evidence": ("validated_panic_classifier",),
        "trade_policy": "NO_TRADE_UNTIL_VALIDATED",
        "allowed_strategy_phases": (),
    },
    "LOW_LIQUIDITY": {
        "activation_status": "UNVALIDATED",
        "required_evidence": ("measured_liquidity_authority", "validated_low_liquidity_classifier"),
        "trade_policy": "NO_TRADE_UNTIL_VALIDATED",
        "allowed_strategy_phases": (),
    },
    "BREAKOUT": {
        "activation_status": "UNVALIDATED",
        "required_evidence": ("validated_breakout_classifier",),
        "trade_policy": "NO_TRADE_UNTIL_VALIDATED",
        "allowed_strategy_phases": (),
    },
    "SHORT_SQUEEZE": {
        "activation_status": "UNVALIDATED",
        "required_evidence": ("validated_derivatives_squeeze_classifier",),
        "trade_policy": "NO_TRADE_UNTIL_VALIDATED",
        "allowed_strategy_phases": (),
    },
    "RANGE_COMPRESSION": {
        "activation_status": "UNVALIDATED",
        "required_evidence": ("validated_range_compression_classifier",),
        "trade_policy": "NO_TRADE_UNTIL_VALIDATED",
        "allowed_strategy_phases": (),
    },
    "NEWS_DRIVEN": {
        "activation_status": "UNAVAILABLE_NO_AUTHORITATIVE_EVENT_SOURCE",
        "required_evidence": ("authoritative_event_source", "event_publication_time_lte_decision_time"),
        "trade_policy": "NO_TRADE_UNTIL_VALIDATED",
        "allowed_strategy_phases": (),
    },
    "UNKNOWN": {
        "activation_status": "UNAVAILABLE",
        "required_evidence": (),
        "trade_policy": "NO_TRADE_FAIL_CLOSED",
        "allowed_strategy_phases": (),
    },
}


def normalize_regime_name(value: Any) -> str:
    state = str(value or "UNKNOWN").upper()
    return state if state in REGIME_DEFINITIONS else "UNKNOWN"


def regime_definition(value: Any) -> dict[str, Any]:
    return dict(REGIME_DEFINITIONS[normalize_regime_name(value)])


def is_qualifiable_regime(value: Any) -> bool:
    return normalize_regime_name(value) in QUALIFIABLE_REGIME_STATES


def regime_authority_metadata(value: Any, *, classification_complete: bool) -> dict[str, Any]:
    state = normalize_regime_name(value)
    definition = REGIME_DEFINITIONS[state]
    activation_status = str(definition["activation_status"])
    classification_status = (
        "INCOMPLETE"
        if not classification_complete
        else "COMPLETE"
        if activation_status == "ACTIVE"
        else activation_status
    )
    return {
        "regime_authority_version": REGIME_AUTHORITY_VERSION,
        "classification_status": classification_status,
        "qualification_eligible": bool(classification_complete and is_qualifiable_regime(state)),
        "activation_status": activation_status,
        "trade_policy": definition["trade_policy"],
        "allowed_strategy_phases": list(definition["allowed_strategy_phases"]),
        "required_regime_evidence": list(definition["required_evidence"]),
        "transition_policy": REGIME_TRANSITION_POLICY["clock"],
        "future_evidence_policy": REGIME_TRANSITION_POLICY["future_evidence"],
        "hysteresis_policy": REGIME_TRANSITION_POLICY["hysteresis"],
        "unvalidated_transition_policy": REGIME_TRANSITION_POLICY["unvalidated_transitions"],
    }
