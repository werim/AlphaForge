from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alphaforge.remote_control.audit import SQLiteReplayStore
from alphaforge.remote_control.commands import RemoteControlCommand
from alphaforge.remote_control.telegram_adapter import TelegramRemoteControlConfig
from alphaforge.remote_control.telegram_state import SQLiteTelegramTransportStateStore
from alphaforge.remote_control.telegram_transport import (
    poll_telegram_loop,
    poll_telegram_once,
    retry_pending_telegram_responses,
)

from test_remote_control_telegram_adapter import make_update
from test_remote_control_telegram_controller import HEALTH_ARGV, STATUS_ARGV, TRUSTED_CONFIG
from test_remote_control_telegram_transport import BOT_TOKEN, FakeRuntimeExecutor, FakeTelegramHttpClient


class TelegramTransportStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "telegram-state.sqlite3"
        self.replay_path = Path(self.tmp.name) / "telegram-replay.sqlite3"
        self.telegram_config = TelegramRemoteControlConfig.from_allowlists(
            allowed_user_ids=(42,),
            allowed_chat_ids=(9001,),
        )

    def poll_once(self, *, executor, http, state_store, replay_store, offset=None, remote_config=TRUSTED_CONFIG):
        return poll_telegram_once(
            bot_token=BOT_TOKEN,
            offset=offset,
            telegram_config=self.telegram_config,
            remote_config=remote_config,
            replay_store=replay_store,
            executor=executor,
            http_client=http,
            poll_timeout=3,
            state_store=state_store,
        )

    def test_offset_survives_reopen_and_never_regresses(self):
        with SQLiteTelegramTransportStateStore(self.state_path) as state:
            self.assertEqual(state.acknowledge_offset(101), 101)
            self.assertEqual(state.acknowledge_offset(99), 101)
        with SQLiteTelegramTransportStateStore(self.state_path) as reopened:
            self.assertEqual(reopened.load_offset(), 101)

    def test_poll_loop_initializes_from_durable_offset(self):
        with SQLiteTelegramTransportStateStore(self.state_path) as state, SQLiteReplayStore(self.replay_path) as replay:
            state.acknowledge_offset(205)
            http = FakeTelegramHttpClient([{"ok": True, "result": []}])
            results = poll_telegram_loop(
                bot_token=BOT_TOKEN,
                offset=None,
                telegram_config=self.telegram_config,
                remote_config=TRUSTED_CONFIG,
                replay_store=replay,
                executor=FakeRuntimeExecutor(),
                http_client=http,
                iterations=1,
                sleep=lambda seconds: None,
                state_store=state,
            )

        self.assertEqual(http.get_calls[0]["offset"], 205)
        self.assertEqual(results[0].next_offset, 205)

    def test_pending_delivery_and_offset_transaction_is_atomic(self):
        def fail_pending_commit(operation):
            if operation == "PERSIST_PENDING_WITH_OFFSET":
                raise RuntimeError("injected transaction failure")

        with SQLiteTelegramTransportStateStore(self.state_path, before_commit=fail_pending_commit) as state:
            with self.assertRaises(RuntimeError):
                state.persist_pending_with_offset(
                    update_id="300",
                    chat_id="9001",
                    text="STATUS: OK",
                    next_offset=301,
                )
            self.assertIsNone(state.load_offset())
            self.assertEqual(state.load_pending(), ())

    def test_poll_transaction_failure_neither_advances_nor_sends(self):
        def fail_pending_commit(operation):
            if operation == "PERSIST_PENDING_WITH_OFFSET":
                raise RuntimeError("injected transaction failure")

        executor = FakeRuntimeExecutor()
        http = FakeTelegramHttpClient([{"ok": True, "result": [make_update(update_id=305, text="/status")]}])
        with SQLiteReplayStore(self.replay_path) as replay, SQLiteTelegramTransportStateStore(
            self.state_path,
            before_commit=fail_pending_commit,
        ) as state:
            result = self.poll_once(
                executor=executor,
                http=http,
                state_store=state,
                replay_store=replay,
            )
            self.assertIsNone(state.load_offset())
            self.assertEqual(state.load_pending(), ())

        self.assertEqual(result.transport_error, "STATE_PERSIST_FAILED")
        self.assertEqual(http.send_calls, [])
        self.assertEqual(executor.commands, [RemoteControlCommand(name="STATUS", argv=STATUS_ARGV)])

    def test_status_failed_send_survives_restart_and_retries_without_executor(self):
        executor = FakeRuntimeExecutor()
        replay = SQLiteReplayStore(self.replay_path)
        self.addCleanup(replay.close)
        failed_http = FakeTelegramHttpClient(
            [{"ok": True, "result": [make_update(update_id=310, text="/status")]}],
            fail_send=True,
        )
        with SQLiteTelegramTransportStateStore(self.state_path) as state:
            result = self.poll_once(
                executor=executor,
                http=failed_http,
                state_store=state,
                replay_store=replay,
            )
            self.assertEqual(state.load_offset(), 311)
            self.assertEqual(state.load_pending()[0].update_id, "310")

        retry_http = FakeTelegramHttpClient()
        with SQLiteTelegramTransportStateStore(self.state_path) as reopened:
            retried = retry_pending_telegram_responses(
                bot_token=BOT_TOKEN,
                http_client=retry_http,
                state_store=reopened,
            )
            self.assertEqual(reopened.load_offset(), 311)
            self.assertEqual(reopened.load_pending(), ())

        self.assertEqual(result.pending_deliveries[0].update_id, "310")
        self.assertEqual(retried[0].status, "DELIVERED")
        self.assertEqual(executor.commands, [RemoteControlCommand(name="STATUS", argv=STATUS_ARGV)])
        self.assertEqual(len(retry_http.send_calls), 1)

    def test_health_failed_send_is_durable(self):
        executor = FakeRuntimeExecutor()
        with SQLiteReplayStore(self.replay_path) as replay, SQLiteTelegramTransportStateStore(self.state_path) as state:
            result = self.poll_once(
                executor=executor,
                http=FakeTelegramHttpClient(
                    [{"ok": True, "result": [make_update(update_id=320, text="/health")]}],
                    fail_send=True,
                ),
                state_store=state,
                replay_store=replay,
            )

            self.assertEqual(result.next_offset, 321)
            self.assertEqual(state.load_pending()[0].update_id, "320")
        self.assertEqual(executor.commands, [RemoteControlCommand(name="HEALTH", argv=HEALTH_ARGV)])

    def test_successful_delivery_is_durably_removed(self):
        with SQLiteReplayStore(self.replay_path) as replay, SQLiteTelegramTransportStateStore(self.state_path) as state:
            result = self.poll_once(
                executor=FakeRuntimeExecutor(),
                http=FakeTelegramHttpClient(
                    [{"ok": True, "result": [make_update(update_id=325, text="/help")]}]
                ),
                state_store=state,
                replay_store=replay,
            )
            self.assertEqual(result.responses_sent, 1)
            self.assertEqual(state.load_offset(), 326)
            self.assertEqual(state.load_pending(), ())

        with SQLiteTelegramTransportStateStore(self.state_path) as reopened:
            self.assertEqual(reopened.load_offset(), 326)
            self.assertEqual(reopened.load_pending(), ())

    def test_send_accepted_but_delivery_ack_lost_may_resend_after_restart(self):
        def fail_delivered_commit(operation):
            if operation == "MARK_DELIVERED":
                raise RuntimeError("simulated crash before delivered acknowledgement")

        executor = FakeRuntimeExecutor()
        first_http = FakeTelegramHttpClient([{"ok": True, "result": [make_update(update_id=330, text="/status")]}])
        with SQLiteReplayStore(self.replay_path) as replay, SQLiteTelegramTransportStateStore(
            self.state_path,
            before_commit=fail_delivered_commit,
        ) as state:
            first = self.poll_once(
                executor=executor,
                http=first_http,
                state_store=state,
                replay_store=replay,
            )
            self.assertEqual(first.responses_sent, 1)
            self.assertEqual(len(state.load_pending()), 1)

        retry_http = FakeTelegramHttpClient()
        with SQLiteTelegramTransportStateStore(self.state_path) as reopened:
            retried = retry_pending_telegram_responses(
                bot_token=BOT_TOKEN,
                http_client=retry_http,
                state_store=reopened,
            )
            self.assertEqual(reopened.load_pending(), ())

        self.assertEqual(retried[0].status, "DELIVERED")
        self.assertEqual(len(first_http.send_calls), 1)
        self.assertEqual(len(retry_http.send_calls), 1)
        self.assertEqual(executor.commands, [RemoteControlCommand(name="STATUS", argv=STATUS_ARGV)])

    def test_state_contains_no_token_or_runtime_identifiers_and_text_is_bounded(self):
        executor = FakeRuntimeExecutor(
            stdout=f"{BOT_TOKEN} /trusted/control.db CID-123 RUN-456 " + ("x" * 5000)
        )
        with SQLiteReplayStore(self.replay_path) as replay, SQLiteTelegramTransportStateStore(self.state_path) as state:
            self.poll_once(
                executor=executor,
                http=FakeTelegramHttpClient(
                    [{"ok": True, "result": [make_update(update_id=340, text="/status")]}],
                    fail_send=True,
                ),
                state_store=state,
                replay_store=replay,
            )
            pending = state.load_pending()[0]
            self.assertLessEqual(len(pending.text), 3900)

        with sqlite3.connect(self.state_path) as connection:
            stored = repr(connection.execute("SELECT * FROM telegram_transport_state").fetchall())
            stored += repr(connection.execute("SELECT * FROM telegram_pending_deliveries").fetchall())
        for secret in (BOT_TOKEN, "/trusted/control.db", "CID-123", "RUN-456"):
            self.assertNotIn(secret, stored)

    def test_corrupt_persisted_delivery_fails_closed_without_send(self):
        with SQLiteTelegramTransportStateStore(self.state_path):
            pass
        with sqlite3.connect(self.state_path) as connection:
            connection.execute(
                """
                INSERT INTO telegram_pending_deliveries (
                    update_id, chat_id, response_text, status, failure_reason, retry_count
                ) VALUES ('350', 'attacker-chat', 'forged', 'PENDING', NULL, 0)
                """
            )
        http = FakeTelegramHttpClient()
        with SQLiteTelegramTransportStateStore(self.state_path) as reopened:
            self.assertEqual(
                retry_pending_telegram_responses(
                    bot_token=BOT_TOKEN,
                    http_client=http,
                    state_store=reopened,
                ),
                (),
            )
        self.assertEqual(http.send_calls, [])

    def test_duplicate_update_still_executes_once_with_durable_state(self):
        update = make_update(update_id=360, text="/health")
        executor = FakeRuntimeExecutor()
        with SQLiteReplayStore(self.replay_path) as replay, SQLiteTelegramTransportStateStore(self.state_path) as state:
            first = self.poll_once(
                executor=executor,
                http=FakeTelegramHttpClient([{"ok": True, "result": [update]}], fail_send=True),
                state_store=state,
                replay_store=replay,
            )
            second = self.poll_once(
                executor=executor,
                http=FakeTelegramHttpClient([{"ok": True, "result": [update]}]),
                state_store=state,
                replay_store=replay,
                offset=None,
            )

        self.assertEqual(first.next_offset, 361)
        self.assertEqual(second.rejected, 1)
        self.assertEqual(executor.commands, [RemoteControlCommand(name="HEALTH", argv=HEALTH_ARGV)])

    def test_only_fake_network_and_executor_boundaries_are_used(self):
        with SQLiteReplayStore(self.replay_path) as replay, SQLiteTelegramTransportStateStore(self.state_path) as state:
            with patch("urllib.request.urlopen") as urlopen, patch("socket.create_connection") as socket_connect, patch(
                "alphaforge.remote_control.commands.subprocess.run"
            ) as subprocess_run:
                self.poll_once(
                    executor=FakeRuntimeExecutor(),
                    http=FakeTelegramHttpClient(
                        [{"ok": True, "result": [make_update(update_id=370, text="/status")]}]
                    ),
                    state_store=state,
                    replay_store=replay,
                )
        urlopen.assert_not_called()
        socket_connect.assert_not_called()
        subprocess_run.assert_not_called()

    def test_unauthorized_update_advances_durable_offset_without_delivery(self):
        executor = FakeRuntimeExecutor()
        with SQLiteReplayStore(self.replay_path) as replay, SQLiteTelegramTransportStateStore(self.state_path) as state:
            result = self.poll_once(
                executor=executor,
                http=FakeTelegramHttpClient(
                    [{"ok": True, "result": [make_update(update_id=380, user_id=7, text="/status")]}]
                ),
                state_store=state,
                replay_store=replay,
            )
            self.assertEqual(state.load_offset(), 381)
            self.assertEqual(state.load_pending(), ())

        self.assertEqual(result.rejected, 1)
        self.assertEqual(executor.commands, [])

    def test_malformed_persisted_offset_fails_closed_before_http(self):
        with SQLiteTelegramTransportStateStore(self.state_path):
            pass
        with sqlite3.connect(self.state_path) as connection:
            connection.execute(
                "INSERT INTO telegram_transport_state (state_key, state_value) VALUES ('next_offset', 'not-an-int')"
            )
        http = FakeTelegramHttpClient()
        with SQLiteReplayStore(self.replay_path) as replay, SQLiteTelegramTransportStateStore(self.state_path) as state:
            result = self.poll_once(
                executor=FakeRuntimeExecutor(),
                http=http,
                state_store=state,
                replay_store=replay,
            )

        self.assertEqual(result.transport_error, "STATE_LOAD_FAILED")
        self.assertEqual(http.get_calls, [])


if __name__ == "__main__":
    unittest.main()
