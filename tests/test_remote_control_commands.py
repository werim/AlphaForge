from __future__ import annotations

import pytest

from alphaforge.remote_control.commands import CommandParseError, map_remote_command, parse_remote_command


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

