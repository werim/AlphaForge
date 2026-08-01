from dataclasses import FrozenInstanceError
import asyncio
from copy import deepcopy
import pytest

from alphaforge.agents.contracts import *
from alphaforge.agents.orchestrator import *


def output(stage_input, status=DecisionStatus.PASS, *, hard=False, reason=""):
    return DecisionEnvelope(stage_input.decision_id, stage_input.correlation_id, stage_input.symbol,
        stage_input.execution_mode, stage_input.stage, status, reason, (reason,) if reason else (),
        {"hard_reject": hard}, stable_hash(stage_input.payload), stable_hash({"test": True}),
        "test", "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z", 0, stage_input.retry_count)


class Handler:
    def __init__(self, fn=None): self.fn = fn or (lambda value: output(value))
    def run(self, value): return self.fn(value)


def test_missing_handlers_skip_in_canonical_order_and_result_is_immutable():
    result = asyncio.run(ShadowAgentOrchestrator().run_shadow(decision_id="d", correlation_id="c",
        execution_mode="PAPER", legacy_decision={"decision": "ACCEPTED"}))
    assert tuple(item.stage for item in result.stage_results) == CANONICAL_STAGE_ORDER
    assert all(item.status is DecisionStatus.SKIPPED for item in result.stage_results)
    assert all(item.primary_reason == "STAGE_HANDLER_NOT_REGISTERED" for item in result.stage_results)
    with pytest.raises(FrozenInstanceError): result.shadow_only = False


def test_exception_and_timeout_become_errors():
    graph = ShadowAgentOrchestrator(AgentGraphConfig(stage_timeout_seconds=.01))
    graph.register_handler(AgentStage.MARKET, Handler(lambda _: (_ for _ in ()).throw(RuntimeError("boom"))))
    async def slow(value):
        await asyncio.sleep(.1)
        return output(value)
    graph.register_handler(AgentStage.SIGNAL, Handler(slow))
    result = asyncio.run(graph.run_shadow(decision_id="d", correlation_id="c", execution_mode="PAPER", legacy_decision={}))
    assert result.stage_results[0].primary_reason == "STAGE_HANDLER_EXCEPTION"
    assert result.stage_results[1].primary_reason == "STAGE_TIMEOUT"


def test_hard_reject_cannot_be_overridden_and_downstream_skips():
    graph = ShadowAgentOrchestrator()
    graph.register_handler(AgentStage.MARKET, Handler(lambda value: output(value, DecisionStatus.REJECT, hard=True, reason="HIGH_SPREAD")))
    graph.register_handler(AgentStage.SIGNAL, Handler())
    result = asyncio.run(graph.run_shadow(decision_id="d", correlation_id="c", execution_mode="PAPER", legacy_decision={}))
    assert result.status is DecisionStatus.REJECT
    assert result.stage_results[1].status is DecisionStatus.SKIPPED
    assert result.stage_results[1].primary_reason == "UPSTREAM_HARD_REJECT"


def test_max_steps_and_retry_bound_and_legacy_no_mutation():
    legacy = {"decision": "ACCEPTED", "nested": {"x": 1}}
    before = deepcopy(legacy)
    graph = ShadowAgentOrchestrator(AgentGraphConfig(max_graph_steps=1, max_reflection_retries=0))
    graph.register_handler(AgentStage.MARKET, Handler())
    graph.register_handler(AgentStage.SIGNAL, Handler())
    result = asyncio.run(graph.run_shadow(decision_id="d", correlation_id="c", execution_mode="PAPER",
        legacy_decision=legacy, context=legacy))
    assert result.stage_results[1].primary_reason == "MAX_GRAPH_STEPS_EXCEEDED"
    assert all(event.retry_count <= graph.config.max_reflection_retries for event in result.stage_results)
    assert legacy == before


def test_mutation_dependencies_and_mutating_handler_are_rejected():
    with pytest.raises(ValueError, match="MUTATION"):
        ShadowAgentOrchestrator(execution_adapter=object())
    class Mutator(Handler):
        def submit(self): pass
    with pytest.raises(ValueError, match="MUTATING"):
        ShadowAgentOrchestrator().register_handler(AgentStage.EXECUTION, Mutator())
