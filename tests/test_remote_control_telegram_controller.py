from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alphaforge.remote_control.audit import SQLiteReplayStore
from alphaforge.remote_control.commands import RemoteControlCommand, RemoteControlResult
from alphaforge.remote_control.telegram_adapter import ALLOWED_TELEGRAM_COMMANDS, TelegramRemoteControlConfig, process_telegram_update
from alphaforge.remote_control.telegram_controller import build_telegram_help_text, process_telegram_request

from test_remote_control_telegram_adapter import make_update


class FakeReadOnlyExecutor:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.commands: list[RemoteControlCommand] = []

    def __call__(self, command: RemoteControlCommand) -> RemoteControlResult:
        self.commands.append(command)
        if self.fail:
            raise RuntimeError("/private/tmp/secret/POSTM0FIX.db traceback")
        return RemoteControlResult(command=command.name, returncode=0, stdout=f"{command.name.lower()} ok", stderr="")


class RemoteControlTelegramControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteReplayStore(Path(self.tmp.name) / "telegram-controller.sqlite3")
        self.addCleanup(self.store.close)
        self.config = TelegramRemoteControlConfig.from_allowlists(
            allowed_user_ids=(42,),
            allowed_chat_ids=(9001,),
        )

    def request_for(self, text: str, *, update_id: int = 1001):
        adapter_result = process_telegram_update(
            make_update(update_id=update_id, text=text),
            config=self.config,
            replay_store=self.store,
        )
        self.assertTrue(adapter_result.accepted)
        self.assertIsNotNone(adapter_result.request)
        return adapter_result.request

    def test_help_returns_allowed_command_list(self):
        executor = FakeReadOnlyExecutor()
        result = process_telegram_request(self.request_for("/help"), executor=executor)

        self.assertEqual(result.command, "HELP")
        self.assertEqual(result.returncode, 0)
        for command in ALLOWED_TELEGRAM_COMMANDS:
            self.assertIn(command, result.stdout)
        self.assertEqual(result.stdout, build_telegram_help_text())

    def test_help_invokes_no_executor(self):
        executor = FakeReadOnlyExecutor()
        process_telegram_request(self.request_for("/help"), executor=executor)

        self.assertEqual(executor.commands, [])

    def test_status_calls_fake_executor_exactly_once_with_normalized_operation(self):
        executor = FakeReadOnlyExecutor()
        result = process_telegram_request(self.request_for("/status"), executor=executor)

        self.assertEqual(result.command, "STATUS")
        self.assertEqual(result.stdout, "status ok")
        self.assertEqual(executor.commands, [RemoteControlCommand(name="STATUS", argv=("STATUS",))])

    def test_health_calls_fake_executor_exactly_once_with_normalized_operation(self):
        executor = FakeReadOnlyExecutor()
        result = process_telegram_request(self.request_for("/health"), executor=executor)

        self.assertEqual(result.command, "HEALTH")
        self.assertEqual(result.stdout, "health ok")
        self.assertEqual(executor.commands, [RemoteControlCommand(name="HEALTH", argv=("HEALTH",))])

    def test_unsupported_command_never_reaches_executor(self):
        executor = FakeReadOnlyExecutor()
        command = RemoteControlCommand(name="REPORT", argv=("REPORT",))
        result = process_telegram_request(
            self.request_for("/report", update_id=2001).__class__(
                update_id="2001",
                user_id="42",
                chat_id="9001",
                authorized_identity="telegram:user_id=42;chat_id=9001",
                command=command,
            ),
            executor=executor,
        )

        self.assertEqual(result.command, "UNSUPPORTED")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "UNSUPPORTED_COMMAND")
        self.assertEqual(executor.commands, [])

    def test_executor_failure_fails_closed_safely(self):
        executor = FakeReadOnlyExecutor(fail=True)
        result = process_telegram_request(self.request_for("/status"), executor=executor)

        self.assertEqual(result.command, "STATUS")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "remote control executor failed safely")
        self.assertNotIn("POSTM0FIX", result.formatted)
        self.assertNotIn("/private/tmp", result.formatted)

    def test_no_arbitrary_telegram_arguments_reach_executor(self):
        executor = FakeReadOnlyExecutor()
        adapter_result = process_telegram_update(
            make_update(update_id=3001, text="/status --db POSTM0FIX.db"),
            config=self.config,
            replay_store=self.store,
        )

        self.assertFalse(adapter_result.accepted)
        self.assertIsNone(adapter_result.request)
        self.assertEqual(adapter_result.rejection_reason, "UNSUPPORTED_COMMAND")
        self.assertEqual(executor.commands, [])

    def test_rejects_tampered_argv_before_executor(self):
        executor = FakeReadOnlyExecutor()
        request = self.request_for("/status", update_id=4001)
        tampered = request.__class__(
            update_id=request.update_id,
            user_id=request.user_id,
            chat_id=request.chat_id,
            authorized_identity=request.authorized_identity,
            command=RemoteControlCommand(name="STATUS", argv=("STATUS", "--db", "POSTM0FIX.db")),
        )
        result = process_telegram_request(tampered, executor=executor)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "UNSUPPORTED_COMMAND")
        self.assertEqual(executor.commands, [])

    def test_controller_invokes_no_subprocess_or_network(self):
        executor = FakeReadOnlyExecutor()
        with patch("alphaforge.remote_control.commands.subprocess.run") as subprocess_run, patch(
            "urllib.request.urlopen"
        ) as urlopen, patch("socket.create_connection") as create_connection:
            result = process_telegram_request(self.request_for("/health"), executor=executor)

        self.assertEqual(result.command, "HEALTH")
        subprocess_run.assert_not_called()
        urlopen.assert_not_called()
        create_connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
