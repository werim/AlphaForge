from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from alphaforge.remote_control.commands import RemoteControlConfig, RemoteControlResult
from alphaforge.remote_control.telegram_adapter import TelegramRemoteControlConfig, process_telegram_update
from alphaforge.remote_control.telegram_controller import process_telegram_request


DEFAULT_POLL_TIMEOUT_SECONDS = 10
DEFAULT_HTTP_TIMEOUT_SECONDS = 15
MAX_TELEGRAM_MESSAGE_CHARS = 3900


class TelegramHttpClient(Protocol):
    def get_updates(self, *, bot_token: str, offset: int | None, timeout: int) -> Mapping[str, Any]: ...

    def send_message(self, *, bot_token: str, chat_id: str, text: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class TelegramPollResult:
    next_offset: int | None
    updates_seen: int
    accepted: int
    rejected: int
    responses_sent: int
    send_failures: int
    transport_error: str | None = None


class UrlLibTelegramHttpClient:
    def __init__(self, *, http_timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS) -> None:
        self._http_timeout = http_timeout

    def get_updates(self, *, bot_token: str, offset: int | None, timeout: int) -> Mapping[str, Any]:
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        return self._post_json(bot_token=bot_token, method="getUpdates", payload=payload)

    def send_message(self, *, bot_token: str, chat_id: str, text: str) -> Mapping[str, Any]:
        return self._post_json(
            bot_token=bot_token,
            method="sendMessage",
            payload={"chat_id": chat_id, "text": text},
        )

    def _post_json(self, *, bot_token: str, method: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        token = _required_token(bot_token)
        url = f"https://api.telegram.org/bot{urllib.parse.quote(token, safe='')}/{method}"
        data = json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._http_timeout) as response:
            if response.status != 200:
                raise ValueError("TELEGRAM_HTTP_ERROR")
            decoded = json.loads(response.read().decode("utf-8"))
        if not isinstance(decoded, Mapping):
            raise ValueError("TELEGRAM_INVALID_JSON")
        return decoded


def poll_telegram_once(
    *,
    bot_token: str,
    offset: int | None,
    telegram_config: TelegramRemoteControlConfig,
    remote_config: RemoteControlConfig | Mapping[str, str],
    replay_store: Any,
    executor: Callable[..., RemoteControlResult],
    http_client: TelegramHttpClient,
    poll_timeout: int = DEFAULT_POLL_TIMEOUT_SECONDS,
    max_message_chars: int = MAX_TELEGRAM_MESSAGE_CHARS,
) -> TelegramPollResult:
    _required_token(bot_token)
    next_offset = offset
    updates_seen = 0
    accepted = 0
    rejected = 0
    responses_sent = 0
    send_failures = 0

    try:
        payload = http_client.get_updates(bot_token=bot_token, offset=offset, timeout=poll_timeout)
        updates = _telegram_result_list(payload)
    except Exception:
        return TelegramPollResult(
            next_offset=next_offset,
            updates_seen=0,
            accepted=0,
            rejected=0,
            responses_sent=0,
            send_failures=0,
            transport_error="GET_UPDATES_FAILED",
        )

    for update in updates:
        updates_seen += 1
        update_id = _update_id(update)
        if update_id is not None:
            next_offset = update_id + 1

        adapter_result = process_telegram_update(
            update if isinstance(update, Mapping) else {},
            config=telegram_config,
            replay_store=replay_store,
        )
        if not adapter_result.accepted or adapter_result.request is None:
            rejected += 1
            continue

        accepted += 1
        response = process_telegram_request(
            adapter_result.request,
            config=remote_config,
            executor=executor,
            max_output_chars=max_message_chars,
        )
        text = _telegram_safe_text(
            response.formatted,
            max_message_chars=max_message_chars,
            secrets=_config_secrets(bot_token, remote_config),
        )
        try:
            sent = http_client.send_message(
                bot_token=bot_token,
                chat_id=adapter_result.request.chat_id,
                text=text,
            )
            if sent.get("ok") is not True:
                send_failures += 1
            else:
                responses_sent += 1
        except Exception:
            send_failures += 1

    return TelegramPollResult(
        next_offset=next_offset,
        updates_seen=updates_seen,
        accepted=accepted,
        rejected=rejected,
        responses_sent=responses_sent,
        send_failures=send_failures,
    )


def poll_telegram_loop(
    *,
    bot_token: str,
    offset: int | None,
    telegram_config: TelegramRemoteControlConfig,
    remote_config: RemoteControlConfig | Mapping[str, str],
    replay_store: Any,
    executor: Callable[..., RemoteControlResult],
    http_client: TelegramHttpClient,
    iterations: int = 1,
    retry_backoff_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    poll_timeout: int = DEFAULT_POLL_TIMEOUT_SECONDS,
    max_message_chars: int = MAX_TELEGRAM_MESSAGE_CHARS,
) -> list[TelegramPollResult]:
    if iterations < 1:
        raise ValueError("iterations must be positive")
    results: list[TelegramPollResult] = []
    next_offset = offset
    for index in range(iterations):
        result = poll_telegram_once(
            bot_token=bot_token,
            offset=next_offset,
            telegram_config=telegram_config,
            remote_config=remote_config,
            replay_store=replay_store,
            executor=executor,
            http_client=http_client,
            poll_timeout=poll_timeout,
            max_message_chars=max_message_chars,
        )
        results.append(result)
        next_offset = result.next_offset
        if result.transport_error is not None and index + 1 < iterations:
            sleep(retry_backoff_seconds)
    return results


def _telegram_result_list(payload: Mapping[str, Any]) -> Sequence[Any]:
    if payload.get("ok") is not True:
        raise ValueError("TELEGRAM_API_NOT_OK")
    result = payload.get("result")
    if not isinstance(result, list):
        raise ValueError("TELEGRAM_RESULT_NOT_LIST")
    return result


def _update_id(update: Any) -> int | None:
    if not isinstance(update, Mapping):
        return None
    value = update.get("update_id")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _required_token(bot_token: str) -> str:
    if not isinstance(bot_token, str) or not bot_token.strip():
        raise ValueError("telegram bot token is required")
    return bot_token.strip()


def _telegram_safe_text(
    text: str,
    *,
    max_message_chars: int,
    secrets: Sequence[str],
) -> str:
    safe = text if isinstance(text, str) else ""
    safe = safe.replace("\r", " ").replace("\n", " ")
    for secret in secrets:
        if secret:
            safe = safe.replace(secret, "[redacted]")
    return safe[:max_message_chars]


def _config_secrets(bot_token: str, remote_config: RemoteControlConfig | Mapping[str, str]) -> tuple[str, ...]:
    values: list[str] = [_required_token(bot_token)]
    if isinstance(remote_config, RemoteControlConfig):
        values.extend((remote_config.db, remote_config.cid, remote_config.run))
    elif isinstance(remote_config, Mapping):
        values.extend(
            str(remote_config.get(key) or "")
            for key in (
                "remote_control_db_path",
                "remote_control_campaign_id",
                "remote_control_run_id",
            )
        )
    return tuple(value for value in values if value)
