from __future__ import annotations

import asyncio
import contextlib
from collections import deque
import logging
import signal
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Protocol

from alphaforge.ai_brain import AIBrain
from alphaforge.contracts import LifecycleEventType, canonical_reject_reason, canonical_utc_timestamp, validate_transition
from alphaforge.execution import build_execution_context
from alphaforge.symbol_selector import SymbolSelectionResult, select_symbols

logger = logging.getLogger(__name__)


class ExecutionMode(str, Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


class RealExecutionAdapter(Protocol):
    async def submit(self, decision: Mapping[str, Any], market_ctx: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(slots=True)
class RuntimeConfig:
    execution_mode: ExecutionMode = ExecutionMode.BACKTEST
    scan_interval_sec: float = 1.0
    heartbeat_interval_sec: float = 30.0
    max_symbols_per_scan: int = 5
    max_reject_log_entries: int = 1000


@dataclass(slots=True)
class RuntimeMetrics:
    scans: int = 0
    symbols_selected: int = 0
    decisions_generated: int = 0
    rejects_persisted: int = 0
    executions: int = 0
    lifecycle_events: int = 0
    last_heartbeat_ts: float = 0.0


@dataclass(slots=True)
class RuntimeOrchestrator:
    config: RuntimeConfig
    ai_brain: AIBrain
    market_scanner: Callable[[], Awaitable[list[dict[str, Any]]]]
    real_execution_adapter: RealExecutionAdapter | None = None
    on_lifecycle_event: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None
    on_reject_persist: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None
    paper_slippage_bps: float = 2.0
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _tasks: list[asyncio.Task[Any]] = field(default_factory=list, init=False)
    _reject_log: deque[dict[str, Any]] = field(init=False)
    metrics: RuntimeMetrics = field(default_factory=RuntimeMetrics, init=False)
    _last_lifecycle_state_by_symbol: dict[str, str] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._reject_log = deque(maxlen=max(1, self.config.max_reject_log_entries))

    async def start(self) -> None:
        self._register_signals()
        self._tasks = [
            asyncio.create_task(self._market_scan_loop(), name="market_scan_loop"),
            asyncio.create_task(self._heartbeat_loop(), name="metrics_heartbeat"),
        ]
        for task in self._tasks:
            task.add_done_callback(self._on_task_done)
        try:
            await self._stop_event.wait()
        finally:
            await self._shutdown_tasks()

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            exc = task.exception()
            if exc is not None:
                logger.exception("runtime_task_failed task=%s", task.get_name(), exc_info=exc)
                self.shutdown()

    def shutdown(self) -> None:
        self._stop_event.set()

    async def _market_scan_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                started = time.time()
                await self._scan_once()
                elapsed = time.time() - started
                await asyncio.sleep(max(0.0, self.config.scan_interval_sec - elapsed))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("market_scan_loop_failed")
            self.shutdown()

    async def _scan_once(self) -> None:
        self.metrics.scans += 1
        candidates = await self.market_scanner()
        selected = select_symbols(candidates)[: self.config.max_symbols_per_scan]
        self.metrics.symbols_selected += len(selected)

        for symbol_result in selected:
            await self._process_symbol(symbol_result)

    async def _process_symbol(self, selection: SymbolSelectionResult) -> None:
        market_ctx = dict(selection.diagnostics.get("inputs", {}))
        signal_payload = self._build_signal(selection, market_ctx)
        regime_ctx = {"alignment": 0.8 if selection.regime_hint != "UNFAVORABLE" else 0.3}
        stats_ctx: dict[str, Any] = {}

        score_ctx, order_plan, explanation = await asyncio.to_thread(
            self.ai_brain.before_real_order,
            signal_payload,
            market_ctx,
            regime_ctx,
            stats_ctx,
        )
        self.metrics.decisions_generated += 1

        if order_plan.decision != "ACCEPTED":
            await self._persist_reject({
                "symbol": selection.symbol,
                "decision": order_plan.decision,
                "reason": canonical_reject_reason(order_plan.reason),
                "confidence": order_plan.confidence,
                "explanation": explanation,
            })
            await self._emit_lifecycle_event(LifecycleEventType.SIGNAL_CREATED.value, selection.symbol, {"reason": ""})
            await self._emit_lifecycle_event(LifecycleEventType.SIGNAL_REJECTED.value, selection.symbol, {"reason": canonical_reject_reason(order_plan.reason)})
            return

        await self._emit_lifecycle_event(LifecycleEventType.SIGNAL_CREATED.value, selection.symbol, {"reason": ""})
        await self._emit_lifecycle_event(LifecycleEventType.WAITING_ENTRY_ZONE.value, selection.symbol, {})
        await self._emit_lifecycle_event(LifecycleEventType.ENTRY_TRIGGERED.value, selection.symbol, {})
        await self._execute(symbol=selection.symbol, decision={
            "order_type": order_plan.order_type,
            "limit_price": order_plan.limit_price,
            "stop_price": order_plan.stop_price,
            "confidence": order_plan.confidence,
        }, market_ctx=market_ctx)

    async def _execute(self, symbol: str, decision: dict[str, Any], market_ctx: Mapping[str, Any]) -> None:
        mode = self.config.execution_mode
        if mode == ExecutionMode.PAPER:
            result = self._simulate_paper_execution(symbol, decision, market_ctx)
        elif mode == ExecutionMode.LIVE:
            if self.real_execution_adapter is None:
                raise RuntimeError("LIVE mode requires real_execution_adapter")
            result = await self.real_execution_adapter.submit(decision, market_ctx)
        else:
            result = {"mode": mode.value, "status": "simulated", "symbol": symbol}

        self.metrics.executions += 1
        await self._emit_lifecycle_event(LifecycleEventType.ORDER_PLACED.value, symbol, {"decision": decision, "result": dict(result)})

    def _simulate_paper_execution(self, symbol: str, decision: Mapping[str, Any], market_ctx: Mapping[str, Any]) -> dict[str, Any]:
        entry = float(market_ctx.get("entry", 0.0) or 0.0)
        slip = self.paper_slippage_bps / 10_000.0
        side = str(market_ctx.get("side", "LONG"))
        fill = entry * (1 + slip) if side.upper() == "LONG" else entry * (1 - slip)
        return {
            "mode": ExecutionMode.PAPER.value,
            "symbol": symbol,
            "status": "filled",
            "order_type": decision.get("order_type", "MARKET"),
            "expected_slippage_pct": slip,
            "fill_price": round(fill, 8),
        }

    async def _persist_reject(self, payload: dict[str, Any]) -> None:
        self._reject_log.append(payload)
        self.metrics.rejects_persisted += 1
        if self.on_reject_persist is not None:
            maybe_coro = self.on_reject_persist(payload)
            if asyncio.iscoroutine(maybe_coro):
                await maybe_coro

    async def _emit_lifecycle_event(self, event: str, symbol: str, details: Mapping[str, Any] | None = None) -> None:
        previous_state = self._last_lifecycle_state_by_symbol.get(symbol)
        lifecycle_state = event if validate_transition(previous_state, event) else LifecycleEventType.ERROR.value
        event_payload = {
            "lifecycle_event_type": lifecycle_state,
            "lifecycle_state": lifecycle_state,
            "symbol": symbol,
            "timestamp": canonical_utc_timestamp(),
            "mode": self.config.execution_mode.value,
            "previous_lifecycle_state": previous_state,
            "details": dict(details or {}),
        }
        self._last_lifecycle_state_by_symbol[symbol] = lifecycle_state
        self.metrics.lifecycle_events += 1
        if self.on_lifecycle_event is not None:
            maybe_coro = self.on_lifecycle_event(event_payload)
            if asyncio.iscoroutine(maybe_coro):
                await maybe_coro

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                self.metrics.last_heartbeat_ts = time.time()
                logger.info("runtime_heartbeat=%s", self.metrics)
                await asyncio.sleep(self.config.heartbeat_interval_sec)
        except asyncio.CancelledError:
            raise

    async def _shutdown_tasks(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _register_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.shutdown)

    @staticmethod
    def _build_signal(selection: SymbolSelectionResult, market_ctx: Mapping[str, Any]) -> dict[str, Any]:
        execution_ctx = build_execution_context(market_ctx)
        rr = float(market_ctx.get("rr", 2.0) or 2.0)
        return {
            "symbol": selection.symbol,
            "side": market_ctx.get("side", "LONG"),
            "timeframe": market_ctx.get("timeframe", "1m"),
            "entry_price": float(market_ctx.get("entry", 0.0) or 0.0),
            "risk_reward": rr,
            "max_spread_bps": 12.0,
            "max_funding_rate": 0.0008,
            "max_expected_slippage_pct": execution_ctx.get("expected_slippage_pct", 0.002) * 1.2,
            "execution_ctx": execution_ctx,
        }


def execution_mode_from_env(raw_mode: str | None) -> ExecutionMode:
    mode = str(raw_mode or "BACKTEST").upper().strip()
    try:
        return ExecutionMode(mode)
    except ValueError as exc:
        raise ValueError(f"Unsupported EXECUTION_MODE={raw_mode!r}. Expected BACKTEST/PAPER/LIVE") from exc
