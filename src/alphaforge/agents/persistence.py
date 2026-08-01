"""SQL-first, additive persistence for shadow graph traces only."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import time
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from .contracts import canonical_json, utc_now_iso

DEFAULT_SHADOW_DATABASE_URL = "sqlite+pysqlite:///data/runtime/alphaforge_agent_shadow.db"

DDL = (
    """CREATE TABLE IF NOT EXISTS agent_runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT, correlation_id TEXT NOT NULL UNIQUE,
      decision_id TEXT NOT NULL, execution_mode TEXT NOT NULL, symbol TEXT,
      shadow_only INTEGER NOT NULL, graph_status TEXT NOT NULL, started_at TEXT NOT NULL,
      completed_at TEXT NOT NULL, duration_ms REAL NOT NULL,
      legacy_decision_reference TEXT, config_hash TEXT NOT NULL,
      orchestrator_version TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS agent_stage_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT, correlation_id TEXT NOT NULL,
      decision_id TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
      primary_reason TEXT NOT NULL, reason_codes_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
      input_hash TEXT NOT NULL, config_hash TEXT NOT NULL, agent_version TEXT NOT NULL,
      started_at TEXT NOT NULL, completed_at TEXT NOT NULL, duration_ms REAL NOT NULL,
      retry_count INTEGER NOT NULL, skipped_reason TEXT, created_at TEXT NOT NULL,
      UNIQUE(decision_id, stage, retry_count))""",
    "CREATE INDEX IF NOT EXISTS ix_agent_runs_decision ON agent_runs(decision_id, graph_status)",
    "CREATE INDEX IF NOT EXISTS ix_agent_events_correlation ON agent_stage_events(correlation_id, stage, status)",
)


def bootstrap_agent_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        for statement in DDL:
            conn.execute(text(statement))


def create_agent_shadow_engine(database_url: str = DEFAULT_SHADOW_DATABASE_URL) -> Engine:
    """Create the isolated Phase A store; never reuse the canonical runtime DB."""
    if not database_url.startswith("sqlite"):
        raise ValueError("PHASE_A_SHADOW_DATABASE_MUST_BE_SQLITE")
    from pathlib import Path
    from sqlalchemy.engine.url import make_url
    database = make_url(database_url).database
    if database and database != ":memory:":
        Path(database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(database_url, future=True, connect_args={"timeout": 1.0})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=1000")
        cursor.close()
    return engine


@dataclass(slots=True)
class AgentPersistenceStats:
    retry_count: int = 0
    lock_wait_ms: float = 0.0


class AgentTraceRepository:
    def __init__(self, engine: Engine, *, max_busy_retries: int = 3,
                 stats: AgentPersistenceStats | None = None) -> None:
        self.engine = engine
        self.max_busy_retries = max(0, max_busy_retries)
        self.stats = stats or AgentPersistenceStats()

    def persist_result(self, result: Any) -> None:
        for attempt in range(self.max_busy_retries + 1):
            try:
                self._persist_result_once(result)
                return
            except OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                if attempt >= self.max_busy_retries:
                    raise
                delay = 0.01 * (2 ** attempt)
                self.stats.retry_count += 1
                started = time.monotonic()
                time.sleep(delay)
                self.stats.lock_wait_ms += (time.monotonic() - started) * 1000

    def _persist_result_once(self, result: Any) -> None:
        first = result.stage_results[0] if result.stage_results else None
        start = datetime.fromisoformat(result.started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(result.completed_at.replace("Z", "+00:00"))
        created = utc_now_iso()
        with self.engine.begin() as conn:
            conn.execute(text("""INSERT INTO agent_runs
              (correlation_id,decision_id,execution_mode,symbol,shadow_only,graph_status,started_at,completed_at,duration_ms,legacy_decision_reference,config_hash,orchestrator_version,created_at)
              VALUES (:cid,:did,:mode,:symbol,1,:status,:started,:completed,:duration,:legacy,:config_hash,:version,:created)
              ON CONFLICT(correlation_id) DO NOTHING"""), {
                "cid": result.correlation_id, "did": first.decision_id if first else result.correlation_id,
                "mode": first.execution_mode if first else "UNAVAILABLE", "symbol": first.symbol if first else None,
                "status": result.status.value, "started": result.started_at, "completed": result.completed_at,
                "duration": max(0.0, (end-start).total_seconds()*1000), "legacy": result.legacy_decision_reference,
                "config_hash": first.config_hash if first else "0" * 64, "version": "phase-a-1", "created": created,
            })
            for event in result.stage_results:
                conn.execute(text("""INSERT INTO agent_stage_events
                  (correlation_id,decision_id,stage,status,primary_reason,reason_codes_json,evidence_json,input_hash,config_hash,agent_version,started_at,completed_at,duration_ms,retry_count,skipped_reason,created_at)
                  VALUES (:cid,:did,:stage,:status,:reason,:codes,:evidence,:input_hash,:config_hash,:version,:started,:completed,:duration,:retry,:skipped,:created)
                  ON CONFLICT(decision_id,stage,retry_count) DO NOTHING"""), {
                    "cid": event.correlation_id, "did": event.decision_id, "stage": event.stage.value,
                    "status": event.status.value, "reason": event.primary_reason,
                    "codes": canonical_json(event.reason_codes), "evidence": canonical_json(event.evidence),
                    "input_hash": event.input_hash, "config_hash": event.config_hash,
                    "version": event.agent_version, "started": event.started_at, "completed": event.completed_at,
                    "duration": event.duration_ms, "retry": event.retry_count,
                    "skipped": event.skipped_reason, "created": created,
                })
