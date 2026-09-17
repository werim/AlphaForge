from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from alphaforge.remote_control.audit import SQLiteReplayStore
from alphaforge.remote_control.auth import is_sender_allowed
from alphaforge.remote_control.commands import RemoteControlResult
from alphaforge.remote_control.envelope import RemoteControlEmailEnvelope, run_remote_control_envelope
from alphaforge.remote_control.freshness import FUTURE_CLOCK_TOLERANCE, MAX_COMMAND_AGE

from test_remote_control_commands import FakeReplayStore, make_config_file


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
FRESH_AT = NOW.isoformat()
ALLOWED_SENDERS = ("sender@example.com",)


def fixed_clock() -> datetime:
    return NOW


def make_envelope(
    body: str | None = "AF STATUS",
    *,
    message_id: str | None = "<envelope-1>",
    sender: str | None = "sender@example.com",
    received_at: str | datetime | None = FRESH_AT,
    subject: str | None = "remote control",
) -> RemoteControlEmailEnvelope:
    return RemoteControlEmailEnvelope(
        message_id=message_id,
        sender=sender,
        subject=subject,
        body=body,
        received_at=received_at,
    )


class RemoteControlEnvelopeTests(unittest.TestCase):
    def test_valid_synthetic_envelope_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = Mock(return_value=RemoteControlResult(command="STATUS", returncode=0, stdout="ok", stderr=""))
            envelope = make_envelope(
                body="  AF STATUS  ",
                message_id="  <envelope-valid>  ",
                sender=" sender@example.com ",
            )
            with patch("alphaforge.remote_control.commands.subprocess.run") as subprocess_run:
                result = run_remote_control_envelope(
                    envelope,
                    config_path=make_config_file(tmp),
                    executor=executor,
                    replay_store=FakeReplayStore(),
                    allowed_senders=ALLOWED_SENDERS,
                    clock=fixed_clock,
                )
            subprocess_run.assert_not_called()
        self.assertEqual(result.command, "STATUS")
        executor.assert_called_once()

    def test_duplicate_message_id_uses_existing_persistent_replay_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = make_config_file(tmp)
            replay_db = Path(tmp) / "controller-envelope-replay.sqlite3"
            executor = Mock(return_value=RemoteControlResult(command="STATUS", returncode=0, stdout="ok", stderr=""))
            envelope = make_envelope(message_id="<envelope-duplicate>")
            with SQLiteReplayStore(replay_db) as store:
                run_remote_control_envelope(
                    envelope,
                    config_path=config_path,
                    executor=executor,
                    replay_store=store,
                    allowed_senders=ALLOWED_SENDERS,
                    clock=fixed_clock,
                )
                with self.assertRaises(ValueError):
                    run_remote_control_envelope(
                        envelope,
                        config_path=config_path,
                        executor=executor,
                        replay_store=store,
                        allowed_senders=ALLOWED_SENDERS,
                        clock=fixed_clock,
                    )
        executor.assert_called_once()

    def test_expired_envelope_is_rejected_by_existing_freshness_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = Mock()
            store = FakeReplayStore()
            envelope = make_envelope(received_at=NOW - MAX_COMMAND_AGE - timedelta(microseconds=1))
            with patch("alphaforge.remote_control.commands.subprocess.run") as subprocess_run:
                with self.assertRaises(ValueError):
                    run_remote_control_envelope(
                        envelope,
                        config_path=make_config_file(tmp),
                        executor=executor,
                        replay_store=store,
                        allowed_senders=ALLOWED_SENDERS,
                        clock=fixed_clock,
                    )
                subprocess_run.assert_not_called()
        self.assertEqual(store.calls, [])
        executor.assert_not_called()

    def test_future_envelope_is_rejected_by_existing_freshness_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = Mock()
            store = FakeReplayStore()
            envelope = make_envelope(received_at=NOW + FUTURE_CLOCK_TOLERANCE + timedelta(seconds=1))
            with self.assertRaises(ValueError):
                run_remote_control_envelope(
                    envelope,
                    config_path=make_config_file(tmp),
                    executor=executor,
                    replay_store=store,
                    allowed_senders=ALLOWED_SENDERS,
                    clock=fixed_clock,
                )
        self.assertEqual(store.calls, [])
        executor.assert_not_called()

    def test_missing_message_id_fails_closed_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            for message_id in (None, "", "   "):
                executor = Mock()
                store = FakeReplayStore()
                with self.subTest(message_id=message_id), self.assertRaises(ValueError):
                    run_remote_control_envelope(
                        make_envelope(message_id=message_id),
                        config_path=make_config_file(tmp),
                        executor=executor,
                        replay_store=store,
                        allowed_senders=ALLOWED_SENDERS,
                        clock=fixed_clock,
                    )
                self.assertEqual(store.calls, [])
                executor.assert_not_called()

    def test_missing_sender_fails_closed_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            for sender in (None, "", "   "):
                executor = Mock()
                store = FakeReplayStore()
                with self.subTest(sender=sender), self.assertRaises(ValueError):
                    run_remote_control_envelope(
                        make_envelope(sender=sender),
                        config_path=make_config_file(tmp),
                        executor=executor,
                        replay_store=store,
                        allowed_senders=ALLOWED_SENDERS,
                        clock=fixed_clock,
                    )
                self.assertEqual(store.calls, [])
                executor.assert_not_called()

    def test_invalid_received_at_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            for received_at in (None, "not-a-timestamp", datetime(2026, 1, 1, 12, 0)):
                executor = Mock()
                store = FakeReplayStore()
                with self.subTest(received_at=received_at), self.assertRaises(ValueError):
                    run_remote_control_envelope(
                        make_envelope(received_at=received_at),
                        config_path=make_config_file(tmp),
                        executor=executor,
                        replay_store=store,
                        allowed_senders=ALLOWED_SENDERS,
                        clock=fixed_clock,
                    )
                self.assertEqual(store.calls, [])
                executor.assert_not_called()

    def test_malformed_command_envelope_is_rejected_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            for body in (None, "", "AF STATUS NOW", "AF STATUS\nAF HEALTH"):
                executor = Mock()
                with self.subTest(body=body), self.assertRaises(ValueError):
                    run_remote_control_envelope(
                        make_envelope(body=body, message_id=f"<bad-{body!r}>"),
                        config_path=make_config_file(tmp),
                        executor=executor,
                        replay_store=FakeReplayStore(),
                        allowed_senders=ALLOWED_SENDERS,
                        clock=fixed_clock,
                    )
                executor.assert_not_called()

    def test_authorized_sender_case_normalized(self):
        self.assertTrue(is_sender_allowed("SENDER@EXAMPLE.COM", ALLOWED_SENDERS))

    def test_display_name_sender_accepted(self):
        self.assertTrue(is_sender_allowed("Example User sender@example.com", ALLOWED_SENDERS))
        self.assertTrue(is_sender_allowed("Example User <sender@example.com>", ALLOWED_SENDERS))

    def test_unauthorized_sender_fails_closed_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = Mock()
            store = FakeReplayStore()
            with self.assertRaises(ValueError):
                run_remote_control_envelope(
                    make_envelope(sender="other@example.com"),
                    config_path=make_config_file(tmp),
                    executor=executor,
                    replay_store=store,
                    allowed_senders=ALLOWED_SENDERS,
                    clock=fixed_clock,
                )
        self.assertEqual(store.calls, [])
        executor.assert_not_called()

    def test_empty_allowlist_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = Mock()
            store = FakeReplayStore()
            with self.assertRaises(ValueError):
                run_remote_control_envelope(
                    make_envelope(),
                    config_path=make_config_file(tmp),
                    executor=executor,
                    replay_store=store,
                    allowed_senders=(),
                    clock=fixed_clock,
                )
        self.assertEqual(store.calls, [])
        executor.assert_not_called()

    def test_malformed_sender_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            for sender in ("not-an-email", "sender@", "@example.com", "sender example.com"):
                executor = Mock()
                store = FakeReplayStore()
                with self.subTest(sender=sender), self.assertRaises(ValueError):
                    run_remote_control_envelope(
                        make_envelope(sender=sender),
                        config_path=make_config_file(tmp),
                        executor=executor,
                        replay_store=store,
                        allowed_senders=ALLOWED_SENDERS,
                        clock=fixed_clock,
                    )
                self.assertEqual(store.calls, [])
                executor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
