from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from alphaforge.persistence import init_db
from alphaforge.reconciliation import ReconciliationEngine, ensure_reconciliation_tables, persist_findings
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


class _AcceptBrain:
    def __init__(self):
        self.session = Session(init_db("sqlite+pysqlite:///:memory:"))

    def before_real_order(self, signal_payload, market_ctx, regime_ctx, stats_ctx):
        class _Plan:
            decision = "ACCEPTED"
            reason = ""
            confidence = 0.9
            order_type = "MARKET"
            limit_price = None
            stop_price = None

        return {}, _Plan(), "ok"


def test_orphan_order_and_repair_generation() -> None:
    engine = ReconciliationEngine(stale_order_seconds=1)
    stale = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    snapshot = engine.snapshot_from_source({"orders": [{"order_id": "x1", "symbol": "BTCUSDT", "status": "OPEN", "created_at": stale}], "positions": [], "fills": []})
    findings, repairs, _ = engine.reconcile(intended_orders=[], lifecycle_state_by_symbol={}, snapshot=snapshot, mode="PAPER")
    types = {f.finding_type for f in findings}
    assert "ORPHAN_ORDER" in types
    assert "STALE_ORDER" in types
    assert any(r.category == "cancel_stale_order" for r in repairs)


def test_lifecycle_divergence_detection() -> None:
    engine = ReconciliationEngine()
    snapshot = engine.snapshot_from_source({"orders": [], "positions": [], "fills": []})
    findings, _, _ = engine.reconcile(intended_orders=[], lifecycle_state_by_symbol={"ETHUSDT": "ENTRY_FILLED"}, snapshot=snapshot, mode="LIVE")
    assert any(f.finding_type == "LIFECYCLE_DIVERGENCE" and f.fail_closed for f in findings)


def test_reconciliation_persistence() -> None:
    db = init_db("sqlite+pysqlite:///:memory:")
    ensure_reconciliation_tables(db)
    engine = ReconciliationEngine()
    snapshot = engine.snapshot_from_source({"orders": [{"order_id": "abc", "symbol": "SOLUSDT", "status": "OPEN"}], "positions": [], "fills": []})
    findings, _, _ = engine.reconcile(intended_orders=[], lifecycle_state_by_symbol={}, snapshot=snapshot, mode="PAPER")
    persist_findings(db, findings)
    with db.begin() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM reconciliation_incidents")).scalar_one()
    assert count >= 1


def test_runtime_reconciliation_fail_closed_and_no_duplicate_repair() -> None:
    events: list[dict] = []

    async def scanner():
        return []

    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER, reconciliation_interval_sec=60.0),
        ai_brain=_AcceptBrain(),
        market_scanner=scanner,
        on_lifecycle_event=lambda e: events.append(e),
    )
    orchestrator._pending_orders["BTCUSDT"] = {"order_id": "o-1", "symbol": "BTCUSDT", "status": "OPEN", "created_at": "2020-01-01T00:00:00Z"}
    asyncio.run(orchestrator._emit_lifecycle_event("SIGNAL_CREATED", "BTCUSDT", {}))
    asyncio.run(orchestrator._run_reconciliation_once())
    asyncio.run(orchestrator._run_reconciliation_once())
    assert len(orchestrator._last_repair_signature) == 1


def test_snapshot_replay_consistency() -> None:
    engine = ReconciliationEngine()
    source = {"orders": [{"order_id": "z", "symbol": "XRPUSDT", "status": "OPEN"}], "positions": [{"symbol": "XRPUSDT", "qty": 1}], "fills": []}
    s1 = engine.snapshot_from_source(source)
    s2 = engine.snapshot_from_source(source)
    f1, r1, _ = engine.reconcile(intended_orders=[], lifecycle_state_by_symbol={}, snapshot=s1, mode="PAPER")
    f2, r2, _ = engine.reconcile(intended_orders=[], lifecycle_state_by_symbol={}, snapshot=s2, mode="PAPER")
    assert [f.finding_type for f in f1] == [f.finding_type for f in f2]
    assert [r.category for r in r1] == [r.category for r in r2]
