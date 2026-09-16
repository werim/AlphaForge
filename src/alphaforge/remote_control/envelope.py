from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Collection

from alphaforge.remote_control import RemoteControlReplayStore, run_remote_control_message
from alphaforge.remote_control.auth import is_sender_allowed, normalize_mailbox
from alphaforge.remote_control.commands import RemoteControlDispatchResult, RemoteControlResult
from alphaforge.remote_control.freshness import current_utc_time


@dataclass(frozen=True)
class RemoteControlEmailEnvelope:
    message_id: str | None
    sender: str | None
    subject: str | None
    body: str | None
    received_at: str | datetime | None


def run_remote_control_envelope(
    envelope: RemoteControlEmailEnvelope,
    *,
    config_path: str | Path,
    executor: Callable[..., RemoteControlResult],
    replay_store: RemoteControlReplayStore,
    allowed_senders: Collection[str],
    clock: Callable[[], datetime] = current_utc_time,
    timeout: float = 5.0,
    max_output_chars: int = 4096,
) -> RemoteControlDispatchResult:
    if not isinstance(envelope, RemoteControlEmailEnvelope):
        raise ValueError("remote control envelope is required")
    sender = _strip_required(envelope.sender, "sender")
    if not is_sender_allowed(sender, allowed_senders):
        raise ValueError("unauthorized remote control sender")
    return run_remote_control_message(
        _strip_required(envelope.body, "body"),
        config_path=config_path,
        executor=executor,
        replay_store=replay_store,
        sender=normalize_mailbox(sender),
        message_id=_strip_required(envelope.message_id, "message_id"),
        received_at=envelope.received_at,
        subject=_strip_optional(envelope.subject),
        clock=clock,
        timeout=timeout,
        max_output_chars=max_output_chars,
    )


def _strip_required(value: str | None, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"remote control envelope {name} must be a non-empty string")
    return value.strip()


def _strip_optional(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("remote control envelope subject must be a string")
    return value.strip()
