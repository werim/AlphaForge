from __future__ import annotations

from enum import Enum


class CanonicalLifecycleEvent(str, Enum):
    SIGNAL_CREATED = "SIGNAL_CREATED"
    SIGNAL_REJECTED = "SIGNAL_REJECTED"
    SYMBOL_REJECTED = "SYMBOL_REJECTED"
    WAITING_ENTRY_ZONE = "WAITING_ENTRY_ZONE"
    ENTRY_TRIGGERED = "ENTRY_TRIGGERED"
    ORDER_PLACED = "ORDER_PLACED"
    ORDER_REJECTED = "ORDER_REJECTED"
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_CLOSED = "POSITION_CLOSED"
    ENTRY_TIMEOUT = "ENTRY_TIMEOUT"
    CANCELLED = "CANCELLED"


CANONICAL_LIFECYCLE_EVENTS: tuple[str, ...] = tuple(event.value for event in CanonicalLifecycleEvent)
CANONICAL_LIFECYCLE_EVENT_SET: frozenset[str] = frozenset(CANONICAL_LIFECYCLE_EVENTS)

LEGACY_LIFECYCLE_EVENT_MAP: dict[str, str] = {
    "CREATED": CanonicalLifecycleEvent.SIGNAL_CREATED.value,
    "SIGNAL_ACCEPTED": CanonicalLifecycleEvent.WAITING_ENTRY_ZONE.value,
    "ORDER_CANCELLED": CanonicalLifecycleEvent.CANCELLED.value,
    "CANCELED": CanonicalLifecycleEvent.CANCELLED.value,
    "EXPIRED": CanonicalLifecycleEvent.ENTRY_TIMEOUT.value,
    "OPEN_AT_END": CanonicalLifecycleEvent.POSITION_CLOSED.value,
    "TP_HIT": CanonicalLifecycleEvent.POSITION_CLOSED.value,
    "SL_HIT": CanonicalLifecycleEvent.POSITION_CLOSED.value,
}

CANONICAL_LIFECYCLE_TRANSITIONS: dict[str, set[str]] = {
    CanonicalLifecycleEvent.SIGNAL_CREATED.value: {
        CanonicalLifecycleEvent.SIGNAL_REJECTED.value,
        CanonicalLifecycleEvent.WAITING_ENTRY_ZONE.value,
        CanonicalLifecycleEvent.ORDER_REJECTED.value,
        CanonicalLifecycleEvent.CANCELLED.value,
    },
    CanonicalLifecycleEvent.WAITING_ENTRY_ZONE.value: {
        CanonicalLifecycleEvent.ENTRY_TRIGGERED.value,
        CanonicalLifecycleEvent.ENTRY_TIMEOUT.value,
        CanonicalLifecycleEvent.CANCELLED.value,
    },
    CanonicalLifecycleEvent.ENTRY_TRIGGERED.value: {
        CanonicalLifecycleEvent.ORDER_PLACED.value,
        CanonicalLifecycleEvent.ORDER_REJECTED.value,
        CanonicalLifecycleEvent.CANCELLED.value,
    },
    CanonicalLifecycleEvent.ORDER_PLACED.value: {
        CanonicalLifecycleEvent.POSITION_OPENED.value,
        CanonicalLifecycleEvent.ORDER_REJECTED.value,
        CanonicalLifecycleEvent.ENTRY_TIMEOUT.value,
        CanonicalLifecycleEvent.CANCELLED.value,
    },
    CanonicalLifecycleEvent.POSITION_OPENED.value: {
        CanonicalLifecycleEvent.POSITION_CLOSED.value,
        CanonicalLifecycleEvent.CANCELLED.value,
    },
    CanonicalLifecycleEvent.SIGNAL_REJECTED.value: {CanonicalLifecycleEvent.SIGNAL_CREATED.value},
    CanonicalLifecycleEvent.SYMBOL_REJECTED.value: {CanonicalLifecycleEvent.SIGNAL_CREATED.value},
    CanonicalLifecycleEvent.ORDER_REJECTED.value: {CanonicalLifecycleEvent.SIGNAL_CREATED.value},
    CanonicalLifecycleEvent.POSITION_CLOSED.value: {CanonicalLifecycleEvent.SIGNAL_CREATED.value},
    CanonicalLifecycleEvent.ENTRY_TIMEOUT.value: {CanonicalLifecycleEvent.SIGNAL_CREATED.value},
    CanonicalLifecycleEvent.CANCELLED.value: {CanonicalLifecycleEvent.SIGNAL_CREATED.value},
}


def normalize_lifecycle_event(raw: str | None, *, allow_legacy: bool = True) -> str:
    value = str(raw or "").strip().upper()
    if allow_legacy and value in LEGACY_LIFECYCLE_EVENT_MAP:
        value = LEGACY_LIFECYCLE_EVENT_MAP[value]
    if value not in CANONICAL_LIFECYCLE_EVENT_SET:
        raise ValueError(f"unknown lifecycle event: {raw!r}")
    return value


def is_canonical_lifecycle_event(raw: str | None) -> bool:
    try:
        normalize_lifecycle_event(raw, allow_legacy=False)
    except ValueError:
        return False
    return True


def is_valid_lifecycle_transition(previous_state: str | None, next_state: str) -> bool:
    next_canonical = normalize_lifecycle_event(next_state)
    if previous_state is None:
        return next_canonical == CanonicalLifecycleEvent.SIGNAL_CREATED.value
    previous_canonical = normalize_lifecycle_event(previous_state)
    return next_canonical in CANONICAL_LIFECYCLE_TRANSITIONS.get(previous_canonical, set())
