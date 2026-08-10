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
    """CREATE TABLE IF NOT EXISTS agent_phase_b_evidence (
      correlation_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, symbol TEXT,
      market_status TEXT, regime TEXT, volatility REAL, trend_strength REAL,
      spread REAL, expected_slippage REAL, liquidity REAL, funding REAL,
      availability_json TEXT NOT NULL, signal_status TEXT, signal_side TEXT,
      setup_type TEXT, score REAL, score_components_json TEXT NOT NULL, raw_rr REAL,
      entry REAL, sl REAL, tp REAL, no_signal_reason TEXT, quality_status TEXT,
      quality_score REAL, expectancy_bucket TEXT, primary_reject_reason TEXT,
      reject_reasons_json TEXT NOT NULL, legacy_decision TEXT,
      legacy_primary_reject_reason TEXT, score_difference REAL, rr_difference REAL,
      reason_code_overlap INTEGER, parity_status TEXT, created_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_phase_b_quality ON agent_phase_b_evidence(quality_status, primary_reject_reason, parity_status)",
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
            events = {event.stage.value: event for event in result.stage_results}
            if {"MARKET", "SIGNAL", "QUALITY"}.issubset(events):
                market, signal, quality = (events[name] for name in ("MARKET", "SIGNAL", "QUALITY"))
                m, s, q = market.evidence, signal.evidence, quality.evidence
                conn.execute(text("""INSERT INTO agent_phase_b_evidence
                  (correlation_id,decision_id,symbol,market_status,regime,volatility,trend_strength,spread,expected_slippage,liquidity,funding,availability_json,signal_status,signal_side,setup_type,score,score_components_json,raw_rr,entry,sl,tp,no_signal_reason,quality_status,quality_score,expectancy_bucket,primary_reject_reason,reject_reasons_json,legacy_decision,legacy_primary_reject_reason,score_difference,rr_difference,reason_code_overlap,parity_status,created_at)
                  VALUES (:cid,:did,:symbol,:ms,:regime,:volatility,:trend,:spread,:slippage,:liquidity,:funding,:availability,:ss,:side,:setup,:score,:components,:rr,:entry,:sl,:tp,:no_signal,:qs,:quality_score,:expectancy,:primary_reason,:reasons,:legacy,:legacy_reason,:score_diff,:rr_diff,:overlap,:parity,:created)
                  ON CONFLICT(correlation_id) DO NOTHING"""), {
                    "cid": result.correlation_id, "did": signal.decision_id, "symbol": signal.symbol,
                    "ms": market.status.value, "regime": m.get("regime"), "volatility": m.get("volatility"),
                    "trend": m.get("trend_strength"), "spread": m.get("spread"), "slippage": m.get("expected_slippage"),
                    "liquidity": m.get("liquidity"), "funding": m.get("funding"),
                    "availability": canonical_json(m.get("availability", {})), "ss": signal.status.value,
                    "side": s.get("signal_side"), "setup": s.get("setup_type"), "score": s.get("score"),
                    "components": canonical_json(s.get("score_components", {})), "rr": s.get("raw_rr"),
                    "entry": s.get("entry"), "sl": s.get("sl"), "tp": s.get("tp"),
                    "no_signal": s.get("no_signal_reason"), "qs": q.get("graph_quality_status", quality.status.value),
                    "quality_score": q.get("quality_score"), "expectancy": q.get("expectancy_bucket"),
                    "primary_reason": q.get("primary_reject_reason"),
                    "reasons": canonical_json(q.get("all_reject_reasons", [])), "legacy": q.get("legacy_decision"),
                    "legacy_reason": q.get("legacy_primary_reject_reason"), "score_diff": q.get("score_difference"),
                    "rr_diff": q.get("rr_difference"), "overlap": int(bool(q.get("reason_code_overlap"))),
                    "parity": q.get("parity_status"), "created": created,
                })
