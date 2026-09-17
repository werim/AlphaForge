from __future__ import annotations

from collections.abc import Collection
from email.utils import parseaddr


def normalize_mailbox(sender: str) -> str:
    if not isinstance(sender, str) or not sender.strip():
        raise ValueError("remote control sender must be a non-empty string")
    _, mailbox = parseaddr(sender.strip())
    if not _is_valid_mailbox(mailbox):
        parts = sender.strip().split()
        if parts:
            _, mailbox = parseaddr(parts[-1])
    if not _is_valid_mailbox(mailbox):
        raise ValueError("remote control sender must contain a valid mailbox")
    return mailbox.lower()


def is_sender_allowed(sender: str, allowed_senders: Collection[str]) -> bool:
    if not allowed_senders:
        return False
    try:
        normalized_sender = normalize_mailbox(sender)
        normalized_allowed = {normalize_mailbox(allowed) for allowed in allowed_senders}
    except ValueError:
        return False
    return normalized_sender in normalized_allowed


def _is_valid_mailbox(mailbox: str) -> bool:
    if not isinstance(mailbox, str):
        return False
    if any(character.isspace() for character in mailbox):
        return False
    if mailbox.count("@") != 1:
        return False
    local, domain = mailbox.split("@", 1)
    return bool(local and domain and "." in domain)
