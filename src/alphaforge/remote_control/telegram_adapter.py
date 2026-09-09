from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from alphaforge.remote_control.commands import CommandParseError, RemoteControlCommand


ALLOWED_TELEGRAM_COMMANDS: Mapping[str, str] = {
    "/status": "STATUS",
    "/health": "HEALTH",
    "/report": "REPORT",
    "/rejects": "REJECTS",
    "/labels": "LABELS",
    "/errors": "ERRORS",
    "/help": "HELP",
}
MAX_TELEGRAM_TEXT_CHARS = 128
MAX_TELEGRAM_ID_CHARS = 64


class TelegramReplayAuditStore(Protocol):
    def reserve(self, message_id: str) -> bool: ...

    def record_remote_control_audit(
        self,
        *,
        transport: str,
        update_id: str | None,
        authorized_identity: str | None,
        normalized_command: str | None,
        result: str,
        rejection_reason: str | None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class TelegramRemoteControlConfig:
    allowed_user_ids: frozenset[str] = frozenset()
    allowed_chat_ids: frozenset[str] = frozenset()

    @classmethod
    def from_allowlists(
        cls,
        *,
        allowed_user_ids: Collection[int | str] = (),
        allowed_chat_ids: Collection[int | str] = (),
    ) -> TelegramRemoteControlConfig:
        return cls(
            allowed_user_ids=_normalize_allowlist(allowed_user_ids, "allowed_user_ids"),
            allowed_chat_ids=_normalize_allowlist(allowed_chat_ids, "allowed_chat_ids"),
        )


@dataclass(frozen=True, slots=True)
class TelegramRemoteControlRequest:
    update_id: str
    user_id: str
    chat_id: str
    authorized_identity: str
    command: RemoteControlCommand


_VERIFIED_REQUEST_CAPABILITY = object()


@dataclass(frozen=True, slots=True)
class VerifiedTelegramRemoteControlRequest:
    update_id: str
    user_id: str
    chat_id: str
    authorized_identity: str
    command: RemoteControlCommand
    _capability: object


@dataclass(frozen=True, slots=True)
class TelegramRemoteControlAdapterResult:
    accepted: bool
    command: str | None
    rejection_reason: str | None
    request: VerifiedTelegramRemoteControlRequest | None = None


def process_telegram_update(
    update: Mapping[str, Any],
    *,
    config: TelegramRemoteControlConfig,
    replay_store: TelegramReplayAuditStore,
) -> TelegramRemoteControlAdapterResult:
    update_id: str | None = None
    identity: str | None = None
    command_name: str | None = None

    try:
        if not isinstance(config, TelegramRemoteControlConfig):
            raise ValueError("INVALID_CONFIG")
        update_id = _required_id(update.get("update_id"), "update_id")
        message = _required_mapping(update.get("message"), "message")
        text = _required_text(message.get("text"))
        sender = _required_mapping(message.get("from"), "from")
        chat = _required_mapping(message.get("chat"), "chat")
        user_id = _required_id(sender.get("id"), "from.id")
        chat_id = _required_id(chat.get("id"), "chat.id")
        identity = f"telegram:user_id={user_id};chat_id={chat_id}"

        _authorize(user_id=user_id, chat_id=chat_id, config=config)
        command_name = parse_telegram_command(text)
        replay_key = f"telegram:{update_id}"
        if not replay_store.reserve(replay_key):
            raise ValueError("DUPLICATE_UPDATE_ID")

        request = VerifiedTelegramRemoteControlRequest(
            update_id=update_id,
            user_id=user_id,
            chat_id=chat_id,
            authorized_identity=identity,
            command=RemoteControlCommand(name=command_name, argv=(command_name,)),
            _capability=_VERIFIED_REQUEST_CAPABILITY,
        )
        replay_store.record_remote_control_audit(
            transport="telegram",
            update_id=update_id,
            authorized_identity=identity,
            normalized_command=command_name,
            result="ACCEPTED",
            rejection_reason=None,
        )
        return TelegramRemoteControlAdapterResult(
            accepted=True,
            command=command_name,
            rejection_reason=None,
            request=request,
        )
    except (CommandParseError, ValueError) as exc:
        reason = str(exc) or "MALFORMED_UPDATE"
        replay_store.record_remote_control_audit(
            transport="telegram",
            update_id=update_id,
            authorized_identity=identity,
            normalized_command=command_name,
            result="REJECTED",
            rejection_reason=reason,
        )
        return TelegramRemoteControlAdapterResult(
            accepted=False,
            command=command_name,
            rejection_reason=reason,
            request=None,
        )


def parse_telegram_command(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("MALFORMED_UPDATE")
    stripped = text.strip()
    if "\n" in stripped or "\r" in stripped or len(stripped) > MAX_TELEGRAM_TEXT_CHARS:
        raise ValueError("MALFORMED_UPDATE")
    if any(character.isspace() for character in stripped):
        raise CommandParseError("UNSUPPORTED_COMMAND")
    normalized = stripped.lower()
    command = ALLOWED_TELEGRAM_COMMANDS.get(normalized)
    if command is None:
        raise CommandParseError("UNSUPPORTED_COMMAND")
    return command


def is_verified_telegram_request(value: object) -> bool:
    return (
        isinstance(value, VerifiedTelegramRemoteControlRequest)
        and value._capability is _VERIFIED_REQUEST_CAPABILITY
    )


def _authorize(
    *,
    user_id: str,
    chat_id: str,
    config: TelegramRemoteControlConfig,
) -> None:
    if not config.allowed_user_ids and not config.allowed_chat_ids:
        raise ValueError("UNAUTHORIZED_IDENTITY")
    if config.allowed_user_ids and user_id not in config.allowed_user_ids:
        raise ValueError("UNAUTHORIZED_IDENTITY")
    if config.allowed_chat_ids and chat_id not in config.allowed_chat_ids:
        raise ValueError("UNAUTHORIZED_IDENTITY")


def _required_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("MALFORMED_UPDATE")
    return value


def _required_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("MALFORMED_UPDATE")
    return value


def _required_id(value: Any, name: str) -> str:
    del name
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("MALFORMED_UPDATE")
    normalized = str(value).strip()
    if not normalized or len(normalized) > MAX_TELEGRAM_ID_CHARS:
        raise ValueError("MALFORMED_UPDATE")
    if not normalized.lstrip("-").isdigit():
        raise ValueError("MALFORMED_UPDATE")
    return normalized


def _normalize_allowlist(values: Collection[int | str], name: str) -> frozenset[str]:
    if not isinstance(values, Collection) or isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be a collection")
    return frozenset(_required_id(value, name) for value in values)
