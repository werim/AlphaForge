from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


def test_fresh_persisted_pending_order_loads_without_stale_reason(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path/'rt.db'}")
    fresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO orders(order_id, symbol, status, created_at) VALUES ('fresh','BTCUSDT','PENDING',:ts)"), {"ts": fresh})
    rt = _runtime(engine, provider=_CleanProvider())
    rt.config.pending_order_timeout_sec = 3600.0
    rt._load_recovery_state()
    assert rt._fail_closed_reason is None
    assert rt._pending_orders["BTCUSDT"]["recovery_age_sec"] is not None
    assert "stale_reason" not in rt._pending_orders["BTCUSDT"]


def test_old_persisted_pending_order_triggers_fail_closed(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path/'rt.db'}")
    old = (datetime.now(timezone.utc) - timedelta(seconds=7200)).isoformat().replace("+00:00", "Z")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO orders(order_id, symbol, status, created_at) VALUES ('old','BTCUSDT','PENDING',:ts)"), {"ts": old})
    rt = _runtime(engine, provider=_CleanProvider())
    rt.config.pending_order_timeout_sec = 60.0
    rt._load_recovery_state()
    assert rt._fail_closed_reason == "STALE_PENDING_ORDER"
    assert rt._pending_orders["BTCUSDT"]["stale_reason"] == "PENDING_ORDER_TIMEOUT_EXCEEDED"
    assert rt._evaluate_runtime_risk("ETHUSDT", {"market_ts": 0}) == "STALE_PENDING_ORDER"


def test_malformed_pending_timestamp_fails_closed(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path/'rt.db'}")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO orders(order_id, symbol, status, created_at) VALUES ('bad','BTCUSDT','PENDING','not-a-timestamp')"))
    rt = _runtime(engine, provider=_CleanProvider())
    rt._load_recovery_state()
    assert rt._fail_closed_reason == "STALE_PENDING_ORDER"
    assert rt._pending_orders["BTCUSDT"]["stale_reason"] == "MISSING_OR_UNPARSEABLE_CREATED_AT"


def test_exchange_readonly_unavailable_fails_paper_runtime_gate(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path/'rt.db'}")
    rt = _runtime(engine, provider=None)
    import asyncio
    asyncio.run(rt._run_reconciliation_once())
    assert rt._exchange_read_only_status == "UNAVAILABLE"
    assert rt._unknown_exchange_state is True
    assert rt._fail_closed_reason == "EXCHANGE_RECONCILIATION_UNAVAILABLE"
    assert rt._evaluate_runtime_risk("BTCUSDT", {"market_ts": 0}) == "EXCHANGE_RECONCILIATION_UNAVAILABLE"


def test_paper_without_provider_fails_even_with_local_state(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path/'rt.db'}")
    rt = _runtime(engine, provider=None)
    rt._pending_orders["BTCUSDT"] = {"order_id": "o-local", "symbol": "BTCUSDT", "status": "OPEN"}
    import asyncio
    asyncio.run(rt._run_reconciliation_once())
    assert rt._exchange_read_only_status == "UNAVAILABLE"
    assert rt._reconciliation_status == "EXCHANGE_RECONCILIATION_UNAVAILABLE"
    assert rt._unknown_exchange_state is True
    assert rt._fail_closed_reason == "EXCHANGE_RECONCILIATION_UNAVAILABLE"


def test_live_precheck_without_provider_fails_closed(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path/'rt.db'}")
    rt = _runtime(engine, mode=ExecutionMode.LIVE_PRECHECK, provider=None)
    import asyncio
    asyncio.run(rt._run_reconciliation_once())
    assert rt._exchange_read_only_status == "UNAVAILABLE"
    assert rt._fail_closed_reason == "EXCHANGE_RECONCILIATION_UNAVAILABLE"


def test_diagnostic_mode_records_local_only_override(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path/'rt.db'}")
    rt = _runtime(engine, provider=None)
    rt.config.diagnostic_mode = True
    rt._pending_orders["BTCUSDT"] = {"order_id": "o-local", "symbol": "BTCUSDT", "status": "OPEN"}
    import asyncio
    asyncio.run(rt._run_reconciliation_once())
    snap = latest_runtime_state_snapshot(engine)
    assert rt._exchange_read_only_status == "LOCAL_ONLY"
    assert "LOCAL_ONLY_DIAGNOSTIC_RECONCILIATION" in snap["runtime_flags"]
    assert snap["diagnostics_json"]["local_only_reconciliation_override"] is True


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


def test_readiness_fails_when_snapshot_missing_even_with_decision_lifecycle_evidence(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path/'rt.db'}")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO order_decisions(decision_id, signal_id, symbol, mode, decision, created_at) VALUES ('d','s','BTCUSDT','PAPER','REJECTED','2026-07-08T00:00:00Z')"))
        conn.execute(text("INSERT INTO trade_lifecycle_events(event_id, signal_id, symbol, mode, lifecycle_state, event_ts) VALUES ('e','s','BTCUSDT','PAPER','SIGNAL_CREATED','2026-07-08T00:00:00Z')"))
    checks = {c.name: c for c in LiveReadinessEvaluator(engine)._check_runtime_state_snapshot()}
    assert checks["runtime_state_snapshot_present"].passed is False
    assert checks["runtime_db_persistence_verified"].passed is False
