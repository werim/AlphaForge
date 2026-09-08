from __future__ import annotations

from datetime import datetime
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime

from alphaforge.remote_control.envelope import RemoteControlEmailEnvelope


def email_message_to_envelope(message: Message) -> RemoteControlEmailEnvelope:
    if not isinstance(message, Message):
        raise ValueError("RFC email message is required")

    message_id = _required_header(message, "Message-ID")
    sender = _required_header(message, "From")
    received_at = _parse_date(_required_header(message, "Date"))
    subject = _decode_header(message.get("Subject"))
    body = _plain_text_body(message)

    return RemoteControlEmailEnvelope(
        message_id=message_id,
        sender=sender,
        subject=subject,
        body=body,
        received_at=received_at,
    )


def _required_header(message: Message, name: str) -> str:
    value = message.get(name)
    if value is None or not str(value).strip():
        raise ValueError(f"RFC email {name} header is required")
    return str(value).strip()


def _decode_header(value: object | None) -> str | None:
    if value is None:
        return None
    try:
        return str(make_header(decode_header(str(value)))).strip()
    except (LookupError, UnicodeError) as exc:
        raise ValueError("RFC email Subject header is invalid") from exc


def _parse_date(value: str) -> datetime:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("RFC email Date header is invalid") from exc
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("RFC email Date header must include a timezone")
    return parsed


def _plain_text_body(message: Message) -> str:
    for part in message.walk():
        if part.is_multipart() or part.get_content_type() != "text/plain":
            continue
        if part.get_content_disposition() == "attachment":
            continue
        body = _decode_part(part).strip()
        if body:
            return body
    raise ValueError("RFC email has no usable text/plain command body")


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw_payload = part.get_payload()
        if not isinstance(raw_payload, str):
            raise ValueError("RFC email text/plain body is invalid")
        return raw_payload
    try:
        return payload.decode(part.get_content_charset() or "utf-8")
    except (LookupError, UnicodeError) as exc:
        raise ValueError("RFC email text/plain body is invalid") from exc
