from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from alphaforge.remote_control.commands import RemoteControlCommand
from alphaforge.remote_control.telegram_queries import TelegramQueryError, execute_telegram_query


def _database(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    path = tmp_path / "campaign.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE burnin_campaigns(
                campaign_id TEXT PRIMARY KEY,
                campaign_status TEXT NOT NULL,
                active_run_id TEXT,
                last_error TEXT
            );
            CREATE TABLE burnin_campaign_runs(
                campaign_id TEXT NOT NULL,
                burnin_run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                continuation_sequence INTEGER NOT NULL
            );
            CREATE TABLE burnin_observations(
                burnin_run_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                metrics_json TEXT
            );
            CREATE TABLE burnin_pending_position_outcomes(
                campaign_id TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE burnin_pending_reject_labels(
                campaign_id TEXT NOT NULL,
                status TEXT NOT NULL,
                due_at TEXT
            );
            CREATE TABLE burnin_ops_incidents(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id TEXT NOT NULL,
                incident_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                status TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO burnin_campaigns VALUES(?,?,?,?)",
            ("camp_1", "RUNNING", "run_1", "NONE"),
        )
        conn.execute(
            "INSERT INTO burnin_campaign_runs VALUES(?,?,?,?)",
            ("camp_1", "run_1", "RUNNING", 0),
        )
        conn.executemany(
            "INSERT INTO burnin_observations VALUES(?,?,?)",
            [
                ("run_1", "ACCEPTED", "{}"),
                ("run_1", "REJECTED", '{"reject_reason":"LOW_SCORE"}'),
                ("run_1", "REJECTED", '{"reject_reason":"LOW_SCORE"}'),
                ("run_1", "REJECTED", '{"reject_reason":"LOW_EFFECTIVE_RR"}'),
            ],
        )
        conn.executemany(
            "INSERT INTO burnin_pending_position_outcomes VALUES(?,?)",
            [("camp_1", "OPEN"), ("camp_1", "CLOSED")],
        )
        conn.executemany(
            "INSERT INTO burnin_pending_reject_labels VALUES(?,?,?)",
            [
                ("camp_1", "PENDING", "2000-01-01T00:00:00Z"),
                ("camp_1", "RESOLVED", "2000-01-01T00:00:00Z"),
                ("camp_1", "AMBIGUOUS", "2000-01-01T00:00:00Z"),
                ("camp_1", "FAILED", "2000-01-01T00:00:00Z"),
            ],
        )
        conn.execute(
            "INSERT INTO burnin_ops_incidents(campaign_id,incident_type,severity,status) VALUES(?,?,?,?)",
            ("camp_1", "SQLITE_BUSY", "WARNING", "RECOVERED"),
        )
    return path, {
        "remote_control_db_path": str(path),
        "remote_control_campaign_id": "camp_1",
        "remote_control_run_id": "run_1",
    }


def _run(name: str, config: dict[str, str]):
    return execute_telegram_query(
        RemoteControlCommand(name=name, argv=(name,)),
        config=config,
    )


def test_report_is_compact_and_campaign_scoped(tmp_path: Path) -> None:
    _, config = _database(tmp_path)
    result = _run("REPORT", config)

    assert result.returncode == 0
    assert result.stdout == (
        "REPORT campaign=camp_1 run=run_1 status=RUNNING decisions=4 accepted=1 rejected=3 "
        "positions_open=1 positions_closed=1 labels_pending=1 labels_resolved=2 labels_failed=1"
    )


def test_rejects_reports_top_canonical_reasons(tmp_path: Path) -> None:
    _, config = _database(tmp_path)
    result = _run("REJECTS", config)

    assert result.returncode == 0
    assert "REJECTS total=3" in result.stdout
    assert "LOW_SCORE:2" in result.stdout
    assert "LOW_EFFECTIVE_RR:1" in result.stdout


def test_labels_exposes_pipeline_and_overdue_without_fabricating_zero_for_missing_schema(tmp_path: Path) -> None:
    path, config = _database(tmp_path)
    result = _run("LABELS", config)

    assert result.stdout == "LABELS pending=1 resolved=2 overdue=1 failed=1 expired=0 ambiguous=1"

    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE burnin_pending_reject_labels")
    unavailable = _run("LABELS", config)
    assert unavailable.stdout == "LABELS availability=UNAVAILABLE_IN_SCHEMA"


def test_errors_uses_structured_codes_only(tmp_path: Path) -> None:
    path, config = _database(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE burnin_campaigns SET last_error=? WHERE campaign_id='camp_1'",
            ("/private/tmp/token=secret traceback",),
        )

    result = _run("ERRORS", config)

    assert "last_error=UNSTRUCTURED" in result.stdout
    assert "SQLITE_BUSY/WARNING/RECOVERED" in result.stdout
    assert "/private/tmp" not in result.stdout
    assert "secret" not in result.stdout


def test_stale_or_wrong_trusted_identity_fails_closed(tmp_path: Path) -> None:
    path, config = _database(tmp_path)
    wrong_run = dict(config, remote_control_run_id="run_missing")
    with pytest.raises(TelegramQueryError, match="RUN_ID_MISMATCH"):
        _run("REPORT", wrong_run)

    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO burnin_campaign_runs VALUES(?,?,?,?)",
            ("camp_1", "run_2", "RUNNING", 1),
        )
        conn.execute(
            "UPDATE burnin_campaigns SET active_run_id='run_2' WHERE campaign_id='camp_1'"
        )
    with pytest.raises(TelegramQueryError, match="STALE_TRUSTED_RUN_ID"):
        _run("REPORT", config)


def test_query_path_is_read_only_and_does_not_change_database_bytes(tmp_path: Path) -> None:
    path, config = _database(tmp_path)
    before = path.read_bytes()

    for command in ("REPORT", "REJECTS", "LABELS", "ERRORS"):
        _run(command, config)

    assert path.read_bytes() == before


def test_tampered_query_argv_is_rejected_before_database_access(tmp_path: Path) -> None:
    _, config = _database(tmp_path)
    with pytest.raises(TelegramQueryError, match="UNSUPPORTED_COMMAND"):
        execute_telegram_query(
            RemoteControlCommand(name="REPORT", argv=("REPORT", "--db", "/tmp/attacker.db")),
            config=config,
        )
