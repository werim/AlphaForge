from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import inspect
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from alphaforge.ai_brain import AIBrain
from alphaforge.adaptive_learning import record_rejected_signal_review
from alphaforge.persistence import init_db
from alphaforge import persistence as persistence_module
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator, _build_runtime_from_env, execution_mode_from_env
from alphaforge.runtime_state import RuntimeStateSnapshot, evaluate_runtime_recovery, save_runtime_state_snapshot, latest_runtime_state_snapshot, build_readonly_reconciliation_probe, persist_verified_paper_recovery
from alphaforge.burnin_campaign import bootstrap_campaign_schema, create_campaign
import alphaforge.runtime as runtime_module


def _brain() -> AIBrain:
    engine = init_db("sqlite+pysqlite:///:memory:")
    return AIBrain(Session(engine), min_accept_score=0.62)


def test_runtime_startup_has_no_direct_exposure_table_sql() -> None:
    source = inspect.getsource(runtime_module)
    assert "FROM positions" not in source
    assert "FROM orders" not in source
    assert "load_active_positions(conn)" in source
    assert "load_pending_orders(conn)" in source




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


def test_attached_campaign_allowlist_bounds_and_deduplicates_before_selection(monkeypatch):
    candidates = [{"symbol": symbol, "source_exchange": "binance"} for symbol in
                  ("BTCUSDT", "ETHUSDT", "BCHUSDT", "XRPUSDT", "BNBUSDT", "BTCUSDT")]
    observed = []
    monkeypatch.setattr(runtime_module, "select_symbols", lambda rows, cfg: [
        SimpleNamespace(symbol=row["symbol"], tradable=True, reject_reasons=[],
                        diagnostics={"inputs": row}) for row in rows])
    async def process(self, selection):
        observed.append(selection.symbol)
    monkeypatch.setattr(RuntimeOrchestrator, "_process_symbol", process)
    orchestrator = RuntimeOrchestrator(RuntimeConfig(execution_mode=ExecutionMode.PAPER,
        max_symbols_per_scan=5), _brain(), lambda: asyncio.sleep(0, result=candidates))
    orchestrator._burnin_run_id = "run"
    orchestrator._campaign_symbols = frozenset({"BTCUSDT", "ETHUSDT"})
    orchestrator._campaign_source_exchanges = frozenset({"binance"})
    asyncio.run(orchestrator._scan_once())
    assert observed == ["BTCUSDT", "ETHUSDT"]
    assert orchestrator.metrics.symbols_selected == 2


def test_campaign_defense_in_depth_fails_before_processing():
    orchestrator = RuntimeOrchestrator(RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        _brain(), lambda: asyncio.sleep(0, result=[]))
    orchestrator._burnin_run_id = "run"
    orchestrator._campaign_symbols = frozenset({"BTCUSDT", "ETHUSDT"})
    orchestrator._campaign_source_exchanges = frozenset({"binance"})
    with pytest.raises(RuntimeError, match="CAMPAIGN_UNIVERSE_RUNTIME_MISMATCH"):
        orchestrator._assert_campaign_candidate("BCHUSDT", "binance", "BEFORE_PROCESS_SYMBOL")
    assert orchestrator.metrics.decisions_generated == 0
    assert orchestrator.metrics.rejects_persisted == 0
    assert orchestrator.metrics.executions == 0


def test_attached_binance_campaign_filters_same_symbol_hyperliquid_candidate(monkeypatch):
    observed = []
    candidates = [{"symbol": "BTCUSDT", "source_exchange": "hyperliquid"},
                  {"symbol": "BTCUSDT", "source_exchange": "binance"}]
    monkeypatch.setattr(runtime_module, "select_symbols", lambda rows, cfg: [
        SimpleNamespace(symbol=row["symbol"], tradable=True, reject_reasons=[],
                        diagnostics={"inputs": row}) for row in rows])
    async def process(self, selection):
        observed.append(selection.diagnostics["inputs"]["source_exchange"])
    monkeypatch.setattr(RuntimeOrchestrator, "_process_symbol", process)
    orchestrator = RuntimeOrchestrator(RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        _brain(), lambda: asyncio.sleep(0, result=candidates))
    orchestrator._burnin_run_id = "run"
    orchestrator._campaign_symbols = frozenset({"BTCUSDT"})
    orchestrator._campaign_source_exchanges = frozenset({"binance"})
    with orchestrator._resolve_persistence_engine().begin() as conn:
        bootstrap_campaign_schema(conn)
    asyncio.run(orchestrator._scan_once())
    assert observed == ["binance"]
    assert orchestrator.metrics.decisions_generated == 0
    assert orchestrator.metrics.rejects_persisted == 0
    assert orchestrator.metrics.burnin_observations == 0
    assert orchestrator.metrics.executions == 0
    with orchestrator._resolve_persistence_engine().connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM order_decisions")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM rejected_signal_reviews")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM burnin_observations")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM burnin_pending_reject_labels")).scalar_one() == 0


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


def test_campaign_promotion_failure_never_persists_operating_snapshot(monkeypatch, tmp_path: Path) -> None:
    import alphaforge.burnin_campaign as campaign_module

    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'promotion-failure.sqlite3'}")
    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=_brain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
    )
    orchestrator.persistence_engine = engine
    orchestrator._burnin_run_id = "campaign_run"
    monkeypatch.setenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID", "campaign")
    monkeypatch.setattr(RuntimeOrchestrator, "_load_recovery_state", lambda self: None)
    monkeypatch.setattr(RuntimeOrchestrator, "_attach_phase8_campaign", lambda self, *_: None)
    monkeypatch.setattr(RuntimeOrchestrator, "_start_or_resume_burnin_run", lambda self: None)

    async def reconciled() -> None:
        return None

    monkeypatch.setattr(RuntimeOrchestrator, "_run_reconciliation_once", lambda self: reconciled())
    monkeypatch.setattr(
        campaign_module, "mark_attached_campaign_operational",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("PHASE8_CAMPAIGN_OPERATIONAL_TRANSITION_DRIFT")),
    )

    with pytest.raises(RuntimeError, match="PHASE8_CAMPAIGN_OPERATIONAL_TRANSITION_DRIFT"):
        asyncio.run(orchestrator.start())

    with engine.connect() as conn:
        statuses = list(conn.execute(text("SELECT runtime_status FROM runtime_state_snapshots ORDER BY id")).scalars())
    # STARTUP persistence is conditional on the runtime persistence contract;
    # the safety boundary requires only that failed campaign promotion never
    # claims that the worker became operational.
    assert "OPERATING" not in statuses
    assert orchestrator._runtime_status != "OPERATING"
    assert orchestrator._tasks == []


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
        return [{"symbol": "BTCUSDT", "entry": 100.0, "sl": 99.5, "tp": 101.2, "rr": 2.0, "side": "LONG", "market_ts": 1.0, "volume_24h_usdt": 90_000_000, "spread_pct": 0.0002, "equity": 100000.0, "available_balance": 100000.0, "notional": 1000.0, "volatility_pct": 0.4, "trend_strength": 0.9, "liquidity_score": 0.9, "chop_score": 0.1}]

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


def test_eligible_paper_runtime_reject_creates_one_pending_label(tmp_path: Path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'pending.db'}")
    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER, reject_forward_horizon_bars=2),
        ai_brain=_brain(), market_scanner=lambda: None, persistence_engine=engine,
    )
    orchestrator._burnin_run_id = "paper-restart-safe-run"
    payload = {"signal_id":"eligible-1","symbol":"BTCUSDT","side":"LONG","timeframe":"1m","entry":100.0,"sl":90.0,"tp":120.0,
               "reason":"LOW_CONFIDENCE","regime":"TRENDING","setup_type":"BREAKOUT","volatility_regime":"NORMAL",
               "decision_timestamp":"2026-01-01T00:00:00Z","execution_ctx":{"spread_pct":.001,"expected_slippage_pct":.001,
               "fee_pct":.001,"funding_rate_pct":0.0,"market_data_latency_ms":10,"liquidity_score":.9}}
    asyncio.run(orchestrator._persist_reject(payload))
    asyncio.run(orchestrator._persist_reject(payload))
    with engine.connect() as conn:
        row = conn.execute(text("SELECT signal_id,regime,status,source_provenance_json FROM burnin_pending_reject_labels")).one()
        reviews = conn.execute(text("SELECT COUNT(*) FROM rejected_signal_reviews WHERE reject_decision_id='reject:eligible-1'")).scalar_one()
        observations = conn.execute(text("SELECT metrics_json,source_provenance_json FROM burnin_observations WHERE decision='REJECTED'")).all()
    assert row.signal_id == "eligible-1" and row.regime == "TRENDING" and row.status == "PENDING"
    assert 'BREAKOUT' in row.source_provenance_json and 'NORMAL' in row.source_provenance_json
    assert reviews == 1
    assert observations
    for metrics_json, provenance_json in observations:
        metrics = json.loads(metrics_json); provenance = json.loads(provenance_json)
        assert metrics["reject_decision_id"] == "reject:eligible-1"
        assert metrics["signal_id"] == "eligible-1"
        assert metrics["runtime_identity"] == "standalone:paper-restart-safe-run"
        assert provenance["runtime_identity"] == "standalone:paper-restart-safe-run"


def test_guided_null_candidate_separates_canonical_and_shadow_geometry(tmp_path: Path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'guided-shadow.db'}")
    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER, reject_forward_horizon_bars=1),
        ai_brain=_brain(), market_scanner=lambda: None, persistence_engine=engine,
    )
    orchestrator._burnin_run_id = "guided-shadow-run"
    source = {
        "signal_id": "guided-null", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1m",
        "entry": 100.0, "sl": 90.0, "tp": 112.0, "rr": 1.2, "effective_rr": 1.1,
        "setup_type": "LEGACY_BREAKOUT", "geometry_status": "COMPLETE",
        "geometry_source": "BINANCE_1M_KLINES", "reason": "MTF_EXECUTION_COUNTER_REGIME",
        "decision_timestamp": "2026-01-01T00:00:00Z",
        "execution_ctx": {"spread_pct": .001, "expected_slippage_pct": .001, "fee_pct": .001,
                          "funding_rate_pct": 0.0, "latency_ms": 10},
        "mtf": {"generation": {"mode": "REGIME_GUIDED", "candidate": None,
                                "evidence_status": "INCOMPLETE"},
                "setup": {"candidate_ready": False, "trade_side": None,
                          "structural_stop": None, "structural_target": None}},
    }

    canonical = orchestrator._canonical_reject_payload(source)
    assert canonical["forward_label_subject"] == "LEGACY_SCANNER_SHADOW_CANDIDATE"
    assert canonical["reject_quality_attributable"] is False
    assert canonical["rr"] is None and canonical["effective_rr"] is None
    assert canonical["side"] is None and canonical["entry"] is None
    assert canonical["sl"] is None and canonical["tp"] is None
    assert canonical["geometry_status"] == "UNAVAILABLE"
    assert canonical["legacy_shadow_geometry"]["rr"] == pytest.approx(1.2)
    assert canonical["legacy_shadow_geometry"]["geometry_status"] == "COMPLETE"
    assert canonical["legacy_shadow_geometry"]["attributable"] is False
    assert canonical["reason"] == "MTF_EXECUTION_COUNTER_REGIME"

    asyncio.run(orchestrator._persist_reject(source))
    with engine.connect() as conn:
        review = conn.execute(text("SELECT side,raw_rr,effective_rr,payload_json FROM rejected_signal_reviews WHERE reject_decision_id='reject:guided-null'")).one()
        pending = conn.execute(text("SELECT side,entry,stop,target,source_provenance_json FROM burnin_pending_reject_labels WHERE reject_decision_id='reject:guided-null'")).one()
    review_payload = json.loads(review.payload_json)
    provenance = json.loads(pending.source_provenance_json)
    assert review.side is None and review.raw_rr is None and review.effective_rr is None
    assert review_payload["geometry_status"] == "UNAVAILABLE"
    assert tuple(pending[:4]) == ("LONG", 100.0, 90.0, 112.0)
    assert provenance["forward_label_subject"] == "LEGACY_SCANNER_SHADOW_CANDIDATE"
    assert provenance["reject_quality_attributable"] is False


def test_real_guided_rejected_candidate_remains_attributable() -> None:
    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=_brain(), market_scanner=lambda: None,
    )
    payload = orchestrator._canonical_reject_payload({
        "signal_id": "guided-real", "symbol": "BTCUSDT", "side": "SHORT",
        "entry": 100.0, "sl": 105.0, "tp": 90.0, "rr": 2.0,
        "effective_rr": 1.8, "geometry_status": "COMPLETE", "reason": "LOW_EFFECTIVE_RR",
        "mtf": {"generation": {"mode": "REGIME_GUIDED", "evidence_status": "COMPLETE",
                                "candidate": {"side": "SHORT", "entry": 100.0,
                                              "sl": 105.0, "tp": 90.0, "rr": 2.0}}},
    })

    assert payload["forward_label_subject"] == "GUIDED_CANDIDATE"
    assert payload["rr"] == pytest.approx(2.0)
    assert payload["effective_rr"] == pytest.approx(1.8)
    assert payload["geometry_status"] == "COMPLETE"
    assert payload["reason"] == "LOW_EFFECTIVE_RR"
    assert "legacy_shadow_geometry" not in payload


def test_standalone_resolver_fetches_each_pending_timeframe(tmp_path: Path) -> None:
    engine=init_db(f"sqlite+pysqlite:///{tmp_path/'intervals.db'}"); seen=[]
    def provider(symbol,start,end,timeframe):
        seen.append(timeframe)
        seconds={'1m':60,'5m':300,'1h':3600}[timeframe]
        from datetime import datetime,timezone
        ts=datetime.fromtimestamp(datetime.fromisoformat(start.replace('Z','+00:00')).timestamp()+seconds,timezone.utc).isoformat()
        return [{'timestamp':ts,'high':121,'low':99}]
    orchestrator=RuntimeOrchestrator(config=RuntimeConfig(execution_mode=ExecutionMode.PAPER,reject_forward_horizon_bars=1),ai_brain=_brain(),market_scanner=lambda:None,persistence_engine=engine,reject_candle_provider=provider)
    orchestrator._burnin_run_id='interval-run'
    for tf in ('1m','5m','1h'):
        orchestrator._persist_pending_reject(orchestrator._canonical_reject_payload({'signal_id':tf,'symbol':'BTCUSDT','side':'LONG','timeframe':tf,'entry':100,'sl':90,'tp':120,'reason':'LOW_CONFIDENCE','decision_timestamp':'2026-01-01T00:00:00Z','execution_ctx':{'spread_pct':.001,'expected_slippage_pct':.001,'fee_pct':.001,'funding_rate_pct':0,'market_data_latency_ms':1}}))
    asyncio.run(orchestrator._resolve_reject_forward_outcomes_once())
    assert set(seen)=={'1m','5m','1h'}


def test_standalone_resolver_runs_after_pre_317_sqlite_upgrade_without_duplicates(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-resolver.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE positions(id INTEGER PRIMARY KEY AUTOINCREMENT,symbol TEXT,qty REAL,status TEXT)")
        conn.execute("CREATE TABLE orders(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id TEXT,symbol TEXT,status TEXT,created_at TEXT)")
        conn.execute("""CREATE TABLE burnin_pending_reject_labels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,pending_label_id TEXT NOT NULL UNIQUE,
            campaign_id TEXT NOT NULL,burnin_run_id TEXT NOT NULL,reject_decision_id TEXT NOT NULL UNIQUE,
            signal_id TEXT,symbol TEXT NOT NULL,side TEXT NOT NULL,decision_timestamp TEXT NOT NULL,
            entry REAL,stop REAL,target REAL,horizon_seconds REAL,execution_cost_assumptions_json TEXT NOT NULL,
            regime TEXT,reject_reason TEXT,source_provenance_json TEXT NOT NULL,due_at TEXT NOT NULL,
            status TEXT NOT NULL,evidence_complete INTEGER NOT NULL DEFAULT 0,last_error TEXT,
            created_at TEXT NOT NULL,resolved_at TEXT,schema_version TEXT NOT NULL)""")

    engine = init_db(f"sqlite+pysqlite:///{db_path}")
    seen = []
    def provider(symbol, start, end, timeframe):
        seen.append(timeframe)
        return [{"timestamp":"2026-01-01T00:01:00Z","high":121,"low":99}]
    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER,reject_forward_horizon_bars=1),
        ai_brain=_brain(),market_scanner=lambda:None,persistence_engine=engine,reject_candle_provider=provider,
    )
    orchestrator._burnin_run_id = "legacy-upgrade-run"
    campaign_id = orchestrator._reject_campaign_id()
    costs = '{"entry_slippage_cost":0.01,"exit_slippage_cost":0.01,"fee_cost":0.01,"funding_cost":0.0,"latency_cost":0.0,"spread_cost":0.01}'
    with engine.begin() as conn:
        conn.execute(text("""INSERT INTO burnin_pending_reject_labels (
            pending_label_id,campaign_id,burnin_run_id,reject_decision_id,signal_id,symbol,side,
            decision_timestamp,entry,stop,target,horizon_seconds,execution_cost_assumptions_json,
            regime,reject_reason,source_provenance_json,due_at,status,created_at,schema_version
        ) VALUES ('legacy',:cid,:run,'legacy','legacy','BTCUSDT','LONG','2026-01-01T00:00:00Z',
            100,90,120,60,:costs,'TRENDING','LOW_CONFIDENCE','{}','2026-01-01T00:01:00Z',
            'PENDING','2026-01-01T00:00:00Z','pre-317')"""), {"cid":campaign_id,"run":orchestrator._burnin_run_id,"costs":costs})
    payload = orchestrator._canonical_reject_payload({
        "signal_id":"new","symbol":"BTCUSDT","side":"LONG","timeframe":"1m","entry":100,
        "sl":90,"tp":120,"reason":"LOW_CONFIDENCE","decision_timestamp":"2026-01-01T00:00:00Z",
        "execution_ctx":{"spread_pct":.001,"expected_slippage_pct":.001,"fee_pct":.001,
                         "funding_rate_pct":0,"market_data_latency_ms":1},
    })
    orchestrator._persist_pending_reject(payload)

    asyncio.run(orchestrator._resolve_reject_forward_outcomes_once())
    restarted = RuntimeOrchestrator(
        config=orchestrator.config,ai_brain=_brain(),market_scanner=lambda:None,
        persistence_engine=engine,reject_candle_provider=provider,
    )
    restarted._burnin_run_id = orchestrator._burnin_run_id
    asyncio.run(restarted._resolve_reject_forward_outcomes_once())

    with engine.connect() as conn:
        legacy = conn.execute(text("SELECT timeframe,horizon_bars,horizon_seconds FROM burnin_pending_reject_labels WHERE reject_decision_id='legacy'")).one()
        new = conn.execute(text("SELECT timeframe,horizon_bars,horizon_seconds FROM burnin_pending_reject_labels WHERE reject_decision_id='reject:new'")).one()
        pending_count = conn.execute(text("SELECT COUNT(*) FROM burnin_pending_reject_labels")).scalar_one()
        outcome_count = conn.execute(text("SELECT COUNT(*) FROM burnin_reject_outcomes")).scalar_one()
    assert tuple(legacy) == (None,None,60.0)
    assert tuple(new) == ("1m",1,60.0)
    assert pending_count == 2 and outcome_count == 2
    assert None in seen and "1m" in seen


def test_reject_review_orphan_self_heals_to_one_pending_label_on_retry(tmp_path: Path) -> None:
    engine=init_db(f"sqlite+pysqlite:///{tmp_path/'recover.db'}")
    orchestrator=RuntimeOrchestrator(config=RuntimeConfig(execution_mode=ExecutionMode.PAPER,reject_forward_horizon_bars=1),ai_brain=_brain(),market_scanner=lambda:None,persistence_engine=engine)
    orchestrator._burnin_run_id='recover-run'
    payload=orchestrator._canonical_reject_payload({'signal_id':'recover','symbol':'BTCUSDT','side':'LONG','timeframe':'1m','entry':100,'sl':90,'tp':120,'reason':'LOW_CONFIDENCE','decision_timestamp':'2026-01-01T00:00:00Z','execution_ctx':{'spread_pct':.001,'expected_slippage_pct':.001,'fee_pct':.001,'funding_rate_pct':0,'market_data_latency_ms':1}})
    with engine.begin() as conn:
        assert record_rejected_signal_review(conn,reject_decision_id=payload['reject_decision_id'],signal_id='recover',symbol='BTCUSDT',side='LONG',reject_reason='LOW_CONFIDENCE',payload_json=payload)
    asyncio.run(orchestrator._persist_reject(payload))
    asyncio.run(orchestrator._persist_reject(payload))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM rejected_signal_reviews WHERE reject_decision_id='reject:recover'")).scalar_one()==1
        assert conn.execute(text("SELECT COUNT(*) FROM burnin_pending_reject_labels WHERE reject_decision_id='reject:recover'")).scalar_one()==1


def test_reconciliation_event_on_timeout_like_execution_state(monkeypatch) -> None:
    monkeypatch.setenv("ALPHAFORGE_ALLOW_LIVE_ORDERS", "true")
    events: list[dict] = []

    class _Adapter:
        async def submit(self, decision, market_ctx):
            return {"status": "timeout", "order_id": "abc-1"}

    async def scanner() -> list[dict]:
        return [{"symbol": "ETHUSDT", "entry": 100.0, "sl": 99.0, "tp": 103.0, "rr": 3.0, "side": "LONG", "volume_24h_usdt": 90_000_000, "spread_pct": 0.0002, "equity": 100000.0, "available_balance": 100000.0, "notional": 1000.0, "volatility_pct": 0.4, "trend_strength": 0.9, "liquidity_score": 0.9, "chop_score": 0.1}]

    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.LIVE, live_trading_enabled=True, allow_live_orders=True, operator_live_acknowledged=True),
        ai_brain=_AlwaysAcceptBrain(),
        market_scanner=scanner,
        real_execution_adapter=_Adapter(),
        on_lifecycle_event=lambda e: events.append(e),
    )
    orchestrator._qualification_report = type("Qualified", (), {"qualified": True, "verdict": "LIVE_READY"})()
    orchestrator._reconciliation_status = "CLEAN"
    asyncio.run(orchestrator._scan_once())
    assert any(evt["lifecycle_event_type"] == "RECONCILIATION_REPAIR" for evt in events)


def test_live_start_blocks_placeholder_bootstrap_scanner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "LIVE")
    monkeypatch.setenv("EXECUTION_MODE", "LIVE")
    monkeypatch.setenv("ALPHAFORGE_REQUIRE_LIVE_QUALIFICATION", "0")
    monkeypatch.setenv("ALPHAFORGE_REQUIRE_EXCHANGE_CONNECTIVITY_FOR_LIVE", "0")
    monkeypatch.setenv("ALPHAFORGE_RUNTIME_SAFE_SCANNER", "1")
    monkeypatch.setenv("ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION", "false")
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
    logging.disable(logging.NOTSET)
    runtime_logger = logging.getLogger("alphaforge.runtime")
    runtime_logger.disabled = False
    runtime_logger.propagate = True
    runtime_logger.setLevel(logging.INFO)
    db_path = tmp_path / "paper_runtime.sqlite3"
    monkeypatch.setenv("ALPHAFORGE_DB_URL", f"sqlite+pysqlite:///{db_path}")
    caplog.set_level(logging.INFO, logger="alphaforge.runtime")
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
        return [{"symbol": "BTCUSDT", "entry": 100.0, "sl": 99.0, "tp": 103.0, "rr": 3.0, "side": "LONG", "market_ts": 99999999999.0, "equity": 100000.0, "available_balance": 100000.0, "notional": 1000.0, "volume_24h_usdt": 90_000_000, "spread_pct": 0.0002, "equity": 100000.0, "available_balance": 100000.0, "notional": 1000.0, "volatility_pct": 0.4, "trend_strength": 0.9, "liquidity_score": 0.9, "chop_score": 0.1}]

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
        return [{"symbol": "BTCUSDT", "entry": 100.0, "sl": 99.0, "tp": 101.0, "rr": 1.1, "side": "LONG", "market_ts": 9999999999.0, "equity": 100000.0, "available_balance": 100000.0, "notional": 1000.0, "volume_24h_usdt": 90_000_000, "spread_pct": 0.0001, "volatility_pct": 0.4, "trend_strength": 0.9, "liquidity_score": 0.9, "chop_score": 0.1}]

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
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "LIVE")
    monkeypatch.setenv("EXECUTION_MODE", "LIVE")
    monkeypatch.setenv("ALPHAFORGE_RUNTIME_SAFE_SCANNER", "1")
    monkeypatch.setenv("ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION", "false")
    orchestrator = _build_runtime_from_env()
    with pytest.raises(RuntimeError, match="LIVE mode blocked: exchange-backed market scanner is required"):
        asyncio.run(orchestrator.start())


def test_live_start_phase6_disablement_precedes_real_adapter_requirement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPHAFORGE_EXECUTION_MODE", "LIVE")
    monkeypatch.setenv("EXECUTION_MODE", "LIVE")
    monkeypatch.setenv("ALPHAFORGE_RUNTIME_SAFE_SCANNER", "0")
    monkeypatch.setenv("ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION", "false")
    orchestrator = _build_runtime_from_env()
    with pytest.raises(RuntimeError, match="LIVE_REAL_ORDERS_DISABLED_IN_PHASE6"):
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


def test_complete_reconciliation_clears_only_stale_exchange_unknown_failure() -> None:
    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=_brain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        live_reconciliation_provider=_StaticProvider({"evidence_status": "COMPLETE", "orders": [], "positions": [], "fills": []}),
    )
    orchestrator._unknown_exchange_state = True
    orchestrator._fail_closed_reason = "EXCHANGE_STATE_UNKNOWN"
    orchestrator._reconciliation_status = "EXCHANGE_STATE_UNKNOWN"

    asyncio.run(orchestrator._reconcile_runtime_state())

    assert orchestrator._unknown_exchange_state is False
    assert orchestrator._fail_closed_reason is None
    assert orchestrator._reconciliation_status == "CLEAN"


def test_runtime_scan_refuses_new_work_when_persisted_kill_switch_on() -> None:
    from sqlalchemy import create_engine
    from alphaforge.runtime_control import RuntimeControlStore

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    store = RuntimeControlStore(engine)
    store.set_kill_switch(True, source="test")
    called = {"scanner": 0}

    async def scanner() -> list[dict]:
        called["scanner"] += 1
        return [{"symbol": "BTCUSDT", "volume_24h_usdt": 1000000.0}]

    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=_brain(),
        market_scanner=scanner,
        control_store=store,
    )
    asyncio.run(orchestrator._scan_once())
    assert called["scanner"] == 0
    assert orchestrator.metrics.scans == 0
    assert orchestrator._last_scan_gate_blockers == ["KILL_SWITCH_ACTIVE"]

class _ParityBrain:
    def before_real_order(self, signal_payload, market_ctx, regime_ctx, stats_ctx):
        score_ctx = self.score_signal(signal_payload, market_ctx, regime_ctx, stats_ctx)
        plan = self.choose_order_plan(signal_payload, market_ctx, score_ctx)
        return score_ctx, plan, self.explain_decision(signal_payload, score_ctx, plan)

    def score_signal(self, signal_payload, market_ctx, regime_ctx, stats_ctx):
        class _Score:
            total_score = 0.88
        return _Score()

    def choose_order_plan(self, signal_payload, market_ctx, score_ctx):
        class _Plan:
            decision = "ACCEPTED"
            reason = ""
            confidence = 0.88
            order_type = "MARKET"
            limit_price = None
            stop_price = None
        return _Plan()

    def explain_decision(self, signal_payload, score_ctx, order_plan):
        return "parity-test"

class _MutationTrapAdapter:
    def __init__(self) -> None:
        self.submit_calls = 0
        self.cancel_calls = 0
        self.modify_calls = 0

    async def submit(self, decision, market_ctx):
        self.submit_calls += 1
        raise AssertionError("LIVE_PRECHECK must not submit")

    async def cancel(self, *args, **kwargs):
        self.cancel_calls += 1
        raise AssertionError("LIVE_PRECHECK must not cancel")

    async def modify(self, *args, **kwargs):
        self.modify_calls += 1
        raise AssertionError("LIVE_PRECHECK must not modify")


def test_live_precheck_uses_paper_decision_pipeline_and_does_not_submit(tmp_path: Path) -> None:
    db_path = tmp_path / "live_precheck.sqlite3"
    engine = init_db(f"sqlite+pysqlite:///{db_path}")
    brain = AIBrain(Session(engine), min_accept_score=0.62)
    adapter = _MutationTrapAdapter()

    async def scanner() -> list[dict]:
        return [{"symbol": "BTCUSDT", "entry": 100.0, "sl": 99.0, "tp": 103.0, "rr": 3.0, "side": "LONG", "market_ts": 9999999999.0, "equity": 100000.0, "available_balance": 100000.0, "notional": 1000.0, "volume_24h_usdt": 90_000_000, "spread_pct": 0.0002, "equity": 100000.0, "available_balance": 100000.0, "notional": 1000.0, "volatility_pct": 0.4, "trend_strength": 0.9, "liquidity_score": 0.9, "chop_score": 0.1}]

    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.LIVE_PRECHECK),
        ai_brain=_ParityBrain(),
        market_scanner=scanner,
        real_execution_adapter=adapter,
        persistence_engine=engine,
    )
    asyncio.run(orchestrator._scan_once())

    assert adapter.submit_calls == 0
    assert adapter.cancel_calls == 0
    assert adapter.modify_calls == 0
    assert orchestrator.metrics.executions == 0
    with Session(engine) as session:
        row = session.execute(text("""
            SELECT mode, phase, no_submit_verified, parity_result, input_snapshot_hash, execution_ctx_missing, order_payload
            FROM order_decisions WHERE mode='LIVE_PRECHECK' AND phase='live_precheck'
        """)).mappings().one()
    payload = __import__("json").loads(row["order_payload"])
    assert row["no_submit_verified"] == 1
    assert row["parity_result"] == "PASS"
    assert row["input_snapshot_hash"]
    assert row["execution_ctx_missing"] == 0
    assert payload["paper"]["decision"] == payload["live_precheck"]["decision"]
    assert payload["mismatch_fields"] == []


def test_live_precheck_execute_path_is_no_submit_even_if_called_directly() -> None:
    adapter = _MutationTrapAdapter()
    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.LIVE_PRECHECK),
        ai_brain=_brain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        real_execution_adapter=adapter,
    )
    asyncio.run(orchestrator._execute("BTCUSDT", {"order_type": "MARKET"}, {"entry": 100.0}))
    assert adapter.submit_calls == 0
    assert orchestrator.metrics.executions == 1
    assert orchestrator._active_positions == {}


def test_recovery_scope_prevents_unrelated_paper_history_poisoning_and_keeps_live_strict(tmp_path: Path) -> None:
    """Production sequence: stale recovery history is audit-only when SQL is clean."""
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'recovery.sqlite3'}")
    with engine.begin() as conn:
        bootstrap_campaign_schema(conn)
        old_campaign = create_campaign(conn, release_id="old", duration_days=1, symbols=["BTCUSDT"], intervals=["1h"])
        conn.execute(text("UPDATE burnin_campaigns SET campaign_status='FAILED' WHERE campaign_id=:id"), {"id": old_campaign.campaign_id})
    save_runtime_state_snapshot(engine, RuntimeStateSnapshot(
        mode="PAPER", requested_mode="PAPER", actual_mode="PAPER", runtime_status="RECOVERY_REQUIRED",
        instance_id="old", startup_id="old-start", campaign_id=old_campaign.campaign_id, process_id=99999999,
        unknown_exchange_state=True, fail_closed_reason="EXCHANGE_RECONCILIATION_UNAVAILABLE",
    ))
    # A later successful reconciliation is authoritative current evidence.
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO exchange_reconciliation_events(event_ts,instance_id,startup_id,mode,status,mismatch_count,orphan_order_count,orphan_position_count,exchange_read_only_status,diagnostics_json) VALUES ('now','new','new','PAPER','CLEAN',0,0,0,'AVAILABLE','{}')"))
    paper = evaluate_runtime_recovery(engine, mode="PAPER", campaign_id="new-campaign")
    assert not paper["blocked"]
    assert paper["scope"] == "UNRELATED_HISTORICAL_RUNTIME"
    assert paper["current_exposure_check"] == {"active_positions": 0, "pending_orders": 0, "orphan_orders": 0, "orphan_positions": 0}
    assert evaluate_runtime_recovery(engine, mode="PAPER", campaign_id=old_campaign.campaign_id)["blocked"]
    assert evaluate_runtime_recovery(engine, mode="LIVE", campaign_id="new-campaign")["blocked"]


@pytest.mark.parametrize("table,sql", [
    ("positions", "INSERT INTO positions(position_id,symbol,qty,status) VALUES ('p','BTCUSDT',1,'OPEN')"),
])
def test_recovery_scope_blocks_authoritative_execution_exposure(tmp_path: Path, table: str, sql: str) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / (table + '.sqlite3')}")
    with engine.begin() as conn: conn.execute(text(sql))
    result = evaluate_runtime_recovery(engine, mode="PAPER", campaign_id="fresh")
    assert result["blocked"]
    assert result["scope"] == "GLOBAL_EXECUTION_RISK"


def test_verified_zero_exposure_paper_recovery_supersedes_unscoped_history(tmp_path: Path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'verified-recovery.sqlite3'}")
    save_runtime_state_snapshot(engine, RuntimeStateSnapshot(
        mode="PAPER", requested_mode="PAPER", actual_mode="PAPER", runtime_status="RECOVERY_REQUIRED",
        instance_id="old", startup_id="old", process_id=99999999, recovery_action_required=True,
    ))
    provider = type("Provider", (), {"snapshot": lambda self: {"evidence_status": "COMPLETE", "orders": [], "positions": [], "errors": []}})()
    decision = evaluate_runtime_recovery(engine, mode="PAPER", campaign_id="new", reconciliation_probe=build_readonly_reconciliation_probe(provider))
    assert decision["blocked"] is False and decision["reconciliation_probe_clean"] is True
    persist_verified_paper_recovery(engine, probe=decision["reconciliation_probe"], prior_snapshot=decision["latest"])
    latest = evaluate_runtime_recovery(engine, mode="PAPER", campaign_id="new")
    assert latest["blocked"] is False and latest["latest"]["runtime_status"] == "RECONCILED"


def test_same_campaign_unclean_paper_recovery_remains_blocked_with_clean_probe(tmp_path: Path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'same-campaign-clean-probe.sqlite3'}")
    save_runtime_state_snapshot(engine, RuntimeStateSnapshot(
        mode="PAPER", requested_mode="PAPER", actual_mode="PAPER", runtime_status="RECOVERY_REQUIRED",
        instance_id="old", startup_id="old", process_id=0, campaign_id="campaign",
        recovery_action_required=True,
    ))
    provider = type("Provider", (), {"snapshot": lambda self: {
        "evidence_status": "COMPLETE", "authenticated": True,
        "input_source": "AUTHENTICATED_EXCHANGE_SNAPSHOT", "orders": [], "positions": [], "errors": [],
    }})()

    decision = evaluate_runtime_recovery(
        engine, mode="PAPER", campaign_id="campaign",
        reconciliation_probe=build_readonly_reconciliation_probe(provider),
    )

    assert decision["reconciliation_probe_clean"] is True
    assert decision["blocked"] is True


def test_unavailable_reconciliation_provider_remains_fail_closed(tmp_path: Path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'missing-provider.sqlite3'}")
    save_runtime_state_snapshot(engine, RuntimeStateSnapshot(mode="PAPER", requested_mode="PAPER", actual_mode="PAPER", runtime_status="RECOVERY_REQUIRED", instance_id="old", startup_id="old"))
    result = evaluate_runtime_recovery(engine, mode="PAPER", campaign_id="new", reconciliation_probe=build_readonly_reconciliation_probe(None))
    assert result["blocked"] is True
    assert any("read_only_reconciliation_provider_unavailable" in e for e in result["query_errors"])


def test_synthetic_reconciled_snapshot_is_not_a_running_worker_and_real_runtime_supersedes_it(tmp_path: Path) -> None:
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'synthetic-recovery.sqlite3'}")
    persist_verified_paper_recovery(engine, probe={"evidence_status": "COMPLETE", "orders": [], "positions": [], "errors": []}, prior_snapshot=None)
    synthetic = latest_runtime_state_snapshot(engine)
    assert synthetic["runtime_status"] == "RECONCILED"
    assert synthetic["process_id"] == 0
    assert evaluate_runtime_recovery(engine, mode="PAPER", campaign_id="new")["previous_process_alive"] is False
    save_runtime_state_snapshot(engine, RuntimeStateSnapshot(
        mode="PAPER", requested_mode="PAPER", actual_mode="PAPER", runtime_status="OPERATING",
        instance_id="real", startup_id="real", process_id=0, campaign_id="new", burnin_run_id="new_run",
    ))
    latest = latest_runtime_state_snapshot(engine)
    assert latest["instance_id"] == "real" and latest["runtime_status"] == "OPERATING"
