from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from alphaforge.remote_control.commands import (
    CommandParseError,
    RemoteControlConfig,
    execute_remote_command,
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

    def test_execute_remote_command_invokes_subprocess_with_exact_argv_and_no_shell(self):
        command = map_remote_command("STATUS", TRUSTED_VALUES)
        proc = Mock(returncode=0, stdout="ok", stderr="")
        with patch("alphaforge.remote_control.commands.subprocess.run", return_value=proc) as run:
            result = execute_remote_command(
                command,
                config=TRUSTED_VALUES,
                timeout=2.5,
                max_output_chars=10,
            )
        run.assert_called_once_with(
            command.argv,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=2.5,
        )
        self.assertEqual(result.command, "STATUS")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "ok")
        self.assertEqual(result.stderr, "")

    def test_execute_remote_command_truncates_stdout_and_stderr(self):
        command = map_remote_command("HEALTH", TRUSTED_VALUES)
        proc = Mock(returncode=1, stdout="x" * 50, stderr="y" * 50)
        with patch("alphaforge.remote_control.commands.subprocess.run", return_value=proc):
            result = execute_remote_command(command, config=TRUSTED_VALUES, max_output_chars=8)
        self.assertEqual(result.stdout, "xxxxxxxx")
        self.assertEqual(result.stderr, "yyyyyyyy")

    def test_execute_remote_command_rejects_unmapped_argv(self):
        command = map_remote_command("STATUS", TRUSTED_VALUES)
        bad = type(command)(name=command.name, argv=command.argv[:-1] + ("WRONG",))
        with self.assertRaises(CommandParseError):
            execute_remote_command(bad, config=TRUSTED_VALUES)

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
            with self.subTest(text=text), patch(
                "alphaforge.remote_control.commands.execute_remote_command"
            ) as execute:
                run_remote_control_text(text, config)
                command = execute.call_args.args[0]
                self.assertEqual(command.argv, argv)
                self.assertEqual(
                    execute.call_args.kwargs["config"],
                    TRUSTED_VALUES,
                )

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
        with patch("alphaforge.remote_control.commands.execute_remote_command") as execute:
            for body in bodies:
                with self.subTest(body=body), self.assertRaises(CommandParseError):
                    run_remote_control_text(body, config)
            execute.assert_not_called()

    def test_wiring_rejects_unknown_command_without_execution(self):
        config = RemoteControlConfig(
            db="/trusted/control.db",
            cid="CID-123",
            run="RUN-456",
        )
        with patch("alphaforge.remote_control.commands.execute_remote_command") as execute:
            with self.assertRaises(CommandParseError):
                run_remote_control_text("AF REPORT", config)
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
