from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alphaforge.remote_control.commands import RemoteControlCommand, RemoteControlResult


TELEGRAM_QUERY_COMMANDS = frozenset({"REPORT", "REJECTS", "LABELS", "ERRORS"})
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_PENDING_LABEL_STATES = {"PENDING", "READY", "RESOLVING"}
_RESOLVED_LABEL_STATES = {"RESOLVED", "AMBIGUOUS"}


class TelegramQueryError(RuntimeError):
    pass


def execute_telegram_query(
    command: RemoteControlCommand,
    *,
    config: Mapping[str, str],
    timeout: float = 5.0,
    max_output_chars: int = 3900,
) -> RemoteControlResult:
    """Run one fixed, read-only Telegram diagnostic against trusted campaign identity."""
    if not isinstance(command, RemoteControlCommand) or command.name not in TELEGRAM_QUERY_COMMANDS:
        raise TelegramQueryError("UNSUPPORTED_COMMAND")
    if command.argv != (command.name,):
        raise TelegramQueryError("UNSUPPORTED_COMMAND")

    db_path = _required(config, "remote_control_db_path")
    campaign_id = _required(config, "remote_control_campaign_id")
    run_id = _required(config, "remote_control_run_id")
    path = Path(db_path).expanduser()
    if not path.is_file():
        raise TelegramQueryError("DATABASE_UNAVAILABLE")

    sqlite_timeout = min(max(float(timeout), 0.05), 5.0)
    try:
        conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=sqlite_timeout)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute(f"PRAGMA busy_timeout={int(sqlite_timeout * 1000)}")
        try:
            identity = _verify_identity(conn, campaign_id=campaign_id, run_id=run_id)
            if command.name == "REPORT":
                text = _report(conn, identity)
            elif command.name == "REJECTS":
                text = _rejects(conn, identity)
            elif command.name == "LABELS":
                text = _labels(conn, identity)
            else:
                text = _errors(conn, identity)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        reason = "DB_LOCKED" if "locked" in str(exc).lower() else "DATABASE_UNAVAILABLE"
        raise TelegramQueryError(reason) from None

    return RemoteControlResult(
        command=command.name,
        returncode=0,
        stdout=text[: max(1, int(max_output_chars))],
        stderr="",
    )


def _required(config: Mapping[str, str], key: str) -> str:
    if not isinstance(config, Mapping):
        raise TelegramQueryError("TRUSTED_CONFIG_UNAVAILABLE")
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TelegramQueryError("TRUSTED_CONFIG_UNAVAILABLE")
    return value.strip()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _verify_identity(conn: sqlite3.Connection, *, campaign_id: str, run_id: str) -> dict[str, Any]:
    tables = _tables(conn)
    if not {"burnin_campaigns", "burnin_campaign_runs"} <= tables:
        raise TelegramQueryError("SCHEMA_UNAVAILABLE")
    campaign_cols = _columns(conn, "burnin_campaigns")
    run_cols = _columns(conn, "burnin_campaign_runs")
    if not {"campaign_id", "campaign_status", "active_run_id"} <= campaign_cols:
        raise TelegramQueryError("SCHEMA_UNAVAILABLE")
    if not {"campaign_id", "burnin_run_id", "status"} <= run_cols:
        raise TelegramQueryError("SCHEMA_UNAVAILABLE")

    campaign = conn.execute(
        "SELECT * FROM burnin_campaigns WHERE campaign_id=?",
        (campaign_id,),
    ).fetchone()
    if campaign is None:
        raise TelegramQueryError("CAMPAIGN_ID_MISMATCH")
    run = conn.execute(
        "SELECT * FROM burnin_campaign_runs WHERE campaign_id=? AND burnin_run_id=?",
        (campaign_id, run_id),
    ).fetchone()
    if run is None:
        raise TelegramQueryError("RUN_ID_MISMATCH")

    campaign_dict = dict(campaign)
    run_dict = dict(run)
    active_run_id = campaign_dict.get("active_run_id")
    if active_run_id and str(active_run_id) != run_id:
        raise TelegramQueryError("STALE_TRUSTED_RUN_ID")
    return {
        "campaign_id": campaign_id,
        "run_id": run_id,
        "campaign": campaign_dict,
        "run": run_dict,
        "tables": tables,
    }


def _campaign_run_ids(conn: sqlite3.Connection, campaign_id: str) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in conn.execute(
            "SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id=? ORDER BY continuation_sequence",
            (campaign_id,),
        ).fetchall()
    )


def _decision_counts(conn: sqlite3.Connection, campaign_id: str) -> tuple[int | None, int | None, int | None]:
    if "burnin_observations" not in _tables(conn):
        return None, None, None
    columns = _columns(conn, "burnin_observations")
    if not {"burnin_run_id", "decision"} <= columns:
        return None, None, None
    run_ids = _campaign_run_ids(conn, campaign_id)
    if not run_ids:
        return 0, 0, 0
    marks = ",".join("?" for _ in run_ids)
    rows = conn.execute(
        f"SELECT UPPER(COALESCE(decision,'')),COUNT(*) FROM burnin_observations "
        f"WHERE burnin_run_id IN ({marks}) GROUP BY UPPER(COALESCE(decision,''))",
        run_ids,
    ).fetchall()
    counts = {str(row[0]): int(row[1]) for row in rows}
    total = sum(counts.values())
    accepted = counts.get("ACCEPT", 0) + counts.get("ACCEPTED", 0)
    rejected = counts.get("REJECTED", 0)
    return total, accepted, rejected


def _position_counts(conn: sqlite3.Connection, campaign_id: str) -> tuple[int | None, int | None]:
    if "burnin_pending_position_outcomes" not in _tables(conn):
        return None, None
    columns = _columns(conn, "burnin_pending_position_outcomes")
    if not {"campaign_id", "status"} <= columns:
        return None, None
    rows = conn.execute(
        "SELECT UPPER(COALESCE(status,'')),COUNT(*) FROM burnin_pending_position_outcomes "
        "WHERE campaign_id=? GROUP BY UPPER(COALESCE(status,''))",
        (campaign_id,),
    ).fetchall()
    counts = {str(row[0]): int(row[1]) for row in rows}
    return counts.get("OPEN", 0), counts.get("CLOSED", 0) + counts.get("RESOLVED", 0)


def _label_counts(conn: sqlite3.Connection, campaign_id: str) -> dict[str, int] | None:
    if "burnin_pending_reject_labels" not in _tables(conn):
        return None
    columns = _columns(conn, "burnin_pending_reject_labels")
    if not {"campaign_id", "status"} <= columns:
        return None
    rows = conn.execute(
        "SELECT UPPER(COALESCE(status,'')),COUNT(*) FROM burnin_pending_reject_labels "
        "WHERE campaign_id=? GROUP BY UPPER(COALESCE(status,''))",
        (campaign_id,),
    ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def _report(conn: sqlite3.Connection, identity: Mapping[str, Any]) -> str:
    campaign_id = str(identity["campaign_id"])
    run_id = str(identity["run_id"])
    campaign = identity["campaign"]
    total, accepted, rejected = _decision_counts(conn, campaign_id)
    open_positions, closed_positions = _position_counts(conn, campaign_id)
    labels = _label_counts(conn, campaign_id)

    pending = None if labels is None else sum(labels.get(state, 0) for state in _PENDING_LABEL_STATES)
    resolved = None if labels is None else sum(labels.get(state, 0) for state in _RESOLVED_LABEL_STATES)
    failed = None if labels is None else labels.get("FAILED", 0)

    return (
        f"REPORT campaign={_token(campaign_id)} run={_token(run_id)} "
        f"status={_token(campaign.get('campaign_status'))} "
        f"decisions={_number(total)} accepted={_number(accepted)} rejected={_number(rejected)} "
        f"positions_open={_number(open_positions)} positions_closed={_number(closed_positions)} "
        f"labels_pending={_number(pending)} labels_resolved={_number(resolved)} labels_failed={_number(failed)}"
    )


def _rejects(conn: sqlite3.Connection, identity: Mapping[str, Any]) -> str:
    campaign_id = str(identity["campaign_id"])
    if "burnin_observations" not in identity["tables"]:
        return "REJECTS availability=UNAVAILABLE_IN_SCHEMA"
    columns = _columns(conn, "burnin_observations")
    if not {"burnin_run_id", "decision", "metrics_json"} <= columns:
        return "REJECTS availability=UNAVAILABLE_IN_SCHEMA"
    run_ids = _campaign_run_ids(conn, campaign_id)
    if not run_ids:
        return "REJECTS total=0 top=none"
    marks = ",".join("?" for _ in run_ids)
    rows = conn.execute(
        f"SELECT metrics_json FROM burnin_observations WHERE burnin_run_id IN ({marks}) "
        "AND UPPER(COALESCE(decision,''))='REJECTED'",
        run_ids,
    ).fetchall()

    reasons: Counter[str] = Counter()
    for row in rows:
        try:
            payload = json.loads(row[0] or "{}")
        except (TypeError, json.JSONDecodeError):
            reasons["MALFORMED_METRICS_JSON"] += 1
            continue
        reason = payload.get("reject_reason") if isinstance(payload, dict) else None
        reasons[_token(reason or "MISSING_REJECT_REASON")] += 1

    top = ",".join(f"{reason}:{count}" for reason, count in reasons.most_common(5)) or "none"
    return f"REJECTS total={len(rows)} top={top}"


def _labels(conn: sqlite3.Connection, identity: Mapping[str, Any]) -> str:
    campaign_id = str(identity["campaign_id"])
    labels = _label_counts(conn, campaign_id)
    if labels is None:
        return "LABELS availability=UNAVAILABLE_IN_SCHEMA"

    overdue = 0
    columns = _columns(conn, "burnin_pending_reject_labels")
    if "due_at" in columns:
        rows = conn.execute(
            "SELECT status,due_at FROM burnin_pending_reject_labels WHERE campaign_id=?",
            (campaign_id,),
        ).fetchall()
        now = datetime.now(timezone.utc)
        for row in rows:
            status = str(row[0] or "").upper()
            if status not in _PENDING_LABEL_STATES:
                continue
            due = _parse_time(row[1])
            if due is not None and due <= now:
                overdue += 1

    pending = sum(labels.get(state, 0) for state in _PENDING_LABEL_STATES)
    resolved = sum(labels.get(state, 0) for state in _RESOLVED_LABEL_STATES)
    return (
        f"LABELS pending={pending} resolved={resolved} overdue={overdue} "
        f"failed={labels.get('FAILED', 0)} expired={labels.get('EXPIRED', 0)} "
        f"ambiguous={labels.get('AMBIGUOUS', 0)}"
    )


def _errors(conn: sqlite3.Connection, identity: Mapping[str, Any]) -> str:
    campaign_id = str(identity["campaign_id"])
    campaign = identity["campaign"]
    last_error = _token(campaign.get("last_error") or "NONE")
    incidents: list[str] = []
    if "burnin_ops_incidents" in identity["tables"]:
        columns = _columns(conn, "burnin_ops_incidents")
        required = {"campaign_id", "incident_type", "severity", "status", "id"}
        if required <= columns:
            rows = conn.execute(
                "SELECT incident_type,severity,status FROM burnin_ops_incidents "
                "WHERE campaign_id=? ORDER BY id DESC LIMIT 5",
                (campaign_id,),
            ).fetchall()
            incidents = [
                f"{_token(row[0])}/{_token(row[1])}/{_token(row[2])}"
                for row in rows
            ]
    recent = ",".join(incidents) if incidents else "none"
    return f"ERRORS last_error={last_error} recent_incidents={recent}"


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _token(value: object) -> str:
    text = str(value or "UNKNOWN").strip()
    return text if _SAFE_TOKEN.fullmatch(text) else "UNSTRUCTURED"


def _number(value: int | None) -> str:
    return "UNAVAILABLE" if value is None else str(int(value))
