import asyncio
from sqlalchemy import create_engine, inspect, text

from alphaforge.agents.orchestrator import AgentGraphConfig, ShadowAgentOrchestrator
from alphaforge.agents.persistence import AgentTraceRepository, bootstrap_agent_schema
from alphaforge.config import load_config_from_env


def run(graph, correlation="c1", legacy=None):
    return asyncio.run(graph.run_shadow(decision_id="d1", correlation_id=correlation,
        execution_mode="PAPER", symbol=None, legacy_decision=legacy or {"decision": "ACCEPTED"},
        context={"market_context": None}))


def test_schema_bootstrap_idempotent_and_trace_duplicate_safe_and_queryable():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    bootstrap_agent_schema(engine); bootstrap_agent_schema(engine)
    assert {"agent_runs", "agent_stage_events"}.issubset(inspect(engine).get_table_names())
    graph = ShadowAgentOrchestrator(persistence=AgentTraceRepository(engine))
    result = run(graph); graph.persist_result(result)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM agent_runs WHERE graph_status='PASS' AND shadow_only=1")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM agent_stage_events WHERE status='SKIPPED'")).scalar_one() == 8
        row = conn.execute(text("SELECT skipped_reason FROM agent_stage_events WHERE stage='MARKET'")).one()
        assert conn.execute(text("SELECT symbol FROM agent_runs")).scalar_one_or_none() is None
        assert row.skipped_reason == "STAGE_HANDLER_NOT_REGISTERED"


def test_persistence_failure_is_diagnostic_and_legacy_result_unchanged():
    class Broken:
        def persist_result(self, result): raise RuntimeError("disk unavailable")
    legacy = {"decision": "REJECTED", "reason": "HIGH_SPREAD"}
    result = run(ShadowAgentOrchestrator(persistence=Broken()), legacy=legacy)
    assert result.persistence_error == "RuntimeError:disk unavailable"
    assert legacy == {"decision": "REJECTED", "reason": "HIGH_SPREAD"}


def test_agent_graph_defaults_disabled_and_shadow(monkeypatch):
    for key in tuple(__import__('os').environ):
        if key.startswith("ALPHAFORGE_AGENT_GRAPH_"): monkeypatch.delenv(key, raising=False)
    config = load_config_from_env().runtime
    assert config.agent_graph_enabled is False
    assert config.agent_graph_shadow is True
