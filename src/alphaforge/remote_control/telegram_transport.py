from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from alphaforge.remote_control.commands import RemoteControlConfig, RemoteControlResult
from alphaforge.remote_control.telegram_adapter import TelegramRemoteControlConfig, process_telegram_update
from alphaforge.remote_control.telegram_controller import process_telegram_request
from alphaforge.remote_control.telegram_state import (
    SQLiteTelegramTransportStateStore,
    StoredTelegramResponseDelivery,
)


DEFAULT_POLL_TIMEOUT_SECONDS = 10
DEFAULT_HTTP_TIMEOUT_SECONDS = 15
MAX_TELEGRAM_MESSAGE_CHARS = 3900
_DELIVERY_CAPABILITY = object()


class TelegramHttpClient(Protocol):
    def get_updates(self, *, bot_token: str, offset: int | None, timeout: int) -> Mapping[str, Any]: ...

    def send_message(self, *, bot_token: str, chat_id: str, text: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class TelegramResponseDelivery:
    update_id: str
    chat_id: str
    text: str
    command_acknowledged: bool
    status: str
    failure_reason: str | None = None
    _capability: object = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class TelegramPollResult:
    next_offset: int | None
    updates_seen: int
    accepted: int
    rejected: int
    responses_sent: int
    send_failures: int
    command_acknowledged: int = 0
    pending_deliveries: tuple[TelegramResponseDelivery, ...] = ()
    processing_failures: int = 0
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
    state_store: SQLiteTelegramTransportStateStore | None = None,
) -> TelegramPollResult:
    _required_token(bot_token)
    try:
        next_offset = _initialize_offset(offset, state_store)
    except Exception:
        return _empty_poll_result(offset, "STATE_LOAD_FAILED")
    updates_seen = 0
    accepted = 0
    rejected = 0
    responses_sent = 0
    send_failures = 0
    command_acknowledged = 0
    pending_deliveries: list[TelegramResponseDelivery] = []
    processing_failures = 0

    try:
        payload = http_client.get_updates(bot_token=bot_token, offset=next_offset, timeout=poll_timeout)
        updates = _telegram_result_list(payload)
    except Exception:
        return _empty_poll_result(next_offset, "GET_UPDATES_FAILED")

    for update in updates:
        updates_seen += 1
        update_id = _update_id(update)
        try:
            adapter_result = process_telegram_update(
                update if isinstance(update, Mapping) else {},
                config=telegram_config,
                replay_store=replay_store,
            )
        except Exception:
            rejected += 1
            processing_failures += 1
            try:
                next_offset = _persist_terminal_offset(next_offset, update_id, state_store)
            except Exception:
                return _poll_result(
                    next_offset=next_offset,
                    updates_seen=updates_seen,
                    accepted=accepted,
                    rejected=rejected,
                    responses_sent=responses_sent,
                    send_failures=send_failures,
                    command_acknowledged=command_acknowledged,
                    pending_deliveries=pending_deliveries,
                    processing_failures=processing_failures,
                    transport_error="STATE_PERSIST_FAILED",
                )
            continue
        if not adapter_result.accepted or adapter_result.request is None:
            rejected += 1
            try:
                next_offset = _persist_terminal_offset(next_offset, update_id, state_store)
            except Exception:
                return _poll_result(
                    next_offset=next_offset,
                    updates_seen=updates_seen,
                    accepted=accepted,
                    rejected=rejected,
                    responses_sent=responses_sent,
                    send_failures=send_failures,
                    command_acknowledged=command_acknowledged,
                    pending_deliveries=pending_deliveries,
                    processing_failures=processing_failures,
                    transport_error="STATE_PERSIST_FAILED",
                )
            continue

        accepted += 1
        try:
            response = process_telegram_request(
                adapter_result.request,
                config=remote_config,
                executor=executor,
                max_output_chars=max_message_chars,
            )
            response_text = response.formatted
        except Exception:
            processing_failures += 1
            response_text = "REMOTE_CONTROL: FAIL rc=1 | stderr=request failed safely"
        command_acknowledged += 1
        text = _telegram_safe_text(
            response_text,
            max_message_chars=max_message_chars,
            secrets=_config_secrets(bot_token, remote_config),
        )
        delivery = _mint_telegram_response_delivery(
            update_id=adapter_result.request.update_id,
            chat_id=adapter_result.request.chat_id,
            text=text,
        )
        acknowledged_offset = _acknowledge_update(next_offset, update_id)
        if state_store is not None:
            if acknowledged_offset is None:
                processing_failures += 1
                pending_deliveries.append(delivery)
                return _poll_result(
                    next_offset=next_offset,
                    updates_seen=updates_seen,
                    accepted=accepted,
                    rejected=rejected,
                    responses_sent=responses_sent,
                    send_failures=send_failures,
                    command_acknowledged=command_acknowledged,
                    pending_deliveries=pending_deliveries,
                    processing_failures=processing_failures,
                    transport_error="INVALID_UPDATE_ID",
                )
            try:
                next_offset = state_store.persist_pending_with_offset(
                    update_id=delivery.update_id,
                    chat_id=delivery.chat_id,
                    text=delivery.text,
                    next_offset=acknowledged_offset,
                )
            except Exception:
                processing_failures += 1
                pending_deliveries.append(delivery)
                return _poll_result(
                    next_offset=next_offset,
                    updates_seen=updates_seen,
                    accepted=accepted,
                    rejected=rejected,
                    responses_sent=responses_sent,
                    send_failures=send_failures,
                    command_acknowledged=command_acknowledged,
                    pending_deliveries=pending_deliveries,
                    processing_failures=processing_failures,
                    transport_error="STATE_PERSIST_FAILED",
                )
        delivery = deliver_telegram_response(
            delivery,
            bot_token=bot_token,
            http_client=http_client,
        )
        if delivery.status == "DELIVERED":
            responses_sent += 1
            if state_store is not None:
                try:
                    state_store.mark_delivered(delivery.update_id)
                except Exception:
                    delivery = _delivery_with_failure(delivery, "DELIVERY_ACK_FAILED")
                    pending_deliveries.append(delivery)
        else:
            send_failures += 1
            pending_deliveries.append(delivery)
            if state_store is not None:
                try:
                    state_store.record_delivery_failure(delivery.update_id)
                except Exception:
                    processing_failures += 1
        if state_store is None:
            next_offset = acknowledged_offset

    return _poll_result(
        next_offset=next_offset,
        updates_seen=updates_seen,
        accepted=accepted,
        rejected=rejected,
        responses_sent=responses_sent,
        send_failures=send_failures,
        command_acknowledged=command_acknowledged,
        pending_deliveries=tuple(pending_deliveries),
        processing_failures=processing_failures,
    )


def deliver_telegram_response(
    delivery: TelegramResponseDelivery,
    *,
    bot_token: str,
    http_client: TelegramHttpClient,
) -> TelegramResponseDelivery:
    """Deliver a prepared response without revisiting authorization or execution."""
    _required_token(bot_token)
    if (
        not isinstance(delivery, TelegramResponseDelivery)
        or delivery._capability is not _DELIVERY_CAPABILITY
        or not delivery.command_acknowledged
    ):
        raise ValueError("invalid Telegram response delivery")
    if delivery.status == "DELIVERED":
        return delivery
    if delivery.status != "PENDING":
        raise ValueError("invalid Telegram response delivery status")
    try:
        sent = http_client.send_message(
            bot_token=bot_token,
            chat_id=delivery.chat_id,
            text=delivery.text,
        )
        if sent.get("ok") is not True:
            raise ValueError("TELEGRAM_SEND_FAILED")
    except Exception:
        return TelegramResponseDelivery(
            update_id=delivery.update_id,
            chat_id=delivery.chat_id,
            text=delivery.text,
            command_acknowledged=True,
            status="PENDING",
            failure_reason="SEND_MESSAGE_FAILED",
            _capability=_DELIVERY_CAPABILITY,
        )
    return TelegramResponseDelivery(
        update_id=delivery.update_id,
        chat_id=delivery.chat_id,
        text=delivery.text,
        command_acknowledged=True,
        status="DELIVERED",
        _capability=_DELIVERY_CAPABILITY,
    )


def retry_pending_telegram_responses(
    *,
    bot_token: str,
    http_client: TelegramHttpClient,
    state_store: SQLiteTelegramTransportStateStore,
) -> tuple[TelegramResponseDelivery, ...]:
    """Retry durable responses without access to command-processing dependencies."""
    _required_token(bot_token)
    if not isinstance(state_store, SQLiteTelegramTransportStateStore):
        return ()
    try:
        stored_deliveries = state_store.load_pending()
    except Exception:
        return ()

    results: list[TelegramResponseDelivery] = []
    for stored in stored_deliveries:
        delivery = _restore_telegram_response_delivery(stored)
        attempted = deliver_telegram_response(delivery, bot_token=bot_token, http_client=http_client)
        if attempted.status == "DELIVERED":
            try:
                state_store.mark_delivered(attempted.update_id)
            except Exception:
                attempted = _delivery_with_failure(attempted, "DELIVERY_ACK_FAILED")
        else:
            try:
                state_store.record_delivery_failure(attempted.update_id)
            except Exception:
                pass
        results.append(attempted)
    return tuple(results)


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
    state_store: SQLiteTelegramTransportStateStore | None = None,
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
            state_store=state_store,
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


def _acknowledge_update(offset: int | None, update_id: int | None) -> int | None:
    if update_id is None:
        return offset
    acknowledged_offset = update_id + 1
    if offset is None:
        return acknowledged_offset
    return max(offset, acknowledged_offset)


def _initialize_offset(
    offset: int | None,
    state_store: SQLiteTelegramTransportStateStore | None,
) -> int | None:
    if state_store is None:
        return offset
    if offset is not None:
        state_store.acknowledge_offset(offset)
    return state_store.load_offset()


def _persist_terminal_offset(
    offset: int | None,
    update_id: int | None,
    state_store: SQLiteTelegramTransportStateStore | None,
) -> int | None:
    next_offset = _acknowledge_update(offset, update_id)
    if state_store is not None and next_offset is not None:
        return state_store.acknowledge_offset(next_offset)
    return next_offset


def _mint_telegram_response_delivery(*, update_id: str, chat_id: str, text: str) -> TelegramResponseDelivery:
    return TelegramResponseDelivery(
        update_id=update_id,
        chat_id=chat_id,
        text=text,
        command_acknowledged=True,
        status="PENDING",
        _capability=_DELIVERY_CAPABILITY,
    )


def _restore_telegram_response_delivery(stored: StoredTelegramResponseDelivery) -> TelegramResponseDelivery:
    if not isinstance(stored, StoredTelegramResponseDelivery):
        raise ValueError("invalid persisted Telegram delivery")
    return TelegramResponseDelivery(
        update_id=stored.update_id,
        chat_id=stored.chat_id,
        text=stored.text,
        command_acknowledged=True,
        status="PENDING",
        failure_reason=stored.failure_reason,
        _capability=_DELIVERY_CAPABILITY,
    )


def _delivery_with_failure(delivery: TelegramResponseDelivery, reason: str) -> TelegramResponseDelivery:
    return TelegramResponseDelivery(
        update_id=delivery.update_id,
        chat_id=delivery.chat_id,
        text=delivery.text,
        command_acknowledged=True,
        status="PENDING",
        failure_reason=reason,
        _capability=_DELIVERY_CAPABILITY,
    )


def _empty_poll_result(next_offset: int | None, error: str) -> TelegramPollResult:
    return TelegramPollResult(
        next_offset=next_offset,
        updates_seen=0,
        accepted=0,
        rejected=0,
        responses_sent=0,
        send_failures=0,
        transport_error=error,
    )


def _poll_result(
    *,
    next_offset: int | None,
    updates_seen: int,
    accepted: int,
    rejected: int,
    responses_sent: int,
    send_failures: int,
    command_acknowledged: int,
    pending_deliveries: Sequence[TelegramResponseDelivery],
    processing_failures: int,
    transport_error: str | None = None,
) -> TelegramPollResult:
    return TelegramPollResult(
        next_offset=next_offset,
        updates_seen=updates_seen,
        accepted=accepted,
        rejected=rejected,
        responses_sent=responses_sent,
        send_failures=send_failures,
        command_acknowledged=command_acknowledged,
        pending_deliveries=tuple(pending_deliveries),
        processing_failures=processing_failures,
        transport_error=transport_error,
    )


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
