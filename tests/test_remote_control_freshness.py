from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from alphaforge.remote_control import run_remote_control_message
from alphaforge.remote_control.audit import SQLiteReplayStore
from alphaforge.remote_control.commands import RemoteControlResult
from alphaforge.remote_control.freshness import FUTURE_CLOCK_TOLERANCE, MAX_COMMAND_AGE

from test_remote_control_commands import FakeReplayStore, make_config_file


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def fixed_clock() -> datetime:
    return NOW


class RemoteControlFreshnessTests(unittest.TestCase):
    def run_message(
        self,
        command: str,
        *,
        received_at: str | datetime | None,
        message_id: str = "<msg-fresh>",
        body: str | None = None,
    ):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config_path = make_config_file(tmp.name)
        executor = Mock(
            return_value=RemoteControlResult(command=command.split()[1], returncode=0, stdout="ok", stderr="")
        )
        store = FakeReplayStore()
        result = run_remote_control_message(
            body if body is not None else command,
            config_path=config_path,
            executor=executor,
            replay_store=store,
            sender="sender@example.com",
            message_id=message_id,
            received_at=received_at,
            clock=fixed_clock,
        )
        return result, executor, store

    def test_fresh_status_and_health_dispatch_exactly_once(self):
        for command in ("AF STATUS", "AF HEALTH"):
            with self.subTest(command=command):
                result, executor, store = self.run_message(command, received_at=NOW)
                self.assertEqual(result.command, command.split()[1])
                executor.assert_called_once()
                self.assertEqual(store.calls, ["<msg-fresh>"])

    def test_exact_maximum_age_is_accepted(self):
        result, executor, _ = self.run_message("AF STATUS", received_at=NOW - MAX_COMMAND_AGE)
        self.assertEqual(result.command, "STATUS")
        executor.assert_called_once()

    def test_older_than_maximum_age_is_rejected_before_replay_or_dispatch(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config_path = make_config_file(tmp.name)
        executor = Mock()
        store = FakeReplayStore()
        with patch("alphaforge.remote_control.commands.subprocess.run") as subprocess_run:
            with self.assertRaises(ValueError):
                run_remote_control_message(
                    "AF STATUS",
                    config_path=config_path,
                    executor=executor,
                    replay_store=store,
                    sender="sender@example.com",
                    message_id="<msg-expired>",
                    received_at=NOW - MAX_COMMAND_AGE - timedelta(microseconds=1),
                    clock=fixed_clock,
                )
            subprocess_run.assert_not_called()
        self.assertEqual(store.calls, [])
        executor.assert_not_called()

    def test_future_tolerance_boundary_is_accepted(self):
        result, executor, _ = self.run_message(
            "AF HEALTH",
            received_at=(NOW + FUTURE_CLOCK_TOLERANCE).isoformat(),
        )
        self.assertEqual(result.command, "HEALTH")
        executor.assert_called_once()

    def test_timestamp_too_far_in_future_is_rejected(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config_path = make_config_file(tmp.name)
        executor = Mock()
        store = FakeReplayStore()
        with self.assertRaises(ValueError):
            run_remote_control_message(
                "AF STATUS",
                config_path=config_path,
                executor=executor,
                replay_store=store,
                sender="sender@example.com",
                message_id="<msg-future>",
                received_at=NOW + FUTURE_CLOCK_TOLERANCE + timedelta(seconds=1),
                clock=fixed_clock,
            )
        self.assertEqual(store.calls, [])
        executor.assert_not_called()

    def test_missing_malformed_and_naive_timestamps_are_rejected(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config_path = make_config_file(tmp.name)
        for received_at in (None, "not-a-timestamp", datetime(2026, 1, 1, 12, 0)):
            executor = Mock()
            store = FakeReplayStore()
            with self.subTest(received_at=received_at), self.assertRaises(ValueError):
                run_remote_control_message(
                    "AF STATUS",
                    config_path=config_path,
                    executor=executor,
                    replay_store=store,
                    sender="sender@example.com",
                    message_id="<msg-invalid-time>",
                    received_at=received_at,
                    clock=fixed_clock,
                )
            self.assertEqual(store.calls, [])
            executor.assert_not_called()

    def test_naive_injected_clock_fails_closed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config_path = make_config_file(tmp.name)
        executor = Mock()
        store = FakeReplayStore()
        with self.assertRaises(ValueError):
            run_remote_control_message(
                "AF STATUS",
                config_path=config_path,
                executor=executor,
                replay_store=store,
                sender="sender@example.com",
                message_id="<msg-clock>",
                received_at=NOW,
                clock=lambda: datetime(2026, 1, 1, 12, 0),
            )
        self.assertEqual(store.calls, [])
        executor.assert_not_called()

    def test_clock_is_dependency_injected(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config_path = make_config_file(tmp.name)
        executor = Mock(
            return_value=RemoteControlResult(command="STATUS", returncode=0, stdout="ok", stderr="")
        )
        clock = Mock(return_value=NOW)
        run_remote_control_message(
            "AF STATUS",
            config_path=config_path,
            executor=executor,
            replay_store=FakeReplayStore(),
            sender="sender@example.com",
            message_id="<msg-injected-clock>",
            received_at=NOW,
            clock=clock,
        )
        clock.assert_called_once_with()
        executor.assert_called_once()

    def test_body_cannot_override_timestamp_policy_or_clock(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config_path = make_config_file(tmp.name)
        executor = Mock()
        store = FakeReplayStore()
        body = "AF STATUS received_at=2099-01-01T00:00:00Z max_age=999999 clock=2099"
        with self.assertRaises(ValueError):
            run_remote_control_message(
                body,
                config_path=config_path,
                executor=executor,
                replay_store=store,
                sender="sender@example.com",
                message_id="<msg-body-time>",
                received_at=NOW - MAX_COMMAND_AGE - timedelta(seconds=1),
                clock=fixed_clock,
            )
        self.assertEqual(store.calls, [])
        executor.assert_not_called()

    def test_fresh_duplicate_persists_and_dispatches_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = make_config_file(tmp)
            db_path = Path(tmp) / "controller-replay.sqlite3"
            executor = Mock(
                return_value=RemoteControlResult(command="STATUS", returncode=0, stdout="ok", stderr="")
            )
            with SQLiteReplayStore(db_path) as store, patch(
                "alphaforge.remote_control.commands.subprocess.run"
            ) as subprocess_run:
                run_remote_control_message(
                    "AF STATUS",
                    config_path=config_path,
                    executor=executor,
                    replay_store=store,
                    sender="sender@example.com",
                    message_id="<msg-persistent-fresh>",
                    received_at=NOW,
                    clock=fixed_clock,
                )
                with self.assertRaises(ValueError):
                    run_remote_control_message(
                        "AF STATUS",
                        config_path=config_path,
                        executor=executor,
                        replay_store=store,
                        sender="sender@example.com",
                        message_id="<msg-persistent-fresh>",
                        received_at=NOW,
                        clock=fixed_clock,
                    )
                subprocess_run.assert_not_called()
            executor.assert_called_once()


if __name__ == "__main__":
    unittest.main()
