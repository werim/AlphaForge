from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from alphaforge.burnin_campaign import create_campaign, start_or_resume_campaign
from alphaforge.execution_ownership import (
    acquire_execution_ownership,
    ensure_execution_ownership_schema,
)
from alphaforge.persistence import init_db
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator
from alphaforge.sqlite_safety import (
    SQLiteBusyExhausted,
    run_sqlite_write_with_retry,
)


def _engine(tmp_path, name: str):
    path = tmp_path / name
    return path, init_db(f"sqlite+pysqlite:///{path}")


def _campaign_runtime(tmp_path, name: str = "runtime.db"):
    path, engine = _engine(tmp_path, name)
    with engine.begin() as conn:
        campaign = create_campaign(
            conn,
            release_id=f"{name}-release",
            duration_days=1,
            symbols=["BTCUSDT"],
            intervals=["1m"],
        )
        run = start_or_resume_campaign(conn, campaign.campaign_id)
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=ExecutionMode.PAPER,
            paper_candidate_notional=10.0,
        ),
        ai_brain=None,
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        persistence_engine=engine,
    )
    runtime._campaign_id = campaign.campaign_id
    runtime._burnin_run_id = run["burnin_run_id"]
    return path, engine, runtime


def _position_inputs():
    decision = {
        "signal_id": "issue571:signal",
        "decision_time": "2026-10-01T06:00:00Z",
    }
    market = {
        "entry": 100.0,
        "sl": 99.0,
        "tp": 103.0,
        "side": "LONG",
        "notional": 10.0,
        "source_exchange": "binance",
        "timeframe": "1m",
        "candidate_rr": 3.0,
        "rr": 3.0,
        "effective_rr": 2.8,
        "execution_ctx": {
            "spread_pct": 0.0002,
            "expected_slippage_pct": 0.0002,
            "fee_pct": 0.0004,
            "funding_rate_pct": 0.0,
            "latency_ms": 0.0,
            "volatility_penalty_pct": 0.0,
            "liquidity_penalty_pct": 0.0,
        },
    }
    result = {
        "status": "filled",
        "expected_fill": 100.02,
        "actual_fill": 100.02,
        "fill_price": 100.02,
        "fill_timestamp": "2026-10-01T06:00:01Z",
    }
    return decision, market, result


def test_shared_sqlite_writer_primitive_retries_fresh_and_preserves_contract(tmp_path):
    path, engine = _engine(tmp_path, "primitive.db")
    blocker = sqlite3.connect(path, timeout=0.01)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(SQLiteBusyExhausted):
            run_sqlite_write_with_retry(
                engine,
                lambda conn: conn.execute(
                    text("INSERT INTO signals(signal_id,symbol) VALUES ('busy','BTCUSDT')")
                ),
                operation_name="issue571_primitive",
            )
    finally:
        blocker.rollback()
        blocker.close()

    run_sqlite_write_with_retry(
        engine,
        lambda conn: conn.execute(
            text("INSERT INTO signals(signal_id,symbol) VALUES ('busy','BTCUSDT')")
        ),
        operation_name="issue571_primitive",
    )
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM signals WHERE signal_id='busy'")
        ).scalar_one() == 1
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 30000
        assert str(conn.exec_driver_sql("PRAGMA journal_mode").scalar_one()).lower() == "wal"
        assert conn.exec_driver_sql("PRAGMA synchronous").scalar_one() == 1
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


def test_shared_sqlite_writer_primitive_does_not_hide_non_busy_errors(tmp_path):
    _, engine = _engine(tmp_path, "nonbusy.db")
    with pytest.raises(OperationalError, match="no such table"):
        run_sqlite_write_with_retry(
            engine,
            lambda conn: conn.execute(text("INSERT INTO missing_table(x) VALUES (1)")),
            operation_name="issue571_nonbusy",
        )


def test_paper_pending_position_lock_is_fail_closed_and_replay_is_exactly_once(tmp_path):
    path, engine, runtime = _campaign_runtime(tmp_path, "pending.db")
    decision, market, result = _position_inputs()
    blocker = sqlite3.connect(path, timeout=0.01)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        persisted = runtime._persist_pending_paper_position(
            "BTCUSDT", "issue571:trade", decision, market, result
        )
    finally:
        blocker.rollback()
        blocker.close()

    assert persisted is None
    assert runtime.metrics.paper_position_persistence_failures == 1
    assert runtime._burnin_evidence_incomplete is True
    assert runtime._recovery_required is True
    assert runtime._fail_closed_reason == "PAPER_POSITION_PERSISTENCE_FAILED"
    with engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM burnin_pending_position_outcomes "
                "WHERE trade_id='issue571:trade'"
            )
        ).scalar_one() == 0

    first = runtime._persist_pending_paper_position(
        "BTCUSDT", "issue571:trade", decision, market, result
    )
    second = runtime._persist_pending_paper_position(
        "BTCUSDT", "issue571:trade", decision, market, result
    )
    assert first == pytest.approx(10.0)
    assert second == pytest.approx(10.0)
    with engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM burnin_pending_position_outcomes "
                "WHERE trade_id='issue571:trade'"
            )
        ).scalar_one() == 1


def test_paper_execute_never_creates_phantom_position_when_persistence_is_locked(tmp_path):
    path, engine, runtime = _campaign_runtime(tmp_path, "execute.db")
    _, ownership_engine = _engine(tmp_path, "ownership.db")
    runtime.execution_ownership_engine = ownership_engine
    decision, market, _ = _position_inputs()

    blocker = sqlite3.connect(path, timeout=0.01)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        executed = asyncio.run(
            runtime._execute(
                "BTCUSDT",
                {**decision, "order_type": "MARKET"},
                market,
            )
        )
    finally:
        blocker.rollback()
        blocker.close()

    assert executed is False
    assert "BTCUSDT" not in runtime._active_positions
    assert "BTCUSDT" not in runtime._pending_orders
    assert runtime._recovery_required is True
    assert runtime._fail_closed_reason == "PAPER_POSITION_PERSISTENCE_FAILED"


def test_execution_ownership_busy_blocks_mutation_without_fencing_bypass(tmp_path):
    path, engine = _engine(tmp_path, "ownership-busy.db")
    ensure_execution_ownership_schema(engine)
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=None,
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        execution_ownership_engine=engine,
    )
    runtime._campaign_id = "issue571-campaign"

    blocker = sqlite3.connect(path, timeout=0.01)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        evidence = runtime._authorize_execution_ownership()
    finally:
        blocker.rollback()
        blocker.close()

    assert evidence["acquired"] is False
    assert evidence["reason"] == "EXECUTION_OWNERSHIP_PERSISTENCE_BUSY"
    assert runtime.metrics.execution_ownership_persistence_failures == 1
    assert runtime._recovery_required is True
    assert runtime._fail_closed_reason == "EXECUTION_OWNERSHIP_PERSISTENCE_BUSY"
    assert runtime._execution_fencing_token is None

    owner = acquire_execution_ownership(
        engine,
        account_scope="paper:campaign:issue571-campaign",
        mode="PAPER",
        owner_instance_id="runtime-a",
        owner_startup_id="startup-a",
        lease_ttl_sec=30.0,
        now=100.0,
    )
    replay = acquire_execution_ownership(
        engine,
        account_scope="paper:campaign:issue571-campaign",
        mode="PAPER",
        owner_instance_id="runtime-a",
        owner_startup_id="startup-a",
        lease_ttl_sec=30.0,
        now=101.0,
    )
    assert owner.acquired is True
    assert replay.acquired is True
    assert replay.fencing_token == owner.fencing_token == 1


def test_geometry_diagnostic_busy_is_nonfatal_but_evidence_is_not_complete(tmp_path):
    path, engine, runtime = _campaign_runtime(tmp_path, "geometry.db")
    blocker = sqlite3.connect(path, timeout=0.01)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        runtime._persist_geometry_diagnostic(
            "BTCUSDT",
            {
                "source_exchange": "binance",
                "timeframe": "1m",
                "regime": "TRENDING",
                "geometry_status": "UNAVAILABLE",
                "geometry_source": "MTF_SETUP_STRUCTURE",
            },
            "NO_EXECUTION_CANDLE_IDENTITY",
        )
    finally:
        blocker.rollback()
        blocker.close()

    assert runtime.metrics.geometry_diagnostic_persistence_failures == 1
    assert runtime._burnin_evidence_incomplete is True
    assert runtime._fatal_task_exception is None
    with engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM burnin_observations "
                "WHERE json_extract(metrics_json,'$.observation_kind')='DIAGNOSTIC'"
            )
        ).scalar_one() == 0


def test_live_precheck_busy_never_yields_durable_pass_and_replay_is_idempotent(
    tmp_path, monkeypatch
):
    path, engine = _engine(tmp_path, "precheck.db")
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.LIVE_PRECHECK),
        ai_brain=None,
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        persistence_engine=engine,
    )
    monkeypatch.setattr(
        RuntimeOrchestrator,
        "_evaluate_pre_submit",
        lambda self, *args, **kwargs: {
            "decision": "ACCEPTED",
            "reject_reason": "",
            "order_type": "MARKET",
            "confidence": 0.9,
            "score": 0.9,
            "raw_rr": 2.0,
            "effective_rr": 1.8,
            "explanation": "fixture",
        },
    )
    signal = {
        "signal_id": "issue571:precheck",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "risk_reward": 2.0,
    }
    market = {
        "entry": 100.0,
        "sl": 99.0,
        "tp": 102.0,
        "side": "LONG",
        "execution_ctx": {
            "spread_pct": 0.0002,
            "expected_slippage_pct": 0.0002,
            "fee_pct": 0.0004,
        },
    }
    score = SimpleNamespace(total_score=0.9)
    plan = SimpleNamespace(
        decision="ACCEPTED",
        reason="",
        order_type="MARKET",
        confidence=0.9,
    )

    blocker = sqlite3.connect(path, timeout=0.01)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        persisted = asyncio.run(
            runtime._persist_live_precheck_evidence(
                "BTCUSDT", signal, market, {}, {}, score, plan, "fixture", 1.8
            )
        )
    finally:
        blocker.rollback()
        blocker.close()

    assert persisted is False
    assert runtime.metrics.live_precheck_persistence_failures == 1
    assert runtime._recovery_required is True
    assert runtime._fail_closed_reason == "LIVE_PRECHECK_EVIDENCE_PERSISTENCE_FAILED"
    with engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM order_decisions "
                "WHERE decision_id='live_precheck:issue571:precheck'"
            )
        ).scalar_one() == 0

    assert asyncio.run(
        runtime._persist_live_precheck_evidence(
            "BTCUSDT", signal, market, {}, {}, score, plan, "fixture", 1.8
        )
    ) is True
    assert asyncio.run(
        runtime._persist_live_precheck_evidence(
            "BTCUSDT", signal, market, {}, {}, score, plan, "fixture", 1.8
        )
    ) is True
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT COUNT(*), MAX(no_submit_verified), MAX(parity_result) "
                "FROM order_decisions "
                "WHERE decision_id='live_precheck:issue571:precheck'"
            )
        ).one()
    assert row[0] == 1
    assert row[1] == 1
    assert row[2] == "PASS"
