from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from alphaforge.persistence import init_db, save_order_decision, save_trade_lifecycle_event
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

def test_duplicate_fill_detection_by_trade_id() -> None:
    engine = ReconciliationEngine()
    snapshot = engine.snapshot_from_source({"orders": [], "positions": [], "fills": [{"trade_id": "t1", "symbol": "BTCUSDT"}, {"trade_id": "t1", "symbol": "BTCUSDT"}]})
    findings, _, _ = engine.reconcile(intended_orders=[], lifecycle_state_by_symbol={}, snapshot=snapshot, mode="LIVE")
    assert any(f.finding_type == "DUPLICATE_FILL" and f.fail_closed for f in findings)


def test_distinct_fill_ids_no_duplicate_detection() -> None:
    engine = ReconciliationEngine()
    snapshot = engine.snapshot_from_source({"orders": [], "positions": [], "fills": [{"trade_id": "t1", "symbol": "BTCUSDT"}, {"trade_id": "t2", "symbol": "BTCUSDT"}]})
    findings, _, _ = engine.reconcile(intended_orders=[], lifecycle_state_by_symbol={}, snapshot=snapshot, mode="LIVE")
    assert not any(f.finding_type == "DUPLICATE_FILL" for f in findings)


def _seed_live_gate_minimum(session: Session) -> None:
    save_order_decision(session, decision_id="d-1", signal_id="s-1", symbol="BTCUSDT", mode="PAPER", decision="REJECTED", reject_reason="HIGH_SPREAD", score=7.0, rr=1.4)
    save_order_decision(session, decision_id="d-2", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", decision="ACCEPTED", reject_reason="", score=8.2, rr=2.0)
    save_trade_lifecycle_event(session, event_id="e-1", signal_id="s-1", symbol="BTCUSDT", mode="PAPER", lifecycle_state="SIGNAL_CREATED", event_ts="2026-01-01T00:00:00Z")
    save_trade_lifecycle_event(session, event_id="e-2", signal_id="s-1", symbol="BTCUSDT", mode="PAPER", lifecycle_state="SIGNAL_REJECTED", reject_reason="HIGH_SPREAD", event_ts="2026-01-01T00:00:01Z", previous_lifecycle_state="SIGNAL_CREATED")
    save_trade_lifecycle_event(session, event_id="e-3", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", lifecycle_state="SIGNAL_CREATED", event_ts="2026-01-01T00:00:00Z")
    save_trade_lifecycle_event(session, event_id="e-4", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", lifecycle_state="WAITING_ENTRY_ZONE", event_ts="2026-01-01T00:00:01Z", previous_lifecycle_state="SIGNAL_CREATED")
    save_trade_lifecycle_event(session, event_id="e-5", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", lifecycle_state="ENTRY_TRIGGERED", event_ts="2026-01-01T00:00:02Z", previous_lifecycle_state="WAITING_ENTRY_ZONE")
    save_trade_lifecycle_event(session, event_id="e-6", signal_id="s-2", symbol="ETHUSDT", mode="PAPER", lifecycle_state="CANCELLED", event_ts="2026-01-01T00:00:03Z", previous_lifecycle_state="ENTRY_TRIGGERED")


def test_live_qualification_reconciliation_findings_fail_closed() -> None:
    class _DirtyProvider:
        def snapshot(self):
            return {
                "evidence_status": "COMPLETE",
                "orders": [{"order_id": "orphan-1", "symbol": "BTCUSDT", "status": "OPEN", "created_at": "2020-01-01T00:00:00Z"}],
                "positions": [{"symbol": "ETHUSDT", "position_amt": 1}],
                "fills": [{"trade_id": "dup-1", "symbol": "BTCUSDT"}, {"trade_id": "dup-1", "symbol": "BTCUSDT"}],
            }

    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.LIVE, enable_shadow_mode=True, enable_canary_mode=True, operator_live_acknowledged=True),
        ai_brain=_AcceptBrain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        real_execution_adapter=object(),
        persistence_engine=init_db("sqlite+pysqlite:///:memory:"),
        scanner_source="EXCHANGE_PUBLIC_MARKET_DATA",
        live_reconciliation_provider=_DirtyProvider(),
    )
    with Session(runtime.persistence_engine) as session:
        _seed_live_gate_minimum(session)
    with asyncio.Runner() as runner:
        try:
            runner.run(runtime._run_live_qualification_gate())
        except RuntimeError:
            pass
    assert runtime._qualification_report is not None
    failed = {c.name for c in runtime._qualification_report.checks if not c.passed}
    assert {"reconciliation_no_orphans", "duplicate_execution_free", "reconciliation_fail_closed_clear"} <= failed
