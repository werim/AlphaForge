from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from alphaforge.burnin_campaign import (
    BurnInCampaignRunner,
    create_campaign,
    qualify_campaign,
    start_or_resume_campaign,
)
from alphaforge.persistence import init_db, save_rejected_decision_artifact
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator
from alphaforge.runtime_state import RuntimeStateSnapshot, persist_reconciliation_cycle


class _Brain:
    session = None


async def _scanner() -> list[dict]:
    return []


def _payload() -> dict[str, object]:
    return {
        "signal_id": "issue550:reject", "symbol": "BTCUSDT", "side": "LONG",
        "timeframe": "1m", "entry": 100.0, "sl": 99.0, "tp": 102.0,
        "reason": "LOW_CONFIDENCE", "regime": "TRENDING",
        "decision_timestamp": "2026-09-29T00:00:00Z",
        "execution_ctx": {
            "spread_pct": 0.001, "expected_slippage_pct": 0.001,
            "fee_pct": 0.001, "funding_rate_pct": 0.0,
            "market_data_latency_ms": 1,
        },
    }


def _runtime(tmp_path, monkeypatch, name="issue550"):
    path = tmp_path / f"{name}.db"
    engine = init_db(f"sqlite+pysqlite:///{path}")
    with engine.begin() as conn:
        campaign = create_campaign(
            conn, release_id=f"{name}-release", duration_days=1,
            symbols=["BTCUSDT"], intervals=["1m"],
        )
        run_id = start_or_resume_campaign(conn, campaign.campaign_id)["burnin_run_id"]
    monkeypatch.setenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID", campaign.campaign_id)

    def artifact(conn, payload):
        return save_rejected_decision_artifact(
            conn, _commit=False, decision_id=payload["reject_decision_id"],
            signal_id=payload["signal_id"], symbol=payload["symbol"],
            side=payload.get("side"), timeframe=payload.get("timeframe"),
            mode="PAPER", phase="final", reject_reason=payload["reason"],
            event_ts=payload["decision_timestamp"],
            execution_ctx=payload.get("execution_ctx"),
        )

    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=_Brain(), market_scanner=_scanner, persistence_engine=engine,
        on_reject_persist_atomic=artifact,
    )
    runtime._campaign_id = campaign.campaign_id
    runtime._burnin_run_id = run_id
    runtime.metrics.persistence_enabled = True
    return path, engine, runtime, campaign.campaign_id


def _reject_counts(engine) -> tuple[int, ...]:
    with engine.connect() as conn:
        return (
            int(conn.execute(text("SELECT COUNT(*) FROM rejected_signal_reviews")).scalar_one()),
            int(conn.execute(text("SELECT COUNT(*) FROM burnin_observations WHERE burnin_run_id NOT LIKE '%__aggregate'")).scalar_one()),
            int(conn.execute(text("SELECT COUNT(*) FROM burnin_pending_reject_labels")).scalar_one()),
            int(conn.execute(text("SELECT COUNT(*) FROM signals")).scalar_one()),
            int(conn.execute(text("SELECT COUNT(*) FROM order_decisions")).scalar_one()),
            int(conn.execute(text("SELECT COUNT(*) FROM trade_lifecycle_events")).scalar_one()),
        )


def test_held_writer_exhaustion_is_atomic_fail_closed_and_recoverable(tmp_path, monkeypatch):
    path, engine, runtime, _ = _runtime(tmp_path, monkeypatch)
    blocker = sqlite3.connect(path, timeout=0.01)
    blocker.execute("BEGIN IMMEDIATE")
    async def scan_once(self):
        await self._persist_reject(_payload())
        self._stop_event.set()
    monkeypatch.setattr(RuntimeOrchestrator, "_scan_once", scan_once)
    try:
        asyncio.run(runtime._market_scan_loop())
    finally:
        blocker.rollback()
        blocker.close()

    assert _reject_counts(engine) == (0, 0, 0, 0, 0, 0)
    assert runtime.metrics.reject_persistence_degraded is True
    assert runtime._recovery_required is True
    assert runtime._fail_closed_reason == "REJECT_PERSISTENCE_FAILED"
    assert runtime._fatal_task_exception is None

    asyncio.run(runtime._persist_reject(_payload()))
    assert _reject_counts(engine) == (1, 1, 1, 1, 1, 1)
    assert runtime.metrics.reject_persistence_degraded is False
    assert runtime._recovery_required is False
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 30000
        assert str(conn.exec_driver_sql("PRAGMA journal_mode").scalar_one()).lower() == "wal"
        assert conn.exec_driver_sql("PRAGMA synchronous").scalar_one() == 1
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


def test_non_busy_reject_database_error_remains_fatal_and_atomic(tmp_path, monkeypatch):
    _, engine, runtime, _ = _runtime(tmp_path, monkeypatch, "fatal")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE order_decisions"))

    with pytest.raises(OperationalError, match="no such table: order_decisions"):
        asyncio.run(runtime._persist_reject(_payload()))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM rejected_signal_reviews")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM burnin_observations")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM signals")).scalar_one() == 0


def test_reject_restart_replay_keeps_all_stable_identities_exactly_once(tmp_path, monkeypatch):
    _, engine, runtime, campaign_id = _runtime(tmp_path, monkeypatch, "restart")
    asyncio.run(runtime._persist_reject(_payload()))
    restarted = RuntimeOrchestrator(
        config=runtime.config, ai_brain=_Brain(), market_scanner=_scanner,
        persistence_engine=engine,
        on_reject_persist_atomic=runtime.on_reject_persist_atomic,
    )
    restarted._campaign_id = campaign_id
    restarted._burnin_run_id = runtime._burnin_run_id
    replay = _payload()
    replay["decision_timestamp"] = "2026-09-29T00:00:01Z"
    asyncio.run(restarted._persist_reject(replay))
    assert _reject_counts(engine) == (1, 1, 1, 1, 1, 1)
    with engine.connect() as conn:
        lifecycle = conn.execute(text(
            "SELECT event_id, event_ts FROM trade_lifecycle_events "
            "WHERE signal_id='issue550:reject' AND lifecycle_state='SIGNAL_REJECTED'"
        )).one()
        assert lifecycle.event_id == "issue550:reject:SIGNAL_REJECTED"
        assert lifecycle.event_ts == "2026-09-29T00:00:01Z"


def test_concurrent_resolver_reconciliation_and_reject_remain_consistent(tmp_path, monkeypatch):
    _, engine, runtime, campaign_id = _runtime(tmp_path, monkeypatch, "concurrent")
    runner = BurnInCampaignRunner(engine, campaign_id, lambda *_args: [])
    barrier = threading.Barrier(3)

    def reject():
        barrier.wait()
        asyncio.run(runtime._persist_reject(_payload()))

    def resolve():
        barrier.wait()
        return runner.resolver_tick()

    def reconcile():
        barrier.wait()
        snapshot = RuntimeStateSnapshot(
            mode="PAPER", requested_mode="PAPER", actual_mode="PAPER",
            runtime_status="RECONCILED", reconciliation_status="CLEAN",
        )
        return persist_reconciliation_cycle(
            engine, cycle_id="recon:v1:issue550", findings=[], snapshot=snapshot,
            diagnostics={"test": "issue550"},
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(fn) for fn in (reject, resolve, reconcile)]
        results = [future.result(timeout=10) for future in futures]

    assert results[1]["status"] == "OK"
    assert results[2] is True
    assert _reject_counts(engine) == (1, 1, 1, 1, 1, 1)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM exchange_reconciliation_events WHERE cycle_id='recon:v1:issue550'")).scalar_one() == 1


def test_unchanged_source_hash_skips_synthetic_aggregate_rebuild(tmp_path, monkeypatch):
    _, engine, runtime, campaign_id = _runtime(tmp_path, monkeypatch, "unchanged")
    asyncio.run(runtime._persist_reject(_payload()))
    first = qualify_campaign(engine, campaign_id)
    with engine.connect() as conn:
        before = conn.execute(text(
            "SELECT COUNT(*) FROM burnin_observations WHERE burnin_run_id=:run"
        ), {"run": f"{campaign_id}__aggregate"}).scalar_one()
    second = qualify_campaign(engine, campaign_id)
    with engine.connect() as conn:
        after = conn.execute(text(
            "SELECT COUNT(*) FROM burnin_observations WHERE burnin_run_id=:run"
        ), {"run": f"{campaign_id}__aggregate"}).scalar_one()

    assert first["materialized"] is True
    assert second["materialized"] is False
    assert second["qualification_id"] == first["qualification_id"]
    assert after == before
