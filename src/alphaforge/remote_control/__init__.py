from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from alphaforge.remote_control.commands import (
    RemoteControlConfig,
    RemoteControlDispatchResult,
    RemoteControlResult,
    dispatch_remote_control_command,
    load_remote_control_config,
    map_remote_command,
    parse_remote_command,
)


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
