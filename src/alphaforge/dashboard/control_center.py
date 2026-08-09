"""Read-only PAPER burn-in observability and guarded canonical CLI controls."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse

from alphaforge import __version__
from alphaforge.burnin_ops import ACTIVE_CAMPAIGN_STATUSES, CONFIG_DRIFT_REASONS, _age, _pid_alive
from alphaforge.config import load_config_from_env

CAMPAIGN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SECRET = re.compile(r"(?i)(authorization|api[_-]?key|secret|token|password)(\s*[:=]\s*|\s+)([^\s,;]+)")
AUTHORIZATION_LINE = re.compile(r"(?im)^(\s*authorization\s*:)[^\r\n]*")
REQUIRED = {
    "burnin_campaigns": {"campaign_id", "campaign_status", "release_id", "active_run_id"},
    "burnin_campaign_runs": {"campaign_id", "burnin_run_id", "continuation_sequence", "status"},
}
PAUSE_WORKER_TIMEOUT_SECONDS_DEFAULT = 2.0
PAUSE_WORKER_POLL_INTERVAL_SECONDS_DEFAULT = 0.1
OPERATION_LEASE_STALE_SECONDS_DEFAULT = 120.0
FRESHNESS_SECONDS_DEFAULT = 120.0


class ControlError(Exception):
    def __init__(self, code: str, message: str, status: int = 503, **metadata: Any):
        self.code, self.message, self.status, self.metadata = code, message, status, metadata
        super().__init__(message)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize(value: str | None, limit: int = 16_384) -> str:
    without_authorization = AUTHORIZATION_LINE.sub(r"\1 [REDACTED]", value or "")
    return SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", without_authorization)[-limit:]


def _freshness(observed_at: str | None, *, threshold_seconds: float) -> dict[str, Any]:
    if not observed_at:
        return {"observed_at": None, "age_seconds": None, "is_stale": None, "freshness_state": "DATA_UNAVAILABLE"}
    try:
        parsed = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return {"observed_at": observed_at, "age_seconds": None, "is_stale": None, "freshness_state": "INVALID_TIMESTAMP"}
    if age < 0:
        return {"observed_at": observed_at, "age_seconds": age, "is_stale": None, "freshness_state": "CLOCK_SKEW"}
    return {"observed_at": observed_at, "age_seconds": age, "is_stale": age > threshold_seconds,
            "freshness_state": "STALE" if age > threshold_seconds else "FRESH"}


class ControlCenterService:
    """Fail-closed adapter over the canonical burn-in SQLite evidence and CLI."""

    _locks_guard = threading.Lock()
    _locks: dict[str, threading.Lock] = {}

    def __init__(self, db_path: Path, project_root: Path, python: Path, token: str | None, *, timeout: float = 30.0,
                 pause_worker_timeout: float = PAUSE_WORKER_TIMEOUT_SECONDS_DEFAULT,
                 pause_worker_poll_interval: float = PAUSE_WORKER_POLL_INTERVAL_SECONDS_DEFAULT,
                 lease_stale_seconds: float = OPERATION_LEASE_STALE_SECONDS_DEFAULT,
                 freshness_seconds: float = FRESHNESS_SECONDS_DEFAULT):
        self.db_path, self.project_root, self.python, self.token, self.timeout = db_path, project_root, python, token, timeout
        self.pause_worker_timeout = max(0.0, pause_worker_timeout)
        self.pause_worker_poll_interval = max(0.01, pause_worker_poll_interval)
        self.lease_stale_seconds = max(1.0, lease_stale_seconds)
        self.freshness_seconds = max(1.0, freshness_seconds)
        self.audit_path = project_root / "artifacts" / "burnin" / "control_center_operations.jsonl"
        self.lease_root = project_root / "artifacts" / "burnin" / ".control-center-leases"

    @classmethod
    def from_environment(cls, database_url: str | None = None) -> "ControlCenterService":
        raw_db = os.getenv("ALPHAFORGE_DB_PATH")
        if not raw_db and database_url and database_url.startswith(("sqlite:///", "sqlite+pysqlite:///")):
            raw_db = database_url.split("///", 1)[1]
        root = Path(os.getenv("ALPHAFORGE_PROJECT_ROOT", Path.cwd())).expanduser().resolve()
        return cls(Path(raw_db).expanduser().resolve() if raw_db else Path("/__missing_alphaforge_db__"), root,
                   Path(os.getenv("ALPHAFORGE_PYTHON_EXECUTABLE", sys.executable)).expanduser().resolve(),
                   os.getenv("ALPHAFORGE_CONTROL_TOKEN"),
                   pause_worker_timeout=float(os.getenv("ALPHAFORGE_CONTROL_PAUSE_WORKER_TIMEOUT_SECONDS", PAUSE_WORKER_TIMEOUT_SECONDS_DEFAULT)),
                   pause_worker_poll_interval=float(os.getenv("ALPHAFORGE_CONTROL_PAUSE_WORKER_POLL_INTERVAL_SECONDS", PAUSE_WORKER_POLL_INTERVAL_SECONDS_DEFAULT)),
                   lease_stale_seconds=float(os.getenv("ALPHAFORGE_CONTROL_LEASE_STALE_SECONDS", OPERATION_LEASE_STALE_SECONDS_DEFAULT)),
                   freshness_seconds=float(os.getenv("ALPHAFORGE_CONTROL_FRESHNESS_SECONDS", FRESHNESS_SECONDS_DEFAULT)))

    def validate(self, *, controls: bool = False) -> None:
        if controls and self.execution_mode() != "PAPER":
            raise ControlError("INVALID_STATE_TRANSITION", "Control Center is PAPER-only", 403)
        if not self.db_path.is_file():
            raise ControlError("DATA_UNAVAILABLE", "ALPHAFORGE_DB_PATH must identify an existing file")
        if not self.project_root.is_dir():
            raise ControlError("DATA_UNAVAILABLE", "ALPHAFORGE_PROJECT_ROOT is invalid")
        if not self.python.is_file() or (os.name != "nt" and not os.access(self.python, os.X_OK)):
            raise ControlError("BACKEND_UNREACHABLE", "ALPHAFORGE_PYTHON_EXECUTABLE is not executable")
        if controls and not self.token:
            raise ControlError("BACKEND_UNREACHABLE", "ALPHAFORGE_CONTROL_TOKEN is not configured", 503)

    @staticmethod
    def execution_mode() -> str:
        mode = load_config_from_env().runtime.execution_mode
        return str(getattr(mode, "value", mode)).upper()

    def diagnostics(self) -> dict[str, Any]:
        def git_value(*args: str) -> str | None:
            try:
                result = subprocess.run(["git", *args], cwd=self.project_root, capture_output=True, text=True,
                                        timeout=2.0, shell=False, check=False)
                return (result.stdout.strip() or None) if result.returncode == 0 else None
            except (OSError, subprocess.SubprocessError):
                return None

        modified = datetime.fromtimestamp(self.db_path.stat().st_mtime, timezone.utc).isoformat() if self.db_path.is_file() else None
        identity = {
            "filename": self.db_path.name,
            "resolved_path_sha256": hashlib.sha256(str(self.db_path).encode("utf-8")).hexdigest(),
            "size_bytes": self.db_path.stat().st_size if self.db_path.is_file() else None,
        }
        return {"hostname": socket.gethostname(), "operating_system": platform.platform(), "backend_version": __version__,
                "git_branch": git_value("rev-parse", "--abbrev-ref", "HEAD"), "git_commit": git_value("rev-parse", "HEAD"),
                "database_modified_at": modified, "project_root": str(self.project_root), "database_identity": identity}

    def health(self) -> dict[str, Any]:
        mode = self.execution_mode()
        payload: dict[str, Any] = {"backend_status": "AVAILABLE", "database_status": "DATABASE_UNAVAILABLE",
                                   "runtime_status": "RUNTIME_NOT_RUNNING", "active_campaign_status": "NO_ACTIVE_CAMPAIGN",
                                   "worker_status": "WORKER_UNHEALTHY",
                                   "control_actions_status": "CONTROL_AVAILABLE" if mode == "PAPER" and self.token else "READ_ONLY",
                                   "execution_mode": mode, "diagnostics": self.diagnostics(),
                                   "source_freshness": {"database_file": _freshness(
                                       datetime.fromtimestamp(self.db_path.stat().st_mtime, timezone.utc).isoformat() if self.db_path.is_file() else None,
                                       threshold_seconds=self.freshness_seconds)}}
        try:
            with self.connect() as conn:
                self._schema(conn); conn.execute("SELECT 1").fetchone()
            payload["database_status"] = "AVAILABLE"
            try:
                campaign = self.active(); status = self.status(campaign["campaign_id"])
            except ControlError as exc:
                if exc.code != "NO_ACTIVE_CAMPAIGN": raise
                return payload
            payload["active_campaign_status"] = campaign["campaign_status"]
            worker = self._worker_verification(status)
            payload["worker_status"] = status["worker"]["health"]
            if campaign["campaign_status"] == "RUNNING" and status["worker"]["health"] == "HEALTHY":
                payload["runtime_status"] = "AVAILABLE"
            payload["worker_verification"] = worker
            payload["source_freshness"].update(status.get("source_freshness", {}))
        except ControlError:
            pass
        return payload

    def connect(self) -> sqlite3.Connection:
        self.validate()
        try:
            uri = f"file:{self.db_path.as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=0.25)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=250")
            return conn
        except sqlite3.OperationalError as exc:
            code = "DB_LOCKED" if "locked" in str(exc).lower() else "BACKEND_UNREACHABLE"
            raise ControlError(code, "Runtime database is unavailable") from None

    def _schema(self, conn: sqlite3.Connection) -> dict[str, set[str]]:
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            schema = {t: {r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')} for t in tables}
        except sqlite3.OperationalError as exc:
            raise ControlError("DB_LOCKED" if "locked" in str(exc).lower() else "SCHEMA_MISMATCH", "Cannot inspect runtime schema") from None
        missing = {t: sorted(cols - schema.get(t, set())) for t, cols in REQUIRED.items() if cols - schema.get(t, set())}
        if missing:
            raise ControlError("SCHEMA_MISMATCH", "Required burn-in schema is unavailable", missing=missing)
        return schema

    def _campaign(self, conn: sqlite3.Connection, campaign_id: str) -> dict[str, Any]:
        self._valid_id(campaign_id)
        row = conn.execute("SELECT * FROM burnin_campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
        if not row:
            raise ControlError("CAMPAIGN_ID_MISMATCH", "Campaign does not exist", 404)
        return dict(row)

    @staticmethod
    def _valid_id(campaign_id: str) -> None:
        if not CAMPAIGN_ID.fullmatch(campaign_id):
            raise ControlError("CAMPAIGN_ID_MISMATCH", "Invalid campaign ID", 400)

    def campaigns(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            schema = self._schema(conn); columns = schema["burnin_campaigns"]
            order = "created_at DESC,id DESC" if {"created_at", "id"} <= columns else ("id DESC" if "id" in columns else "campaign_id")
            return [dict(r) for r in conn.execute(f"SELECT * FROM burnin_campaigns ORDER BY {order}")]

    def active(self) -> dict[str, Any]:
        with self.connect() as conn:
            schema = self._schema(conn); columns = schema["burnin_campaigns"]
            marks = ",".join("?" for _ in ACTIVE_CAMPAIGN_STATUSES)
            order = "created_at DESC" if "created_at" in columns else ("id DESC" if "id" in columns else "campaign_id")
            rows = conn.execute(f"SELECT * FROM burnin_campaigns WHERE campaign_status IN ({marks}) ORDER BY {order}", tuple(sorted(ACTIVE_CAMPAIGN_STATUSES))).fetchall()
            if not rows:
                raise ControlError("NO_ACTIVE_CAMPAIGN", "No active PAPER burn-in campaign", 404)
            if len(rows) != 1:
                raise ControlError("SCHEMA_MISMATCH", "Multiple active campaigns violate the canonical contract", active_count=len(rows))
            return dict(rows[0])

    def status(self, campaign_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            schema = self._schema(conn); campaign = self._campaign(conn, campaign_id)
            runs = [dict(r) for r in conn.execute("SELECT * FROM burnin_campaign_runs WHERE campaign_id=? ORDER BY continuation_sequence", (campaign_id,))]
            run_ids = [r["burnin_run_id"] for r in runs]
            active_run = next((r for r in runs if r.get("burnin_run_id") == campaign.get("active_run_id")), None)
            counts = None
            if "burnin_observations" in schema and {"burnin_run_id", "decision"} <= schema["burnin_observations"] and run_ids:
                marks = ",".join("?" for _ in run_ids)
                rows = conn.execute(f"SELECT decision,COUNT(*) n FROM burnin_observations WHERE burnin_run_id IN ({marks}) GROUP BY decision", tuple(run_ids)).fetchall()
                grouped = {str(r[0]).upper(): int(r[1]) for r in rows}
                counts = {"total_decisions": sum(grouped.values()), "accepted_decisions": grouped.get("ACCEPTED", 0), "rejected_decisions": grouped.get("REJECTED", 0)}
                total = counts["total_decisions"]
                counts.update(reject_rate=(counts["rejected_decisions"] / total if total else None), acceptance_rate=(counts["accepted_decisions"] / total if total else None))
            pid, heartbeat = campaign.get("worker_pid"), campaign.get("last_heartbeat_at")
            hb_age = _age(heartbeat); alive = _pid_alive(pid) if pid else False
            attachment: dict[str, Any] | None = None
            event_columns = schema.get("burnin_campaign_events", set())
            if {"id", "campaign_id", "burnin_run_id", "event_type", "event_time", "details_json"} <= event_columns and campaign.get("active_run_id"):
                rows = conn.execute("""SELECT event_time,details_json,burnin_run_id FROM burnin_campaign_events
                                       WHERE campaign_id=? AND event_type='PHASE8_CAMPAIGN_ATTACHED'
                                         AND (? IS NULL OR event_time>=?) ORDER BY id DESC""",
                                    (campaign_id, campaign.get("worker_started_at"), campaign.get("worker_started_at"))).fetchall()
                for row in rows:
                    try: details = json.loads(row["details_json"] or "{}")
                    except (TypeError, json.JSONDecodeError): details = None
                    if isinstance(details, dict) and details.get("active_run_id") == campaign.get("active_run_id"):
                        attachment = {"event_time": row["event_time"], "burnin_run_id": row["burnin_run_id"], "details": details,
                                      "available": True}
                        break
            attachment_verified = bool(
                attachment and attachment["available"] and attachment["details"].get("active_run_id") == campaign.get("active_run_id")
                and attachment["details"].get("runtime_instance_id") and campaign.get("worker_started_at")
                and attachment["event_time"] >= campaign.get("worker_started_at")
                and _freshness(attachment["event_time"], threshold_seconds=self.freshness_seconds)["freshness_state"] in {"FRESH", "STALE"}
            )
            healthy = bool(campaign.get("campaign_status") == "RUNNING" and active_run and active_run.get("status") == "RUNNING"
                           and pid and alive and hb_age is not None and 0 <= hb_age <= self.freshness_seconds and attachment_verified)
            unhealthy = bool(campaign.get("campaign_status") == "RUNNING" and ((pid and not alive)
                             or (hb_age is not None and hb_age > self.freshness_seconds)))
            worker_health = "HEALTHY" if healthy else ("WORKER_UNHEALTHY" if unhealthy else "UNKNOWN")
            return {"campaign": campaign, "runs": runs, "continuation_count": len(runs), "metrics": counts,
                    "metrics_availability": "AVAILABLE" if counts is not None else "UNAVAILABLE_IN_SCHEMA",
                    "worker": {"pid": pid, "process_exists": alive, "started_at": campaign.get("worker_started_at"), "last_heartbeat_at": heartbeat,
                               "heartbeat_age_seconds": hb_age, "health": worker_health,
                               "attachment": attachment, "attachment_verified": attachment_verified},
                    "config_drift": campaign.get("last_error") in CONFIG_DRIFT_REASONS,
                    "recovery_required": campaign.get("campaign_status") == "RECOVERY_REQUIRED",
                    "duplicate_continuation_sequence": len({r["continuation_sequence"] for r in runs}) != len(runs),
                    "aggregate_contamination": None, "aggregate_contamination_availability": "DATA_UNAVAILABLE",
                    "source_freshness": {
                        "worker_heartbeat": _freshness(heartbeat, threshold_seconds=self.freshness_seconds),
                        "worker_attachment": _freshness(attachment.get("event_time") if attachment else None, threshold_seconds=self.freshness_seconds),
                    }}

    def rejects(self, campaign_id: str, limit: int = 200) -> dict[str, Any]:
        """Return canonical rejected observations; never merge pending labels."""
        with self.connect() as conn:
            schema = self._schema(conn); self._campaign(conn, campaign_id)
            columns = schema.get("burnin_observations", set())
            required = {"burnin_run_id", "decision", "metrics_json"}
            if not required <= columns:
                return {"availability": "UNAVAILABLE_IN_SCHEMA", "items": None, "reject_total": None,
                        "returned_count": 0, "limit": limit, "deduplication": {"applied": False, "key": None, "semantics": "UNAVAILABLE_IN_SCHEMA"}}
            run_ids = [r[0] for r in conn.execute("SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id=?", (campaign_id,))]
            if not run_ids:
                return {"availability": "AVAILABLE", "items": [], "reject_total": 0, "returned_count": 0, "limit": limit,
                        "deduplication": {"applied": "observation_id" in columns, "key": "observation_id" if "observation_id" in columns else None,
                                            "semantics": "DISTINCT_CANONICAL_OBSERVATIONS" if "observation_id" in columns else "RAW_OBSERVATION_ROWS"},
                        "reason_distribution_scope": "campaign_distribution", "reason_distribution": [], "pagination": {"has_more": False}}
            marks = ",".join("?" for _ in run_ids); params = tuple(run_ids)
            has_id = "observation_id" in columns
            identity = "observation_id" if has_id else "_source_rowid"
            # observation_id is the canonical persisted identity. Historical schemas
            # without it deliberately receive raw-row semantics; rowid is ordering
            # only and is never represented as a deduplication key.
            cte = (f"WITH rejected AS (SELECT * FROM (SELECT *,ROW_NUMBER() OVER (PARTITION BY observation_id ORDER BY rowid DESC) rn "
                   f"FROM burnin_observations WHERE burnin_run_id IN ({marks}) AND UPPER(COALESCE(decision,''))='REJECTED') WHERE rn=1)" if has_id else
                   f"WITH rejected AS (SELECT *,rowid AS _source_rowid FROM burnin_observations WHERE burnin_run_id IN ({marks}) AND UPPER(COALESCE(decision,''))='REJECTED')")
            total = int(conn.execute(cte + " SELECT COUNT(*) FROM rejected", params).fetchone()[0])
            reason_sql = cte + """ SELECT
                CASE WHEN json_valid(metrics_json) THEN json_extract(metrics_json,'$.reject_reason') ELSE NULL END reason,
                CASE WHEN NOT json_valid(metrics_json) THEN 'MALFORMED_METRICS_JSON'
                     WHEN json_type(metrics_json,'$.reject_reason') IS NULL OR TRIM(COALESCE(json_extract(metrics_json,'$.reject_reason'),''))='' THEN 'MISSING'
                     ELSE 'EXPLICIT' END quality, COUNT(*) count
                FROM rejected GROUP BY reason,quality ORDER BY count DESC,reason"""
            distribution = []
            for row in conn.execute(reason_sql, params):
                distribution.append({"reason": row[0], "reason_quality": row[1], "count": int(row[2]),
                                     "rate": (int(row[2]) / total if total else None)})
            select_columns = sorted(columns)
            time_order = '"observed_at" DESC,' if "observed_at" in columns else ""
            selected_columns_sql = ",".join('"' + column + '"' for column in select_columns)
            page_sql = cte + f' SELECT {selected_columns_sql} FROM rejected ORDER BY {time_order}"{identity}" DESC LIMIT ?'
            items = [dict(r) for r in conn.execute(page_sql, (*params, min(max(limit, 1), 500)))]
            for item in items:
                try: metrics = json.loads(item.get("metrics_json") or "{}")
                except (TypeError, json.JSONDecodeError): metrics = None
                reason = metrics.get("reject_reason") if isinstance(metrics, dict) else None
                item["reject_reason"] = reason
                item["reject_reason_quality"] = "MALFORMED_METRICS_JSON" if metrics is None else ("MISSING" if reason is None or not str(reason).strip() else "EXPLICIT")
            return {"availability": "AVAILABLE", "items": items, "reject_total": total, "returned_count": len(items), "limit": limit,
                    "deduplication": {"applied": has_id, "key": "observation_id" if has_id else None,
                                        "semantics": "DISTINCT_CANONICAL_OBSERVATIONS" if has_id else "RAW_OBSERVATION_ROWS_NO_RELIABLE_UNIQUE_KEY"},
                    "reason_distribution_scope": "campaign_distribution", "reason_distribution": distribution,
                    "pagination": {"has_more": total > len(items)},
                    "source_observed_at": items[0].get("observed_at") if items and "observed_at" in columns else None}

    def rows(self, campaign_id: str, kind: str, limit: int = 200) -> dict[str, Any]:
        specs = {
            "positions": ("burnin_pending_position_outcomes", ("created_at", "entry_time", "resolved_at")),
            "events": ("burnin_campaign_events", ("event_time",)),
        }
        table, timestamp_candidates = specs[kind]
        with self.connect() as conn:
            schema = self._schema(conn); self._campaign(conn, campaign_id)
            if table not in schema or "campaign_id" not in schema[table]:
                return {"availability": "UNAVAILABLE_IN_SCHEMA", "items": None}
            columns = schema[table]
            timestamp = next((candidate for candidate in timestamp_candidates if candidate in columns), None)
            order = f'"{timestamp}" DESC' if timestamp else ('"id" DESC' if "id" in columns else None)
            if order is None:
                return {"availability": "UNAVAILABLE_IN_SCHEMA", "items": None, "source_observed_at": None}
            items = [dict(r) for r in conn.execute(f'SELECT * FROM "{table}" WHERE campaign_id=? ORDER BY {order} LIMIT ?', (campaign_id, min(max(limit, 1), 500)))]
            return {"availability": "AVAILABLE", "items": items,
                    "source_observed_at": items[0].get(timestamp) if items and timestamp else None}

    def preflight(self) -> dict[str, Any]:
        with self.connect() as conn:
            schema = self._schema(conn)
            if "burnin_preflight_reports" not in schema or not {"id", "generated_at"} <= schema["burnin_preflight_reports"]:
                return {"availability": "UNAVAILABLE_IN_SCHEMA", "report": None}
            row = conn.execute("SELECT * FROM burnin_preflight_reports ORDER BY generated_at DESC,id DESC LIMIT 1").fetchone()
            if not row: return {"availability": "DATA_UNAVAILABLE", "report": None}
            report = dict(row)
            for key in ("blockers_json", "checks_json"):
                try: report[key.removesuffix("_json")] = json.loads(report.get(key) or "null")
                except (TypeError, json.JSONDecodeError): report[key.removesuffix("_json")] = None; report[f"{key}_quality"] = "MALFORMED_JSON"
            return {"availability": "AVAILABLE", "report": report}

    def logs(self, campaign_id: str, lines: int) -> dict[str, Any]:
        self._valid_id(campaign_id); self.status(campaign_id)
        root = (self.project_root / "artifacts" / "burnin" / campaign_id).resolve()
        allowed = [root / "worker.stdout.log", root / "worker.stderr.log"]
        if not all(p.resolve().is_relative_to(root) for p in allowed):
            raise ControlError("CAMPAIGN_ID_MISMATCH", "Unsafe log path", 400)
        result = {}
        for path in allowed:
            result[path.name] = _sanitize("\n".join(path.read_text(errors="replace").splitlines()[-min(max(lines, 1), 500):])) if path.is_file() else None
        modified = max((path.stat().st_mtime for path in allowed if path.is_file()), default=None)
        return {"logs": result, "availability": "AVAILABLE" if any(v is not None for v in result.values()) else "DATA_UNAVAILABLE",
                "source_observed_at": datetime.fromtimestamp(modified, timezone.utc).isoformat() if modified is not None else None}

    def _lease_path(self, campaign_id: str) -> Path:
        self._valid_id(campaign_id)
        path = (self.lease_root / campaign_id).resolve()
        if not path.is_relative_to(self.lease_root.resolve()):
            raise ControlError("CAMPAIGN_ID_MISMATCH", "Invalid campaign lease identity", 400)
        return path

    @staticmethod
    def _lease_metadata(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads((path / "owner.json").read_text(encoding="utf-8"))
            return value if isinstance(value, dict) and isinstance(value.get("owner_token"), str) else None
        except (OSError, json.JSONDecodeError):
            return None

    def _write_lease(self, path: Path, owner_token: str) -> None:
        path.mkdir()
        temp = path / f"owner.{owner_token}.tmp"
        temp.write_text(json.dumps({"owner_token": owner_token, "started_at": _now()}), encoding="utf-8")
        temp.replace(path / "owner.json")

    def _acquire_lease(self, campaign_id: str, owner_token: str) -> Path:
        self.lease_root.mkdir(parents=True, exist_ok=True)
        path = self._lease_path(campaign_id)
        try:
            self._write_lease(path, owner_token)
            return path
        except FileExistsError:
            metadata = self._lease_metadata(path)
            if metadata is None:
                raise ControlError("INVALID_STATE_TRANSITION", "Campaign operation lease is unavailable", 409) from None
            try:
                started = datetime.fromisoformat(str(metadata["started_at"]).replace("Z", "+00:00"))
                stale = (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds() > self.lease_stale_seconds
            except (KeyError, TypeError, ValueError):
                stale = False
            if not stale:
                raise ControlError("INVALID_STATE_TRANSITION", "Another campaign operation is in progress", 409)
            quarantine = path.with_name(f"{path.name}.stale.{uuid.uuid4().hex}")
            try:
                path.replace(quarantine)
            except OSError:
                raise ControlError("INVALID_STATE_TRANSITION", "Another campaign operation is in progress", 409) from None
            moved = self._lease_metadata(quarantine)
            if moved is None or moved.get("owner_token") != metadata.get("owner_token"):
                if not path.exists():
                    try: quarantine.replace(path)
                    except OSError: pass
                raise ControlError("INVALID_STATE_TRANSITION", "Campaign operation lease changed during takeover", 409)
            try:
                self._write_lease(path, owner_token)
            except OSError:
                if not path.exists():
                    try: quarantine.replace(path)
                    except OSError: pass
                raise ControlError("INVALID_STATE_TRANSITION", "Another campaign operation is in progress", 409) from None
            shutil.rmtree(quarantine, ignore_errors=True)
            return path

    def _release_lease(self, path: Path, owner_token: str) -> bool:
        """Release through an owner-verified rename; never delete a replacement lease."""
        metadata = self._lease_metadata(path)
        if metadata is None or metadata.get("owner_token") != owner_token:
            return False
        quarantine = path.with_name(f"{path.name}.release.{owner_token}")
        try:
            path.replace(quarantine)
        except OSError:
            return False
        moved = self._lease_metadata(quarantine)
        if moved is None or moved.get("owner_token") != owner_token:
            if not path.exists():
                try: quarantine.replace(path)
                except OSError: pass
            return False
        shutil.rmtree(quarantine, ignore_errors=True)
        return True

    @staticmethod
    def _worker_verification(status: dict[str, Any]) -> dict[str, Any]:
        campaign, worker, runs = status["campaign"], status["worker"], status["runs"]
        active_run = next((r for r in runs if r.get("burnin_run_id") == campaign.get("active_run_id")), None)
        pid, alive = worker.get("pid"), worker.get("process_exists")
        run_state = active_run.get("status") if active_run else None
        source = "burnin_campaigns.worker_pid+process_existence+burnin_campaign_runs.active_run"
        if pid and alive:
            state = "ACTIVE"
        elif run_state == "PAUSED" and (not pid or alive is False):
            state = "STOPPED"
        else:
            state = "UNKNOWN"
        return {"status": state, "pid": pid, "process_exists": alive, "active_run_id": campaign.get("active_run_id"),
                "active_run_status": run_state, "worker_started_at": worker.get("started_at"), "source": source}

    def _poll_pause_postcondition(self, campaign_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        deadline = time.monotonic() + self.pause_worker_timeout
        while True:
            status = self.status(campaign_id)
            worker = self._worker_verification(status)
            if status["campaign"].get("campaign_status") == "PAUSED" and worker["status"] == "STOPPED":
                return status, worker
            if time.monotonic() >= deadline:
                return status, worker
            time.sleep(min(self.pause_worker_poll_interval, max(0.0, deadline - time.monotonic())))

    def control(self, campaign_id: str, operation: str, supplied_token: str | None) -> dict[str, Any]:
        self.validate(controls=True); self._valid_id(campaign_id)
        if not supplied_token or not hmac.compare_digest(supplied_token, self.token or ""):
            raise ControlError("BACKEND_UNREACHABLE", "Invalid control token", 401)
        with self._locks_guard: lock = self._locks.setdefault(campaign_id, threading.Lock())
        if not lock.acquire(blocking=False): raise ControlError("INVALID_STATE_TRANSITION", "Another campaign operation is in progress", 409)
        operation_id = str(uuid.uuid4()); lease_path: Path | None = None
        record = {"operation_id": operation_id, "operation": operation, "campaign_id": campaign_id, "requested_at": _now(), "started_at": _now(),
                  "previous_status": "UNKNOWN", "previous_campaign_status": "UNKNOWN",
                  "expected_status": "PAUSED" if operation == "pause" else "RUNNING",
                  "previous_worker_status": "UNKNOWN", "worker_verification_source": "DATA_UNAVAILABLE",
                  "worker_verification_timeout": self.pause_worker_timeout if operation == "pause" else 0.0,
                  "verified_campaign_status": "UNKNOWN", "verified_worker_status": "UNKNOWN", "result": "COMMAND_FAILED"}
        try:
            lease_path = self._acquire_lease(campaign_id, operation_id)
            active = self.active()
            if active["campaign_id"] != campaign_id:
                raise ControlError("CAMPAIGN_ID_MISMATCH", "Requested campaign is not the active campaign", 409)
            before = self.status(campaign_id); state = before["campaign"]["campaign_status"]
            expected = record["expected_status"]; previous_worker = self._worker_verification(before)
            record.update(previous_status=state, previous_campaign_status=state,
                          previous_worker_status=previous_worker["status"], worker_verification_source=previous_worker["source"])
            if operation == "pause" and state != "RUNNING":
                raise ControlError("INVALID_STATE_TRANSITION", "Pause requires RUNNING", 409)
            if operation == "resume":
                last_error = str(before["campaign"].get("last_error") or "")
                if before["recovery_required"] or "RECOVERY" in last_error:
                    raise ControlError("RECOVERY_REQUIRED", "Resume is blocked by canonical recovery state", 409)
                if before["config_drift"]:
                    raise ControlError("INVALID_STATE_TRANSITION", "Resume is blocked by config drift", 409)
                if state != "PAUSED":
                    raise ControlError("INVALID_STATE_TRANSITION", "Resume requires PAUSED", 409)
                active_mappings = [r for r in before["runs"] if r.get("burnin_run_id") == before["campaign"].get("active_run_id")]
                if len(active_mappings) != 1 or active_mappings[0].get("status") != "PAUSED" or before["duplicate_continuation_sequence"]:
                    raise ControlError("RECOVERY_REQUIRED", "Active continuation identity is inconsistent", 409)
                if previous_worker["status"] != "STOPPED":
                    raise ControlError("RECOVERY_REQUIRED", "Worker stop identity is not verified", 409)
            cmd = [str(self.python), "-m", "alphaforge.burnin_cli", "--db", str(self.db_path), "--json", operation, "--campaign-id", campaign_id]
            if operation == "resume": cmd.append("--detach")
            try: completed = subprocess.run(cmd, cwd=self.project_root, capture_output=True, text=True, timeout=self.timeout, shell=False, check=False)
            except subprocess.TimeoutExpired as exc: raise ControlError("COMMAND_FAILED", "Canonical CLI timed out", 504) from exc
            record.update(exit_code=completed.returncode, sanitized_stdout=_sanitize(completed.stdout), sanitized_stderr=_sanitize(completed.stderr))
            if completed.returncode: raise ControlError("COMMAND_FAILED", "Canonical CLI returned non-zero", 502, exit_code=completed.returncode)
            if operation == "pause":
                try: after, worker_after = self._poll_pause_postcondition(campaign_id)
                except Exception as exc:
                    record.update(verified_campaign_status="UNKNOWN", verified_worker_status="UNKNOWN", result="PARTIAL_FAILURE")
                    raise ControlError("PARTIAL_FAILURE", "Campaign command completed but worker verification failed", 502,
                                       verification_error=_sanitize(f"{exc.__class__.__name__}:{exc}")) from None
            else:
                after = self.status(campaign_id); worker_after = self._worker_verification(after)
            verified = after["campaign"]["campaign_status"]
            record.update(verified_status=verified, verified_campaign_status=verified, verified_worker_status=worker_after["status"],
                          worker_verification_source=worker_after["source"])
            if verified != expected: raise ControlError("COMMAND_FAILED", "Post-command status did not reach expected state", 502, verified_status=verified)
            if operation == "pause" and worker_after["status"] != "STOPPED":
                raise ControlError("PARTIAL_FAILURE", "Campaign paused but worker stop/detach is not verified", 502,
                                   verified_campaign_status=verified, verified_worker_status=worker_after["status"], worker=worker_after)
            if operation == "resume" and after["worker"]["health"] != "HEALTHY": raise ControlError("PARTIAL_FAILURE", "Campaign resumed but worker health is not verified", 502, verified_status=verified)
            record["result"] = "SUCCESS"; return {"operation": record, "status": after}
        except ControlError as exc:
            record["result"] = exc.code; raise
        finally:
            record["completed_at"] = _now()
            try:
                self.audit_path.parent.mkdir(parents=True, exist_ok=True)
                with self.audit_path.open("a", encoding="utf-8") as fh: fh.write(json.dumps(record, sort_keys=True) + "\n")
            except OSError as exc:
                if record.get("result") == "SUCCESS": raise ControlError("PARTIAL_FAILURE", "Command succeeded but audit persistence failed", 502) from exc
            finally:
                if lease_path is not None:
                    self._release_lease(lease_path, operation_id)
                lock.release()


def router(service: ControlCenterService) -> APIRouter:
    api = APIRouter(prefix="/api")
    def envelope(data: Any, source: str = "canonical_sqlite", *, observed_at: str | None = None) -> dict[str, Any]:
        freshness = _freshness(observed_at, threshold_seconds=service.freshness_seconds)
        return {"data": data, "source": source, "generated_at": _now(), **freshness,
                "availability": "AVAILABLE" if freshness["freshness_state"] in {"FRESH", "STALE"} else freshness["freshness_state"]}
    @api.get("/health")
    def health():
        return envelope(service.health(), "control_center_health")
    @api.get("/runtime/status")
    @api.get("/runtime")
    def runtime():
        try: active = service.active(); status = service.status(active["campaign_id"])
        except ControlError as exc:
            if exc.code != "NO_ACTIVE_CAMPAIGN": raise
            active, status = None, None
        return envelope({"database_accessible": True, "execution_mode": service.execution_mode(), "active_campaign": active, "campaign_status": status})
    @api.get("/campaigns")
    def campaigns(): return envelope(service.campaigns())
    @api.get("/campaigns/current")
    @api.get("/campaigns/active")
    def active(): return envelope(service.active())
    @api.get("/campaigns/{campaign_id}")
    @api.get("/campaigns/{campaign_id}/status")
    def status(campaign_id: str): return envelope(service.status(campaign_id))
    @api.get("/campaigns/{campaign_id}/rejects")
    def rejects(campaign_id: str, limit: int = Query(200, ge=1, le=500)):
        data = service.rejects(campaign_id, limit); return envelope(data, observed_at=data.get("source_observed_at"))
    @api.get("/campaigns/{campaign_id}/positions")
    def positions(campaign_id: str, limit: int = Query(200, ge=1, le=500)):
        data = service.rows(campaign_id, "positions", limit); return envelope(data, observed_at=data.get("source_observed_at"))
    @api.get("/campaigns/{campaign_id}/events")
    def events(campaign_id: str, limit: int = Query(200, ge=1, le=500)):
        data = service.rows(campaign_id, "events", limit); return envelope(data, observed_at=data.get("source_observed_at"))
    @api.get("/campaigns/{campaign_id}/logs")
    def logs(campaign_id: str, lines: int = Query(100, ge=1, le=500)):
        data = service.logs(campaign_id, lines); return envelope(data, "bounded_runtime_logs", observed_at=data.get("source_observed_at"))
    @api.get("/preflight/latest")
    def preflight():
        data = service.preflight(); report = data.get("report") or {}; return envelope(data, observed_at=report.get("generated_at"))
    @api.post("/campaigns/{campaign_id}/pause")
    def pause(campaign_id: str, x_alphaforge_control_token: str | None = Header(None)): return envelope(service.control(campaign_id, "pause", x_alphaforge_control_token), "canonical_burnin_cli")
    @api.post("/campaigns/{campaign_id}/resume")
    def resume(campaign_id: str, x_alphaforge_control_token: str | None = Header(None)): return envelope(service.control(campaign_id, "resume", x_alphaforge_control_token), "canonical_burnin_cli")
    return api


def install_error_handler(app: Any) -> None:
    @app.exception_handler(ControlError)
    async def control_error(_request: Request, exc: ControlError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message, "metadata": exc.metadata}, "generated_at": _now()})
