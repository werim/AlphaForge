from __future__ import annotations

import asyncio
import contextlib
from collections import deque
import hashlib
import json
import logging
import os
import signal
import time
import uuid
import subprocess
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Protocol

from alphaforge.ai_brain import AIBrain
from alphaforge.contracts import LifecycleEventType, canonical_reject_reason, canonical_utc_timestamp, validate_transition
from alphaforge.order import LifecycleState
from alphaforge.execution import build_execution_context, build_execution_cost_model
from alphaforge.live_readiness import LiveReadinessEvaluator, QualificationReport
from alphaforge.runtime_heartbeat import save_runtime_heartbeat
from alphaforge.runtime_control import RuntimeControlStore
from alphaforge.exchange_connectivity import ExchangeHealth, check_required_exchanges_health
from alphaforge.exchange_market_scanner import scan_exchange_markets
from alphaforge.binance_reconciliation_provider import BinanceReadonlyReconciliationConfig, BinanceReadonlyReconciliationProvider
from alphaforge.reconciliation import ReconciliationEngine, persist_findings, summarize_findings
from alphaforge.symbol_selector import SymbolSelectionResult, select_symbols
from alphaforge.persistence import init_db
from alphaforge.burnin import BurnInRun, bootstrap_burnin_schema, config_hash as burnin_config_hash, universe_hash as burnin_universe_hash, persist_burnin_run, persist_burnin_observation, persist_burnin_trade_outcome, update_burnin_run_counters, next_burnin_continuation_sequence
from alphaforge.burnin_qualification import BurnInQualificationEngine
from alphaforge.portfolio_risk import evaluate_portfolio_risk, snapshot_from_state
from alphaforge.runtime_state import RuntimeStateSnapshot, save_runtime_state_snapshot, save_runtime_recovery_event, save_exchange_reconciliation_event, latest_runtime_state_snapshot
from alphaforge.config import load_config_from_env, runtime_filter_config
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)


class ExecutionMode(str, Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE_PRECHECK = "LIVE_PRECHECK"
    LIVE = "LIVE"


class RealExecutionAdapter(Protocol):
    async def submit(self, decision: Mapping[str, Any], market_ctx: Mapping[str, Any]) -> Mapping[str, Any]: ...


class ExchangeSnapshotProvider(Protocol):
    def snapshot(self) -> Mapping[str, Any]: ...


class ObservabilityProbe(Protocol):
    def probe(self) -> Mapping[str, Any]: ...


class RollbackReadinessProbe(Protocol):
    def probe(self) -> Mapping[str, Any]: ...


class LiveReconciliationProvider(ExchangeSnapshotProvider, Protocol):
    pass


@dataclass(slots=True)
class RuntimeConfig:
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    min_signal_score: float = 0.62
    scan_interval_sec: float = 1.0
    heartbeat_interval_sec: float = 30.0
    max_symbols_per_scan: int = 5
    max_reject_log_entries: int = 1000
    max_concurrent_positions: int = 3
    symbol_cooldown_sec: float = 120.0
    max_notional_exposure: float = 100_000.0
    max_symbol_notional: float = 50_000.0
    max_open_positions: int = 3
    max_daily_loss_pct: float = 0.03
    max_rolling_drawdown_pct: float = 0.08
    max_correlation_group_exposure: float = 75_000.0
    max_correlated_positions: int = 2
    reject_unknown_portfolio_risk: bool = True
    stale_market_data_sec: float = 15.0
    min_rr: float = 1.20
    min_effective_rr: float = 1.10
    max_spread_pct: float = 0.0025
    max_expected_slippage_pct: float = 0.0020
    max_abs_funding_rate_pct: float = 0.0010
    min_liquidity_usd: float = 5_000_000.0
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
    pending_order_timeout_sec: float = 300.0
    require_exchange_reconciliation_for_paper: bool = True
    diagnostic_mode: bool = False
    phase7_burnin_release_id: str = "default"
    phase7_burnin_snapshot_interval_sec: float = 300.0


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
    burnin_observations: int = 0
    burnin_outcomes: int = 0
    burnin_snapshots: int = 0
    persistence_enabled: bool = False


@dataclass(slots=True)
class RuntimeOrchestrator:
    config: RuntimeConfig
    ai_brain: AIBrain
    market_scanner: Callable[[], Awaitable[list[dict[str, Any]]]]
    scanner_source: str = "UNKNOWN"
    real_execution_adapter: RealExecutionAdapter | None = None
    live_reconciliation_provider: LiveReconciliationProvider | None = None
    exchange_snapshot_provider: ExchangeSnapshotProvider | None = None
    observability_probe: ObservabilityProbe | None = None
    rollback_readiness_probe: RollbackReadinessProbe | None = None
    on_lifecycle_event: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None
    on_reject_persist: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None
    paper_slippage_bps: float = 2.0
    persistence_engine: Engine | None = None
    control_store: RuntimeControlStore | None = None
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _tasks: list[asyncio.Task[Any]] = field(default_factory=list, init=False)
    _reject_log: deque[dict[str, Any]] = field(init=False)
    metrics: RuntimeMetrics = field(default_factory=RuntimeMetrics, init=False)
    runtime_instance_id: str = field(default_factory=lambda: f"runtime:{uuid.uuid4().hex}", init=False)
    startup_id: str = field(default_factory=lambda: f"startup:{uuid.uuid4().hex}", init=False)
    _runtime_status: str = field(default="INITIALIZING", init=False)
    _last_start_time: str | None = field(default=None, init=False)
    _last_shutdown_time: str | None = field(default=None, init=False)
    _last_error: str | None = field(default=None, init=False)
    _recovery_required: bool = field(default=False, init=False)
    _fail_closed_reason: str | None = field(default=None, init=False)
    _unknown_exchange_state: bool = field(default=False, init=False)
    _reconciliation_status: str = field(default="UNKNOWN", init=False)
    _exchange_read_only_status: str = field(default="UNKNOWN", init=False)
    _unreconciled_symbols: set[str] = field(default_factory=set, init=False)
    _orphan_orders: list[dict[str, Any]] = field(default_factory=list, init=False)
    _orphan_positions: list[dict[str, Any]] = field(default_factory=list, init=False)
    _stale_market_data_symbols: set[str] = field(default_factory=set, init=False)
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
    _live_order_submission_enabled: bool = field(default=False, init=False)
    _mutation_trap_active: bool = field(default=False, init=False)
    _exchange_health: list[ExchangeHealth] = field(default_factory=list, init=False)
    _burnin_run_id: str | None = field(default=None, init=False)
    _burnin_campaign_id: str | None = field(default=None, init=False)
    _burnin_campaign_attached: bool = field(default=False, init=False)
    _burnin_evidence_incomplete: bool = field(default=False, init=False)
    _burnin_suspended: bool = field(default=False, init=False)
    _last_burnin_snapshot_ts: float = field(default=0.0, init=False)
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
        if self._burnin_campaign_id:
            try:
                from alphaforge.burnin_campaign import update_campaign_heartbeat
                with engine.begin() as conn:
                    update_campaign_heartbeat(conn, self._burnin_campaign_id, runtime_status=runtime_state, worker_pid=os.getpid(), burnin_run_id=self._burnin_run_id)
            except Exception as exc:
                self._fail_closed_reason = "PHASE8_CAMPAIGN_PERSISTENCE_FAILURE"
                logger.exception("phase8_campaign_heartbeat_failed", exc_info=exc)

    def _kill_switch_active(self) -> bool:
        if self.config.global_kill_switch:
            return True
        if self.control_store is not None:
            return self.control_store.is_kill_switch_active()
        return False

    def _build_runtime_state_snapshot(self, *, status: str | None = None) -> RuntimeStateSnapshot:
        hb_age = None
        if self.metrics.last_heartbeat_ts:
            hb_age = max(0.0, time.time() - self.metrics.last_heartbeat_ts)
        flags = []
        if self._recovery_required:
            flags.append("RECOVERY_REQUIRED")
        if self._kill_switch_active():
            flags.append("KILL_SWITCH_ACTIVE")
        if self._unknown_exchange_state and self.config.execution_mode != ExecutionMode.BACKTEST:
            flags.append("EXCHANGE_STATE_UNKNOWN")
        if self._exchange_read_only_status == "LOCAL_ONLY":
            flags.append("LOCAL_ONLY_DIAGNOSTIC_RECONCILIATION")
        return RuntimeStateSnapshot(
            mode=self.config.execution_mode.value,
            requested_mode=self.config.execution_mode.value,
            actual_mode=self.config.execution_mode.value,
            runtime_status=status or self._runtime_status,
            heartbeat_age_sec=hb_age,
            instance_id=self.runtime_instance_id,
            startup_id=self.startup_id,
            last_start_time=self._last_start_time,
            last_shutdown_time=self._last_shutdown_time,
            last_error=self._last_error,
            kill_switch_active=self._kill_switch_active(),
            kill_switch_reason="KILL_SWITCH_ACTIVE" if self._kill_switch_active() else None,
            active_symbols=sorted(self._active_positions),
            active_position_count=len(self._active_positions),
            active_positions=[{"symbol": s, "notional": n} for s, n in sorted(self._active_positions.items())],
            pending_order_count=len(self._pending_orders),
            pending_orders=list(self._pending_orders.values()),
            cooldown_symbols=sorted(s for s, until in self._symbol_cooldown_until.items() if until > time.time()),
            stale_market_data_symbols=sorted(self._stale_market_data_symbols),
            unreconciled_symbols=sorted(self._unreconciled_symbols),
            orphan_order_count=len(self._orphan_orders),
            orphan_orders=list(self._orphan_orders),
            orphan_position_count=len(self._orphan_positions),
            orphan_positions=list(self._orphan_positions),
            unknown_exchange_state=self._unknown_exchange_state,
            exchange_connectivity_status="HEALTHY" if self._exchange_health and all(h.connected for h in self._exchange_health) else ("UNKNOWN" if not self._exchange_health else "DEGRADED"),
            exchange_read_only_status=self._exchange_read_only_status,
            reconciliation_status=self._reconciliation_status,
            reconciliation_mismatch_count=len(self._unreconciled_symbols) + len(self._orphan_orders) + len(self._orphan_positions),
            recovery_action_required=self._recovery_required,
            fail_closed_reason=self._fail_closed_reason,
            runtime_flags=flags,
            diagnostics_json={"metrics": self.metrics.__dict__ if hasattr(self.metrics, "__dict__") else str(self.metrics), "diagnostic_mode": self.config.diagnostic_mode, "local_only_reconciliation_override": self._exchange_read_only_status == "LOCAL_ONLY", "burnin_campaign_id": self._burnin_campaign_id, "burnin_run_id": self._burnin_run_id},
        )

    def _persist_runtime_state_snapshot(self, status: str | None = None) -> None:
        engine = self._resolve_persistence_engine()
        if engine is None or not self.metrics.persistence_enabled:
            return
        save_runtime_state_snapshot(engine, self._build_runtime_state_snapshot(status=status))

    @staticmethod
    def _parse_runtime_ts(raw: Any) -> float | None:
        if raw in (None, ""):
            return None
        if isinstance(raw, (int, float)):
            return float(raw)
        text_value = str(raw).strip()
        if not text_value:
            return None
        try:
            return datetime.fromisoformat(text_value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    def _load_recovery_state(self) -> None:
        engine = self._resolve_persistence_engine()
        if engine is None:
            self._recovery_required = True; self._fail_closed_reason = "RUNTIME_DB_UNAVAILABLE"; return
        latest = latest_runtime_state_snapshot(engine)
        if latest and str(latest.get("runtime_status")) not in {"STOPPED", "CLEAN_SHUTDOWN", "STOPPING"}:
            self._recovery_required = True
            self._fail_closed_reason = "UNCLEAN_SHUTDOWN_RECOVERY_REQUIRED"
            save_runtime_recovery_event(engine, instance_id=self.runtime_instance_id, startup_id=self.startup_id, mode=self.config.execution_mode.value, status="RECOVERY_REQUIRED", reason=self._fail_closed_reason, diagnostics={"previous": latest})
        now = time.time()
        with engine.connect() as conn:
            for row in conn.execute(text("SELECT symbol, qty, status FROM positions WHERE UPPER(COALESCE(status,'')) IN ('OPEN','POSITION_OPENED','ACTIVE')")).mappings():
                self._active_positions[str(row['symbol'])] = float(row.get('qty') or 0.0)
            for row in conn.execute(text("SELECT order_id, symbol, status, created_at FROM orders WHERE UPPER(COALESCE(status,'')) IN ('PENDING','OPEN','ORDER_PLACED','ENTRY_SUBMITTED')")).mappings():
                self._pending_orders[str(row['symbol'])] = dict(row)
            for row in conn.execute(text("SELECT symbol, cooldown_remaining_sec FROM cooldown_states WHERE cooldown_remaining_sec > 0")).mappings():
                self._symbol_cooldown_until[str(row['symbol'])] = now + float(row['cooldown_remaining_sec'] or 0)
        stale_orders: list[dict[str, Any]] = []
        for order in self._pending_orders.values():
            created_ts = self._parse_runtime_ts(order.get("created_at"))
            age_sec = None if created_ts is None else max(0.0, now - created_ts)
            order["recovery_age_sec"] = age_sec
            if age_sec is None or age_sec > float(self.config.pending_order_timeout_sec):
                reason = "MISSING_OR_UNPARSEABLE_CREATED_AT" if age_sec is None else "PENDING_ORDER_TIMEOUT_EXCEEDED"
                order["stale_reason"] = reason
                stale_orders.append({"order_id": order.get("order_id"), "symbol": order.get("symbol"), "created_at": order.get("created_at"), "age_sec": age_sec, "timeout_sec": self.config.pending_order_timeout_sec, "reason": reason})
        if stale_orders:
            self._recovery_required = True
            self._fail_closed_reason = self._fail_closed_reason or "STALE_PENDING_ORDER"
            save_runtime_recovery_event(engine, instance_id=self.runtime_instance_id, startup_id=self.startup_id, mode=self.config.execution_mode.value, status="RECOVERY_REQUIRED", reason="STALE_PENDING_ORDER", diagnostics={"stale_pending_orders": stale_orders})
        if self._kill_switch_active():
            self._recovery_required = True; self._fail_closed_reason = self._fail_closed_reason or "KILL_SWITCH_ACTIVE"

    async def start(self) -> None:
        self._last_start_time = canonical_utc_timestamp()
        self._runtime_status = "STARTING"
        if self.config.execution_mode in {ExecutionMode.LIVE, ExecutionMode.LIVE_PRECHECK}:
            allowed_sources = {"EXCHANGE_PUBLIC_MARKET_DATA"}
            scanner_source = str(self.scanner_source or "UNKNOWN").strip().upper()
            if not scanner_source or scanner_source == "UNKNOWN":
                raise RuntimeError("LIVE mode blocked: market scanner provenance is not verified")
            if scanner_source not in allowed_sources:
                raise RuntimeError("LIVE mode blocked: exchange-backed market scanner is required")
        if self.metrics.persistence_enabled:
            self._load_recovery_state()
            self._persist_runtime_state_snapshot("STARTUP")
        if self._kill_switch_active():
            self._runtime_status = "RECOVERY_REQUIRED"
            self._fail_closed_reason = "KILL_SWITCH_ACTIVE"
            self._persist_runtime_state_snapshot("RECOVERY_REQUIRED")
            raise RuntimeError("KILL_SWITCH_ACTIVE")
        if self._recovery_required:
            self._runtime_status = "RECOVERY_REQUIRED"
            self._persist_runtime_state_snapshot("RECOVERY_REQUIRED")
            raise RuntimeError(self._fail_closed_reason or "RUNTIME_RECOVERY_REQUIRED")
        if self.config.execution_mode in {ExecutionMode.LIVE, ExecutionMode.LIVE_PRECHECK}:
            allowed_sources = {"EXCHANGE_PUBLIC_MARKET_DATA"}
            scanner_source = str(self.scanner_source or "UNKNOWN").strip().upper()
            if not scanner_source or scanner_source == "UNKNOWN":
                raise RuntimeError("LIVE mode blocked: market scanner provenance is not verified")
            if scanner_source not in allowed_sources:
                raise RuntimeError("LIVE mode blocked: exchange-backed market scanner is required")
            if self.config.execution_mode == ExecutionMode.LIVE:
                await self._reject_real_live_in_phase6()
            await self._run_live_exchange_connectivity_gate()
            if self.config.execution_mode == ExecutionMode.LIVE_PRECHECK and self.config.require_live_qualification:
                await self._run_live_precheck_qualification_gate()
        if self.config.execution_mode in {ExecutionMode.PAPER, ExecutionMode.LIVE_PRECHECK}:
            self._start_or_resume_burnin_run()
            await self._run_reconciliation_once()
            if self._fail_closed_reason and not self.config.diagnostic_mode:
                self._persist_runtime_state_snapshot("RECOVERY_REQUIRED")
                raise RuntimeError(self._fail_closed_reason)
        elif self.config.execution_mode == ExecutionMode.BACKTEST:
            self._unknown_exchange_state = False
            self._exchange_read_only_status = "NOT_REQUIRED_BACKTEST"
            self._reconciliation_status = "NOT_REQUIRED_BACKTEST"
        self._runtime_status = "OPERATING"
        self._persist_runtime_state_snapshot("OPERATING")
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
            self._runtime_status = "STOPPING"
            self._last_shutdown_time = canonical_utc_timestamp()
            self._finalize_burnin_run(status="COMPLETED")
            self._generate_burnin_snapshot(reason="shutdown")
            self._persist_runtime_heartbeat(runtime_state="STOPPING")
            self._persist_runtime_state_snapshot("CLEAN_SHUTDOWN")
            await self._shutdown_tasks()

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            exc = task.exception()
            if exc is not None:
                logger.exception("runtime_task_failed task=%s", task.get_name(), exc_info=exc)
                self.shutdown()

    def _git_commit(self) -> str:
        try:
            return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=os.getcwd(), text=True, timeout=2).strip()
        except Exception:
            return "UNKNOWN_GIT_COMMIT"


    def _attach_phase8_campaign(self, engine: Engine, campaign_id: str) -> bool:
        try:
            from alphaforge.burnin_campaign import bootstrap_campaign_schema, get_campaign, update_campaign_heartbeat, _exec
            with engine.begin() as conn:
                bootstrap_campaign_schema(conn)
                campaign = get_campaign(conn, campaign_id)
                if not campaign:
                    self._fail_closed_reason = "PHASE8_CAMPAIGN_NOT_FOUND"; return False
                if self.config.execution_mode != ExecutionMode.PAPER:
                    self._fail_closed_reason = "PHASE8_CAMPAIGN_ATTACH_FAILED"; return False
                active_run_id = campaign.get("active_run_id")
                if not active_run_id:
                    self._fail_closed_reason = "PHASE8_CAMPAIGN_ACTIVE_RUN_MISSING"; return False
                row = _exec(conn, "SELECT * FROM burnin_runs WHERE burnin_run_id=:bid", {"bid": active_run_id}).fetchone()
                if row is None:
                    self._fail_closed_reason = "PHASE8_CAMPAIGN_ACTIVE_RUN_MISSING"; return False
                mapping = row if hasattr(row, "keys") else row._mapping
                cfg = self._canonical_filter_config(); symbols = list(cfg.get("symbols") or cfg.get("active_symbols") or []); intervals = list(cfg.get("intervals") or cfg.get("timeframes") or [])
                expected_config = burnin_config_hash(cfg)
                expected_strategy = burnin_config_hash({"min_signal_score": self.config.min_signal_score, "min_effective_rr": self.config.min_effective_rr, "min_rr": self.config.min_rr})
                expected_universe = burnin_universe_hash(symbols, intervals)
                if mapping["config_hash"] != campaign["config_hash"] or mapping["strategy_config_hash"] != campaign["strategy_config_hash"] or mapping["universe_hash"] != campaign["universe_hash"]:
                    self._fail_closed_reason = "PHASE8_CAMPAIGN_CONFIG_DRIFT"; return False
                self._burnin_campaign_id = campaign_id; self._burnin_run_id = str(active_run_id); self._burnin_campaign_attached = True
                update_campaign_heartbeat(conn, campaign_id, runtime_status="ATTACHED", worker_pid=os.getpid(), burnin_run_id=self._burnin_run_id)
                return True
        except Exception as exc:
            self._fail_closed_reason = "PHASE8_CAMPAIGN_PERSISTENCE_FAILURE"
            logger.exception("phase8_campaign_attach_failed", exc_info=exc)
            return False

    def _start_or_resume_burnin_run(self) -> None:
        if self.config.execution_mode not in {ExecutionMode.PAPER, ExecutionMode.LIVE_PRECHECK} or self._burnin_run_id:
            return
        engine = self._resolve_persistence_engine()
        if engine is None:
            self._burnin_evidence_incomplete = True
            if self.config.execution_mode == ExecutionMode.LIVE_PRECHECK:
                self._fail_closed_reason = "PHASE7_BURNIN_PERSISTENCE_UNAVAILABLE"
            return
        campaign_id = os.getenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID")
        if campaign_id:
            if not self._attach_phase8_campaign(engine, campaign_id):
                self._burnin_evidence_incomplete = True
            return
        release_id = os.getenv("ALPHAFORGE_RELEASE_ID", self.config.phase7_burnin_release_id)
        parent_run_id = None
        parent_qualification_id = None
        sequence = None
        if self.config.execution_mode == ExecutionMode.LIVE_PRECHECK:
            try:
                with engine.connect() as conn:
                    row = conn.execute(text("""
                        SELECT q.qualification_id, q.burnin_run_id
                        FROM burnin_qualification_snapshots q
                        JOIN burnin_runs r ON r.burnin_run_id = q.burnin_run_id
                        WHERE q.release_id=:release_id AND q.status='CANARY_QUALIFIED' AND r.execution_mode='PAPER'
                        ORDER BY q.generated_at DESC, q.id DESC LIMIT 1
                    """), {"release_id": release_id}).mappings().first()
                if row is None:
                    self._burnin_evidence_incomplete = True
                    self._fail_closed_reason = "PHASE7_PRIOR_PAPER_QUALIFICATION_REQUIRED"
                    return
                parent_run_id = str(row["burnin_run_id"]); parent_qualification_id = str(row["qualification_id"])
            except Exception as exc:
                self._burnin_evidence_incomplete = True
                self._fail_closed_reason = "PHASE7_PRIOR_PAPER_QUALIFICATION_LOOKUP_FAILED"
                logger.exception("phase7_prior_paper_qualification_lookup_failed", exc_info=exc)
                return
        cfg = self._canonical_filter_config()
        symbols = list(cfg.get("symbols") or cfg.get("active_symbols") or [])
        intervals = list(cfg.get("intervals") or cfg.get("timeframes") or [])
        source = {"provider": self.scanner_source or "UNKNOWN", "scanner_source": self.scanner_source or "UNKNOWN", "runtime_instance_id": self.runtime_instance_id, "parent_burnin_run_id": parent_run_id, "parent_qualification_id": parent_qualification_id}
        try:
            with engine.begin() as conn:
                bootstrap_burnin_schema(conn)
                sequence = next_burnin_continuation_sequence(conn, release_id=release_id, execution_mode=self.config.execution_mode.value)
                self._burnin_run_id = f"phase7:{release_id}:{self.config.execution_mode.value}:{sequence}"
                run = BurnInRun(
                    burnin_run_id=self._burnin_run_id,
                    release_id=release_id,
                    execution_mode=self.config.execution_mode.value,
                    parent_burnin_run_id=parent_run_id,
                    parent_qualification_id=parent_qualification_id,
                    continuation_sequence=sequence,
                    git_commit=self._git_commit(),
                    config_hash=burnin_config_hash(cfg),
                    strategy_config_hash=burnin_config_hash({"min_signal_score": self.config.min_signal_score, "min_effective_rr": self.config.min_effective_rr, "min_rr": self.config.min_rr}),
                    universe_hash=burnin_universe_hash(symbols, intervals),
                    source_provenance=source,
                    symbols=symbols,
                    intervals=intervals,
                )
                persist_burnin_run(conn, run)
        except Exception as exc:
            self._burnin_evidence_incomplete = True
            self._fail_closed_reason = "PHASE7_BURNIN_PERSISTENCE_FAILURE"
            logger.exception("phase7_burnin_run_persistence_failed", exc_info=exc)

    def _phase7_costs_from_execution_ctx(self, execution_ctx: Mapping[str, Any]) -> dict[str, Any]:
        spread = execution_ctx.get("spread_pct")
        slip = execution_ctx.get("expected_slippage_pct")
        funding = execution_ctx.get("funding_rate_pct")
        latency_ms = execution_ctx.get("market_data_latency_ms")
        return {
            "spread_cost": None if spread is None else abs(float(spread)),
            "entry_slippage_cost": None if slip is None else abs(float(slip)) / 2.0,
            "exit_slippage_cost": None if slip is None else abs(float(slip)) / 2.0,
            "fee_cost": execution_ctx.get("fee_pct"),
            "funding_cost": None if funding is None else abs(float(funding)),
            "latency_cost": None if latency_ms is None else abs(float(latency_ms)) / 1_000_000.0,
            "volatility_penalty": execution_ctx.get("volatility_penalty_pct"),
            "liquidity_penalty": execution_ctx.get("liquidity_penalty_pct"),
        }

    def _persist_burnin_decision(self, payload: Mapping[str, Any], *, lifecycle_state: str | None = None) -> None:
        if self.config.execution_mode not in {ExecutionMode.PAPER, ExecutionMode.LIVE_PRECHECK}:
            return
        if not self._burnin_run_id:
            self._start_or_resume_burnin_run()
        engine = self._resolve_persistence_engine()
        if engine is None or not self._burnin_run_id:
            self._burnin_evidence_incomplete = True
            return
        try:
            execution_ctx = dict(payload.get("execution_ctx") or {})
            missing = [name for name in ("signal_id", "symbol", "decision") if not payload.get(name)]
            with engine.begin() as conn:
                persist_burnin_observation(conn, observation_id=f"obs:{payload.get('signal_id')}:{payload.get('decision')}:{canonical_utc_timestamp()}", burnin_run_id=self._burnin_run_id, release_id=os.getenv("ALPHAFORGE_RELEASE_ID", self.config.phase7_burnin_release_id), execution_mode=self.config.execution_mode.value, symbol=payload.get("symbol"), interval=payload.get("timeframe"), regime=payload.get("regime") or execution_ctx.get("volatility_regime") or payload.get("volatility_regime") or "UNKNOWN", decision=payload.get("decision"), lifecycle_state=lifecycle_state, metrics={**{k: payload.get(k) for k in ("score","rr","effective_rr","confidence","spread_pct","expected_slippage_pct","latency_ms","funding_rate_pct")}, "campaign_id": self._burnin_campaign_id, "execution_ctx": execution_ctx}, source_provenance={"provider": self.scanner_source or "UNKNOWN", "campaign_id": self._burnin_campaign_id}, missing_fields=missing)
            self.metrics.burnin_observations += 1
            with engine.begin() as conn:
                update_burnin_run_counters(conn, self._burnin_run_id)
                if self._burnin_campaign_id:
                    from alphaforge.burnin_campaign import update_campaign_heartbeat
                    update_campaign_heartbeat(conn, self._burnin_campaign_id, runtime_status=self._runtime_status, worker_pid=os.getpid(), burnin_run_id=self._burnin_run_id)
        except Exception as exc:
            self._burnin_evidence_incomplete = True
            self._fail_closed_reason = "PHASE7_BURNIN_PERSISTENCE_FAILURE"
            logger.exception("phase7_burnin_decision_persistence_failed", exc_info=exc)

    def _persist_burnin_trade_outcome(self, symbol: str, decision: Mapping[str, Any], market_ctx: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        """Deprecated guard: entry fills/open positions are not realized burn-in outcomes."""
        self._burnin_evidence_incomplete = True
        logger.warning("phase7_ignored_non_closed_trade_outcome symbol=%s status=%s", symbol, result.get("status"))
        return


    def _persist_burnin_closed_trade_from_lifecycle(self, symbol: str, details: Mapping[str, Any]) -> None:
        if self.config.execution_mode not in {ExecutionMode.PAPER, ExecutionMode.LIVE_PRECHECK}:
            return
        if not self._burnin_run_id:
            self._start_or_resume_burnin_run()
        engine = self._resolve_persistence_engine()
        if engine is None or not self._burnin_run_id:
            self._burnin_evidence_incomplete = True
            return
        required = ("gross_pnl", "gross_r", "net_pnl", "net_r", "exit_reason", "mfe", "mae", "hold_duration_seconds")
        if any(details.get(k) is None for k in required):
            self._burnin_evidence_incomplete = True
        costs = {
            "spread_cost": details.get("entry_spread_cost"),
            "entry_slippage_cost": details.get("entry_slippage_cost"),
            "exit_slippage_cost": details.get("exit_slippage_cost"),
            "fee_cost": details.get("fee_cost"),
            "funding_cost": details.get("funding_cost"),
            "latency_cost": details.get("latency_cost"),
            "volatility_penalty": details.get("volatility_penalty"),
            "liquidity_penalty": details.get("liquidity_penalty"),
        }
        try:
            with engine.begin() as conn:
                if self._burnin_campaign_id and details.get("trade_id") and details.get("exit_price") is not None:
                    from alphaforge.burnin_resolver import resolve_position_closure
                    resolve_position_closure(conn, trade_id=str(details.get("trade_id")), exit_time=str(details.get("exit_time") or details.get("closed_at") or canonical_utc_timestamp()), exit_price=float(details.get("exit_price")), exit_reason=str(details.get("exit_reason") or "UNKNOWN"), exit_costs={"exit_spread": details.get("exit_spread_cost"), "exit_slippage": details.get("exit_slippage_cost"), "exit_fee": details.get("exit_fee_cost") or details.get("fee_cost"), "funding": details.get("funding_cost"), "latency_impact_penalty": details.get("latency_cost")}, mfe=details.get("mfe"), mae=details.get("mae"))
                else:
                    persist_burnin_trade_outcome(conn, outcome_id=str(details.get("outcome_id") or f"out:{symbol}:{details.get('trade_id') or canonical_utc_timestamp()}"), burnin_run_id=self._burnin_run_id, release_id=os.getenv("ALPHAFORGE_RELEASE_ID", self.config.phase7_burnin_release_id), symbol=symbol, regime=str(details.get("regime") or "UNKNOWN"), trade_id=details.get("trade_id"), gross_r=details.get("gross_r"), gross_pnl=details.get("gross_pnl"), costs=costs, net_r=details.get("net_r"), net_pnl=details.get("net_pnl"), effective_rr_at_entry=details.get("effective_rr_at_entry"), realized_effective_rr=details.get("realized_effective_rr"), hold_duration_seconds=details.get("hold_duration_seconds"), mfe=details.get("mfe"), mae=details.get("mae"), exit_reason=details.get("exit_reason"), payload=dict(details))
                update_burnin_run_counters(conn, self._burnin_run_id)
            self.metrics.burnin_outcomes += 1
        except Exception as exc:
            self._burnin_evidence_incomplete = True
            self._fail_closed_reason = "PHASE7_BURNIN_PERSISTENCE_FAILURE"
            logger.exception("phase7_burnin_closed_trade_persistence_failed", exc_info=exc)

    def _persist_burnin_periodic_metrics(self) -> None:
        if self.config.execution_mode not in {ExecutionMode.PAPER, ExecutionMode.LIVE_PRECHECK} or not self._burnin_run_id:
            return
        engine = self._resolve_persistence_engine()
        if engine is None:
            return
        now = canonical_utc_timestamp()
        try:
            with engine.begin() as conn:
                conn.execute(text("""INSERT INTO burnin_execution_metrics(burnin_run_id,release_id,metric_window,spread_baseline,spread_current,slippage_baseline,slippage_current,latency_baseline,latency_current,fill_probability_baseline,fill_probability_current,timeout_rate,execution_rejects,stale_data_count,reconciliation_quality,status,generated_at,schema_version) VALUES (:bid,:rel,'CURRENT',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,0,:rejects,:stale,:recon,:status,:ts,'phase7_burnin_v1')"""), {"bid": self._burnin_run_id, "rel": os.getenv("ALPHAFORGE_RELEASE_ID", self.config.phase7_burnin_release_id), "rejects": self.metrics.rejects_persisted, "stale": len(self._stale_market_data_symbols), "recon": self._reconciliation_status, "status": "STABLE" if self._reconciliation_status == "CLEAN" else "INSUFFICIENT_EVIDENCE", "ts": now})
                conn.execute(text("""INSERT INTO burnin_drawdown_events(drawdown_event_id,burnin_run_id,release_id,peak_equity,trough_equity,drawdown_pct,consecutive_losses,rolling_expectancy,resolved,payload_json,schema_version) VALUES (:id,:bid,:rel,NULL,NULL,0,0,NULL,1,:payload,'phase7_burnin_v1')"""), {"id": f"dd:{self._burnin_run_id}:{now}", "bid": self._burnin_run_id, "rel": os.getenv("ALPHAFORGE_RELEASE_ID", self.config.phase7_burnin_release_id), "payload": json.dumps({"runtime_status": self._runtime_status})})
        except Exception as exc:
            self._burnin_evidence_incomplete = True
            self._fail_closed_reason = "PHASE7_BURNIN_PERSISTENCE_FAILURE"
            logger.exception("phase7_burnin_metric_persistence_failed", exc_info=exc)

    def _generate_burnin_snapshot(self, *, reason: str = "periodic") -> None:
        if self.config.execution_mode not in {ExecutionMode.PAPER, ExecutionMode.LIVE_PRECHECK} or not self._burnin_run_id:
            return
        if reason == "periodic" and time.time() - self._last_burnin_snapshot_ts < self.config.phase7_burnin_snapshot_interval_sec:
            return
        self._persist_burnin_periodic_metrics()
        engine = self._resolve_persistence_engine()
        if engine is None:
            self._burnin_evidence_incomplete = True
            return
        try:
            snap = BurnInQualificationEngine(engine).evaluate(self._burnin_run_id)
            self.metrics.burnin_snapshots += 1
            self._last_burnin_snapshot_ts = time.time()
            safety_blockers = {"MUTATION_ATTEMPT_DETECTED", "RECONCILIATION_NOT_CLEAN", "OPERATOR_ACK_MISSING_OR_EXPIRED", "ROLLBACK_NOT_VERIFIED", "RUNBOOK_NOT_VERIFIED", "PERSISTENCE_FAILURE"}
            should_stop = snap.status == "CANARY_SUSPENDED" or (self.config.execution_mode == ExecutionMode.LIVE_PRECHECK and bool(safety_blockers.intersection(set(snap.blockers))))
            if should_stop:
                self._burnin_suspended = True
                self._fail_closed_reason = "PHASE7_CANARY_SUSPENDED" if snap.status == "CANARY_SUSPENDED" else "PHASE7_CANARY_SAFETY_BLOCKER"
                self._runtime_status = "STOPPING"
                self._persist_runtime_heartbeat(runtime_state="STOPPING")
                self._persist_runtime_state_snapshot("STOPPING")
                self.shutdown()
        except Exception as exc:
            self._burnin_evidence_incomplete = True
            self._fail_closed_reason = "PHASE7_BURNIN_QUALIFICATION_FAILURE"
            logger.exception("phase7_burnin_snapshot_failed", exc_info=exc)
            if self.config.execution_mode == ExecutionMode.LIVE_PRECHECK:
                self.shutdown()

    def _finalize_burnin_run(self, *, status: str) -> None:
        if not self._burnin_run_id:
            return
        engine = self._resolve_persistence_engine()
        if engine is None:
            return
        try:
            with engine.begin() as conn:
                update_burnin_run_counters(conn, self._burnin_run_id, status=status, end_time=canonical_utc_timestamp())
        except Exception as exc:
            self._burnin_evidence_incomplete = True
            self._fail_closed_reason = "PHASE7_BURNIN_COUNTER_UPDATE_FAILED"
            logger.exception("phase7_burnin_finalize_failed", exc_info=exc)

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

    async def _reject_real_live_in_phase6(self) -> None:
        self._live_order_submission_enabled = False
        self._runtime_status = "STOPPING"
        self._fail_closed_reason = "LIVE_REAL_ORDERS_DISABLED_IN_PHASE6"
        self._persist_runtime_heartbeat(runtime_state="STOPPING")
        self._persist_runtime_state_snapshot("STOPPING")
        raise RuntimeError("LIVE_REAL_ORDERS_DISABLED_IN_PHASE6")

    async def _run_live_qualification_gate(self) -> None:
        if self.config.execution_mode == ExecutionMode.LIVE:
            await self._reject_real_live_in_phase6()
        await self._run_live_precheck_qualification_gate()

    async def _run_live_precheck_qualification_gate(self) -> None:
        if self.config.execution_mode == ExecutionMode.LIVE:
            await self._reject_real_live_in_phase6()
        self._live_order_submission_enabled = False
        self._mutation_trap_active = True
        engine = self._resolve_persistence_engine()
        if engine is None:
            raise RuntimeError("LIVE qualification requires runtime persistence engine")
        evaluator = LiveReadinessEvaluator(engine)
        mode_parity = self._build_mode_parity_evidence(min_sample_count=3)
        readiness_inputs: dict[str, dict[str, Any]] = {
            "mode_parity": self._readiness_input_metadata("mode_parity", self, mode_parity),
        }
        snapshot_provider = self.exchange_snapshot_provider or self.live_reconciliation_provider
        reconciliation_snapshot = self._missing_readiness_input("exchange_snapshot", "EXCHANGE_SNAPSHOT_PROVIDER_MISSING")
        if snapshot_provider is not None:
            provider_snapshot = dict(snapshot_provider.snapshot())
            readiness_inputs["exchange_snapshot"] = self._readiness_input_metadata("exchange_snapshot", snapshot_provider, provider_snapshot)
            provider_snapshot = self._reject_synthetic_live_input(provider_snapshot)
            evidence_status = str(provider_snapshot.get("evidence_status") or "INCOMPLETE").upper()
            reconciliation_snapshot = {"provider_configured": True, **provider_snapshot, "evidence_status": evidence_status}
            if evidence_status == "COMPLETE":
                snapshot = self._reconciliation_engine.snapshot_from_source(provider_snapshot)
                findings, _recommendations, _metrics = self._reconciliation_engine.reconcile(
                    intended_orders=[] if self._exchange_read_only_status == "LOCAL_ONLY" else list(self._pending_orders.values()),
                    lifecycle_state_by_symbol=self._last_lifecycle_state_by_symbol,
                    snapshot=snapshot,
                    mode=ExecutionMode.LIVE.value,
                )
                reconciliation_snapshot.update(summarize_findings(findings))
        else:
            readiness_inputs["exchange_snapshot"] = dict(reconciliation_snapshot)

        observability_snapshot = self._missing_readiness_input("observability", "OBSERVABILITY_PROBE_MISSING")
        if self.observability_probe is not None:
            probed = self._reject_synthetic_live_input(dict(self.observability_probe.probe()))
            readiness_inputs["observability"] = self._readiness_input_metadata("observability", self.observability_probe, probed)
            observability_snapshot = {"provider_configured": True, **probed}
        else:
            readiness_inputs["observability"] = dict(observability_snapshot)

        rollback_snapshot = self._missing_readiness_input("rollback", "ROLLBACK_READINESS_PROBE_MISSING")
        if self.rollback_readiness_probe is not None:
            probed = self._reject_synthetic_live_input(dict(self.rollback_readiness_probe.probe()))
            readiness_inputs["rollback"] = self._readiness_input_metadata("rollback", self.rollback_readiness_probe, probed)
            rollback_snapshot = {"provider_configured": True, **probed}
        else:
            readiness_inputs["rollback"] = dict(rollback_snapshot)
        observability_snapshot = {**observability_snapshot, **rollback_snapshot}
        report = evaluator.evaluate(
            mode_parity=mode_parity,
            reconciliation_snapshot=reconciliation_snapshot,
            observability_snapshot=observability_snapshot,
            canary_enabled=self.config.enable_canary_mode,
            shadow_mode_enabled=self.config.enable_shadow_mode,
            operator_ack=self.config.operator_live_acknowledged,
            kill_switch_active=self._kill_switch_active(),
        )
        report.readiness_inputs = readiness_inputs
        evaluator.persist_report(report)
        self._qualification_report = report
        logger.warning("live_readiness_report=%s", report.to_dict())
        allowed_non_mutating_verdicts = {"LIVE_REAL_ORDERS_BLOCKED", "CANARY_READY"}
        if self._live_order_submission_enabled:
            self._persist_runtime_heartbeat(runtime_state="STOPPING")
            raise RuntimeError("LIVE_PRECHECK blocked: live_order_submission_enabled must remain false")
        if not self._mutation_trap_active:
            self._persist_runtime_heartbeat(runtime_state="STOPPING")
            raise RuntimeError("LIVE_PRECHECK blocked: mutation trap is not active")
        if report.verdict not in allowed_non_mutating_verdicts:
            self._persist_runtime_heartbeat(runtime_state="STOPPING")
            raise RuntimeError(f"LIVE_PRECHECK blocked: readiness qualification failed; verdict {report.verdict} is not a non-mutating Phase 6 verdict")


    @staticmethod
    def _readiness_input_metadata(name: str, provider: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "name": name,
            "source": str(payload.get("input_source") or payload.get("evidence_source") or payload.get("observability_evidence_source") or payload.get("rollback_evidence_source") or provider.__class__.__name__),
            "type": str(payload.get("input_type") or payload.get("provider_type") or provider.__class__.__name__),
            "timestamp": str(payload.get("input_timestamp") or payload.get("generated_at") or payload.get("recorded_at") or canonical_utc_timestamp()),
        }

    @staticmethod
    def _missing_readiness_input(name: str, reason: str) -> dict[str, Any]:
        return {"name": name, "provider_configured": False, "evidence_status": "INCOMPLETE", "input_source": "MISSING", "input_type": "MISSING", "input_timestamp": canonical_utc_timestamp(), "blocking_reasons": [reason]}

    @staticmethod
    def _reject_synthetic_live_input(payload: dict[str, Any]) -> dict[str, Any]:
        source = str(payload.get("input_source") or payload.get("evidence_source") or payload.get("observability_evidence_source") or payload.get("rollback_evidence_source") or "").upper()
        input_type = str(payload.get("input_type") or payload.get("provider_type") or "").upper()
        synthetic = bool(payload.get("synthetic", False)) or source in {"SYNTHETIC", "FIXTURE", "DETERMINISTIC_FIXTURE"} or input_type in {"SYNTHETIC", "FIXTURE", "DETERMINISTIC_FIXTURE"}
        if synthetic:
            reasons = list(payload.get("blocking_reasons") or [])
            reasons.append("SYNTHETIC_LIVE_READINESS_INPUT")
            payload.update({"evidence_status": "INCOMPLETE", "blocking_reasons": reasons})
        return payload

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
            "reject_reason": canonical_reject_reason(order_plan.reason) if order_plan.decision != "ACCEPTED" else "",
            "raw_rr": float(signal_payload.get("risk_reward", signal_payload.get("rr", 0.0)) or 0.0),
            "effective_rr": self._effective_rr_from_execution(signal_payload.get("risk_reward", signal_payload.get("rr", 0.0)), market_ctx.get("execution_ctx", market_ctx)),
            "explanation": explanation,
        }

    @staticmethod
    def _effective_rr_from_execution(raw_rr: Any, execution_ctx: Mapping[str, Any]) -> float:
        rr = float(raw_rr or 0.0)
        model = build_execution_cost_model(execution_ctx, include_missing_penalty=False)
        return round(max(rr - model.total_penalty, 0.0), 6)

    def _build_mode_parity_evidence(self, *, min_sample_count: int = 3) -> dict[str, Any]:
        samples = list(self._qualification_samples[: max(0, int(min_sample_count))])
        comparisons: list[dict[str, Any]] = []
        mismatch_count = 0
        missing_field_count = 0
        compare_fields = ("decision", "reject_reason", "order_type", "confidence", "score", "raw_rr", "effective_rr", "explanation")
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
            execution_ctx = build_execution_context(sample)
            normalized_market = {**sample, "execution_ctx": execution_ctx}
            paper_eval = self._evaluate_pre_submit(paper_signal_payload, {**normalized_market, "mode": "PAPER"}, regime_ctx, stats_ctx)
            live_eval = self._evaluate_pre_submit(live_precheck_signal_payload, {**normalized_market, "mode": "LIVE_PRECHECK"}, regime_ctx, stats_ctx)
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
                "input_snapshot_hash": self._snapshot_hash({"signal": paper_signal_payload, "market": normalized_market, "regime": regime_ctx, "stats": stats_ctx}),
                "symbol": sample.get("symbol"),
                "timestamp": canonical_utc_timestamp(sample.get("market_ts")),
                "execution_context": execution_ctx,
                "no_submit_verified": True,
                "parity_result": "PASS" if not missing and not mismatch else "FAIL",
            })
        return {
            "evidence_status": "COMPLETE" if samples and mismatch_count == 0 and missing_field_count == 0 else "INCOMPLETE",
            "sample_count": len(samples),
            "min_sample_count": int(min_sample_count),
            "mismatch_count": mismatch_count,
            "missing_field_count": missing_field_count,
            "no_order_submission_verified": True,
            "no_submit_verified": True,
            "execution_context_complete": all(str(c.get("execution_context", {}).get("evidence_status", "")).upper() not in {"", "UNAVAILABLE", "UNKNOWN"} for c in comparisons),
            "comparison_fields": list(compare_fields),
            "samples": comparisons,
            "generated_at": canonical_utc_timestamp(),
        }

    @staticmethod
    def _snapshot_hash(payload: Mapping[str, Any]) -> str:
        import json
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()

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

    def _canonical_filter_config(self) -> dict[str, Any]:
        return runtime_filter_config(self.config, mode=self.config.execution_mode.value)

    async def _scan_once(self) -> None:
        if self._kill_switch_active():
            self._last_scan_gate_blockers = ["KILL_SWITCH_ACTIVE"]
            return
        self.metrics.scans += 1
        candidates = await self.market_scanner()
        self.metrics.last_scan_ts = canonical_utc_timestamp()
        pre_selection = select_symbols(candidates, {**self._canonical_filter_config(), "include_rejected": True})
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
        raw_rr = market_ctx.get("rr")
        effective_rr = self._effective_rr_from_execution(raw_rr, execution_ctx)
        risk_reject = self._evaluate_runtime_risk(selection.symbol, market_ctx)
        await self._emit_lifecycle_event(LifecycleState.SIGNAL_CREATED.value, selection.symbol, {"reason": "", "signal_id": signal_id})
        if self._kill_switch_active():
            reject_payload = {"signal_id": signal_id, "symbol": selection.symbol, "mode": self.config.execution_mode.value, "phase": "final", "decision": "REJECTED", "reason": "KILL_SWITCH_ACTIVE", "confidence": 0.0, "score": 0.0, "rr": raw_rr, "effective_rr": effective_rr, "explanation": "runtime_control_gate", "execution_ctx": execution_ctx, "spread_pct": execution_ctx.get("spread_pct"), "expected_slippage_pct": execution_ctx.get("expected_slippage_pct"), "latency_ms": execution_ctx.get("market_data_latency_ms"), "funding_rate_pct": execution_ctx.get("funding_rate_pct"), "orderbook_imbalance": execution_ctx.get("orderbook_imbalance"), "volatility_regime": execution_ctx.get("volatility_regime")}
            await self._persist_reject(reject_payload)
            await self._emit_lifecycle_event(LifecycleState.SIGNAL_REJECTED.value, selection.symbol, {**reject_payload, "reject_reason": "KILL_SWITCH_ACTIVE"})
            return
        if risk_reject is not None:
            reject_payload = {"signal_id": signal_id, "symbol": selection.symbol, "mode": self.config.execution_mode.value, "phase": "final", "decision": "REJECTED", "reason": risk_reject, "confidence": 0.0, "score": 0.0, "rr": raw_rr, "effective_rr": effective_rr, "explanation": "runtime_risk_gate", "execution_ctx": execution_ctx, "spread_pct": execution_ctx.get("spread_pct"), "expected_slippage_pct": execution_ctx.get("expected_slippage_pct"), "latency_ms": execution_ctx.get("market_data_latency_ms"), "funding_rate_pct": execution_ctx.get("funding_rate_pct"), "orderbook_imbalance": execution_ctx.get("orderbook_imbalance"), "volatility_regime": execution_ctx.get("volatility_regime")}
            await self._persist_reject(reject_payload)
            await self._emit_lifecycle_event(LifecycleState.SIGNAL_REJECTED.value, selection.symbol, {**reject_payload, "reject_reason": risk_reject})
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

        if self._kill_switch_active():
            reject_payload = {"signal_id": signal_id, "symbol": selection.symbol, "mode": self.config.execution_mode.value, "phase": "final", "decision": "REJECTED", "reason": "KILL_SWITCH_ACTIVE", "confidence": order_plan.confidence, "score": getattr(score_ctx, "total_score", None), "rr": signal_payload.get("risk_reward"), "effective_rr": effective_rr, "explanation": "runtime_control_gate", "execution_ctx": execution_ctx, "spread_pct": execution_ctx.get("spread_pct"), "expected_slippage_pct": execution_ctx.get("expected_slippage_pct"), "latency_ms": execution_ctx.get("market_data_latency_ms"), "funding_rate_pct": execution_ctx.get("funding_rate_pct"), "orderbook_imbalance": execution_ctx.get("orderbook_imbalance"), "volatility_regime": execution_ctx.get("volatility_regime")}
            await self._persist_reject(reject_payload)
            await self._emit_lifecycle_event(LifecycleState.SIGNAL_REJECTED.value, selection.symbol, {**reject_payload, "reject_reason": "KILL_SWITCH_ACTIVE"})
            return

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
                "effective_rr": effective_rr,
                "explanation": explanation,
                "execution_ctx": execution_ctx,
                "spread_pct": execution_ctx.get("spread_pct"),
                "expected_slippage_pct": execution_ctx.get("expected_slippage_pct"),
                "latency_ms": execution_ctx.get("market_data_latency_ms"),
                "funding_rate_pct": execution_ctx.get("funding_rate_pct"),
                "orderbook_imbalance": execution_ctx.get("orderbook_imbalance"),
                "volatility_regime": execution_ctx.get("volatility_regime"),
            })
            await self._emit_lifecycle_event(LifecycleState.SIGNAL_REJECTED.value, selection.symbol, {
                "reason": reject_reason,
                "reject_reason": reject_reason,
                "decision": "REJECTED",
                "signal_id": signal_id,
                "score": getattr(score_ctx, "total_score", None),
                "rr": signal_payload.get("risk_reward"),
                "effective_rr": effective_rr,
                "execution_ctx": execution_ctx,
            })
            return

        if effective_rr < self.config.min_effective_rr:
            reject_reason = "LOW_EFFECTIVE_RR"
            reject_payload = {"signal_id": signal_id, "symbol": selection.symbol, "mode": self.config.execution_mode.value, "phase": "final", "decision": "REJECTED", "reason": reject_reason, "confidence": order_plan.confidence, "score": getattr(score_ctx, "total_score", None), "rr": signal_payload.get("risk_reward"), "effective_rr": effective_rr, "explanation": "canonical_effective_rr_gate", "execution_ctx": execution_ctx, "spread_pct": execution_ctx.get("spread_pct"), "expected_slippage_pct": execution_ctx.get("expected_slippage_pct"), "latency_ms": execution_ctx.get("market_data_latency_ms"), "funding_rate_pct": execution_ctx.get("funding_rate_pct"), "orderbook_imbalance": execution_ctx.get("orderbook_imbalance"), "volatility_regime": execution_ctx.get("volatility_regime")}
            await self._persist_reject(reject_payload)
            await self._emit_lifecycle_event(LifecycleState.SIGNAL_REJECTED.value, selection.symbol, {**reject_payload, "reject_reason": reject_reason})
            return

        candidate_notional = market_ctx.get("notional") or market_ctx.get("notional_usdt") or market_ctx.get("order_notional")
        if candidate_notional is None:
            candidate_notional = min(float(self.config.max_symbol_notional or 0.0), float(self.config.max_notional_exposure or 0.0)) * 0.1
        inferred_equity = market_ctx.get("equity", market_ctx.get("available_balance"))
        snapshot = snapshot_from_state(
            mode=self.config.execution_mode.value,
            symbol=selection.symbol,
            side=str(market_ctx.get("side", signal_payload.get("side", "LONG"))),
            candidate_notional=candidate_notional,
            equity=inferred_equity,
            available_balance=market_ctx.get("available_balance", inferred_equity),
            open_positions={k: {"notional": v, "side": "LONG"} for k, v in self._active_positions.items()},
            config=self.config,
            now=time.time(),
            cooldown_until=self._symbol_cooldown_until,
        )
        portfolio_decision = evaluate_portfolio_risk({"symbol": selection.symbol, "side": market_ctx.get("side"), "entry": market_ctx.get("entry"), "quantity": market_ctx.get("quantity", market_ctx.get("qty")), "notional": candidate_notional}, snapshot, self.config, mode=self.config.execution_mode.value)
        if not portfolio_decision.accepted:
            reject_reason = portfolio_decision.reject_reason or "UNKNOWN_PORTFOLIO_RISK"
            reject_payload = {"signal_id": signal_id, "symbol": selection.symbol, "mode": self.config.execution_mode.value, "phase": "final", "decision": "REJECTED", "reason": reject_reason, "reject_reason": reject_reason, "confidence": order_plan.confidence, "score": getattr(score_ctx, "total_score", None), "rr": signal_payload.get("risk_reward"), "effective_rr": effective_rr, "explanation": "portfolio_risk_gate", "execution_ctx": execution_ctx, "portfolio_reject_reason": reject_reason, "portfolio_risk_state": portfolio_decision.risk_state, "portfolio_diagnostics": portfolio_decision.diagnostics, "risk_flags": portfolio_decision.risk_flags, "spread_pct": execution_ctx.get("spread_pct"), "expected_slippage_pct": execution_ctx.get("expected_slippage_pct"), "latency_ms": execution_ctx.get("market_data_latency_ms"), "funding_rate_pct": execution_ctx.get("funding_rate_pct"), "orderbook_imbalance": execution_ctx.get("orderbook_imbalance"), "volatility_regime": execution_ctx.get("volatility_regime")}
            await self._persist_reject(reject_payload)
            await self._emit_lifecycle_event(LifecycleState.SIGNAL_REJECTED.value, selection.symbol, reject_payload)
            return

        if self.config.execution_mode in {ExecutionMode.PAPER, ExecutionMode.LIVE_PRECHECK}:
            await self._emit_lifecycle_event(LifecycleState.WAITING_ENTRY_ZONE.value, selection.symbol, {})
            await self._emit_lifecycle_event(LifecycleState.ENTRY_TRIGGERED.value, selection.symbol, {})
            await self._emit_lifecycle_event(LifecycleState.ORDER_PLACED.value, selection.symbol, {})
        else:
            await self._emit_lifecycle_event(LifecycleEventType.ENTRY_PENDING.value, selection.symbol, {})
            await self._emit_lifecycle_event(LifecycleEventType.ENTRY_SUBMITTED.value, selection.symbol, {})
        if self.config.execution_mode == ExecutionMode.LIVE_PRECHECK:
            await self._persist_live_precheck_evidence(selection.symbol, signal_payload, market_ctx, regime_ctx, stats_ctx, score_ctx, order_plan, explanation, effective_rr)
            self._persist_burnin_decision({"signal_id": signal_id, "symbol": selection.symbol, "mode": self.config.execution_mode.value, "decision": "ACCEPTED", "score": getattr(score_ctx, "total_score", None), "rr": signal_payload.get("risk_reward"), "effective_rr": effective_rr, "confidence": order_plan.confidence, "execution_ctx": execution_ctx}, lifecycle_state=LifecycleState.ORDER_PLACED.value)
            self._generate_burnin_snapshot(reason="periodic")
            return

        self._persist_burnin_decision({"signal_id": signal_id, "symbol": selection.symbol, "mode": self.config.execution_mode.value, "decision": "ACCEPTED", "score": getattr(score_ctx, "total_score", None), "rr": signal_payload.get("risk_reward"), "effective_rr": effective_rr, "confidence": order_plan.confidence, "execution_ctx": execution_ctx}, lifecycle_state=LifecycleState.ORDER_PLACED.value)
        await self._execute(symbol=selection.symbol, decision={
            "order_type": order_plan.order_type,
            "limit_price": order_plan.limit_price,
            "stop_price": order_plan.stop_price,
            "confidence": order_plan.confidence,
        }, market_ctx=market_ctx)

    async def _execute(self, symbol: str, decision: dict[str, Any], market_ctx: Mapping[str, Any]) -> None:
        if self._kill_switch_active():
            raise RuntimeError("KILL_SWITCH_ACTIVE")
        mode = self.config.execution_mode
        if mode == ExecutionMode.PAPER:
            result = self._simulate_paper_execution(symbol, decision, market_ctx)
        elif mode == ExecutionMode.LIVE_PRECHECK:
            result = {"mode": mode.value, "status": "no_submit_verified", "symbol": symbol}
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
        if result_status == "no_submit_verified":
            return
        if result_status == "partial_fill":
            await self._emit_lifecycle_event(LifecycleState.POSITION_OPENED.value, symbol, {"result": dict(result), "fill_state": "partial"})
        elif result_status in {"rejected", "exchange_reject"}:
            await self._emit_lifecycle_event(LifecycleState.ORDER_REJECTED.value, symbol, {"reason": "exchange_rejected_order", "result": dict(result)})
            return
        elif result_status in {"timeout", "error", "missing_ack"}:
            await self._record_incident(symbol, LifecycleState.ENTRY_TIMEOUT.value, "execution_uncertain_state")
            await self._reconcile_symbol_state(symbol, result, market_ctx)
            return
        if self._burnin_campaign_id and self._burnin_run_id and result_status in {"filled", "partial_fill"}:
            try:
                engine = self._resolve_persistence_engine()
                if engine is not None:
                    from alphaforge.burnin_resolver import persist_pending_position
                    exec_ctx = dict(market_ctx.get("execution_ctx") or {})
                    with engine.begin() as conn:
                        persist_pending_position(conn, trade_id=order_id, campaign_id=self._burnin_campaign_id, burnin_run_id=self._burnin_run_id, signal_id=market_ctx.get("signal_id"), symbol=symbol, side=market_ctx.get("side"), entry_time=canonical_utc_timestamp(), planned_entry=market_ctx.get("entry"), simulated_fill=result.get("fill_price"), stop=market_ctx.get("stop"), target=market_ctx.get("target"), quantity=market_ctx.get("quantity") or market_ctx.get("qty"), notional=market_ctx.get("notional") or market_ctx.get("notional_usdt") or market_ctx.get("order_notional"), entry_spread=exec_ctx.get("spread_pct") or market_ctx.get("spread_pct"), entry_slippage=result.get("expected_slippage_pct") or exec_ctx.get("expected_slippage_pct"), entry_fee=exec_ctx.get("fee_pct") or market_ctx.get("fee_pct"), regime=market_ctx.get("regime") or exec_ctx.get("volatility_regime"), source_provenance={"provider": self.scanner_source or "UNKNOWN", "campaign_id": self._burnin_campaign_id})
            except Exception as exc:
                self._burnin_evidence_incomplete = True
                self._fail_closed_reason = "PHASE8_CAMPAIGN_PERSISTENCE_FAILURE"
                logger.exception("phase8_pending_position_persistence_failed", exc_info=exc)
        await self._emit_lifecycle_event(LifecycleState.POSITION_OPENED.value, symbol, {"result": dict(result)})
        self._generate_burnin_snapshot(reason="periodic")
        self._active_positions[symbol] = float(market_ctx.get("notional") or market_ctx.get("notional_usdt") or market_ctx.get("order_notional") or 0.0)
        self._symbol_cooldown_until[symbol] = time.time() + self.config.symbol_cooldown_sec

    async def _persist_live_precheck_evidence(self, symbol: str, signal_payload: Mapping[str, Any], market_ctx: Mapping[str, Any], regime_ctx: Mapping[str, Any], stats_ctx: Mapping[str, Any], score_ctx: Any, order_plan: Any, explanation: str, effective_rr: float) -> None:
        engine = self._resolve_persistence_engine()
        if engine is None:
            return
        from alphaforge.persistence import save_order_decision
        paper_signal = {**dict(signal_payload), "mode": ExecutionMode.PAPER.value}
        precheck_signal = {**dict(signal_payload), "mode": ExecutionMode.LIVE_PRECHECK.value}
        paper_eval = self._evaluate_pre_submit(paper_signal, {**dict(market_ctx), "mode": ExecutionMode.PAPER.value}, regime_ctx, stats_ctx)
        live_eval = self._evaluate_pre_submit(precheck_signal, {**dict(market_ctx), "mode": ExecutionMode.LIVE_PRECHECK.value}, regime_ctx, stats_ctx)
        fields = ("decision", "reject_reason", "order_type", "confidence", "score", "raw_rr", "effective_rr", "explanation")
        mismatch = [field for field in fields if paper_eval.get(field) != live_eval.get(field)]
        execution_ctx = dict(market_ctx.get("execution_ctx") or build_execution_context(market_ctx))
        input_hash = self._snapshot_hash({"signal": paper_signal, "market": {**dict(market_ctx), "mode": ExecutionMode.PAPER.value}, "regime": dict(regime_ctx), "stats": dict(stats_ctx)})
        with sessionmaker(bind=engine, expire_on_commit=False, future=True)() as session:
            save_order_decision(
                session,
                decision_id=f"live_precheck:{signal_payload.get('signal_id')}",
                signal_id=signal_payload.get("signal_id"),
                symbol=symbol,
                mode=ExecutionMode.LIVE_PRECHECK.value,
                phase="live_precheck",
                decision=order_plan.decision,
                reject_reason=canonical_reject_reason(order_plan.reason) if order_plan.decision != "ACCEPTED" else "",
                score=getattr(score_ctx, "total_score", None),
                rr=signal_payload.get("risk_reward"),
                effective_rr=effective_rr,
                order_type=order_plan.order_type,
                confidence=order_plan.confidence,
                explanation=explanation,
                execution_ctx=execution_ctx,
                execution_ctx_missing=str(execution_ctx.get("evidence_status", "")).upper() in {"", "UNAVAILABLE", "UNKNOWN"},
                expected_slippage_pct=execution_ctx.get("expected_slippage_pct"),
                spread_pct=execution_ctx.get("spread_pct"),
                latency_ms=execution_ctx.get("market_data_latency_ms"),
                funding_rate_pct=execution_ctx.get("funding_rate_pct"),
                orderbook_imbalance=execution_ctx.get("orderbook_imbalance"),
                volatility_regime=execution_ctx.get("volatility_regime"),
                input_snapshot_hash=input_hash,
                no_submit_verified=True,
                parity_result="PASS" if not mismatch else "FAIL",
                order_payload={"paper": paper_eval, "live_precheck": live_eval, "mismatch_fields": mismatch, "no_submit_verified": True, "input_snapshot_hash": input_hash},
            )
            session.commit()

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
        self._persist_burnin_decision({**payload, "decision": "REJECTED"}, lifecycle_state=LifecycleState.SIGNAL_REJECTED.value)
        if self._burnin_campaign_id and self._burnin_run_id:
            try:
                engine = self._resolve_persistence_engine()
                if engine is not None:
                    from alphaforge.burnin_resolver import persist_pending_reject_label
                    execution_ctx = dict(payload.get("execution_ctx") or {})
                    with engine.begin() as conn:
                        persist_pending_reject_label(conn, campaign_id=self._burnin_campaign_id, burnin_run_id=self._burnin_run_id, reject_decision_id=str(payload.get("reject_decision_id") or payload.get("decision_id") or payload.get("signal_id") or canonical_utc_timestamp()), signal_id=payload.get("signal_id"), symbol=payload.get("symbol"), side=payload.get("side") or payload.get("direction"), decision_timestamp=payload.get("decision_timestamp") or payload.get("timestamp"), entry=payload.get("entry"), stop=payload.get("stop"), target=payload.get("target"), horizon_seconds=payload.get("forward_horizon_seconds"), execution_cost_assumptions=payload.get("execution_cost_assumptions"), regime=payload.get("regime") or payload.get("volatility_regime") or execution_ctx.get("volatility_regime"), reject_reason=payload.get("reject_reason") or payload.get("reason"), source_provenance={"provider": self.scanner_source or "UNKNOWN", "campaign_id": self._burnin_campaign_id})
            except Exception as exc:
                self._burnin_evidence_incomplete = True
                self._fail_closed_reason = "PHASE8_CAMPAIGN_PERSISTENCE_FAILURE"
                logger.exception("phase8_reject_label_persistence_failed", exc_info=exc)
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
        if lifecycle_state == LifecycleState.POSITION_CLOSED.value:
            self._persist_burnin_closed_trade_from_lifecycle(symbol, detail_payload)
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
        if self._fail_closed_reason:
            return self._fail_closed_reason
        if self._recovery_required:
            return "RUNTIME_RECOVERY_REQUIRED"
        if self.metrics.last_heartbeat_ts and (now - self.metrics.last_heartbeat_ts) > max(self.config.heartbeat_interval_sec * 3, 120.0):
            return "HEARTBEAT_STALE"
        if self.config.execution_mode != ExecutionMode.BACKTEST and self._unknown_exchange_state:
            return "EXCHANGE_STATE_UNKNOWN"
        if self._orphan_orders:
            return "ORPHAN_ORDER_DETECTED"
        if self._orphan_positions:
            return "ORPHAN_POSITION_DETECTED"
        if self._unreconciled_symbols:
            return "UNRECONCILED_POSITION"
        if self.config.global_kill_switch:
            return "GLOBAL_KILL_SWITCH"
        if len(self._active_positions) >= self.config.max_concurrent_positions:
            return "MAX_CONCURRENT_POSITIONS"
        if now < self._symbol_cooldown_until.get(symbol, 0.0):
            return "SYMBOL_COOLDOWN"
        market_ts_raw = market_ctx.get("market_ts", now)
        market_ts = float(now if market_ts_raw in (None, "") else market_ts_raw)
        if (now - market_ts) > self.config.stale_market_data_sec:
            self._stale_market_data_symbols.add(symbol)
            return "STALE_MARKET_DATA"
        spread_pct = float(market_ctx.get("spread_pct", 0.0) or 0.0)
        if spread_pct > self.config.max_spread_pct:
            return "SPREAD_TOO_HIGH"
        slippage = float(market_ctx.get("expected_slippage_pct", 0.0) or 0.0)
        if slippage > self.config.max_expected_slippage_pct:
            return "SLIPPAGE_TOO_HIGH"
        funding = abs(float(market_ctx.get("funding_rate_pct", 0.0) or 0.0))
        if funding > self.config.max_abs_funding_rate_pct:
            return "FUNDING_TOO_HIGH"
        liquidity = float(market_ctx.get("volume_24h_usdt", self.config.min_liquidity_usd) or 0.0)
        if liquidity < self.config.min_liquidity_usd:
            return "THIN_LIQUIDITY"
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
                self._persist_runtime_state_snapshot("OPERATING")
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
        if self.config.execution_mode == ExecutionMode.BACKTEST:
            self._unknown_exchange_state = False
            self._exchange_read_only_status = "NOT_REQUIRED_BACKTEST"
            self._reconciliation_status = "NOT_REQUIRED_BACKTEST"
            snapshot_source = {"orders": list(self._pending_orders.values()), "positions": [{"symbol": s, "qty": q} for s, q in self._active_positions.items()], "fills": []}
        elif self.config.execution_mode in {ExecutionMode.PAPER, ExecutionMode.LIVE_PRECHECK, ExecutionMode.LIVE}:
            provider = self.live_reconciliation_provider or self.exchange_snapshot_provider
            if provider is None:
                if self.config.execution_mode == ExecutionMode.LIVE:
                    raise RuntimeError("LIVE mode blocked: reconciliation provider is not configured")
                if self.config.diagnostic_mode:
                    self._unknown_exchange_state = False
                    self._exchange_read_only_status = "LOCAL_ONLY"
                    self._reconciliation_status = "LOCAL_ONLY_DIAGNOSTIC"
                    snapshot_source = {"orders": list(self._pending_orders.values()), "positions": [{"symbol": s, "qty": q} for s, q in self._active_positions.items()], "fills": [], "evidence_status": "LOCAL_ONLY_DIAGNOSTIC", "diagnostic_override": True}
                else:
                    self._unknown_exchange_state = True
                    self._exchange_read_only_status = "UNAVAILABLE"
                    self._reconciliation_status = "EXCHANGE_RECONCILIATION_UNAVAILABLE"
                    self._fail_closed_reason = "EXCHANGE_RECONCILIATION_UNAVAILABLE"
                    snapshot_source = {"orders": [], "positions": [], "fills": [], "evidence_status": "INCOMPLETE", "blocking_reason": "EXCHANGE_RECONCILIATION_UNAVAILABLE"}
            else:
                snapshot_source = dict(provider.snapshot())
                complete = str(snapshot_source.get("evidence_status") or "INCOMPLETE").upper() == "COMPLETE"
                self._unknown_exchange_state = not complete
                self._exchange_read_only_status = "AVAILABLE" if complete else "UNAVAILABLE"
                if not complete:
                    if self.config.execution_mode == ExecutionMode.LIVE:
                        raise RuntimeError("LIVE mode blocked: reconciliation evidence incomplete")
                    self._fail_closed_reason = "EXCHANGE_STATE_UNKNOWN"
                    self._reconciliation_status = "EXCHANGE_STATE_UNKNOWN"
        else:
            snapshot_source = {"orders": [], "positions": [], "fills": []}
        snapshot = self._reconciliation_engine.snapshot_from_source(snapshot_source)
        findings, recommendations, _metrics = self._reconciliation_engine.reconcile(
            intended_orders=[] if self._exchange_read_only_status == "LOCAL_ONLY" else list(self._pending_orders.values()),
            lifecycle_state_by_symbol=self._last_lifecycle_state_by_symbol,
            snapshot=snapshot,
            mode=self.config.execution_mode.value,
        )
        self._orphan_orders = [dict(getattr(f, "evidence", {}) or {"symbol": f.symbol, "type": f.finding_type}) for f in findings if "ORDER" in str(f.finding_type).upper() and getattr(f, "fail_closed", False)]
        self._orphan_positions = [dict(getattr(f, "evidence", {}) or {"symbol": f.symbol, "type": f.finding_type}) for f in findings if "POSITION" in str(f.finding_type).upper() and getattr(f, "fail_closed", False)]
        self._unreconciled_symbols = {str(f.symbol) for f in findings if getattr(f, "fail_closed", False)}
        if findings and self._fail_closed_reason is None:
            self._fail_closed_reason = "ORPHAN_ORDER_DETECTED" if self._orphan_orders else ("ORPHAN_POSITION_DETECTED" if self._orphan_positions else "UNRECONCILED_POSITION")
        if not self._fail_closed_reason:
            self._unknown_exchange_state = False
            if self.config.execution_mode != ExecutionMode.BACKTEST:
                self._reconciliation_status = "CLEAN"
        engine = self._resolve_persistence_engine()
        if engine is not None:
            persist_findings(engine, findings)
            save_exchange_reconciliation_event(engine, instance_id=self.runtime_instance_id, startup_id=self.startup_id, mode=self.config.execution_mode.value, status=self._reconciliation_status, mismatch_count=len(findings), orphan_order_count=len(self._orphan_orders), orphan_position_count=len(self._orphan_positions), exchange_read_only_status=self._exchange_read_only_status, diagnostics=snapshot_source)
            self._persist_runtime_state_snapshot("RECONCILED" if not self._fail_closed_reason else "RECOVERY_REQUIRED")
        for finding in findings:
            if not finding.fail_closed:
                continue
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
        raise ValueError(f"Unsupported EXECUTION_MODE={raw_mode!r}. Expected BACKTEST/PAPER/LIVE_PRECHECK/LIVE") from exc


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
    config = RuntimeConfig(execution_mode=mode, min_signal_score=cfg.runtime.min_signal_score, scan_interval_sec=cfg.runtime.scan_interval_sec, heartbeat_interval_sec=cfg.runtime.heartbeat_interval_sec, max_symbols_per_scan=cfg.runtime.max_symbols_per_scan, max_reject_log_entries=cfg.runtime.max_reject_log_entries, max_concurrent_positions=cfg.runtime.max_concurrent_positions, symbol_cooldown_sec=cfg.runtime.symbol_cooldown_sec, max_notional_exposure=cfg.runtime.max_notional_exposure, max_symbol_notional=cfg.runtime.max_symbol_notional, stale_market_data_sec=cfg.runtime.stale_market_data_sec, max_spread_pct=cfg.runtime.max_spread_pct, max_abs_funding_rate_pct=cfg.runtime.max_abs_funding_rate_pct, global_kill_switch=cfg.runtime.global_kill_switch, require_live_qualification=cfg.runtime.require_live_qualification, enable_shadow_mode=cfg.runtime.enable_shadow_mode, enable_canary_mode=cfg.runtime.enable_canary_mode, operator_live_acknowledged=cfg.runtime.operator_live_acknowledged, reconciliation_interval_sec=cfg.runtime.reconciliation_interval_sec, reconciliation_timeout_sec=cfg.runtime.reconciliation_timeout_sec, require_exchange_connectivity_for_live=cfg.runtime.require_exchange_connectivity_for_live, required_live_exchanges=cfg.runtime.required_live_exchanges, exchange_connectivity_timeout_sec=cfg.runtime.exchange_connectivity_timeout_sec, enable_binance_readonly_reconciliation=cfg.runtime.enable_binance_readonly_reconciliation, min_rr=cfg.runtime.min_rr, min_effective_rr=cfg.runtime.min_effective_rr, max_expected_slippage_pct=cfg.runtime.max_expected_slippage_pct, min_liquidity_usd=cfg.runtime.min_liquidity_usd)

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
        if (
            payload.get("lifecycle_state") == LifecycleState.SIGNAL_REJECTED.value
            and str(details.get("decision") or "").upper() == "REJECTED"
        ):
            return
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
                reject_reason=details.get("reject_reason") or details.get("reason"),
                score=details.get("score"),
                rr=details.get("rr"),
                effective_rr=details.get("effective_rr"),
                expectancy_bucket=details.get("expectancy_bucket"),
                execution_ctx=details.get("execution_ctx", {}),
                execution_ctx_missing=details.get("execution_ctx_missing"),
            ):
                raise RuntimeError("trade_lifecycle_event_persistence_failed")
            session.commit()

    def _persist_reject(payload: dict[str, Any]) -> None:
        if not persistence_enabled:
            return
        from alphaforge.persistence import save_rejected_decision_artifact
        with SessionLocal() as session:
            persisted = save_rejected_decision_artifact(
                session,
                mode=mode.value,
                phase=payload.get("phase", "final"),
                signal_id=payload.get("signal_id"),
                symbol=payload.get("symbol"),
                reject_reason=payload.get("reason"),
                confidence=payload.get("confidence"),
                score=payload.get("score"),
                rr=payload.get("rr"),
                raw_rr=payload.get("rr"),
                effective_rr=payload.get("effective_rr"),
                explanation=payload.get("explanation"),
                execution_ctx=payload.get("execution_ctx", {}),
                spread_pct=payload.get("spread_pct"),
                expected_slippage_pct=payload.get("expected_slippage_pct"),
                latency_ms=payload.get("latency_ms"),
                funding_rate_pct=payload.get("funding_rate_pct"),
                orderbook_imbalance=payload.get("orderbook_imbalance"),
                volatility_regime=payload.get("volatility_regime"),
                portfolio_reject_reason=payload.get("portfolio_reject_reason"),
                portfolio_risk_state=payload.get("portfolio_risk_state"),
                portfolio_diagnostics=payload.get("portfolio_diagnostics"),
                risk_flags=payload.get("risk_flags"),
            )
            if persisted is None:
                raise RuntimeError("rejected_decision_artifact_persistence_failed")
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
        control_store=RuntimeControlStore(engine),
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
