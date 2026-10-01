from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from alphaforge.remote_control.audit import SQLiteReplayStore
from alphaforge.remote_control.commands import RemoteControlCommand, RemoteControlResult
from alphaforge.remote_control.telegram_adapter import TelegramRemoteControlConfig, process_telegram_update
from alphaforge.remote_control.telegram_control import SQLiteTelegramControlStore, TelegramControlError
from alphaforge.remote_control.telegram_controller import process_telegram_request
from alphaforge.remote_control.telegram_observability import execute_telegram_observability


def _update(text: str, update_id: int) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "text": text,
            "from": {"id": 42},
            "chat": {"id": 9001},
        },
    }


def _request(tmp_path: Path, text: str, update_id: int):
    store = SQLiteReplayStore(tmp_path / f"replay-{update_id}.db")
    try:
        result = process_telegram_update(
            _update(text, update_id),
            config=TelegramRemoteControlConfig.from_allowlists(allowed_chat_ids=(9001,)),
            replay_store=store,
        )
    finally:
        store.close()
    assert result.accepted is True
    assert result.request is not None
    return result.request


def _config(path: Path) -> dict[str, str]:
    return {
        "remote_control_db_path": str(path),
        "remote_control_campaign_id": "camp_1",
        "remote_control_run_id": "run_1",
    }


def _campaign_db(path: Path, *, healthy_runtime: bool = True) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE burnin_campaigns(
                campaign_id TEXT PRIMARY KEY,
                campaign_status TEXT NOT NULL,
                active_run_id TEXT,
                last_error TEXT,
                last_heartbeat_at TEXT,
                worker_pid INTEGER,
                restart_count INTEGER
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
            CREATE TABLE burnin_pending_reject_labels(
                campaign_id TEXT NOT NULL,
                status TEXT NOT NULL,
                due_at TEXT
            );
            CREATE TABLE burnin_preflight_reports(
                id INTEGER PRIMARY KEY,
                campaign_id TEXT,
                status TEXT NOT NULL,
                blockers_json TEXT NOT NULL
            );
            CREATE TABLE burnin_ops_incidents(
                id INTEGER PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                incident_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                status TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO burnin_campaigns VALUES(?,?,?,?,?,?,?)",
            ("camp_1", "RUNNING", "run_1", None, None, None, 0),
        )
        conn.execute(
            "INSERT INTO burnin_campaign_runs VALUES(?,?,?,?)",
            ("camp_1", "run_1", "RUNNING", 0),
        )
        conn.execute(
            "INSERT INTO burnin_observations VALUES(?,?,?)",
            ("run_1", "REJECTED", '{"reject_reason":"LOW_SCORE"}'),
        )
        conn.execute(
            "INSERT INTO burnin_pending_reject_labels VALUES(?,?,?)",
            ("camp_1", "RESOLVED", "2000-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO burnin_preflight_reports VALUES(?,?,?,?)",
            (1, "camp_1", "PASS", "[]"),
        )
        if healthy_runtime:
            conn.executescript(
                """
                CREATE TABLE runtime_state_snapshots(
                    id INTEGER PRIMARY KEY,
                    campaign_id TEXT,
                    runtime_status TEXT,
                    reconciliation_status TEXT,
                    recovery_action_required INTEGER,
                    diagnostics_json TEXT
                );
                """
            )
            conn.execute(
                "INSERT INTO runtime_state_snapshots VALUES(?,?,?,?,?,?)",
                (
                    1,
                    "camp_1",
                    "OPERATING",
                    "CLEAN",
                    0,
                    '{"market_data":{"health_status":"VALID_EMPTY"},'
                    '"metrics":{"heartbeat_persistence_degraded":false}}',
                ),
            )


def test_confirm_and_cancel_grammar_is_strict_but_supported(tmp_path: Path) -> None:
    accepted = _request(tmp_path, "/confirm abcdefghijklmnop", 1)
    assert accepted.command == RemoteControlCommand("CONFIRM", ("CONFIRM", "abcdefghijklmnop"))

    for index, text in enumerate(
        (
            "/confirm short",
            "/confirm abcdefghijklmnop extra",
            "/confirm abcdefghijklmnop;rm",
            "/pause now",
        ),
        start=10,
    ):
        store = SQLiteReplayStore(tmp_path / f"bad-{index}.db")
        try:
            result = process_telegram_update(
                _update(text, index),
                config=TelegramRemoteControlConfig.from_allowlists(allowed_chat_ids=(9001,)),
                replay_store=store,
            )
        finally:
            store.close()
        assert result.accepted is False
        assert result.rejection_reason == "UNSUPPORTED_COMMAND"


def test_confirmed_pause_executes_once_and_replay_stays_closed(tmp_path: Path) -> None:
    db = tmp_path / "campaign.db"
    _campaign_db(db)
    control = SQLiteTelegramControlStore(
        tmp_path / "control.db",
        id_factory=lambda: "abcdefghijklmnop",
    )
    try:
        first = process_telegram_request(
            _request(tmp_path, "/pause", 100),
            config=_config(db),
            control_store=control,
        )
        assert first.returncode == 0
        assert "CONFIRM_REQUIRED id=abcdefghijklmnop operation=PAUSE" in first.stdout

        calls: list[str] = []

        def action(operation: str, **_: object) -> RemoteControlResult:
            calls.append(operation)
            return RemoteControlResult(operation, 0, "verified_status=PAUSED", "")

        confirmed = process_telegram_request(
            _request(tmp_path, "/confirm abcdefghijklmnop", 101),
            config=_config(db),
            control_store=control,
            action_executor=action,
        )
        assert confirmed.returncode == 0
        assert calls == ["PAUSE"]

        replay = process_telegram_request(
            _request(tmp_path, "/confirm abcdefghijklmnop", 102),
            config=_config(db),
            control_store=control,
            action_executor=action,
        )
        assert replay.returncode == 1
        assert replay.stderr == "CONFIRMATION_NOT_PENDING"
        assert calls == ["PAUSE"]
    finally:
        control.close()


def test_confirmation_is_bound_to_campaign_and_run_identity(tmp_path: Path) -> None:
    store = SQLiteTelegramControlStore(
        tmp_path / "control.db",
        id_factory=lambda: "abcdefghijklmnop",
    )
    try:
        pending = store.create_confirmation(
            operation="RESUME",
            campaign_id="camp_1",
            run_id="run_1",
        )
        with pytest.raises(TelegramControlError, match="CONFIRMATION_IDENTITY_MISMATCH"):
            store.consume_confirmation(
                pending.confirmation_id,
                campaign_id="camp_1",
                run_id="run_2",
            )
        with pytest.raises(TelegramControlError, match="CONFIRMATION_NOT_PENDING"):
            store.consume_confirmation(
                pending.confirmation_id,
                campaign_id="camp_1",
                run_id="run_1",
            )
    finally:
        store.close()


def test_health_is_fail_closed_when_runtime_evidence_is_missing(tmp_path: Path) -> None:
    db = tmp_path / "campaign.db"
    _campaign_db(db, healthy_runtime=False)

    result = execute_telegram_observability(
        RemoteControlCommand("HEALTH", ("HEALTH",)),
        config=_config(db),
    )

    assert "RUNTIME_EVIDENCE_UNAVAILABLE" in result.stdout
    assert "RECONCILIATION_UNAVAILABLE" in result.stdout
    assert "PERSISTENCE_EVIDENCE_UNAVAILABLE" in result.stdout
    assert "MARKET_DATA_EVIDENCE_UNAVAILABLE" in result.stdout
    assert "action=INSPECT" in result.stdout


def test_readonly_health_and_preflight_do_not_mutate_campaign_db(tmp_path: Path) -> None:
    db = tmp_path / "campaign.db"
    _campaign_db(db)
    before = db.read_bytes()

    health = execute_telegram_observability(
        RemoteControlCommand("HEALTH", ("HEALTH",)),
        config=_config(db),
    )
    preflight = execute_telegram_observability(
        RemoteControlCommand("PREFLIGHT", ("PREFLIGHT",)),
        config=_config(db),
    )

    assert "reconciliation=CLEAN" in health.stdout
    assert "persistence=HEALTHY" in health.stdout
    assert "market_data=VALID_EMPTY" in health.stdout
    assert "action=CONTINUE" in health.stdout
    assert preflight.stdout == "PREFLIGHT status=PASS blockers=none"
    assert db.read_bytes() == before
