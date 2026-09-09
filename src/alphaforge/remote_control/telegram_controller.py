from __future__ import annotations

from collections.abc import Callable

from alphaforge.remote_control.commands import RemoteControlCommand, RemoteControlDispatchResult, RemoteControlResult
from alphaforge.remote_control.telegram_adapter import VerifiedTelegramRemoteControlRequest, is_verified_telegram_request


TELEGRAM_EXECUTOR_COMMANDS = {"STATUS", "HEALTH"}
TELEGRAM_CONTROLLER_COMMANDS = TELEGRAM_EXECUTOR_COMMANDS | {"HELP"}
EXECUTABLE_TELEGRAM_COMMANDS = ("/status", "/health", "/help")
SAFE_EXECUTOR_ERROR = "remote control executor failed safely"


def process_telegram_request(
    request: VerifiedTelegramRemoteControlRequest,
    *,
    executor: Callable[[RemoteControlCommand], RemoteControlResult] | None = None,
    max_output_chars: int = 4096,
) -> RemoteControlDispatchResult:
    if not is_verified_telegram_request(request):
        return _safe_failure("UNKNOWN", "invalid remote control request", max_output_chars=max_output_chars)

    command = request.command
    if command.name == "HELP":
        return _help_result(max_output_chars=max_output_chars)
    if command.name not in TELEGRAM_EXECUTOR_COMMANDS:
        return _safe_failure(command.name, "UNSUPPORTED_COMMAND", max_output_chars=max_output_chars)
    if command.argv != (command.name,):
        return _safe_failure(command.name, "UNSUPPORTED_COMMAND", max_output_chars=max_output_chars)
    if executor is None:
        return _safe_failure(command.name, "EXECUTOR_UNAVAILABLE", max_output_chars=max_output_chars)

    try:
        result = executor(command)
    except Exception:
        return _safe_failure(command.name, SAFE_EXECUTOR_ERROR, max_output_chars=max_output_chars)

    if not isinstance(result, RemoteControlResult) or result.command != command.name:
        return _safe_failure(command.name, "INVALID_EXECUTOR_RESULT", max_output_chars=max_output_chars)
    return _dispatch_result(
        command=result.command,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        max_output_chars=max_output_chars,
    )


def build_telegram_help_text() -> str:
    commands = " ".join(EXECUTABLE_TELEGRAM_COMMANDS)
    return f"Supported read-only commands: {commands}"


def _help_result(*, max_output_chars: int) -> RemoteControlDispatchResult:
    return _dispatch_result(
        command="HELP",
        returncode=0,
        stdout=build_telegram_help_text(),
        stderr="",
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
    stdout = _safe_text(stdout, max_output_chars=max_output_chars)
    stderr = _safe_text(stderr, max_output_chars=max_output_chars)
    status = "OK" if returncode == 0 else "FAIL"
    parts = [f"{command}: {status} rc={returncode}"]
    if stdout:
        parts.append(f"stdout={stdout}")
    if stderr:
        parts.append(f"stderr={stderr}")
    return RemoteControlDispatchResult(
        command=command,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        formatted=" | ".join(parts),
    )


def _safe_text(value: str, *, max_output_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\r", " ").replace("\n", " ")[:max_output_chars]
