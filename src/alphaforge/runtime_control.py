from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from alphaforge.contracts import canonical_utc_timestamp

VALID_MODES = {"PAPER", "LIVE"}
VALID_STATUSES = {"STOPPED", "STARTING", "RUNNING_PAPER", "RUNNING_LIVE", "STOPPING", "ERROR", "KILL_SWITCH_ACTIVE"}


@dataclass(frozen=True, slots=True)
class RuntimeControlState:
    mode_requested: str = "PAPER"
    mode_running: str | None = None
    kill_switch_active: bool = False
    kill_switch_source: str | None = None
    kill_switch_updated_at: str | None = None
    runtime_status: str = "STOPPED"
    last_error: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode_requested": self.mode_requested,
            "mode_running": self.mode_running,
            "kill_switch_active": self.kill_switch_active,
            "kill_switch_state": "ACTIVE" if self.kill_switch_active else "INACTIVE",
            "kill_switch_source": self.kill_switch_source,
            "kill_switch_updated_at": self.kill_switch_updated_at,
            "runtime_status": self.runtime_status,
            "last_error": self.last_error,
            "updated_at": self.updated_at,
        }


def ensure_runtime_control_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS runtime_control_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                mode_requested TEXT NOT NULL,
                mode_running TEXT,
                kill_switch_active INTEGER NOT NULL DEFAULT 0,
                kill_switch_source TEXT,
                kill_switch_updated_at TEXT,
                runtime_status TEXT NOT NULL,
                last_error TEXT,
                updated_at TEXT NOT NULL
            )
        """))
        now = canonical_utc_timestamp()
        conn.execute(text("""
            INSERT OR IGNORE INTO runtime_control_state(
                id, mode_requested, mode_running, kill_switch_active, runtime_status, updated_at
            ) VALUES (1, 'PAPER', NULL, 0, 'STOPPED', :now)
        """), {"now": now})


class RuntimeControlStore:
    def __init__(self, engine: Engine):
        self.engine = engine
        ensure_runtime_control_schema(engine)

    def read(self) -> RuntimeControlState:
        ensure_runtime_control_schema(self.engine)
        with self.engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM runtime_control_state WHERE id = 1")).mappings().one()
        return RuntimeControlState(
            mode_requested=str(row["mode_requested"] or "PAPER").upper(),
            mode_running=str(row["mode_running"]).upper() if row["mode_running"] else None,
            kill_switch_active=bool(row["kill_switch_active"]),
            kill_switch_source=row["kill_switch_source"],
            kill_switch_updated_at=row["kill_switch_updated_at"],
            runtime_status=str(row["runtime_status"] or "STOPPED").upper(),
            last_error=row["last_error"],
            updated_at=row["updated_at"],
        )

    def set_requested_mode(self, mode: str) -> RuntimeControlState:
        mode = str(mode or "").strip().upper()
        if mode not in VALID_MODES:
            raise ValueError("mode_requested must be PAPER or LIVE")
        state = self.read()
        if state.runtime_status not in {"STOPPED", "ERROR", "KILL_SWITCH_ACTIVE"} or state.mode_running:
            raise RuntimeError("mode_requested can only be changed while runtime is stopped")
        now = canonical_utc_timestamp()
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE runtime_control_state SET mode_requested=:mode, last_error=NULL, updated_at=:now WHERE id=1"), {"mode": mode, "now": now})
        return self.read()

    def set_kill_switch(self, active: bool, *, source: str = "dashboard") -> RuntimeControlState:
        now = canonical_utc_timestamp()
        status = "KILL_SWITCH_ACTIVE" if active else self.read().runtime_status
        if not active and status == "KILL_SWITCH_ACTIVE":
            status = "STOPPED"
        with self.engine.begin() as conn:
            conn.execute(text("""
                UPDATE runtime_control_state
                SET kill_switch_active=:active, kill_switch_source=:source,
                    kill_switch_updated_at=:now, runtime_status=:status,
                    last_error=CASE WHEN :active = 1 THEN 'KILL_SWITCH_ACTIVE' ELSE last_error END,
                    mode_running=CASE WHEN :active = 1 THEN NULL ELSE mode_running END,
                    updated_at=:now
                WHERE id=1
            """), {"active": 1 if active else 0, "source": source, "now": now, "status": status})
        return self.read()

    def set_status(self, status: str, *, mode_running: str | None = None, last_error: str | None = None) -> RuntimeControlState:
        status = str(status or "").strip().upper()
        if status not in VALID_STATUSES:
            raise ValueError("unsupported runtime_status")
        now = canonical_utc_timestamp()
        with self.engine.begin() as conn:
            conn.execute(text("""
                UPDATE runtime_control_state
                SET runtime_status=:status, mode_running=:mode_running, last_error=:last_error, updated_at=:now
                WHERE id=1
            """), {"status": status, "mode_running": mode_running, "last_error": last_error, "now": now})
        return self.read()

    def is_kill_switch_active(self) -> bool:
        try:
            return self.read().kill_switch_active
        except SQLAlchemyError:
            return True


class RuntimeSupervisor:
    def __init__(self, store: RuntimeControlStore, runtime_factory: Callable[[str], Any]):
        self.store = store
        self.runtime_factory = runtime_factory
        self._task: asyncio.Task[Any] | None = None
        self._orchestrator: Any | None = None

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> RuntimeControlState:
        state = self.store.read()
        if state.kill_switch_active:
            return self.store.set_status("KILL_SWITCH_ACTIVE", last_error="KILL_SWITCH_ACTIVE")
        if self.is_running():
            return self.store.read()
        mode = state.mode_requested
        self.store.set_status("STARTING", last_error=None)
        try:
            orchestrator = self.runtime_factory(mode)
            actual = orchestrator.config.execution_mode.value
            if actual != mode:
                raise RuntimeError(f"Runtime mode mismatch: requested {mode}, built {actual}")
            self._orchestrator = orchestrator
            self._task = asyncio.create_task(self._run(orchestrator, actual), name=f"dashboard_runtime_{actual.lower()}")
            return self.store.set_status(f"RUNNING_{actual}", mode_running=actual, last_error=None)
        except Exception as exc:
            return self.store.set_status("ERROR", mode_running=None, last_error=str(exc))

    async def _run(self, orchestrator: Any, mode: str) -> None:
        try:
            await orchestrator.start()
            self.store.set_status("STOPPED", mode_running=None, last_error=None)
        except Exception as exc:
            self.store.set_status("ERROR", mode_running=None, last_error=str(exc))

    async def stop(self) -> RuntimeControlState:
        if not self.is_running():
            return self.store.set_status("STOPPED", mode_running=None, last_error=None)
        self.store.set_status("STOPPING", mode_running=self.store.read().mode_running)
        if self._orchestrator is not None:
            self._orchestrator.shutdown()
        assert self._task is not None
        await asyncio.wait_for(self._task, timeout=5)
        return self.store.set_status("STOPPED", mode_running=None, last_error=None)
