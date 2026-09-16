from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone


MAX_COMMAND_AGE = timedelta(minutes=10)
FUTURE_CLOCK_TOLERANCE = timedelta(seconds=60)


def current_utc_time() -> datetime:
    return datetime.now(timezone.utc)


def validate_message_freshness(
    received_at: str | datetime | None,
    *,
    clock: Callable[[], datetime],
) -> datetime:
    timestamp = _parse_aware_timestamp(received_at, "received_at")
    now = _parse_aware_timestamp(clock(), "current time")

    age = now - timestamp
    if age > MAX_COMMAND_AGE:
        raise ValueError("remote control message has expired")
    if age < -FUTURE_CLOCK_TOLERANCE:
        raise ValueError("remote control message timestamp is too far in the future")
    return timestamp


def _parse_aware_timestamp(value: str | datetime | None, name: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError(f"remote control {name} must be a valid timestamp") from exc
    if not isinstance(value, datetime):
        raise ValueError(f"remote control {name} must be a valid timestamp")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"remote control {name} must be timezone-aware")
    return value.astimezone(timezone.utc)
