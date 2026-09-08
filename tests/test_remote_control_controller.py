from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from alphaforge.remote_control.audit import SQLiteReplayStore
from alphaforge.remote_control.commands import RemoteControlConfig, RemoteControlResult
from alphaforge.remote_control.controller import process_envelope
from alphaforge.remote_control.envelope import RemoteControlEmailEnvelope
from alphaforge.remote_control.freshness import FUTURE_CLOCK_TOLERANCE, MAX_COMMAND_AGE


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
FRESH_AT = NOW.isoformat()
ALLOWED_SENDERS = ("sender@example.com",)
CONFIG = RemoteControlConfig(
    db="/trusted/control.db",
    cid="CID-123",
    run="RUN-456",
    authorized_sender="sender@example.com",
)


def fixed_clock() -> datetime:
    return NOW


def make_envelope(
    body: str | None = "AF STATUS",
    *,
    message_id: str | None = "<controller-1>",
    sender: str | None = "sender@example.com",
    received_at: str | datetime | None = FRESH_AT,
) -> RemoteControlEmailEnvelope:
    return RemoteControlEmailEnvelope(
        message_id=message_id,
        sender=sender,
        subject="remote control",
        body=body,
        received_at=received_at,
    )


class SpyExecutor:
    def __init__(self) -> None:
        self.commands = []

    def __call__(self, command, *, config, timeout, max_output_chars):
        self.commands.append(command)
        return RemoteControlResult(command=command.name, returncode=0, stdout="ok", stderr="")


class RemoteControlControllerTests(unittest.TestCase):
    def run_with_store(self, envelope, *, allowed_senders=ALLOWED_SENDERS):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SQLiteReplayStore(Path(tmp.name) / "controller-replay.sqlite3")
        self.addCleanup(store.close)
        executor = SpyExecutor()
        result = process_envelope(
            envelope,
            config=CONFIG,
            allowed_senders=allowed_senders,
            replay_store=store,
            executor=executor,
            now=fixed_clock,
        )
        return result, executor, store

    def test_valid_pipeline_executes_once(self):
        result, executor, _ = self.run_with_store(make_envelope(body="AF STATUS"))
        self.assertEqual(result.command, "STATUS")
        self.assertEqual(len(executor.commands), 1)

    def test_duplicate_blocked_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteReplayStore(Path(tmp) / "controller-replay.sqlite3")
            self.addCleanup(store.close)
            executor = SpyExecutor()
            envelope = make_envelope(message_id="<controller-duplicate>")
            process_envelope(
                envelope,
                config=CONFIG,
                allowed_senders=ALLOWED_SENDERS,
                replay_store=store,
                executor=executor,
                now=fixed_clock,
            )
            with self.assertRaises(ValueError):
                process_envelope(
                    envelope,
                    config=CONFIG,
                    allowed_senders=ALLOWED_SENDERS,
                    replay_store=store,
                    executor=executor,
                    now=fixed_clock,
                )
        self.assertEqual(len(executor.commands), 1)

    def test_unauthorized_blocked_before_execution(self):
        _, executor, _ = self.assert_rejected_before_execution(make_envelope(sender="other@example.com"))
        self.assertEqual(executor.commands, [])

    def test_expired_blocked_before_execution(self):
        envelope = make_envelope(received_at=NOW - MAX_COMMAND_AGE - timedelta(microseconds=1))
        _, executor, _ = self.assert_rejected_before_execution(envelope)
        self.assertEqual(executor.commands, [])

    def test_future_blocked_before_execution(self):
        envelope = make_envelope(received_at=NOW + FUTURE_CLOCK_TOLERANCE + timedelta(seconds=1))
        _, executor, _ = self.assert_rejected_before_execution(envelope)
        self.assertEqual(executor.commands, [])

    def test_malformed_command_blocked_before_execution(self):
        for body in ("AF STATUS NOW", "AF STATUS\nAF HEALTH", "", None):
            with self.subTest(body=body):
                _, executor, _ = self.assert_rejected_before_execution(make_envelope(body=body))
                self.assertEqual(executor.commands, [])

    def test_empty_allowlist_blocked(self):
        _, executor, _ = self.assert_rejected_before_execution(make_envelope(), allowed_senders=())
        self.assertEqual(executor.commands, [])

    def test_unauthorized_does_not_poison_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteReplayStore(Path(tmp) / "controller-replay.sqlite3")
            self.addCleanup(store.close)
            executor = SpyExecutor()
            message_id = "<controller-not-poisoned>"
            with self.assertRaises(ValueError):
                process_envelope(
                    make_envelope(message_id=message_id, sender="other@example.com"),
                    config=CONFIG,
                    allowed_senders=ALLOWED_SENDERS,
                    replay_store=store,
                    executor=executor,
                    now=fixed_clock,
                )
            result = process_envelope(
                make_envelope(message_id=message_id),
                config=CONFIG,
                allowed_senders=ALLOWED_SENDERS,
                replay_store=store,
                executor=executor,
                now=fixed_clock,
            )
        self.assertEqual(result.command, "STATUS")
        self.assertEqual(len(executor.commands), 1)

    def test_malformed_does_not_poison_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteReplayStore(Path(tmp) / "controller-replay.sqlite3")
            self.addCleanup(store.close)
            executor = SpyExecutor()
            message_id = "<controller-malformed-not-poisoned>"
            with self.assertRaises(ValueError):
                process_envelope(
                    make_envelope(body="AF STATUS NOW", message_id=message_id),
                    config=CONFIG,
                    allowed_senders=ALLOWED_SENDERS,
                    replay_store=store,
                    executor=executor,
                    now=fixed_clock,
                )
            result = process_envelope(
                make_envelope(body="AF HEALTH", message_id=message_id),
                config=CONFIG,
                allowed_senders=ALLOWED_SENDERS,
                replay_store=store,
                executor=executor,
                now=fixed_clock,
            )
        self.assertEqual(result.command, "HEALTH")
        self.assertEqual(len(executor.commands), 1)

    def test_replay_and_freshness_still_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteReplayStore(Path(tmp) / "controller-replay.sqlite3")
            self.addCleanup(store.close)
            executor = SpyExecutor()
            process_envelope(
                make_envelope(message_id="<controller-fresh>"),
                config=CONFIG,
                allowed_senders=ALLOWED_SENDERS,
                replay_store=store,
                executor=executor,
                now=fixed_clock,
            )
            with self.assertRaises(ValueError):
                process_envelope(
                    make_envelope(message_id="<controller-fresh>"),
                    config=CONFIG,
                    allowed_senders=ALLOWED_SENDERS,
                    replay_store=store,
                    executor=executor,
                    now=fixed_clock,
                )
            with self.assertRaises(ValueError):
                process_envelope(
                    make_envelope(
                        message_id="<controller-expired>",
                        received_at=NOW - MAX_COMMAND_AGE - timedelta(microseconds=1),
                    ),
                    config=CONFIG,
                    allowed_senders=ALLOWED_SENDERS,
                    replay_store=store,
                    executor=executor,
                    now=fixed_clock,
                )
        self.assertEqual(len(executor.commands), 1)

    def test_no_real_subprocess_path_is_invoked(self):
        with patch("alphaforge.remote_control.commands.subprocess.run") as subprocess_run:
            self.run_with_store(make_envelope(message_id="<controller-no-subprocess>"))
            subprocess_run.assert_not_called()

    def assert_rejected_before_execution(self, envelope, *, allowed_senders=ALLOWED_SENDERS):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SQLiteReplayStore(Path(tmp.name) / "controller-replay.sqlite3")
        self.addCleanup(store.close)
        executor = SpyExecutor()
        with self.assertRaises(ValueError):
            process_envelope(
                envelope,
                config=CONFIG,
                allowed_senders=allowed_senders,
                replay_store=store,
                executor=executor,
                now=fixed_clock,
            )
        return None, executor, store


if __name__ == "__main__":
    unittest.main()
