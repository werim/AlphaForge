"""Bounded shadow-only orchestration; this module has no execution imports."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import inspect
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from .contracts import (AgentStage, DecisionEnvelope, DecisionStatus, StageInput,
                        StageOutput, stable_hash, utc_now_iso)

CANONICAL_STAGE_ORDER = tuple(AgentStage)
ORCHESTRATOR_VERSION = "phase-a-1"


class AgentStageHandler(Protocol):
    def run(self, stage_input: StageInput) -> StageOutput | DecisionEnvelope: ...


@dataclass(frozen=True, slots=True)
class AgentGraphConfig:
    enabled: bool = False
    shadow_mode: bool = True
    max_graph_steps: int = 12
    max_reflection_retries: int = 1
    stage_timeout_seconds: float = 5.0
    persist_traces: bool = True

    def __post_init__(self) -> None:
        if not self.shadow_mode:
            raise ValueError("PHASE_A_REQUIRES_SHADOW_MODE")
        if self.max_graph_steps < 1 or self.max_reflection_retries < 0 or self.stage_timeout_seconds <= 0:
            raise ValueError("agent graph bounds must be positive (retries may be zero)")


@dataclass(frozen=True, slots=True)
class AgentGraphRunResult:
    correlation_id: str
    started_at: str
    completed_at: str
    status: DecisionStatus
    stage_results: tuple[DecisionEnvelope, ...]
    halted_stage: AgentStage | None
    halt_reason: str | None
    legacy_decision_reference: str | None
    shadow_only: bool = True
    persistence_error: str | None = None


class ShadowAgentOrchestrator:
    def __init__(self, config: AgentGraphConfig | None = None, *, persistence: Any = None,
                 execution_adapter: Any = None, mutating_callback: Any = None) -> None:
        if execution_adapter is not None or mutating_callback is not None:
            raise ValueError("PHASE_A_MUTATION_DEPENDENCY_FORBIDDEN")
        self.config = config or AgentGraphConfig()
        self._handlers: dict[AgentStage, AgentStageHandler] = {}
        self._persistence = persistence

    def register_handler(self, stage: AgentStage, handler: AgentStageHandler) -> None:
        stage = AgentStage(stage)
        if any(hasattr(handler, name) for name in ("submit", "cancel_order", "modify_order", "simulate_order")):
            raise ValueError("MUTATING_AGENT_HANDLER_FORBIDDEN")
        if not callable(getattr(handler, "run", None)):
            raise TypeError("handler must define run(stage_input)")
        self._handlers[stage] = handler

    def validate_graph(self) -> tuple[AgentStage, ...]:
        if set(self._handlers) - set(CANONICAL_STAGE_ORDER):
            raise ValueError("NON_CANONICAL_STAGE")
        return CANONICAL_STAGE_ORDER

    async def _invoke(self, handler: AgentStageHandler, stage_input: StageInput) -> DecisionEnvelope:
        result = handler.run(stage_input)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, StageOutput):
            result = result.envelope
        if not isinstance(result, DecisionEnvelope):
            raise TypeError("handler must return StageOutput or DecisionEnvelope")
        if result.stage is not stage_input.stage:
            raise ValueError("HANDLER_STAGE_MISMATCH")
        return result

    @staticmethod
    def _envelope(stage_input: StageInput, status: DecisionStatus, reason: str, started: str,
                  *, evidence: Mapping[str, Any] | None = None) -> DecisionEnvelope:
        completed = utc_now_iso()
        start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(completed.replace("Z", "+00:00"))
        return DecisionEnvelope(
            decision_id=stage_input.decision_id, correlation_id=stage_input.correlation_id,
            symbol=stage_input.symbol, execution_mode=stage_input.execution_mode, stage=stage_input.stage,
            status=status, primary_reason=reason, reason_codes=(reason,), evidence=evidence or {},
            input_hash=stable_hash(stage_input.payload), config_hash=stable_hash({"shadow": True}),
            agent_version=ORCHESTRATOR_VERSION, started_at=started, completed_at=completed,
            duration_ms=max(0.0, (end_dt - start_dt).total_seconds() * 1000),
            retry_count=stage_input.retry_count, skipped_reason=reason if status is DecisionStatus.SKIPPED else None,
        )

    async def run_shadow(self, *, decision_id: str, correlation_id: str, execution_mode: str,
                         legacy_decision: Any, symbol: str | None = None,
                         context: Mapping[str, Any] | None = None) -> AgentGraphRunResult:
        if not self.config.shadow_mode:
            raise RuntimeError("PHASE_A_REQUIRES_SHADOW_MODE")
        self.validate_graph()
        started_at = utc_now_iso()
        # Deep-copy is the boundary: handlers never receive the authoritative object.
        snapshot = deepcopy(dict(context or {}))
        legacy_reference = stable_hash(deepcopy(legacy_decision)) if legacy_decision is not None else None
        results: list[DecisionEnvelope] = []
        hard_reject = False
        halt_reason: str | None = None
        halted_stage: AgentStage | None = None
        steps = 0
        for stage in CANONICAL_STAGE_ORDER:
            stage_input = StageInput(decision_id, correlation_id, execution_mode, stage, symbol,
                                     MappingProxyType(snapshot), tuple(results), 0)
            stage_started = utc_now_iso()
            if steps >= self.config.max_graph_steps:
                result = self._envelope(stage_input, DecisionStatus.SKIPPED, "MAX_GRAPH_STEPS_EXCEEDED", stage_started)
                halt_reason, halted_stage = "MAX_GRAPH_STEPS_EXCEEDED", stage
            elif hard_reject:
                result = self._envelope(stage_input, DecisionStatus.SKIPPED, "UPSTREAM_HARD_REJECT", stage_started)
            elif stage not in self._handlers:
                result = self._envelope(stage_input, DecisionStatus.SKIPPED, "STAGE_HANDLER_NOT_REGISTERED", stage_started)
            else:
                steps += 1
                try:
                    result = await asyncio.wait_for(self._invoke(self._handlers[stage], stage_input),
                                                    timeout=self.config.stage_timeout_seconds)
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    result = self._envelope(stage_input, DecisionStatus.ERROR, "STAGE_TIMEOUT", stage_started)
                except Exception as exc:
                    result = self._envelope(stage_input, DecisionStatus.ERROR, "STAGE_HANDLER_EXCEPTION", stage_started,
                                            evidence={"exception_type": type(exc).__name__, "message": str(exc)})
            hard_reject = hard_reject or result.hard_reject
            results.append(result)
        completed_at = utc_now_iso()
        status = DecisionStatus.REJECT if hard_reject else (DecisionStatus.ERROR if any(r.status is DecisionStatus.ERROR for r in results) else DecisionStatus.PASS)
        run = AgentGraphRunResult(correlation_id, started_at, completed_at, status, tuple(results),
                                  halted_stage, halt_reason, legacy_reference, True)
        return self.persist_result(run)

    def persist_result(self, result: AgentGraphRunResult) -> AgentGraphRunResult:
        if not self.config.persist_traces or self._persistence is None:
            return result
        try:
            self._persistence.persist_result(result)
        except Exception as exc:  # diagnostics only: legacy authority is outside this graph
            return AgentGraphRunResult(result.correlation_id, result.started_at, result.completed_at,
                                       result.status, result.stage_results, result.halted_stage,
                                       result.halt_reason, result.legacy_decision_reference, True,
                                       f"{type(exc).__name__}:{exc}")
        return result
