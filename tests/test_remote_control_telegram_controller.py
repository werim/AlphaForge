from __future__ import annotations

from dataclasses import replace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alphaforge.remote_control.audit import SQLiteReplayStore
from alphaforge.remote_control.commands import RemoteControlCommand, RemoteControlConfig, RemoteControlResult
from alphaforge.remote_control.telegram_adapter import (
    TelegramRemoteControlConfig,
    TelegramRemoteControlRequest,
    process_telegram_update,
)
from alphaforge.remote_control.telegram_controller import (
    EXECUTABLE_TELEGRAM_COMMANDS,
    build_telegram_help_text,
    process_telegram_request,
)

from test_remote_control_telegram_adapter import make_update


TRUSTED_CONFIG = RemoteControlConfig(
    db="/trusted/control.db",
    cid="CID-123",
    run="RUN-456",
    authorized_sender="sender@example.com",
)
TRUSTED_CONFIG_MAPPING = {
    "remote_control_db_path": "/trusted/control.db",
    "remote_control_campaign_id": "CID-123",
    "remote_control_run_id": "RUN-456",
}
STATUS_ARGV = (
    "burnin_ops",
    "--db",
    "/trusted/control.db",
    "status",
    "--campaign-id",
    "CID-123",
    "--run-id",
    "RUN-456",
)
HEALTH_ARGV = (
    "burnin_ops",
    "--db",
    "/trusted/control.db",
    "health",
    "--campaign-id",
    "CID-123",
    "--run-id",
    "RUN-456",
)


class FakeReadOnlyExecutor:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.commands: list[RemoteControlCommand] = []
        self.kwargs: list[dict] = []

    def __call__(self, command: RemoteControlCommand, **kwargs) -> RemoteControlResult:
        self.commands.append(command)
        self.kwargs.append(kwargs)
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
        for command in EXECUTABLE_TELEGRAM_COMMANDS:
            self.assertIn(command, result.stdout)
        for planned_command in ("/report", "/rejects", "/labels", "/errors"):
            self.assertNotIn(planned_command, result.stdout)
        self.assertEqual(result.stdout, build_telegram_help_text())

    def test_help_invokes_no_executor(self):
        executor = FakeReadOnlyExecutor()
        process_telegram_request(self.request_for("/help"), executor=executor)

        self.assertEqual(executor.commands, [])

    def test_status_calls_fake_executor_exactly_once_with_normalized_operation(self):
        executor = FakeReadOnlyExecutor()
        result = process_telegram_request(self.request_for("/status"), config=TRUSTED_CONFIG, executor=executor)

        self.assertEqual(result.command, "STATUS")
        self.assertEqual(result.stdout, "status ok")
        self.assertEqual(executor.commands, [RemoteControlCommand(name="STATUS", argv=STATUS_ARGV)])
        self.assertEqual(executor.kwargs[0]["config"], TRUSTED_CONFIG_MAPPING)
        self.assertEqual(executor.kwargs[0]["timeout"], 5.0)

    def test_health_calls_fake_executor_exactly_once_with_normalized_operation(self):
        executor = FakeReadOnlyExecutor()
        result = process_telegram_request(self.request_for("/health"), config=TRUSTED_CONFIG_MAPPING, executor=executor)

        self.assertEqual(result.command, "HEALTH")
        self.assertEqual(result.stdout, "health ok")
        self.assertEqual(executor.commands, [RemoteControlCommand(name="HEALTH", argv=HEALTH_ARGV)])
        self.assertEqual(executor.kwargs[0]["config"], TRUSTED_CONFIG_MAPPING)

    def test_telegram_request_data_cannot_alter_trusted_runtime_arguments(self):
        executor = FakeReadOnlyExecutor()
        request = self.request_for("/status", update_id=1002)
        tampered = replace(
            request,
            update_id="--db",
            user_id="POSTM0FIX",
            chat_id="camp-attacker",
            authorized_identity="telegram:user_id=POSTM0FIX;chat_id=camp-attacker",
        )
        result = process_telegram_request(tampered, config=TRUSTED_CONFIG, executor=executor)

        self.assertEqual(result.command, "STATUS")
        self.assertEqual(executor.commands, [RemoteControlCommand(name="STATUS", argv=STATUS_ARGV)])
        self.assertNotIn("POSTM0FIX", repr(executor.commands))
        self.assertNotIn("camp-attacker", repr(executor.commands))

    def test_directly_constructed_unverified_request_cannot_reach_executor(self):
        executor = FakeReadOnlyExecutor()
        request = TelegramRemoteControlRequest(
            update_id="9999",
            user_id="42",
            chat_id="9001",
            authorized_identity="telegram:user_id=42;chat_id=9001",
            command=RemoteControlCommand(name="STATUS", argv=("STATUS",)),
        )
        result = process_telegram_request(request, config=TRUSTED_CONFIG, executor=executor)  # type: ignore[arg-type]

        self.assertEqual(result.command, "UNSUPPORTED")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "invalid remote control request")
        self.assertEqual(executor.commands, [])

    def test_unsupported_command_never_reaches_executor(self):
        executor = FakeReadOnlyExecutor()
        result = process_telegram_request(self.request_for("/report", update_id=2001), config=TRUSTED_CONFIG, executor=executor)

        self.assertEqual(result.command, "UNSUPPORTED")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "UNSUPPORTED_COMMAND")
        self.assertEqual(executor.commands, [])

    def test_executor_failure_fails_closed_safely(self):
        executor = FakeReadOnlyExecutor(fail=True)
        result = process_telegram_request(self.request_for("/status"), config=TRUSTED_CONFIG, executor=executor)

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

    def test_duplicate_update_id_cannot_reach_executor(self):
        executor = FakeReadOnlyExecutor()
        accepted = process_telegram_update(
            make_update(update_id=3501, text="/status"),
            config=self.config,
            replay_store=self.store,
        )
        duplicate = process_telegram_update(
            make_update(update_id=3501, text="/health"),
            config=self.config,
            replay_store=self.store,
        )

        self.assertTrue(accepted.accepted)
        self.assertFalse(duplicate.accepted)
        self.assertIsNone(duplicate.request)
        self.assertEqual(duplicate.rejection_reason, "DUPLICATE_UPDATE_ID")
        self.assertEqual(executor.commands, [])

    def test_unauthorized_user_and_chat_cannot_reach_executor(self):
        executor = FakeReadOnlyExecutor()
        unauthorized_user = process_telegram_update(
            make_update(update_id=3601, user_id=7, text="/status"),
            config=self.config,
            replay_store=self.store,
        )
        unauthorized_chat = process_telegram_update(
            make_update(update_id=3602, chat_id=123, text="/health"),
            config=self.config,
            replay_store=self.store,
        )

        self.assertFalse(unauthorized_user.accepted)
        self.assertFalse(unauthorized_chat.accepted)
        self.assertIsNone(unauthorized_user.request)
        self.assertIsNone(unauthorized_chat.request)
        self.assertEqual(executor.commands, [])

    def test_rejects_tampered_argv_before_executor(self):
        executor = FakeReadOnlyExecutor()
        request = self.request_for("/status", update_id=4001)
        tampered = replace(request, command=RemoteControlCommand(name="STATUS", argv=("STATUS", "--db", "POSTM0FIX.db")))
        result = process_telegram_request(tampered, config=TRUSTED_CONFIG, executor=executor)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "UNSUPPORTED_COMMAND")
        self.assertEqual(executor.commands, [])

    def test_missing_trusted_config_fails_before_executor(self):
        invalid_configs = (
            None,
            {},
            {"remote_control_db_path": "/trusted/control.db"},
            {
                "remote_control_db_path": "",
                "remote_control_campaign_id": "CID-123",
                "remote_control_run_id": "RUN-456",
            },
        )
        for index, config in enumerate(invalid_configs, start=1):
            with self.subTest(index=index):
                executor = FakeReadOnlyExecutor()
                result = process_telegram_request(
                    self.request_for("/status", update_id=4500 + index),
                    config=config,  # type: ignore[arg-type]
                    executor=executor,
                )
                self.assertEqual(result.command, "STATUS")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, "remote control executor failed safely")
                self.assertEqual(executor.commands, [])

    def test_controller_invokes_no_subprocess_or_network(self):
        executor = FakeReadOnlyExecutor()
        with patch("alphaforge.remote_control.commands.subprocess.run") as subprocess_run, patch(
            "urllib.request.urlopen"
        ) as urlopen, patch("socket.create_connection") as create_connection:
            result = process_telegram_request(self.request_for("/health"), config=TRUSTED_CONFIG, executor=executor)

        self.assertEqual(result.command, "HEALTH")
        subprocess_run.assert_not_called()
        urlopen.assert_not_called()
        create_connection.assert_not_called()

    def test_shell_true_is_never_used_by_controller(self):
        executor = FakeReadOnlyExecutor()
        result = process_telegram_request(self.request_for("/status", update_id=5001), config=TRUSTED_CONFIG, executor=executor)

        self.assertEqual(result.command, "STATUS")
        self.assertNotIn("shell", executor.kwargs[0])


if __name__ == "__main__":
    unittest.main()
