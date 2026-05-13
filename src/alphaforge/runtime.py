from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
import inspect
import logging
import os
import signal
from typing import Any, Awaitable, Callable, Mapping

from alphaforge.execution import build_execution_context
from alphaforge.symbol_selector import SymbolSelectionResult, select_symbols

logger = logging.getLogger(__name__)


class ExecutionMode(str, Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"
    SIMULATED = "SIMULATED"


@dataclass
class RuntimeConfig:
    execution_mode: ExecutionMode
    scan_interval_sec: float
    heartbeat_interval_sec: float
    scan_timeout_sec: float
    max_symbols_per_scan: int
    max_reject_log_entries: int
    require_market_context: bool = True


@dataclass
class RuntimeMetrics:
    scans: int = 0
    scan_failures: int = 0
    scan_timeouts: int = 0
    symbols_selected: int = 0
    signals_created: int = 0
    decisions_generated: int = 0
    rejects_persisted: int = 0
    orders_placed: int = 0
    executions: int = 0
    lifecycle_events: int = 0
    last_heartbeat_ts: str | None = None


@dataclass(frozen=True)
class SimulatedOrderPlan:
    decision: str
    order_type: str
    reason: str


class SimulatedAIBrain:
    def before_real_order(self, signal: Mapping[str, Any], market_ctx: Mapping[str, Any], regime_ctx: Mapping[str, Any], stats_ctx: Mapping[str, Any]):
        score = float(signal.get("risk_reward", 0.0) or 0.0)
        if score >= 1.5:
            return {}, SimulatedOrderPlan("ACCEPTED", "MARKET", "SIMULATED_ACCEPT"), "simulated accepted"
        return {}, SimulatedOrderPlan("REJECTED", "NONE", "SIMULATED_REJECT"), "simulated rejected"


def execution_mode_from_env(value: str | None = None) -> ExecutionMode:
    raw = (value or os.getenv("ALPHAFORGE_EXECUTION_MODE", "SIMULATED")).strip().upper()
    try:
        return ExecutionMode(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid execution mode: {raw}") from exc


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


async def _simulated_market_scanner() -> list[dict[str, Any]]:
    logger.info("SIMULATED PAPER MODE: using simulated market scanner")
    return [
        {
            "symbol": "SIM-BTCUSDT",
            "entry": 50000.0,
            "side": "BUY",
            "timeframe": "5m",
            "risk_reward": 2.0,
            "volume_24h_usdt": 8_000_000.0,
            "spread_pct": 0.08,
            "liquidity_score": 0.9,
            "volatility_pct": 2.0,
            "trend_strength": 0.7,
            "recent_volume_change_pct": 5.0,
            "chop_score": 0.4,
        }
    ]


def build_runtime_from_env() -> tuple[RuntimeOrchestrator, bool]:
    mode = execution_mode_from_env()
    cfg = RuntimeConfig(
        execution_mode=mode,
        scan_interval_sec=float(os.getenv("ALPHAFORGE_SCAN_INTERVAL_SEC", "2.0")),
        heartbeat_interval_sec=float(os.getenv("ALPHAFORGE_HEARTBEAT_INTERVAL_SEC", "5.0")),
        scan_timeout_sec=float(os.getenv("ALPHAFORGE_SCAN_TIMEOUT_SEC", "2.0")),
        max_symbols_per_scan=int(os.getenv("ALPHAFORGE_MAX_SYMBOLS_PER_SCAN", "5")),
        max_reject_log_entries=int(os.getenv("ALPHAFORGE_MAX_REJECT_LOG_ENTRIES", "100")),
        require_market_context=_bool_env("ALPHAFORGE_REQUIRE_MARKET_CONTEXT", True),
    )
    run_once = _bool_env("ALPHAFORGE_RUN_ONCE", False)

    if mode == ExecutionMode.LIVE:
        raise ValueError("LIVE mode requires explicit application wiring with real_execution_adapter and validated secrets/config")
    if mode == ExecutionMode.PAPER:
        raise ValueError("PAPER mode requires explicit application wiring with real market scanner + AI brain; implicit simulation disabled")
    if mode == ExecutionMode.BACKTEST:
        raise ValueError("BACKTEST mode must run through backtest_order.py; runtime env bootstrap disabled")

    logger.warning("SIMULATED mode: building runtime with simulated scanner/AI brain")
    return RuntimeOrchestrator(cfg, SimulatedAIBrain(), _simulated_market_scanner), run_once


class RuntimeOrchestrator:
    def __init__(self, config: RuntimeConfig, ai_brain: Any, market_scanner: Callable[[], Awaitable[list[dict[str, Any]]]], *, real_execution_adapter: Any | None = None, on_lifecycle_event: Callable[[dict[str, Any]], Any] | None = None, on_reject_persist: Callable[[dict[str, Any]], Any] | None = None) -> None:
        self.config = config
        self.ai_brain = ai_brain
        self.market_scanner = market_scanner
        self.real_execution_adapter = real_execution_adapter
        self.on_lifecycle_event = on_lifecycle_event
        self.on_reject_persist = on_reject_persist
        self.metrics = RuntimeMetrics()
        self._stop_event = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._reject_log: deque[dict[str, Any]] = deque(maxlen=config.max_reject_log_entries)

    async def start(self, *, run_once: bool = False) -> None:
        if self.config.execution_mode == ExecutionMode.LIVE and self.real_execution_adapter is None:
            raise ValueError("LIVE mode requires real_execution_adapter")

        if run_once:
            await self._scan_once()
            logger.info("runtime_once metrics=%s", asdict(self.metrics))
            return

        self._register_signals()
        self._stop_event.clear()
        self._tasks = {
            asyncio.create_task(self._market_scan_loop(), name="market_scan_loop"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat_loop"),
        }

        for task in list(self._tasks):
            task.add_done_callback(self._on_task_done)

        await self._stop_event.wait()
        await self._shutdown_tasks()

    def shutdown(self) -> None:
        self._stop_event.set()

    def _register_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.shutdown)
            except (NotImplementedError, RuntimeError):
                logger.debug("signal handlers unavailable for %s", sig)

    async def _shutdown_tasks(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.exception("Background task failed: %s", exc)
            self.shutdown()

    async def _market_scan_loop(self) -> None:
        while not self._stop_event.is_set():
            await self._scan_once()
            await asyncio.sleep(self.config.scan_interval_sec)

    async def _scan_once(self) -> None:
        self.metrics.scans += 1
        try:
            candidates = await asyncio.wait_for(self.market_scanner(), timeout=self.config.scan_timeout_sec)
        except asyncio.TimeoutError:
            self.metrics.scan_timeouts += 1
            logger.warning("Market scanner timed out")
            return
        except Exception:
            self.metrics.scan_failures += 1
            logger.exception("Market scanner failed")
            return

        selections = select_symbols(candidates)
        selections = selections[: self.config.max_symbols_per_scan]
        self.metrics.symbols_selected += len(selections)

        # TODO: Replace sequential flow with backpressure-aware worker queue once DB/session ownership is explicit.
        for selection in selections:
            await self._process_symbol(selection)

    async def _process_symbol(self, selection: SymbolSelectionResult) -> None:
        market_ctx = self._extract_market_context(selection)
        missing = self._validate_market_context(market_ctx)
        if missing and self.config.require_market_context:
            reject_payload = {"symbol": selection.symbol, "reason": "MISSING_MARKET_CONTEXT", "missing_fields": missing}
            await self._persist_reject(reject_payload)
            await self._emit_lifecycle_event("SIGNAL_REJECTED", selection.symbol, reject_payload)
            return

        signal_payload = self._build_signal(selection, market_ctx)
        self.metrics.signals_created += 1
        await self._emit_lifecycle_event("SIGNAL_CREATED", selection.symbol, signal_payload)

        execution_ctx = build_execution_context(market_ctx)
        signal_payload["execution_ctx"] = execution_ctx
        ai_market_ctx = self._enrich_ai_market_context(market_ctx, execution_ctx)

        # TODO: before_real_order currently runs on event-loop thread because SQLAlchemy session ownership
        # must be refactored before safe worker-thread offload.
        score_ctx, order_plan, explanation = self.ai_brain.before_real_order(signal_payload, ai_market_ctx, {}, {})
        self.metrics.decisions_generated += 1

        if getattr(order_plan, "decision", None) != "ACCEPTED":
            reject_payload = {
                "symbol": selection.symbol,
                "decision": getattr(order_plan, "decision", "REJECTED"),
                "reason": getattr(order_plan, "reason", "AI_REJECTED"),
                "explanation": explanation,
            }
            await self._persist_reject(reject_payload)
            await self._emit_lifecycle_event("SIGNAL_REJECTED", selection.symbol, reject_payload)
            return

        await self._emit_lifecycle_event("ORDER_PLACED", selection.symbol, {"order_type": getattr(order_plan, "order_type", "UNKNOWN")})
        self.metrics.orders_placed += 1

        result = await self._execute(selection.symbol, order_plan, ai_market_ctx)
        self.metrics.executions += 1
        event = "ORDER_FILLED" if str(result.get("status", "")).upper() == "FILLED" else "ORDER_EXECUTED"
        await self._emit_lifecycle_event(event, selection.symbol, result)

    def _extract_market_context(self, selection: SymbolSelectionResult) -> dict[str, Any]:
        diagnostics = selection.diagnostics or {}
        return dict(diagnostics.get("inputs", {}) or {})

    def _validate_market_context(self, market_ctx: Mapping[str, Any]) -> list[str]:
        missing: list[str] = []
        for field in ("entry", "side", "timeframe"):
            value = market_ctx.get(field)
            if value is None or value == "":
                missing.append(field)
        return missing

    def _build_signal(self, selection: SymbolSelectionResult, market_ctx: Mapping[str, Any]) -> dict[str, Any]:
        # TODO: default RR is a temporary fallback; replace with adaptive RR from expectancy/risk modules.
        rr = float(market_ctx.get("risk_reward", market_ctx.get("rr", 2.0)) or 2.0)
        return {
            "symbol": selection.symbol,
            "side": market_ctx.get("side"),
            "timeframe": market_ctx.get("timeframe"),
            "entry_price": market_ctx.get("entry"),
            "risk_reward": rr,
            "max_spread_bps": market_ctx.get("max_spread_bps", 8.0),
            "max_funding_rate": market_ctx.get("max_funding_rate", 0.0006),
            "max_expected_slippage_pct": market_ctx.get("max_expected_slippage_pct", 0.003),
            "execution_ctx": {},
        }

    def _enrich_ai_market_context(self, market_ctx: Mapping[str, Any], execution_ctx: Mapping[str, Any]) -> dict[str, Any]:
        enriched = dict(market_ctx)
        for key in (
            "expected_slippage_pct",
            "spread_pct",
            "funding_rate_pct",
            "latency_ms",
            "liquidity_score",
            "orderbook_imbalance",
            "volatility_regime",
            "volume_24h_usdt",
        ):
            if enriched.get(key) in (None, "") and key in execution_ctx:
                enriched[key] = execution_ctx[key]
        return enriched

    async def _execute(self, symbol: str, decision: Any, market_ctx: Mapping[str, Any]) -> dict[str, Any]:
        if self.config.execution_mode == ExecutionMode.PAPER:
            return self._simulate_paper_execution(symbol, decision, market_ctx)
        if self.config.execution_mode == ExecutionMode.BACKTEST:
            return {
                "mode": ExecutionMode.BACKTEST.value,
                "symbol": symbol,
                "status": "SIMULATED",
                "note": "Historical TP/SL simulation belongs to backtest_order.py",
            }
        if self.config.execution_mode == ExecutionMode.LIVE:
            if self.real_execution_adapter is None:
                raise ValueError("LIVE mode requires real_execution_adapter")
            return await self.real_execution_adapter.submit(symbol=symbol, decision=decision, market_ctx=dict(market_ctx))
        raise AssertionError("Unknown execution mode")

    def _simulate_paper_execution(self, symbol: str, decision: Any, market_ctx: Mapping[str, Any]) -> dict[str, Any]:
        entry = float(market_ctx.get("entry", 0.0) or 0.0)
        slip = float(market_ctx.get("expected_slippage_pct", 0.0) or 0.0)
        fill_price = entry * (1.0 + slip)
        return {
            "mode": ExecutionMode.PAPER.value,
            "symbol": symbol,
            "status": "FILLED",
            "order_type": getattr(decision, "order_type", "MARKET"),
            "expected_slippage_pct": slip,
            "fill_price": fill_price,
        }

    async def _persist_reject(self, payload: dict[str, Any]) -> None:
        self._reject_log.append(payload)
        self.metrics.rejects_persisted += 1
        if self.on_reject_persist is None:
            return
        try:
            result = self.on_reject_persist(payload)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("reject persistence callback failed")

    async def _emit_lifecycle_event(self, event: str, symbol: str, details: Mapping[str, Any]) -> None:
        payload = {
            "event": event,
            "symbol": symbol,
            "ts": datetime.now(timezone.utc).isoformat(),
            "mode": self.config.execution_mode.value,
            "details": dict(details),
        }
        self.metrics.lifecycle_events += 1
        if self.on_lifecycle_event is None:
            return
        try:
            result = self.on_lifecycle_event(payload)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("lifecycle callback failed")

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                self.metrics.last_heartbeat_ts = datetime.now(timezone.utc).isoformat()
                logger.info("runtime_heartbeat metrics=%s", asdict(self.metrics))
                await asyncio.sleep(self.config.heartbeat_interval_sec)
        except asyncio.CancelledError:
            raise
            

async def main() -> None:
    logging.basicConfig(level=os.getenv("ALPHAFORGE_LOG_LEVEL", "INFO").upper())
    runtime, run_once = build_runtime_from_env()
    await runtime.start(run_once=run_once)


if __name__ == "__main__":
    asyncio.run(main())
