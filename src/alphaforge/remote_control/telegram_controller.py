from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from alphaforge.remote_control.commands import (
    RemoteControlConfig,
    RemoteControlDispatchResult,
    RemoteControlResult,
    map_remote_command,
)
from alphaforge.remote_control.telegram_adapter import VerifiedTelegramRemoteControlRequest, is_verified_telegram_request
from alphaforge.remote_control.telegram_control import (
    CONFIRMABLE_OPERATIONS,
    SQLiteTelegramControlStore,
    TelegramControlError,
    execute_paper_action,
)
from alphaforge.remote_control.telegram_observability import (
    TELEGRAM_QUERY_COMMANDS,
    execute_telegram_observability,
)


TELEGRAM_EXECUTOR_COMMANDS = {"STATUS", "HEALTH"}
TELEGRAM_MUTATION_COMMANDS = set(CONFIRMABLE_OPERATIONS)
TELEGRAM_CONTROLLER_COMMANDS = TELEGRAM_EXECUTOR_COMMANDS | set(TELEGRAM_QUERY_COMMANDS) | TELEGRAM_MUTATION_COMMANDS | {"CONFIRM", "CANCEL", "HELP"}
EXECUTABLE_TELEGRAM_COMMANDS = (
    "/status",
    "/health",
    "/report",
    "/rejects",
    "/labels",
    "/errors",
    "/help",
    "/preflight",
    "/pause",
    "/resume",
    "/recovery",
    "/confirm <id>",
    "/cancel <id>",
)
SAFE_QUERY_ERROR = "remote control query failed safely"
SAFE_CONTROL_ERROR = "remote control action failed safely"


def process_telegram_request(
    request: VerifiedTelegramRemoteControlRequest,
    *,
    config: RemoteControlConfig | Mapping[str, str] | None = None,
    executor: Callable[..., RemoteControlResult] | None = None,
    query_executor: Callable[..., RemoteControlResult] = execute_telegram_observability,
    control_store: SQLiteTelegramControlStore | None = None,
    action_executor: Callable[..., RemoteControlResult] = execute_paper_action,
    confirmation_ttl_seconds: float = 120.0,
    timeout: float = 5.0,
    max_output_chars: int = 4096,
) -> RemoteControlDispatchResult:
    if control_store is None and executor is not None:
        candidate_store = getattr(executor, "telegram_control_store", None)
        if isinstance(candidate_store, SQLiteTelegramControlStore):
            control_store = candidate_store
        candidate_action = getattr(executor, "telegram_action_executor", None)
        if callable(candidate_action):
            action_executor = candidate_action
    if not is_verified_telegram_request(request):
        return _safe_failure("UNKNOWN", "invalid remote control request", max_output_chars=max_output_chars)

    command = request.command
    if command.name == "HELP":
        return _help_result(max_output_chars=max_output_chars)
    if command.name not in TELEGRAM_CONTROLLER_COMMANDS:
        return _safe_failure(command.name, "UNSUPPORTED_COMMAND", max_output_chars=max_output_chars)

    try:
        trusted_config = _trusted_config_values(config)
    except Exception:
        return _safe_failure(command.name, SAFE_CONTROL_ERROR, max_output_chars=max_output_chars)

    if command.name in TELEGRAM_EXECUTOR_COMMANDS:
        if command.argv != (command.name,):
            return _safe_failure(command.name, "UNSUPPORTED_COMMAND", max_output_chars=max_output_chars)
        if executor is None:
            return _safe_failure(command.name, "EXECUTOR_UNAVAILABLE", max_output_chars=max_output_chars)
        try:
            mapped = map_remote_command(command.name, trusted_config)
            result = executor(
                mapped,
                config=trusted_config,
                timeout=timeout,
                max_output_chars=max_output_chars,
            )
        except Exception:
            return _safe_failure(command.name, SAFE_CONTROL_ERROR, max_output_chars=max_output_chars)
        return _validated_result(command.name, result, max_output_chars=max_output_chars)

    if command.name in TELEGRAM_QUERY_COMMANDS:
        if command.argv != (command.name,):
            return _safe_failure(command.name, "UNSUPPORTED_COMMAND", max_output_chars=max_output_chars)
        try:
            result = query_executor(
                command,
                config=trusted_config,
                timeout=timeout,
                max_output_chars=max_output_chars,
            )
        except Exception:
            return _safe_failure(command.name, SAFE_QUERY_ERROR, max_output_chars=max_output_chars)
        return _validated_result(command.name, result, max_output_chars=max_output_chars)

    if command.name in TELEGRAM_MUTATION_COMMANDS:
        if command.argv != (command.name,):
            return _safe_failure(command.name, "UNSUPPORTED_COMMAND", max_output_chars=max_output_chars)
        if control_store is None:
            return _safe_failure(command.name, "CONTROL_STORE_UNAVAILABLE", max_output_chars=max_output_chars)
        try:
            pending = control_store.create_confirmation(
                operation=command.name,
                campaign_id=trusted_config["remote_control_campaign_id"],
                run_id=trusted_config["remote_control_run_id"],
                ttl_seconds=confirmation_ttl_seconds,
            )
        except Exception:
            return _safe_failure(command.name, SAFE_CONTROL_ERROR, max_output_chars=max_output_chars)
        return _dispatch_result(
            command=command.name,
            returncode=0,
            stdout=(
                f"CONFIRM_REQUIRED id={pending.confirmation_id} operation={pending.operation} "
                f"expires={pending.expires_at}"
            ),
            stderr="",
            max_output_chars=max_output_chars,
        )

    if command.name in {"CONFIRM", "CANCEL"}:
        if len(command.argv) != 2 or command.argv[0] != command.name:
            return _safe_failure(command.name, "UNSUPPORTED_COMMAND", max_output_chars=max_output_chars)
        if control_store is None:
            return _safe_failure(command.name, "CONTROL_STORE_UNAVAILABLE", max_output_chars=max_output_chars)
        confirmation_id = command.argv[1]
        if command.name == "CANCEL":
            try:
                operation = control_store.cancel_confirmation(
                    confirmation_id,
                    campaign_id=trusted_config["remote_control_campaign_id"],
                    run_id=trusted_config["remote_control_run_id"],
                )
            except TelegramControlError as exc:
                return _safe_failure(command.name, str(exc), max_output_chars=max_output_chars)
            except Exception:
                return _safe_failure(command.name, SAFE_CONTROL_ERROR, max_output_chars=max_output_chars)
            return _dispatch_result(
                command="CANCEL",
                returncode=0,
                stdout=f"CANCELLED id={confirmation_id} operation={operation}",
                stderr="",
                max_output_chars=max_output_chars,
            )

        try:
            pending = control_store.consume_confirmation(
                confirmation_id,
                campaign_id=trusted_config["remote_control_campaign_id"],
                run_id=trusted_config["remote_control_run_id"],
            )
        except TelegramControlError as exc:
            return _safe_failure(command.name, str(exc), max_output_chars=max_output_chars)
        except Exception:
            return _safe_failure(command.name, SAFE_CONTROL_ERROR, max_output_chars=max_output_chars)

        try:
            result = action_executor(
                pending.operation,
                config=trusted_config,
                timeout=timeout,
                max_output_chars=max_output_chars,
            )
        except Exception:
            result = RemoteControlResult(pending.operation, 1, "", SAFE_CONTROL_ERROR)
        result_code = "SUCCESS" if isinstance(result, RemoteControlResult) and result.returncode == 0 else "FAILED"
        try:
            control_store.record_confirmation_result(confirmation_id, result_code)
        except Exception:
            if result_code == "SUCCESS":
                return _safe_failure("CONFIRM", "CONTROL_AUDIT_PERSIST_FAILED", max_output_chars=max_output_chars)
        if not isinstance(result, RemoteControlResult) or result.command != pending.operation:
            return _safe_failure("CONFIRM", "INVALID_EXECUTOR_RESULT", max_output_chars=max_output_chars)
        return _dispatch_result(
            command="CONFIRM",
            returncode=result.returncode,
            stdout=f"{pending.operation} {result.stdout}".strip(),
            stderr=result.stderr,
            max_output_chars=max_output_chars,
        )

    return _safe_failure(command.name, "UNSUPPORTED_COMMAND", max_output_chars=max_output_chars)


def build_telegram_help_text() -> str:
    commands = " ".join(EXECUTABLE_TELEGRAM_COMMANDS)
    return f"Supported commands: {commands}. PAPER mutations require expiring single-use confirmation; LIVE/order mutation is unavailable."


def _help_result(*, max_output_chars: int) -> RemoteControlDispatchResult:
    return _dispatch_result(
        command="HELP",
        returncode=0,
        stdout=build_telegram_help_text(),
        stderr="",
        max_output_chars=max_output_chars,
    )


def _validated_result(
    expected_command: str,
    result: object,
    *,
    max_output_chars: int,
) -> RemoteControlDispatchResult:
    if not isinstance(result, RemoteControlResult) or result.command != expected_command:
        return _safe_failure(expected_command, "INVALID_EXECUTOR_RESULT", max_output_chars=max_output_chars)
    return _dispatch_result(
        command=result.command,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        max_output_chars=max_output_chars,
    )


def _safe_failure(command: str, message: str, *, max_output_chars: int) -> RemoteControlDispatchResult:
    safe_command = command if command in TELEGRAM_CONTROLLER_COMMANDS else "UNSUPPORTED"
    return _dispatch_result(
        command=safe_command,
        returncode=1,
        stdout="",
        stderr=message,
        max_output_chars=max_output_chars,
    )


def _dispatch_result(
    *,
    command: str,
    returncode: int,
    stdout: str,
    stderr: str,
    max_output_chars: int,
) -> RemoteControlDispatchResult:
    safe_stdout = _safe_output(stdout, max_output_chars)
    safe_stderr = _safe_output(stderr, max_output_chars)
    status = "OK" if returncode == 0 else "FAIL"
    parts = [f"{command}: {status} rc={returncode}"]
    if safe_stdout:
        parts.append(f"stdout={safe_stdout}")
    if safe_stderr:
        parts.append(f"stderr={safe_stderr}")
    return RemoteControlDispatchResult(
        command=command,
        returncode=returncode,
        stdout=safe_stdout,
        stderr=safe_stderr,
        formatted=" | ".join(parts),
    )


def _safe_output(value: Any, max_output_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    safe = value.replace("\r", " ").replace("\n", " ")
    return safe[: max(1, int(max_output_chars))]


def _trusted_config_values(config: RemoteControlConfig | Mapping[str, str] | None) -> dict[str, str]:
    if isinstance(config, RemoteControlConfig):
        values: Mapping[str, str] = {
            "remote_control_db_path": config.db,
            "remote_control_campaign_id": config.cid,
            "remote_control_run_id": config.run,
        }
    elif isinstance(config, Mapping):
        values = config
    else:
        raise ValueError("trusted config unavailable")
    return {
        "remote_control_db_path": _required_trusted_text(values.get("remote_control_db_path")),
        "remote_control_campaign_id": _required_trusted_text(values.get("remote_control_campaign_id")),
        "remote_control_run_id": _required_trusted_text(values.get("remote_control_run_id")),
    }


def _required_trusted_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("trusted config value unavailable")
    return value.strip()
