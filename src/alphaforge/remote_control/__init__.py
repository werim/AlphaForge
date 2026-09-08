from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from alphaforge.remote_control.commands import (
    RemoteControlConfig,
    RemoteControlDispatchResult,
    RemoteControlResult,
    dispatch_remote_control_command,
    load_remote_control_config,
    map_remote_command,
    parse_remote_command,
)
from alphaforge.remote_control.freshness import current_utc_time, validate_message_freshness
from alphaforge.remote_control.telegram_adapter import (
    ALLOWED_TELEGRAM_COMMANDS,
    TelegramRemoteControlAdapterResult,
    TelegramRemoteControlConfig,
    TelegramRemoteControlRequest,
    parse_telegram_command,
    process_telegram_update,
)
from alphaforge.remote_control.telegram_controller import (
    build_telegram_help_text,
    process_telegram_request,
)


class RemoteControlReplayStore(Protocol):
    def reserve(self, message_id: str) -> bool: ...


def load_remote_control_config_file(path: str | Path) -> RemoteControlConfig:
    with Path(path).open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return load_remote_control_config(data)


def run_remote_control_local(
    text: str,
    *,
    config_path: str | Path,
    executor: Callable[..., RemoteControlResult],
    timeout: float = 5.0,
    max_output_chars: int = 4096,
) -> RemoteControlDispatchResult:
    command_name = parse_remote_command(text)
    config = load_remote_control_config_file(config_path)
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


def run_remote_control_message(
    body: str,
    *,
    config_path: str | Path,
    executor: Callable[..., RemoteControlResult],
    replay_store: RemoteControlReplayStore,
    sender: str | None = None,
    message_id: str | None = None,
    received_at: str | datetime | None = None,
    subject: str | None = None,
    clock: Callable[[], datetime] = current_utc_time,
    timeout: float = 5.0,
    max_output_chars: int = 4096,
    max_body_chars: int = 1024,
    max_message_id_chars: int = 128,
) -> RemoteControlDispatchResult:
    del subject
    config = load_remote_control_config_file(config_path)
    if not isinstance(sender, str) or not sender.strip():
        raise ValueError("remote control sender must be a non-empty string")
    if sender.strip() != config.authorized_sender:
        raise ValueError("unauthorized remote control sender")
    validate_message_freshness(received_at, clock=clock)
    if not isinstance(message_id, str) or not message_id.strip():
        raise ValueError("remote control message_id must be a non-empty string")
    message_id = message_id.strip()
    if len(message_id) > max_message_id_chars:
        raise ValueError("remote control message_id is too long")
    if not replay_store.reserve(message_id):
        raise ValueError("duplicate remote control message_id")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("remote control body must be a non-empty string")
    if "\n" in body or "\r" in body:
        raise ValueError("remote control body must be a single line")
    if len(body) > max_body_chars:
        raise ValueError("remote control body is too long")
    trusted = {
        "remote_control_db_path": config.db,
        "remote_control_campaign_id": config.cid,
        "remote_control_run_id": config.run,
    }
    command_name = parse_remote_command(body.strip())
    command = map_remote_command(command_name, trusted)
    return dispatch_remote_control_command(
        command,
        config=trusted,
        executor=executor,
        timeout=timeout,
        max_output_chars=max_output_chars,
    )
