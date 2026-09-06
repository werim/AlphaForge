from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from alphaforge.remote_control.commands import CommandParseError, execute_remote_command, map_remote_command, parse_remote_command


def test_parse_remote_command_accepts_only_exact_status_and_health():
    assert parse_remote_command("AF STATUS") == "STATUS"
    assert parse_remote_command("AF HEALTH") == "HEALTH"
    for text in ("AF", "AF STATUS NOW", "AF  STATUS", "af status", "STATUS AF", "AF UNKNOWN"):
        with pytest.raises(CommandParseError):
            parse_remote_command(text)


def test_map_remote_command_uses_trusted_config_values_only():
    config = {
        "remote_control_db_path": "/tmp/POSTM0FIX.db",
        "remote_control_campaign_id": "CID-123",
        "remote_control_run_id": "RUN-456",
    }
    assert map_remote_command("STATUS", config).argv == (
        "burnin_ops",
        "--db",
        "/tmp/POSTM0FIX.db",
        "status",
        "--campaign-id",
        "CID-123",
        "--run-id",
        "RUN-456",
    )
    assert map_remote_command("HEALTH", config).argv == (
        "burnin_ops",
        "--db",
        "/tmp/POSTM0FIX.db",
        "health",
        "--campaign-id",
        "CID-123",
        "--run-id",
        "RUN-456",
    )
    with pytest.raises(ValueError):
        map_remote_command("STATUS", {"remote_control_db_path": "", "remote_control_campaign_id": "CID", "remote_control_run_id": "RUN"})


def test_execute_remote_command_invokes_subprocess_with_exact_argv_and_no_shell():
    config = {
        "remote_control_db_path": "/tmp/POSTM0FIX.db",
        "remote_control_campaign_id": "CID-123",
        "remote_control_run_id": "RUN-456",
    }
    command = map_remote_command("STATUS", config)
    proc = Mock(returncode=0, stdout="ok", stderr="")
    with patch("alphaforge.remote_control.commands.subprocess.run", return_value=proc) as run:
        result = execute_remote_command(command, config=config, timeout=2.5, max_output_chars=10)
    run.assert_called_once_with(
        command.argv,
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        timeout=2.5,
    )
    assert result.command == "STATUS"
    assert result.returncode == 0
    assert result.stdout == "ok"
    assert result.stderr == ""


def test_execute_remote_command_truncates_stdout_and_stderr():
    command = map_remote_command("HEALTH", {
        "remote_control_db_path": "/tmp/POSTM0FIX.db",
        "remote_control_campaign_id": "CID-123",
        "remote_control_run_id": "RUN-456",
    })
    proc = Mock(returncode=1, stdout="x" * 50, stderr="y" * 50)
    with patch("alphaforge.remote_control.commands.subprocess.run", return_value=proc):
        result = execute_remote_command(command, config=config, max_output_chars=8)
    assert result.stdout == "xxxxxxxx"
    assert result.stderr == "yyyyyyyy"


def test_execute_remote_command_rejects_unmapped_argv():
    command = map_remote_command("STATUS", {
        "remote_control_db_path": "/tmp/POSTM0FIX.db",
        "remote_control_campaign_id": "CID-123",
        "remote_control_run_id": "RUN-456",
    })
    bad = type(command)(name=command.name, argv=command.argv[:-1] + ("WRONG",))
    with pytest.raises(CommandParseError):
        execute_remote_command(bad, config=config)
