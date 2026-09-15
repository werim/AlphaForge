from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
from urllib import error

from sqlalchemy import text

from alphaforge.binance_reconciliation_provider import (
    BinanceReadonlyReconciliationConfig, BinanceReadonlyReconciliationProvider,
)
from alphaforge.burnin_campaign import (
    BurnInCampaignRunner, ProviderFailure, create_campaign, event, start_or_resume_campaign,
)
from alphaforge.burnin_resolver import persist_pending_position
from alphaforge.burnin_ops import bootstrap_ops_schema, health_payload
from alphaforge.persistence import init_db
from alphaforge.provider_failures import classify_provider_exception
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


class _Brain:
    session = None


async def _scanner():
    return []


def test_provider_failure_classes_are_conservative():
    for exc in (socket.gaierror(-3, "dns"), error.URLError("network"),
                TimeoutError("timeout"), ConnectionResetError("reset")):
        assert classify_provider_exception(exc) == "TRANSIENT_TRANSPORT"
    assert classify_provider_exception(error.HTTPError("https://example.com", 401, "auth", None, None)) == "PERMANENT_AUTH_OR_PROTOCOL"
    assert classify_provider_exception(ValueError("unclassified")) == "UNKNOWN"


def _runtime(tmp_path, *, grace=300.0):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}")
    outage = {"active": True, "auth": False}

    def http(url, headers, timeout):
        if outage["auth"]:
            raise error.HTTPError(url, 401, "unauthorized", None, None)
        if outage["active"]:
            raise socket.gaierror(-3, "test dns outage")
        return []

    provider = BinanceReadonlyReconciliationProvider(
        config=BinanceReadonlyReconciliationConfig(base_url="https://fapi.binance.com", api_key="k", api_secret="s"),
        http_get_json=http,
    )
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER,
                             reconciliation_interval_sec=0.01,
                             provider_transient_outage_grace_seconds=grace),
        ai_brain=_Brain(), market_scanner=_scanner, persistence_engine=engine,
        live_reconciliation_provider=provider,
    )
    runtime.metrics.persistence_enabled = True
    return engine, runtime, outage


def _campaign(tmp_path, *, grace=300.0):
    path = tmp_path / "campaign.db"
    engine = init_db(f"sqlite+pysqlite:///{path}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    campaign = create_campaign(conn, release_id="test-outage", duration_days=7,
                               symbols=["ETHUSDT"], intervals=["1m"])
    run = start_or_resume_campaign(conn, campaign.campaign_id)
    persist_pending_position(
        conn, trade_id="paper-open", campaign_id=campaign.campaign_id,
        burnin_run_id=run["burnin_run_id"], signal_id="s", symbol="ETHUSDT",
        side="LONG", entry_time="2026-01-01T00:00:00Z", planned_entry=100,
        simulated_fill=100, stop=90, target=120, quantity=1, notional=100,
        entry_spread=0, entry_slippage=0, entry_fee=0, regime="TREND",
        source_provenance={},
    )
    conn.commit()
    conn.close()
    failure = {"active": True, "permanent": False}

    def candles(*_):
        if failure["permanent"]:
            raise error.HTTPError("https://fapi.binance.com", 401, "unauthorized", None, None)
        if failure["active"]:
            raise ProviderFailure("PROVIDER_FAILURE:URLError") from error.URLError(socket.gaierror(-3, "test dns outage"))
        return []

    runner = BurnInCampaignRunner(engine, campaign.campaign_id, candles,
                                  provider_transient_outage_grace_seconds=grace)
    return engine, path, campaign.campaign_id, run["burnin_run_id"], runner, failure


def test_single_gaierror_keeps_runtime_and_resolver_alive_then_clean_recovers(tmp_path):
    engine, runtime, outage = _runtime(tmp_path)
    runtime._active_positions["ETHUSDT"] = 100
    runtime._pending_orders["ETHUSDT"] = {"order_id": "pending", "symbol": "ETHUSDT"}
    asyncio.run(runtime._run_reconciliation_once())
    assert not runtime._stop_event.is_set()
    assert runtime._reconciliation_status == "EXCHANGE_STATE_UNKNOWN"
    assert runtime._evaluate_runtime_risk("BTCUSDT", {}) == "EXCHANGE_STATE_UNKNOWN"
    assert runtime._build_runtime_state_snapshot(status="OPERATING").runtime_status == "RECOVERY_REQUIRED"
    assert asyncio.run(runtime._execute("BTCUSDT", {"signal_id": "inflight"}, {"entry": 100})) is False
    assert runtime.metrics.executions == 0
    assert runtime._active_positions == {"ETHUSDT": 100}
    runtime._persist_runtime_heartbeat()
    with engine.connect() as conn:
        row = conn.execute(text("SELECT diagnostics_json FROM exchange_reconciliation_events ORDER BY id DESC LIMIT 1")).scalar_one()
        assert conn.execute(text("SELECT runtime_state FROM runtime_heartbeats ORDER BY id DESC LIMIT 1")).scalar_one() == "RECOVERY_REQUIRED"
    attempts = json.loads(row)["request_attempts"]
    assert [a["outcome"] for a in attempts] == ["RETRY", "FAIL"]
    outage["active"] = False
    asyncio.run(runtime._run_reconciliation_once())
    assert runtime._reconciliation_status == "CLEAN"
    assert runtime._fail_closed_reason is None
    assert runtime._build_runtime_state_snapshot(status="OPERATING").runtime_status == "OPERATING"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT status FROM runtime_recovery_events ORDER BY id DESC LIMIT 1")).scalar_one() == "RECOVERED"
        assert [r[0] for r in conn.execute(text("SELECT status FROM exchange_reconciliation_events ORDER BY id"))] == ["EXCHANGE_STATE_UNKNOWN", "CLEAN"]
    engine.dispose()


def test_identical_provider_snapshots_keep_each_failure_and_recovery_auditable(tmp_path):
    engine, runtime, _ = _runtime(tmp_path)
    source = {"orders": [], "positions": [], "fills": [], "evidence_status": "COMPLETE",
              "authenticated": True, "input_source": "AUTHENTICATED_EXCHANGE_SNAPSHOT"}
    class StaticProvider:
        def snapshot(self):
            return dict(source)
    runtime.live_reconciliation_provider = StaticProvider()
    asyncio.run(runtime._run_reconciliation_once())
    source.update(evidence_status="INCOMPLETE", failure_class="TRANSIENT_TRANSPORT")
    for _ in range(3):
        asyncio.run(runtime._run_reconciliation_once())
    source.update(evidence_status="COMPLETE", failure_class=None)
    asyncio.run(runtime._run_reconciliation_once())
    with engine.connect() as conn:
        statuses = [r[0] for r in conn.execute(text("SELECT status FROM exchange_reconciliation_events ORDER BY id"))]
        assert statuses == ["CLEAN", "EXCHANGE_STATE_UNKNOWN", "EXCHANGE_STATE_UNKNOWN", "EXCHANGE_STATE_UNKNOWN", "CLEAN"]
        assert conn.execute(text("SELECT COUNT(*) FROM runtime_recovery_events WHERE status='RECOVERED'")).scalar_one() == 1
    engine.dispose()


def test_provider_that_raises_gaierror_does_not_exit_reconciliation_loop(tmp_path):
    engine, runtime, _ = _runtime(tmp_path)
    class RaisingProvider:
        def snapshot(self):
            raise socket.gaierror(-3, "dns")
    runtime.live_reconciliation_provider = RaisingProvider()
    asyncio.run(runtime._run_reconciliation_once())
    assert not runtime._stop_event.is_set()
    assert runtime._execution_reconciliation_blocked()
    with engine.connect() as conn:
        details = json.loads(conn.execute(text("SELECT diagnostics_json FROM exchange_reconciliation_events ORDER BY id DESC LIMIT 1")).scalar_one())
        assert details["failure_class"] == "TRANSIENT_TRANSPORT"
    engine.dispose()


def test_complete_but_dirty_exchange_read_does_not_clear_unknown(tmp_path):
    engine, runtime, _ = _runtime(tmp_path)
    asyncio.run(runtime._run_reconciliation_once())
    class ExposedProvider:
        def snapshot(self):
            return {"evidence_status": "COMPLETE", "authenticated": True,
                    "input_source": "AUTHENTICATED_EXCHANGE_SNAPSHOT",
                    "orders": [{"order_id": "orphan", "symbol": "BTCUSDT", "status": "OPEN"}],
                    "positions": [], "fills": []}
    runtime.live_reconciliation_provider = ExposedProvider()
    asyncio.run(runtime._run_reconciliation_once())
    assert runtime._unknown_exchange_state is True
    assert runtime._fail_closed_reason == "ORPHAN_ORDER_DETECTED"
    assert runtime._reconciliation_status == "DIRTY"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM runtime_recovery_events WHERE status='RECOVERED'")).scalar_one() == 0
    engine.dispose()


def test_multiple_transient_resolver_failures_remain_auditable_within_grace(tmp_path):
    engine, path, cid, _, runner, failure = _campaign(tmp_path)
    for _ in range(4):
        assert runner.resolver_tick()["status"] == "RETRYING"
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT campaign_status FROM burnin_campaigns WHERE campaign_id=?", (cid,)).fetchone()[0] == "RUNNING"
    details = [json.loads(r[0]) for r in conn.execute("SELECT details_json FROM burnin_campaign_events WHERE campaign_id=? AND event_type='RESOLVER_BATCH_FAILED' ORDER BY id", (cid,))]
    assert len(details) == 4 and all(d["failure_class"] == "TRANSIENT_TRANSPORT" for d in details)
    async def prove_loop_alive():
        runner.resolver_interval_seconds = 0.01
        runner._stop_event = asyncio.Event()
        task = asyncio.create_task(runner._resolver_loop())
        await asyncio.sleep(0.05)
        assert not task.done()
        runner._stop_event.set()
        await task
    asyncio.run(prove_loop_alive())
    failure["active"] = False
    assert runner.resolver_tick()["status"] == "OK"
    assert conn.execute("SELECT COUNT(*) FROM burnin_campaign_events WHERE campaign_id=? AND event_type='RESOLVER_PROVIDER_RECOVERED'", (cid,)).fetchone()[0] == 1
    conn.close(); engine.dispose()


def test_resolver_only_outage_blocks_attached_runtime_until_clean_reconciliation(tmp_path):
    runtime_engine, runtime, exchange = _runtime(tmp_path)
    runner_engine, _, _, _, runner, candles = _campaign(tmp_path)
    runtime._unknown_exchange_state = False
    runtime._reconciliation_status = "CLEAN"
    runtime._exchange_read_only_status = "AVAILABLE"
    runner._attached_runtime = runtime
    runner.resolver_interval_seconds = 0.01

    async def exercise():
        runner._stop_event = asyncio.Event()
        task = asyncio.create_task(runner._resolver_loop())
        async def until(predicate):
            for _ in range(100):
                if predicate(): return
                await asyncio.sleep(0.01)
            raise AssertionError("resolver transition did not occur")
        try:
            await until(lambda: runtime._resolver_provider_unavailable)
            assert runtime._build_runtime_state_snapshot(status="OPERATING").runtime_status == "RECOVERY_REQUIRED"
            assert await runtime._execute("BTCUSDT", {"signal_id": "blocked"}, {"entry": 100}) is False
            candles["active"] = False
            await until(lambda: runtime._resolver_provider_recovery_pending)
            assert await runtime._execute("BTCUSDT", {"signal_id": "still-blocked"}, {"entry": 100}) is False
            exchange["active"] = False
            await runtime._run_reconciliation_once()
            assert runtime._reconciliation_status == "CLEAN"
            assert not runtime._execution_reconciliation_blocked()
        finally:
            runner._stop_event.set()
            await task

    asyncio.run(exercise())
    with runtime_engine.connect() as conn:
        assert conn.execute(text("SELECT status FROM runtime_recovery_events ORDER BY id DESC LIMIT 1")).scalar_one() == "RECOVERED"
    runtime_engine.dispose(); runner_engine.dispose()


def test_idle_resolver_probes_provider_before_claiming_recovery(tmp_path):
    engine, path, cid, _, runner, failure = _campaign(tmp_path)
    assert runner.resolver_tick()["status"] == "RETRYING"
    with engine.begin() as conn:
        conn.execute(text("UPDATE burnin_pending_position_outcomes SET status='CLOSED' WHERE campaign_id=:cid"), {"cid": cid})
    calls = []
    original = runner.candle_provider
    def tracked(*args):
        calls.append(args)
        return original(*args)
    runner.candle_provider = tracked
    assert runner.resolver_tick()["status"] == "RETRYING"
    assert calls
    failure["active"] = False
    assert runner.resolver_tick()["status"] == "OK"
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM burnin_campaign_events WHERE event_type='RESOLVER_PROVIDER_RECOVERED'").fetchone()[0] == 1
    conn.close(); engine.dispose()


def test_grace_expiry_pauses_all_continuation_rows_atomically(tmp_path):
    engine, path, cid, run, runner, _ = _campaign(tmp_path, grace=300)
    assert runner.resolver_tick()["status"] == "RETRYING"
    runner._transient_failure_started_monotonic -= 301
    assert runner.resolver_tick()["status"] == "PAUSED"
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT campaign_status,last_error FROM burnin_campaigns WHERE campaign_id=?", (cid,)).fetchone() == ("PAUSED", "PROVIDER_TRANSIENT_OUTAGE_GRACE_EXPIRED")
    assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (run,)).fetchone()[0] == "PAUSED"
    assert conn.execute("SELECT status FROM burnin_campaign_runs WHERE burnin_run_id=?", (run,)).fetchone()[0] == "PAUSED"
    conn.close(); engine.dispose()


def test_permanent_auth_failure_pauses_without_grace(tmp_path):
    engine, path, cid, run, runner, failure = _campaign(tmp_path)
    failure["permanent"] = True
    assert runner.resolver_tick()["status"] == "PAUSED"
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT last_error FROM burnin_campaigns WHERE campaign_id=?", (cid,)).fetchone()[0] == "PROVIDER_PERMANENT_FAILURE"
    assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (run,)).fetchone()[0] == "PAUSED"
    conn.close(); engine.dispose()


def test_runtime_permanent_auth_and_grace_expiry_use_consistent_pause(tmp_path):
    engine, runtime, outage = _runtime(tmp_path)
    path = tmp_path / "runtime.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    campaign = create_campaign(conn, release_id="runtime-outage", duration_days=7,
                               symbols=["ETHUSDT"], intervals=["1m"])
    run = start_or_resume_campaign(conn, campaign.campaign_id)
    conn.commit(); conn.close()
    runtime._campaign_id = campaign.campaign_id
    runtime._burnin_run_id = run["burnin_run_id"]
    outage["auth"] = True
    asyncio.run(runtime._run_reconciliation_once())
    assert runtime._stop_event.is_set()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT campaign_status,last_error FROM burnin_campaigns WHERE campaign_id=?", (campaign.campaign_id,)).fetchone() == ("PAUSED", "PROVIDER_PERMANENT_FAILURE")
    assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (run["burnin_run_id"],)).fetchone()[0] == "PAUSED"
    assert conn.execute("SELECT status FROM burnin_campaign_runs WHERE burnin_run_id=?", (run["burnin_run_id"],)).fetchone()[0] == "PAUSED"
    conn.close(); engine.dispose()


def test_runtime_transient_grace_expiry_pauses_consistent_lineage(tmp_path):
    engine, runtime, _ = _runtime(tmp_path)
    path = tmp_path / "runtime.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    campaign = create_campaign(conn, release_id="runtime-transient", duration_days=7,
                               symbols=["ETHUSDT"], intervals=["1m"])
    run = start_or_resume_campaign(conn, campaign.campaign_id)
    conn.commit(); conn.close()
    runtime._campaign_id = campaign.campaign_id
    runtime._burnin_run_id = run["burnin_run_id"]
    asyncio.run(runtime._run_reconciliation_once())
    assert not runtime._stop_event.is_set()
    runtime._transient_provider_outage_started_monotonic -= 301
    asyncio.run(runtime._run_reconciliation_once())
    assert runtime._stop_event.is_set()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT campaign_status,last_error FROM burnin_campaigns WHERE campaign_id=?", (campaign.campaign_id,)).fetchone() == ("PAUSED", "PROVIDER_TRANSIENT_OUTAGE_GRACE_EXPIRED")
    assert conn.execute("SELECT status FROM burnin_runs WHERE burnin_run_id=?", (run["burnin_run_id"],)).fetchone()[0] == "PAUSED"
    assert conn.execute("SELECT status FROM burnin_campaign_runs WHERE burnin_run_id=?", (run["burnin_run_id"],)).fetchone()[0] == "PAUSED"
    conn.close(); engine.dispose()


def test_clean_read_does_not_clear_unknown_until_recovery_transaction_commits(tmp_path):
    engine, runtime, outage = _runtime(tmp_path)
    asyncio.run(runtime._run_reconciliation_once())
    outage["active"] = False
    blocker = sqlite3.connect(tmp_path / "runtime.db", timeout=0.01)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        asyncio.run(runtime._run_reconciliation_once())
        assert runtime._unknown_exchange_state is True
        assert runtime._fail_closed_reason == "RECONCILIATION_PERSISTENCE_FAILED"
        assert runtime._execution_reconciliation_blocked()
    finally:
        blocker.rollback(); blocker.close()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM runtime_recovery_events WHERE status='RECOVERED'")).scalar_one() == 0
    asyncio.run(runtime._run_reconciliation_once())
    assert runtime._unknown_exchange_state is False
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM runtime_recovery_events WHERE status='RECOVERED'")).scalar_one() == 1
        statuses = [r[0] for r in conn.execute(text("SELECT status FROM exchange_reconciliation_events ORDER BY id"))]
        assert statuses == ["EXCHANGE_STATE_UNKNOWN", "PERSISTENCE_FAILED", "CLEAN"]
    engine.dispose()


def test_final_paper_boundary_rechecks_after_earlier_clean_gate(tmp_path):
    _, runtime, _ = _runtime(tmp_path)
    runtime._unknown_exchange_state = False
    runtime._exchange_read_only_status = "AVAILABLE"
    runtime._reconciliation_status = "CLEAN"
    assert runtime._evaluate_runtime_risk("BTCUSDT", {"market_ts": 99999999999}) is None
    runtime._unknown_exchange_state = True
    runtime._exchange_read_only_status = "UNAVAILABLE"
    runtime._reconciliation_status = "EXCHANGE_STATE_UNKNOWN"
    runtime._fail_closed_reason = "EXCHANGE_STATE_UNKNOWN"
    assert asyncio.run(runtime._execute("BTCUSDT", {"signal_id": "inflight"}, {"entry": 100})) is False
    assert runtime.metrics.executions == 0


def test_recovered_provider_failures_do_not_accumulate_in_watchdog(tmp_path):
    engine, path, cid, _, runner, failure = _campaign(tmp_path)
    for _ in range(3):
        runner.resolver_tick()
    failure["active"] = False
    assert runner.resolver_tick()["status"] == "OK"
    failure["active"] = True
    assert runner.resolver_tick()["status"] == "RETRYING"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    bootstrap_ops_schema(conn)
    health = health_payload(conn, cid, max_heartbeat_age=10**9)
    assert health["provider_failure_count"] == 1
    assert "REPEATED_PROVIDER_FAILURES" not in health["unhealthy_reasons"]
    assert "TRANSIENT_PROVIDER_RECOVERY" in health["warning_reasons"]
    conn.close(); engine.dispose()
