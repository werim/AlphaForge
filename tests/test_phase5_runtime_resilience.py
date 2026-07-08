from __future__ import annotations

from sqlalchemy import text

from alphaforge.live_readiness import LiveReadinessEvaluator
from alphaforge.persistence import init_db
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator
from alphaforge.runtime_state import RuntimeStateSnapshot, latest_runtime_state_snapshot, save_runtime_state_snapshot


class _Brain:
    session = None


async def _scanner():
    return []


class _CleanProvider:
    def snapshot(self):
        return {"evidence_status": "COMPLETE", "orders": [], "positions": [], "balances": [{"asset": "USDT", "free": 1000}], "authenticated": True}


def _runtime(engine, mode=ExecutionMode.PAPER, provider=None):
    rt = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=mode, heartbeat_interval_sec=1.0, require_exchange_reconciliation_for_paper=True),
        ai_brain=_Brain(),
        market_scanner=_scanner,
        persistence_engine=engine,
        live_reconciliation_provider=provider,
    )
    rt.metrics.persistence_enabled = True
    return rt


def test_startup_snapshot_and_reconciliation_event_persisted(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path/'rt.db'}")
    rt = _runtime(engine, provider=_CleanProvider())
    rt._last_start_time = "2026-07-08T00:00:00Z"
    rt._persist_runtime_state_snapshot("STARTUP")
    snap = latest_runtime_state_snapshot(engine)
    assert snap["instance_id"] == rt.runtime_instance_id
    assert snap["runtime_status"] == "STARTUP"


def test_unclean_shutdown_marks_recovery_required(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path/'rt.db'}")
    save_runtime_state_snapshot(engine, RuntimeStateSnapshot(mode="PAPER", requested_mode="PAPER", actual_mode="PAPER", runtime_status="OPERATING", instance_id="runtime:old"))
    rt = _runtime(engine, provider=_CleanProvider())
    rt._load_recovery_state()
    assert rt._recovery_required is True
    assert rt._fail_closed_reason == "UNCLEAN_SHUTDOWN_RECOVERY_REQUIRED"


def test_stale_persisted_pending_order_triggers_fail_closed(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path/'rt.db'}")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO orders(order_id, symbol, status, created_at) VALUES ('o1','BTCUSDT','PENDING','2026-07-07T00:00:00Z')"))
    rt = _runtime(engine, provider=_CleanProvider())
    rt._load_recovery_state()
    assert rt._fail_closed_reason == "STALE_PENDING_ORDER"
    assert rt._evaluate_runtime_risk("ETHUSDT", {"market_ts": 0}) == "STALE_PENDING_ORDER"


def test_exchange_readonly_unavailable_fails_paper_runtime_gate(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path/'rt.db'}")
    rt = _runtime(engine, provider=None)
    import asyncio
    asyncio.run(rt._run_reconciliation_once())
    assert rt._fail_closed_reason == "EXCHANGE_RECONCILIATION_UNAVAILABLE"
    assert rt._evaluate_runtime_risk("BTCUSDT", {"market_ts": 0}) == "EXCHANGE_RECONCILIATION_UNAVAILABLE"


def test_backtest_reconciliation_not_required_snapshot(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path/'rt.db'}")
    rt = _runtime(engine, mode=ExecutionMode.BACKTEST)
    import asyncio
    asyncio.run(rt._run_reconciliation_once())
    snap = latest_runtime_state_snapshot(engine)
    assert snap["reconciliation_status"] == "NOT_REQUIRED_BACKTEST"
    assert snap["exchange_read_only_status"] == "NOT_REQUIRED_BACKTEST"


def test_readiness_fails_on_missing_and_dirty_runtime_snapshot(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path/'rt.db'}")
    checks = {c.name: c for c in LiveReadinessEvaluator(engine)._check_runtime_state_snapshot()}
    assert checks["runtime_state_snapshot_present"].passed is False
    save_runtime_state_snapshot(engine, RuntimeStateSnapshot(mode="PAPER", requested_mode="PAPER", actual_mode="PAPER", runtime_status="RECOVERY_REQUIRED", instance_id="runtime:dirty", recovery_action_required=True, fail_closed_reason="ORPHAN_ORDER_DETECTED", orphan_order_count=1, reconciliation_status="DIRTY", exchange_read_only_status="AVAILABLE"))
    checks = {c.name: c for c in LiveReadinessEvaluator(engine)._check_runtime_state_snapshot()}
    assert checks["runtime_recovery_not_required"].passed is False
    assert checks["no_orphan_orders"].passed is False
    assert checks["exchange_reconciliation_clean"].passed is False
