from __future__ import annotations

import asyncio

from sqlalchemy import create_engine

from alphaforge.runtime_control import RuntimeControlStore, RuntimeSupervisor


class _Cfg:
    def __init__(self, mode: str):
        self.execution_mode = type("Mode", (), {"value": mode})()


class _Runtime:
    def __init__(self, mode: str):
        self.config = _Cfg(mode)
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def start(self):
        self.started.set()
        await self.stopped.wait()

    def shutdown(self):
        self.stopped.set()


def test_runtime_control_kill_switch_persists_and_reads():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    store = RuntimeControlStore(engine)
    state = store.set_kill_switch(True, source="dashboard")
    assert state.kill_switch_active is True
    assert state.kill_switch_source == "dashboard"
    assert store.is_kill_switch_active() is True
    assert RuntimeControlStore(engine).read().kill_switch_active is True
    state = store.set_kill_switch(False, source="dashboard")
    assert state.kill_switch_active is False


def test_runtime_supervisor_starts_requested_paper_and_prevents_duplicate_loops():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    store = RuntimeControlStore(engine)
    created: list[_Runtime] = []

    def factory(mode: str):
        rt = _Runtime(mode)
        created.append(rt)
        return rt

    supervisor = RuntimeSupervisor(store, factory)

    async def run():
        await supervisor.start()
        await supervisor.start()
        assert len(created) == 1
        assert store.read().runtime_status == "RUNNING_PAPER"
        assert store.read().mode_running == "PAPER"
        await supervisor.stop()

    asyncio.run(run())
    assert store.read().runtime_status == "STOPPED"


def test_runtime_supervisor_live_mode_fails_closed_on_guard_error():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    store = RuntimeControlStore(engine)
    store.set_requested_mode("LIVE")

    def factory(mode: str):
        raise RuntimeError("LIVE mode blocked: exchange connectivity unavailable (binance:UNAVAILABLE)")

    supervisor = RuntimeSupervisor(store, factory)
    state = asyncio.run(supervisor.start())
    assert state.runtime_status == "ERROR"
    assert "exchange connectivity unavailable" in (state.last_error or "")
    assert state.mode_running is None
