from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alphaforge.remote_control.audit import SQLiteReplayStore
from alphaforge.remote_control.commands import RemoteControlCommand, RemoteControlConfig, RemoteControlResult
from alphaforge.remote_control.telegram_adapter import TelegramRemoteControlConfig, process_telegram_update
from alphaforge.remote_control.telegram_transport import (
    TelegramResponseDelivery,
    deliver_telegram_response,
    poll_telegram_loop,
    poll_telegram_once,
)

from test_remote_control_telegram_adapter import make_update
from test_remote_control_telegram_controller import HEALTH_ARGV, STATUS_ARGV, TRUSTED_CONFIG


BOT_TOKEN = "123456:SECRET-TELEGRAM-BOT-TOKEN"


class FakeTelegramHttpClient:
    def __init__(self, updates_payloads=None, *, send_ok: bool = True, fail_get: bool = False, fail_send: bool = False):
        self.updates_payloads = list(updates_payloads or [])
        self.send_ok = send_ok
        self.fail_get = fail_get
        self.fail_send = fail_send
        self.get_calls = []
        self.send_calls = []

    def get_updates(self, *, bot_token: str, offset: int | None, timeout: int):
        self.get_calls.append({"bot_token": bot_token, "offset": offset, "timeout": timeout})
        if self.fail_get:
            raise RuntimeError(f"failed with token {bot_token}")
        if self.updates_payloads:
            return self.updates_payloads.pop(0)
        return {"ok": True, "result": []}

    def send_message(self, *, bot_token: str, chat_id: str, text: str):
        self.send_calls.append({"bot_token": bot_token, "chat_id": chat_id, "text": text})
        if self.fail_send:
            raise RuntimeError(f"failed with token {bot_token}")
        return {"ok": self.send_ok, "result": {"chat": {"id": chat_id}, "text": text}}


class FakeRuntimeExecutor:
    def __init__(self, *, fail: bool = False, stdout: str | None = None):
        self.fail = fail
        self.stdout = stdout
        self.commands: list[RemoteControlCommand] = []
        self.kwargs: list[dict] = []

    def __call__(self, command: RemoteControlCommand, **kwargs):
        self.commands.append(command)
        self.kwargs.append(kwargs)
        if self.fail:
            raise RuntimeError("/private/tmp/POSTM0FIX.db TOKEN traceback")
        return RemoteControlResult(command=command.name, returncode=0, stdout=self.stdout or f"{command.name} OK", stderr="")


class RemoteControlTelegramTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteReplayStore(Path(self.tmp.name) / "telegram-transport.sqlite3")
        self.addCleanup(self.store.close)
        self.telegram_config = TelegramRemoteControlConfig.from_allowlists(allowed_user_ids=(42,), allowed_chat_ids=(9001,))

    def poll_once(
        self,
        http_client,
        executor,
        *,
        offset: int | None = None,
        remote_config=TRUSTED_CONFIG,
        state_store=None,
    ):
        return poll_telegram_once(
            bot_token=BOT_TOKEN,
            offset=offset,
            telegram_config=self.telegram_config,
            remote_config=remote_config,
            replay_store=self.store,
            executor=executor,
            http_client=http_client,
            poll_timeout=3,
            state_store=state_store,
        )

    def test_valid_authorized_status_flows_to_fake_executor_and_send_message_once(self):
        http = FakeTelegramHttpClient([{"ok": True, "result": [make_update(update_id=10, text="/status")]}])
        executor = FakeRuntimeExecutor()

        result = self.poll_once(http, executor, offset=5)

        self.assertEqual(result.next_offset, 11)
        self.assertEqual(result.accepted, 1)
        self.assertEqual(result.responses_sent, 1)
        self.assertEqual(result.command_acknowledged, 1)
        self.assertEqual(result.pending_deliveries, ())
        self.assertEqual(executor.commands, [RemoteControlCommand(name="STATUS", argv=STATUS_ARGV)])
        self.assertEqual(len(http.send_calls), 1)
        self.assertEqual(http.send_calls[0]["chat_id"], "9001")
        self.assertIn("STATUS: OK", http.send_calls[0]["text"])

    def test_valid_authorized_health_flows_to_fake_executor_and_send_message_once(self):
        http = FakeTelegramHttpClient([{"ok": True, "result": [make_update(update_id=20, text="/health")]}])
        executor = FakeRuntimeExecutor()

        result = self.poll_once(http, executor)

        self.assertEqual(result.next_offset, 21)
        self.assertEqual(result.responses_sent, 1)
        self.assertEqual(result.command_acknowledged, 1)
        self.assertEqual(executor.commands, [RemoteControlCommand(name="HEALTH", argv=HEALTH_ARGV)])
        self.assertEqual(http.send_calls[0]["chat_id"], "9001")
        self.assertIn("HEALTH: OK", http.send_calls[0]["text"])

    def test_help_invokes_no_runtime_executor_and_sends_one_response(self):
        http = FakeTelegramHttpClient([{"ok": True, "result": [make_update(update_id=30, text="/help")]}])
        executor = FakeRuntimeExecutor()

        result = self.poll_once(http, executor)

        self.assertEqual(result.next_offset, 31)
        self.assertEqual(result.responses_sent, 1)
        self.assertEqual(executor.commands, [])
        self.assertIn("/status /health /help", http.send_calls[0]["text"])
        self.assertNotIn("/report", http.send_calls[0]["text"])

    def test_unauthorized_user_causes_no_executor_and_no_command_response(self):
        http = FakeTelegramHttpClient([{"ok": True, "result": [make_update(update_id=40, user_id=7, text="/status")]}])
        executor = FakeRuntimeExecutor()

        result = self.poll_once(http, executor)

        self.assertEqual(result.next_offset, 41)
        self.assertEqual(result.rejected, 1)
        self.assertEqual(result.responses_sent, 0)
        self.assertEqual(executor.commands, [])
        self.assertEqual(http.send_calls, [])

    def test_duplicate_update_id_never_executes_twice(self):
        http = FakeTelegramHttpClient(
            [{"ok": True, "result": [make_update(update_id=50, text="/status"), make_update(update_id=50, text="/health")]}]
        )
        executor = FakeRuntimeExecutor()

        result = self.poll_once(http, executor)

        self.assertEqual(result.next_offset, 51)
        self.assertEqual(result.accepted, 1)
        self.assertEqual(result.rejected, 1)
        self.assertEqual(executor.commands, [RemoteControlCommand(name="STATUS", argv=STATUS_ARGV)])
        self.assertEqual(len(http.send_calls), 1)

    def test_malformed_update_does_not_crash_loop(self):
        http = FakeTelegramHttpClient([{"ok": True, "result": [{"update_id": 60}, "not-a-dict", make_update(update_id=61, text="/help")]}])
        executor = FakeRuntimeExecutor()

        result = self.poll_once(http, executor)

        self.assertEqual(result.next_offset, 62)
        self.assertEqual(result.updates_seen, 3)
        self.assertEqual(result.accepted, 1)
        self.assertEqual(result.rejected, 2)
        self.assertEqual(result.responses_sent, 1)

    def test_telegram_request_data_cannot_modify_trusted_runtime_argv(self):
        http = FakeTelegramHttpClient(
            [
                {
                    "ok": True,
                    "result": [
                        make_update(
                            update_id=70,
                            user_id=42,
                            chat_id=9001,
                            text="/status",
                            username="POSTM0FIX.db --campaign-id attacker",
                        )
                    ],
                }
            ]
        )
        executor = FakeRuntimeExecutor()

        self.poll_once(http, executor)

        self.assertEqual(executor.commands, [RemoteControlCommand(name="STATUS", argv=STATUS_ARGV)])
        self.assertNotIn("POSTM0FIX", repr(executor.commands))
        self.assertNotIn("attacker", repr(executor.commands))

    def test_offset_advances_across_poll_loop(self):
        http = FakeTelegramHttpClient(
            [
                {"ok": True, "result": [make_update(update_id=80, text="/help")]},
                {"ok": True, "result": [make_update(update_id=81, text="/help")]},
            ]
        )
        executor = FakeRuntimeExecutor()

        results = poll_telegram_loop(
            bot_token=BOT_TOKEN,
            offset=None,
            telegram_config=self.telegram_config,
            remote_config=TRUSTED_CONFIG,
            replay_store=self.store,
            executor=executor,
            http_client=http,
            iterations=2,
            sleep=lambda seconds: None,
        )

        self.assertEqual([call["offset"] for call in http.get_calls], [None, 81])
        self.assertEqual([result.next_offset for result in results], [81, 82])

    def test_bot_token_absent_from_audit_results_and_exception_text(self):
        http = FakeTelegramHttpClient([{"ok": True, "result": [make_update(update_id=90, text="/status")]}])
        executor = FakeRuntimeExecutor(stdout=f"ok {BOT_TOKEN} /trusted/control.db CID-123 RUN-456")

        result = self.poll_once(http, executor)

        rows = repr(self.store.list_remote_control_audit_rows())
        sent_text = http.send_calls[0]["text"]
        self.assertNotIn(BOT_TOKEN, rows)
        self.assertNotIn(BOT_TOKEN, repr(result))
        self.assertNotIn(BOT_TOKEN, sent_text)
        self.assertNotIn("/trusted/control.db", sent_text)
        self.assertNotIn("CID-123", sent_text)
        self.assertNotIn("RUN-456", sent_text)

    def test_send_message_targets_only_verified_chat_id(self):
        http = FakeTelegramHttpClient([{"ok": True, "result": [make_update(update_id=100, chat_id=9001, text="/health")]}])
        executor = FakeRuntimeExecutor()

        self.poll_once(http, executor)

        self.assertEqual([call["chat_id"] for call in http.send_calls], ["9001"])

    def test_transport_failures_do_not_execute_or_leak_token(self):
        malformed_payloads = (
            {"ok": False, "description": BOT_TOKEN},
            {"ok": True, "result": {}},
        )
        for index, payload in enumerate(malformed_payloads, start=1):
            with self.subTest(index=index):
                http = FakeTelegramHttpClient([payload])
                executor = FakeRuntimeExecutor()
                result = self.poll_once(http, executor)

                self.assertEqual(result.transport_error, "GET_UPDATES_FAILED")
                self.assertNotIn(BOT_TOKEN, repr(result))
                self.assertEqual(executor.commands, [])
                self.assertEqual(http.send_calls, [])

    def test_send_message_failure_does_not_crash_or_leak_token(self):
        http = FakeTelegramHttpClient([{"ok": True, "result": [make_update(update_id=110, text="/status")]}], fail_send=True)
        executor = FakeRuntimeExecutor()

        result = self.poll_once(http, executor)

        self.assertEqual(result.next_offset, 111)
        self.assertEqual(result.responses_sent, 0)
        self.assertEqual(result.send_failures, 1)
        self.assertEqual(result.command_acknowledged, 1)
        self.assertEqual(len(result.pending_deliveries), 1)
        self.assertEqual(result.pending_deliveries[0].status, "PENDING")
        self.assertEqual(result.pending_deliveries[0].update_id, "110")
        self.assertIsInstance(result.pending_deliveries[0].update_id, str)
        self.assertEqual(result.pending_deliveries[0].failure_reason, "SEND_MESSAGE_FAILED")
        self.assertEqual(executor.commands, [RemoteControlCommand(name="STATUS", argv=STATUS_ARGV)])
        self.assertNotIn(BOT_TOKEN, repr(result))

        retry_http = FakeTelegramHttpClient()
        delivered = deliver_telegram_response(
            result.pending_deliveries[0],
            bot_token=BOT_TOKEN,
            http_client=retry_http,
        )

        self.assertEqual(delivered.status, "DELIVERED")
        self.assertEqual(delivered.update_id, result.pending_deliveries[0].update_id)
        self.assertEqual(len(retry_http.send_calls), 1)
        self.assertEqual(executor.commands, [RemoteControlCommand(name="STATUS", argv=STATUS_ARGV)])

    def test_duplicate_after_send_failure_does_not_execute_twice(self):
        update = make_update(update_id=111, text="/health")
        executor = FakeRuntimeExecutor()
        first = self.poll_once(FakeTelegramHttpClient([{"ok": True, "result": [update]}], fail_send=True), executor)
        second = self.poll_once(FakeTelegramHttpClient([{"ok": True, "result": [update]}]), executor, offset=None)

        self.assertEqual(first.next_offset, 112)
        self.assertEqual(second.next_offset, 112)
        self.assertEqual(second.rejected, 1)
        self.assertEqual(executor.commands, [RemoteControlCommand(name="HEALTH", argv=HEALTH_ARGV)])

    def test_fabricated_response_delivery_cannot_target_a_chat(self):
        http = FakeTelegramHttpClient()
        fabricated = TelegramResponseDelivery(
            update_id="999",
            chat_id="attacker",
            text="fabricated",
            command_acknowledged=True,
            status="PENDING",
        )

        with self.assertRaisesRegex(ValueError, "invalid Telegram response delivery"):
            deliver_telegram_response(fabricated, bot_token=BOT_TOKEN, http_client=http)

        self.assertEqual(http.send_calls, [])

    def test_adapter_request_and_response_delivery_share_normalized_update_id_type(self):
        update_id = 9_223_372_036_854_775_937
        adapter_store = SQLiteReplayStore(Path(self.tmp.name) / "adapter-large-id.sqlite3")
        self.addCleanup(adapter_store.close)
        adapter_result = process_telegram_update(
            make_update(update_id=update_id, text="/status"),
            config=self.telegram_config,
            replay_store=adapter_store,
        )

        poll_result = self.poll_once(
            FakeTelegramHttpClient(
                [{"ok": True, "result": [make_update(update_id=update_id, text="/status")]}],
                fail_send=True,
            ),
            FakeRuntimeExecutor(),
        )

        self.assertTrue(adapter_result.accepted)
        self.assertIsNotNone(adapter_result.request)
        self.assertEqual(adapter_result.request.update_id, str(update_id))
        self.assertEqual(poll_result.pending_deliveries[0].update_id, adapter_result.request.update_id)
        self.assertIsInstance(poll_result.pending_deliveries[0].update_id, str)
        self.assertEqual(poll_result.next_offset, update_id + 1)

    def test_negative_and_invalid_update_ids_keep_existing_behavior(self):
        negative_executor = FakeRuntimeExecutor()
        negative_result = self.poll_once(
            FakeTelegramHttpClient(
                [{"ok": True, "result": [make_update(update_id=-7, text="/health")]}],
                fail_send=True,
            ),
            negative_executor,
        )
        invalid_executor = FakeRuntimeExecutor()
        invalid_result = self.poll_once(
            FakeTelegramHttpClient(
                [{"ok": True, "result": [make_update(update_id="not-numeric", text="/status")]}]
            ),
            invalid_executor,
            offset=25,
        )

        self.assertEqual(negative_result.next_offset, -6)
        self.assertEqual(negative_result.pending_deliveries[0].update_id, "-7")
        self.assertEqual(negative_executor.commands, [RemoteControlCommand(name="HEALTH", argv=HEALTH_ARGV)])
        self.assertEqual(invalid_result.next_offset, 25)
        self.assertEqual(invalid_result.rejected, 1)
        self.assertEqual(invalid_executor.commands, [])

    def test_adapter_store_exception_is_isolated_and_advances_offset(self):
        http = FakeTelegramHttpClient([{"ok": True, "result": [make_update(update_id=112, text="/status")]}])
        executor = FakeRuntimeExecutor()

        with patch(
            "alphaforge.remote_control.telegram_transport.process_telegram_update",
            side_effect=RuntimeError(f"store failed {BOT_TOKEN} /trusted/control.db"),
        ):
            result = self.poll_once(http, executor, offset=100)

        self.assertEqual(result.next_offset, 113)
        self.assertEqual(result.processing_failures, 1)
        self.assertEqual(result.rejected, 1)
        self.assertEqual(executor.commands, [])
        self.assertEqual(http.send_calls, [])
        self.assertNotIn(BOT_TOKEN, repr(result))
        self.assertNotIn("/trusted/control.db", repr(result))

    def test_controller_exception_is_isolated_and_sends_safe_failure(self):
        http = FakeTelegramHttpClient([{"ok": True, "result": [make_update(update_id=113, text="/status")]}])
        executor = FakeRuntimeExecutor()

        with patch(
            "alphaforge.remote_control.telegram_transport.process_telegram_request",
            side_effect=RuntimeError(f"controller failed {BOT_TOKEN} /trusted/control.db"),
        ):
            result = self.poll_once(http, executor)

        self.assertEqual(result.next_offset, 114)
        self.assertEqual(result.command_acknowledged, 1)
        self.assertEqual(result.processing_failures, 1)
        self.assertEqual(result.responses_sent, 1)
        self.assertEqual(executor.commands, [])
        self.assertEqual(http.send_calls[0]["text"], "REMOTE_CONTROL: FAIL rc=1 | stderr=request failed safely")

    def test_offset_semantics_cover_terminal_update_outcomes(self):
        cases = (
            ("unauthorized", make_update(update_id=140, user_id=7, text="/status"), FakeRuntimeExecutor(), False),
            ("malformed", {"update_id": 141}, FakeRuntimeExecutor(), False),
            ("executor_failure", make_update(update_id=142, text="/health"), FakeRuntimeExecutor(fail=True), False),
            ("send_failure", make_update(update_id=143, text="/status"), FakeRuntimeExecutor(), True),
        )
        for name, update, executor, fail_send in cases:
            with self.subTest(name=name):
                http = FakeTelegramHttpClient([{"ok": True, "result": [update]}], fail_send=fail_send)
                result = self.poll_once(http, executor, offset=10)
                self.assertEqual(result.next_offset, update["update_id"] + 1)

    def test_malformed_update_without_id_does_not_change_offset(self):
        result = self.poll_once(
            FakeTelegramHttpClient([{"ok": True, "result": ["malformed"]}]),
            FakeRuntimeExecutor(),
            offset=150,
        )

        self.assertEqual(result.next_offset, 150)
        self.assertEqual(result.rejected, 1)

    def test_retry_backoff_is_bounded_and_injected(self):
        http = FakeTelegramHttpClient(
            [
                {"ok": False, "description": "temporary"},
                {"ok": True, "result": [make_update(update_id=120, text="/help")]},
            ]
        )
        sleeps = []

        results = poll_telegram_loop(
            bot_token=BOT_TOKEN,
            offset=12,
            telegram_config=self.telegram_config,
            remote_config=TRUSTED_CONFIG,
            replay_store=self.store,
            executor=FakeRuntimeExecutor(),
            http_client=http,
            iterations=2,
            retry_backoff_seconds=0.25,
            sleep=sleeps.append,
        )

        self.assertEqual(sleeps, [0.25])
        self.assertEqual(results[0].transport_error, "GET_UPDATES_FAILED")
        self.assertEqual(results[1].responses_sent, 1)

    def test_network_functions_are_fake_injected(self):
        http = FakeTelegramHttpClient([{"ok": True, "result": [make_update(update_id=130, text="/help")]}])
        with patch("urllib.request.urlopen") as urlopen, patch("socket.create_connection") as create_connection, patch(
            "alphaforge.remote_control.commands.subprocess.run"
        ) as subprocess_run:
            result = self.poll_once(http, FakeRuntimeExecutor())

        self.assertEqual(result.responses_sent, 1)
        urlopen.assert_not_called()
        create_connection.assert_not_called()
        subprocess_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
