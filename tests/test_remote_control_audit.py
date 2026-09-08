from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from alphaforge.remote_control import run_remote_control_message
from alphaforge.remote_control.audit import SQLiteReplayStore
from alphaforge.remote_control.commands import RemoteControlResult

from test_remote_control_commands import make_config_file


class SQLiteReplayStoreTests(unittest.TestCase):
    def test_first_claim_succeeds_and_duplicate_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "controller-replay.sqlite3"
            with SQLiteReplayStore(db_path) as store:
                self.assertTrue(store.claim_message_id("<msg-1>"))
                self.assertFalse(store.claim_message_id("<msg-1>"))

    def test_duplicate_fails_after_store_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "controller-replay.sqlite3"
            with SQLiteReplayStore(db_path) as store:
                self.assertTrue(store.claim_message_id("<msg-restart>"))
            with SQLiteReplayStore(db_path) as reopened:
                self.assertFalse(reopened.claim_message_id("<msg-restart>"))

    def test_two_store_instances_cannot_both_claim_same_message_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "controller-replay.sqlite3"
            with SQLiteReplayStore(db_path) as first, SQLiteReplayStore(db_path) as second:
                claims = (
                    first.claim_message_id("<msg-shared>"),
                    second.claim_message_id("<msg-shared>"),
                )
            self.assertEqual(claims.count(True), 1)
            self.assertEqual(claims.count(False), 1)

    def test_message_id_is_bound_as_a_sql_parameter(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "controller-replay.sqlite3"
            message_id = "<msg-'; DROP TABLE replay_message_ids;--?>"
            with SQLiteReplayStore(db_path) as store:
                self.assertTrue(store.claim_message_id(message_id))
                self.assertFalse(store.claim_message_id(message_id))
                self.assertTrue(store.claim_message_id("<msg-after>"))

    def test_malformed_message_id_is_rejected_before_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = make_config_file(tmp)
            db_path = Path(tmp) / "controller-replay.sqlite3"
            executor = Mock()
            with SQLiteReplayStore(db_path) as store, patch.object(store, "reserve", wraps=store.reserve) as reserve:
                for message_id in (None, "", "x" * 129):
                    with self.subTest(message_id=message_id), self.assertRaises(ValueError):
                        run_remote_control_message(
                            "AF STATUS",
                            config_path=config_path,
                            executor=executor,
                            replay_store=store,
                            sender="sender@example.com",
                            message_id=message_id,
                        )
                reserve.assert_not_called()
            executor.assert_not_called()

    def test_persistent_store_dispatches_new_status_once_and_rejects_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = make_config_file(tmp)
            db_path = Path(tmp) / "controller-replay.sqlite3"
            executor = Mock(
                return_value=RemoteControlResult(command="STATUS", returncode=0, stdout="ok", stderr="")
            )
            with SQLiteReplayStore(db_path) as store, patch(
                "alphaforge.remote_control.commands.subprocess.run"
            ) as subprocess_run:
                result = run_remote_control_message(
                    "AF STATUS",
                    config_path=config_path,
                    executor=executor,
                    replay_store=store,
                    sender="sender@example.com",
                    message_id="<msg-status>",
                )
                with self.assertRaises(ValueError):
                    run_remote_control_message(
                        "AF STATUS",
                        config_path=config_path,
                        executor=executor,
                        replay_store=store,
                        sender="sender@example.com",
                        message_id="<msg-status>",
                    )
                subprocess_run.assert_not_called()
            self.assertEqual(result.command, "STATUS")
            executor.assert_called_once()


if __name__ == "__main__":
    unittest.main()
