from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from alphaforge.ai_brain import AIBrain
from alphaforge.persistence import init_db
from alphaforge import persistence as persistence_module
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator, _build_runtime_from_env, execution_mode_from_env


def _brain() -> AIBrain:
    engine = init_db("sqlite+pysqlite:///:memory:")
    return AIBrain(Session(engine), min_accept_score=0.62)




def test_ai_brain_persistence_uses_short_lived_sessions_across_to_thread(tmp_path: Path) -> None:
    db_path = tmp_path / "threadsafe.sqlite3"
    engine = init_db(f"sqlite+pysqlite:///{db_path}")
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    shared_session = factory()
    brain = AIBrain(shared_session, session_factory=factory, min_accept_score=0.62)

    signal = {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1m", "entry_price": 100.0, "risk_reward": 2.0, "setup_quality": 0.9}
    market_ctx = {"momentum_confirmation": 0.9, "liquidity_quality": 0.9, "volatility_fit": 0.8, "spread_bps": 1.0}
    regime_ctx = {"alignment": 0.9}
    stats_ctx = {"sample_size": 200}

    async def _run_calls() -> None:
        await asyncio.gather(*[
            asyncio.to_thread(brain.before_real_order, {**signal, "signal_id": f"sig-{idx}"}, market_ctx, regime_ctx, stats_ctx)
            for idx in range(6)
        ])

    asyncio.run(_run_calls())

    with factory() as verify_session:
        decisions = verify_session.execute(text("SELECT COUNT(*) FROM order_decisions WHERE signal_id LIKE 'sig-%'")) .scalar_one()
    assert decisions == 6
    shared_session.close()


def test_ai_brain_persists_canonical_effective_rr_and_execution_breakdown() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        brain = AIBrain(session, min_accept_score=0.99)
        signal = {"signal_id": "sig-rr-1", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1m", "risk_reward": 2.0}
        market_ctx = {"spread_pct": 0.002, "expected_slippage_pct": 0.001, "latency_ms": 250.0, "liquidity_score": 0.5, "funding_rate_pct": 0.0004}
        brain.before_real_order(signal, market_ctx, {"alignment": 0.7}, {})
        row = session.execute(text("SELECT s.rr, s.effective_rr, od.effective_rr, rsr.raw_rr, rsr.effective_rr FROM signals s JOIN order_decisions od ON od.signal_id=s.signal_id JOIN rejected_signal_reviews rsr ON rsr.signal_id=s.signal_id WHERE s.signal_id='sig-rr-1'")).one()
        assert row.effective_rr < row.rr
        assert row.effective_rr == row.effective_rr
        assert row.effective_rr == pytest.approx(row[2], rel=1e-9)
        assert row[4] < row[3]
        exec_features = session.execute(text("SELECT execution_features FROM ai_decision_features WHERE decision_id LIKE 'sig-rr-1:%'")).scalar_one()
        assert '"spread_penalty"' in exec_features
        assert '"slippage_penalty"' in exec_features
        assert '"latency_penalty"' in exec_features
        assert '"liquidity_penalty"' in exec_features
        assert '"funding_penalty"' in exec_features
        assert '"total_penalty"' in exec_features


def test_ai_brain_zero_cost_and_missing_execution_context_handled() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        brain = AIBrain(session, min_accept_score=0.99)
        signal = {"signal_id": "sig-rr-2", "symbol": "ETHUSDT", "side": "LONG", "timeframe": "1m", "risk_reward": 1.8}
        market_ctx = {"spread_pct": 0.0, "expected_slippage_pct": 0.0, "latency_ms": 0.0, "liquidity_score": 1.0, "funding_rate_pct": 0.0}
        brain.before_real_order(signal, market_ctx, {"alignment": 0.7}, {})
        rr, effective_rr = session.execute(text("SELECT rr, effective_rr FROM signals WHERE signal_id='sig-rr-2'")).one()
        assert effective_rr == pytest.approx(rr, rel=1e-9)

        signal_missing = {"signal_id": "sig-rr-3", "symbol": "SOLUSDT", "side": "LONG", "timeframe": "1m", "risk_reward": 1.4}
        brain.before_real_order(signal_missing, {}, {"alignment": 0.7}, {})
        exec_features = session.execute(text("SELECT execution_features FROM ai_decision_features WHERE decision_id LIKE 'sig-rr-3:%'")).scalar_one()
        assert '"completeness"' in exec_features
        assert '"missing_fields"' in exec_features

class _AlwaysAcceptBrain:
    def before_real_order(self, signal_payload, market_ctx, regime_ctx, stats_ctx):
        class _Plan:
            decision = "ACCEPTED"
            reason = ""
            confidence = 0.9
            order_type = "MARKET"
            limit_price = None
            stop_price = None

        return {}, _Plan(), "ok"


def test_execution_mode_from_env_parses_and_validates() -> None:
    assert execution_mode_from_env("paper") == ExecutionMode.PAPER
    assert execution_mode_from_env(None) == ExecutionMode.PAPER
    with pytest.raises(ValueError):
        execution_mode_from_env("sandbox")


def test_paper_execution_simulator_produces_fill() -> None:
    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=_brain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
    )
    result = orchestrator._simulate_paper_execution(
        symbol="BTCUSDT",
        decision={"order_type": "MARKET"},
        market_ctx={"entry": 100.0, "side": "LONG"},
    )
    assert result["status"] == "filled"
    assert result["fill_price"] > 100.0


def test_reject_lifecycle_persistence_increments_metrics() -> None:
    events: list[dict] = []
    rejects: list[dict] = []

    async def scanner() -> list[dict]:
        return [{"symbol": "BTCUSDT", "entry": 100.0, "sl": 99.5, "tp": 100.8, "rr": 1.0, "side": "LONG", "volume_24h_usdt": 5_000_000, "spread_pct": 0.01, "volatility_pct": 2.0, "trend_strength": 0.4, "liquidity_score": 0.8, "chop_score": 0.3}]

    def on_event(payload: dict) -> None:
        events.append(payload)

    def on_reject(payload: dict) -> None:
        rejects.append(payload)

    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.BACKTEST),
        ai_brain=_brain(),
        market_scanner=scanner,
        on_lifecycle_event=on_event,
        on_reject_persist=on_reject,
    )

    asyncio.run(orchestrator._scan_once())
    assert orchestrator.metrics.scans == 1
    assert orchestrator.metrics.rejects_persisted == 1
    assert orchestrator.metrics.rejects_persisted == 1
    assert rejects and events
    assert all("lifecycle_event_type" in evt for evt in events)
    assert any(evt["lifecycle_event_type"] == "SIGNAL_REJECTED" for evt in events)
    assert rejects[0].get("signal_id")
    assert rejects[0].get("reason") not in {"", "UNKNOWN"}


def test_rejected_signal_never_executes() -> None:
    async def scanner() -> list[dict]:
        return [{"symbol": "BTCUSDT", "entry": 100.0, "sl": 99.5, "tp": 100.8, "rr": 1.0, "side": "LONG", "volume_24h_usdt": 5_000_000, "spread_pct": 0.01, "volatility_pct": 2.0, "trend_strength": 0.4, "liquidity_score": 0.8, "chop_score": 0.3}]

    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.LIVE),
        ai_brain=_brain(),
        market_scanner=scanner,
        real_execution_adapter=None,
    )
    asyncio.run(orchestrator._scan_once())
    assert orchestrator.metrics.executions == 0


def test_shutdown_cancels_background_tasks() -> None:
    async def scanner() -> list[dict]:
        await asyncio.sleep(0.01)
        return []

    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.BACKTEST, scan_interval_sec=0.01, heartbeat_interval_sec=0.1),
        ai_brain=_brain(),
        market_scanner=scanner,
    )

    async def _run() -> None:
        task = asyncio.create_task(orchestrator.start())
        await asyncio.sleep(0.05)
        orchestrator.shutdown()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(_run())
    assert all(t.done() for t in orchestrator._tasks)


def test_invalid_lifecycle_transition_explicitly_marked_error() -> None:
    events: list[dict] = []
    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.BACKTEST),
        ai_brain=_brain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        on_lifecycle_event=lambda e: events.append(e),
    )
    asyncio.run(orchestrator._emit_lifecycle_event("ORDER_PLACED", "BTCUSDT", {}))
    assert events[-1]["lifecycle_event_type"] == "ERROR"


def test_runtime_risk_gate_rejects_stale_market_data() -> None:
    events: list[dict] = []
    rejects: list[dict] = []

    async def scanner() -> list[dict]:
        return [{"symbol": "BTCUSDT", "entry": 100.0, "sl": 99.5, "tp": 101.2, "rr": 2.0, "side": "LONG", "market_ts": 1.0, "volume_24h_usdt": 90_000_000, "spread_pct": 0.0002, "volatility_pct": 0.4, "trend_strength": 0.9, "liquidity_score": 0.9, "chop_score": 0.1}]

    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.BACKTEST, stale_market_data_sec=0.01),
        ai_brain=_AlwaysAcceptBrain(),
        market_scanner=scanner,
        on_lifecycle_event=lambda e: events.append(e),
        on_reject_persist=lambda r: rejects.append(r),
    )
    asyncio.run(orchestrator._scan_once())
    assert rejects
    assert rejects[0].get("signal_id")
    assert rejects[0].get("reason") == "STALE_MARKET_DATA"
    assert any(evt["lifecycle_event_type"] == "SIGNAL_REJECTED" for evt in events)


def test_runtime_final_reject_uses_canonical_effective_rr() -> None:
    rejects: list[dict] = []

    async def scanner() -> list[dict]:
        return [{"symbol": "BTCUSDT", "entry": 100.0, "sl": 99.0, "tp": 102.0, "rr": 2.0, "side": "LONG", "market_ts": 1.0, "spread_pct": 0.002, "expected_slippage_pct": 0.001, "latency_ms": 300.0, "liquidity_score": 0.5, "funding_rate_pct": 0.0005, "volume_24h_usdt": 90_000_000, "volatility_pct": 0.3, "trend_strength": 0.9, "chop_score": 0.1}]

    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.BACKTEST, stale_market_data_sec=0.01),
        ai_brain=_AlwaysAcceptBrain(),
        market_scanner=scanner,
        on_reject_persist=lambda r: rejects.append(r),
    )
    asyncio.run(orchestrator._scan_once())
    assert rejects
    assert rejects[0]["effective_rr"] < rejects[0]["rr"]


def test_runtime_exception_persists_diagnostic_error_lifecycle() -> None:
    events: list[dict] = []

    class _ExplodingBrain:
        def before_real_order(self, signal_payload, market_ctx, regime_ctx, stats_ctx):
            raise ValueError("decision pipeline blew up")

    async def scanner() -> list[dict]:
        return [{"symbol": "BTCUSDT", "entry": 100.0, "side": "LONG", "spread_pct": 0.0001, "funding_rate_pct": 0.0, "volume_24h_usdt": 95_000_000, "volatility_pct": 0.3, "trend_strength": 0.85, "liquidity_score": 0.9, "chop_score": 0.1}]

    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=_ExplodingBrain(),
        market_scanner=scanner,
        on_lifecycle_event=lambda e: events.append(e),
    )
    asyncio.run(orchestrator._scan_once())
    error_events = [evt for evt in events if evt["lifecycle_event_type"] == "ERROR"]
    assert error_events
    details = error_events[-1]["details"]
    assert "ValueError" in details.get("failure_reason", "")
    assert details.get("incident_payload", {}).get("exception_type") == "ValueError"
    assert details.get("incident_payload", {}).get("signal_id")


def test_runtime_rejected_decisions_do_not_persist_incomplete_real_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "runtime_rejects.sqlite3"
    monkeypatch.setenv("ALPHAFORGE_DB_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("EXECUTION_MODE", "BACKTEST")
    monkeypatch.setenv("ALPHAFORGE_RUNTIME_SAFE_SCANNER", "1")
    orchestrator = _build_runtime_from_env()
    asyncio.run(orchestrator._scan_once())

    engine = init_db(f"sqlite+pysqlite:///{db_path}")
    with Session(engine) as verify_session:
        rows = verify_session.execute(text("SELECT decision_id, signal_id, symbol, decision, reject_reason FROM order_decisions WHERE UPPER(decision)='REJECTED'")).all()
    assert rows
    assert all(str(row.signal_id or "").strip() for row in rows)
    assert all(str(row.symbol or "").strip() for row in rows)
    assert all(str(row.reject_reason or "").strip() for row in rows)
    assert not any(":real:" in str(row.decision_id) and (not str(row.symbol or "").strip() or not str(row.reject_reason or "").strip()) for row in rows)


def test_paper_runtime_rejected_rows_use_paper_mode_and_single_final_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "runtime_paper_rejects.sqlite3"
    monkeypatch.setenv("ALPHAFORGE_DB_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("EXECUTION_MODE", "PAPER")
    monkeypatch.setenv("ALPHAFORGE_RUNTIME_SAFE_SCANNER", "1")
    orchestrator = _build_runtime_from_env()
    asyncio.run(orchestrator._scan_once())

    engine = init_db(f"sqlite+pysqlite:///{db_path}")
    with Session(engine) as verify_session:
        runtime_rows = verify_session.execute(text("SELECT decision_id, mode, phase, signal_id, symbol, reject_reason, score, rr FROM order_decisions WHERE signal_id LIKE 'runtime:%' AND UPPER(decision)='REJECTED'")).all()
        final_count = verify_session.execute(text("SELECT COUNT(*) FROM order_decisions WHERE signal_id LIKE 'runtime:%' AND UPPER(decision)='REJECTED' AND COALESCE(phase,'final')='final'")).scalar_one()
    assert runtime_rows
    assert all(row.mode == "PAPER" for row in runtime_rows)
    assert all(str(row.signal_id or "").strip() and str(row.symbol or "").strip() and str(row.reject_reason or "").strip() for row in runtime_rows)
    assert any(row.phase == "final" and row.score is not None and row.rr is not None for row in runtime_rows)
    assert final_count == 1
    assert any((str(row.phase).startswith("ai_internal_")) for row in runtime_rows)


def test_reconciliation_event_on_timeout_like_execution_state() -> None:
    events: list[dict] = []

    class _Adapter:
        async def submit(self, decision, market_ctx):
            return {"status": "timeout", "order_id": "abc-1"}

    async def scanner() -> list[dict]:
        return [{"symbol": "ETHUSDT", "entry": 100.0, "sl": 99.0, "tp": 103.0, "rr": 3.0, "side": "LONG", "volume_24h_usdt": 90_000_000, "spread_pct": 0.0002, "volatility_pct": 0.4, "trend_strength": 0.9, "liquidity_score": 0.9, "chop_score": 0.1}]

    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.LIVE),
        ai_brain=_AlwaysAcceptBrain(),
        market_scanner=scanner,
        real_execution_adapter=_Adapter(),
        on_lifecycle_event=lambda e: events.append(e),
    )
    asyncio.run(orchestrator._scan_once())
    assert any(evt["lifecycle_event_type"] == "RECONCILIATION_REPAIR" for evt in events)


def test_live_start_blocks_placeholder_bootstrap_scanner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXECUTION_MODE", "LIVE")
    monkeypatch.setenv("ALPHAFORGE_REQUIRE_LIVE_QUALIFICATION", "0")
    monkeypatch.setenv("ALPHAFORGE_REQUIRE_EXCHANGE_CONNECTIVITY_FOR_LIVE", "0")
    monkeypatch.setenv("ALPHAFORGE_RUNTIME_SAFE_SCANNER", "1")
    orchestrator = _build_runtime_from_env()
    with pytest.raises(RuntimeError, match="exchange-backed market scanner is required"):
        asyncio.run(orchestrator.start())

def test_runtime_module_bootstrap_builds_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXECUTION_MODE", "paper")
    monkeypatch.setenv("ALPHAFORGE_SCAN_INTERVAL_SEC", "0.01")
    monkeypatch.setenv("ALPHAFORGE_HEARTBEAT_INTERVAL_SEC", "0.02")
    rt = _build_runtime_from_env()
    assert rt.config.execution_mode == ExecutionMode.PAPER
    assert rt.config.scan_interval_sec == pytest.approx(0.01)
    assert rt.config.heartbeat_interval_sec == pytest.approx(0.02)
    assert rt.metrics.persistence_enabled is True


def test_paper_bootstrap_initializes_schema_with_empty_cycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "paper.sqlite3"
    monkeypatch.setenv("EXECUTION_MODE", "paper")
    monkeypatch.setenv("ALPHAFORGE_DB_URL", f"sqlite+pysqlite:///{db_path}")
    rt = _build_runtime_from_env()
    assert rt.metrics.persistence_enabled is True
    assert rt.metrics.decisions_generated == 0
    with rt.ai_brain.session_factory().get_bind().connect() as conn:
        tables = {str(row[0]) for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert "signals" in tables
    assert "order_decisions" in tables
    assert "trade_lifecycle_events" in tables


def test_runtime_logs_absolute_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    db_path = tmp_path / "paper_runtime.sqlite3"
    monkeypatch.setenv("ALPHAFORGE_DB_URL", f"sqlite+pysqlite:///{db_path}")
    caplog.set_level(logging.INFO)
    _build_runtime_from_env()
    assert any("resolved_db_url=sqlite+pysqlite:///" in rec.message and str(db_path.resolve()) in rec.message for rec in caplog.records)


def test_symbols_selected_zero_records_gate_reasons() -> None:
    async def scanner() -> list[dict]:
        return [{"symbol": "BTCUSDT", "spread_pct": 999.0, "volume_24h_usdt": 0.0}]

    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=_brain(),
        market_scanner=scanner,
    )
    asyncio.run(orchestrator._scan_once())
    assert orchestrator.metrics.symbols_selected == 0
    assert orchestrator._last_scan_gate_blockers == ["NO_TRADABLE_SYMBOLS_AFTER_SELECTION"]
    assert orchestrator._last_scan_rejection_summary


def test_runtime_start_loop_does_not_exit_until_shutdown() -> None:
    async def scanner() -> list[dict]:
        await asyncio.sleep(0)
        return []

    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.BACKTEST, scan_interval_sec=0.01, heartbeat_interval_sec=0.01),
        ai_brain=_brain(),
        market_scanner=scanner,
    )

    async def _run() -> bool:
        task = asyncio.create_task(orchestrator.start())
        await asyncio.sleep(0.03)
        still_running = not task.done()
        orchestrator.shutdown()
        await asyncio.wait_for(task, timeout=1)
        return still_running

    assert asyncio.run(_run())


def test_runtime_signal_uses_dynamic_rr_not_fallback_when_present() -> None:
    selection = type("Sel", (), {"symbol": "BTCUSDT"})()
    payload = RuntimeOrchestrator._build_signal(selection, {"entry": 100.0, "rr": 3.25})
    assert payload["risk_reward"] == pytest.approx(3.25)


def test_paper_accept_path_uses_canonical_lifecycle_sequence() -> None:
    events: list[dict] = []

    async def scanner() -> list[dict]:
        return [{"symbol": "BTCUSDT", "entry": 100.0, "sl": 99.0, "tp": 103.0, "rr": 3.0, "side": "LONG", "market_ts": 99999999999.0, "volume_24h_usdt": 90_000_000, "spread_pct": 0.0002, "volatility_pct": 0.4, "trend_strength": 0.9, "liquidity_score": 0.9, "chop_score": 0.1}]

    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=_AlwaysAcceptBrain(),
        market_scanner=scanner,
        on_lifecycle_event=lambda e: events.append(e),
    )
    asyncio.run(orchestrator._scan_once())
    lifecycle = [e["lifecycle_event_type"] for e in events]
    assert lifecycle[0] == "SIGNAL_CREATED"
    assert "ORDER_PLACED" in lifecycle
    assert lifecycle[:4] == ["SIGNAL_CREATED", "WAITING_ENTRY_ZONE", "ENTRY_TRIGGERED", "ORDER_PLACED"]


def test_paper_reject_emits_signal_rejected_after_signal_created() -> None:
    events: list[dict] = []
    rejects: list[dict] = []

    async def scanner() -> list[dict]:
        return [{"symbol": "BTCUSDT", "entry": 100.0, "sl": 99.0, "tp": 101.0, "rr": 1.1, "side": "LONG", "market_ts": 9999999999.0, "volume_24h_usdt": 90_000_000, "spread_pct": 0.0001, "volatility_pct": 0.4, "trend_strength": 0.9, "liquidity_score": 0.9, "chop_score": 0.1}]

    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=_brain(),
        market_scanner=scanner,
        on_lifecycle_event=lambda e: events.append(e),
        on_reject_persist=lambda p: rejects.append(p),
    )
    asyncio.run(orchestrator._scan_once())
    assert rejects
    assert [events[0]["lifecycle_event_type"], events[1]["lifecycle_event_type"]] == ["SIGNAL_CREATED", "SIGNAL_REJECTED"]


def test_runtime_persistence_callback_fails_closed_on_lifecycle_write_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPHAFORGE_PERSISTENCE_ENABLED", "1")
    monkeypatch.setenv("EXECUTION_MODE", "paper")
    monkeypatch.setattr(persistence_module, "save_trade_lifecycle_event", lambda *args, **kwargs: False)
    orchestrator = _build_runtime_from_env()
    with pytest.raises(RuntimeError, match="trade_lifecycle_event_persistence_failed"):
        asyncio.run(orchestrator._emit_lifecycle_event("SIGNAL_CREATED", "BTCUSDT", {}))


def test_build_runtime_uses_exchange_scanner_for_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXECUTION_MODE", "PAPER")
    monkeypatch.setattr("alphaforge.runtime.scan_exchange_markets", lambda cfg: asyncio.sleep(0, result=[]))
    orchestrator = _build_runtime_from_env()
    assert orchestrator.market_scanner.__name__ == "_runtime_market_scanner"


def test_build_runtime_keeps_safe_scanner_for_backtest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXECUTION_MODE", "BACKTEST")
    monkeypatch.setattr("alphaforge.runtime.scan_exchange_markets", lambda cfg: asyncio.sleep(0, result=[]))
    orchestrator = _build_runtime_from_env()
    asyncio.run(orchestrator._scan_once())
    assert orchestrator.metrics.scans == 1


def test_live_start_blocks_safe_scanner_override_through_runtime_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXECUTION_MODE", "LIVE")
    monkeypatch.setenv("ALPHAFORGE_RUNTIME_SAFE_SCANNER", "1")
    orchestrator = _build_runtime_from_env()
    with pytest.raises(RuntimeError, match="LIVE mode blocked: exchange-backed market scanner is required"):
        asyncio.run(orchestrator.start())


def test_live_start_blocks_when_real_execution_adapter_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXECUTION_MODE", "LIVE")
    monkeypatch.setenv("ALPHAFORGE_RUNTIME_SAFE_SCANNER", "0")
    orchestrator = _build_runtime_from_env()
    with pytest.raises(RuntimeError, match="LIVE mode blocked: real execution adapter is not configured"):
        asyncio.run(orchestrator.start())


def test_build_runtime_assigns_deterministic_scanner_source_for_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXECUTION_MODE", "PAPER")
    monkeypatch.setenv("ALPHAFORGE_RUNTIME_SAFE_SCANNER", "0")
    orchestrator = _build_runtime_from_env()
    assert orchestrator.scanner_source == "EXCHANGE_PUBLIC_MARKET_DATA"


def test_build_runtime_assigns_safe_placeholder_scanner_source_when_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXECUTION_MODE", "PAPER")
    monkeypatch.setenv("ALPHAFORGE_RUNTIME_SAFE_SCANNER", "1")
    orchestrator = _build_runtime_from_env()
    assert orchestrator.scanner_source == "SAFE_PLACEHOLDER"


def test_live_start_blocks_unknown_scanner_provenance() -> None:
    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.LIVE, require_exchange_connectivity_for_live=False, require_live_qualification=False),
        ai_brain=_brain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        scanner_source="UNKNOWN",
        real_execution_adapter=object(),
    )
    with pytest.raises(RuntimeError, match="provenance is not verified"):
        asyncio.run(orchestrator.start())


def test_live_start_blocks_non_allowlisted_scanner_provenance() -> None:
    for source in ("SAFE_PLACEHOLDER", "MOCK", "OFFLINE", "SYNTHETIC", ""):
        orchestrator = RuntimeOrchestrator(
            config=RuntimeConfig(execution_mode=ExecutionMode.LIVE, require_exchange_connectivity_for_live=False, require_live_qualification=False),
            ai_brain=_brain(),
            market_scanner=lambda: asyncio.sleep(0, result=[]),
            scanner_source=source,
            real_execution_adapter=object(),
        )
        with pytest.raises(RuntimeError, match="exchange-backed market scanner is required|provenance is not verified"):
            asyncio.run(orchestrator.start())


def test_live_reconciliation_requires_provider() -> None:
    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.LIVE),
        ai_brain=_brain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
    )
    with pytest.raises(RuntimeError, match="reconciliation provider is not configured"):
        asyncio.run(orchestrator._reconcile_runtime_state())

class _StaticProvider:
    def __init__(self, payload):
        self._payload = payload

    def snapshot(self):
        return dict(self._payload)


def test_live_reconciliation_loop_fails_closed_on_incomplete_evidence() -> None:
    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.LIVE, reconciliation_timeout_sec=1.0),
        ai_brain=_brain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        real_execution_adapter=object(),
        live_reconciliation_provider=_StaticProvider({"evidence_status": "INCOMPLETE", "orders": [], "positions": [], "fills": []}),
    )
    with pytest.raises(RuntimeError, match="evidence incomplete"):
        asyncio.run(orchestrator._reconcile_runtime_state())
