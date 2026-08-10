import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text

from alphaforge.agents.contracts import AgentStage, DecisionStatus, StageInput
from alphaforge.agents.orchestrator import ShadowAgentOrchestrator
from alphaforge.agents.persistence import AgentTraceRepository, bootstrap_agent_schema
from alphaforge.agents.phase_b import MarketAgent, SignalAgent, QualityAgent, register_phase_b_handlers


def stage(stage, payload, prior=()):
    return StageInput("d", "c", "PAPER", stage, "BTCUSDT", payload, prior)


def fixture(**overrides):
    base = {"symbol": "BTCUSDT", "timestamp": datetime.now(timezone.utc).isoformat(),
            "regime": "TREND", "atr_pct": 1.2, "trend_strength": .8,
            "spread_pct": .001, "expected_slippage_pct": .001, "liquidity_score": .9,
            "side": "LONG", "setup_type": "TREND_CONTINUATION", "entry": 100, "sl": 98,
            "tp": 105, "setup_quality": .8, "regime_alignment": .9, "expectancy_edge": .7,
            "momentum_confirmation": .8, "liquidity_quality": .9, "volatility_fit": .7,
            "risk_reward_quality": .8, "expectancy": .2, "decision": "REJECTED",
            "reject_reason": "DAILY_GLOBAL_TRADE_LIMIT"}
    return {**base, **overrides}


def test_market_is_deterministic_and_unavailable_is_none_not_zero():
    agent = MarketAgent()
    a = agent.run(stage(AgentStage.MARKET, fixture()))
    b = agent.run(stage(AgentStage.MARKET, fixture()))
    assert a.evidence["regime"] == b.evidence["regime"] == "TRENDING"
    assert a.evidence["funding"] is None
    assert a.evidence["availability"]["funding"] is False


def test_market_stale_defers_and_supported_regimes_differ():
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert MarketAgent().run(stage(AgentStage.MARKET, fixture(timestamp=stale))).status is DecisionStatus.DEFER
    assert MarketAgent().run(stage(AgentStage.MARKET, fixture(regime="RANGE"))).evidence["regime"] == "MEAN_REVERTING"


def test_signal_real_component_aggregation_varies_and_rr_uses_geometry():
    agent = SignalAgent()
    high = agent.run(stage(AgentStage.SIGNAL, fixture()))
    low = agent.run(stage(AgentStage.SIGNAL, fixture(setup_quality=.1, momentum_confirmation=.1)))
    weighted = sum(high.evidence["score_components"][k] * agent.WEIGHTS[k]
                   for k in high.evidence["score_components"])
    assert high.evidence["score"] == round(weighted / high.evidence["score_coverage"], 10)
    assert high.evidence["score"] != low.evidence["score"]
    assert high.evidence["score"] != .8
    assert high.evidence["raw_rr"] == 2.5
    assert agent.run(stage(AgentStage.SIGNAL, fixture(tp=106))).evidence["raw_rr"] == 3.0


def test_signal_invalid_and_no_candidate_are_explicit():
    invalid = SignalAgent().run(stage(AgentStage.SIGNAL, fixture(sl=101)))
    assert invalid.status is DecisionStatus.REJECT
    assert invalid.primary_reason == "INVALID_SIGNAL_GEOMETRY"
    empty = SignalAgent().run(stage(AgentStage.SIGNAL, {"symbol": "BTCUSDT"}))
    assert empty.status is DecisionStatus.DEFER
    assert empty.evidence["no_signal_reason"] == "NO_COMPLETE_SIGNAL_CANDIDATE"


def test_quality_preserves_legacy_hard_reject_and_parity():
    payload = fixture()
    signal = SignalAgent().run(stage(AgentStage.SIGNAL, payload))
    quality = QualityAgent().run(stage(AgentStage.QUALITY, payload, (signal,)))
    assert quality.status is DecisionStatus.REJECT
    assert "DAILY_GLOBAL_TRADE_LIMIT" in quality.evidence["all_reject_reasons"]
    assert quality.evidence["parity_status"] in {"MATCH", "PARTIAL_MATCH"}


def test_quality_low_score_and_unavailable_checks_are_concrete():
    payload = fixture(**{key: .1 for key in SignalAgent.WEIGHTS}, decision="ACCEPTED", reject_reason="")
    signal = SignalAgent().run(stage(AgentStage.SIGNAL, payload))
    quality = QualityAgent().run(stage(AgentStage.QUALITY, payload, (signal,)))
    assert quality.status is DecisionStatus.REJECT
    assert "LOW_SCORE" in quality.evidence["all_reject_reasons"]
    empty_signal = SignalAgent().run(stage(AgentStage.SIGNAL, {"symbol": "BTCUSDT"}))
    deferred = QualityAgent().run(stage(AgentStage.QUALITY, {}, (empty_signal,)))
    assert deferred.status is DecisionStatus.DEFER
    assert "spread_pct" in deferred.evidence["unavailable_checks"]


def test_phase_b_persistence_is_queryable_null_safe_and_duplicate_safe():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    bootstrap_agent_schema(engine)
    graph = ShadowAgentOrchestrator(persistence=AgentTraceRepository(engine))
    register_phase_b_handlers(graph)
    result = asyncio.run(graph.run_shadow(decision_id="d", correlation_id="c", execution_mode="PAPER",
        symbol="BTCUSDT", legacy_decision=fixture(), context=fixture()))
    graph.persist_result(result)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT regime,score,raw_rr,funding,parity_status FROM agent_phase_b_evidence")).one()
        assert row.regime == "TRENDING" and row.raw_rr == 2.5 and row.funding is None
        assert conn.execute(text("SELECT count(*) FROM agent_phase_b_evidence")).scalar_one() == 1


def test_parity_mismatch_and_unavailable_are_conservative():
    payload = fixture(decision="ACCEPTED", reject_reason="")
    signal = SignalAgent().run(stage(AgentStage.SIGNAL, payload))
    assert QualityAgent().run(stage(AgentStage.QUALITY, payload, (signal,))).evidence["parity_status"] == "MISMATCH"
    missing = QualityAgent().run(stage(AgentStage.QUALITY, {}, ()))
    assert missing.evidence["parity_status"] == "UNAVAILABLE"
