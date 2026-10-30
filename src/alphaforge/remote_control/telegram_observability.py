from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alphaforge.remote_control.commands import RemoteControlCommand, RemoteControlResult
from alphaforge.remote_control.telegram_queries import TELEGRAM_QUERY_COMMANDS as BASE_QUERY_COMMANDS
from alphaforge.remote_control.telegram_queries import execute_telegram_query as execute_base_query


TELEGRAM_OBSERVABILITY_COMMANDS = frozenset({"STATUS", "HEALTH", "PREFLIGHT"})
TELEGRAM_QUERY_COMMANDS = BASE_QUERY_COMMANDS | {"PREFLIGHT"}


class TelegramObservabilityError(RuntimeError):
    pass


def execute_telegram_observability(
    command: RemoteControlCommand,
    *,
    config: Mapping[str, str],
    timeout: float = 5.0,
    max_output_chars: int = 3900,
) -> RemoteControlResult:
    if command.name in BASE_QUERY_COMMANDS:
        return execute_base_query(
            command,
            config=config,
            timeout=timeout,
            max_output_chars=max_output_chars,
        )
    if command.name not in TELEGRAM_OBSERVABILITY_COMMANDS or command.argv != (command.name,):
        raise TelegramObservabilityError("UNSUPPORTED_COMMAND")

    path = Path(_required(config, "remote_control_db_path")).expanduser()
    campaign_id = _required(config, "remote_control_campaign_id")
    run_id = _required(config, "remote_control_run_id")
    if not path.is_file():
        raise TelegramObservabilityError("DATABASE_UNAVAILABLE")
    sqlite_timeout = min(max(float(timeout), 0.05), 5.0)
    try:
        conn = sqlite3.connect(
            f"file:{path.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=sqlite_timeout,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute(f"PRAGMA busy_timeout={int(sqlite_timeout * 1000)}")
        try:
            campaign, run = _identity(conn, campaign_id, run_id)
            if command.name == "STATUS":
                output = _status(campaign, run)
            elif command.name == "HEALTH":
                output = _health(conn, campaign_id, campaign)
            else:
                output = _preflight(conn, campaign_id)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        reason = "DB_LOCKED" if "locked" in str(exc).lower() else "DATABASE_UNAVAILABLE"
        raise TelegramObservabilityError(reason) from None
    return RemoteControlResult(command.name, 0, output[:max_output_chars], "")


def _required(config: Mapping[str, str], key: str) -> str:
    value = config.get(key) if isinstance(config, Mapping) else None
    if not isinstance(value, str) or not value.strip():
        raise TelegramObservabilityError("TRUSTED_CONFIG_UNAVAILABLE")
    return value.strip()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _identity(
    conn: sqlite3.Connection,
    campaign_id: str,
    run_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not _table_exists(conn, "burnin_campaigns") or not _table_exists(conn, "burnin_campaign_runs"):
        raise TelegramObservabilityError("SCHEMA_UNAVAILABLE")
    campaign = conn.execute(
        "SELECT * FROM burnin_campaigns WHERE campaign_id=?",
        (campaign_id,),
    ).fetchone()
    run = conn.execute(
        "SELECT * FROM burnin_campaign_runs WHERE campaign_id=? AND burnin_run_id=?",
        (campaign_id, run_id),
    ).fetchone()
    if campaign is None:
        raise TelegramObservabilityError("CAMPAIGN_ID_MISMATCH")
    if run is None:
        raise TelegramObservabilityError("RUN_ID_MISMATCH")
    campaign_data = dict(campaign)
    run_data = dict(run)
    if str(campaign_data.get("active_run_id") or "") != run_id:
        raise TelegramObservabilityError("STALE_TRUSTED_RUN_ID")
    return campaign_data, run_data


def _status(campaign: Mapping[str, Any], run: Mapping[str, Any]) -> str:
    campaign_status = _safe(campaign.get("campaign_status"))
    run_status = _safe(run.get("status"))
    heartbeat_age = _age_seconds(campaign.get("last_heartbeat_at"))
    recovery = (
        campaign_status == "RECOVERY_REQUIRED"
        or "RECOVERY" in str(campaign.get("last_error") or "").upper()
    )
    pid = campaign.get("worker_pid")
    if pid in (None, ""):
        worker = "UNKNOWN" if campaign_status in {"RUNNING", "STARTING"} else "NONE"
    else:
        try:
            from alphaforge.process_liveness import process_is_alive
            worker = "ALIVE" if process_is_alive(pid) else "DEAD"
        except Exception:
            worker = "UNKNOWN"
    return (
        f"STATUS campaign_status={campaign_status} run_status={run_status} "
        f"worker={worker} heartbeat_age_s={_num(heartbeat_age)} "
        f"restart_count={_num(campaign.get('restart_count'))} "
        f"recovery_required={'YES' if recovery else 'NO'}"
    )


def _health(
    conn: sqlite3.Connection,
    campaign_id: str,
    campaign: Mapping[str, Any],
) -> str:
    runtime: dict[str, Any] | None = None
    if _table_exists(conn, "runtime_state_snapshots"):
        cols = _columns(conn, "runtime_state_snapshots")
        if "campaign_id" in cols:
            order = "id DESC" if "id" in cols else ("timestamp DESC" if "timestamp" in cols else "")
            sql = "SELECT * FROM runtime_state_snapshots WHERE campaign_id=?"
            if order:
                sql += f" ORDER BY {order}"
            sql += " LIMIT 1"
            row = conn.execute(sql, (campaign_id,)).fetchone()
            runtime = dict(row) if row else None

    reconciliation = runtime.get("reconciliation_status") if runtime else None
    runtime_status = (runtime.get("runtime_status") or runtime.get("status")) if runtime else None
    recovery = (
        str(campaign.get("campaign_status") or "").upper() == "RECOVERY_REQUIRED"
        or "RECOVERY" in str(campaign.get("last_error") or "").upper()
        or bool(runtime and runtime.get("recovery_action_required"))
    )
    persistence: str | None = None
    market_data: str | None = None
    if runtime:
        try:
            diagnostics = json.loads(runtime.get("diagnostics_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            diagnostics = {}
        if isinstance(diagnostics, dict):
            metrics = diagnostics.get("metrics")
            if isinstance(metrics, dict) and "heartbeat_persistence_degraded" in metrics:
                persistence = "DEGRADED" if bool(metrics.get("heartbeat_persistence_degraded")) else "HEALTHY"
            market = diagnostics.get("market_data")
            if isinstance(market, dict):
                market_data = str(market.get("health_status") or market.get("status") or "") or None

    labels_pending: int | None = None
    if _table_exists(conn, "burnin_pending_reject_labels"):
        cols = _columns(conn, "burnin_pending_reject_labels")
        if {"campaign_id", "status"} <= cols:
            row = conn.execute(
                "SELECT COUNT(*) FROM burnin_pending_reject_labels "
                "WHERE campaign_id=? AND UPPER(status) IN ('PENDING','READY','RESOLVING')",
                (campaign_id,),
            ).fetchone()
            labels_pending = int(row[0])

    blockers: list[str] = []
    if recovery:
        blockers.append("RECOVERY_REQUIRED")
    if runtime is None:
        blockers.append("RUNTIME_EVIDENCE_UNAVAILABLE")
    if reconciliation is None:
        blockers.append("RECONCILIATION_UNAVAILABLE")
    elif str(reconciliation).upper() != "CLEAN":
        blockers.append("RECONCILIATION_NOT_CLEAN")
    if persistence is None:
        blockers.append("PERSISTENCE_EVIDENCE_UNAVAILABLE")
    elif persistence != "HEALTHY":
        blockers.append("PERSISTENCE_DEGRADED")
    if market_data is None:
        blockers.append("MARKET_DATA_EVIDENCE_UNAVAILABLE")
    elif market_data.upper() in {"DEGRADED", "UNAVAILABLE"}:
        blockers.append("MARKET_DATA_" + market_data.upper())
    if labels_pending is None:
        blockers.append("LABEL_EVIDENCE_UNAVAILABLE")
    action = "RECOVERY_REQUIRED" if recovery else ("INSPECT" if blockers else "CONTINUE")
    return (
        f"HEALTH runtime={_safe(runtime_status) if runtime_status else 'UNAVAILABLE'} "
        f"reconciliation={_safe(reconciliation) if reconciliation else 'UNAVAILABLE'} "
        f"persistence={persistence or 'UNAVAILABLE'} market_data={_safe(market_data) if market_data else 'UNAVAILABLE'} "
        f"labels_pending={_num(labels_pending)} blockers={','.join(blockers) if blockers else 'none'} "
        f"action={action}"
    )


def _preflight(conn: sqlite3.Connection, campaign_id: str) -> str:
    if not _table_exists(conn, "burnin_preflight_reports"):
        return "PREFLIGHT availability=UNAVAILABLE_IN_SCHEMA"
    cols = _columns(conn, "burnin_preflight_reports")
    if not {"status", "blockers_json"} <= cols:
        return "PREFLIGHT availability=UNAVAILABLE_IN_SCHEMA"
    where = " WHERE campaign_id=?" if "campaign_id" in cols else ""
    params: tuple[Any, ...] = (campaign_id,) if where else ()
    order = "id DESC" if "id" in cols else ("generated_at DESC" if "generated_at" in cols else "")
    sql = "SELECT * FROM burnin_preflight_reports" + where
    if order:
        sql += f" ORDER BY {order}"
    row = conn.execute(sql + " LIMIT 1", params).fetchone()
    if row is None:
        return "PREFLIGHT availability=NO_EVIDENCE"
    data = dict(row)
    try:
        blockers = json.loads(data.get("blockers_json") or "[]")
        if not isinstance(blockers, list):
            blockers = ["MALFORMED_BLOCKERS"]
    except (TypeError, json.JSONDecodeError):
        blockers = ["MALFORMED_BLOCKERS"]
    return (
        f"PREFLIGHT status={_safe(data.get('status'))} "
        f"blockers={','.join(_safe(item) for item in blockers[:5]) if blockers else 'none'}"
    )


def _age_seconds(value: object) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()))
    except ValueError:
        return None


def _safe(value: object) -> str:
    text = str(value or "UNKNOWN").strip()
    return "".join(ch if ch.isalnum() or ch in "_.:-" else "_" for ch in text)[:128] or "UNKNOWN"


def _num(value: object) -> str:
    if value is None:
        return "UNAVAILABLE"
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "UNAVAILABLE"
