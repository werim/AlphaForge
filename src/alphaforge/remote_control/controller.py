from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Callable

from alphaforge.remote_control import RemoteControlReplayStore
from alphaforge.remote_control.auth import is_sender_allowed, normalize_mailbox
from alphaforge.remote_control.commands import (
    RemoteControlConfig,
    RemoteControlDispatchResult,
    RemoteControlResult,
    dispatch_remote_control_command,
    map_remote_command,
    parse_remote_command,
)
from alphaforge.remote_control.envelope import RemoteControlEmailEnvelope
from alphaforge.remote_control.freshness import current_utc_time, validate_message_freshness

MAX_BODY_CHARS = 1024
MAX_MESSAGE_ID_CHARS = 128


def process_envelope(
    envelope: RemoteControlEmailEnvelope,
    *,
    config: RemoteControlConfig,
    allowed_senders: Collection[str],
    replay_store: RemoteControlReplayStore,
    executor: Callable[..., RemoteControlResult],
    now: Callable[[], datetime] = current_utc_time,
    timeout: float = 5.0,
    max_output_chars: int = 4096,
) -> RemoteControlDispatchResult:
    if not isinstance(envelope, RemoteControlEmailEnvelope):
        raise ValueError("remote control envelope is required")

    sender = _required_text(envelope.sender, "sender")
    if not is_sender_allowed(sender, allowed_senders):
        raise ValueError("unauthorized remote control sender")
    validate_message_freshness(envelope.received_at, clock=now)

    body = _required_text(envelope.body, "body")
    if "\n" in body or "\r" in body:
        raise ValueError("remote control body must be a single line")
    if len(body) > MAX_BODY_CHARS:
        raise ValueError("remote control body is too long")

    command_name = parse_remote_command(body)
    message_id = _required_text(envelope.message_id, "message_id")
    if len(message_id) > MAX_MESSAGE_ID_CHARS:
        raise ValueError("remote control message_id is too long")
    if not replay_store.reserve(message_id):
        raise ValueError("duplicate remote control message_id")

    trusted = {
        "remote_control_db_path": config.db,
        "remote_control_campaign_id": config.cid,
        "remote_control_run_id": config.run,
    }
    command = map_remote_command(command_name, trusted)
    return dispatch_remote_control_command(
        command,
        config=trusted,
        executor=executor,
        timeout=timeout,
        max_output_chars=max_output_chars,
    )


def _required_text(value: str | None, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"remote control envelope {name} must be a non-empty string")
    if name == "sender":
        return normalize_mailbox(value)
    return value.strip()
