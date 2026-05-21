from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from alphaforge.ai_brain import AIBrain
from alphaforge.persistence import init_db
from alphaforge import persistence as persistence_module
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator, _build_runtime_from_env, execution_mode_from_env


def _brain() -> AIBrain:
    engine = init_db("sqlite+pysqlite:///:memory:")
    return AIBrain(Session(engine), min_accept_score=0.62)


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
    assert any(evt["lifecycle_event_type"] == "SIGNAL_REJECTED" for evt in events)


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
    with rt.ai_brain.session.get_bind().connect() as conn:
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
    lifecycle = [evt["lifecycle_event_type"] for evt in events]
    assert lifecycle[0] == "SIGNAL_CREATED"
    assert "WAITING_ENTRY_ZONE" in lifecycle
    assert "ENTRY_TRIGGERED" in lifecycle
    assert "ORDER_PLACED" in lifecycle


def test_runtime_persistence_callback_fails_closed_on_lifecycle_write_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPHAFORGE_PERSISTENCE_ENABLED", "1")
    monkeypatch.setenv("EXECUTION_MODE", "paper")
    monkeypatch.setattr(persistence_module, "save_trade_lifecycle_event", lambda *args, **kwargs: False)
    orchestrator = _build_runtime_from_env()
    with pytest.raises(RuntimeError, match="trade_lifecycle_event_persistence_failed"):
        asyncio.run(orchestrator._emit_lifecycle_event("SIGNAL_CREATED", "BTCUSDT", {}))
