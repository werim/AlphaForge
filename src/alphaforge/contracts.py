from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from alphaforge.lifecycle_contract import (
    CANONICAL_LIFECYCLE_EVENTS,
    CANONICAL_LIFECYCLE_TRANSITIONS,
    CanonicalLifecycleEvent,
    is_canonical_lifecycle_event,
    is_valid_lifecycle_transition,
    normalize_lifecycle_event,
)


class RejectReason(str, Enum):
    LOW_EFFECTIVE_RR = "LOW_EFFECTIVE_RR"
    HIGH_SLIPPAGE = "HIGH_SLIPPAGE"
    THIN_LIQUIDITY = "THIN_LIQUIDITY"
    SPOOF_RISK = "SPOOF_RISK"
    OVEREXTENDED_MOVE = "OVEREXTENDED_MOVE"
    NEWS_RISK = "NEWS_RISK"
    LOW_ALIGNMENT = "LOW_ALIGNMENT"
    REGIME_MISMATCH = "REGIME_MISMATCH"
    EXCESSIVE_VOLATILITY = "EXCESSIVE_VOLATILITY"
    BAD_EXECUTION = "BAD_EXECUTION"
    CORRELATION_OVEREXPOSURE = "CORRELATION_OVEREXPOSURE"
    MOMENTUM_EXHAUSTION = "MOMENTUM_EXHAUSTION"
    MTF_REGIME_UNAVAILABLE = "MTF_REGIME_UNAVAILABLE"
    MTF_SETUP_UNAVAILABLE = "MTF_SETUP_UNAVAILABLE"
    MTF_EXECUTION_UNAVAILABLE = "MTF_EXECUTION_UNAVAILABLE"
    MTF_CONTEXT_STALE = "MTF_CONTEXT_STALE"
    MTF_REGIME_SETUP_MISMATCH = "MTF_REGIME_SETUP_MISMATCH"
    MTF_SETUP_EXECUTION_MISMATCH = "MTF_SETUP_EXECUTION_MISMATCH"
    MTF_DIRECTION_MISMATCH = "MTF_DIRECTION_MISMATCH"
    MTF_NO_VALID_SETUP = "MTF_NO_VALID_SETUP"
    MTF_EXECUTION_NOT_CONFIRMED = "MTF_EXECUTION_NOT_CONFIRMED"
    UNKNOWN = "UNKNOWN"


class LifecycleEventType(str, Enum):
    SIGNAL_CREATED = "SIGNAL_CREATED"
    SIGNAL_REJECTED = "SIGNAL_REJECTED"
    WAITING_ENTRY_ZONE = "WAITING_ENTRY_ZONE"
    ENTRY_TRIGGERED = "ENTRY_TRIGGERED"
    ORDER_PLACED = "ORDER_PLACED"
    ORDER_REJECTED = "ORDER_REJECTED"
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_CLOSED = "POSITION_CLOSED"
    TP_HIT = "TP_HIT"
    SL_HIT = "SL_HIT"
    OPEN_AT_END = "OPEN_AT_END"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"
    ENTRY_PENDING = "ENTRY_PENDING"
    ENTRY_SUBMITTED = "ENTRY_SUBMITTED"
    ENTRY_ACKNOWLEDGED = "ENTRY_ACKNOWLEDGED"
    ENTRY_PARTIAL = "ENTRY_PARTIAL"
    ENTRY_FILLED = "ENTRY_FILLED"
    STOP_SUBMITTED = "STOP_SUBMITTED"
    TAKE_PROFIT_SUBMITTED = "TAKE_PROFIT_SUBMITTED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    RECONCILIATION_REPAIR = "RECONCILIATION_REPAIR"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    EXCHANGE_REJECT = "EXCHANGE_REJECT"
    RUNTIME_PROTECTIVE_EXIT = "RUNTIME_PROTECTIVE_EXIT"
    ENTRY_TIMEOUT = "ENTRY_TIMEOUT"


ALLOWED_LIFECYCLE_TRANSITIONS: dict[str, set[str]] = {**CANONICAL_LIFECYCLE_TRANSITIONS}
ALLOWED_LIFECYCLE_TRANSITIONS.update({
    LifecycleEventType.ERROR.value: {LifecycleEventType.SIGNAL_CREATED.value},
    LifecycleEventType.ENTRY_PENDING.value: {LifecycleEventType.ENTRY_SUBMITTED.value, LifecycleEventType.CANCEL_REQUESTED.value, LifecycleEventType.EXECUTION_ERROR.value, LifecycleEventType.RUNTIME_PROTECTIVE_EXIT.value, LifecycleEventType.ERROR.value},
    LifecycleEventType.ENTRY_SUBMITTED.value: {LifecycleEventType.ENTRY_ACKNOWLEDGED.value, LifecycleEventType.EXCHANGE_REJECT.value, LifecycleEventType.EXECUTION_ERROR.value, LifecycleEventType.CANCEL_REQUESTED.value, LifecycleEventType.ERROR.value},
    LifecycleEventType.ENTRY_ACKNOWLEDGED.value: {LifecycleEventType.ENTRY_PARTIAL.value, LifecycleEventType.ENTRY_FILLED.value, LifecycleEventType.CANCEL_REQUESTED.value, LifecycleEventType.EXECUTION_ERROR.value, LifecycleEventType.ERROR.value},
    LifecycleEventType.ENTRY_PARTIAL.value: {LifecycleEventType.ENTRY_FILLED.value, LifecycleEventType.CANCEL_REQUESTED.value, LifecycleEventType.EXECUTION_ERROR.value, LifecycleEventType.ERROR.value},
    LifecycleEventType.ENTRY_FILLED.value: {LifecycleEventType.STOP_SUBMITTED.value, LifecycleEventType.TAKE_PROFIT_SUBMITTED.value, LifecycleEventType.CANCEL_REQUESTED.value, LifecycleEventType.RECONCILIATION_REPAIR.value, LifecycleEventType.ERROR.value},
    LifecycleEventType.STOP_SUBMITTED.value: {LifecycleEventType.TAKE_PROFIT_SUBMITTED.value, LifecycleEventType.CANCEL_REQUESTED.value, LifecycleEventType.RECONCILIATION_REPAIR.value, LifecycleEventType.ERROR.value},
    LifecycleEventType.TAKE_PROFIT_SUBMITTED.value: {LifecycleEventType.CANCEL_REQUESTED.value, LifecycleEventType.RECONCILIATION_REPAIR.value, LifecycleEventType.ERROR.value},
    LifecycleEventType.CANCEL_REQUESTED.value: {LifecycleEventType.CANCELLED.value, LifecycleEventType.RECONCILIATION_REPAIR.value, LifecycleEventType.EXECUTION_ERROR.value, LifecycleEventType.ERROR.value},
    LifecycleEventType.RECONCILIATION_REPAIR.value: {LifecycleEventType.CANCEL_REQUESTED.value, LifecycleEventType.CANCELLED.value, LifecycleEventType.ENTRY_ACKNOWLEDGED.value, LifecycleEventType.ENTRY_PARTIAL.value, LifecycleEventType.ENTRY_FILLED.value, LifecycleEventType.ERROR.value},
    LifecycleEventType.EXECUTION_ERROR.value: {LifecycleEventType.CANCEL_REQUESTED.value, LifecycleEventType.RECONCILIATION_REPAIR.value, LifecycleEventType.RUNTIME_PROTECTIVE_EXIT.value, LifecycleEventType.ERROR.value},
    LifecycleEventType.EXCHANGE_REJECT.value: {LifecycleEventType.CANCELLED.value, LifecycleEventType.RECONCILIATION_REPAIR.value, LifecycleEventType.ERROR.value},
    LifecycleEventType.RUNTIME_PROTECTIVE_EXIT.value: {LifecycleEventType.CANCEL_REQUESTED.value, LifecycleEventType.CANCELLED.value, LifecycleEventType.ERROR.value},
})


def canonical_utc_timestamp(raw: str | None = None) -> str:
    if raw:
        return raw
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_reject_reason(raw: str | None) -> str:
    value = str(raw or "").strip().upper()
    return value if value else RejectReason.UNKNOWN.value


def validate_transition(previous_state: str | None, next_state: str) -> bool:
    if previous_state is None:
        return next_state in {LifecycleEventType.SIGNAL_CREATED.value, LifecycleEventType.ENTRY_PENDING.value}
    try:
        return is_valid_lifecycle_transition(previous_state, next_state)
    except ValueError:
        return next_state in ALLOWED_LIFECYCLE_TRANSITIONS.get(previous_state, set())
