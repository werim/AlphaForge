from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alphaforge.remote_control.audit import SQLiteReplayStore
from alphaforge.remote_control.telegram_adapter import (
    ALLOWED_TELEGRAM_COMMANDS,
    TelegramRemoteControlConfig,
    parse_telegram_command,
    process_telegram_update,
)


BOT_TOKEN = "123456:SECRET-TELEGRAM-BOT-TOKEN"


def make_update(
    *,
    update_id: int | str = 1001,
    user_id: int | str = 42,
    chat_id: int | str = 9001,
    text: str = "/status",
    username: str = "ignored_admin_name",
) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 11,
            "from": {
                "id": user_id,
                "is_bot": False,
                "username": username,
                "first_name": "Ignored",
            },
            "chat": {
                "id": chat_id,
                "type": "private",
                "username": username,
            },
            "text": text,
        },
    }


class RemoteControlTelegramAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteReplayStore(Path(self.tmp.name) / "telegram-replay.sqlite3")
        self.addCleanup(self.store.close)
        self.config = TelegramRemoteControlConfig.from_allowlists(
            allowed_user_ids=(42,),
            allowed_chat_ids=(9001,),
        )

    def rows(self):
        return self.store.list_remote_control_audit_rows()

    def test_valid_authorized_status_returns_normalized_read_only_command(self):
        result = process_telegram_update(make_update(text="/status"), config=self.config, replay_store=self.store)

        self.assertTrue(result.accepted)
        self.assertEqual(result.command, "STATUS")
        self.assertIsNotNone(result.request)
        assert result.request is not None
        self.assertEqual(result.request.command.name, "STATUS")
        self.assertEqual(result.request.command.argv, ("STATUS",))
        self.assertEqual(
            self.rows()[0],
            {
                "transport": "telegram",
                "update_id": "1001",
                "authorized_identity": "telegram:user_id=42;chat_id=9001",
                "normalized_command": "STATUS",
                "result": "ACCEPTED",
                "rejection_reason": None,
                "created_at": self.rows()[0]["created_at"],
            },
        )

    def test_valid_authorized_health_returns_normalized_read_only_command(self):
        result = process_telegram_update(make_update(update_id=1002, text="/health"), config=self.config, replay_store=self.store)

        self.assertTrue(result.accepted)
        self.assertEqual(result.command, "HEALTH")
        self.assertEqual(self.rows()[0]["normalized_command"], "HEALTH")

    def test_all_allowed_read_only_commands_are_supported(self):
        for index, (telegram_text, command_name) in enumerate(ALLOWED_TELEGRAM_COMMANDS.items(), start=1):
            with self.subTest(telegram_text=telegram_text):
                result = process_telegram_update(
                    make_update(update_id=2000 + index, text=telegram_text),
                    config=self.config,
                    replay_store=self.store,
                )
                self.assertTrue(result.accepted)
                self.assertEqual(result.command, command_name)

        self.assertEqual(
            [row["normalized_command"] for row in self.rows()],
            list(ALLOWED_TELEGRAM_COMMANDS.values()),
        )

    def test_unauthorized_user_is_rejected_without_replay_poisoning(self):
        first = process_telegram_update(
            make_update(update_id=3001, user_id=7),
            config=self.config,
            replay_store=self.store,
        )
        second = process_telegram_update(
            make_update(update_id=3001, user_id=42),
            config=self.config,
            replay_store=self.store,
        )

        self.assertFalse(first.accepted)
        self.assertEqual(first.rejection_reason, "UNAUTHORIZED_IDENTITY")
        self.assertTrue(second.accepted)
        self.assertEqual([row["result"] for row in self.rows()], ["REJECTED", "ACCEPTED"])

    def test_unauthorized_chat_is_rejected_when_chat_allowlist_configured(self):
        result = process_telegram_update(
            make_update(update_id=3002, chat_id=123),
            config=self.config,
            replay_store=self.store,
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.rejection_reason, "UNAUTHORIZED_IDENTITY")
        self.assertEqual(self.rows()[0]["authorized_identity"], "telegram:user_id=42;chat_id=123")

    def test_user_only_allowlist_accepts_any_chat(self):
        config = TelegramRemoteControlConfig.from_allowlists(allowed_user_ids=(42,))
        result = process_telegram_update(make_update(update_id=3003, chat_id=123), config=config, replay_store=self.store)

        self.assertTrue(result.accepted)
        self.assertEqual(result.command, "STATUS")

    def test_duplicate_update_id_is_rejected_without_new_acceptance(self):
        first = process_telegram_update(make_update(update_id=4001), config=self.config, replay_store=self.store)
        second = process_telegram_update(
            make_update(update_id=4001, text="/health"),
            config=self.config,
            replay_store=self.store,
        )

        self.assertTrue(first.accepted)
        self.assertFalse(second.accepted)
        self.assertEqual(second.rejection_reason, "DUPLICATE_UPDATE_ID")
        self.assertEqual([row["result"] for row in self.rows()], ["ACCEPTED", "REJECTED"])
        self.assertEqual([row["normalized_command"] for row in self.rows()], ["STATUS", "HEALTH"])

    def test_malformed_update_is_rejected(self):
        malformed_updates = (
            {},
            {"update_id": 5001},
            {"update_id": 5002, "message": {"text": "/status"}},
            make_update(update_id="not-numeric"),
            make_update(update_id=5003, user_id="not-numeric"),
            make_update(update_id=5004, chat_id="not-numeric"),
            make_update(update_id=5005, text=""),
            make_update(update_id=5006, text="/status\n/health"),
        )

        for update in malformed_updates:
            with self.subTest(update=update):
                result = process_telegram_update(update, config=self.config, replay_store=self.store)
                self.assertFalse(result.accepted)
                self.assertEqual(result.rejection_reason, "MALFORMED_UPDATE")

    def test_unknown_command_is_rejected(self):
        result = process_telegram_update(make_update(update_id=6001, text="/start"), config=self.config, replay_store=self.store)

        self.assertFalse(result.accepted)
        self.assertEqual(result.rejection_reason, "UNSUPPORTED_COMMAND")
        self.assertEqual(self.rows()[0]["normalized_command"], None)

    def test_arguments_and_injection_like_text_are_rejected(self):
        bad_commands = (
            "/status now",
            "/status --db POSTM0FIX.db",
            "/health; rm -rf /",
            "/report && burnin_ops status",
            "/labels | cat",
            "/errors $(echo x)",
        )

        for index, text in enumerate(bad_commands, start=1):
            with self.subTest(text=text):
                result = process_telegram_update(
                    make_update(update_id=7000 + index, text=text),
                    config=self.config,
                    replay_store=self.store,
                )
                self.assertFalse(result.accepted)
                self.assertEqual(result.rejection_reason, "UNSUPPORTED_COMMAND")

    def test_parse_telegram_command_rejects_arguments(self):
        self.assertEqual(parse_telegram_command("/REPORT"), "REPORT")
        with self.assertRaises(Exception):
            parse_telegram_command("/report full")

    def test_bot_token_is_never_persisted_in_replay_or_audit_rows(self):
        result = process_telegram_update(
            make_update(update_id=8001, text="/help", username=BOT_TOKEN),
            config=self.config,
            replay_store=self.store,
        )

        self.assertTrue(result.accepted)
        serialized_rows = repr(self.rows())
        self.assertNotIn(BOT_TOKEN, serialized_rows)
        self.assertNotIn("SECRET-TELEGRAM-BOT-TOKEN", serialized_rows)

    def test_adapter_never_invokes_subprocess_network_or_runtime_db(self):
        with patch("alphaforge.remote_control.commands.subprocess.run") as subprocess_run, patch(
            "urllib.request.urlopen"
        ) as urlopen, patch("socket.create_connection") as create_connection:
            result = process_telegram_update(
                make_update(update_id=9001, text="/status"),
                config=self.config,
                replay_store=self.store,
            )

        self.assertTrue(result.accepted)
        subprocess_run.assert_not_called()
        urlopen.assert_not_called()
        create_connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
