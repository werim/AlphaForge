from __future__ import annotations

import argparse, asyncio, csv, hashlib, json, os, signal, sqlite3, subprocess, sys, time, urllib.request
from pathlib import Path, PureWindowsPath
from urllib.parse import quote
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import create_engine

from alphaforge.burnin import canonical_hash, utc_now
from alphaforge.burnin_campaign import (
    CAMPAIGN_SCHEMA_VERSION, BinanceReadOnlyCandleProvider, BurnInCampaignRunner,
    aggregate_campaign, bootstrap_campaign_schema, build_phase8_campaign_identity,
    check_campaign_completion, create_campaign, event, export_campaign_bundle,
    get_campaign, pause_campaign, qualify_campaign, start_or_resume_campaign,
    update_campaign_heartbeat, _exec,
    fail_active_campaign_run, campaign_attachment_identity, run_attachment_identity,
    identity_mismatches, load_active_campaign_attachment, ATTACHMENT_IDENTITY_FIELDS,
)
from alphaforge.config import load_config_from_env, load_reconciliation_settings
from alphaforge.config_audit import audit_config
from alphaforge.env_contract import dotenv_status
from alphaforge.runtime_state import evaluate_runtime_recovery, persist_verified_paper_recovery, persist_historical_paper_recovery_without_provider, persist_campaign_linked_zero_exposure_reconciliation_evidence
from alphaforge.runtime_state import build_readonly_reconciliation_probe
from alphaforge.persistence import init_db
from alphaforge.schema_doctor import ensure_database_schema, validate_required_schema
from alphaforge.binance_reconciliation_provider import BinanceReadonlyReconciliationConfig, BinanceReadonlyReconciliationProvider
from alphaforge.reject_label_status import reject_label_status

PHASE9_SCHEMA_VERSION = "phase9_ops_v2"
ALLOWED_FINAL_DECISIONS = {"PAPER_BURNIN_INCOMPLETE", "PAPER_BURNIN_FAILED", "PAPER_BURNIN_QUALIFIED_FOR_CANARY_REVIEW", "PAPER_BURNIN_SUSPENDED"}
VALID_INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"}
CONFIG_DRIFT_REASONS = {"CONFIG_DRIFT", "PHASE8_CAMPAIGN_CONFIG_DRIFT", "PHASE8_CAMPAIGN_STRATEGY_DRIFT", "PHASE8_CAMPAIGN_UNIVERSE_DRIFT", "PHASE8_CAMPAIGN_EXECUTION_COST_DRIFT", "PHASE8_CAMPAIGN_RELEASE_MISMATCH", "PHASE8_CAMPAIGN_EXECUTION_MODE_INVALID"}
ACTIVE_CAMPAIGN_STATUSES = {"CREATED", "STARTING", "RUNNING", "PAUSED", "RECOVERY_REQUIRED"}
TERMINAL_CAMPAIGN_STATUSES = {"COMPLETED", "FAILED", "QUALIFIED", "SUSPENDED"}
ACTIVE_RUN_STATUSES = {"STARTING", "RUNNING"}
STARTUP_FAILURE_REASONS = {"WORKER_ATTACHMENT_TIMEOUT", "WORKER_EXITED_BEFORE_ATTACHMENT", "WORKER_ATTACHMENT_INTERRUPTED", "WORKER_IDENTITY_MISMATCH", "WORKER_SPAWN_FAILED", "WORKER_STARTUP_EXITED", "LAUNCH_STARTUP_FAILED", "SYSTEM_EXIT_DURING_ATTACHMENT"}


def _db_path(args: Any) -> str:
    db = getattr(args, "db", None) or os.getenv("ALPHAFORGE_DB_PATH")
    if db:
        return str(db)
    url = load_config_from_env().persistence.database_url
    if url.startswith("sqlite+pysqlite:///"):
        return url.removeprefix("sqlite+pysqlite:///")
    if url.startswith("sqlite:///"):
        return url.removeprefix("sqlite:///")
    return "alphaforge.db"


def _connect(db: str) -> sqlite3.Connection:
    # Bootstrap the same canonical persistence database used by runtime before
    # adding burn-in operational tables.  Previously preflight could prepare
    # only ops tables and then query an obsolete core schema.
    init_db(f"sqlite+pysqlite:///{Path(db).expanduser().resolve()}").dispose()
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    bootstrap_ops_schema(conn)
    return conn


def _readonly_sqlite_uri(db: str, *, platform: str | None = None) -> str:
    """Build a SQLite read-only URI without losing Windows drive letters."""
    target_platform = platform or os.name
    if target_platform == "nt":
        raw = PureWindowsPath(db).as_posix()
        if not PureWindowsPath(db).is_absolute():
            raw = PureWindowsPath(Path(db).resolve()).as_posix()
        return f"file:{quote(raw, safe='/:')}?mode=ro"
    return f"file:{quote(Path(db).expanduser().resolve().as_posix(), safe='/:')}?mode=ro"


def _connect_readonly(db: str) -> sqlite3.Connection:
    """Open an existing SQLite database without bootstrap or journal writes."""
    path = Path(db).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"database_not_found:{path}")
    conn = sqlite3.connect(_readonly_sqlite_uri(str(path)), uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def database_diagnosis(db: str, *, max_heartbeat_age: float = 120.0) -> dict[str, Any]:
    """Return campaign state and a conservative cleanup plan without mutation."""
    try:
        conn = _connect_readonly(db)
    except Exception as exc:
        return {"status": "BLOCKED_DATABASE_STATE", "read_only": True, "database": str(db),
                "campaigns_available": False, "campaigns": None, "cleanup_plan": None,
                "query_errors": [{"source": "database", "query_error": f"{exc.__class__.__name__}:{exc}"}]}
    try:
        query_errors: list[dict[str, Any]] = []
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            schemas = {table: {row[1] for row in conn.execute(f"PRAGMA table_info({table})")} for table in tables}
        except Exception as exc:
            return {"status": "BLOCKED_DATABASE_STATE", "read_only": True, "campaigns_available": False,
                    "campaigns": None, "cleanup_plan": None,
                    "query_errors": [{"source": "schema", "query_error": f"{exc.__class__.__name__}:{exc}"}]}

        def safe_rows(source: str, table: str, columns: Sequence[str], *, where: str = "", params: Sequence[Any] = (), order: str = "") -> tuple[bool, list[dict[str, Any]] | None]:
            missing = sorted(set(columns) - schemas.get(table, set()))
            if table not in tables or missing:
                error = {"source": source, "available": False, "missing_schema": {"table": table if table not in tables else None, "columns": missing}}
                query_errors.append(error)
                return False, None
            sql = f"SELECT {','.join(columns)} FROM {table}"
            if where:
                sql += f" WHERE {where}"
            if order:
                sql += f" ORDER BY {order}"
            try:
                return True, [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]
            except Exception as exc:
                query_errors.append({"source": source, "available": False, "query_error": f"{exc.__class__.__name__}:{exc}"})
                return False, None

        campaign_columns = ["campaign_id", "campaign_status", "active_run_id", "worker_pid", "last_heartbeat_at", "release_id", "git_commit", "config_hash", "strategy_config_hash", "universe_hash", "execution_cost_config_hash"]
        if "burnin_campaigns" not in tables or "campaign_id" not in schemas.get("burnin_campaigns", set()):
            query_errors.append({"source": "campaigns", "available": False, "missing_schema": {"table": "burnin_campaigns" if "burnin_campaigns" not in tables else None, "columns": ["campaign_id"]}})
            return {"status": "BLOCKED_DATABASE_STATE", "read_only": True, "campaigns_available": False,
                    "campaigns": None, "cleanup_plan": None, "query_errors": query_errors}
        available_campaign_columns = [column for column in campaign_columns if column in schemas["burnin_campaigns"]]
        missing_campaign_columns = sorted(set(campaign_columns) - set(available_campaign_columns))
        if missing_campaign_columns:
            query_errors.append({"source": "campaigns", "available": False, "missing_schema": {"table": None, "columns": missing_campaign_columns}})
        try:
            campaign_rows = [dict(row) for row in conn.execute(f"SELECT {','.join(available_campaign_columns)} FROM burnin_campaigns ORDER BY campaign_id").fetchall()]
            for row in campaign_rows:
                for column in missing_campaign_columns:
                    row[column] = None
        except Exception as exc:
            query_errors.append({"source": "campaigns", "available": False, "query_error": f"{exc.__class__.__name__}:{exc}"})
            return {"status": "BLOCKED_DATABASE_STATE", "read_only": True, "campaigns_available": False,
                    "campaigns": None, "cleanup_plan": None, "query_errors": query_errors}

        campaigns: list[dict[str, Any]] = []
        cleanup_plan: list[dict[str, Any]] = []
        for campaign in campaign_rows or []:
            cid = campaign["campaign_id"]
            campaign_errors_start = len(query_errors)
            continuation_columns = ["campaign_id", "burnin_run_id", "continuation_sequence", "status", "started_at", "ended_at"]
            continuations_ok, continuations = safe_rows("continuations", "burnin_campaign_runs", continuation_columns, where="campaign_id=?", params=(cid,), order="continuation_sequence")
            active = next((row for row in (continuations or []) if row["burnin_run_id"] == campaign.get("active_run_id")), None) if continuations_ok else None
            pid = campaign.get("worker_pid")
            heartbeat_age = _age(campaign.get("last_heartbeat_at"))
            worker_alive = _pid_alive(pid) if pid else False
            stale = heartbeat_age is None or heartbeat_age > max_heartbeat_age
            orphaned = [row for row in (continuations or []) if row["status"] in ACTIVE_RUN_STATUSES and row["burnin_run_id"] != campaign.get("active_run_id")] if continuations_ok else None
            position_columns = ["pending_position_id", "trade_id", "burnin_run_id", "symbol", "side", "status", "entry_time"]
            positions_ok, open_positions = safe_rows("campaign_open_positions", "burnin_pending_position_outcomes", position_columns, where="campaign_id=? AND status='OPEN'", params=(cid,))
            reject_columns = ["pending_label_id", "reject_decision_id", "burnin_run_id", "symbol", "status", "due_at", "reject_reason"]
            rejects_ok, pending_rejects = safe_rows("pending_reject_labels", "burnin_pending_reject_labels", reject_columns, where="campaign_id=? AND status IN ('PENDING','READY')", params=(cid,))
            runtime_columns = ["id", "campaign_id", "burnin_run_id", "release_id", "active_position_count", "active_positions", "pending_order_count", "pending_orders", "orphan_position_count", "orphan_order_count", "unknown_exchange_state", "recovery_action_required", "reconciliation_status", "exchange_read_only_status", "diagnostics_json", "timestamp"]
            runtime_ok, runtime_rows = safe_rows("runtime_snapshot", "runtime_state_snapshots", runtime_columns, where="campaign_id=?", params=(cid,), order="id DESC")
            latest_runtime = runtime_rows[0] if runtime_ok and runtime_rows else None

            def decoded_runtime(field: str, source: str) -> tuple[bool, Any]:
                if latest_runtime is None:
                    return False, None
                try:
                    value = json.loads(latest_runtime[field])
                    if not isinstance(value, list):
                        raise ValueError("expected_json_array")
                    return True, value
                except Exception as exc:
                    query_errors.append({"source": source, "available": False, "query_error": f"{exc.__class__.__name__}:{exc}"})
                    return False, None

            runtime_positions_ok, runtime_positions = decoded_runtime("active_positions", "runtime_positions")
            runtime_orders_ok, runtime_orders = decoded_runtime("pending_orders", "runtime_pending_orders")
            try:
                diagnostics = json.loads(latest_runtime["diagnostics_json"] or "{}") if latest_runtime else None
                if latest_runtime and not isinstance(diagnostics, dict):
                    raise ValueError("expected_json_object")
            except Exception as exc:
                diagnostics = None
                query_errors.append({"source": "reconciliation_evidence", "available": False, "query_error": f"{exc.__class__.__name__}:{exc}"})
            reconciliation_ok = bool(latest_runtime and diagnostics is not None)
            reconciliation_status = latest_runtime.get("reconciliation_status") if latest_runtime else None
            recovery_required = campaign.get("campaign_status") == "RECOVERY_REQUIRED" or bool(latest_runtime and latest_runtime.get("recovery_action_required"))
            terminal = campaign.get("campaign_status") in TERMINAL_CAMPAIGN_STATUSES
            identity = {key: campaign.get(key) for key in ("release_id", "git_commit", "config_hash", "strategy_config_hash", "universe_hash", "execution_cost_config_hash")}
            lineage_matches = bool(latest_runtime and latest_runtime.get("campaign_id") == cid and latest_runtime.get("release_id") == campaign.get("release_id") and latest_runtime.get("burnin_run_id") == campaign.get("active_run_id"))
            snapshot_age = _age(latest_runtime.get("timestamp")) if latest_runtime else None
            snapshot_fresh = snapshot_age is not None and snapshot_age <= max_heartbeat_age
            orphan_ok = bool(latest_runtime and latest_runtime.get("orphan_position_count") is not None and latest_runtime.get("orphan_order_count") is not None)
            authenticated_complete = bool(reconciliation_ok and diagnostics.get("evidence_status") == "COMPLETE" and (diagnostics.get("input_source") == "AUTHENTICATED_EXCHANGE_SNAPSHOT" or diagnostics.get("authenticated") is True))
            item = {"campaign_id": cid, "campaign_status": campaign.get("campaign_status"),
                    "active_continuation": active, "continuations_available": continuations_ok, "continuations": continuations,
                    "worker": {"pid": pid, "alive": worker_alive, "last_heartbeat_at": campaign.get("last_heartbeat_at"), "heartbeat_age_seconds": heartbeat_age, "fresh": not stale},
                    "identity": identity,
                    "campaign_open_positions_available": positions_ok, "campaign_open_positions": open_positions,
                    "pending_reject_labels_available": rejects_ok, "pending_reject_labels": pending_rejects,
                    "runtime_positions_available": runtime_ok and runtime_positions_ok, "runtime_positions": runtime_positions,
                    "runtime_pending_orders_available": runtime_ok and runtime_orders_ok, "runtime_pending_orders": runtime_orders,
                    "orphan_evidence_available": orphan_ok, "orphan_position_count": latest_runtime.get("orphan_position_count") if orphan_ok else None, "orphan_order_count": latest_runtime.get("orphan_order_count") if orphan_ok else None,
                    "reconciliation_evidence_available": reconciliation_ok, "reconciliation_status": reconciliation_status, "reconciliation_evidence": diagnostics,
                    "latest_runtime_state": latest_runtime, "runtime_lineage_matches": lineage_matches,
                    "runtime_snapshot_age_seconds": snapshot_age, "runtime_snapshot_fresh": snapshot_fresh,
                    "recovery_required": recovery_required, "stale_continuation": bool(active and active.get("status") in ACTIVE_RUN_STATUSES and (stale or not worker_alive)),
                    "orphaned_continuations": orphaned, "query_errors": query_errors[campaign_errors_start:]}
            campaigns.append(item)
            all_available = all((positions_ok, rejects_ok, runtime_ok, runtime_positions_ok, runtime_orders_ok, orphan_ok, reconciliation_ok, continuations_ok))
            counters_zero = bool(latest_runtime and all(latest_runtime.get(key) == 0 for key in ("active_position_count", "pending_order_count", "orphan_position_count", "orphan_order_count")))
            collections_zero = all_available and not open_positions and not pending_rejects and not runtime_positions and not runtime_orders
            safe_exchange = bool(latest_runtime and latest_runtime.get("unknown_exchange_state") in (0, False) and latest_runtime.get("recovery_action_required") in (0, False) and reconciliation_status != "LOCAL_ONLY_DIAGNOSTIC" and latest_runtime.get("exchange_read_only_status") != "UNAVAILABLE" and authenticated_complete)
            verified_zero = bool(all_available and not item["query_errors"] and counters_zero and collections_zero and lineage_matches and snapshot_fresh and safe_exchange)
            if terminal and verified_zero:
                classification, action, reason = "ARCHIVABLE", "ARCHIVE_EVIDENCE_PACKAGE", "terminal with verified zero local/runtime exposure and no pending labels"
            elif terminal:
                classification, action, reason = "MANUAL_REVIEW", "PRESERVE_AND_REVIEW", "terminal campaign retains pending or unknown evidence"
            elif item["stale_continuation"] and not open_positions and not pending_rejects and verified_zero:
                classification, action, reason = "RECOVERABLE", "RUN_AUTHENTICATED_RECONCILIATION_THEN_RECOVERY_DRILL", "stale worker with verified zero exposure; recovery must remain gated"
            else:
                classification, action, reason = "MANUAL_REVIEW", "DO_NOT_CLEAR", "active, exposed, pending, or unknown state must fail closed"
            cleanup_plan.append({"campaign_id": cid, "classification": classification, "action": action, "reason": reason,
                                 "delete_evidence_rows": False, "convert_unknown_exposure_to_zero": False,
                                 "automatically_clear_live_or_reconciliation_blockers": False})
        return {"status": "COMPLETE", "read_only": True, "database": str(Path(db).resolve()),
                "campaigns_available": True, "campaign_count": len(campaigns), "campaigns": campaigns, "cleanup_plan": cleanup_plan, "query_errors": query_errors,
                "safety": {"database_mutated": False, "evidence_deletion": "DISABLED", "live_mutation": "DISABLED"}}
    finally:
        conn.close()


def bootstrap_ops_schema(conn: Any) -> None:
    bootstrap_campaign_schema(conn)
    _exec(conn, """CREATE TABLE IF NOT EXISTS burnin_preflight_reports(id INTEGER PRIMARY KEY AUTOINCREMENT, preflight_id TEXT UNIQUE NOT NULL, campaign_id TEXT, release_id TEXT NOT NULL, generated_at TEXT NOT NULL, status TEXT NOT NULL, blockers_json TEXT NOT NULL, checks_json TEXT NOT NULL, output_dir TEXT, schema_version TEXT NOT NULL)""")
    _exec(conn, """CREATE TABLE IF NOT EXISTS burnin_ops_incidents(id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT UNIQUE NOT NULL, campaign_id TEXT NOT NULL, incident_type TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL, detected_at TEXT NOT NULL, details_json TEXT NOT NULL, schema_version TEXT NOT NULL)""")
    _exec(conn, """CREATE TABLE IF NOT EXISTS burnin_health_history(id INTEGER PRIMARY KEY AUTOINCREMENT, health_id TEXT UNIQUE NOT NULL, campaign_id TEXT NOT NULL, generated_at TEXT NOT NULL, status TEXT NOT NULL, unhealthy_reasons_json TEXT NOT NULL, payload_json TEXT NOT NULL, schema_version TEXT NOT NULL)""")
    _exec(conn, """CREATE TABLE IF NOT EXISTS burnin_recovery_drills(id INTEGER PRIMARY KEY AUTOINCREMENT, drill_id TEXT UNIQUE NOT NULL, campaign_id TEXT NOT NULL, generated_at TEXT NOT NULL, status TEXT NOT NULL, checks_json TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL, schema_version TEXT NOT NULL)""")
    _exec(conn, """CREATE TABLE IF NOT EXISTS burnin_integrity_audits(id INTEGER PRIMARY KEY AUTOINCREMENT, audit_id TEXT UNIQUE NOT NULL, campaign_id TEXT NOT NULL, generated_at TEXT NOT NULL, status TEXT NOT NULL, violations_json TEXT NOT NULL, checks_json TEXT NOT NULL, aggregate_evidence_hash TEXT, schema_version TEXT NOT NULL)""")
    _exec(conn, """CREATE TABLE IF NOT EXISTS burnin_release_decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT UNIQUE NOT NULL, campaign_id TEXT NOT NULL, generated_at TEXT NOT NULL, decision TEXT NOT NULL, blockers_json TEXT NOT NULL, package_dir TEXT NOT NULL, checksums_json TEXT NOT NULL, schema_version TEXT NOT NULL)""")
    _exec(conn, """CREATE TABLE IF NOT EXISTS burnin_source_evidence_hashes(id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL, burnin_run_id TEXT NOT NULL, captured_at TEXT NOT NULL, evidence_hash TEXT NOT NULL, row_ids_json TEXT NOT NULL, run_status TEXT, baseline_reason TEXT, schema_version TEXT NOT NULL, UNIQUE(campaign_id,burnin_run_id))""")
    for stmt in ("ALTER TABLE burnin_source_evidence_hashes ADD COLUMN run_status TEXT", "ALTER TABLE burnin_source_evidence_hashes ADD COLUMN baseline_reason TEXT"):
        try:
            _exec(conn, stmt)
        except Exception:
            pass


def _write_json_csv(base: Path, stem: str, payload: Mapping[str, Any]) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{stem}.json").write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    with (base / f"{stem}.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["key", "value"])
        for key, value in payload.items():
            writer.writerow([key, json.dumps(value, sort_keys=True, default=str) if isinstance(value, (dict, list)) else value])


def _dt(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except Exception:
        return None


def _age(ts: Any) -> float | None:
    parsed = _dt(ts)
    if parsed is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def _pid_alive(pid: Any) -> bool:
    try:
        pid_int = int(pid or 0)
        if pid_int <= 0:
            return False
        os.kill(pid_int, 0)
        return True
    except Exception:
        return False


def _git_clean() -> bool:
    return subprocess.run(["git", "diff", "--quiet"], cwd=Path.cwd()).returncode == 0 and subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=Path.cwd()).returncode == 0


def _git_commit() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def parse_symbols(raw: str | Sequence[str]) -> list[str]:
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    parsed: list[str] = []
    for value in values:
        parsed.extend(item.strip().upper() for item in str(value).split(",") if item.strip())
    return parsed


_symbols = parse_symbols


def _intervals(raw: str | Sequence[str]) -> list[str]:
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    parsed: list[str] = []
    for value in values:
        parsed.extend(item.strip() for item in str(value).split(",") if item.strip())
    return parsed


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row) if isinstance(row, sqlite3.Row) else dict(row._mapping)


def _event_details(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        return json.loads(row["details_json"] or "{}")
    except Exception:
        return {}


def _candidate_identity(release_id: str, symbols: Sequence[str], intervals: Sequence[str]) -> dict[str, Any]:
    return build_phase8_campaign_identity(load_config_from_env().runtime, symbols, intervals, release_id=release_id)


def _actual_runtime_identity(release_id: str, symbols: Sequence[str], intervals: Sequence[str]) -> dict[str, Any]:
    old_release = os.environ.get("ALPHAFORGE_RELEASE_ID")
    old_exec = os.environ.get("ALPHAFORGE_EXECUTION_MODE")
    old_mode = os.environ.get("EXECUTION_MODE")
    os.environ["ALPHAFORGE_RELEASE_ID"] = release_id
    os.environ["ALPHAFORGE_EXECUTION_MODE"] = "PAPER"
    os.environ["EXECUTION_MODE"] = "PAPER"
    try:
        from alphaforge.runtime import _build_runtime_from_env
        runtime = _build_runtime_from_env()
        # Preserve the canonical builder's payloads for an auditable preflight
        # comparison; callers still enforce all critical hashes below.
        return runtime._phase8_runtime_hashes(list(symbols), list(intervals))
    finally:
        for key, value in (("ALPHAFORGE_RELEASE_ID", old_release), ("ALPHAFORGE_EXECUTION_MODE", old_exec), ("EXECUTION_MODE", old_mode)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _binance_server_time_ms() -> dict[str, Any]:
    with urllib.request.urlopen("https://fapi.binance.com/fapi/v1/time", timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return {"provider_utc_ms": int(payload["serverTime"]), "provider_provenance": {"provider": "BINANCE_READ_ONLY_SERVER_TIME", "endpoint": "https://fapi.binance.com/fapi/v1/time", "order_submission": "DISABLED"}}


def clock_skew_check(*, max_skew_ms: int | None = None, provider: Any | None = None) -> dict[str, Any]:
    configured = int(max_skew_ms if max_skew_ms is not None else os.getenv("ALPHAFORGE_MAX_CLOCK_SKEW_MS", "5000"))
    local_ms = int(time.time() * 1000)
    try:
        raw = provider() if provider is not None else _binance_server_time_ms()
        if isinstance(raw, Mapping):
            provider_ms = int(raw["provider_utc_ms"] if "provider_utc_ms" in raw else raw["serverTime"])
            provenance = dict(raw.get("provider_provenance") or raw.get("provenance") or {"provider": "READ_ONLY_TIME_PROVIDER"})
        else:
            provider_ms = int(raw)
            provenance = {"provider": "READ_ONLY_TIME_PROVIDER"}
        skew = abs(local_ms - provider_ms)
        return {"status": "PASS" if skew <= configured else "FAIL", "local_utc_ms": local_ms, "provider_utc_ms": provider_ms, "absolute_skew_ms": skew, "configured_max_skew_ms": configured, "provider_provenance": provenance}
    except Exception as exc:
        return {"status": "UNAVAILABLE", "local_utc_ms": local_ms, "provider_utc_ms": None, "absolute_skew_ms": None, "configured_max_skew_ms": configured, "provider_provenance": {"provider": "BINANCE_READ_ONLY_SERVER_TIME"}, "error": f"{exc.__class__.__name__}:{exc}"}


def _readonly_reconciliation_provider(cfg: Any, symbols: Sequence[str] = ()) -> Any | None:
    if not getattr(cfg.runtime, "enable_binance_readonly_reconciliation", False):
        return None
    reconciliation = load_reconciliation_settings()
    if not reconciliation.api_key.strip() or not reconciliation.api_secret.strip():
        return None
    return BinanceReadonlyReconciliationProvider(
        config=BinanceReadonlyReconciliationConfig(
            base_url=reconciliation.base_url,
            api_key=reconciliation.api_key,
            api_secret=reconciliation.api_secret,
            recv_window_ms=reconciliation.recv_window_ms,
            request_timeout_sec=reconciliation.timeout_sec,
            trade_lookback_ms=reconciliation.trade_lookback_ms,
            position_epsilon=Decimal(reconciliation.position_epsilon),
            max_fill_symbols=reconciliation.max_fill_symbols,
        ),
        tracked_symbols=lambda: set(_symbols(symbols)),
    )


def preflight(db: str, release_id: str, symbols: Sequence[str], intervals: Sequence[str], *, output_dir: str | Path | None = None, require_market_data: bool = True, reconciliation_provider: Any | None = None) -> dict[str, Any]:
    cfg = load_config_from_env()
    checks: list[dict[str, Any]] = []
    blockers: list[str] = []
    out = Path(output_dir or f"artifacts/burnin/preflight_{release_id}")

    def add(name: str, status: str, details: Any = "", *, critical: bool = True) -> None:
        checks.append({"name": name, "status": status, "details": details, "critical": critical})
        if critical and status != "PASS":
            blockers.append(name)

    env_audit = audit_config()
    dotenv = dotenv_status()
    add("env_contract_valid", "PASS" if env_audit["status"] != "FAIL" else "FAIL", {"errors": env_audit["errors"], "warnings": env_audit["warnings"]})
    add("dotenv_loaded", "PASS", {"path": dotenv.path, "loaded": dotenv.loaded})
    add("no_duplicate_env_keys", "PASS" if not env_audit["duplicate_template_variables"] else "FAIL", env_audit["duplicate_template_variables"])
    add("no_unknown_operational_env_variables", "PASS" if not env_audit["unknown_process_variables"] else "FAIL", env_audit["unknown_process_variables"])
    endpoint_details = {"environment": cfg.binance.environment, "rest_base_url": cfg.binance.base_url, "ws_base_url": cfg.binance.ws_url, "resolution_source": cfg.binance.resolution_source}
    add("binance_environment_consistent", "PASS", endpoint_details)
    add("reconciliation_endpoint_matches_environment", "PASS", endpoint_details)
    reconciliation_enabled = bool(cfg.runtime.enable_binance_readonly_reconciliation)
    secret_rows = env_audit["resolved_non_secret_configuration"]
    credentials_ok = all(bool(secret_rows.get(name, {}).get("present")) and not bool(secret_rows.get(name, {}).get("placeholder_detected")) for name in ("BINANCE_API_KEY", "BINANCE_API_SECRET"))
    add("reconciliation_credentials_non_placeholder", "PASS" if (not reconciliation_enabled or credentials_ok) else "FAIL", {"enabled": reconciliation_enabled, "credentials_present": credentials_ok})

    try:
        commit = _git_commit()
        add("git_commit_known", "PASS" if commit else "FAIL", commit)
    except Exception as exc:
        commit = "UNKNOWN"
        add("git_commit_known", "FAIL", str(exc))
    add("working_tree_clean", "PASS" if _git_clean() else "FAIL", "dev branch must be clean")
    try:
        branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True).strip()
        add("dev_branch", "PASS" if branch == "dev" else "FAIL", branch)
    except Exception as exc:
        add("dev_branch", "FAIL", str(exc))
    mode = str(cfg.runtime.execution_mode).upper()
    add("execution_mode_paper", "PASS" if mode == "PAPER" else "FAIL", mode)
    add("live_mutation_path_disabled", "PASS" if mode != "LIVE" and not bool(getattr(cfg.runtime, "enable_live_execution", False)) else "FAIL", "LIVE mutation path must be unavailable")
    add("symbols_valid", "PASS" if bool(symbols) and all(s.endswith("USDT") and s.replace("USDT", "").isalnum() for s in symbols) else "FAIL", list(symbols))
    add("intervals_valid", "PASS" if bool(intervals) and all(i in VALID_INTERVALS for i in intervals) else "FAIL", list(intervals))

    conn: sqlite3.Connection | None = None
    try:
        Path(db).parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(db)
        conn.execute("CREATE TABLE IF NOT EXISTS burnin_ops_write_probe(x INTEGER)")
        conn.commit()
        canonical_db = str(Path(db).expanduser().resolve())
        add("database_writable", "PASS", canonical_db)
        schema = validate_required_schema(conn)
        add("schema_current", "PASS" if schema.schema_status == "VALID" else "FAIL", {**schema.as_dict(), "campaign_schema": CAMPAIGN_SCHEMA_VERSION, "ops_schema": PHASE9_SCHEMA_VERSION})
    except Exception as exc:
        add("database_writable", "FAIL", str(exc))
        add("schema_current", "UNAVAILABLE", "database unavailable")

    ident = _candidate_identity(release_id, symbols, intervals)
    cid = "camp_" + canonical_hash({"release_id": release_id, "config_hash": ident["config_hash"], "strategy_config_hash": ident["strategy_config_hash"], "universe_hash": ident["universe_hash"]})[:16]
    add("campaign_identity_deterministic", "PASS" if ident.get("config_hash") and ident.get("universe_hash") else "FAIL", ident)
    try:
        runtime_ident = _actual_runtime_identity(release_id, symbols, intervals)
        expected = {key: ident.get(key) for key in ("release_id", "config_hash", "strategy_config_hash", "universe_hash", "execution_cost_config_hash")}
        expected["execution_mode"] = "PAPER"
        mismatches = {key: {"expected": expected[key], "observed": runtime_ident.get(key)} for key in expected if runtime_ident.get(key) != expected[key]}
        candidate_payload = ident.get("config_payload", {})
        runtime_payload = runtime_ident.get("config_payload", {})
        payload_differences = {
            key: {"candidate": candidate_payload.get(key), "runtime": runtime_payload.get(key)}
            for key in sorted(set(candidate_payload) | set(runtime_payload))
            if candidate_payload.get(key) != runtime_payload.get(key)
        }
        add("runtime_identity_matches_campaign_identity", "PASS" if not mismatches else "FAIL", {
            "expected": expected,
            "observed": runtime_ident,
            "mismatches": mismatches,
            "candidate_config_payload": candidate_payload,
            "runtime_config_payload": runtime_payload,
            "config_payload_differences": payload_differences,
        })
    except Exception as exc:
        add("runtime_identity_matches_campaign_identity", "UNAVAILABLE", f"{exc.__class__.__name__}:{exc}")
    add("execution_cost_identity_complete", "PASS" if ident.get("execution_cost_config_hash") and all(k in ident.get("execution_cost_payload", {}) for k in ("max_spread_pct", "paper_slippage_bps", "min_liquidity_usd")) else "FAIL", ident.get("execution_cost_payload"))
    try:
        provenance = BinanceReadOnlyCandleProvider(interval=intervals[0] if intervals else "1h").source_provenance
        ok = provenance.get("provider") == "BINANCE_READ_ONLY_KLINES" and provenance.get("order_submission") == "DISABLED"
        add("source_provenance_present", "PASS" if ok else "FAIL", provenance)
    except Exception as exc:
        add("source_provenance_present", "UNAVAILABLE", f"{exc.__class__.__name__}:{exc}")

    if conn is not None:
        # Preflight is the supported fail-closed entry point for stale worker
        # ownership. It preserves all evidence and requires the normal recovery
        # drill before any continuation can resume.
        cleanup_dead_worker(conn, cid)
        dup = conn.execute("SELECT COUNT(*) FROM burnin_campaigns WHERE release_id=? AND config_hash=? AND strategy_config_hash=? AND universe_hash=? AND campaign_status IN ('CREATED','STARTING','RUNNING','PAUSED','RECOVERY_REQUIRED')", (release_id, ident["config_hash"], ident["strategy_config_hash"], ident["universe_hash"])).fetchone()[0]
        add("no_duplicate_active_campaign", "PASS" if int(dup) == 0 else "FAIL", {"candidate_campaign_id": cid, "duplicates": dup})
        stale = conn.execute("SELECT COUNT(*) FROM burnin_campaigns WHERE campaign_id=? AND worker_pid IS NOT NULL AND campaign_status IN ('CREATED','STARTING','RUNNING','PAUSED','RECOVERY_REQUIRED')", (cid,)).fetchone()[0]
        add("no_stale_worker_occupying_campaign", "PASS" if int(stale) == 0 else "FAIL", stale)
        recovery_engine = init_db(f"sqlite+pysqlite:///{db}")
        try:
            recovery = evaluate_runtime_recovery(recovery_engine, mode="PAPER", campaign_id=cid, reconciliation_probe=build_readonly_reconciliation_probe(reconciliation_provider or _readonly_reconciliation_provider(cfg, symbols)))
            # A complete empty account snapshot is the required exchange evidence
            # for clearing an unrelated PAPER predecessor.  Preserve it append-only;
            # never edit the predecessor's unclean snapshot in place.
            if recovery.get("prior_unclean") and recovery.get("reconciliation_probe_clean"):
                persist_verified_paper_recovery(recovery_engine, probe=recovery["reconciliation_probe"], prior_snapshot=recovery.get("latest"))
                recovery = evaluate_runtime_recovery(recovery_engine, mode="PAPER", campaign_id=cid)
            add("runtime_recovery_scope", "PASS" if not recovery["blocked"] else "FAIL", recovery)
        finally:
            recovery_engine.dispose()
    usage = __import__("shutil").disk_usage(Path(db).parent if Path(db).parent.exists() else Path.cwd())
    add("disk_space_sufficient", "PASS" if usage.free > 100 * 1024 * 1024 else "FAIL", {"free_bytes": usage.free})
    if require_market_data:
        try:
            BinanceReadOnlyCandleProvider(interval=intervals[0] if intervals else "1h")(symbols[0], "2024-01-01T00:00:00Z", "2024-01-01T02:00:00Z")
            add("binance_readonly_klines_reachable", "PASS", symbols[0] if symbols else None)
        except Exception as exc:
            add("binance_readonly_klines_reachable", "FAIL", f"{exc.__class__.__name__}:{exc}")
    skew = clock_skew_check()
    add("clock_skew_acceptable", skew["status"], skew)

    status = "PASS" if not blockers else "FAIL_CLOSED"
    payload = {"preflight_id": "pre_" + canonical_hash({"release_id": release_id, "at": utc_now(), "checks": checks})[:20], "release_id": release_id, "campaign_id": cid, "generated_at": utc_now(), "status": status, "blockers": blockers, "checks": checks, "evidence_locations": {"json": str(out / "burnin_preflight.json"), "csv": str(out / "burnin_preflight.csv")}}
    _write_json_csv(out, "burnin_preflight", payload)
    if conn is not None:
        conn.execute("INSERT OR REPLACE INTO burnin_preflight_reports(preflight_id,campaign_id,release_id,generated_at,status,blockers_json,checks_json,output_dir,schema_version) VALUES (?,?,?,?,?,?,?,?,?)", (payload["preflight_id"], cid, release_id, payload["generated_at"], status, json.dumps(blockers), json.dumps(checks, default=str), str(out), PHASE9_SCHEMA_VERSION))
        conn.commit()
        conn.close()
    return payload


def _latest_attach(conn: sqlite3.Connection, campaign_id: str, since: str | None = None, run_id: str | None = None) -> sqlite3.Row | None:
    params: list[Any] = [campaign_id]
    where = "campaign_id=? AND event_type='PHASE8_CAMPAIGN_ATTACHED'"
    if since:
        where += " AND event_time >= ?"
        params.append(since)
    rows = conn.execute(f"SELECT * FROM burnin_campaign_events WHERE {where} ORDER BY id DESC", params).fetchall()
    for row in rows:
        details = _event_details(row)
        if run_id is None or details.get("active_run_id") == run_id:
            return row
    return None


def _latest_attach_any(conn: sqlite3.Connection, campaign_id: str, since: str | None = None) -> sqlite3.Row | None:
    params: list[Any] = [campaign_id]
    where = "campaign_id=? AND event_type='PHASE8_CAMPAIGN_ATTACHED'"
    if since:
        where += " AND event_time >= ?"
        params.append(since)
    return conn.execute(f"SELECT * FROM burnin_campaign_events WHERE {where} ORDER BY id DESC LIMIT 1", params).fetchone()


def verify_worker_attachment(conn: sqlite3.Connection, campaign_id: str, *, worker_started_at: str, launch_started_at: str, timeout_seconds: float = 60.0, process: subprocess.Popen[Any] | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() <= deadline:
        conn.commit()
        campaign = get_campaign(conn, campaign_id)
        if not campaign:
            last = {"status": "FAILED", "reason": "CAMPAIGN_NOT_FOUND"}
            break
        pid = campaign.get("worker_pid")
        active_run_id = campaign.get("active_run_id")
        raw_attach = _latest_attach_any(conn, campaign_id, since=launch_started_at)
        raw_details = _event_details(raw_attach)
        if raw_attach is not None and raw_details.get("active_run_id") != active_run_id:
            last = {"status": "FAILED", "reason": "WORKER_IDENTITY_MISMATCH", "worker_pid": pid, "active_run_id": active_run_id, "attached_run_id": raw_details.get("active_run_id"), "runtime_instance_id": raw_details.get("runtime_instance_id")}
            break
        attach = _latest_attach(conn, campaign_id, since=launch_started_at, run_id=active_run_id)
        details = _event_details(attach)
        heartbeat = campaign.get("last_heartbeat_at")
        worker_exit_code = process.poll() if process is not None else None
        checks = {
            "worker_alive": _pid_alive(pid),
            "worker_not_exited": worker_exit_code is None,
            "attach_event_after_launch": attach is not None,
            "runtime_instance_evidence": bool(details.get("runtime_instance_id")),
            "heartbeat_newer_than_worker_started": bool(heartbeat and heartbeat >= worker_started_at),
            "active_run_id_matches_attach": bool(active_run_id and details.get("active_run_id") == active_run_id),
        }
        last = {"status": "ATTACHED" if all(checks.values()) else "WAITING", "checks": checks, "worker_pid": pid, "worker_exit_code": worker_exit_code, "runtime_instance_id": details.get("runtime_instance_id"), "active_run_id": active_run_id, "heartbeat": heartbeat}
        if all(checks.values()):
            return last
        if worker_exit_code is not None or (pid and not _pid_alive(pid)):
            last["status"] = "FAILED"
            last["reason"] = "WORKER_EXITED_BEFORE_ATTACHMENT"
            break
        time.sleep(0.25)
    reason = last.get("reason") or "WORKER_ATTACHMENT_TIMEOUT"
    _mark_campaign_failed(conn, campaign_id, reason, last)
    return {**last, "status": "FAILED", "reason": reason}

def _mark_campaign_failed(conn: sqlite3.Connection, campaign_id: str, reason: str, details: Mapping[str, Any] | None = None) -> None:
    campaign = get_campaign(conn, campaign_id)
    active_run_id = campaign.get("active_run_id") if campaign else None
    ts = utc_now()
    if active_run_id:
        conn.execute("UPDATE burnin_runs SET status='FAILED', end_time=COALESCE(end_time, ?) WHERE burnin_run_id=? AND status IN ('STARTING','RUNNING')", (ts, active_run_id))
        conn.execute("UPDATE burnin_campaign_runs SET status='FAILED', ended_at=COALESCE(ended_at, ?) WHERE campaign_id=? AND burnin_run_id=? AND status IN ('STARTING','RUNNING')", (ts, campaign_id, active_run_id))
    conn.execute("UPDATE burnin_campaigns SET campaign_status='FAILED', last_error=?, worker_pid=NULL, worker_started_at=NULL WHERE campaign_id=?", (reason, campaign_id))
    event(conn, campaign_id, "PHASE9_CAMPAIGN_FAILED", details={"reason": reason, **dict(details or {})})
    conn.commit()


def _mark_starting(conn: sqlite3.Connection, campaign_id: str, run_id: str) -> None:
    conn.execute("UPDATE burnin_runs SET status='STARTING' WHERE burnin_run_id=? AND status='RUNNING'", (run_id,))
    conn.execute("UPDATE burnin_campaign_runs SET status='STARTING' WHERE campaign_id=? AND burnin_run_id=? AND status='RUNNING'", (campaign_id, run_id))
    conn.execute("UPDATE burnin_campaigns SET campaign_status='STARTING' WHERE campaign_id=? AND campaign_status='RUNNING'", (campaign_id,))
    event(conn, campaign_id, "PHASE9_CAMPAIGN_STARTING", burnin_run_id=run_id)


def _mark_attached_running(conn: sqlite3.Connection, campaign_id: str, run_id: str, details: Mapping[str, Any]) -> None:
    conn.execute("UPDATE burnin_runs SET status='RUNNING' WHERE burnin_run_id=? AND status='STARTING'", (run_id,))
    conn.execute("UPDATE burnin_campaign_runs SET status='RUNNING' WHERE campaign_id=? AND burnin_run_id=? AND status='STARTING'", (campaign_id, run_id))
    conn.execute("UPDATE burnin_campaigns SET campaign_status='RUNNING', last_error=NULL WHERE campaign_id=? AND campaign_status='STARTING'", (campaign_id,))
    event(conn, campaign_id, "PHASE9_CAMPAIGN_RUNNING_AFTER_ATTACHMENT", burnin_run_id=run_id, details=details)
    conn.commit()


def _safe_startup_failure(conn: sqlite3.Connection | None, campaign_id: str | None, reason: str, details: Mapping[str, Any] | None = None) -> None:
    if conn is None or not campaign_id:
        return
    try:
        _mark_campaign_failed(conn, campaign_id, reason, {"startup_failure": True, **dict(details or {})})
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass



def _startup_diagnostics(campaign_id: str, process: subprocess.Popen[Any] | None = None, **extra: Any) -> dict[str, Any]:
    stdout_path, stderr_path = _worker_log_paths(campaign_id)
    worker_exit_code = None
    if process is not None:
        try:
            worker_exit_code = process.poll()
        except Exception as exc:
            worker_exit_code = f"POLL_UNAVAILABLE:{exc.__class__.__name__}:{exc}"
    return {
        "stdout_log_path": str(stdout_path),
        "stderr_log_path": str(stderr_path),
        "worker_exit_code": worker_exit_code,
        **extra,
    }

def launch_campaign(db: str, release_id: str, duration_days: float, symbols: Sequence[str], intervals: Sequence[str], *, detach: bool = False, attach_timeout_seconds: float = 60.0) -> dict[str, Any]:
    pf = preflight(db, release_id, symbols, intervals)
    if pf["status"] != "PASS":
        return {"status": "FAILED_CLOSED", "preflight": pf}
    conn = _connect(db)
    campaign_id: str | None = None
    run_id: str | None = None
    try:
        campaign = create_campaign(conn, release_id=release_id, duration_days=duration_days, symbols=symbols, intervals=intervals, runtime_config=load_config_from_env().runtime, source_provenance={"provider": "BINANCE_READ_ONLY_KLINES", "mode": "PAPER"})
        campaign_id = campaign.campaign_id
        launch_started_at = utc_now()
        start = start_or_resume_campaign(conn, campaign.campaign_id)
        run_id = start["burnin_run_id"]
        if detach:
            _mark_starting(conn, campaign.campaign_id, run_id)
        conn.commit()
        if detach:
            try:
                proc = _launch_worker(db, campaign.campaign_id)
            except RuntimeError as exc:
                reason = str(exc) or "WORKER_LAUNCH_RUNTIME_ERROR"
                _safe_startup_failure(conn, campaign.campaign_id, reason, _startup_diagnostics(campaign.campaign_id, exception_type=exc.__class__.__name__, error=reason))
                return {"status": "FAILED", "campaign_id": campaign.campaign_id, "reason": reason, **_startup_diagnostics(campaign.campaign_id)}
            except Exception as exc:
                diagnostics = _startup_diagnostics(campaign.campaign_id, exception_type=exc.__class__.__name__, error=f"{exc.__class__.__name__}:{exc}")
                _safe_startup_failure(conn, campaign.campaign_id, "WORKER_SPAWN_FAILED", diagnostics)
                return {"status": "FAILED", "campaign_id": campaign.campaign_id, "reason": "PHASE9_WORKER_SPAWN_FAILED", "startup_failure_reason": "WORKER_SPAWN_FAILED", **diagnostics}
            worker_started_at = utc_now()
            conn.execute("UPDATE burnin_campaigns SET worker_pid=?, worker_started_at=? WHERE campaign_id=?", (proc.pid, worker_started_at, campaign.campaign_id))
            conn.commit()
            if proc.poll() is not None or not _pid_alive(proc.pid):
                diagnostics = _startup_diagnostics(campaign.campaign_id, proc, worker_pid=proc.pid)
                _mark_campaign_failed(conn, campaign.campaign_id, "WORKER_STARTUP_EXITED", diagnostics)
                return {"status": "FAILED", "campaign_id": campaign.campaign_id, "reason": "WORKER_STARTUP_EXITED", **diagnostics}
            try:
                attach = verify_worker_attachment(conn, campaign.campaign_id, worker_started_at=worker_started_at, launch_started_at=launch_started_at, timeout_seconds=attach_timeout_seconds, process=proc)
            except KeyboardInterrupt:
                _safe_startup_failure(conn, campaign.campaign_id, "WORKER_ATTACHMENT_INTERRUPTED", _startup_diagnostics(campaign.campaign_id, proc, exception_type="KeyboardInterrupt", worker_pid=proc.pid))
                raise
            except SystemExit as exc:
                _safe_startup_failure(conn, campaign.campaign_id, "SYSTEM_EXIT_DURING_ATTACHMENT", _startup_diagnostics(campaign.campaign_id, proc, exception_type="SystemExit", code=exc.code, worker_pid=proc.pid))
                raise
            except BaseException as exc:
                _safe_startup_failure(conn, campaign.campaign_id, "LAUNCH_STARTUP_FAILED", _startup_diagnostics(campaign.campaign_id, proc, exception_type=exc.__class__.__name__, error=str(exc), worker_pid=proc.pid))
                raise
            if attach.get("status") != "ATTACHED":
                diagnostics = _startup_diagnostics(campaign.campaign_id, proc, worker_pid=proc.pid)
                return {"status": "FAILED", "campaign_id": campaign.campaign_id, "attachment": {**diagnostics, **attach}, **diagnostics}
            _mark_attached_running(conn, campaign.campaign_id, run_id, attach)
            return {"status": "LAUNCHED", "campaign_id": campaign.campaign_id, "burnin_run_id": start["burnin_run_id"], "worker_pid": proc.pid, "attachment": attach, "evidence_locations": {"preflight": pf["evidence_locations"], "database": db, "artifacts": f"artifacts/burnin/{campaign.campaign_id}"}}
        engine = create_engine(f"sqlite+pysqlite:///{db}", future=True)
        try:
            runner = BurnInCampaignRunner(engine, campaign.campaign_id, BinanceReadOnlyCandleProvider())
            result = asyncio.run(runner.run_foreground())
            return {"status": "FOREGROUND_STOPPED", "campaign_id": campaign.campaign_id, "burnin_run_id": start["burnin_run_id"], "runner": result}
        finally:
            engine.dispose()
    except Exception as exc:
        if campaign_id and run_id:
            _safe_startup_failure(conn, campaign_id, "LAUNCH_STARTUP_FAILED", {"exception_type": exc.__class__.__name__, "error": str(exc)})
        raise
    finally:
        conn.close()

def _counts(conn: sqlite3.Connection, campaign_id: str) -> dict[str, int]:
    def cnt(sql: str, params: tuple[Any, ...] = ()) -> int:
        return int(conn.execute(sql, params).fetchone()[0] or 0)
    return {
        "pending_reject_labels": cnt("SELECT COUNT(*) FROM burnin_pending_reject_labels WHERE campaign_id=? AND status IN ('PENDING','READY')", (campaign_id,)),
        "open_positions": cnt("SELECT COUNT(*) FROM burnin_pending_position_outcomes WHERE campaign_id=? AND status='OPEN'", (campaign_id,)),
        "resolver_failures": cnt("SELECT COUNT(*) FROM burnin_campaign_events WHERE campaign_id=? AND event_type='RESOLVER_BATCH_FAILED'", (campaign_id,)),
        "provider_failures": cnt("SELECT COUNT(*) FROM burnin_campaign_events WHERE campaign_id=? AND (event_type LIKE '%PROVIDER%' OR details_json LIKE '%PROVIDER%' OR details_json LIKE '%MARKET_DATA%')", (campaign_id,)),
        "qualification_failures": cnt("SELECT COUNT(*) FROM burnin_campaign_events WHERE campaign_id=? AND event_type LIKE '%QUALIFICATION%' AND details_json LIKE '%error%'", (campaign_id,)),
    }


def _recovery_campaign_exposure(conn: sqlite3.Connection, campaign_id: str) -> dict[str, Any]:
    errors: list[str] = []
    availability = {"campaign_open_positions_available": False, "pending_reject_labels_available": False}

    def cnt(name: str, sql: str) -> int:
        try:
            value = int(conn.execute(sql, (campaign_id,)).fetchone()[0] or 0)
            availability[f"{name}_available"] = True
            return value
        except Exception as exc:
            errors.append(f"{name}:{exc.__class__.__name__}:{exc}")
            return 0

    return {
        "pending_reject_labels": cnt("pending_reject_labels", "SELECT COUNT(*) FROM burnin_pending_reject_labels WHERE campaign_id=? AND status IN ('PENDING','READY')"),
        "open_positions": cnt("campaign_open_positions", "SELECT COUNT(*) FROM burnin_pending_position_outcomes WHERE campaign_id=? AND status='OPEN'"),
        "query_errors": errors,
        "availability": availability,
    }


def _startup_terminalization_evidence(conn: sqlite3.Connection, campaign_id: str, run_id: str | None) -> dict[str, Any]:
    """Collect fail-closed SQL evidence that a PAPER startup never traded."""
    errors: list[str] = []
    counts: dict[str, int | None] = {
        "decisions": None,
        "executions": None,
        "lifecycle_executions": None,
    }
    run_mode: str | None = None
    run_status: str | None = None

    def count(name: str, sql: str, params: Sequence[Any]) -> None:
        try:
            counts[name] = int(conn.execute(sql, tuple(params)).fetchone()[0] or 0)
        except Exception as exc:
            errors.append(f"{name}:{exc.__class__.__name__}:{exc}")

    if not run_id:
        errors.append("run:missing_active_run_id")
    else:
        try:
            row = conn.execute(
                "SELECT r.execution_mode, r.status FROM burnin_runs r "
                "JOIN burnin_campaign_runs cr ON cr.burnin_run_id=r.burnin_run_id "
                "WHERE cr.campaign_id=? AND r.burnin_run_id=?",
                (campaign_id, run_id),
            ).fetchone()
            if row is None:
                errors.append("run:missing_campaign_run_lineage")
            else:
                run_mode, run_status = str(row[0] or "").upper(), str(row[1] or "").upper()
        except Exception as exc:
            errors.append(f"run:{exc.__class__.__name__}:{exc}")
        count("decisions", "SELECT COUNT(*) FROM burnin_observations WHERE burnin_run_id=?", (run_id,))
        count(
            "executions",
            "SELECT (SELECT COUNT(*) FROM burnin_trade_outcomes WHERE burnin_run_id=?) + "
            "(SELECT COUNT(*) FROM burnin_pending_position_outcomes WHERE campaign_id=? AND burnin_run_id=?)",
            (run_id, campaign_id, run_id),
        )
        count(
            "lifecycle_executions",
            "SELECT COUNT(*) FROM burnin_observations WHERE burnin_run_id=? AND UPPER(COALESCE(lifecycle_state,'')) "
            "IN ('ENTRY_TRIGGERED','ORDER_PLACED','PARTIAL_FILL','FILLED','TP_HIT','SL_HIT','CANCELLED','OPEN_AT_END')",
            (run_id,),
        )
    return {
        **counts,
        "run_mode": run_mode,
        "run_status": run_status,
        "query_errors": errors,
        "available": not errors and all(value is not None for value in counts.values()),
    }

def cleanup_dead_worker(conn: sqlite3.Connection, campaign_id: str) -> bool:
    """Fail closed into the supported recovery workflow for a dead worker."""
    campaign = get_campaign(conn, campaign_id)
    if not campaign or campaign.get("campaign_status") not in {"STARTING", "RUNNING"}:
        return False
    pid = campaign.get("worker_pid")
    if not pid or _pid_alive(pid):
        return False
    run_id, now = campaign.get("active_run_id"), utc_now()
    if run_id:
        conn.execute("UPDATE burnin_runs SET status='RECOVERY_REQUIRED', end_time=COALESCE(end_time,?) WHERE burnin_run_id=? AND status IN ('STARTING','RUNNING')", (now, run_id))
        conn.execute("UPDATE burnin_campaign_runs SET status='RECOVERY_REQUIRED', ended_at=COALESCE(ended_at,?) WHERE campaign_id=? AND burnin_run_id=? AND status IN ('STARTING','RUNNING')", (now, campaign_id, run_id))
    conn.execute("UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED', last_error='DEAD_WORKER_RECOVERY_REQUIRED', worker_pid=NULL, worker_started_at=NULL WHERE campaign_id=?", (campaign_id,))
    event(conn, campaign_id, "PHASE9_DEAD_WORKER_RECOVERY_REQUIRED", burnin_run_id=run_id, details={"worker_pid": pid, "last_heartbeat_at": campaign.get("last_heartbeat_at"), "evidence_preserved": True})
    conn.commit()
    return True

def health_payload(conn: sqlite3.Connection, campaign_id: str, *, max_heartbeat_age: float = 120.0, max_open_positions: int = 25) -> dict[str, Any]:
    campaign = get_campaign(conn, campaign_id)
    if not campaign:
        return {"status": "UNAVAILABLE", "unhealthy_reasons": ["NO_CAMPAIGN"], "campaign_id": campaign_id}
    agg = aggregate_campaign(conn, campaign_id)
    metrics = agg.get("metrics", {}) if agg.get("status") == "OK" else {}
    pid = campaign.get("worker_pid")
    alive = _pid_alive(pid)
    age = _age(campaign.get("last_heartbeat_at"))
    runtime_row = None
    try:
        runtime_row = conn.execute(
            "SELECT heartbeat_ts,last_scan_ts,last_decision_ts,payload_json,runtime_state "
            "FROM runtime_heartbeats WHERE execution_mode='PAPER' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        pass
    runtime_hb = dict(runtime_row) if runtime_row else {}
    runtime_payload = json.loads(runtime_hb.get("payload_json") or "{}") if runtime_hb else {}
    runtime_age = _age(runtime_hb.get("heartbeat_ts"))
    scan_age = _age(runtime_hb.get("last_scan_ts"))
    qrow = conn.execute("SELECT status, blockers_json FROM burnin_qualification_snapshots WHERE qualification_id=?", (campaign.get("latest_qualification_id"),)).fetchone() if campaign.get("latest_qualification_id") else None
    counts = _counts(conn, campaign_id)
    runs = [dict(r) for r in conn.execute("SELECT burnin_run_id, continuation_sequence FROM burnin_campaign_runs WHERE campaign_id=? ORDER BY continuation_sequence", (campaign_id,)).fetchall()]
    duplicate_seq = len({r["continuation_sequence"] for r in runs}) != len(runs)
    aggregate_contamination = any(str(r["burnin_run_id"]).endswith("__aggregate") or "aggregate" in str(r["burnin_run_id"]).lower() for r in runs)
    latest_hist = conn.execute("SELECT payload_json FROM burnin_health_history WHERE campaign_id=? ORDER BY id DESC LIMIT 1", (campaign_id,)).fetchone()
    previous = json.loads(latest_hist[0]) if latest_hist else {}
    backlog_growth = bool(previous and counts["pending_reject_labels"] > int(previous.get("pending_reject_labels") or 0))
    evidence_regression = bool(previous and str(previous.get("evidence_completeness_status") or "") == "PASS" and str(campaign.get("evidence_completeness_status") or "") != "PASS")
    payload = {
        "campaign_id": campaign_id, "campaign_status": campaign.get("campaign_status"), "worker_pid": pid, "worker_alive": alive, "heartbeat_age": age,
        "runtime_heartbeat_age": runtime_age, "last_scan_ts": runtime_hb.get("last_scan_ts"), "last_decision_ts": runtime_hb.get("last_decision_ts"),
        "scans": int(runtime_payload.get("scans") or 0), "symbols_selected": int(runtime_payload.get("symbols_selected") or 0),
        "decisions_generated": int(runtime_payload.get("decisions_generated") or 0), "rejects_persisted": int(runtime_payload.get("rejects_persisted") or 0),
        "runtime_status": "ATTACHED" if _latest_attach(conn, campaign_id) is not None else "UNKNOWN", "active_continuation_run": campaign.get("active_run_id"),
        "continuation_count": len(runs), "restart_count": campaign.get("restart_count"), "observed_duration": campaign.get("observed_duration_seconds"),
        "total_decisions": metrics.get("sample_count", 0), "accepted_decisions": metrics.get("accepted_count", 0), "rejected_decisions": metrics.get("rejected_count", 0),
        "pending_reject_labels": counts["pending_reject_labels"], "resolved_reject_labels": int(conn.execute("SELECT COUNT(*) FROM burnin_pending_reject_labels WHERE campaign_id=? AND status='RESOLVED'", (campaign_id,)).fetchone()[0] or 0),
        "expired_reject_labels": int(conn.execute("SELECT COUNT(*) FROM burnin_pending_reject_labels WHERE campaign_id=? AND status='EXPIRED'", (campaign_id,)).fetchone()[0] or 0),
        "failed_reject_labels": int(conn.execute("SELECT COUNT(*) FROM burnin_pending_reject_labels WHERE campaign_id=? AND status='FAILED'", (campaign_id,)).fetchone()[0] or 0),
        "open_paper_positions": counts["open_positions"], "closed_paper_positions": int(conn.execute("SELECT COUNT(*) FROM burnin_pending_position_outcomes WHERE campaign_id=? AND status='CLOSED'", (campaign_id,)).fetchone()[0] or 0),
        "incomplete_outcomes": int(conn.execute("SELECT COUNT(*) FROM burnin_pending_position_outcomes WHERE campaign_id=? AND status='CLOSED' AND evidence_complete=0", (campaign_id,)).fetchone()[0] or 0),
        "resolver_failure_count": counts["resolver_failures"], "provider_failure_count": counts["provider_failures"], "qualification_failure_count": counts["qualification_failures"],
        "latest_qualification_verdict": (qrow["status"] if qrow else campaign.get("qualification_status")), "latest_blockers": json.loads(qrow["blockers_json"] or "[]") if qrow else [],
        "source_run_ids": metrics.get("source_run_ids", []), "aggregate_evidence_hash": agg.get("evidence_hash"),
        "config_drift_status": "DRIFT" if campaign.get("last_error") in CONFIG_DRIFT_REASONS else "OK", "config_drift_reason": campaign.get("last_error") if campaign.get("last_error") in CONFIG_DRIFT_REASONS else None,
        "reconciliation_status": "SQL_DERIVED", "evidence_completeness_status": campaign.get("evidence_completeness_status"),
        "resolver_backlog_growth": backlog_growth, "evidence_completeness_regression": evidence_regression, "aggregate_contamination": aggregate_contamination, "duplicate_continuation_sequence": duplicate_seq,
    }
    unhealthy: list[str] = []
    if campaign.get("campaign_status") == "RUNNING" and not pid:
        unhealthy.append("RUNNING_WITHOUT_WORKER")
    if campaign.get("campaign_status") == "RUNNING" and pid and not alive:
        unhealthy.append("DEAD_WORKER")
    if age is None or age > max_heartbeat_age:
        unhealthy.append("STALE_HEARTBEAT")
    if campaign.get("campaign_status") == "RUNNING" and (runtime_age is None or runtime_age > max_heartbeat_age):
        unhealthy.append("RUNTIME_HEARTBEAT_STALE")
    startup_age = _age(campaign.get("worker_started_at"))
    if campaign.get("campaign_status") == "RUNNING" and startup_age is not None and startup_age > max_heartbeat_age and (scan_age is None or scan_age > max_heartbeat_age):
        unhealthy.append("RUNTIME_SCAN_STALLED")
    if campaign.get("campaign_status") in {"FAILED", "RECOVERY_REQUIRED"}:
        unhealthy.append(str(campaign.get("campaign_status")))
    if counts["open_positions"] > max_open_positions:
        unhealthy.append("UNRESOLVED_POSITION_EXCESS")
    if counts["provider_failures"] >= 3:
        unhealthy.append("REPEATED_PROVIDER_FAILURES")
    if counts["qualification_failures"]:
        unhealthy.append("QUALIFICATION_SNAPSHOT_FAILURE")
    if backlog_growth:
        unhealthy.append("RESOLVER_BACKLOG_GROWTH")
    if evidence_regression:
        unhealthy.append("EVIDENCE_COMPLETENESS_REGRESSION")
    if campaign.get("last_error") in CONFIG_DRIFT_REASONS:
        unhealthy.append(str(campaign.get("last_error")))
    if aggregate_contamination:
        unhealthy.append("SOURCE_AGGREGATE_CONTAMINATION")
    if duplicate_seq:
        unhealthy.append("DUPLICATE_CONTINUATION_SEQUENCE")
    payload["status"] = "HEALTHY" if not unhealthy else "UNHEALTHY"
    payload["unhealthy_reasons"] = unhealthy
    hid = "health_" + canonical_hash({"campaign_id": campaign_id, "at": utc_now(), "payload": payload})[:20]
    conn.execute("INSERT OR REPLACE INTO burnin_health_history(health_id,campaign_id,generated_at,status,unhealthy_reasons_json,payload_json,schema_version) VALUES (?,?,?,?,?,?,?)", (hid, campaign_id, utc_now(), payload["status"], json.dumps(unhealthy), json.dumps(payload, sort_keys=True, default=str), PHASE9_SCHEMA_VERSION))
    conn.commit()
    return payload


def persist_incident(conn: sqlite3.Connection, campaign_id: str, incident_type: str, details: Mapping[str, Any]) -> str:
    iid = "inc_" + canonical_hash({"cid": campaign_id, "type": incident_type, "at": utc_now(), "details": details})[:20]
    conn.execute("INSERT OR IGNORE INTO burnin_ops_incidents(incident_id,campaign_id,incident_type,severity,status,detected_at,details_json,schema_version) VALUES (?,?,?,?,?,?,?,?)", (iid, campaign_id, incident_type, "BLOCKING", "OPEN", utc_now(), json.dumps(details, sort_keys=True, default=str), PHASE9_SCHEMA_VERSION))
    event(conn, campaign_id, "PHASE9_INCIDENT", details={"incident_id": iid, "type": incident_type})
    conn.commit()
    return iid


def watch_once(conn: sqlite3.Connection, campaign_id: str) -> dict[str, Any]:
    cleaned_dead_worker = cleanup_dead_worker(conn, campaign_id)
    try:
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS burnin_watchdog_write_probe(x INTEGER)")
        db_write_ok = True
    except Exception:
        db_write_ok = False
    health = health_payload(conn, campaign_id)
    failures = list(health.get("unhealthy_reasons", []))
    if not db_write_ok:
        failures.append("DB_WRITE_FAILURE")
    if failures:
        persist_incident(conn, campaign_id, "WATCHDOG_FAILURE", {"failures": failures, "health": health})
        if not cleaned_dead_worker:
            conn.execute("UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED', last_error='WATCHDOG_FAILURE' WHERE campaign_id=?", (campaign_id,))
        conn.commit()
    return {"status": "OK" if not failures else "RECOVERY_REQUIRED", "failures": failures, "health": health, "cleaned_dead_worker": cleaned_dead_worker}


def _run_source_ids(conn: sqlite3.Connection, campaign_id: str) -> list[str]:
    return [r[0] for r in conn.execute("SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id=? ORDER BY continuation_sequence", (campaign_id,)).fetchall()]


def _source_rows_snapshot(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    tables = {"observations": "burnin_observations", "trades": "burnin_trade_outcomes", "rejects": "burnin_reject_outcomes", "regimes": "burnin_regime_metrics", "execution": "burnin_execution_metrics", "calibration": "burnin_calibration_metrics", "drawdowns": "burnin_drawdown_events"}
    table_rows: dict[str, dict[str, str]] = {}
    full_rows: dict[str, list[dict[str, Any]]] = {}
    for name, table in tables.items():
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE burnin_run_id=? ORDER BY id", (run_id,)).fetchall()]
        full_rows[name] = rows
        table_rows[name] = {str(r["id"]): canonical_hash(r) for r in rows}
    return {"row_hashes": table_rows, "evidence_hash": canonical_hash(full_rows)}


def _source_row_ids_and_hash(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    snap = _source_rows_snapshot(conn, run_id)
    return {"row_ids": {table: sorted(ids.keys(), key=lambda x: int(x)) for table, ids in snap["row_hashes"].items()}, "evidence_hash": snap["evidence_hash"], "row_hashes": snap["row_hashes"]}


def _run_status(conn: sqlite3.Connection, run_id: str) -> str:
    row = conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (run_id,)).fetchone()
    return str(row[0] if row else "UNKNOWN").upper()


def _terminal_run_status(status: str) -> bool:
    return status.upper() in {"RECOVERY_REQUIRED", "COMPLETED", "FAILED", "SUSPENDED"}


def _check_and_update_source_baseline(conn: sqlite3.Connection, campaign_id: str, run_id: str) -> tuple[bool, dict[str, Any]]:
    status = _run_status(conn, run_id)
    terminal = _terminal_run_status(status)
    current = _source_rows_snapshot(conn, run_id)
    old = conn.execute("SELECT evidence_hash,row_ids_json,run_status,baseline_reason FROM burnin_source_evidence_hashes WHERE campaign_id=? AND burnin_run_id=?", (campaign_id, run_id)).fetchone()
    reason = f"TERMINAL_{status}" if terminal else "RUNNING_APPEND_ONLY"
    if old is None:
        conn.execute("INSERT INTO burnin_source_evidence_hashes(campaign_id,burnin_run_id,captured_at,evidence_hash,row_ids_json,run_status,baseline_reason,schema_version) VALUES (?,?,?,?,?,?,?,?)", (campaign_id, run_id, utc_now(), current["evidence_hash"], json.dumps(current["row_hashes"], sort_keys=True), status, reason, PHASE9_SCHEMA_VERSION))
        return True, {"run_id": run_id, "status": status, "baseline_created": True, "baseline_reason": reason}
    old_hashes = json.loads(old["row_ids_json"] or "{}")
    old_terminal = str(old["baseline_reason"] or "").startswith("TERMINAL_") or _terminal_run_status(str(old["run_status"] or ""))
    missing: dict[str, list[str]] = {}
    mutated: dict[str, list[str]] = {}
    added: dict[str, list[str]] = {}
    for table, ids in old_hashes.items():
        cur_table = current["row_hashes"].get(table, {})
        for row_id, row_hash in ids.items():
            if row_id not in cur_table:
                missing.setdefault(table, []).append(row_id)
            elif cur_table[row_id] != row_hash:
                mutated.setdefault(table, []).append(row_id)
    for table, ids in current["row_hashes"].items():
        old_table = old_hashes.get(table, {})
        for row_id in ids:
            if row_id not in old_table:
                added.setdefault(table, []).append(row_id)
    if missing or mutated or (old_terminal and added):
        return False, {"run_id": run_id, "status": status, "old_status": old["run_status"], "baseline_reason": old["baseline_reason"], "missing_rows": missing, "mutated_rows": mutated, "added_rows": added, "mode": "IMMUTABLE" if old_terminal else "RUNNING_APPEND_ONLY"}
    # RUNNING runs are append-only: after preservation checks pass, extend the baseline to include appended rows.
    # When a run first reaches terminal status, freeze the full current snapshot.
    conn.execute("UPDATE burnin_source_evidence_hashes SET captured_at=?, evidence_hash=?, row_ids_json=?, run_status=?, baseline_reason=?, schema_version=? WHERE campaign_id=? AND burnin_run_id=?", (utc_now(), current["evidence_hash"], json.dumps(current["row_hashes"], sort_keys=True), status, reason, PHASE9_SCHEMA_VERSION, campaign_id, run_id))
    return True, {"run_id": run_id, "status": status, "baseline_updated": True, "baseline_reason": reason, "added_rows": added}

def evidence_hash(conn: sqlite3.Connection, campaign_id: str) -> str | None:
    return aggregate_campaign(conn, campaign_id).get("evidence_hash")


def _campaign_source_evidence_hash(conn: sqlite3.Connection, campaign_id: str) -> str:
    return canonical_hash({rid: _source_row_ids_and_hash(conn, rid)["evidence_hash"] for rid in _run_source_ids(conn, campaign_id)})


def _stop_worker(pid: Any, timeout: float = 10.0) -> bool:
    if not _pid_alive(pid):
        return True
    try:
        os.kill(int(pid), signal.SIGTERM)
    except Exception:
        return not _pid_alive(pid)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return not _pid_alive(pid)


def _authoritative_recovery_exposure(db: str, campaign_id: str) -> dict[str, Any]:
    """Read the execution-owned recovery gate before advancing a continuation.

    Campaign tables own forward-label and PAPER-position outcomes only.  Orders,
    orphan findings, and reconciliation completeness are runtime-owned, so a
    continuation recovery must consult the runtime gate as well.
    """
    engine = init_db(f"sqlite+pysqlite:///{db}")
    try:
        cfg = load_config_from_env()
        return evaluate_runtime_recovery(
            engine, mode="PAPER", campaign_id=campaign_id,
            reconciliation_probe=build_readonly_reconciliation_probe(_readonly_reconciliation_provider(cfg)),
        )
    finally:
        engine.dispose()


TERMINALIZATION_EVIDENCE_MAX_AGE_SECONDS = 120.0
TERMINALIZATION_RUNTIME_IDENTITY_FIELDS = ("id", "timestamp", "instance_id", "startup_id", "campaign_id", "burnin_run_id", "release_id", "process_id", "mode", "reconciliation_status", "exchange_read_only_status", "unknown_exchange_state", "recovery_action_required", "active_position_count", "pending_order_count", "orphan_position_count", "orphan_order_count")


def _runtime_evidence_identity(runtime: Mapping[str, Any], campaign_id: str, run_id: str) -> dict[str, Any]:
    latest = dict(runtime.get("latest") or {})
    snapshot = {key: latest.get(key) for key in TERMINALIZATION_RUNTIME_IDENTITY_FIELDS}
    observed_at = latest.get("timestamp") or latest.get("created_at")
    age = _age(observed_at)
    checks = {
        "snapshot_id_versioned": latest.get("id") is not None,
        "evidence_timestamp_present": observed_at is not None,
        "evidence_fresh": age is not None and 0 <= age <= TERMINALIZATION_EVIDENCE_MAX_AGE_SECONDS,
        "campaign_linked": latest.get("campaign_id") == campaign_id,
        "run_linked": latest.get("burnin_run_id") == run_id,
        "paper_mode": str(latest.get("mode") or "").upper() == "PAPER",
    }
    return {"snapshot_id": latest.get("id"), "observed_at": observed_at, "age_seconds": age, "snapshot_hash": canonical_hash(snapshot), "snapshot": snapshot, "checks": checks}


def _local_terminalization_snapshot(conn: sqlite3.Connection, campaign_id: str, run_id: str | None) -> dict[str, Any]:
    campaign_row = conn.execute("SELECT * FROM burnin_campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
    campaign = dict(campaign_row) if campaign_row else None
    mapping_row = conn.execute("SELECT * FROM burnin_campaign_runs WHERE campaign_id=? AND burnin_run_id=?", (campaign_id, run_id)).fetchone() if run_id else None
    run_row = conn.execute("SELECT * FROM burnin_runs WHERE burnin_run_id=?", (run_id,)).fetchone() if run_id else None
    mapping = dict(mapping_row) if mapping_row else None
    run = dict(run_row) if run_row else None
    local = _recovery_campaign_exposure(conn, campaign_id)
    execution = _startup_terminalization_evidence(conn, campaign_id, run_id)
    try:
        pending_orders = int(conn.execute("SELECT COUNT(*) FROM orders WHERE UPPER(COALESCE(mode,''))='PAPER' AND UPPER(COALESCE(status,'')) NOT IN ('FILLED','CANCELLED','REJECTED','EXPIRED','FAILED','CLOSED')").fetchone()[0] or 0)
        pending_orders_available = True
    except Exception as exc:
        pending_orders, pending_orders_available = None, False
        local.setdefault("query_errors", []).append(f"pending_orders:{exc.__class__.__name__}:{exc}")
    runtime_row = conn.execute("SELECT * FROM runtime_state_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    runtime_link = dict(runtime_row) if runtime_row else None
    worker_row = conn.execute("SELECT event_id,event_type,event_time,details_json FROM burnin_campaign_events WHERE campaign_id=? AND event_type IN ('PHASE9_DEAD_WORKER_RECOVERY_REQUIRED','PHASE9_STALE_CONTINUATION_RECOVERED') ORDER BY id DESC LIMIT 1", (campaign_id,)).fetchone()
    worker = dict(worker_row) if worker_row else None
    worker_details = json.loads(worker["details_json"] or "{}") if worker else {}
    campaign_identity = {key: campaign.get(key) if campaign else None for key in ("release_id", "config_hash", "strategy_config_hash", "universe_hash", "git_commit", "execution_cost_config_hash", "active_run_id", "worker_pid", "worker_started_at")}
    continuation_identity = {key: (mapping or {}).get(key) for key in ("campaign_id", "burnin_run_id", "continuation_sequence", "status", "started_at", "ended_at")}
    run_identity = {key: (run or {}).get(key) for key in ("burnin_run_id", "release_id", "execution_mode", "status", "git_commit", "config_hash", "strategy_config_hash", "universe_hash", "continuation_sequence")}
    return {
        "campaign": campaign, "mapping": mapping, "run": run, "local": local, "execution": execution,
        "pending_orders": pending_orders, "pending_orders_available": pending_orders_available,
        "runtime_link": runtime_link, "runtime_link_hash": canonical_hash({key: (runtime_link or {}).get(key) for key in TERMINALIZATION_RUNTIME_IDENTITY_FIELDS}),
        "worker_evidence": worker, "worker_details": worker_details,
        "campaign_identity_hash": canonical_hash(campaign_identity), "continuation_identity_hash": canonical_hash(continuation_identity), "run_identity_hash": canonical_hash(run_identity),
        "source_hash": _campaign_source_evidence_hash(conn, campaign_id),
    }


def _terminalization_pre_begin_hook() -> None:
    """Test seam for deterministic precheck/transaction race coverage."""


def terminalize_zero_exposure_recovery(conn: sqlite3.Connection, campaign_id: str) -> dict[str, Any]:
    """Atomically terminalize a fully evidenced zero-execution PAPER recovery."""
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    initial_campaign = get_campaign(conn, campaign_id)
    if not initial_campaign:
        raise KeyError("campaign not found")
    expected_run_id = initial_campaign.get("active_run_id")

    # Strict replay is tied to the exact campaign/run and recorded identities.
    prior = conn.execute("SELECT event_id,details_json FROM burnin_campaign_events WHERE campaign_id=? AND burnin_run_id IS ? AND event_type='PHASE9_MANUAL_ZERO_EXPOSURE_TERMINALIZED' ORDER BY id DESC LIMIT 1", (campaign_id, expected_run_id)).fetchone()
    if initial_campaign.get("campaign_status") == "FAILED":
        if not prior:
            raise RuntimeError("FAILED_CAMPAIGN_HAS_NO_MATCHING_TERMINALIZATION_AUDIT")
        details = json.loads(prior["details_json"] or "{}")
        current_run = conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (expected_run_id,)).fetchone()
        current_mapping = conn.execute("SELECT status FROM burnin_campaign_runs WHERE campaign_id=? AND burnin_run_id=?", (campaign_id, expected_run_id)).fetchone()
        current_link = _local_terminalization_snapshot(conn, campaign_id, expected_run_id)
        if not current_run or not current_mapping or current_run[0] != "FAILED" or current_mapping[0] != "FAILED" or details.get("idempotency_identity") is None or details.get("source_hash") != current_link["source_hash"] or details.get("runtime_evidence", {}).get("snapshot_hash") != current_link["runtime_link_hash"]:
            raise RuntimeError("TERMINALIZATION_REPLAY_IDENTITY_MISMATCH")
        return {"status": "PASS", "campaign_id": campaign_id, "burnin_run_id": expected_run_id, "terminal_status": "FAILED", "idempotent_replay": True, "event_id": prior["event_id"], "idempotency_identity": details["idempotency_identity"]}
    initial_status = initial_campaign.get("campaign_status")
    if initial_status not in {"STARTING", "RECOVERY_REQUIRED"}:
        raise RuntimeError("MANUAL_TERMINALIZATION_REQUIRES_STARTING_OR_RECOVERY_REQUIRED")

    precheck = _local_terminalization_snapshot(conn, campaign_id, expected_run_id)
    preliminary_failures: list[str] = []
    preliminary_run = precheck.get("run") or {}
    preliminary_local = precheck.get("local") or {}
    worker_pid = (precheck.get("worker_details") or {}).get("worker_pid") or initial_campaign.get("worker_pid")
    if not expected_run_id or precheck.get("mapping") is None or not preliminary_run:
        preliminary_failures.append("CONTINUATION_MISSING")
    if str(preliminary_run.get("execution_mode") or "").upper() != "PAPER":
        preliminary_failures.append("PAPER_MODE_REQUIRED")
    if worker_pid is None:
        preliminary_failures.append("WORKER_DEATH_IDENTITY_UNAVAILABLE")
    elif _pid_alive(worker_pid) or bool(initial_campaign.get("worker_pid") and _pid_alive(initial_campaign.get("worker_pid"))):
        preliminary_failures.append("WORKER_BECAME_ALIVE")
    if not all(bool(v) for v in (preliminary_local.get("availability") or {}).values()) or preliminary_local.get("query_errors"):
        preliminary_failures.append("LOCAL_EXPOSURE_UNAVAILABLE")
    if preliminary_local.get("open_positions") != 0 or preliminary_local.get("pending_reject_labels") != 0:
        preliminary_failures.append("LOCAL_EXPOSURE_NONZERO")
    if preliminary_failures:
        return {"status": "FAIL_CLOSED", "campaign_id": campaign_id, "burnin_run_id": expected_run_id, "failure_reasons": preliminary_failures}
    runtime = _authoritative_recovery_exposure(db, campaign_id)
    runtime_identity = _runtime_evidence_identity(runtime, campaign_id, expected_run_id)
    if not all(runtime_identity["checks"].values()):
        availability = dict(runtime.get("availability") or {})
        counts = dict(runtime.get("current_exposure_check") or {})
        probe = runtime.get("reconciliation_probe")
        bridge_safe = (
            isinstance(probe, Mapping) and runtime.get("reconciliation_probe_clean") is True
            and probe.get("authenticated") is True
            and str(probe.get("input_source") or "").upper() == "AUTHENTICATED_EXCHANGE_SNAPSHOT"
            and (not runtime.get("blocked") or (
                runtime.get("scope") == "SAME_CAMPAIGN" and runtime.get("prior_unclean") is True
                and runtime.get("previous_process_alive") is False
            ))
            and not runtime.get("query_errors")
            and runtime.get("kill_switch_active") is False
            and all(bool(availability.get(key)) for key in ("active_positions_available", "pending_orders_available", "orphan_evidence_available", "kill_switch_available"))
            and all(counts.get(key) == 0 for key in ("active_positions", "pending_orders", "orphan_orders", "orphan_positions"))
        )
        if bridge_safe:
            engine = init_db(f"sqlite+pysqlite:///{db}")
            try:
                persisted = persist_campaign_linked_zero_exposure_reconciliation_evidence(
                    engine, probe=probe, prior_snapshot=runtime.get("latest"), campaign_id=campaign_id,
                    burnin_run_id=expected_run_id, release_id=str(initial_campaign.get("release_id") or ""),
                )
            finally:
                engine.dispose()
            runtime = {**runtime, "latest": persisted, "reconciliation_status": "CLEAN", "blocked": False}
            runtime_identity = _runtime_evidence_identity(runtime, campaign_id, expected_run_id)
    _terminalization_pre_begin_hook()

    try:
        conn.execute("BEGIN IMMEDIATE")
        final = _local_terminalization_snapshot(conn, campaign_id, expected_run_id)
        campaign, mapping, run = final["campaign"], final["mapping"], final["run"]
        failures: list[str] = []
        def require(condition: bool, reason: str) -> None:
            if not condition: failures.append(reason)
        require(campaign is not None, "CAMPAIGN_MISSING")
        require(bool(campaign and campaign.get("campaign_status") == initial_status), "CAMPAIGN_STATUS_CHANGED")
        require(bool(campaign and campaign.get("active_run_id") == expected_run_id), "ACTIVE_RUN_CHANGED")
        require(mapping is not None and run is not None, "CONTINUATION_MISSING")
        require(bool(mapping and mapping.get("status") == initial_status and run and run.get("status") == initial_status), "CONTINUATION_STATUS_CHANGED")
        require(final["campaign_identity_hash"] == precheck["campaign_identity_hash"] and final["continuation_identity_hash"] == precheck["continuation_identity_hash"] and final["run_identity_hash"] == precheck["run_identity_hash"], "IDENTITY_HASH_CHANGED")
        require(final["source_hash"] == precheck["source_hash"], "SOURCE_HASH_CHANGED")
        require(final["runtime_link_hash"] == runtime_identity["snapshot_hash"], "EXTERNAL_EVIDENCE_LINKAGE_CHANGED")
        require(all(runtime_identity["checks"].values()), "EXTERNAL_EVIDENCE_INVALID_OR_STALE")
        current_evidence_age = _age(runtime_identity["observed_at"])
        require(current_evidence_age is not None and 0 <= current_evidence_age <= TERMINALIZATION_EVIDENCE_MAX_AGE_SECONDS, "EXTERNAL_EVIDENCE_BECAME_STALE")
        pid = (campaign or {}).get("worker_pid")
        require(pid == (precheck["campaign"] or {}).get("worker_pid") and (campaign or {}).get("worker_started_at") == (precheck["campaign"] or {}).get("worker_started_at"), "WORKER_IDENTITY_CHANGED")
        worker_pid = final["worker_details"].get("worker_pid") or pid
        require(final["worker_evidence"] == precheck["worker_evidence"] and final["worker_details"] == precheck["worker_details"], "WORKER_IDENTITY_CHANGED")
        require(worker_pid is not None, "WORKER_DEATH_IDENTITY_UNAVAILABLE")
        require(not _pid_alive(worker_pid) and not bool(pid and _pid_alive(pid)), "WORKER_BECAME_ALIVE")
        local = final["local"]; execution = final["execution"]
        require(all(bool(v) for v in (local.get("availability") or {}).values()) and not local.get("query_errors"), "LOCAL_EXPOSURE_UNAVAILABLE")
        require(local.get("open_positions") == 0, "OPEN_POSITIONS_NONZERO")
        require(final["pending_orders_available"] and final["pending_orders"] == 0, "PENDING_ORDERS_NONZERO_OR_UNAVAILABLE")
        require(local.get("pending_reject_labels") == 0, "PENDING_REJECTS_NONZERO")
        require(execution.get("available") is True and execution.get("executions") == 0, "EXECUTIONS_NONZERO_OR_UNAVAILABLE")
        require(execution.get("lifecycle_executions") == 0, "LIFECYCLE_EXECUTIONS_NONZERO")
        availability = dict(runtime.get("availability") or {}); counts = dict(runtime.get("current_exposure_check") or {})
        require(all(bool(availability.get(k)) for k in ("active_positions_available", "pending_orders_available", "orphan_evidence_available", "kill_switch_available")) and not runtime.get("query_errors"), "RUNTIME_EXPOSURE_UNAVAILABLE")
        require(all(counts.get(k) == 0 for k in ("active_positions", "pending_orders", "orphan_orders", "orphan_positions")), "RUNTIME_EXPOSURE_NONZERO")
        require(runtime.get("kill_switch_active") is False and runtime.get("reconciliation_status") == "CLEAN" and not runtime.get("blocked"), "RECONCILIATION_NOT_CLEAN")
        if failures:
            conn.rollback()
            return {"status": "FAIL_CLOSED", "campaign_id": campaign_id, "burnin_run_id": expected_run_id, "failure_reasons": failures}

        observed = runtime_identity["observed_at"]
        replay_identity = "term_" + canonical_hash({"campaign_id": campaign_id, "run_id": expected_run_id, "source_hash": final["source_hash"], "runtime_evidence": runtime_identity})[:24]
        details = {"campaign_id": campaign_id, "run_id": expected_run_id, "previous_campaign_status": campaign["campaign_status"], "previous_continuation_status": mapping["status"], "terminal_status": "FAILED", "operator_action": "--terminalize-zero-exposure", "local_exposure": {"open_positions": local["open_positions"], "pending_orders": final["pending_orders"]}, "execution_count": execution["executions"], "lifecycle_execution_count": execution["lifecycle_executions"], "pending_reject_count": local["pending_reject_labels"], "reconciliation_status": runtime["reconciliation_status"], "runtime_evidence": runtime_identity, "source_hash": final["source_hash"], "campaign_identity_hash": final["campaign_identity_hash"], "continuation_identity_hash": final["continuation_identity_hash"], "worker_evidence_id": (final["worker_evidence"] or {}).get("event_id"), "worker_evidence_time": (final["worker_evidence"] or {}).get("event_time"), "worker_pid": worker_pid, "idempotency_identity": replay_identity, "evidence_observed_at": observed}
        now = utc_now()
        updates = [
            conn.execute("UPDATE burnin_runs SET status='FAILED', end_time=COALESCE(end_time,?) WHERE burnin_run_id=? AND status=? AND release_id=? AND git_commit=? AND config_hash IS ? AND strategy_config_hash IS ? AND universe_hash IS ?", (now, expected_run_id, initial_status, run.get("release_id"), run.get("git_commit"), run.get("config_hash"), run.get("strategy_config_hash"), run.get("universe_hash"))),
            conn.execute("UPDATE burnin_campaign_runs SET status='FAILED', ended_at=COALESCE(ended_at,?) WHERE campaign_id=? AND burnin_run_id=? AND continuation_sequence=? AND status=?", (now, campaign_id, expected_run_id, mapping.get("continuation_sequence"), initial_status)),
            conn.execute("UPDATE burnin_campaigns SET campaign_status='FAILED', last_error='MANUAL_ZERO_EXPOSURE_TERMINALIZED', worker_pid=NULL, worker_started_at=NULL WHERE campaign_id=? AND campaign_status=? AND active_run_id=? AND release_id=? AND git_commit=? AND config_hash=? AND strategy_config_hash=? AND universe_hash=? AND execution_cost_config_hash IS ?", (campaign_id, initial_status, expected_run_id, campaign["release_id"], campaign["git_commit"], campaign["config_hash"], campaign["strategy_config_hash"], campaign["universe_hash"], campaign.get("execution_cost_config_hash"))),
        ]
        if any(result.rowcount != 1 for result in updates):
            conn.rollback()
            return {"status": "FAIL_CLOSED", "campaign_id": campaign_id, "burnin_run_id": expected_run_id, "failure_reasons": ["CONDITIONAL_UPDATE_ROWCOUNT_MISMATCH"], "rowcounts": [r.rowcount for r in updates]}
        event(conn, campaign_id, "PHASE9_MANUAL_ZERO_EXPOSURE_TERMINALIZED", burnin_run_id=expected_run_id, details=details)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {"status": "PASS", "campaign_id": campaign_id, "burnin_run_id": expected_run_id, "terminal_status": "FAILED", "idempotent_replay": False, "idempotency_identity": replay_identity, "runtime_evidence": runtime_identity, "source_evidence_hash": final["source_hash"]}


def recovery_drill(conn: sqlite3.Connection, campaign_id: str, *, attach_timeout_seconds: float = 60.0) -> dict[str, Any]:
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    campaign = get_campaign(conn, campaign_id)
    if not campaign:
        raise KeyError("campaign not found")
    # A completed drill already attached the current continuation.  Replaying its
    # operator request must not stop that verified worker and allocate another
    # successor; a later genuinely dead worker bypasses this branch.
    previous = conn.execute("SELECT * FROM burnin_recovery_drills WHERE campaign_id=? AND status='PASS' ORDER BY id DESC LIMIT 1", (campaign_id,)).fetchone()
    if previous and campaign.get("worker_pid") and _pid_alive(campaign.get("worker_pid")):
        previous_after = json.loads(previous["after_json"] or "{}")
        previous_resume = previous_after.get("resume") or {}
        if previous_resume.get("burnin_run_id") == campaign.get("active_run_id"):
            return {"drill_id": previous["drill_id"], "campaign_id": campaign_id, "generated_at": utc_now(), "status": "PASS", "checks": {**json.loads(previous["checks_json"] or "{}"), "idempotent_replay": True}, "before": json.loads(previous["before_json"] or "{}"), "after": {**previous_after, "idempotent_replay": True}}
    source_before = {rid: _source_row_ids_and_hash(conn, rid) for rid in _run_source_ids(conn, campaign_id)}
    pending_ids_before = [r[0] for r in conn.execute("SELECT pending_label_id FROM burnin_pending_reject_labels WHERE campaign_id=? AND status IN ('PENDING','READY') ORDER BY pending_label_id", (campaign_id,)).fetchall()]
    position_ids_before = [r[0] for r in conn.execute("SELECT pending_position_id FROM burnin_pending_position_outcomes WHERE campaign_id=? AND status='OPEN' ORDER BY pending_position_id", (campaign_id,)).fetchall()]
    old_run = campaign.get("active_run_id")
    old_pid = campaign.get("worker_pid")
    started_at = campaign.get("started_at")
    restart_count = int(campaign.get("restart_count") or 0)
    runs_before = _run_source_ids(conn, campaign_id)
    old_hash = _campaign_source_evidence_hash(conn, campaign_id)
    old_status = _run_status(conn, old_run) if old_run else "UNKNOWN"
    old_alive = bool(old_pid and _pid_alive(old_pid))
    exposure = _recovery_campaign_exposure(conn, campaign_id)
    startup_evidence = _startup_terminalization_evidence(conn, campaign_id, old_run)
    runtime_recovery = _authoritative_recovery_exposure(db, campaign_id)
    original_runtime_recovery = dict(runtime_recovery)

    # A worker that actually scanned while STARTING crossed the startup boundary
    # but lost the launcher's status write.  Do not resume this ambiguous run.
    # The terminalizer independently re-probes and then re-reads every predicate
    # under BEGIN IMMEDIATE, so this dispatch is not itself a safety decision.
    stale_scanning_start = (
        campaign.get("campaign_status") == "STARTING"
        and old_status == "STARTING"
        and not old_alive
        and old_pid is not None
        and startup_evidence.get("available") is True
        and startup_evidence.get("run_mode") == "PAPER"
        and int(startup_evidence.get("decisions") or 0) > 0
    )
    if stale_scanning_start:
        terminal = terminalize_zero_exposure_recovery(conn, campaign_id)
        if terminal.get("status") == "PASS":
            return {
                "drill_id": "drill_" + canonical_hash({"cid": campaign_id, "terminal": terminal.get("idempotency_identity")})[:20],
                "campaign_id": campaign_id, "generated_at": utc_now(), "status": "PASS",
                "checks": {"stale_scanning_start": True, "safe_terminalization": True,
                           "run_decisions": startup_evidence.get("decisions")},
                "before": {"run_ids": runs_before, "pending_reject_ids": pending_ids_before,
                           "open_position_ids": position_ids_before, "source_hash": old_hash},
                "after": {"run_ids": runs_before, "resume": None, "attach": None,
                          "terminalization": terminal},
            }
        # Preserve the normal incident/audit path for a fail-closed result.

    def runtime_zero_available(decision: Mapping[str, Any]) -> bool:
        runtime_exposure_local = dict(decision.get("current_exposure_check") or {})
        runtime_availability = dict(decision.get("availability") or {})
        required_available = all(bool(runtime_availability.get(key)) for key in ("active_positions_available", "pending_orders_available", "orphan_evidence_available", "kill_switch_available"))
        return required_available and not any(int(runtime_exposure_local.get(name) or 0) for name in ("active_positions", "pending_orders", "orphan_orders", "orphan_positions")) and not decision.get("kill_switch_active")

    runtime_exposure = dict(runtime_recovery.get("current_exposure_check") or {})
    unsafe_exposure = {name: int(runtime_exposure.get(name) or 0) for name in ("active_positions", "pending_orders", "orphan_orders", "orphan_positions")}
    stale_dead_worker = old_status == "RUNNING" and not old_alive
    provider_unavailable_errors = list(runtime_recovery.get("provider_unavailable_errors") or [])
    all_runtime_query_errors = list(runtime_recovery.get("query_errors") or [])
    provider_only_error = bool(provider_unavailable_errors) and sorted(provider_unavailable_errors) == sorted(all_runtime_query_errors)
    campaign_available = all((exposure.get("availability") or {}).values()) and not exposure.get("query_errors")
    zero_campaign_exposure = exposure["open_positions"] == 0 and exposure["pending_reject_labels"] == 0
    local_runtime_zero_available = runtime_zero_available(runtime_recovery)
    mode_safe_for_local_fallback = str((runtime_recovery.get("latest") or {}).get("mode") or "PAPER").upper() not in {"LIVE", "LIVE_PRECHECK"}
    historical_zero_local_fallback_candidate = (
        stale_dead_worker
        and mode_safe_for_local_fallback
        and runtime_recovery.get("scope") == "UNRELATED_HISTORICAL_RUNTIME"
        and runtime_recovery.get("previous_process_alive") is False
        and not bool(old_pid and old_alive)
        and campaign_available
        and zero_campaign_exposure
        and local_runtime_zero_available
        and runtime_recovery.get("reason") == "RECOVERY_EVIDENCE_UNAVAILABLE"
        and provider_only_error
    )
    campaign_status = campaign.get("campaign_status")
    campaign_last_error = campaign.get("last_error")
    campaign_state_terminalizable = (
        campaign_status in {"FAILED", "PAUSED"}
        or (campaign_status == "RECOVERY_REQUIRED" and campaign_last_error == "RECOVERY_DRILL_PRECHECK_FAILED")
    )
    terminal_zero_startup_fallback_candidate = (
        # PAUSED is an operator state, while the narrowly stamped recovery state
        # can be this drill's own prior mutation. Permit either only through this
        # provider-only zero-activity path; the campaign is never resumed below.
        campaign_state_terminalizable
        and old_status == "FAILED"
        and not old_alive
        and not bool(old_pid)
        and startup_evidence["available"]
        and startup_evidence["run_mode"] == "PAPER"
        and startup_evidence["decisions"] == 0
        and startup_evidence["executions"] == 0
        and startup_evidence["lifecycle_executions"] == 0
        and mode_safe_for_local_fallback
        and runtime_recovery.get("previous_process_alive") is False
        and campaign_available
        and zero_campaign_exposure
        and local_runtime_zero_available
        and runtime_recovery.get("reason") == "RECOVERY_EVIDENCE_UNAVAILABLE"
        and provider_only_error
    )
    recovery_safe = not runtime_recovery.get("blocked") and campaign_available and zero_campaign_exposure and local_runtime_zero_available
    prechecks = {"worker_pid_present": bool(old_pid), "worker_alive_before_stop": old_alive, "active_run_status_running": old_status == "RUNNING", "campaign_status": campaign_status, "last_error": campaign_last_error, "campaign_state_terminalizable": campaign_state_terminalizable, "campaign_open_positions": exposure["open_positions"], "pending_reject_labels": exposure["pending_reject_labels"], "campaign_exposure_available": campaign_available, "campaign_query_errors": exposure.get("query_errors", []), "startup_terminalization_evidence": startup_evidence, "runtime_exposure": unsafe_exposure, "runtime_recovery_blocked": bool(runtime_recovery.get("blocked")), "runtime_recovery_reason": runtime_recovery.get("reason"), "runtime_availability": runtime_recovery.get("availability", {}), "provider_only_error": provider_only_error, "historical_zero_local_fallback": False, "terminal_zero_startup_fallback_candidate": terminal_zero_startup_fallback_candidate}
    run_decisions = startup_evidence.get("decisions")
    zero_exposure_failed_startup = (
        old_status in {"FAILED", "STARTING"}
        and not old_alive
        and not bool(old_pid)
        and campaign.get("campaign_status") in {"FAILED", "RECOVERY_REQUIRED", "STARTING"}
        and (str(campaign.get("last_error") or "") in STARTUP_FAILURE_REASONS | {"DEAD_WORKER"} or str(campaign.get("last_error") or "").startswith("PHASE8_CAMPAIGN_"))
        and startup_evidence["available"]
        and startup_evidence["run_mode"] == "PAPER"
        and run_decisions == 0
        and startup_evidence["executions"] == 0
        and startup_evidence["lifecycle_executions"] == 0
        and recovery_safe
    )
    prechecks["run_decisions"] = run_decisions
    prechecks["zero_exposure_failed_startup_terminalizable"] = zero_exposure_failed_startup

    if historical_zero_local_fallback_candidate or terminal_zero_startup_fallback_candidate:
        engine = init_db(f"sqlite+pysqlite:///{db}")
        try:
            persist_historical_paper_recovery_without_provider(engine, prior_snapshot=runtime_recovery.get("latest"), diagnostics={"campaign_id": campaign_id, "old_run_id": old_run, "worker_pid": old_pid, "worker_alive": old_alive, "campaign_exposure": exposure, "startup_terminalization_evidence": startup_evidence, "runtime_recovery": runtime_recovery, "provider_unavailable_reason": "read_only_reconciliation_provider_unavailable", "recovery_scope": "TERMINAL_ZERO_EXECUTION_STARTUP" if terminal_zero_startup_fallback_candidate else "UNRELATED_HISTORICAL_RUNTIME"})
            runtime_recovery = _authoritative_recovery_exposure(db, campaign_id)
            runtime_recovery["fallback_original_runtime_recovery"] = original_runtime_recovery
            prechecks["runtime_recovery_after_fallback"] = runtime_recovery
            recovery_safe = not runtime_recovery.get("blocked") and campaign_available and zero_campaign_exposure and runtime_zero_available(runtime_recovery)
            prechecks["historical_zero_local_fallback"] = recovery_safe
        finally:
            engine.dispose()
        if terminal_zero_startup_fallback_candidate:
            zero_exposure_failed_startup = recovery_safe
            prechecks["zero_exposure_failed_startup_terminalizable"] = zero_exposure_failed_startup
    if zero_exposure_failed_startup:
        conn.execute("UPDATE burnin_campaigns SET campaign_status='FAILED', worker_pid=NULL, worker_started_at=NULL WHERE campaign_id=?", (campaign_id,))
        evidence = {"old_run_id": old_run, "old_status": old_status, "campaign_exposure": exposure, "runtime_recovery": runtime_recovery, "run_decisions": run_decisions, "policy": "SAFE_TERMINALIZATION"}
        event(conn, campaign_id, "PHASE9_ZERO_EXPOSURE_STARTUP_FAILURE_TERMINALIZED", burnin_run_id=old_run, details=evidence)
        payload = {"drill_id": "drill_" + canonical_hash({"cid": campaign_id, "at": utc_now(), "terminalized": old_run})[:20], "campaign_id": campaign_id, "generated_at": utc_now(), "status": "PASS", "checks": {**prechecks, "safe_terminalization": True}, "before": {"run_ids": runs_before, "pending_reject_ids": pending_ids_before, "open_position_ids": position_ids_before, "source_hash": old_hash}, "after": {"run_ids": runs_before, "resume": None, "attach": None, "terminalization": evidence}}
        conn.execute("INSERT OR REPLACE INTO burnin_recovery_drills(drill_id,campaign_id,generated_at,status,checks_json,before_json,after_json,schema_version) VALUES (?,?,?,?,?,?,?,?)", (payload["drill_id"], campaign_id, payload["generated_at"], "PASS", json.dumps(payload["checks"]), json.dumps(payload["before"]), json.dumps(payload["after"], default=str), PHASE9_SCHEMA_VERSION))
        conn.commit()
        return payload
    if stale_dead_worker and recovery_safe:
        # PID metadata is attachment evidence, not a prerequisite for recovery.
        # Terminalize both linked rows before allocating a successor.
        ts = utc_now()
        conn.execute("UPDATE burnin_runs SET status='RECOVERY_REQUIRED', end_time=COALESCE(end_time,?) WHERE burnin_run_id=? AND status='RUNNING'", (ts, old_run))
        conn.execute("UPDATE burnin_campaign_runs SET status='RECOVERY_REQUIRED', ended_at=COALESCE(ended_at,?) WHERE campaign_id=? AND burnin_run_id=? AND status='RUNNING'", (ts, campaign_id, old_run))
        conn.execute("UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED', worker_pid=NULL, worker_started_at=NULL, last_error='DEAD_WORKER_ZERO_EXPOSURE_RECOVERY_REQUIRED' WHERE campaign_id=?", (campaign_id,))
        evidence = {"old_run_id": old_run, "old_status": old_status, "worker_pid": old_pid, "worker_alive": old_alive, "heartbeat_at": campaign.get("last_heartbeat_at"), "campaign_exposure": exposure, "runtime_recovery": runtime_recovery, "transition": "RUNNING->RECOVERY_REQUIRED"}
        event(conn, campaign_id, "PHASE9_STALE_CONTINUATION_RECOVERED", burnin_run_id=old_run, details=evidence)
        persist_incident(conn, campaign_id, "STALE_CONTINUATION_ZERO_EXPOSURE", evidence)
        conn.commit()
    elif not all((bool(old_pid), old_alive, old_status == "RUNNING")):
        conn.execute("UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED', last_error='RECOVERY_DRILL_PRECHECK_FAILED' WHERE campaign_id=?", (campaign_id,))
        failure_reason = "UNRESOLVED_RUNTIME_EXPOSURE_OR_RECONCILIATION" if stale_dead_worker else "STALE_OR_INVALID_CONTINUATION_REQUIRES_MANUAL_RECOVERY"
        failure = {"reason": failure_reason, "prechecks": prechecks, "old_run_id": old_run, "runtime_recovery": runtime_recovery, "transition_attempted": None}
        persist_incident(conn, campaign_id, "RECOVERY_DRILL_PRECHECK_FAILED", failure)
        payload = {"drill_id": "drill_" + canonical_hash({"cid": campaign_id, "at": utc_now(), "precheck": prechecks})[:20], "campaign_id": campaign_id, "generated_at": utc_now(), "status": "FAIL", "checks": {**prechecks, "failure_reasons": [failure["reason"]]}, "before": {"run_ids": runs_before, "pending_reject_ids": pending_ids_before, "open_position_ids": position_ids_before, "source_hash": old_hash}, "after": {"run_ids": runs_before, "resume": None, "attach": None, "failure": failure}}
        conn.execute("INSERT OR REPLACE INTO burnin_recovery_drills(drill_id,campaign_id,generated_at,status,checks_json,before_json,after_json,schema_version) VALUES (?,?,?,?,?,?,?,?)", (payload["drill_id"], campaign_id, payload["generated_at"], "FAIL", json.dumps(payload["checks"]), json.dumps(payload["before"]), json.dumps(payload["after"]), PHASE9_SCHEMA_VERSION)); conn.commit()
        return payload
    terminated = True if stale_dead_worker else _stop_worker(old_pid)
    if not terminated:
        conn.execute("UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED', last_error='RECOVERY_DRILL_WORKER_TERMINATION_FAILED' WHERE campaign_id=?", (campaign_id,))
        failure = {"reason": "RECOVERY_DRILL_WORKER_TERMINATION_FAILED", "worker_pid": old_pid, "active_run_id": old_run, "transition_attempted": None}
        persist_incident(conn, campaign_id, "RECOVERY_DRILL_WORKER_TERMINATION_FAILED", failure)
        payload = {"drill_id": "drill_" + canonical_hash({"cid": campaign_id, "at": utc_now(), "termination_failed": old_pid})[:20], "campaign_id": campaign_id, "generated_at": utc_now(), "status": "FAIL", "checks": {**prechecks, "worker_terminated": False, "no_resume_attempted": True, "failure_reasons": [failure["reason"]]}, "before": {"run_ids": runs_before, "pending_reject_ids": pending_ids_before, "open_position_ids": position_ids_before, "source_hash": old_hash}, "after": {"run_ids": runs_before, "resume": None, "attach": None, "failure": failure}}
        conn.execute("INSERT OR REPLACE INTO burnin_recovery_drills(drill_id,campaign_id,generated_at,status,checks_json,before_json,after_json,schema_version) VALUES (?,?,?,?,?,?,?,?)", (payload["drill_id"], campaign_id, payload["generated_at"], "FAIL", json.dumps(payload["checks"]), json.dumps(payload["before"]), json.dumps(payload["after"]), PHASE9_SCHEMA_VERSION)); conn.commit()
        return payload
    resume = start_or_resume_campaign(conn, campaign_id, resume=True)
    if old_run:
        _check_and_update_source_baseline(conn, campaign_id, old_run)
    worker_started_at = utc_now()
    proc = _launch_worker(db, campaign_id)
    conn.execute("UPDATE burnin_campaigns SET worker_pid=?, worker_started_at=? WHERE campaign_id=?", (proc.pid, worker_started_at, campaign_id))
    conn.commit()
    attach = verify_worker_attachment(conn, campaign_id, worker_started_at=worker_started_at, launch_started_at=worker_started_at, timeout_seconds=attach_timeout_seconds)
    current = get_campaign(conn, campaign_id) or {}
    runs_after = _run_source_ids(conn, campaign_id)
    source_after = {rid: _source_row_ids_and_hash(conn, rid) for rid in runs_before}
    new_hash = _campaign_source_evidence_hash(conn, campaign_id)
    try:
        engine = create_engine(f"sqlite+pysqlite:///{db}", future=True)
        q = qualify_campaign(engine, campaign_id)
    except Exception as exc:
        q = {"error": str(exc)}
    finally:
        try:
            engine.dispose()
        except Exception:
            pass
    qrow = conn.execute("SELECT source_run_ids_json FROM burnin_qualification_snapshots WHERE campaign_id=? ORDER BY id DESC LIMIT 1", (campaign_id,)).fetchone()
    q_source = json.loads(qrow[0] or "[]") if qrow else []
    checks = {
        "worker_terminated": terminated,
        "old_continuation_recovery_required": bool(old_run and conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (old_run,)).fetchone()[0] == "RECOVERY_REQUIRED"),
        "new_worker_started": bool(proc.pid and _pid_alive(proc.pid)),
        "new_attach_event": attach.get("status") == "ATTACHED",
        "exactly_one_new_continuation": len(runs_after) == len(runs_before) + 1,
        "pending_reject_ids_preserved_exactly": pending_ids_before == [r[0] for r in conn.execute("SELECT pending_label_id FROM burnin_pending_reject_labels WHERE campaign_id=? AND status IN ('PENDING','READY') ORDER BY pending_label_id", (campaign_id,)).fetchall()],
        "open_position_ids_preserved_exactly": position_ids_before == [r[0] for r in conn.execute("SELECT pending_position_id FROM burnin_pending_position_outcomes WHERE campaign_id=? AND status='OPEN' ORDER BY pending_position_id", (campaign_id,)).fetchall()],
        "campaign_start_time_unchanged": current.get("started_at") == started_at,
        "restart_count_incremented_once": int(current.get("restart_count") or 0) == restart_count + 1,
        "qualification_includes_all_source_runs": q_source == runs_after,
        "source_evidence_immutable": source_before == source_after,
        "evidence_hash_changes_from_source_evidence": (old_hash != new_hash) == (source_before != {rid: _source_row_ids_and_hash(conn, rid) for rid in runs_after}),
    }
    status = "PASS" if all(checks.values()) else "FAIL"
    payload = {"drill_id": "drill_" + canonical_hash({"cid": campaign_id, "at": utc_now()})[:20], "campaign_id": campaign_id, "generated_at": utc_now(), "status": status, "checks": {**prechecks, **checks}, "before": {"run_ids": runs_before, "pending_reject_ids": pending_ids_before, "open_position_ids": position_ids_before, "source_hash": old_hash}, "after": {"run_ids": runs_after, "source_hash": new_hash, "resume": resume, "attach": attach, "qualification": q}}
    conn.execute("INSERT OR REPLACE INTO burnin_recovery_drills(drill_id,campaign_id,generated_at,status,checks_json,before_json,after_json,schema_version) VALUES (?,?,?,?,?,?,?,?)", (payload["drill_id"], campaign_id, payload["generated_at"], status, json.dumps(payload["checks"]), json.dumps(payload["before"], default=str), json.dumps(payload["after"], default=str), PHASE9_SCHEMA_VERSION))
    conn.commit()
    return payload


def _dashboard_campaign_snapshot(db: str, campaign_id: str) -> dict[str, Any]:
    from alphaforge.dashboard.queries import fetch_phase8_campaign
    engine = create_engine(f"sqlite+pysqlite:///{db}", future=True)
    try:
        return fetch_phase8_campaign(engine, campaign_id)
    finally:
        engine.dispose()


def audit_payload(conn: sqlite3.Connection, campaign_id: str) -> dict[str, Any]:
    campaign = get_campaign(conn, campaign_id)
    checks: list[dict[str, Any]] = []
    violations: list[str] = []

    def chk(name: str, ok: bool, details: Any = None) -> None:
        checks.append({"name": name, "status": "PASS" if ok else "FAIL", "details": details})
        if not ok:
            violations.append(name)

    if not campaign:
        return {"status": "FAIL", "violations": ["NO_CAMPAIGN"], "checks": []}
    run_rows = [dict(r) for r in conn.execute("SELECT * FROM burnin_campaign_runs WHERE campaign_id=? ORDER BY continuation_sequence", (campaign_id,)).fetchall()]
    run_ids = [r["burnin_run_id"] for r in run_rows]
    seq = [r["continuation_sequence"] for r in run_rows]
    chk("every_campaign_run_belongs_to_exactly_one_campaign", all(conn.execute("SELECT COUNT(*) FROM burnin_campaign_runs WHERE burnin_run_id=?", (rid,)).fetchone()[0] == 1 for rid in run_ids), run_ids)
    chk("continuation_sequence_unique_monotonic", seq == sorted(set(seq)), seq)
    chk("aggregate_run_excluded_from_source_run_list", not any(str(rid).endswith("__aggregate") for rid in run_ids), run_ids)
    chk("no_recursive_aggregate_rows", conn.execute("SELECT COUNT(*) FROM burnin_campaign_runs WHERE campaign_id=? AND LOWER(burnin_run_id) LIKE '%aggregate%'", (campaign_id,)).fetchone()[0] == 0)
    if run_ids:
        ph = ",".join("?" for _ in run_ids)
        chk("no_entry_only_records_counted_as_closed_trades", conn.execute(f"SELECT COUNT(*) FROM burnin_trade_outcomes WHERE burnin_run_id IN ({ph}) AND closed_at IS NULL AND evidence_complete=1", run_ids).fetchone()[0] == 0)
        chk("no_incomplete_outcomes_counted_complete", conn.execute(f"SELECT COUNT(*) FROM burnin_trade_outcomes WHERE burnin_run_id IN ({ph}) AND closed_at IS NOT NULL AND evidence_complete=0", run_ids).fetchone()[0] == 0)
        chk("no_missing_cost_fields_in_qualified_outcomes", conn.execute(f"SELECT COUNT(*) FROM burnin_trade_outcomes WHERE burnin_run_id IN ({ph}) AND evidence_complete=1 AND (total_execution_cost IS NULL OR net_r IS NULL)", run_ids).fetchone()[0] == 0)
        bad_pre_decision = 0
        bad_dual_hit = 0
        provider_expired = 0
        for row in conn.execute(f"SELECT * FROM burnin_reject_outcomes WHERE burnin_run_id IN ({ph})", run_ids).fetchall():
            payload = json.loads(row["payload_json"] or "{}")
            decision_dt = _dt(row["decision_time"])
            for ts in payload.get("candle_timestamps", []) or payload.get("post_decision_candle_timestamps", []):
                if decision_dt and _dt(ts) and _dt(ts) <= decision_dt:
                    bad_pre_decision += 1
            if payload.get("same_candle_dual_hit") and not bool(row["ambiguous"]):
                bad_dual_hit += 1
            if str(row["forward_label"]).upper() == "EXPIRED" and (payload.get("provider_failure") or payload.get("market_data_error")):
                provider_expired += 1
        chk("rejected_labels_use_post_decision_candles_only", bad_pre_decision == 0, {"bad_rows": bad_pre_decision})
        chk("same_candle_tp_sl_remains_ambiguous", bad_dual_hit == 0, {"bad_rows": bad_dual_hit})
        chk("failed_provider_calls_do_not_become_expired", provider_expired == 0, {"bad_rows": provider_expired})
        chk("expired_outcomes_are_not_counted_complete", conn.execute(f"SELECT COUNT(*) FROM burnin_reject_outcomes WHERE burnin_run_id IN ({ph}) AND forward_label='EXPIRED' AND evidence_complete=1", run_ids).fetchone()[0] == 0)
        qrows = conn.execute("SELECT source_run_ids_json, aggregate_evidence_hash FROM burnin_qualification_snapshots WHERE campaign_id=? AND source_run_ids_json IS NOT NULL", (campaign_id,)).fetchall()
        chk("qualification_source_run_ids_json_exact", all(json.loads(r["source_run_ids_json"] or "[]") == run_ids for r in qrows), run_ids)
        recomputed = aggregate_campaign(conn, campaign_id).get("evidence_hash")
        missing_hashes = [idx for idx, r in enumerate(qrows) if r["aggregate_evidence_hash"] is None]
        mismatched_hashes = [idx for idx, r in enumerate(qrows) if r["aggregate_evidence_hash"] is not None and r["aggregate_evidence_hash"] != recomputed]
        chk("AGGREGATE_EVIDENCE_HASH_MISSING", not missing_hashes, {"missing_snapshot_indexes": missing_hashes})
        chk("AGGREGATE_EVIDENCE_HASH_MISMATCH", not mismatched_hashes, {"mismatched_snapshot_indexes": mismatched_hashes, "recomputed": recomputed})
        chk("stored_aggregate_evidence_hash_matches_recomputed", not missing_hashes and not mismatched_hashes, recomputed)
    agg = aggregate_campaign(conn, campaign_id)
    chk("aggregate_evidence_hash_reproducible", agg.get("evidence_hash") == aggregate_campaign(conn, campaign_id).get("evidence_hash"), agg.get("evidence_hash"))
    immutable_ok = True
    immutable_details = {}
    for rid in run_ids:
        ok, details = _check_and_update_source_baseline(conn, campaign_id, rid)
        if not ok:
            immutable_ok = False
            immutable_details[rid] = details
    chk("source_run_append_only_or_terminal_immutable", immutable_ok, immutable_details)
    try:
        conn.commit()
        db = conn.execute("PRAGMA database_list").fetchone()[2]
        dash = _dashboard_campaign_snapshot(db, campaign_id)
        chk("dashboard_counters_match_sql_counters", dash.get("decisions") == agg.get("metrics", {}).get("sample_count") and dash.get("accepted") == agg.get("metrics", {}).get("accepted_count") and dash.get("rejected") == agg.get("metrics", {}).get("rejected_count"), {"dashboard": dash, "aggregate": agg.get("metrics", {})})
    except Exception as exc:
        chk("dashboard_counters_match_sql_counters", False, f"{exc.__class__.__name__}:{exc}")
    status = "PASS" if not violations else "FAIL"
    payload = {"audit_id": "audit_" + canonical_hash({"cid": campaign_id, "at": utc_now(), "checks": checks})[:20], "campaign_id": campaign_id, "generated_at": utc_now(), "status": status, "violations": violations, "checks": checks, "aggregate_evidence_hash": agg.get("evidence_hash")}
    conn.execute("INSERT OR REPLACE INTO burnin_integrity_audits(audit_id,campaign_id,generated_at,status,violations_json,checks_json,aggregate_evidence_hash,schema_version) VALUES (?,?,?,?,?,?,?,?)", (payload["audit_id"], campaign_id, payload["generated_at"], status, json.dumps(violations), json.dumps(checks, default=str), payload.get("aggregate_evidence_hash"), PHASE9_SCHEMA_VERSION))
    conn.commit()
    return payload


def _group(conn: sqlite3.Connection, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def daily_report(conn: sqlite3.Connection, campaign_id: str, outdir: str | Path) -> dict[str, Any]:
    health = health_payload(conn, campaign_id)
    agg = aggregate_campaign(conn, campaign_id)
    campaign = get_campaign(conn, campaign_id) or {}
    run_ids = _run_source_ids(conn, campaign_id)
    ph = ",".join("?" for _ in run_ids) or "''"
    params = run_ids
    prev = conn.execute("SELECT payload_json FROM burnin_health_history WHERE campaign_id=? ORDER BY id DESC LIMIT 2", (campaign_id,)).fetchall()
    payload = {
        "campaign_id": campaign_id, "generated_at": utc_now(),
        "operational_uptime": {"observed_duration_seconds": campaign.get("observed_duration_seconds"), "heartbeat_age": health.get("heartbeat_age")},
        "runtime_incidents": conn.execute("SELECT COUNT(*) FROM burnin_ops_incidents WHERE campaign_id=?", (campaign_id,)).fetchone()[0],
        "decisions": {"total": health.get("total_decisions"), "accepted": health.get("accepted_decisions"), "rejected": health.get("rejected_decisions")},
        "decisions_by_regime": _group(conn, f"SELECT regime, decision, COUNT(*) AS count FROM burnin_observations WHERE burnin_run_id IN ({ph}) GROUP BY regime, decision ORDER BY regime, decision", params) if run_ids else [],
        "rejects_by_reason": _group(conn, f"SELECT reject_reason, COUNT(*) AS count FROM burnin_reject_outcomes WHERE burnin_run_id IN ({ph}) GROUP BY reject_reason ORDER BY count DESC", params) if run_ids else [],
        "trade_outcomes": _group(conn, f"SELECT symbol, regime, COUNT(*) AS closed_trades, AVG(net_r) AS avg_net_r, SUM(net_r) AS realized_net_r FROM burnin_trade_outcomes WHERE burnin_run_id IN ({ph}) AND closed_at IS NOT NULL GROUP BY symbol, regime", params) if run_ids else [],
        "realized_net_r": conn.execute(f"SELECT SUM(net_r) FROM burnin_trade_outcomes WHERE burnin_run_id IN ({ph}) AND closed_at IS NOT NULL", params).fetchone()[0] if run_ids else None,
        "effective_rr_distribution": _group(conn, f"SELECT MIN(effective_rr_at_entry) AS min_effective_rr, AVG(effective_rr_at_entry) AS avg_effective_rr, MAX(effective_rr_at_entry) AS max_effective_rr FROM burnin_trade_outcomes WHERE burnin_run_id IN ({ph})", params) if run_ids else [],
        "execution_cost_drag": conn.execute(f"SELECT SUM(total_execution_cost) FROM burnin_trade_outcomes WHERE burnin_run_id IN ({ph})", params).fetchone()[0] if run_ids else None,
        "spread_slippage_latency": _group(conn, f"SELECT AVG(spread_cost) AS avg_spread, AVG(entry_slippage_cost + exit_slippage_cost) AS avg_slippage, AVG(latency_cost) AS avg_latency FROM burnin_trade_outcomes WHERE burnin_run_id IN ({ph})", params) if run_ids else [],
        "rejected_candidate_outcomes": {"pending": health.get("pending_reject_labels"), "resolved": health.get("resolved_reject_labels"), "expired": health.get("expired_reject_labels"), "failed": health.get("failed_reject_labels")},
        "avoided_losses": conn.execute(f"SELECT SUM(avoided_loss) FROM burnin_reject_outcomes WHERE burnin_run_id IN ({ph})", params).fetchone()[0] if run_ids else None,
        "missed_profits": conn.execute(f"SELECT SUM(missed_profit) FROM burnin_reject_outcomes WHERE burnin_run_id IN ({ph})", params).fetchone()[0] if run_ids else None,
        "calibration": _group(conn, f"SELECT scope, sample_count, brier_score, log_loss, calibration_error, expected_calibration_error, status FROM burnin_calibration_metrics WHERE burnin_run_id IN ({ph})", params) if run_ids else [],
        "drawdown": _group(conn, f"SELECT drawdown_event_id, drawdown_pct, consecutive_losses, resolved FROM burnin_drawdown_events WHERE burnin_run_id IN ({ph})", params) if run_ids else [],
        "concentration": _group(conn, f"SELECT symbol, COUNT(*) AS observations FROM burnin_observations WHERE burnin_run_id IN ({ph}) GROUP BY symbol ORDER BY observations DESC", params) if run_ids else [],
        "backlog_trend": {"current_pending_rejects": health.get("pending_reject_labels"), "previous_pending_rejects": (json.loads(prev[1][0]).get("pending_reject_labels") if len(prev) > 1 else None)},
        "changes_since_prior_report": {"health_status": health.get("status"), "new_unhealthy_reasons": health.get("unhealthy_reasons", [])},
        "failure_classification": "OPERATIONAL_FAILURE" if health.get("unhealthy_reasons") else ("STRATEGY_SAMPLE_INSUFFICIENT" if not health.get("total_decisions") else "NONE"),
        "qualification_blockers": health.get("latest_blockers"), "aggregate": agg,
    }
    out = Path(outdir)
    _write_json_csv(out, "daily_summary", payload)
    (out / "daily_summary.md").write_text("# AlphaForge PAPER Burn-in Daily Summary\n\n```json\n" + json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n```\n")
    return payload


def _latest_integrity_audit(conn: sqlite3.Connection, campaign_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM burnin_integrity_audits WHERE campaign_id=? ORDER BY id DESC LIMIT 1", (campaign_id,)).fetchone()
    return dict(row) if row else None


def finalize(conn: sqlite3.Connection, db: str, campaign_id: str, outdir: str | Path) -> dict[str, Any]:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    audit = audit_payload(conn, campaign_id)
    health = health_payload(conn, campaign_id)
    completion = check_campaign_completion(conn, campaign_id)
    conn.commit()
    bundle = export_campaign_bundle(db, out, campaign_id)
    blockers = list(completion.get("blockers", [])) + list(audit.get("violations", [])) + list(health.get("unhealthy_reasons", []))
    campaign = get_campaign(conn, campaign_id) or {}
    qrow = conn.execute("SELECT status, aggregate_evidence_hash, source_run_ids_json FROM burnin_qualification_snapshots WHERE qualification_id=?", (campaign.get("latest_qualification_id"),)).fetchone() if campaign.get("latest_qualification_id") else None
    final_qualification = qrow["status"] if qrow else None
    exact_hash_link = bool(qrow and qrow["aggregate_evidence_hash"] is not None and qrow["aggregate_evidence_hash"] == aggregate_campaign(conn, campaign_id).get("evidence_hash"))
    bounded_backlog = int(health.get("pending_reject_labels") or 0) == 0 and int(health.get("open_paper_positions") or 0) == 0
    if campaign.get("campaign_status") == "SUSPENDED":
        decision = "PAPER_BURNIN_SUSPENDED"
    elif campaign.get("campaign_status") == "FAILED" or audit.get("status") != "PASS":
        decision = "PAPER_BURNIN_FAILED"
    elif completion.get("complete") and audit.get("status") == "PASS" and final_qualification == "CANARY_QUALIFIED" and exact_hash_link and health.get("status") == "HEALTHY" and bounded_backlog:
        decision = "PAPER_BURNIN_QUALIFIED_FOR_CANARY_REVIEW"
    else:
        decision = "PAPER_BURNIN_INCOMPLETE"
    if decision != "PAPER_BURNIN_QUALIFIED_FOR_CANARY_REVIEW":
        if final_qualification != "CANARY_QUALIFIED":
            blockers.append("FINAL_QUALIFICATION_NOT_CANARY_QUALIFIED")
        if not exact_hash_link:
            blockers.append("AGGREGATE_EVIDENCE_HASH_LINK_MISSING")
        if not bounded_backlog:
            blockers.append("PENDING_BACKLOG_NOT_BOUNDED")
    release_decision = {"decision": decision, "allowed_decisions": sorted(ALLOWED_FINAL_DECISIONS), "campaign_id": campaign_id, "blockers": sorted(set(blockers)), "generated_at": utc_now(), "final_qualification": final_qualification, "exact_aggregate_evidence_hash_linkage": exact_hash_link, "bounded_pending_backlog": bounded_backlog}
    (out / "release_decision.json").write_text(json.dumps(release_decision, indent=2, sort_keys=True, default=str))
    manifest = {"campaign_manifest": campaign, "source_run_list": _run_source_ids(conn, campaign_id), "preflight_report": conn.execute("SELECT * FROM burnin_preflight_reports WHERE campaign_id=? ORDER BY id DESC LIMIT 1", (campaign_id,)).fetchone(), "health": health, "incident_history": [dict(r) for r in conn.execute("SELECT * FROM burnin_ops_incidents WHERE campaign_id=? ORDER BY id", (campaign_id,)).fetchall()], "recovery_drill_result": conn.execute("SELECT * FROM burnin_recovery_drills WHERE campaign_id=? ORDER BY id DESC LIMIT 1", (campaign_id,)).fetchone(), "integrity_audit": audit, "completion": completion, "evidence_bundle": bundle, "decision": release_decision, "git_commit": campaign.get("git_commit")}
    (out / "final_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str))
    checksums: dict[str, str] = {}
    for path in sorted(out.rglob("*")):
        if path.is_file() and path.name != "checksums.json":
            checksums[str(path.relative_to(out))] = hashlib.sha256(path.read_bytes()).hexdigest()
    (out / "checksums.json").write_text(json.dumps(checksums, indent=2, sort_keys=True))
    did = "dec_" + canonical_hash({"cid": campaign_id, "decision": decision, "checks": checksums})[:20]
    conn.execute("INSERT OR REPLACE INTO burnin_release_decisions(decision_id,campaign_id,generated_at,decision,blockers_json,package_dir,checksums_json,schema_version) VALUES (?,?,?,?,?,?,?,?)", (did, campaign_id, utc_now(), decision, json.dumps(release_decision["blockers"]), str(out), json.dumps(checksums, sort_keys=True), PHASE9_SCHEMA_VERSION))
    conn.commit()
    return {"decision": decision, "campaign_id": campaign_id, "output_dir": str(out), "blockers": release_decision["blockers"], "checksums": checksums}


def _worker_log_paths(campaign_id: str) -> tuple[Path, Path]:
    root = Path("artifacts") / "burnin" / campaign_id
    root.mkdir(parents=True, exist_ok=True)
    return root / "worker.stdout.log", root / "worker.stderr.log"


def _launch_worker(db: str, campaign_id: str) -> subprocess.Popen[Any]:
    cmd = [sys.executable, "-m", "alphaforge.burnin_cli", "--db", db, "worker", "--campaign-id", campaign_id]
    conn = _connect(db)
    try:
        campaign, run, mapping, error = load_active_campaign_attachment(conn, campaign_id)
        campaign_identity = campaign_attachment_identity(campaign) if campaign else {}
        run_identity = run_attachment_identity(run) if run else {}
        mismatches = identity_mismatches(campaign_identity, run_identity, ATTACHMENT_IDENTITY_FIELDS) if run else {"active_run": {"expected": campaign.get("active_run_id") if campaign else None, "observed": None}}
        if error or mismatches:
            reason = error or "PHASE8_CAMPAIGN_RUN_IDENTITY_MISMATCH"
            if campaign:
                fail_active_campaign_run(conn, campaign_id, reason, details={"reason": reason, "campaign_identity": campaign_identity, "run_identity": run_identity, "runtime_identity": {}, "campaign_run_mismatches": mismatches, "run_runtime_mismatches": {}, "identity_sources": {"campaign": "burnin_campaigns", "run": "burnin_runs", "runtime_release": None}, "active_run_mapping": mapping or {}})
                conn.commit()
            raise RuntimeError(reason)
        release_id = str(campaign_identity["release_id"])
    finally:
        conn.close()
    # The persisted campaign is the worker attachment contract; do not inherit an
    # unrelated shell release identity into a new continuation process.
    stdout_path, stderr_path = _worker_log_paths(campaign_id)
    stdout = stdout_path.open("ab", buffering=0)
    stderr = stderr_path.open("ab", buffering=0)
    try:
        creationflags = 0
        if os.name == "nt":
            creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.Popen(cmd, stdout=stdout, stderr=stderr, env={**os.environ, "ALPHAFORGE_RELEASE_ID": release_id, "ALPHAFORGE_EXECUTION_MODE": "PAPER", "EXECUTION_MODE": "PAPER", "ALPHAFORGE_BURNIN_DATABASE_PATH": str(Path(db).expanduser().resolve())}, creationflags=creationflags)
    finally:
        stdout.close()
        stderr.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m alphaforge.burnin_ops")
    parser.add_argument("--db")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("preflight", epilog="PowerShell example: --symbols BTCUSDT,ETHUSDT --intervals 1h,4h"); p.add_argument("--release-id", required=True); p.add_argument("--symbols", required=True, nargs="+", help="Symbols as comma-separated values (BTCUSDT,ETHUSDT) or as separate values (BTCUSDT ETHUSDT)."); p.add_argument("--intervals", required=True, nargs="+", help="Intervals as comma-separated values (1h,4h) or as separate values (1h 4h)."); p.add_argument("--output-dir")
    l = sub.add_parser("launch", epilog="PowerShell example: python -m alphaforge.burnin_ops --db $DB launch --release-id $RELEASE_ID --duration-days 3 --symbols BTCUSDT,ETHUSDT --intervals 1h --detach"); l.add_argument("--release-id", required=True); l.add_argument("--duration-days", type=float, required=True); l.add_argument("--symbols", required=True, nargs="+", help="Symbols as comma-separated values (BTCUSDT,ETHUSDT) or as separate values (BTCUSDT ETHUSDT)."); l.add_argument("--intervals", required=True, nargs="+", help="Intervals as comma-separated values (1h,4h) or as separate values (1h 4h)."); l.add_argument("--detach", action="store_true"); l.add_argument("--attach-timeout-seconds", type=float, default=60.0)
    for name in ("health", "watch", "recovery-drill", "audit", "pause", "resume", "status"):
        s = sub.add_parser(name); s.add_argument("--campaign-id", required=True)
    rr = sub.add_parser("recover-runtime"); rr.add_argument("--campaign-id"); rr.add_argument("--terminalize-zero-exposure", action="store_true", help="explicitly terminalize a dead RECOVERY_REQUIRED PAPER continuation only after complete zero-exposure verification")
    r = sub.add_parser("report"); r.add_argument("--campaign-id", required=True); r.add_argument("--output-dir", required=True)
    f = sub.add_parser("finalize"); f.add_argument("--campaign-id", required=True); f.add_argument("--output-dir", required=True)
    d = sub.add_parser("diagnose-db", help="read-only campaign state diagnosis and safe cleanup plan"); d.add_argument("--max-heartbeat-age", type=float, default=120.0)
    doctor = sub.add_parser("db-doctor", help="inspect or safely migrate the canonical SQLite schema")
    doctor_mode = doctor.add_mutually_exclusive_group(); doctor_mode.add_argument("--check-only", action="store_true"); doctor_mode.add_argument("--apply", action="store_true")
    labels = sub.add_parser("reject-label-status", help="read-only PAPER reject forward-outcome integrity gate")
    label_identity = labels.add_mutually_exclusive_group(required=True)
    label_identity.add_argument("--campaign-id")
    label_identity.add_argument("--runtime-identity", help="canonical standalone:<burnin_run_id> identity")
    labels.add_argument("--stale-claim-seconds", type=float, default=300.0)
    args = parser.parse_args(argv)
    db = _db_path(args)
    try:
        if args.cmd == "db-doctor":
            report = ensure_database_schema(db) if args.apply else validate_required_schema(db)
            print(json.dumps(report.as_dict(), indent=2, sort_keys=True, default=str))
            return 0 if report.schema_status in {"VALID", "MIGRATED"} else 2
        if args.cmd == "diagnose-db":
            out = database_diagnosis(db, max_heartbeat_age=args.max_heartbeat_age)
            print(json.dumps(out, indent=2, sort_keys=True, default=str)); return 0 if out.get("status") == "COMPLETE" else 2
        if args.cmd == "reject-label-status":
            identity = args.campaign_id or args.runtime_identity
            if args.runtime_identity and not args.runtime_identity.startswith("standalone:"):
                print(json.dumps({"status": "ERROR", "error": "INVALID_STANDALONE_RUNTIME_IDENTITY"}, indent=2)); return 2
            readonly = _connect_readonly(db)
            try:
                out = reject_label_status(readonly, identity, stale_claim_seconds=args.stale_claim_seconds)
            finally:
                readonly.close()
            print(json.dumps(out, indent=2, sort_keys=True, default=str))
            return {"PASS": 0, "INCOMPLETE": 3, "FAIL": 4}.get(out.get("status"), 2)
        if args.cmd == "preflight":
            out = preflight(db, args.release_id, _symbols(args.symbols), _intervals(args.intervals), output_dir=args.output_dir)
            print(json.dumps(out, indent=2, sort_keys=True, default=str)); return 0 if out["status"] == "PASS" else 3
        if args.cmd == "launch":
            out = launch_campaign(db, args.release_id, args.duration_days, _symbols(args.symbols), _intervals(args.intervals), detach=args.detach, attach_timeout_seconds=args.attach_timeout_seconds)
            print(json.dumps(out, indent=2, sort_keys=True, default=str)); return 0 if out.get("status") in {"LAUNCHED", "FOREGROUND_STOPPED"} else 1
        conn = _connect(db)
        if args.cmd in {"health", "status"}:
            out = health_payload(conn, args.campaign_id); code = 0 if out.get("status") == "HEALTHY" else 1
        elif args.cmd == "watch":
            out = watch_once(conn, args.campaign_id); code = 0 if out.get("status") == "OK" else 2
        elif args.cmd == "pause":
            pause_campaign(conn, args.campaign_id); conn.commit(); out = {"status": "PAUSED", "campaign_id": args.campaign_id}; code = 0
        elif args.cmd == "resume":
            out = start_or_resume_campaign(conn, args.campaign_id, resume=True); conn.commit(); code = 0
        elif args.cmd == "recovery-drill":
            out = recovery_drill(conn, args.campaign_id); code = 0 if out["status"] == "PASS" else 1
        elif args.cmd == "recover-runtime":
            cid = args.campaign_id
            if not cid:
                row = conn.execute("SELECT campaign_id FROM burnin_campaigns WHERE campaign_status='RECOVERY_REQUIRED' ORDER BY id DESC LIMIT 1").fetchone()
                if not row:
                    out = {"status": "NO_RECOVERY_REQUIRED", "campaign_id": None}; code = 0
                else:
                    cid = row[0]
            if cid:
                out = terminalize_zero_exposure_recovery(conn, cid) if args.terminalize_zero_exposure else recovery_drill(conn, cid)
                code = 0 if out["status"] == "PASS" else 1
        elif args.cmd == "audit":
            out = audit_payload(conn, args.campaign_id); _write_json_csv(Path(f"artifacts/burnin/{args.campaign_id}"), "burnin_integrity_audit", out); code = 0 if out["status"] == "PASS" else 1
        elif args.cmd == "report":
            out = daily_report(conn, args.campaign_id, args.output_dir); code = 0
        elif args.cmd == "finalize":
            out = finalize(conn, db, args.campaign_id, args.output_dir); code = 0
        else:
            out = {"status": "ERROR", "error": "UNKNOWN_COMMAND"}; code = 2
        print(json.dumps(out, indent=2, sort_keys=True, default=str)); return code
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "error": f"{exc.__class__.__name__}:{exc}"}, indent=2)); return 1


if __name__ == "__main__":
    sys.exit(main())
