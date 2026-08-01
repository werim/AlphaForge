"""Read-only PAPER burn-in observability and guarded canonical CLI controls."""
from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse

from alphaforge.burnin_ops import ACTIVE_CAMPAIGN_STATUSES, CONFIG_DRIFT_REASONS, _age, _pid_alive

CAMPAIGN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SECRET = re.compile(r"(?i)(authorization|api[_-]?key|secret|token|password)(\s*[:=]\s*|\s+)([^\s,;]+)")
REQUIRED = {
    "burnin_campaigns": {"campaign_id", "campaign_status", "release_id", "active_run_id"},
    "burnin_campaign_runs": {"campaign_id", "burnin_run_id", "continuation_sequence", "status"},
}


class ControlError(Exception):
    def __init__(self, code: str, message: str, status: int = 503, **metadata: Any):
        self.code, self.message, self.status, self.metadata = code, message, status, metadata
        super().__init__(message)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize(value: str | None, limit: int = 16_384) -> str:
    return SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", value or "")[-limit:]


class ControlCenterService:
    """Fail-closed adapter over the canonical burn-in SQLite evidence and CLI."""

    _locks_guard = threading.Lock()
    _locks: dict[str, threading.Lock] = {}

    def __init__(self, db_path: Path, project_root: Path, python: Path, token: str | None, *, timeout: float = 30.0):
        self.db_path, self.project_root, self.python, self.token, self.timeout = db_path, project_root, python, token, timeout
        self.audit_path = project_root / "artifacts" / "burnin" / "control_center_operations.jsonl"

    @classmethod
    def from_environment(cls, database_url: str | None = None) -> "ControlCenterService":
        raw_db = os.getenv("ALPHAFORGE_DB_PATH")
        if not raw_db and database_url and database_url.startswith(("sqlite:///", "sqlite+pysqlite:///")):
            raw_db = database_url.split("///", 1)[1]
        root = Path(os.getenv("ALPHAFORGE_PROJECT_ROOT", Path.cwd())).expanduser().resolve()
        return cls(Path(raw_db).expanduser().resolve() if raw_db else Path("/__missing_alphaforge_db__"), root,
                   Path(os.getenv("ALPHAFORGE_PYTHON_EXECUTABLE", sys.executable)).expanduser().resolve(),
                   os.getenv("ALPHAFORGE_CONTROL_TOKEN"))

    def validate(self, *, controls: bool = False) -> None:
        if os.getenv("ALPHAFORGE_EXECUTION_MODE", os.getenv("EXECUTION_MODE", "PAPER")).upper() != "PAPER":
            raise ControlError("INVALID_STATE_TRANSITION", "Control Center is PAPER-only", 403)
        if not self.db_path.is_file():
            raise ControlError("DATA_UNAVAILABLE", "ALPHAFORGE_DB_PATH must identify an existing file")
        if not self.project_root.is_dir():
            raise ControlError("DATA_UNAVAILABLE", "ALPHAFORGE_PROJECT_ROOT is invalid")
        if not self.python.is_file() or (os.name != "nt" and not os.access(self.python, os.X_OK)):
            raise ControlError("BACKEND_UNREACHABLE", "ALPHAFORGE_PYTHON_EXECUTABLE is not executable")
        if controls and not self.token:
            raise ControlError("BACKEND_UNREACHABLE", "ALPHAFORGE_CONTROL_TOKEN is not configured", 503)

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
            self._schema(conn)
            return [dict(r) for r in conn.execute("SELECT * FROM burnin_campaigns ORDER BY created_at DESC, id DESC")]

    def active(self) -> dict[str, Any]:
        with self.connect() as conn:
            self._schema(conn)
            marks = ",".join("?" for _ in ACTIVE_CAMPAIGN_STATUSES)
            rows = conn.execute(f"SELECT * FROM burnin_campaigns WHERE campaign_status IN ({marks}) ORDER BY created_at DESC", tuple(sorted(ACTIVE_CAMPAIGN_STATUSES))).fetchall()
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
            healthy = bool(campaign.get("campaign_status") == "RUNNING" and pid and alive and hb_age is not None and hb_age <= 120)
            return {"campaign": campaign, "runs": runs, "continuation_count": len(runs), "metrics": counts,
                    "metrics_availability": "AVAILABLE" if counts is not None else "UNAVAILABLE_IN_SCHEMA",
                    "worker": {"pid": pid, "process_exists": alive, "started_at": campaign.get("worker_started_at"), "last_heartbeat_at": heartbeat,
                               "heartbeat_age_seconds": hb_age, "health": "HEALTHY" if healthy else "UNKNOWN"},
                    "config_drift": campaign.get("last_error") in CONFIG_DRIFT_REASONS,
                    "recovery_required": campaign.get("campaign_status") == "RECOVERY_REQUIRED",
                    "duplicate_continuation_sequence": len({r["continuation_sequence"] for r in runs}) != len(runs),
                    "aggregate_contamination": any("aggregate" in r["burnin_run_id"].lower() for r in runs)}

    def rows(self, campaign_id: str, kind: str, limit: int = 200) -> dict[str, Any]:
        specs = {
            "rejects": ("burnin_pending_reject_labels", "decision_timestamp"),
            "positions": ("burnin_pending_position_outcomes", "created_at"),
            "events": ("burnin_campaign_events", "event_time"),
        }
        table, order = specs[kind]
        with self.connect() as conn:
            schema = self._schema(conn); self._campaign(conn, campaign_id)
            if table not in schema or "campaign_id" not in schema[table]:
                return {"availability": "UNAVAILABLE_IN_SCHEMA", "items": None}
            items = [dict(r) for r in conn.execute(f'SELECT * FROM "{table}" WHERE campaign_id=? ORDER BY "{order}" DESC LIMIT ?', (campaign_id, min(max(limit, 1), 500)))]
            if kind == "rejects":
                for item in items:
                    item["reject_reason_quality"] = "MISSING" if not str(item.get("reject_reason") or "").strip() else "AVAILABLE"
            return {"availability": "AVAILABLE", "items": items}

    def preflight(self) -> dict[str, Any]:
        with self.connect() as conn:
            schema = self._schema(conn)
            if "burnin_preflight_reports" not in schema:
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
        return {"logs": result, "availability": "AVAILABLE" if any(v is not None for v in result.values()) else "DATA_UNAVAILABLE"}

    def control(self, campaign_id: str, operation: str, supplied_token: str | None) -> dict[str, Any]:
        self.validate(controls=True); self._valid_id(campaign_id)
        if not supplied_token or not hmac.compare_digest(supplied_token, self.token or ""):
            raise ControlError("BACKEND_UNREACHABLE", "Invalid control token", 401)
        active = self.active()
        if active["campaign_id"] != campaign_id:
            raise ControlError("CAMPAIGN_ID_MISMATCH", "Requested campaign is not the active campaign", 409)
        before = self.status(campaign_id); state = before["campaign"]["campaign_status"]
        expected = "PAUSED" if operation == "pause" else "RUNNING"
        if operation == "pause" and state != "RUNNING": raise ControlError("INVALID_STATE_TRANSITION", "Pause requires RUNNING", 409)
        if operation == "resume" and state != "PAUSED": raise ControlError("INVALID_STATE_TRANSITION", "Resume requires PAUSED", 409)
        if operation == "resume" and (before["recovery_required"] or before["config_drift"]): raise ControlError("RECOVERY_REQUIRED", "Resume is blocked by recovery/config drift", 409)
        with self._locks_guard: lock = self._locks.setdefault(campaign_id, threading.Lock())
        if not lock.acquire(blocking=False): raise ControlError("INVALID_STATE_TRANSITION", "Another campaign operation is in progress", 409)
        record = {"operation_id": str(uuid.uuid4()), "operation": operation, "campaign_id": campaign_id, "requested_at": _now(), "started_at": _now(), "previous_status": state, "expected_status": expected}
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
        except ControlError as exc:
            record["result"] = exc.code; raise
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
        generated = _now(); observed = generated
        return {"data": data, "source": source, "observed_at": observed, "generated_at": generated, "age_seconds": 0.0, "is_stale": False}
    @api.get("/health")
    def health():
        service.validate();
        with service.connect() as conn: service._schema(conn); conn.execute("SELECT 1").fetchone()
        return envelope({"status": "AVAILABLE", "paper_only": True})
    @api.get("/runtime")
    def runtime():
        try: active = service.active(); status = service.status(active["campaign_id"])
        except ControlError as exc:
            if exc.code != "NO_ACTIVE_CAMPAIGN": raise
            active, status = None, None
        return envelope({"database_accessible": True, "execution_mode": "PAPER", "active_campaign": active, "campaign_status": status})
    @api.get("/campaigns")
    def campaigns(): return envelope(service.campaigns())
    @api.get("/campaigns/active")
    def active(): return envelope(service.active())
    @api.get("/campaigns/{campaign_id}/status")
    def status(campaign_id: str): return envelope(service.status(campaign_id))
    @api.get("/campaigns/{campaign_id}/rejects")
    def rejects(campaign_id: str, limit: int = Query(200, ge=1, le=500)): return envelope(service.rows(campaign_id, "rejects", limit))
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
