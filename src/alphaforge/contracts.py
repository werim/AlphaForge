from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum


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
    UNKNOWN = "UNKNOWN"


class LifecycleEventType(str, Enum):
    SIGNAL_CREATED = "SIGNAL_CREATED"
    SIGNAL_REJECTED = "SIGNAL_REJECTED"
    WAITING_ENTRY_ZONE = "WAITING_ENTRY_ZONE"
    ENTRY_TRIGGERED = "ENTRY_TRIGGERED"
    ORDER_PLACED = "ORDER_PLACED"
    ORDER_REJECTED = "ORDER_REJECTED"
    POSITION_OPENED = "POSITION_OPENED"
    TP_HIT = "TP_HIT"
    SL_HIT = "SL_HIT"
    OPEN_AT_END = "OPEN_AT_END"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"


ALLOWED_LIFECYCLE_TRANSITIONS: dict[str, set[str]] = {
    LifecycleEventType.SIGNAL_CREATED.value: {LifecycleEventType.SIGNAL_REJECTED.value, LifecycleEventType.WAITING_ENTRY_ZONE.value, LifecycleEventType.ERROR.value},
    LifecycleEventType.WAITING_ENTRY_ZONE.value: {LifecycleEventType.ENTRY_TRIGGERED.value, LifecycleEventType.CANCELLED.value, LifecycleEventType.ERROR.value},
    LifecycleEventType.ENTRY_TRIGGERED.value: {LifecycleEventType.ORDER_PLACED.value, LifecycleEventType.ORDER_REJECTED.value, LifecycleEventType.CANCELLED.value, LifecycleEventType.ERROR.value},
    LifecycleEventType.ORDER_PLACED.value: {LifecycleEventType.POSITION_OPENED.value, LifecycleEventType.ORDER_REJECTED.value, LifecycleEventType.CANCELLED.value, LifecycleEventType.ERROR.value},
    LifecycleEventType.POSITION_OPENED.value: {LifecycleEventType.TP_HIT.value, LifecycleEventType.SL_HIT.value, LifecycleEventType.OPEN_AT_END.value, LifecycleEventType.CANCELLED.value, LifecycleEventType.ERROR.value},
}


def canonical_utc_timestamp(raw: str | None = None) -> str:
    if raw:
        return raw
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_reject_reason(raw: str | None) -> str:
    value = str(raw or "").strip().upper()
    return value if value else RejectReason.UNKNOWN.value


def validate_transition(previous_state: str | None, next_state: str) -> bool:
    if previous_state is None:
        return next_state == LifecycleEventType.SIGNAL_CREATED.value
    return next_state in ALLOWED_LIFECYCLE_TRANSITIONS.get(previous_state, set())
