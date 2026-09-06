from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


class CommandParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RemoteControlCommand:
    name: str
    argv: tuple[str, ...]


def _require_trusted_config(config: Mapping[str, str], *keys: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for key in keys:
        value = config.get(key)
        if not value:
            raise ValueError(f"missing trusted config value: {key}")
        values[key] = str(value)
    return values


def parse_remote_command(text: str) -> str:
    if text == "AF STATUS":
        return "STATUS"
    if text == "AF HEALTH":
        return "HEALTH"
    raise CommandParseError("unsupported remote control command")


def map_remote_command(command: str, config: Mapping[str, str]) -> RemoteControlCommand:
    trusted = _require_trusted_config(
        config,
        "remote_control_db_path",
        "remote_control_campaign_id",
        "remote_control_run_id",
    )
    if command == "STATUS":
        return RemoteControlCommand(
            name="STATUS",
            argv=(
                "burnin_ops",
                "--db",
                trusted["remote_control_db_path"],
                "status",
                "--campaign-id",
                trusted["remote_control_campaign_id"],
                "--run-id",
                trusted["remote_control_run_id"],
            ),
        )
    if command == "HEALTH":
        return RemoteControlCommand(
            name="HEALTH",
            argv=(
                "burnin_ops",
                "--db",
                trusted["remote_control_db_path"],
                "health",
                "--campaign-id",
                trusted["remote_control_campaign_id"],
                "--run-id",
                trusted["remote_control_run_id"],
            ),
        )
    raise CommandParseError("unsupported remote control command")
