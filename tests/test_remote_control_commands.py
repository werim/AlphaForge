from __future__ import annotations

import unittest
from unittest.mock import Mock

from alphaforge.remote_control.commands import (
    CommandParseError,
    RemoteControlConfig,
    RemoteControlResult,
    dispatch_remote_control_command,
    load_remote_control_config,
    map_remote_command,
    parse_remote_command,
    run_remote_control_text,
)


TRUSTED_VALUES = {
    "remote_control_db_path": "/trusted/control.db",
    "remote_control_campaign_id": "CID-123",
    "remote_control_run_id": "RUN-456",
}


class RemoteControlCommandTests(unittest.TestCase):
    def test_parse_remote_command_accepts_only_exact_status_and_health(self):
        self.assertEqual(parse_remote_command("AF STATUS"), "STATUS")
        self.assertEqual(parse_remote_command("AF HEALTH"), "HEALTH")
        for text in ("AF", "AF STATUS NOW", "AF  STATUS", "af status", "STATUS AF", "AF UNKNOWN"):
            with self.subTest(text=text), self.assertRaises(CommandParseError):
                parse_remote_command(text)

    def test_load_remote_control_config_accepts_valid_config(self):
        config = load_remote_control_config(
            {"db": "/trusted/control.db", "cid": "CID-123", "run": "RUN-456"}
        )
        self.assertEqual(
            config,
            RemoteControlConfig(
                db="/trusted/control.db",
                cid="CID-123",
                run="RUN-456",
            ),
        )

    def test_load_remote_control_config_rejects_missing_or_invalid_fields(self):
        invalid_configs = (
            {"db": "/trusted/control.db", "cid": "CID-123"},
            {"db": "/trusted/control.db", "cid": "CID-123", "run": "RUN-456", "extra": "no"},
            {"db": 123, "cid": "CID-123", "run": "RUN-456"},
            {"db": "/trusted/control.db", "cid": None, "run": "RUN-456"},
            {"db": "/trusted/control.db", "cid": "CID-123", "run": ""},
        )
        for data in invalid_configs:
            with self.subTest(data=data), self.assertRaises(ValueError):
                load_remote_control_config(data)
        with self.assertRaises(ValueError):
            load_remote_control_config([])  # type: ignore[arg-type]

    def test_map_remote_command_uses_trusted_config_values_only(self):
        self.assertEqual(
            map_remote_command("STATUS", TRUSTED_VALUES).argv,
            (
                "burnin_ops",
                "--db",
                "/trusted/control.db",
                "status",
                "--campaign-id",
                "CID-123",
                "--run-id",
                "RUN-456",
            ),
        )
        self.assertEqual(
            map_remote_command("HEALTH", TRUSTED_VALUES).argv,
            (
                "burnin_ops",
                "--db",
                "/trusted/control.db",
                "health",
                "--campaign-id",
                "CID-123",
                "--run-id",
                "RUN-456",
            ),
        )
        invalid = dict(TRUSTED_VALUES, remote_control_db_path="")
        with self.assertRaises(ValueError):
            map_remote_command("STATUS", invalid)

    def test_dispatch_remote_control_command_uses_fake_executor_and_formats_output(self):
        command = map_remote_command("STATUS", TRUSTED_VALUES)
        fake_result = RemoteControlResult(
            command="STATUS",
            returncode=0,
            stdout="ok",
            stderr="",
        )
        executor = Mock(return_value=fake_result)
        result = dispatch_remote_control_command(
            command,
            config=TRUSTED_VALUES,
            executor=executor,
            timeout=2.5,
            max_output_chars=10,
        )
        executor.assert_called_once_with(
            command,
            config=TRUSTED_VALUES,
            timeout=2.5,
            max_output_chars=10,
        )
        self.assertEqual(result.command, "STATUS")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "ok")
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.formatted, "STATUS: OK rc=0 | stdout=ok")

    def test_dispatch_remote_control_command_truncates_output_safely(self):
        command = map_remote_command("HEALTH", TRUSTED_VALUES)
        fake_result = RemoteControlResult(
            command="HEALTH",
            returncode=1,
            stdout="x" * 50,
            stderr="y" * 50,
        )
        executor = Mock(return_value=fake_result)
        result = dispatch_remote_control_command(
            command,
            config=TRUSTED_VALUES,
            executor=executor,
            max_output_chars=8,
        )
        self.assertEqual(result.stdout, "xxxxxxxx")
        self.assertEqual(result.stderr, "yyyyyyyy")
        self.assertIn("HEALTH: FAIL rc=1", result.formatted)
        self.assertIn("stdout=xxxxxxxx", result.formatted)
        self.assertIn("stderr=yyyyyyyy", result.formatted)

    def test_dispatch_remote_control_command_rejects_unmapped_argv(self):
        command = map_remote_command("STATUS", TRUSTED_VALUES)
        bad = type(command)(name=command.name, argv=command.argv[:-1] + ("WRONG",))
        executor = Mock()
        with self.assertRaises(CommandParseError):
            dispatch_remote_control_command(bad, config=TRUSTED_VALUES, executor=executor)
        executor.assert_not_called()

    def test_wiring_maps_status_and_health_from_loaded_config(self):
        config = load_remote_control_config(
            {"db": "/trusted/control.db", "cid": "CID-123", "run": "RUN-456"}
        )
        expected_argv = {
            "AF STATUS": (
                "burnin_ops",
                "--db",
                "/trusted/control.db",
                "status",
                "--campaign-id",
                "CID-123",
                "--run-id",
                "RUN-456",
            ),
            "AF HEALTH": (
                "burnin_ops",
                "--db",
                "/trusted/control.db",
                "health",
                "--campaign-id",
                "CID-123",
                "--run-id",
                "RUN-456",
            ),
        }
        for text, argv in expected_argv.items():
            with self.subTest(text=text):
                executor = Mock(return_value=RemoteControlResult(command=text.split()[1], returncode=0, stdout="ok", stderr=""))
                result = run_remote_control_text(text, config, executor=executor)
                command = executor.call_args.args[0]
                self.assertEqual(command.argv, argv)
                self.assertEqual(executor.call_args.kwargs["config"], TRUSTED_VALUES)
                self.assertTrue(result.formatted.startswith(f"{command.name}: OK rc=0"))

    def test_email_body_cannot_override_trusted_values(self):
        config = RemoteControlConfig(
            db="/trusted/control.db",
            cid="CID-123",
            run="RUN-456",
        )
        bodies = (
            "AF STATUS --db /attacker.db",
            "AF HEALTH --campaign-id OTHER",
            "AF STATUS --run-id OTHER",
        )
        executor = Mock()
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(CommandParseError):
                run_remote_control_text(body, config, executor=executor)
        executor.assert_not_called()

    def test_wiring_rejects_unknown_command_without_execution(self):
        config = RemoteControlConfig(
            db="/trusted/control.db",
            cid="CID-123",
            run="RUN-456",
        )
        with self.assertRaises(CommandParseError):
            run_remote_control_text("AF REPORT", config, executor=Mock())


if __name__ == "__main__":
    unittest.main()
