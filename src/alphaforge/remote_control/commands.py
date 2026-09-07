from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class CommandParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RemoteControlCommand:
    name: str
    argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RemoteControlResult:
    command: str
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class RemoteControlConfig:
    db: str
    cid: str
    run: str


def load_remote_control_config(data: Mapping[str, Any]) -> RemoteControlConfig:
    if not isinstance(data, Mapping):
        raise ValueError("remote control config must be an object")
    allowed = {"db", "cid", "run"}
    if set(data.keys()) != allowed:
        raise ValueError("remote control config must contain exactly db, cid, and run")
    values: dict[str, str] = {}
    for key in allowed:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"invalid remote control config field: {key}")
        values[key] = value
    return RemoteControlConfig(db=values["db"], cid=values["cid"], run=values["run"])


def _trusted_values(config: RemoteControlConfig) -> dict[str, str]:
    return {
        "remote_control_db_path": config.db,
        "remote_control_campaign_id": config.cid,
        "remote_control_run_id": config.run,
    }


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


def execute_remote_command(
    command: RemoteControlCommand,
    *,
    config: Mapping[str, str],
    timeout: float = 5.0,
    max_output_chars: int = 4096,
) -> RemoteControlResult:
    if command.name not in {"STATUS", "HEALTH"}:
        raise CommandParseError("unsupported remote control command")
    expected = map_remote_command(command.name, config)
    if command.argv != expected.argv:
        raise CommandParseError("unexpected remote control argv")
    completed = subprocess.run(
        command.argv,
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    stdout = (completed.stdout or "")[:max_output_chars]
    stderr = (completed.stderr or "")[:max_output_chars]
    return RemoteControlResult(command=command.name, returncode=completed.returncode, stdout=stdout, stderr=stderr)


def run_remote_control_text(
    text: str,
    config: RemoteControlConfig,
    *,
    timeout: float = 5.0,
    max_output_chars: int = 4096,
) -> RemoteControlResult:
    command_name = parse_remote_command(text)
    trusted = _trusted_values(config)
    mapped = map_remote_command(command_name, trusted)
    return execute_remote_command(
        mapped,
        config=trusted,
        timeout=timeout,
        max_output_chars=max_output_chars,
    )
