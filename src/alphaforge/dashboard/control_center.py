"""Read-only PAPER burn-in observability and guarded canonical CLI controls."""
from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse

from alphaforge.burnin_ops import ACTIVE_CAMPAIGN_STATUSES, CONFIG_DRIFT_REASONS, _pid_alive

CAMPAIGN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SECRET = re.compile(r"(?i)(authorization|api[_-]?key|secret|token|password)(\s*[:=]\s*|\s+)([^\s,;]+)")
REQUIRED = {
    "burnin_campaigns": {"campaign_id", "campaign_status", "release_id", "active_run_id"},
    "burnin_campaign_runs": {"campaign_id", "burnin_run_id", "continuation_sequence", "status"},
}
RECOVERY_EVENT_TYPES = {"RECOVERY_REQUIRED"}
RECOVERY_RUN_STATUSES = {"RECOVERY_REQUIRED"}


class ControlError(Exception):
    def __init__(self, code: str, message: str, status: int = 503, **metadata: Any):
        self.code, self.message, self.status, self.metadata = code, message, status, metadata
        super().__init__(message)


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().isoformat()


def _sanitize(value: str | None, limit: int = 16_384) -> str:
    return SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", value or "")[-limit:]


def _freshness(value: Any, source: str, threshold: float, *, availability: str = "AVAILABLE") -> dict[str, Any]:
    base = {"observed_at": None, "age_seconds": None, "is_stale": None, "source": source, "availability": availability}
    if value in (None, ""):
        base["availability"] = "DATA_UNAVAILABLE" if availability == "AVAILABLE" else availability
        return base
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return {**base, "availability": "INVALID_TIMESTAMP", "error": "TIMESTAMP_INVALID"}
    age = (_now_dt() - parsed).total_seconds()
    if age < -1.0:
        return {**base, "observed_at": parsed.isoformat(), "age_seconds": age, "availability": "CLOCK_SKEW", "error": "TIMESTAMP_IN_FUTURE"}
    return {**base, "observed_at": parsed.isoformat(), "age_seconds": max(age, 0.0), "is_stale": age > threshold}


def _sqlite_error(exc: sqlite3.Error) -> ControlError:
    locked = "locked" in str(exc).lower()
    return ControlError("DB_LOCKED" if locked else "BACKEND_UNREACHABLE", "Runtime database query failed", 503)


class _CampaignLock:
    """Atomic directory lease shared by Uvicorn processes; no SQLite mutation."""

    def __init__(self, path: Path, operation_id: str, stale_after: float):
        self.path, self.operation_id, self.stale_after, self.acquired = path, operation_id, stale_after, False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                self.path.mkdir()
                (self.path / "owner.json").write_text(json.dumps({"pid": os.getpid(), "operation_id": self.operation_id, "created_at": _now()}), encoding="utf-8")
                self.acquired = True
                return
            except FileExistsError:
                stale = False
                try:
                    owner = json.loads((self.path / "owner.json").read_text(encoding="utf-8"))
                    created = datetime.fromisoformat(str(owner["created_at"]).replace("Z", "+00:00"))
                    age = (_now_dt() - created.astimezone(timezone.utc)).total_seconds()
                    stale = age > self.stale_after or not _pid_alive(int(owner["pid"]))
                except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                    # An incomplete atomic acquisition is removable only after its directory mtime ages out.
                    try: stale = time.time() - self.path.stat().st_mtime > self.stale_after
                    except OSError: stale = False
                if stale and attempt == 0:
                    shutil.rmtree(self.path, ignore_errors=True)
                    continue
                raise ControlError("INVALID_STATE_TRANSITION", "Another campaign operation is in progress", 409, reason="PROCESS_SAFE_LOCK_HELD") from None
            except OSError as exc:
                raise ControlError("BACKEND_UNREACHABLE", "Campaign operation lock is unavailable", 503) from exc

    def release(self) -> None:
        if self.acquired:
            shutil.rmtree(self.path, ignore_errors=True)
            self.acquired = False


class ControlCenterService:
    """Fail-closed adapter over canonical burn-in SQLite evidence and CLI."""

    def __init__(self, db_path: Path, project_root: Path, python: Path, token: str | None, *, timeout: float = 30.0, stale_after: float = 120.0):
        self.db_path, self.project_root, self.python, self.token = db_path, project_root, python, token
        self.timeout, self.stale_after = timeout, stale_after
        self.audit_path = project_root / "artifacts" / "burnin" / "control_center_operations.jsonl"
        self.lock_root = project_root / "artifacts" / "burnin" / "control_center_locks"

    @classmethod
    def from_environment(cls, database_url: str | None = None) -> "ControlCenterService":
        raw_db = os.getenv("ALPHAFORGE_DB_PATH")
        if not raw_db and database_url and database_url.startswith(("sqlite:///", "sqlite+pysqlite:///")):
            raw_db = database_url.split("///", 1)[1]
        root = Path(os.getenv("ALPHAFORGE_PROJECT_ROOT", Path.cwd())).expanduser().resolve()
        return cls(Path(raw_db).expanduser().resolve() if raw_db else Path("/__missing_alphaforge_db__"), root,
                   Path(os.getenv("ALPHAFORGE_PYTHON_EXECUTABLE", sys.executable)).expanduser().resolve(), os.getenv("ALPHAFORGE_CONTROL_TOKEN"),
                   timeout=float(os.getenv("ALPHAFORGE_CONTROL_COMMAND_TIMEOUT_SECONDS", "30")),
                   stale_after=float(os.getenv("ALPHAFORGE_CONTROL_STALE_AFTER_SECONDS", "120")))

    def validate(self, *, controls: bool = False) -> None:
        if os.getenv("ALPHAFORGE_EXECUTION_MODE", os.getenv("EXECUTION_MODE", "PAPER")).upper() != "PAPER":
            raise ControlError("INVALID_STATE_TRANSITION", "Control Center is PAPER-only", 403)
        if not self.db_path.is_file(): raise ControlError("DATA_UNAVAILABLE", "ALPHAFORGE_DB_PATH must identify an existing file")
        if not self.project_root.is_dir(): raise ControlError("DATA_UNAVAILABLE", "ALPHAFORGE_PROJECT_ROOT is invalid")
        if not self.python.is_file() or (os.name != "nt" and not os.access(self.python, os.X_OK)):
            raise ControlError("BACKEND_UNREACHABLE", "ALPHAFORGE_PYTHON_EXECUTABLE is not executable")
        if controls and not self.token: raise ControlError("BACKEND_UNREACHABLE", "ALPHAFORGE_CONTROL_TOKEN is not configured")

    def connect(self) -> sqlite3.Connection:
        self.validate()
        try:
            conn = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=0.25)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON"); conn.execute("PRAGMA busy_timeout=250")
            return conn
        except sqlite3.Error as exc: raise _sqlite_error(exc) from None

    def _query(self, conn: sqlite3.Connection, sql: str, params: Sequence[Any] = (), *, one: bool = False) -> Any:
        try:
            cursor = conn.execute(sql, tuple(params))
            return cursor.fetchone() if one else cursor.fetchall()
        except sqlite3.Error as exc: raise _sqlite_error(exc) from None

    def _schema(self, conn: sqlite3.Connection) -> dict[str, set[str]]:
        tables = {r[0] for r in self._query(conn, "SELECT name FROM sqlite_master WHERE type='table'")}
        schema = {t: {r[1] for r in self._query(conn, f'PRAGMA table_info("{t}")')} for t in tables}
        missing = {t: sorted(cols - schema.get(t, set())) for t, cols in REQUIRED.items() if cols - schema.get(t, set())}
        if missing: raise ControlError("SCHEMA_MISMATCH", "Required burn-in schema is unavailable", missing=missing)
        return schema

    @staticmethod
    def _valid_id(campaign_id: str) -> None:
        if not CAMPAIGN_ID.fullmatch(campaign_id): raise ControlError("CAMPAIGN_ID_MISMATCH", "Invalid campaign ID", 400)

    def _campaign(self, conn: sqlite3.Connection, campaign_id: str) -> dict[str, Any]:
        self._valid_id(campaign_id)
        row = self._query(conn, "SELECT * FROM burnin_campaigns WHERE campaign_id=?", (campaign_id,), one=True)
        if not row: raise ControlError("CAMPAIGN_ID_MISMATCH", "Campaign does not exist", 404)
        return dict(row)

    @staticmethod
    def _order(schema: dict[str, set[str]], table: str, candidates: Sequence[str]) -> str | None:
        return next((column for column in candidates if column in schema.get(table, set())), None)

    def campaigns(self) -> dict[str, Any]:
        with self.connect() as conn:
            schema = self._schema(conn); order = self._order(schema, "burnin_campaigns", ("created_at", "id"))
            sql = "SELECT * FROM burnin_campaigns" + (f' ORDER BY "{order}" DESC' if order else "")
            items = [dict(r) for r in self._query(conn, sql)]
            timestamp_column = "created_at" if "created_at" in schema["burnin_campaigns"] else None
            observed = max((i.get(timestamp_column) for i in items if timestamp_column and i.get(timestamp_column)), default=None)
            return {"items": items, "_freshness": {"campaigns": _freshness(observed, f"burnin_campaigns.{timestamp_column}" if timestamp_column else "burnin_campaigns", self.stale_after, availability="AVAILABLE" if timestamp_column else "UNAVAILABLE_IN_SCHEMA")}}

    def active(self) -> dict[str, Any]:
        with self.connect() as conn:
            schema = self._schema(conn); marks = ",".join("?" for _ in ACTIVE_CAMPAIGN_STATUSES)
            order = self._order(schema, "burnin_campaigns", ("created_at", "id"))
            sql = f"SELECT * FROM burnin_campaigns WHERE campaign_status IN ({marks})" + (f' ORDER BY "{order}" DESC' if order else "")
            rows = self._query(conn, sql, tuple(sorted(ACTIVE_CAMPAIGN_STATUSES)))
            if not rows: raise ControlError("NO_ACTIVE_CAMPAIGN", "No active PAPER burn-in campaign", 404)
            if len(rows) != 1: raise ControlError("SCHEMA_MISMATCH", "Multiple active campaigns violate the canonical contract", active_count=len(rows))
            return dict(rows[0])

    def _recovery(self, conn: sqlite3.Connection, schema: dict[str, set[str]], campaign: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
        cid, active_run = campaign["campaign_id"], campaign.get("active_run_id")
        evidence: list[str] = []
        if campaign.get("campaign_status") == "RECOVERY_REQUIRED": evidence.append("burnin_campaigns.campaign_status")
        active = next((r for r in runs if r.get("burnin_run_id") == active_run), None)
        if active and active.get("status") in RECOVERY_RUN_STATUSES: evidence.append("burnin_campaign_runs.status")
        if "burnin_campaign_events" in schema and {"campaign_id", "event_type", "burnin_run_id"} <= schema["burnin_campaign_events"]:
            marks = ",".join("?" for _ in RECOVERY_EVENT_TYPES)
            row = self._query(conn, f"SELECT 1 FROM burnin_campaign_events WHERE campaign_id=? AND burnin_run_id IS ? AND event_type IN ({marks}) LIMIT 1", (cid, active_run, *sorted(RECOVERY_EVENT_TYPES)), one=True)
            if row: evidence.append("burnin_campaign_events.event_type")
        runtime_cols = schema.get("runtime_state_snapshots", set())
        required = {"campaign_id", "burnin_run_id", "recovery_action_required"}
        if not required <= runtime_cols:
            return {"required": True if evidence else None, "availability": "AVAILABLE" if evidence else "UNAVAILABLE_IN_SCHEMA", "sources": evidence, "reason": "CANONICAL_RUNTIME_RECOVERY_FLAG_UNAVAILABLE" if not evidence else None}
        order = self._order(schema, "runtime_state_snapshots", ("timestamp", "id"))
        if not order:
            return {"required": True if evidence else None, "availability": "AVAILABLE" if evidence else "UNAVAILABLE_IN_SCHEMA", "sources": evidence, "reason": "CANONICAL_RUNTIME_RECOVERY_ORDER_UNAVAILABLE"}
        snap = self._query(conn, f'SELECT campaign_id,burnin_run_id,recovery_action_required FROM runtime_state_snapshots WHERE campaign_id=? AND burnin_run_id=? ORDER BY "{order}" DESC LIMIT 1', (cid, active_run), one=True)
        if not snap:
            return {"required": True if evidence else None, "availability": "AVAILABLE" if evidence else "DATA_UNAVAILABLE", "sources": evidence, "reason": "CANONICAL_RUNTIME_RECOVERY_EVIDENCE_MISSING"}
        evidence.append("runtime_state_snapshots.recovery_action_required")
        return {"required": bool(evidence[:-1]) or bool(snap["recovery_action_required"]), "availability": "AVAILABLE", "sources": evidence, "reason": None}

    def status(self, campaign_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            schema = self._schema(conn); campaign = self._campaign(conn, campaign_id)
            runs = [dict(r) for r in self._query(conn, "SELECT * FROM burnin_campaign_runs WHERE campaign_id=? ORDER BY continuation_sequence", (campaign_id,))]
            run_ids = [r["burnin_run_id"] for r in runs]; metrics = None; metrics_observed = None
            obs_cols = schema.get("burnin_observations", set())
            if {"burnin_run_id", "decision"} <= obs_cols and run_ids:
                marks = ",".join("?" for _ in run_ids)
                rows = self._query(conn, f"SELECT decision,COUNT(DISTINCT observation_id) n FROM burnin_observations WHERE burnin_run_id IN ({marks}) GROUP BY decision" if "observation_id" in obs_cols else f"SELECT decision,COUNT(*) n FROM burnin_observations WHERE burnin_run_id IN ({marks}) GROUP BY decision", tuple(run_ids))
                grouped = {str(r[0]).upper(): int(r[1]) for r in rows}; total = sum(grouped.values())
                metrics = {"total_decisions": total, "accepted_decisions": grouped.get("ACCEPTED", 0), "rejected_decisions": grouped.get("REJECTED", 0)}
                metrics.update(reject_rate=metrics["rejected_decisions"] / total if total else None, acceptance_rate=metrics["accepted_decisions"] / total if total else None)
                if "observed_at" in obs_cols:
                    metrics_observed = self._query(conn, f"SELECT MAX(observed_at) FROM burnin_observations WHERE burnin_run_id IN ({marks})", tuple(run_ids), one=True)[0]
            heartbeat = campaign.get("last_heartbeat_at") if "last_heartbeat_at" in schema["burnin_campaigns"] else None
            heartbeat_fresh = _freshness(heartbeat, "burnin_campaigns.last_heartbeat_at", self.stale_after, availability="AVAILABLE" if "last_heartbeat_at" in schema["burnin_campaigns"] else "UNAVAILABLE_IN_SCHEMA")
            pid = campaign.get("worker_pid") if "worker_pid" in schema["burnin_campaigns"] else None; alive = _pid_alive(pid) if pid else False
            healthy = bool(campaign.get("campaign_status") == "RUNNING" and pid and alive and heartbeat_fresh["is_stale"] is False)
            campaign_ts = next((campaign.get(c) for c in ("last_operator_activity_at", "completed_at", "started_at", "created_at") if c in schema["burnin_campaigns"] and campaign.get(c)), None)
            recovery = self._recovery(conn, schema, campaign, runs)
            freshness = {
                "campaign": _freshness(campaign_ts, "burnin_campaigns canonical timestamps", self.stale_after),
                "heartbeat": heartbeat_fresh,
                "metrics": _freshness(metrics_observed, "burnin_observations.observed_at", self.stale_after, availability="AVAILABLE" if "observed_at" in obs_cols else "UNAVAILABLE_IN_SCHEMA"),
            }
            return {"campaign": campaign, "runs": runs, "continuation_count": len(runs), "metrics": metrics,
                    "metrics_availability": "AVAILABLE" if metrics is not None else "UNAVAILABLE_IN_SCHEMA", "worker": {"pid": pid, "process_exists": alive, "started_at": campaign.get("worker_started_at") if "worker_started_at" in schema["burnin_campaigns"] else None, "health": "HEALTHY" if healthy else "UNKNOWN"},
                    "config_drift": campaign.get("last_error") in CONFIG_DRIFT_REASONS, "recovery_required": recovery["required"], "recovery": recovery,
                    "duplicate_continuation_sequence": len({r["continuation_sequence"] for r in runs}) != len(runs),
                    "aggregate_contamination": None, "aggregate_contamination_availability": "DATA_UNAVAILABLE", "_freshness": freshness}

    def rows(self, campaign_id: str, kind: str, limit: int = 200) -> dict[str, Any]:
        specs = {"positions": ("burnin_pending_position_outcomes", ("created_at", "entry_time", "resolved_at")), "events": ("burnin_campaign_events", ("event_time",))}
        table, candidates = specs[kind]
        with self.connect() as conn:
            schema = self._schema(conn); self._campaign(conn, campaign_id); cols = schema.get(table, set())
            if not {"campaign_id"} <= cols: return {"availability": "UNAVAILABLE_IN_SCHEMA", "items": None, "_freshness": {kind: _freshness(None, table, self.stale_after, availability="UNAVAILABLE_IN_SCHEMA")}}
            order = self._order(schema, table, candidates)
            if not order: return {"availability": "UNAVAILABLE_IN_SCHEMA", "items": None, "reason": "SAFE_ORDER_COLUMN_UNAVAILABLE", "_freshness": {kind: _freshness(None, table, self.stale_after, availability="UNAVAILABLE_IN_SCHEMA")}}
            items = [dict(r) for r in self._query(conn, f'SELECT * FROM "{table}" WHERE campaign_id=? ORDER BY "{order}" DESC LIMIT ?', (campaign_id, min(max(limit, 1), 500)))]
            observed = max((r.get(order) for r in items if r.get(order)), default=None)
            return {"availability": "AVAILABLE", "items": items, "_freshness": {kind: _freshness(observed, f"{table}.{order}", self.stale_after)}}

    def rejects(self, campaign_id: str, limit: int = 200) -> dict[str, Any]:
        """Return canonical rejected observations; pending labels remain explicitly separate queue evidence."""
        with self.connect() as conn:
            schema = self._schema(conn); self._campaign(conn, campaign_id)
            runs = [r[0] for r in self._query(conn, "SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id=?", (campaign_id,))]
            cols = schema.get("burnin_observations", set()); required = {"burnin_run_id", "decision"}
            if not runs or not required <= cols:
                return {"availability": "UNAVAILABLE_IN_SCHEMA", "items": None, "pending_label_queue": None, "_freshness": {"rejects": _freshness(None, "burnin_observations", self.stale_after, availability="UNAVAILABLE_IN_SCHEMA")}}
            marks = ",".join("?" for _ in runs); order = self._order(schema, "burnin_observations", ("observed_at", "id"))
            if not order: return {"availability": "UNAVAILABLE_IN_SCHEMA", "items": None, "reason": "SAFE_ORDER_COLUMN_UNAVAILABLE", "_freshness": {"rejects": _freshness(None, "burnin_observations", self.stale_after, availability="UNAVAILABLE_IN_SCHEMA")}}
            distinct = "observation_id" if "observation_id" in cols else None
            rows = [dict(r) for r in self._query(conn, f'SELECT * FROM burnin_observations WHERE burnin_run_id IN ({marks}) AND UPPER(decision)=? ORDER BY "{order}" DESC LIMIT ?', (*runs, "REJECTED", min(max(limit, 1), 500)))]
            if distinct:
                dedup: dict[Any, dict[str, Any]] = {}; [dedup.setdefault(r[distinct], r) for r in rows]; rows = list(dedup.values())
            reasons: dict[str, int] = {}; missing = malformed = 0
            for row in rows:
                try: metrics = json.loads(row.get("metrics_json") or "{}") if "metrics_json" in cols else {}
                except (TypeError, json.JSONDecodeError): metrics = {}; malformed += 1
                reason = metrics.get("reject_reason") if isinstance(metrics, dict) else None
                row["reject_reason"] = reason if isinstance(reason, str) and reason.strip() else None
                if row["reject_reason"] is None: missing += 1
                else: reasons[row["reject_reason"]] = reasons.get(row["reject_reason"], 0) + 1
            pending = None
            pcols = schema.get("burnin_pending_reject_labels", set())
            if {"campaign_id"} <= pcols:
                pending = int(self._query(conn, "SELECT COUNT(*) FROM burnin_pending_reject_labels WHERE campaign_id=?", (campaign_id,), one=True)[0])
            observed = max((r.get(order) for r in rows if r.get(order)), default=None)
            return {"availability": "AVAILABLE", "scope": "CANONICAL_REJECTED_OBSERVATIONS", "items": rows, "reject_total": len(rows), "reason_distribution": reasons,
                    "reason_quality": {"labelled_count": sum(reasons.values()), "missing_count": missing, "malformed_metrics_json_count": malformed, "matches_reject_total": sum(reasons.values()) == len(rows)},
                    "pending_label_queue": {"count": pending, "scope": "UNFINALIZED_FORWARD_LABEL_QUEUE"} if pending is not None else None,
                    "pending_label_queue_availability": "AVAILABLE" if pending is not None else "UNAVAILABLE_IN_SCHEMA",
                    "_freshness": {"rejects": _freshness(observed, f"burnin_observations.{order}", self.stale_after)}}

    def preflight(self) -> dict[str, Any]:
        with self.connect() as conn:
            schema = self._schema(conn); cols = schema.get("burnin_preflight_reports", set())
            if not cols: return {"availability": "UNAVAILABLE_IN_SCHEMA", "report": None, "_freshness": {"preflight": _freshness(None, "burnin_preflight_reports", self.stale_after, availability="UNAVAILABLE_IN_SCHEMA")}}
            order = self._order(schema, "burnin_preflight_reports", ("generated_at", "id"))
            if not order: return {"availability": "UNAVAILABLE_IN_SCHEMA", "report": None, "reason": "SAFE_ORDER_COLUMN_UNAVAILABLE", "_freshness": {"preflight": _freshness(None, "burnin_preflight_reports", self.stale_after, availability="UNAVAILABLE_IN_SCHEMA")}}
            row = self._query(conn, f'SELECT * FROM burnin_preflight_reports ORDER BY "{order}" DESC LIMIT 1', one=True)
            if not row: return {"availability": "DATA_UNAVAILABLE", "report": None, "_freshness": {"preflight": _freshness(None, f"burnin_preflight_reports.{order}", self.stale_after)}}
            report = dict(row)
            for key in ("blockers_json", "checks_json"):
                if key not in cols: continue
                try: report[key.removesuffix("_json")] = json.loads(report.get(key) or "null")
                except (TypeError, json.JSONDecodeError): report[key.removesuffix("_json")] = None; report[f"{key}_quality"] = "MALFORMED_JSON"
            timestamp = report.get("generated_at") if "generated_at" in cols else None
            return {"availability": "AVAILABLE", "report": report, "_freshness": {"preflight": _freshness(timestamp, "burnin_preflight_reports.generated_at" if timestamp else "burnin_preflight_reports", self.stale_after, availability="AVAILABLE" if "generated_at" in cols else "UNAVAILABLE_IN_SCHEMA")}}

    def logs(self, campaign_id: str, lines: int) -> dict[str, Any]:
        self._valid_id(campaign_id); self.status(campaign_id)
        root = (self.project_root / "artifacts" / "burnin" / campaign_id).resolve(); allowed = [root / "worker.stdout.log", root / "worker.stderr.log"]
        if not all(p.resolve().is_relative_to(root) for p in allowed): raise ControlError("CAMPAIGN_ID_MISMATCH", "Unsafe log path", 400)
        result, mtimes = {}, []
        for path in allowed:
            try:
                result[path.name] = _sanitize("\n".join(path.read_text(errors="replace").splitlines()[-min(max(lines, 1), 500):])) if path.is_file() else None
                if path.is_file(): mtimes.append(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat())
            except OSError: result[path.name] = None
        observed = max(mtimes, default=None)
        return {"logs": result, "availability": "AVAILABLE" if any(v is not None for v in result.values()) else "DATA_UNAVAILABLE", "_freshness": {"logs": _freshness(observed, "worker log file mtime", self.stale_after)}}

    def control(self, campaign_id: str, operation: str, supplied_token: str | None) -> dict[str, Any]:
        self.validate(controls=True); self._valid_id(campaign_id)
        if not supplied_token or not hmac.compare_digest(supplied_token, self.token or ""): raise ControlError("BACKEND_UNREACHABLE", "Invalid control token", 401)
        active = self.active()
        if active["campaign_id"] != campaign_id: raise ControlError("CAMPAIGN_ID_MISMATCH", "Requested campaign is not the active campaign", 409)
        before = self.status(campaign_id); state = before["campaign"]["campaign_status"]; expected = "PAUSED" if operation == "pause" else "RUNNING"
        if operation == "pause" and state != "RUNNING": raise ControlError("INVALID_STATE_TRANSITION", "Pause requires RUNNING", 409)
        if operation == "resume" and state != "PAUSED": raise ControlError("INVALID_STATE_TRANSITION", "Resume requires PAUSED", 409)
        if operation == "resume" and (before["recovery_required"] is not False or before["config_drift"]):
            raise ControlError("RECOVERY_REQUIRED", "Resume is blocked until canonical recovery evidence is clean", 409, recovery=before["recovery"])
        operation_id = str(uuid.uuid4()); lock = _CampaignLock(self.lock_root / f"{campaign_id}.lock", operation_id, max(self.timeout * 2, 60.0)); lock.acquire()
        record = {"operation_id": operation_id, "operation": operation, "campaign_id": campaign_id, "requested_at": _now(), "started_at": _now(), "previous_status": state, "expected_status": expected}
        try:
            cmd = [str(self.python), "-m", "alphaforge.burnin_cli", "--db", str(self.db_path), "--json", operation, "--campaign-id", campaign_id]
            if operation == "resume": cmd.append("--detach")
            try: completed = subprocess.run(cmd, cwd=self.project_root, capture_output=True, text=True, timeout=self.timeout, shell=False, check=False)
            except subprocess.TimeoutExpired as exc: raise ControlError("COMMAND_FAILED", "Canonical CLI timed out", 504) from exc
            record.update(exit_code=completed.returncode, sanitized_stdout=_sanitize(completed.stdout), sanitized_stderr=_sanitize(completed.stderr))
            if completed.returncode: raise ControlError("COMMAND_FAILED", "Canonical CLI returned non-zero", 502, exit_code=completed.returncode)
            after = self.status(campaign_id); verified = after["campaign"]["campaign_status"]; record["verified_status"] = verified
            if verified != expected: raise ControlError("COMMAND_FAILED", "Post-command status did not reach expected state", 502, verified_status=verified)
            if operation == "resume" and after["worker"]["health"] != "HEALTHY": raise ControlError("PARTIAL_FAILURE", "Campaign resumed but worker health is not verified", 502, verified_status=verified)
            record["result"] = "SUCCESS"; return {"operation": record, "status": after}
        except ControlError as exc: record["result"] = exc.code; raise
        finally:
            record["completed_at"] = _now()
            try:
                self.audit_path.parent.mkdir(parents=True, exist_ok=True)
                with self.audit_path.open("a", encoding="utf-8") as fh: fh.write(json.dumps(record, sort_keys=True) + "\n")
            except OSError as exc:
                if record.get("result") == "SUCCESS": raise ControlError("PARTIAL_FAILURE", "Command succeeded but audit persistence failed", 502) from exc
            finally: lock.release()


def router(service: ControlCenterService) -> APIRouter:
    api = APIRouter(prefix="/api")
    def envelope(data: Any, source: str = "canonical_sqlite") -> dict[str, Any]:
        payload = dict(data) if isinstance(data, dict) else {"items": data}; freshness = payload.pop("_freshness", {})
        primary = next(iter(freshness.values()), {})
        return {"data": payload, "source": source, "observed_at": primary.get("observed_at"), "generated_at": _now(), "age_seconds": primary.get("age_seconds"), "is_stale": primary.get("is_stale"), "freshness": freshness}
    @api.get("/health")
    def health():
        with service.connect() as conn: service._schema(conn); service._query(conn, "SELECT 1", one=True)
        return envelope({"status": "AVAILABLE", "paper_only": True, "_freshness": {"database": _freshness(None, "read-only SELECT 1", service.stale_after, availability="NOT_TIMESTAMPED")}})
    @api.get("/runtime")
    def runtime():
        try: active = service.active(); status = service.status(active["campaign_id"])
        except ControlError as exc:
            if exc.code != "NO_ACTIVE_CAMPAIGN": raise
            active, status = None, None
        fresh = status.pop("_freshness", {}) if status else {}
        return envelope({"database_accessible": True, "execution_mode": "PAPER", "active_campaign": active, "campaign_status": status, "_freshness": fresh})
    @api.get("/campaigns")
    def campaigns(): return envelope(service.campaigns())
    @api.get("/campaigns/active")
    def active():
        item = service.active(); ts = next((item.get(c) for c in ("last_heartbeat_at", "started_at", "created_at") if item.get(c)), None)
        return envelope({**item, "_freshness": {"campaign": _freshness(ts, "burnin_campaigns canonical timestamps", service.stale_after)}})
    @api.get("/campaigns/{campaign_id}/status")
    def status(campaign_id: str): return envelope(service.status(campaign_id))
    @api.get("/campaigns/{campaign_id}/rejects")
    def rejects(campaign_id: str, limit: int = Query(200, ge=1, le=500)): return envelope(service.rejects(campaign_id, limit))
    @api.get("/campaigns/{campaign_id}/positions")
    def positions(campaign_id: str, limit: int = Query(200, ge=1, le=500)): return envelope(service.rows(campaign_id, "positions", limit))
    @api.get("/campaigns/{campaign_id}/events")
    def events(campaign_id: str, limit: int = Query(200, ge=1, le=500)): return envelope(service.rows(campaign_id, "events", limit))
    @api.get("/campaigns/{campaign_id}/logs")
    def logs(campaign_id: str, lines: int = Query(100, ge=1, le=500)): return envelope(service.logs(campaign_id, lines), "bounded_runtime_logs")
    @api.get("/preflight/latest")
    def preflight(): return envelope(service.preflight())
    @api.post("/campaigns/{campaign_id}/pause")
    def pause(campaign_id: str, x_alphaforge_control_token: str | None = Header(None)): return envelope(service.control(campaign_id, "pause", x_alphaforge_control_token), "canonical_burnin_cli")
    @api.post("/campaigns/{campaign_id}/resume")
    def resume(campaign_id: str, x_alphaforge_control_token: str | None = Header(None)): return envelope(service.control(campaign_id, "resume", x_alphaforge_control_token), "canonical_burnin_cli")
    return api


def install_error_handler(app: Any) -> None:
    @app.exception_handler(ControlError)
    async def control_error(_request: Request, exc: ControlError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message, "metadata": exc.metadata}, "generated_at": _now()})
