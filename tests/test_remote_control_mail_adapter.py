from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

from alphaforge.remote_control.audit import SQLiteReplayStore
from alphaforge.remote_control.commands import RemoteControlConfig, RemoteControlResult
from alphaforge.remote_control.controller import process_envelope
from alphaforge.remote_control.freshness import FUTURE_CLOCK_TOLERANCE, MAX_COMMAND_AGE
from alphaforge.remote_control.mail_adapter import email_message_to_envelope


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
CONFIG = RemoteControlConfig(
    db="/trusted/control.db",
    cid="CID-123",
    run="RUN-456",
    authorized_sender="sender@example.com",
)


def fixed_clock() -> datetime:
    return NOW


def make_message(
    *,
    message_id: str = "<rfc-1>",
    sender: str = "sender@example.com",
    date: datetime = NOW,
    body: str = "AF STATUS",
) -> EmailMessage:
    message = EmailMessage()
    message["Message-ID"] = message_id
    message["From"] = sender
    message["Date"] = format_datetime(date)
    message["Subject"] = "remote control"
    message.set_content(body)
    return message


class SpyExecutor:
    def __init__(self) -> None:
        self.commands = []

    def __call__(self, command, *, config, timeout, max_output_chars):
        self.commands.append(command)
        return RemoteControlResult(command=command.name, returncode=0, stdout="ok", stderr="")


class RemoteControlMailAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteReplayStore(Path(self.tmp.name) / "controller-replay.sqlite3")
        self.addCleanup(self.store.close)
        self.executor = SpyExecutor()

    def process(self, message: EmailMessage):
        return process_envelope(
            email_message_to_envelope(message),
            config=CONFIG,
            allowed_senders=("sender@example.com",),
            replay_store=self.store,
            executor=self.executor,
            now=fixed_clock,
        )

    def test_valid_plain_text_mail(self):
        envelope = email_message_to_envelope(make_message())
        self.assertEqual(envelope.message_id, "<rfc-1>")
        self.assertEqual(envelope.sender, "sender@example.com")
        self.assertEqual(envelope.body, "AF STATUS")
        self.assertEqual(envelope.received_at, NOW)

    def test_valid_multipart_mail_ignores_attachment(self):
        message = make_message(message_id="<multipart>")
        message.add_attachment(b"AF HEALTH", maintype="text", subtype="plain", filename="command.txt")
        envelope = email_message_to_envelope(message)
        self.assertEqual(envelope.body, "AF STATUS")

    def test_encoded_subject_supported(self):
        message = make_message(message_id="<subject>")
        del message["Subject"]
        message["Subject"] = Header("Uzaktan kontrol", "utf-8").encode()
        self.assertEqual(email_message_to_envelope(message).subject, "Uzaktan kontrol")

    def test_missing_or_blank_message_id_fails_closed(self):
        for value in (None, "", "   "):
            message = make_message()
            del message["Message-ID"]
            if value is not None:
                message["Message-ID"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                email_message_to_envelope(message)

    def test_missing_from_fails_closed(self):
        message = make_message()
        del message["From"]
        with self.assertRaises(ValueError):
            email_message_to_envelope(message)

    def test_malformed_from_fails_closed_downstream(self):
        message = make_message(sender="not-an-email")
        with self.assertRaises(ValueError):
            self.process(message)
        self.assertEqual(self.executor.commands, [])

    def test_missing_or_malformed_date_fails_closed(self):
        for value in (None, "not-a-date", "Thu, 01 Jan 2026 12:00:00"):
            message = make_message()
            del message["Date"]
            if value is not None:
                message["Date"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                email_message_to_envelope(message)

    def test_missing_usable_body_fails_closed(self):
        message = make_message()
        message.clear_content()
        with self.assertRaises(ValueError):
            email_message_to_envelope(message)

    def test_attachment_is_not_used_as_command(self):
        message = EmailMessage()
        message["Message-ID"] = "<attachment-only>"
        message["From"] = "sender@example.com"
        message["Date"] = format_datetime(NOW)
        message.add_attachment(b"AF STATUS", maintype="text", subtype="plain", filename="command.txt")
        with self.assertRaises(ValueError):
            email_message_to_envelope(message)

    def test_html_only_fails_closed(self):
        message = make_message()
        message.clear_content()
        message.set_content("<p>AF STATUS</p>", subtype="html")
        with self.assertRaises(ValueError):
            email_message_to_envelope(message)

    def test_full_offline_pipeline_and_duplicate_replay(self):
        message = make_message(message_id="<pipeline>")
        result = self.process(message)
        self.assertEqual(result.command, "STATUS")
        with self.assertRaises(ValueError):
            self.process(message)
        self.assertEqual(len(self.executor.commands), 1)

    def test_unauthorized_rfc_mail_blocked(self):
        with self.assertRaises(ValueError):
            self.process(make_message(message_id="<unauthorized>", sender="other@example.com"))
        self.assertEqual(self.executor.commands, [])

    def test_expired_rfc_mail_blocked(self):
        with self.assertRaises(ValueError):
            self.process(make_message(message_id="<expired>", date=NOW - MAX_COMMAND_AGE - timedelta(seconds=1)))
        self.assertEqual(self.executor.commands, [])

    def test_future_rfc_mail_blocked(self):
        with self.assertRaises(ValueError):
            self.process(
                make_message(
                    message_id="<future>",
                    date=NOW + FUTURE_CLOCK_TOLERANCE + timedelta(seconds=1),
                )
            )
        self.assertEqual(self.executor.commands, [])


if __name__ == "__main__":
    unittest.main()
