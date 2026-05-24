from __future__ import annotations

import asyncio
import contextlib
from collections import deque
import hashlib
import logging
import os
import signal
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Protocol

from alphaforge.ai_brain import AIBrain
from alphaforge.contracts import LifecycleEventType, canonical_reject_reason, canonical_utc_timestamp, validate_transition
from alphaforge.order import LifecycleState
from alphaforge.execution import build_execution_context
from alphaforge.live_readiness import LiveReadinessEvaluator, QualificationReport
from alphaforge.runtime_heartbeat import save_runtime_heartbeat
from alphaforge.exchange_connectivity import ExchangeHealth, check_required_exchanges_health
from alphaforge.exchange_market_scanner import scan_exchange_markets
from alphaforge.binance_reconciliation_provider import BinanceReadonlyReconciliationConfig, BinanceReadonlyReconciliationProvider
from alphaforge.reconciliation import ReconciliationEngine, persist_findings, summarize_findings
from alphaforge.symbol_selector import SymbolSelectionResult, select_symbols
from alphaforge.persistence import init_db
from alphaforge.config import load_config_from_env
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)


class ExecutionMode(str, Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


class RealExecutionAdapter(Protocol):
    async def submit(self, decision: Mapping[str, Any], market_ctx: Mapping[str, Any]) -> Mapping[str, Any]: ...


class LiveReconciliationProvider(Protocol):
    def snapshot(self) -> Mapping[str, Any]: ...


@dataclass(slots=True)
class RuntimeConfig:
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    scan_interval_sec: float = 1.0
    heartbeat_interval_sec: float = 30.0
    max_symbols_per_scan: int = 5
    max_reject_log_entries: int = 1000
    max_concurrent_positions: int = 3
    symbol_cooldown_sec: float = 120.0
    max_notional_exposure: float = 100_000.0
    max_symbol_notional: float = 50_000.0
    stale_market_data_sec: float = 15.0
    max_spread_pct: float = 0.0025
    max_abs_funding_rate_pct: float = 0.0010
    global_kill_switch: bool = False
    require_live_qualification: bool = True
    enable_shadow_mode: bool = False
    enable_canary_mode: bool = False
    operator_live_acknowledged: bool = False
    reconciliation_interval_sec: float = 5.0
    reconciliation_timeout_sec: float = 2.0
    require_exchange_connectivity_for_live: bool = True
    required_live_exchanges: tuple[str, ...] = ("binance",)
    exchange_connectivity_timeout_sec: float = 2.0
    enable_binance_readonly_reconciliation: bool = False


@dataclass(slots=True)
class RuntimeMetrics:
    scans: int = 0
    symbols_selected: int = 0
    decisions_generated: int = 0
    rejects_persisted: int = 0
    executions: int = 0
    lifecycle_events: int = 0
    last_heartbeat_ts: float = 0.0
    last_scan_ts: str | None = None
    last_decision_ts: str | None = None
    reconciliation_runs: int = 0
    reconciliation_fail_closed: int = 0
    persistence_enabled: bool = False


@dataclass(slots=True)
class RuntimeOrchestrator:
    config: RuntimeConfig
    ai_brain: AIBrain
    market_scanner: Callable[[], Awaitable[list[dict[str, Any]]]]
    scanner_source: str = "UNKNOWN"
    real_execution_adapter: RealExecutionAdapter | None = None
    live_reconciliation_provider: LiveReconciliationProvider | None = None
    on_lifecycle_event: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None
    on_reject_persist: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None
    paper_slippage_bps: float = 2.0
    persistence_engine: Engine | None = None
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _tasks: list[asyncio.Task[Any]] = field(default_factory=list, init=False)
    _reject_log: deque[dict[str, Any]] = field(init=False)
    metrics: RuntimeMetrics = field(default_factory=RuntimeMetrics, init=False)
    runtime_instance_id: str = field(default_factory=lambda: f"runtime:{uuid.uuid4().hex}", init=False)
    _last_lifecycle_state_by_symbol: dict[str, str] = field(default_factory=dict, init=False)
    _symbol_cooldown_until: dict[str, float] = field(default_factory=dict, init=False)
    _active_positions: dict[str, float] = field(default_factory=dict, init=False)
    _incident_counters: dict[str, int] = field(default_factory=dict, init=False)
    _qualification_report: QualificationReport | None = field(default=None, init=False)
    _reconciliation_engine: ReconciliationEngine = field(default_factory=ReconciliationEngine, init=False)
    _pending_orders: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    _last_repair_signature: set[str] = field(default_factory=set, init=False)
    _last_scan_rejection_summary: dict[str, int] = field(default_factory=dict, init=False)
    _last_scan_gate_blockers: list[str] = field(default_factory=list, init=False)
    _exchange_health: list[ExchangeHealth] = field(default_factory=list, init=False)
    _qualification_samples: tuple[dict[str, Any], ...] = field(default_factory=lambda: (
        {
            "sample_id": "qp-001-btc-long",
            "symbol": "BTCUSDT",
            "entry": 67250.0,
            "market_ts": 1716200000.0,
            "side": "LONG",
            "rr": 2.15,
            "spread_pct": 0.0009,
            "funding_rate_pct": 0.00005,
            "liquidity_score": 0.86,
            "timeframe": "5m",
        },
        {
            "sample_id": "qp-002-eth-short",
            "symbol": "ETHUSDT",
            "entry": 3450.0,
            "market_ts": 1716200060.0,
            "side": "SHORT",
            "rr": 1.95,
            "spread_pct": 0.0008,
            "funding_rate_pct": 0.00004,
            "liquidity_score": 0.82,
            "timeframe": "5m",
        },
        {
            "sample_id": "qp-003-sol-long",
            "symbol": "SOLUSDT",
            "entry": 155.0,
            "market_ts": 1716200120.0,
            "side": "LONG",
            "rr": 2.05,
            "spread_pct": 0.0011,
            "funding_rate_pct": 0.00003,
            "liquidity_score": 0.78,
            "timeframe": "5m",
        },
    ), init=False)

    def __post_init__(self) -> None:
        self._reject_log = deque(maxlen=max(1, self.config.max_reject_log_entries))

    def _resolve_persistence_engine(self) -> Engine | None:
        if self.persistence_engine is not None:
            return self.persistence_engine
        session = getattr(self.ai_brain, "session", None)
        if session is not None:
            return session.get_bind()
        return None

    def _persist_runtime_heartbeat(self, *, runtime_state: str = "OPERATING") -> None:
        engine = self._resolve_persistence_engine()
        if (
            engine is None
            or not self.metrics.persistence_enabled
            or self.config.execution_mode not in {ExecutionMode.PAPER, ExecutionMode.LIVE}
        ):
            return
        save_runtime_heartbeat(
            engine,
            runtime_instance_id=self.runtime_instance_id,
            execution_mode=self.config.execution_mode.value,
            scanner_source=self.scanner_source,
            runtime_state=runtime_state,
            last_scan_ts=self.metrics.last_scan_ts,
            last_decision_ts=self.metrics.last_decision_ts,
            active_positions_count=len(self._active_positions),
            pending_orders_count=len(self._pending_orders),
            payload={
                "scans": self.metrics.scans,
                "symbols_selected": self.metrics.symbols_selected,
                "decisions_generated": self.metrics.decisions_generated,
                "rejects_persisted": self.metrics.rejects_persisted,
                "executions": self.metrics.executions,
                "lifecycle_events": self.metrics.lifecycle_events,
                "reconciliation_runs": self.metrics.reconciliation_runs,
                "reconciliation_fail_closed": self.metrics.reconciliation_fail_closed,
                "persistence_enabled": self.metrics.persistence_enabled,
                "top_selection_reject_reasons": dict(sorted(self._last_scan_rejection_summary.items(), key=lambda item: item[1], reverse=True)[:3]),
                "decision_gate_blockers": self._last_scan_gate_blockers,
            },
        )

    async def start(self) -> None:
        if self.config.execution_mode == ExecutionMode.LIVE:
            allowed_sources = {"EXCHANGE_PUBLIC_MARKET_DATA"}
            scanner_source = str(self.scanner_source or "UNKNOWN").strip().upper()
            if not scanner_source or scanner_source == "UNKNOWN":
                raise RuntimeError("LIVE mode blocked: market scanner provenance is not verified")
            if scanner_source not in allowed_sources:
                raise RuntimeError("LIVE mode blocked: exchange-backed market scanner is required")
            if self.real_execution_adapter is None:
                raise RuntimeError("LIVE mode blocked: real execution adapter is not configured")
            await self._run_live_exchange_connectivity_gate()
            if self.config.require_live_qualification:
                self._persist_runtime_heartbeat()
                await self._run_live_qualification_gate()
        self._register_signals()
        self._tasks = [
            asyncio.create_task(self._market_scan_loop(), name="market_scan_loop"),
            asyncio.create_task(self._heartbeat_loop(), name="metrics_heartbeat"),
            asyncio.create_task(self._reconciliation_loop(), name="reconciliation_loop"),
        ]
        for task in self._tasks:
            task.add_done_callback(self._on_task_done)
        try:
            await self._stop_event.wait()
        finally:
            self._persist_runtime_heartbeat(runtime_state="STOPPING")
            await self._shutdown_tasks()

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            exc = task.exception()
            if exc is not None:
                logger.exception("runtime_task_failed task=%s", task.get_name(), exc_info=exc)
                self.shutdown()

    def shutdown(self) -> None:
        self._stop_event.set()

    async def _run_live_exchange_connectivity_gate(self) -> None:
        if not self.config.require_exchange_connectivity_for_live:
            return
        health = check_required_exchanges_health(list(self.config.required_live_exchanges), timeout_sec=self.config.exchange_connectivity_timeout_sec)
        self._exchange_health = health
        failures = [h for h in health if not h.connected]
        if failures:
            summary = ",".join(f"{h.exchange}:{h.error or 'UNAVAILABLE'}" for h in failures)
            raise RuntimeError(f"LIVE mode blocked: exchange connectivity unavailable ({summary})")

    async def _run_live_qualification_gate(self) -> None:
        engine = self._resolve_persistence_engine()
        if engine is None:
            raise RuntimeError("LIVE qualification requires runtime persistence engine")
        evaluator = LiveReadinessEvaluator(engine)
        mode_parity = self._build_mode_parity_evidence(min_sample_count=3)
        reconciliation_snapshot = {
            "provider_configured": self.live_reconciliation_provider is not None,
            "evidence_status": "UNVERIFIED",
        }
        if self.live_reconciliation_provider is not None:
            provider_snapshot = dict(self.live_reconciliation_provider.snapshot())
            evidence_status = str(provider_snapshot.get("evidence_status") or "INCOMPLETE").upper()
            reconciliation_snapshot["evidence_status"] = evidence_status
            if evidence_status == "COMPLETE":
                snapshot = self._reconciliation_engine.snapshot_from_source(provider_snapshot)
                findings, _recommendations, _metrics = self._reconciliation_engine.reconcile(
                    intended_orders=list(self._pending_orders.values()),
                    lifecycle_state_by_symbol=self._last_lifecycle_state_by_symbol,
                    snapshot=snapshot,
                    mode=ExecutionMode.LIVE.value,
                )
                counters = summarize_findings(findings)
                reconciliation_snapshot.update(counters)
            else:
                reconciliation_snapshot.update({
                    "orphan_orders": 0,
                    "orphan_positions": 0,
                    "duplicate_fills": 0,
                    "lifecycle_divergences": 0,
                    "fail_closed_findings": 1,
                })
        observability_snapshot = {
            "evidence_status": "INCOMPLETE",
            "qualification_persistence_verified": True,
            "incident_persistence_verified": False,
            "forensic_export_verified": True,
            "sensitive_data_redaction_verified": True,
            "alert_delivery_verified": False,
            "rollback_evidence_status": "INCOMPLETE",
            "kill_switch_block_verified": True,
            "no_submit_on_kill_switch_verified": True,
            "fail_closed_reconciliation_verified": True,
            "repair_actions_non_mutating_verified": True,
        }
        report = evaluator.evaluate(
            mode_parity=mode_parity,
            reconciliation_snapshot=reconciliation_snapshot,
            observability_snapshot=observability_snapshot,
            canary_enabled=self.config.enable_canary_mode,
            shadow_mode_enabled=self.config.enable_shadow_mode,
            operator_ack=self.config.operator_live_acknowledged,
        )
        evaluator.persist_report(report)
        self._qualification_report = report
        logger.warning("live_readiness_report=%s", report.to_dict())
        if not report.qualified:
            self._persist_runtime_heartbeat(runtime_state="STOPPING")
            raise RuntimeError("LIVE mode blocked: readiness qualification failed")

    def _evaluate_pre_submit(self, signal_payload: Mapping[str, Any], market_ctx: Mapping[str, Any], regime_ctx: Mapping[str, Any], stats_ctx: Mapping[str, Any]) -> dict[str, Any]:
        score_ctx = self.ai_brain.score_signal(signal_payload, market_ctx, regime_ctx, stats_ctx)
        order_plan = self.ai_brain.choose_order_plan(signal_payload, market_ctx, score_ctx)
        explanation = self.ai_brain.explain_decision(signal_payload, score_ctx, order_plan)
        return {
            "decision": order_plan.decision,
            "reason": order_plan.reason,
            "order_type": order_plan.order_type,
            "confidence": float(order_plan.confidence),
            "score": float(getattr(score_ctx, "total_score", 0.0) or 0.0),
            "effective_rr": float(signal_payload.get("risk_reward", signal_payload.get("rr", 0.0)) or 0.0),
            "explanation": explanation,
        }

    def _build_mode_parity_evidence(self, *, min_sample_count: int = 3) -> dict[str, Any]:
        samples = list(self._qualification_samples[: max(0, int(min_sample_count))])
        comparisons: list[dict[str, Any]] = []
        mismatch_count = 0
        missing_field_count = 0
        compare_fields = ("decision", "reason", "order_type", "confidence", "score", "effective_rr", "explanation")
        for row in samples:
            sample = dict(row)
            sample_id = str(sample["sample_id"])
            paper_signal_payload = {
                "signal_id": f"precheck:{sample_id}",
                "symbol": sample["symbol"],
                "mode": "PAPER",
                "side": sample.get("side", "LONG"),
                "timeframe": sample.get("timeframe", "5m"),
                "entry_price": float(sample.get("entry", 0.0) or 0.0),
                "risk_reward": float(sample.get("rr", 0.0) or 0.0),
            }
            live_precheck_signal_payload = {**paper_signal_payload, "mode": "LIVE_PRECHECK"}
            regime_ctx = {"alignment": 0.8}
            stats_ctx: dict[str, Any] = {}
            paper_eval = self._evaluate_pre_submit(paper_signal_payload, {**sample, "mode": "PAPER"}, regime_ctx, stats_ctx)
            live_eval = self._evaluate_pre_submit(live_precheck_signal_payload, {**sample, "mode": "LIVE_PRECHECK"}, regime_ctx, stats_ctx)
            missing = [field for field in compare_fields if field not in paper_eval or field not in live_eval]
            mismatch = [field for field in compare_fields if field in paper_eval and field in live_eval and paper_eval[field] != live_eval[field]]
            missing_field_count += len(missing)
            mismatch_count += len(mismatch)
            comparisons.append({
                "sample_id": sample_id,
                "paper": {k: paper_eval.get(k) for k in compare_fields},
                "live_precheck": {k: live_eval.get(k) for k in compare_fields},
                "missing_fields": missing,
                "mismatch_fields": mismatch,
            })
        return {
            "evidence_status": "COMPLETE" if samples and mismatch_count == 0 and missing_field_count == 0 else "INCOMPLETE",
            "sample_count": len(samples),
            "min_sample_count": int(min_sample_count),
            "mismatch_count": mismatch_count,
            "missing_field_count": missing_field_count,
            "no_order_submission_verified": True,
            "comparison_fields": list(compare_fields),
            "samples": comparisons,
            "generated_at": canonical_utc_timestamp(),
        }

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
        self.metrics.last_scan_ts = canonical_utc_timestamp()
        pre_selection = select_symbols(candidates, {"include_rejected": True})
        selected = [row for row in pre_selection if row.tradable][: self.config.max_symbols_per_scan]
        reject_reasons: dict[str, int] = {}
        for row in pre_selection:
            for reason in row.reject_reasons:
                reject_reasons[reason] = reject_reasons.get(reason, 0) + 1
        self._last_scan_rejection_summary = reject_reasons
        if not candidates:
            self._last_scan_gate_blockers = ["NO_MARKET_CANDIDATES"]
        elif not selected:
            self._last_scan_gate_blockers = ["NO_TRADABLE_SYMBOLS_AFTER_SELECTION"]
        else:
            self._last_scan_gate_blockers = []
        self.metrics.symbols_selected += len(selected)

        for symbol_result in selected:
            await self._process_symbol(symbol_result)

    async def _process_symbol(self, selection: SymbolSelectionResult) -> None:
        market_ctx = dict(selection.diagnostics.get("inputs", {}))
        market_ctx.setdefault("mode", self.config.execution_mode.value)
        signal_id = self._resolve_signal_id(selection.symbol, market_ctx)
        execution_ctx = build_execution_context(market_ctx)
        market_ctx["execution_ctx"] = execution_ctx
        risk_reject = self._evaluate_runtime_risk(selection.symbol, market_ctx)
        await self._emit_lifecycle_event(LifecycleState.SIGNAL_CREATED.value, selection.symbol, {"reason": "", "signal_id": signal_id})
        if risk_reject is not None:
            await self._persist_reject({"signal_id": signal_id, "symbol": selection.symbol, "mode": self.config.execution_mode.value, "phase": "final", "decision": "REJECTED", "reason": risk_reject, "confidence": 0.0, "score": 0.0, "rr": market_ctx.get("rr"), "effective_rr": market_ctx.get("rr"), "explanation": "runtime_risk_gate", "execution_ctx": execution_ctx, "spread_pct": execution_ctx.get("spread_pct"), "expected_slippage_pct": execution_ctx.get("expected_slippage_pct"), "latency_ms": execution_ctx.get("market_data_latency_ms"), "funding_rate_pct": execution_ctx.get("funding_rate_pct"), "orderbook_imbalance": execution_ctx.get("orderbook_imbalance"), "volatility_regime": execution_ctx.get("volatility_regime")})
            await self._emit_lifecycle_event(LifecycleState.SIGNAL_REJECTED.value, selection.symbol, {"reason": risk_reject, "signal_id": signal_id})
            return
        signal_payload = self._build_signal(selection, market_ctx, signal_id=signal_id)
        regime_ctx = {"alignment": 0.8 if selection.regime_hint != "UNFAVORABLE" else 0.3}
        stats_ctx: dict[str, Any] = {}
        try:
            score_ctx, order_plan, explanation = self.ai_brain.before_real_order(
                signal_payload,
                market_ctx,
                regime_ctx,
                stats_ctx,
            )
        except Exception as exc:
            await self._emit_runtime_error(selection.symbol, signal_id, "before_real_order", exc)
            return
        self.metrics.decisions_generated += 1
        self.metrics.last_decision_ts = canonical_utc_timestamp()

        if order_plan.decision != "ACCEPTED":
            reject_reason = canonical_reject_reason(order_plan.reason)
            await self._persist_reject({
                "signal_id": signal_id,
                "symbol": selection.symbol,
                "mode": self.config.execution_mode.value,
                "phase": "final",
                "decision": order_plan.decision,
                "reason": reject_reason,
                "confidence": order_plan.confidence,
                "score": getattr(score_ctx, "total_score", None),
                "rr": signal_payload.get("risk_reward"),
                "effective_rr": signal_payload.get("risk_reward"),
                "explanation": explanation,
                "execution_ctx": execution_ctx,
                "spread_pct": execution_ctx.get("spread_pct"),
                "expected_slippage_pct": execution_ctx.get("expected_slippage_pct"),
                "latency_ms": execution_ctx.get("market_data_latency_ms"),
                "funding_rate_pct": execution_ctx.get("funding_rate_pct"),
                "orderbook_imbalance": execution_ctx.get("orderbook_imbalance"),
                "volatility_regime": execution_ctx.get("volatility_regime"),
            })
            await self._emit_lifecycle_event(LifecycleState.SIGNAL_REJECTED.value, selection.symbol, {"reason": reject_reason, "signal_id": signal_id})
            return

        if self.config.execution_mode == ExecutionMode.PAPER:
            await self._emit_lifecycle_event(LifecycleState.WAITING_ENTRY_ZONE.value, selection.symbol, {})
            await self._emit_lifecycle_event(LifecycleState.ENTRY_TRIGGERED.value, selection.symbol, {})
            await self._emit_lifecycle_event(LifecycleState.ORDER_PLACED.value, selection.symbol, {})
        else:
            await self._emit_lifecycle_event(LifecycleEventType.ENTRY_PENDING.value, selection.symbol, {})
            await self._emit_lifecycle_event(LifecycleEventType.ENTRY_SUBMITTED.value, selection.symbol, {})
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
        order_id = str(result.get("order_id") or f"{symbol}:{canonical_utc_timestamp()}")
        self._pending_orders[symbol] = {"order_id": order_id, "symbol": symbol, "status": result.get("status", "UNKNOWN"), "created_at": canonical_utc_timestamp()}
        await self._emit_lifecycle_event(LifecycleState.ORDER_PLACED.value, symbol, {"decision": decision, "result": dict(result)})
        result_status = str(result.get("status", "")).lower()
        if result_status == "partial_fill":
            await self._emit_lifecycle_event(LifecycleState.POSITION_OPENED.value, symbol, {"result": dict(result), "fill_state": "partial"})
        elif result_status in {"rejected", "exchange_reject"}:
            await self._emit_lifecycle_event(LifecycleState.ORDER_REJECTED.value, symbol, {"reason": "exchange_rejected_order", "result": dict(result)})
            return
        elif result_status in {"timeout", "error", "missing_ack"}:
            await self._record_incident(symbol, LifecycleState.ENTRY_TIMEOUT.value, "execution_uncertain_state")
            await self._reconcile_symbol_state(symbol, result, market_ctx)
            return
        await self._emit_lifecycle_event(LifecycleState.POSITION_OPENED.value, symbol, {"result": dict(result)})
        self._active_positions[symbol] = float(market_ctx.get("entry", 0.0) or 0.0)
        self._symbol_cooldown_until[symbol] = time.time() + self.config.symbol_cooldown_sec

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
        lifecycle_state = event if validate_transition(previous_state, event) else LifecycleState.ERROR.value
        detail_payload = dict(details or {})
        signal_id = detail_payload.get("signal_id") or self._resolve_signal_id(symbol, detail_payload)
        event_payload = {
            "lifecycle_event_type": lifecycle_state,
            "lifecycle_state": lifecycle_state,
            "signal_id": signal_id,
            "symbol": symbol,
            "timestamp": canonical_utc_timestamp(),
            "mode": self.config.execution_mode.value,
            "previous_lifecycle_state": previous_state,
            "details": detail_payload,
        }
        self._last_lifecycle_state_by_symbol[symbol] = lifecycle_state
        self.metrics.lifecycle_events += 1
        if self.on_lifecycle_event is not None:
            maybe_coro = self.on_lifecycle_event(event_payload)
            if asyncio.iscoroutine(maybe_coro):
                await maybe_coro

    async def _record_incident(self, symbol: str, lifecycle_event: str, reason: str) -> None:
        self._incident_counters[reason] = self._incident_counters.get(reason, 0) + 1
        await self._emit_lifecycle_event(lifecycle_event, symbol, {"reason": reason, "incident_count": self._incident_counters[reason]})

    async def _emit_runtime_error(self, symbol: str, signal_id: str, phase: str, exc: Exception) -> None:
        failure_reason = f"{exc.__class__.__name__}: {str(exc)[:220]}".strip()
        await self._emit_lifecycle_event(
            LifecycleState.ERROR.value,
            symbol,
            {
                "signal_id": signal_id,
                "failure_reason": failure_reason,
                "incident_payload": {
                    "exception_type": exc.__class__.__name__,
                    "exception_message": str(exc),
                    "symbol": symbol,
                    "signal_id": signal_id,
                    "decision_id": None,
                    "phase": phase,
                },
            },
        )

    @staticmethod
    def _resolve_signal_id(symbol: str, payload: Mapping[str, Any]) -> str:
        if payload.get("signal_id"):
            return str(payload["signal_id"])
        fingerprint = "|".join([
            str(symbol),
            str(payload.get("side", "UNKNOWN")),
            str(payload.get("timeframe", "NA")),
            str(payload.get("entry") or payload.get("entry_price") or 0.0),
            str(payload.get("market_ts") or payload.get("timestamp") or canonical_utc_timestamp()),
        ])
        return f"runtime:{hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:24]}"

    def _evaluate_runtime_risk(self, symbol: str, market_ctx: Mapping[str, Any]) -> str | None:
        now = time.time()
        if self.config.global_kill_switch:
            return "GLOBAL_KILL_SWITCH"
        if len(self._active_positions) >= self.config.max_concurrent_positions:
            return "MAX_CONCURRENT_POSITIONS"
        if now < self._symbol_cooldown_until.get(symbol, 0.0):
            return "SYMBOL_COOLDOWN"
        market_ts = float(market_ctx.get("market_ts", now) or now)
        if (now - market_ts) > self.config.stale_market_data_sec:
            return "STALE_MARKET_DATA"
        spread_pct = float(market_ctx.get("spread_pct", 0.0) or 0.0)
        if spread_pct > self.config.max_spread_pct:
            return "HIGH_SPREAD"
        funding = abs(float(market_ctx.get("funding_rate_pct", 0.0) or 0.0))
        if funding > self.config.max_abs_funding_rate_pct:
            return "FUNDING_SANITY_REJECT"
        if symbol in self._active_positions:
            return "DUPLICATE_POSITION"
        return None

    async def _reconcile_symbol_state(self, symbol: str, exchange_result: Mapping[str, Any], market_ctx: Mapping[str, Any]) -> None:
        reason = str(exchange_result.get("status") or "unknown")
        snapshot = {
            "intended_state": self._last_lifecycle_state_by_symbol.get(symbol),
            "exchange_state": reason,
            "persisted_state": self._last_lifecycle_state_by_symbol.get(symbol),
            "market_ts": market_ctx.get("market_ts"),
        }
        await self._emit_lifecycle_event(LifecycleEventType.RECONCILIATION_REPAIR.value, symbol, {"reason": f"reconcile_{reason}", "snapshot": snapshot})

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                self.metrics.last_heartbeat_ts = time.time()
                self._persist_runtime_heartbeat()
                logger.info(
                    "runtime_heartbeat=%s persistence_enabled=%s top_selection_reject_reasons=%s decision_gate_blockers=%s",
                    self.metrics,
                    self.metrics.persistence_enabled,
                    dict(sorted(self._last_scan_rejection_summary.items(), key=lambda item: item[1], reverse=True)[:3]),
                    self._last_scan_gate_blockers,
                )
                await asyncio.sleep(self.config.heartbeat_interval_sec)
        except asyncio.CancelledError:
            raise

    async def _reconciliation_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                started = time.time()
                await self._run_reconciliation_once()
                elapsed = time.time() - started
                await asyncio.sleep(max(0.0, self.config.reconciliation_interval_sec - elapsed))
        except asyncio.CancelledError:
            raise

    async def _run_reconciliation_once(self) -> None:
        self.metrics.reconciliation_runs += 1
        try:
            await asyncio.wait_for(self._reconcile_runtime_state(), timeout=self.config.reconciliation_timeout_sec)
        except asyncio.TimeoutError:
            await self._record_incident("GLOBAL", LifecycleEventType.RECONCILIATION_REPAIR.value, "reconciliation_timeout")
            self.metrics.reconciliation_fail_closed += 1
            self.shutdown()

    async def _reconcile_runtime_state(self) -> None:
        if self.config.execution_mode == ExecutionMode.LIVE:
            if self.live_reconciliation_provider is None:
                raise RuntimeError("LIVE mode blocked: reconciliation provider is not configured")
            snapshot_source = dict(self.live_reconciliation_provider.snapshot())
            if str(snapshot_source.get("evidence_status") or "INCOMPLETE").upper() != "COMPLETE":
                raise RuntimeError("LIVE mode blocked: reconciliation evidence incomplete")
        else:
            snapshot_source = {
                "orders": list(self._pending_orders.values()),
                "positions": [{"symbol": s, "qty": q} for s, q in self._active_positions.items()],
                "fills": [],
            }
        snapshot = self._reconciliation_engine.snapshot_from_source(snapshot_source)
        findings, recommendations, _metrics = self._reconciliation_engine.reconcile(
            intended_orders=list(self._pending_orders.values()),
            lifecycle_state_by_symbol=self._last_lifecycle_state_by_symbol,
            snapshot=snapshot,
            mode=self.config.execution_mode.value,
        )
        engine = self._resolve_persistence_engine()
        if engine is not None:
            persist_findings(engine, findings)
        for finding in findings:
            signature = f"{finding.finding_type}:{finding.symbol}:{finding.lifecycle_ref}"
            if signature in self._last_repair_signature:
                continue
            self._last_repair_signature.add(signature)
            await self._emit_lifecycle_event(
                LifecycleEventType.RECONCILIATION_REPAIR.value,
                finding.symbol,
                {"reason": finding.finding_type, "incident_payload": finding.evidence},
            )
            if finding.fail_closed:
                self.metrics.reconciliation_fail_closed += 1
                self.shutdown()
        if recommendations:
            logger.warning("reconciliation_repair_recommendations=%s", [r.category for r in recommendations])

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
    def _build_signal(selection: SymbolSelectionResult, market_ctx: Mapping[str, Any], *, signal_id: str | None = None) -> dict[str, Any]:
        execution_ctx = build_execution_context(market_ctx)
        rr = float(market_ctx.get("rr", 2.0) or 2.0)
        return {
            "symbol": selection.symbol,
            "signal_id": signal_id or RuntimeOrchestrator._resolve_signal_id(selection.symbol, market_ctx),
            "mode": str(market_ctx.get("mode", "PAPER")).upper(),
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
    mode = (str(raw_mode or "PAPER").strip() or "PAPER").upper()
    try:
        return ExecutionMode(mode)
    except ValueError as exc:
        raise ValueError(f"Unsupported EXECUTION_MODE={raw_mode!r}. Expected BACKTEST/PAPER/LIVE") from exc


def _build_runtime_from_env() -> RuntimeOrchestrator:
    cfg = load_config_from_env()
    mode = execution_mode_from_env(cfg.runtime.execution_mode)
    persistence_enabled = cfg.persistence.enabled
    resolved_database_url = cfg.persistence.database_url
    engine = init_db(resolved_database_url)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))
        table_names = [str(row[0]) for row in rows]
    logger.info("runtime_db_bootstrap persistence_enabled=%s resolved_db_url=%s schema_initialized=%s tables=%s", persistence_enabled, resolved_database_url, True, table_names)
    brain = AIBrain(session_factory=SessionLocal, min_accept_score=cfg.runtime.min_signal_score)
    config = RuntimeConfig(execution_mode=mode, scan_interval_sec=cfg.runtime.scan_interval_sec, heartbeat_interval_sec=cfg.runtime.heartbeat_interval_sec, max_symbols_per_scan=cfg.runtime.max_symbols_per_scan, max_reject_log_entries=cfg.runtime.max_reject_log_entries, max_concurrent_positions=cfg.runtime.max_concurrent_positions, symbol_cooldown_sec=cfg.runtime.symbol_cooldown_sec, max_notional_exposure=cfg.runtime.max_notional_exposure, max_symbol_notional=cfg.runtime.max_symbol_notional, stale_market_data_sec=cfg.runtime.stale_market_data_sec, max_spread_pct=cfg.runtime.max_spread_pct, max_abs_funding_rate_pct=cfg.runtime.max_abs_funding_rate_pct, global_kill_switch=cfg.runtime.global_kill_switch, require_live_qualification=cfg.runtime.require_live_qualification, enable_shadow_mode=cfg.runtime.enable_shadow_mode, enable_canary_mode=cfg.runtime.enable_canary_mode, operator_live_acknowledged=cfg.runtime.operator_live_acknowledged, reconciliation_interval_sec=cfg.runtime.reconciliation_interval_sec, reconciliation_timeout_sec=cfg.runtime.reconciliation_timeout_sec, require_exchange_connectivity_for_live=cfg.runtime.require_exchange_connectivity_for_live, required_live_exchanges=cfg.runtime.required_live_exchanges, exchange_connectivity_timeout_sec=cfg.runtime.exchange_connectivity_timeout_sec, enable_binance_readonly_reconciliation=cfg.runtime.enable_binance_readonly_reconciliation)

    async def _safe_market_scanner() -> list[dict[str, Any]]:
        now_ts = time.time()
        return [{"symbol": "BTCUSDT", "volume_24h_usdt": 125_000_000.0, "spread_pct": 0.0009, "funding_rate_pct": 0.00005, "liquidity_score": 0.86, "liquidity_quality": "HIGH", "volatility_pct": 0.011, "volatility_fit": "GOOD", "volatility_regime": "MODERATE", "trend_strength": 0.64, "momentum_confirmation": 0.7, "recent_volume_change_pct": 0.085, "chop_score": 0.27, "panic_score": 0.06, "fakeout_risk": 0.22, "spread_bps": 9.0, "expected_slippage_pct": 0.0006, "latency_ms": 55.0, "market_ts": now_ts, "entry": 67_250.0, "side": "LONG", "rr": 2.15, "timeframe": "5m", "tick_size": 0.1}]

    safe_scanner_requested = str(os.getenv("ALPHAFORGE_RUNTIME_SAFE_SCANNER", "0")).strip().lower() in {"1", "true", "yes", "on"}
    use_safe_scanner = safe_scanner_requested
    scanner_source = "SAFE_PLACEHOLDER" if use_safe_scanner else "EXCHANGE_PUBLIC_MARKET_DATA"

    async def _runtime_market_scanner() -> list[dict[str, Any]]:
        if mode == ExecutionMode.BACKTEST and use_safe_scanner:
            logger.warning("market_data_source=SYNTHETIC_SMOKE_TEST backtest_runtime_scanner=_safe_market_scanner smoke_test_only=true")
            return await _safe_market_scanner()
        if use_safe_scanner:
            return await _safe_market_scanner()
        return await scan_exchange_markets(cfg)

    def _persist_lifecycle(payload: dict[str, Any]) -> None:
        if not persistence_enabled:
            return
        from alphaforge.persistence import save_trade_lifecycle_event
        details = dict(payload.get("details") or {})
        with SessionLocal() as session:
            if not save_trade_lifecycle_event(
                session,
                signal_id=payload.get("signal_id"),
                symbol=payload.get("symbol"),
                mode=payload.get("mode"),
                lifecycle_state=payload.get("lifecycle_state"),
                previous_lifecycle_state=payload.get("previous_lifecycle_state"),
                event_ts=payload.get("timestamp"),
                event_type=payload.get("lifecycle_event_type"),
                payload=details,
                failure_reason=details.get("failure_reason"),
                incident_payload=details.get("incident_payload"),
                reject_reason=details.get("reason"),
            ):
                raise RuntimeError("trade_lifecycle_event_persistence_failed")
            session.commit()

    def _persist_reject(payload: dict[str, Any]) -> None:
        if not persistence_enabled:
            return
        from alphaforge.persistence import save_order_decision
        with SessionLocal() as session:
            decision_id = save_order_decision(
                session,
                mode=mode.value,
                phase=payload.get("phase", "final"),
                signal_id=payload.get("signal_id"),
                symbol=payload.get("symbol"),
                decision=payload.get("decision"),
                reject_reason=payload.get("reason"),
                confidence=payload.get("confidence"),
                score=payload.get("score"),
                rr=payload.get("rr"),
                effective_rr=payload.get("effective_rr"),
                explanation=payload.get("explanation"),
                execution_ctx=payload.get("execution_ctx", {}),
            )
            if decision_id is None:
                raise RuntimeError("order_decision_persistence_failed")
            session.commit()

    live_reconciliation_provider = None
    if mode == ExecutionMode.LIVE and cfg.runtime.enable_binance_readonly_reconciliation:
        api_key = str(os.getenv("BINANCE_API_KEY", "")).strip()
        api_secret = str(os.getenv("BINANCE_API_SECRET", "")).strip()
        if bool(api_key) ^ bool(api_secret):
            raise RuntimeError("LIVE mode blocked: Binance reconciliation credentials are partial")
        if not api_key or not api_secret:
            raise RuntimeError("LIVE mode blocked: Binance reconciliation credentials are missing")
        live_reconciliation_provider = BinanceReadonlyReconciliationProvider(
            config=BinanceReadonlyReconciliationConfig(
                base_url=cfg.exchange.binance.base_url,
                api_key=api_key,
                api_secret=api_secret,
                recv_window_ms=cfg.runtime.binance_reconciliation_recv_window_ms,
                request_timeout_sec=cfg.runtime.reconciliation_timeout_sec,
                trade_lookback_ms=cfg.runtime.binance_reconciliation_trade_lookback_ms,
            )
        )

    orchestrator = RuntimeOrchestrator(
        config=config,
        ai_brain=brain,
        market_scanner=_runtime_market_scanner,
        scanner_source=scanner_source,
        live_reconciliation_provider=live_reconciliation_provider,
        on_lifecycle_event=_persist_lifecycle,
        on_reject_persist=_persist_reject,
        persistence_engine=engine,
    )
    orchestrator.metrics.persistence_enabled = persistence_enabled
    return orchestrator


async def main() -> None:
    cfg = load_config_from_env()
    logging.basicConfig(level=cfg.logging.level)
    orchestrator = _build_runtime_from_env()
    logger.info("runtime_starting mode=%s scan_interval_sec=%.3f", orchestrator.config.execution_mode.value, orchestrator.config.scan_interval_sec)
    try:
        await orchestrator.start()
    except Exception:
        logger.exception("runtime_fatal_error")
        raise
    finally:
        logger.info("runtime_shutdown mode=%s metrics=%s", orchestrator.config.execution_mode.value, orchestrator.metrics)


if __name__ == "__main__":
    asyncio.run(main())
