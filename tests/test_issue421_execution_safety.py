from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from alphaforge.ai_brain import AIBrain, OrderPlan, ScoreContext
from alphaforge.execution import build_execution_context, evaluate_execution_safety
from alphaforge.persistence import (
    init_db,
    save_order_decision,
    save_rejected_decision_artifact,
    save_trade_lifecycle_event,
)
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


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


def _execution_ctx(**overrides: Any) -> dict[str, Any]:
    base = {
        "spread_pct": 0.0002,
        "spread_status": "MEASURED",
        "spread_source": "BOOK_TICKER",
        "expected_slippage_pct": 0.0002,
        "slippage_status": "MODEL_ESTIMATE",
        "slippage_source": "CONFIGURED_PAPER_ASSUMPTION",
        "latency_ms": 50.0,
        "latency_status": "MODEL_ESTIMATE",
        "latency_source": "CONFIGURED_PAPER_ASSUMPTION",
        "liquidity_score": 0.90,
        "liquidity_status": "MEASURED",
        "liquidity_source": "FIXTURE",
        "funding_rate_pct": 0.00005,
        "funding_status": "MEASURED",
        "funding_source": "FIXTURE",
        "orderbook_imbalance": 0.10,
        "orderbook_status": "MEASURED",
        "orderbook_source": "FIXTURE",
        "volatility_regime": "normal",
        "volatility_status": "MEASURED",
        "volatility_source": "FIXTURE",
        "fee_pct": 0.0004,
        "fee_status": "CONFIGURED",
        "fee_source": "FIXTURE",
    }
    base.update(overrides)
    return base


def _thresholds(**overrides: Any) -> dict[str, Any]:
    base = {
        "MAX_SPREAD_PCT": 0.0025,
        "MAX_EXPECTED_SLIPPAGE_PCT": 0.0020,
        "MAX_TOTAL_COST_PCT": 0.20,
        "MIN_LIQUIDITY_SCORE": 0.30,
        "MAX_VOLATILITY_PENALTY_PCT": 0.20,
        "REJECT_UNKNOWN_EXECUTION_CONTEXT": True,
        "MAX_LATENCY_MS": 2500,
        "MAX_ABS_FUNDING_RATE_PCT": 0.0010,
        "ENABLE_ORDERBOOK_FILTER": False,
    }
    base.update(overrides)
    return base


def test_none_volatility_value_cannot_be_promoted_to_measured_string_evidence() -> None:
    ctx = build_execution_context({
        "spread_pct": 0.0002,
        "spread_status": "MEASURED",
        "expected_slippage_pct": 0.0002,
        "slippage_status": "MODEL_ESTIMATE",
        "latency_ms": 50.0,
        "latency_status": "MODEL_ESTIMATE",
        "liquidity_score": 0.9,
        "liquidity_status": "MEASURED",
        "funding_rate_pct": 0.00005,
        "funding_status": "MEASURED",
        "volatility_regime": None,
        "volatility_status": "MEASURED",
    })
    assert ctx["volatility_regime"] is None

    result = evaluate_execution_safety(
        ctx,
        effective_rr=9.0,
        min_effective_rr=1.10,
        thresholds=_thresholds(),
    )
    assert result["accepted"] is False
    assert result["primary_reject_reason"] == "EXECUTION_CONTEXT_UNAVAILABLE"
    assert "volatility_regime" in result["missing_fields"]


def test_execution_safety_complete_context_passes_without_recomputing_geometry() -> None:
    result = evaluate_execution_safety(
        _execution_ctx(),
        effective_rr=1.25,
        min_effective_rr=1.10,
        thresholds=_thresholds(),
    )
    assert result["accepted"] is True
    assert result["primary_reject_reason"] is None
    assert result["effective_rr"] == pytest.approx(1.25)
    assert result["all_failed_gates"] == []


@pytest.mark.parametrize(
    ("changes", "effective_rr", "expected_gate"),
    [
        ({"spread_pct": None, "spread_status": "UNAVAILABLE"}, 9.0, "EXECUTION_CONTEXT_UNAVAILABLE"),
        ({"expected_slippage_pct": None, "slippage_status": "UNAVAILABLE"}, 9.0, "EXECUTION_CONTEXT_UNAVAILABLE"),
        ({"latency_ms": None, "latency_status": "UNAVAILABLE"}, 9.0, "EXECUTION_CONTEXT_UNAVAILABLE"),
        ({"liquidity_score": None, "liquidity_status": "UNAVAILABLE"}, 9.0, "EXECUTION_CONTEXT_UNAVAILABLE"),
        ({"spread_pct": 0.0030}, 9.0, "SPREAD_TOO_HIGH"),
        ({"expected_slippage_pct": 0.0030}, 9.0, "SLIPPAGE_TOO_HIGH"),
        ({"fee_pct": 0.1997}, 9.0, "HIGH_TOTAL_COST"),
        ({"liquidity_score": 0.10}, 9.0, "THIN_LIQUIDITY"),
        ({"latency_ms": 3000.0}, 9.0, "HIGH_LATENCY"),
        ({"volatility_regime": "extreme"}, 9.0, "EXCESSIVE_VOLATILITY"),
        ({"funding_rate_pct": 0.0020}, 9.0, "FUNDING_TOO_HIGH"),
        ({}, 1.00, "LOW_EFFECTIVE_RR"),
    ],
)
def test_each_protected_execution_gate_independently_kills_acceptance(
    changes: dict[str, Any],
    effective_rr: float,
    expected_gate: str,
) -> None:
    result = evaluate_execution_safety(
        _execution_ctx(**changes),
        effective_rr=effective_rr,
        min_effective_rr=1.10,
        thresholds=_thresholds(),
    )
    assert result["accepted"] is False
    assert expected_gate in result["all_failed_gates"]
    assert any(
        row["gate"] == expected_gate and row["source"] == "EXECUTION_SAFETY_CONTRACT"
        for row in result["failed_gate_evidence"]
    )


@pytest.mark.parametrize(
    ("changes", "missing_field"),
    [
        (
            {
                "funding_rate_pct": None,
                "funding_status": "UNAVAILABLE",
                "funding_source": "UNAVAILABLE",
            },
            "funding_rate_pct",
        ),
        (
            {
                "volatility_regime": None,
                "volatility_status": "UNAVAILABLE",
                "volatility_source": "UNAVAILABLE",
            },
            "volatility_regime",
        ),
    ],
)
def test_unknown_funding_or_volatility_is_fail_closed(
    changes: dict[str, Any],
    missing_field: str,
) -> None:
    result = evaluate_execution_safety(
        _execution_ctx(**changes),
        effective_rr=9.0,
        min_effective_rr=1.10,
        thresholds=_thresholds(),
    )
    assert result["accepted"] is False
    assert result["primary_reject_reason"] == "EXECUTION_CONTEXT_UNAVAILABLE"
    assert missing_field in result["missing_fields"]


def test_disabled_orderbook_filter_does_not_poison_active_execution_evidence_status() -> None:
    ctx = _execution_ctx(
        orderbook_imbalance=None,
        orderbook_status="UNAVAILABLE",
        orderbook_source="UNAVAILABLE",
    )
    result = evaluate_execution_safety(
        ctx,
        effective_rr=1.25,
        min_effective_rr=1.10,
        thresholds=_thresholds(ENABLE_ORDERBOOK_FILTER=False),
    )
    assert result["accepted"] is True
    assert result["execution_evidence_status"] == "PARTIAL_ESTIMATED"
    assert result["raw_execution_evidence_status"] == "UNAVAILABLE_BLOCKING"

    required = evaluate_execution_safety(
        ctx,
        effective_rr=1.25,
        min_effective_rr=1.10,
        thresholds=_thresholds(ENABLE_ORDERBOOK_FILTER=True),
    )
    assert required["accepted"] is False
    assert required["primary_reject_reason"] == "EXECUTION_CONTEXT_UNAVAILABLE"
    assert "orderbook_imbalance" in required["missing_fields"]


def test_live_precheck_requires_measured_execution_evidence() -> None:
    result = evaluate_execution_safety(
        _execution_ctx(),
        effective_rr=9.0,
        min_effective_rr=1.10,
        thresholds=_thresholds(),
        require_measured=True,
    )
    assert result["accepted"] is False
    assert result["primary_reject_reason"] == "EXECUTION_CONTEXT_UNAVAILABLE"
    assert {"expected_slippage_pct", "latency_ms"} <= set(result["missing_fields"])


def _market(**overrides: Any) -> dict[str, Any]:
    base = {
        "symbol": "BTCUSDT",
        "source_exchange": "fixture",
        "timeframe": "1m",
        "entry": 100.0,
        "sl": 99.0,
        "tp": 103.0,
        "rr": 3.0,
        "side": "LONG",
        "market_ts": time.time(),
        "volume_24h_usdt": 90_000_000.0,
        "spread_pct": 0.0002,
        "spread_status": "MEASURED",
        "expected_slippage_pct": 0.0002,
        "slippage_status": "MODEL_ESTIMATE",
        "liquidity_score": 0.90,
        "liquidity_status": "MEASURED",
        "funding_rate_pct": 0.00005,
        "funding_status": "MEASURED",
        "orderbook_imbalance": 0.10,
        "orderbook_status": "MEASURED",
        "volatility_regime": "normal",
        "volatility_status": "MEASURED",
        "trend_strength": 0.90,
        "chop_score": 0.10,
    }
    base.update(overrides)
    return base


def _run_paper(
    market: dict[str, Any],
    *,
    config: RuntimeConfig | None = None,
    paper_slippage_bps: float | None = 2.0,
) -> tuple[RuntimeOrchestrator, list[dict[str, Any]]]:
    rejects: list[dict[str, Any]] = []
    orchestrator = RuntimeOrchestrator(
        config=config or RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=_AlwaysAcceptBrain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        on_reject_persist=lambda payload: rejects.append(payload),
        paper_slippage_bps=paper_slippage_bps,
    )
    selection = SimpleNamespace(
        symbol=str(market["symbol"]),
        diagnostics={"inputs": market},
    )
    asyncio.run(orchestrator._process_symbol(selection))
    return orchestrator, rejects


@pytest.mark.parametrize(
    ("market_changes", "config", "paper_slippage_bps", "expected_gate"),
    [
        (
            {"spread_pct": None, "spread_status": "UNAVAILABLE", "rr": 10.0},
            RuntimeConfig(execution_mode=ExecutionMode.PAPER),
            2.0,
            "EXECUTION_CONTEXT_UNAVAILABLE",
        ),
        (
            {"expected_slippage_pct": None, "slippage_status": "UNAVAILABLE", "rr": 10.0},
            RuntimeConfig(execution_mode=ExecutionMode.PAPER),
            None,
            "EXECUTION_CONTEXT_UNAVAILABLE",
        ),
        (
            {"rr": 10.0},
            RuntimeConfig(execution_mode=ExecutionMode.PAPER, paper_execution_latency_ms=None),
            2.0,
            "EXECUTION_CONTEXT_UNAVAILABLE",
        ),
        (
            {"liquidity_score": None, "liquidity_status": "UNAVAILABLE", "rr": 10.0},
            RuntimeConfig(execution_mode=ExecutionMode.PAPER),
            2.0,
            "EXECUTION_CONTEXT_UNAVAILABLE",
        ),
        (
            {"fee_pct": 0.1997, "fee_status": "CONFIGURED"},
            RuntimeConfig(execution_mode=ExecutionMode.PAPER, paper_fee_bps=None),
            2.0,
            "HIGH_TOTAL_COST",
        ),
        (
            {"liquidity_score": 0.10},
            RuntimeConfig(execution_mode=ExecutionMode.PAPER),
            2.0,
            "THIN_LIQUIDITY",
        ),
        (
            {"volatility_regime": "extreme"},
            RuntimeConfig(execution_mode=ExecutionMode.PAPER),
            2.0,
            "EXCESSIVE_VOLATILITY",
        ),
        (
            {},
            RuntimeConfig(execution_mode=ExecutionMode.PAPER, paper_execution_latency_ms=3000.0),
            2.0,
            "HIGH_LATENCY",
        ),
    ],
)
def test_paper_process_symbol_enforces_each_execution_safety_family(
    market_changes: dict[str, Any],
    config: RuntimeConfig,
    paper_slippage_bps: float | None,
    expected_gate: str,
) -> None:
    runtime, rejects = _run_paper(
        _market(**market_changes),
        config=config,
        paper_slippage_bps=paper_slippage_bps,
    )
    assert runtime.metrics.executions == 0
    assert rejects
    reject = rejects[-1]
    assert expected_gate in reject["all_failed_gates"]
    assert reject["execution_safety"]["accepted"] is False
    assert any(
        row["gate"] == expected_gate
        and row.get("source") == "EXECUTION_SAFETY_CONTRACT"
        for row in reject["failed_gate_evidence"]
    )


def test_live_precheck_runtime_rejects_modelled_execution_evidence_before_submit() -> None:
    rejects: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    market = _market(
        latency_ms=50.0,
        latency_status="MODEL_ESTIMATE",
        latency_source="EXPLICIT_EXECUTION_LATENCY",
        expected_slippage_pct=0.0002,
        slippage_status="MODEL_ESTIMATE",
        slippage_source="MODEL",
    )
    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.LIVE_PRECHECK),
        ai_brain=_AlwaysAcceptBrain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        on_reject_persist=lambda payload: rejects.append(payload),
        on_lifecycle_event=lambda event: events.append(event),
    )
    selection = SimpleNamespace(
        symbol=str(market["symbol"]),
        diagnostics={"inputs": market},
    )

    asyncio.run(orchestrator._process_symbol(selection))

    assert orchestrator.metrics.executions == 0
    assert rejects
    reject = rejects[-1]
    assert reject["reason"] == "EXECUTION_CONTEXT_UNAVAILABLE"
    assert {
        "expected_slippage_pct",
        "latency_ms",
    } <= set(reject["execution_safety"]["missing_fields"])
    assert "ORDER_PLACED" not in {
        event.get("lifecycle_event_type")
        for event in events
    }


def test_execution_safety_reject_happens_before_ai_brain_is_called() -> None:
    class _CountingBrain:
        def __init__(self) -> None:
            self.calls = 0

        def before_real_order(self, signal_payload, market_ctx, regime_ctx, stats_ctx):
            self.calls += 1
            raise AssertionError("unsafe execution context reached AIBrain")

    rejects: list[dict[str, Any]] = []
    brain = _CountingBrain()
    market = _market(
        rr=25.0,
        liquidity_score=None,
        liquidity_status="UNAVAILABLE",
    )
    orchestrator = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=brain,
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        on_reject_persist=lambda payload: rejects.append(payload),
        paper_slippage_bps=2.0,
    )
    asyncio.run(orchestrator._process_symbol(SimpleNamespace(
        symbol="BTCUSDT",
        diagnostics={"inputs": market},
    )))

    assert brain.calls == 0
    assert orchestrator.metrics.decisions_generated == 0
    assert orchestrator.metrics.executions == 0
    assert rejects[-1]["reason"] == "EXECUTION_CONTEXT_UNAVAILABLE"


def test_high_raw_rr_cannot_bypass_unknown_execution_context() -> None:
    runtime, rejects = _run_paper(
        _market(
            rr=25.0,
            liquidity_score=None,
            liquidity_status="UNAVAILABLE",
        )
    )
    assert runtime.metrics.executions == 0
    assert rejects[-1]["reason"] == "EXECUTION_CONTEXT_UNAVAILABLE"
    assert "EXECUTION_CONTEXT_UNAVAILABLE" in rejects[-1]["all_failed_gates"]


def test_multi_gate_execution_reject_persists_all_failed_threshold_evidence() -> None:
    config = RuntimeConfig(
        execution_mode=ExecutionMode.PAPER,
        paper_execution_latency_ms=3000.0,
    )
    runtime, rejects = _run_paper(
        _market(
            spread_pct=0.01,
            liquidity_score=0.10,
            funding_rate_pct=0.01,
            volatility_regime="extreme",
        ),
        config=config,
    )
    assert runtime.metrics.executions == 0
    reject = rejects[-1]
    expected = {
        "SPREAD_TOO_HIGH",
        "THIN_LIQUIDITY",
        "HIGH_LATENCY",
        "EXCESSIVE_VOLATILITY",
        "FUNDING_TOO_HIGH",
    }
    assert expected <= set(reject["all_failed_gates"])
    evidence = {
        row["gate"]: row
        for row in reject["failed_gate_evidence"]
        if row.get("source") == "EXECUTION_SAFETY_CONTRACT"
    }
    assert expected <= set(evidence)
    for gate in expected:
        assert evidence[gate]["observed"] is not None
        assert evidence[gate]["threshold"] is not None


def test_persistence_uses_active_safety_status_for_execution_ctx_missing() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    blocking = {
        "evidence_status": "UNAVAILABLE_BLOCKING",
        "spread_pct": None,
    }
    active_safe = {
        "evidence_status": "UNAVAILABLE_BLOCKING",
        "safety_evidence_status": "PARTIAL_ESTIMATED",
        "spread_pct": 0.0002,
    }

    with Session(engine) as session:
        save_order_decision(
            session,
            decision_id="blocking-decision",
            signal_id="blocking-signal",
            symbol="BTCUSDT",
            mode="PAPER",
            phase="final",
            decision="REJECTED",
            reject_reason="EXECUTION_CONTEXT_UNAVAILABLE",
            execution_ctx=blocking,
        )
        save_order_decision(
            session,
            decision_id="active-safe-decision",
            signal_id="active-safe-signal",
            symbol="ETHUSDT",
            mode="PAPER",
            phase="final",
            decision="ACCEPTED",
            execution_ctx=active_safe,
        )
        save_trade_lifecycle_event(
            session,
            event_id="blocking-event",
            signal_id="blocking-signal",
            symbol="BTCUSDT",
            mode="PAPER",
            lifecycle_state="SIGNAL_CREATED",
            execution_ctx=blocking,
            event_ts="2026-09-23T00:00:00Z",
        )

    with engine.connect() as conn:
        rows = {
            row.decision_id: row.execution_ctx_missing
            for row in conn.execute(text(
                "SELECT decision_id, execution_ctx_missing "
                "FROM order_decisions "
                "WHERE decision_id IN ('blocking-decision','active-safe-decision')"
            ))
        }
        lifecycle_missing = conn.execute(text(
            "SELECT execution_ctx_missing FROM trade_lifecycle_events "
            "WHERE event_id='blocking-event'"
        )).scalar_one()

    assert rows["blocking-decision"] == 1
    assert rows["active-safe-decision"] == 0
    assert lifecycle_missing == 1


def test_aibrain_internal_audit_persists_canonical_effective_rr_and_execution_latency() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    session = Session(engine)
    brain = AIBrain(session)
    score = ScoreContext(
        total_score=0.9,
        expectancy_edge=0.7,
        components={"momentum_confirmation": 0.9},
        penalties={},
        accepted=True,
        reason_flags=[],
        probabilistic={"p_win": 0.6},
    )
    plan = OrderPlan(
        decision="ACCEPTED",
        order_type="LIMIT",
        limit_price=100.0,
        stop_price=None,
        confidence=0.9,
        reason="fixture",
    )
    execution_ctx = _execution_ctx(
        evidence_status="UNAVAILABLE_BLOCKING",
        safety_evidence_status="PARTIAL_ESTIMATED",
        safety_missing_fields=[],
        market_data_latency_ms=1500.0,
        latency_ms=50.0,
    )
    signal = {
        "signal_id": "ai-audit-signal",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "timeframe": "1m",
        "mode": "PAPER",
        "entry_price": 100.0,
        "risk_reward": 2.0,
    }
    market = {
        "mode": "PAPER",
        "market_ts": 1_790_000_000.0,
        "effective_rr": 1.234,
        "execution_ctx": execution_ctx,
    }

    brain._persist_decision(signal, market, score, plan, "fixture", "real")

    with engine.connect() as conn:
        decision = conn.execute(text(
            "SELECT rr, effective_rr, latency_ms, execution_ctx_missing, execution_ctx "
            "FROM order_decisions WHERE signal_id='ai-audit-signal' "
            "AND phase='ai_internal_real'"
        )).mappings().one()
        signal_row = conn.execute(text(
            "SELECT rr, effective_rr FROM signals WHERE signal_id='ai-audit-signal'"
        )).mappings().one()
        features = conn.execute(text(
            "SELECT execution_features FROM ai_decision_features "
            "WHERE decision_id IN (SELECT decision_id FROM order_decisions "
            "WHERE signal_id='ai-audit-signal' AND phase='ai_internal_real')"
        )).scalar_one()

    persisted_ctx = json.loads(decision["execution_ctx"])
    persisted_features = json.loads(features)
    assert decision["rr"] == pytest.approx(2.0)
    assert decision["effective_rr"] == pytest.approx(1.234)
    assert decision["latency_ms"] == pytest.approx(50.0)
    assert decision["execution_ctx_missing"] == 0
    assert persisted_ctx["market_data_latency_ms"] == pytest.approx(1500.0)
    assert persisted_ctx["latency_ms"] == pytest.approx(50.0)
    assert signal_row["effective_rr"] == pytest.approx(1.234)
    assert persisted_features["latency_ms"] == pytest.approx(50.0)
    assert persisted_features["safety_evidence_status"] == "PARTIAL_ESTIMATED"
    session.close()


def test_rejected_artifact_never_promotes_market_data_rtt_to_execution_latency() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    execution_ctx = _execution_ctx(
        evidence_status="PARTIAL_ESTIMATED",
        safety_evidence_status="PARTIAL_ESTIMATED",
        market_data_latency_ms=1500.0,
        latency_ms=50.0,
    )

    with Session(engine) as session:
        artifact = save_rejected_decision_artifact(
            session,
            decision_id="reject-latency-contract",
            event_id="reject-latency-event",
            signal_id="reject-latency-signal",
            symbol="BTCUSDT",
            side="LONG",
            timeframe="1m",
            mode="PAPER",
            reason="HIGH_LATENCY",
            raw_rr=2.0,
            effective_rr=1.2,
            execution_ctx=execution_ctx,
            event_ts="2026-09-23T00:00:01Z",
        )
        assert artifact is not None

    with engine.connect() as conn:
        latency = conn.execute(text(
            "SELECT latency_ms FROM order_decisions "
            "WHERE decision_id='reject-latency-contract'"
        )).scalar_one()

    assert latency == pytest.approx(50.0)


def test_runtime_decision_evidence_persists_execution_safety_snapshot() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=_AlwaysAcceptBrain(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
        persistence_engine=engine,
        scanner_source="FIXTURE",
    )
    runtime._start_or_resume_burnin_run()
    assert runtime._burnin_run_id

    execution_ctx = _execution_ctx(
        evidence_status="UNAVAILABLE_BLOCKING",
        safety_evidence_status="UNAVAILABLE_BLOCKING",
        safety_missing_fields=["liquidity_score"],
        safety_fake_zero_fields=[],
        safety_all_failed_gates=["EXECUTION_CONTEXT_UNAVAILABLE"],
        unavailable_fields=["liquidity_score"],
        liquidity_score=None,
        liquidity_status="UNAVAILABLE",
        total_explicit_cost_pct=0.00085,
        volatility_penalty_pct=0.0,
    )
    runtime._persist_burnin_decision(
        {
            "signal_id": "runtime-safety-evidence",
            "symbol": "BTCUSDT",
            "source_exchange": "fixture",
            "decision": "REJECTED",
            "reason": "EXECUTION_CONTEXT_UNAVAILABLE",
            "primary_reject_reason": "EXECUTION_CONTEXT_UNAVAILABLE",
            "reject_reasons": ["EXECUTION_CONTEXT_UNAVAILABLE"],
            "all_failed_gates": ["EXECUTION_CONTEXT_UNAVAILABLE"],
            "failed_gate_evidence": [
                {
                    "gate": "EXECUTION_CONTEXT_UNAVAILABLE",
                    "observed": ["liquidity_score"],
                    "threshold": "AVAILABLE",
                    "comparison": "required",
                    "source": "EXECUTION_SAFETY_CONTRACT",
                }
            ],
            "decision_timestamp": "2026-09-23T00:00:02Z",
            "side": "LONG",
            "entry": 100.0,
            "sl": 99.0,
            "tp": 102.0,
            "rr": 2.0,
            "executable_raw_rr": 1.8,
            "remaining_execution_penalty": 0.2,
            "effective_rr": 1.6,
            "execution_ctx": execution_ctx,
        },
        lifecycle_state="SIGNAL_REJECTED",
    )

    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT unavailable_fields, latency_ms, funding_rate_pct, "
            "volatility_penalty_pct, total_explicit_cost_pct, reject_reason "
            "FROM decision_evidence WHERE signal_id='runtime-safety-evidence'"
        )).mappings().one()

    assert json.loads(row["unavailable_fields"]) == ["liquidity_score"]
    assert row["latency_ms"] == pytest.approx(50.0)
    assert row["funding_rate_pct"] == pytest.approx(0.00005)
    assert row["volatility_penalty_pct"] == pytest.approx(0.0)
    assert row["total_explicit_cost_pct"] == pytest.approx(0.00085)
    assert row["reject_reason"] == "EXECUTION_CONTEXT_UNAVAILABLE"


def test_aibrain_internal_audit_does_not_fabricate_effective_rr_when_missing() -> None:
    engine = init_db("sqlite+pysqlite:///:memory:")
    session = Session(engine)
    brain = AIBrain(session)
    score = ScoreContext(
        total_score=0.9,
        expectancy_edge=0.7,
        components={},
        penalties={},
        accepted=True,
        reason_flags=[],
        probabilistic={},
    )
    plan = OrderPlan(
        decision="ACCEPTED",
        order_type="LIMIT",
        limit_price=100.0,
        stop_price=None,
        confidence=0.9,
        reason="fixture",
    )
    execution_ctx = _execution_ctx(
        evidence_status="PARTIAL_ESTIMATED",
        safety_evidence_status="PARTIAL_ESTIMATED",
        safety_missing_fields=[],
    )
    brain._persist_decision(
        {
            "signal_id": "ai-missing-effective-rr",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "timeframe": "1m",
            "mode": "PAPER",
            "entry_price": 100.0,
            "risk_reward": 2.0,
        },
        {
            "mode": "PAPER",
            "market_ts": 1_790_000_001.0,
            "execution_ctx": execution_ctx,
        },
        score,
        plan,
        "fixture",
        "real",
    )

    with engine.connect() as conn:
        decision_effective = conn.execute(text(
            "SELECT effective_rr FROM order_decisions "
            "WHERE signal_id='ai-missing-effective-rr' AND phase='ai_internal_real'"
        )).scalar_one()
        signal_effective = conn.execute(text(
            "SELECT effective_rr FROM signals WHERE signal_id='ai-missing-effective-rr'"
        )).scalar_one()

    assert decision_effective is None
    assert signal_effective is None
    session.close()


def test_mode_parity_cannot_treat_unavailable_blocking_context_as_complete() -> None:
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(execution_mode=ExecutionMode.PAPER),
        ai_brain=AIBrain.for_stateless_scoring(),
        market_scanner=lambda: asyncio.sleep(0, result=[]),
    )

    parity = runtime._build_mode_parity_evidence(min_sample_count=3)

    assert parity["sample_count"] == 3
    assert parity["execution_context_complete"] is False
    assert any(
        sample["execution_context"]["evidence_status"] == "UNAVAILABLE_BLOCKING"
        for sample in parity["samples"]
    )
