from __future__ import annotations

import asyncio
import contextlib
from collections import deque
import hashlib
import json
import logging
import math
import os
import signal
import time
import uuid
import subprocess
from datetime import datetime, timezone
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, MutableMapping, Protocol

from alphaforge.ai_brain import AIBrain, score_reject_reason
from alphaforge.contracts import LifecycleEventType, canonical_reject_reason, canonical_utc_timestamp, validate_transition
from alphaforge.order import LifecycleState, OrderExecutionContext, TradingMode, validate_live_order_authorization
from alphaforge.execution import (
    PROVENANCE_ACTUAL,
    PROVENANCE_ESTIMATED,
    PROVENANCE_MODELLED,
    PROVENANCE_UNAVAILABLE,
    build_execution_context,
    build_execution_cost_model,
    evaluate_execution_safety,
    build_execution_cost_semantics,
    execution_context_is_unavailable,
    weighted_average_fill_price,
)
from alphaforge.scoring_context import build_signal_payload, finite_numeric, normalize_scoring_context
from alphaforge.live_readiness import LiveReadinessEvaluator, QualificationReport
from alphaforge.runtime_heartbeat import is_sqlite_busy_error, save_runtime_heartbeat
from alphaforge.runtime_control import RuntimeControlStore
from alphaforge.exchange_connectivity import ExchangeHealth, check_required_exchanges_health
from alphaforge.exchange_market_scanner import enrich_selected_market_geometry, scan_exchange_markets
from alphaforge.binance_reconciliation_provider import BinanceReadonlyReconciliationConfig, BinanceReadonlyReconciliationProvider
from alphaforge.reconciliation import ReconciliationEngine, summarize_findings
from alphaforge.symbol_selector import SymbolSelectionResult, select_symbols
from alphaforge.persistence import fetch_expectancy_stat_detail, init_db, save_decision_evidence
from alphaforge.adaptive_learning import record_rejected_signal_review
from alphaforge.schema_doctor import load_active_positions, load_pending_orders
from alphaforge.burnin import BurnInRun, DIAGNOSTIC_OBSERVATION_KIND, bootstrap_burnin_schema, canonical_decision_sql, canonical_hash, config_hash as burnin_config_hash, universe_hash as burnin_universe_hash, persist_burnin_run, persist_burnin_observation, persist_burnin_trade_outcome, update_burnin_run_counters, next_burnin_continuation_sequence
from alphaforge.burnin_qualification import BurnInQualificationEngine
from alphaforge.burnin_resolver import persist_pending_position, persist_pending_reject_label, resolve_campaign_batch
from alphaforge.burnin_campaign import bootstrap_campaign_schema, get_campaign as get_burnin_campaign, event as burnin_campaign_event, _exec as burnin_campaign_exec, build_phase8_campaign_identity, canonical_paper_source_exchanges, fail_active_campaign_run, pause_campaign_for_provider_failure, terminalize_active_campaign_run, campaign_attachment_identity, run_attachment_identity, identity_mismatches, load_active_campaign_attachment, ATTACHMENT_IDENTITY_FIELDS, RUNTIME_ATTACHMENT_IDENTITY_FIELDS, CAMPAIGN_RUNTIME_IDENTITY_FIELDS
from alphaforge.provider_failures import classify_provider_exception, classify_reconciliation_snapshot, TRANSIENT_TRANSPORT, PERMANENT_AUTH_OR_PROTOCOL, UNKNOWN
from alphaforge.portfolio_risk import evaluate_portfolio_risk, snapshot_from_state
from alphaforge.runtime_state import RuntimeStateSnapshot, save_runtime_state_snapshot, save_runtime_recovery_event, evaluate_runtime_recovery, build_readonly_reconciliation_probe, persist_reconciliation_cycle, ReconciliationPersistenceFailure
from alphaforge.config import (load_config_from_env, load_reconciliation_settings,
    normalize_mtf_execution_confirmation_mode, runtime_filter_config)
from alphaforge.config_registry import managed_config_value
from alphaforge.agents.orchestrator import AgentGraphConfig, ShadowAgentOrchestrator
from alphaforge.agents.phase_b import register_phase_b_handlers
from alphaforge.agents.persistence import (AgentPersistenceStats, AgentTraceRepository,
    bootstrap_agent_schema, create_agent_shadow_engine)
from alphaforge.multi_timeframe import BinanceMTFProvider
from alphaforge.state_direction_shadow import StateDirectionShadowStore, build_state_direction_shadow_draft
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
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
    max_clock_skew_ms: int = field(default_factory=lambda: int(managed_config_value("ALPHAFORGE_MAX_CLOCK_SKEW_MS")))
    min_rr: float = 1.20
    min_effective_rr: float = 1.10
    max_spread_pct: float = 0.0025
    max_expected_slippage_pct: float = 0.0020
    max_total_cost_pct: float = 0.20
    min_liquidity_score: float = 0.30
    max_volatility_penalty_pct: float = 0.20
    reject_unknown_execution_context: bool = True
    max_latency_ms: int = 2500
    paper_fee_bps: float | None = 4.0
    paper_execution_latency_ms: float | None = 50.0
    # Explicit PAPER-only portfolio evidence. These values never substitute
    # for unknown LIVE/LIVE_PRECHECK account state.
    paper_initial_equity: float | None = 1_000.0
    paper_candidate_notional: float | None = 10.0  # conservative 1% of the default ledger
    market_data_base_url: str = "https://fapi.binance.com"
    regime_timeframe: str = "1h"
    setup_timeframe: str = "15m"
    execution_timeframe: str = "1m"
    mtf_guided_signal_generation_enabled: bool = True
    mtf_execution_confirmation_mode: str = "ENFORCE"
    regime_direction_threshold: float = 0.0005
    setup_direction_threshold: float = 0.0003
    execution_direction_threshold: float = 0.0005
    enable_state_direction_resolution: bool = False
    paper_decision_timeframe: str = "1m"  # deprecated alias
    require_mtf_alignment: bool = False
    max_abs_funding_rate_pct: float = 0.0010
    min_liquidity_usd: float = 5_000_000.0
    # Keep every decision-filter field that can be environment-configured on
    # the runtime config.  Phase 8/9 campaign identity hashes this effective
    # filter configuration, so dropping any of these while building the
    # orchestrator would make a PAPER preflight compare two different payloads.
    min_sl_pct: float = 0.15
    max_sl_pct: float = 1.5
    min_atr_pct: float = 0.25
    max_atr_pct: float = 3.0
    block_unknown_expectancy: bool = True
    block_chop_market: bool = True
    require_regime_alignment: bool = True
    enable_orderbook_filter: bool = False
    stop_too_wide_hard_reject: bool = True
    stop_too_wide_soft_score_min: float = 9.0
    stop_too_wide_soft_effective_rr_min: float = 1.75
    stop_too_wide_max_risk_scale: float = 0.50
    stop_too_wide_extreme_mult: float = 1.50
    max_trades_global_per_day: int = 10
    max_trades_symbol_per_day: int = 2
    symbol_loss_streak_limit: int = 3
    global_loss_streak_limit: int = 5
    global_kill_switch: bool = False
    require_live_qualification: bool = True
    enable_shadow_mode: bool = False
    enable_canary_mode: bool = False
    operator_live_acknowledged: bool = False
    allow_live_orders: bool = False
    live_trading_enabled: bool = False
    reconciliation_interval_sec: float = 5.0
    reconciliation_timeout_sec: float = 2.0
    provider_transient_outage_grace_seconds: float = 300.0
    require_exchange_connectivity_for_live: bool = True
    required_live_exchanges: tuple[str, ...] = ("binance",)
    exchange_connectivity_timeout_sec: float = 2.0
    enable_binance_readonly_reconciliation: bool = False
    pending_order_timeout_sec: float = 300.0
    require_exchange_reconciliation_for_paper: bool = True
    diagnostic_mode: bool = False
    phase7_burnin_release_id: str = "default"
    phase7_burnin_snapshot_interval_sec: float = 300.0
    reject_forward_horizon_bars: int = 240
    reject_resolver_interval_sec: float = 60.0
    agent_graph_enabled: bool = False
    agent_graph_shadow: bool = True
    agent_graph_max_steps: int = 12
    agent_graph_max_reflection_retries: int = 1
    agent_graph_stage_timeout_seconds: float = 5.0
    agent_graph_persist_traces: bool = True
    agent_graph_max_pending_runs: int = 64
    agent_graph_database_url: str = "sqlite+pysqlite:///data/runtime/alphaforge_agent_shadow.db"

    def __post_init__(self) -> None:
        self.mtf_execution_confirmation_mode = normalize_mtf_execution_confirmation_mode(
            self.mtf_execution_confirmation_mode
        )
        if int(self.max_clock_skew_ms) < 0:
            raise ValueError("max_clock_skew_ms must be >= 0")
        if (self.mtf_execution_confirmation_mode == "SHADOW"
                and str(getattr(self.execution_mode, "value", self.execution_mode)).upper() != "PAPER"):
            raise ValueError("MTF_EXECUTION_CONFIRMATION_MODE=SHADOW is PAPER-only")


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
    agent_shadow_runs: int = 0
    agent_shadow_errors: int = 0
    agent_shadow_stage_events: int = 0
    agent_shadow_persistence_errors: int = 0
    agent_shadow_queue_depth: int = 0
    agent_shadow_dropped: int = 0
    agent_shadow_deferred: int = 0
    agent_shadow_persistence_retries: int = 0
    agent_shadow_lock_wait_ms: float = 0.0
    agent_shadow_worker_count: int = 0
    market_agent_pass: int = 0
    market_agent_reject: int = 0
    market_agent_defer: int = 0
    signal_candidates_generated: int = 0
    signal_no_candidate: int = 0
    quality_pass: int = 0
    quality_reject: int = 0
    quality_defer: int = 0
    phase_b_errors: int = 0
    burnin_outcomes: int = 0
    burnin_snapshots: int = 0
    mtf_contexts_built: int = 0
    mtf_alignment_pass: int = 0
    mtf_alignment_reject: int = 0
    mtf_regime_missing: int = 0
    mtf_setup_missing: int = 0
    mtf_execution_missing: int = 0
    mtf_execution_not_confirmed: int = 0
    mtf_execution_confirmation_shadow: int = 0
    mtf_execution_counter_regime: int = 0
    mtf_direction_mismatch: int = 0
    mtf_stale_context: int = 0
    mtf_guided_candidates_generated: int = 0
    mtf_legacy_candidates_shadowed: int = 0
    mtf_setup_continuation: int = 0
    mtf_setup_pullback: int = 0
    mtf_setup_reentry_ready: int = 0
    mtf_setup_no_setup: int = 0
    mtf_setup_overextended: int = 0
    mtf_setup_invalid: int = 0
    equal_execution_candles_skipped: int = 0
    replayed_execution_candles_skipped: int = 0
    malformed_execution_candles_skipped: int = 0
    finalized_signal_replays_skipped: int = 0
    final_decision_lookup_failures: int = 0
    heartbeat_persistence_failures: int = 0
    heartbeat_persistence_recoveries: int = 0
    heartbeat_persistence_degraded: bool = False
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
    reject_candle_provider: Callable[[str, str, str], Any] | None = None
    on_lifecycle_event: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None
    on_reject_persist: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None
    state_direction_shadow_enabled: bool = False
    state_direction_shadow_store: StateDirectionShadowStore | None = None
    paper_slippage_bps: float = 2.0
    persistence_engine: Engine | None = None
    control_store: RuntimeControlStore | None = None
    selected_candidate_enricher: Callable[[list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]] | None = None
    mtf_context_provider: Any | None = None
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _tasks: list[asyncio.Task[Any]] = field(default_factory=list, init=False)
    _shadow_queue: asyncio.Queue[dict[str, Any]] | None = field(default=None, init=False)
    _shadow_worker_task: asyncio.Task[Any] | None = field(default=None, init=False)
    _agent_trace_repository: AgentTraceRepository | None = field(default=None, init=False)
    _agent_persistence_stats: AgentPersistenceStats = field(default_factory=AgentPersistenceStats, init=False)
    _reject_log: deque[dict[str, Any]] = field(init=False)
    _persisted_reject_decision_ids: set[str] = field(default_factory=set, init=False)
    _state_direction_shadow_drafts: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    metrics: RuntimeMetrics = field(default_factory=RuntimeMetrics, init=False)
    runtime_instance_id: str = field(default_factory=lambda: f"runtime:{uuid.uuid4().hex}", init=False)
    startup_id: str = field(default_factory=lambda: f"startup:{uuid.uuid4().hex}", init=False)
    _runtime_status: str = field(default="INITIALIZING", init=False)
    _last_start_time: str | None = field(default=None, init=False)
    _last_shutdown_time: str | None = field(default=None, init=False)
    _last_error: str | None = field(default=None, init=False)
    _recovery_required: bool = field(default=False, init=False)
    _fail_closed_reason: str | None = field(default=None, init=False)
    _campaign_intervals: tuple[str, ...] = field(default=(), init=False)
    _campaign_id: str | None = field(default=None, init=False)
    _campaign_symbols: frozenset[str] = field(default_factory=frozenset, init=False)
    _campaign_source_exchanges: frozenset[str] = field(default_factory=frozenset, init=False)
    _fatal_task_exception: BaseException | None = field(default=None, init=False)
    _fatal_task_name: str | None = field(default=None, init=False)
    _unknown_exchange_state: bool = field(default=False, init=False)
    _reconciliation_status: str = field(default="UNKNOWN", init=False)
    _reconciliation_persistence_unhealthy: bool = field(default=False, init=False)
    _heartbeat_persistence_failure_streak: int = field(default=0, init=False)
    _heartbeat_persistence_failure_threshold: int = field(default=3, init=False)
    _provider_failure_class: str | None = field(default=None, init=False)
    _transient_provider_outage_started_monotonic: float | None = field(default=None, init=False)
    _provider_failure_count: int = field(default=0, init=False)
    _resolver_provider_unavailable: bool = field(default=False, init=False)
    _resolver_provider_recovery_pending: bool = field(default=False, init=False)
    _pending_reconciliation_persistence_failures: list[dict[str, Any]] = field(default_factory=list, init=False)
    _exchange_read_only_status: str = field(default="UNKNOWN", init=False)
    _unreconciled_symbols: set[str] = field(default_factory=set, init=False)
    _orphan_orders: list[dict[str, Any]] = field(default_factory=list, init=False)
    _orphan_positions: list[dict[str, Any]] = field(default_factory=list, init=False)
    _stale_market_data_symbols: set[str] = field(default_factory=set, init=False)
    _last_lifecycle_state_by_signal: dict[str, str] = field(default_factory=dict, init=False)
    _current_signal_id_by_symbol: dict[str, str] = field(default_factory=dict, init=False)
    _last_lifecycle_state_by_symbol: dict[str, str] = field(default_factory=dict, init=False)
    _symbol_cooldown_until: dict[str, float] = field(default_factory=dict, init=False)
    _active_positions: dict[str, float] = field(default_factory=dict, init=False)
    _active_position_sides: dict[str, str] = field(default_factory=dict, init=False)
    _incident_counters: dict[str, int] = field(default_factory=dict, init=False)
    _qualification_report: QualificationReport | None = field(default=None, init=False)
    _reconciliation_engine: ReconciliationEngine = field(default_factory=ReconciliationEngine, init=False)
    _pending_orders: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    _last_repair_signature: set[str] = field(default_factory=set, init=False)
    _last_scan_rejection_summary: dict[str, int] = field(default_factory=dict, init=False)
    _last_scan_advisory_summary: dict[str, int] = field(default_factory=dict, init=False)
    _last_scan_gate_blockers: list[str] = field(default_factory=list, init=False)
    _market_data_health_status: str = field(default="UNKNOWN", init=False)
    _market_data_failure_streak: int = field(default=0, init=False)
    _last_market_data_diagnostics: dict[str, Any] = field(default_factory=dict, init=False)
    _live_order_submission_enabled: bool = field(default=False, init=False)
    _mutation_trap_active: bool = field(default=False, init=False)
    _exchange_health: list[ExchangeHealth] = field(default_factory=list, init=False)
    _burnin_run_id: str | None = field(default=None, init=False)
    _burnin_evidence_incomplete: bool = field(default=False, init=False)
    _burnin_suspended: bool = field(default=False, init=False)
    _latest_execution_candle_by_market: dict[tuple[str, str, str], Any] = field(default_factory=dict, init=False)
    _canonical_setup_reject_ids: set[str] = field(default_factory=set, init=False)
    _accepted_setup_identities: set[str] = field(default_factory=set, init=False)
    _last_burnin_snapshot_ts: float = field(default=0.0, init=False)
    _recovery_decision: dict[str, Any] = field(default_factory=dict, init=False)
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

    def _prepare_state_direction_shadow(
        self, *, symbol: str, signal_id: str, market_ctx: Mapping[str, Any],
        mtf: Mapping[str, Any], execution_ctx: Mapping[str, Any],
    ) -> None:
        if (
            not self.state_direction_shadow_enabled
            or self.config.execution_mode is not ExecutionMode.PAPER
            or self.config.enable_state_direction_resolution
        ):
            return
        try:
            draft = build_state_direction_shadow_draft(
                symbol=symbol, signal_id=signal_id, market_ctx=dict(market_ctx),
                mtf=dict(mtf), execution_ctx=dict(execution_ctx),
                horizon_bars=self.config.reject_forward_horizon_bars,
            )
            if draft is not None:
                self._state_direction_shadow_drafts[signal_id] = draft
        except Exception as exc:
            logger.exception("state_direction_shadow_evaluation_failed", exc_info=exc)

    def _record_state_direction_shadow(
        self, payload: Mapping[str, Any], *, actual_decision: str,
    ) -> None:
        signal_id = str(payload.get("signal_id") or "")
        draft = self._state_direction_shadow_drafts.pop(signal_id, None)
        if draft is None or self.state_direction_shadow_store is None:
            return
        try:
            self.state_direction_shadow_store.record(
                draft,
                actual_decision=actual_decision,
                actual_side=payload.get("side"),
                actual_reject_reason=(
                    payload.get("reject_reason") or payload.get("reason")
                    if actual_decision != "ACCEPTED" else None
                ),
            )
        except Exception as exc:
            logger.exception("state_direction_shadow_persistence_failed", exc_info=exc)

    def _resolve_persistence_engine(self) -> Engine | None:
        if self.persistence_engine is not None:
            return self.persistence_engine
        session = getattr(self.ai_brain, "session", None)
        if session is not None:
            return session.get_bind()
        return None

    @staticmethod
    def _finite_numeric(*candidates: tuple[str, Any]) -> tuple[float | None, str | None]:
        """Return the first canonical finite numeric value without mapping labels."""
        return finite_numeric(*candidates)

    def _build_scoring_context(
        self,
        signal_payload: Mapping[str, Any],
        market_ctx: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Build AIBrain inputs from canonical runtime/MTF evidence and SQL stats."""
        scored_market, regime_ctx, _ = normalize_scoring_context(signal_payload, market_ctx)
        stats_ctx = self._build_stats_context(signal_payload, regime_ctx)
        scored_market["scoring_context_diagnostics"]["sample_size"] = stats_ctx["sample_size"]
        return scored_market, regime_ctx, stats_ctx

    def _build_stats_context(
        self,
        signal_payload: Mapping[str, Any],
        regime_ctx: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Load only the expectancy statistics already consumed by AIBrain."""
        stats: dict[str, Any] = {"setup": {}, "regime": {}, "symbol": {}, "sample_size": 0}
        engine = self._resolve_persistence_engine()
        if engine is None:
            return stats
        keys = (
            ("setup", "setup_expectancy_stats", "setup", str(signal_payload.get("setup", "unknown"))),
            ("regime", "regime_expectancy_stats", "regime", str(regime_ctx.get("regime", "unknown"))),
            ("symbol", "symbol_expectancy_stats", "symbol", str(signal_payload.get("symbol", "unknown"))),
        )
        sample_sizes = {scope: 0 for scope, *_ in keys}
        with engine.connect() as connection:
            for scope, table, column, key in keys:
                detail = fetch_expectancy_stat_detail(connection, table, column, key)
                if detail is None:
                    continue
                expectancy = detail.get("expectancy")
                if isinstance(expectancy, (int, float)) and not isinstance(expectancy, bool) and math.isfinite(float(expectancy)):
                    stats[scope][key] = float(expectancy)
                    sample_sizes[scope] = int(detail.get("sample_size") or 0)
        # AIBrain applies one confidence value to the equally weighted setup,
        # regime, and symbol expectations. Use the least-supported scope so a
        # broad symbol history cannot confer confidence on an unseen setup.
        stats["sample_size"] = min(sample_sizes.values(), default=0)
        return stats

    def _schedule_agent_shadow(self, legacy_decision: Mapping[str, Any]) -> None:
        """Enqueue an isolated copy without creating a task or blocking legacy flow."""
        if not (self.config.agent_graph_enabled and self.config.agent_graph_shadow):
            return
        snapshot = json.loads(json.dumps(dict(legacy_decision), default=str))
        signal_id = str(snapshot.get("signal_id") or uuid.uuid4().hex)
        item = {"decision_id": signal_id, "correlation_id": f"shadow:{signal_id}:{uuid.uuid4().hex}",
                "snapshot": snapshot}
        if self._shadow_queue is None:
            self.metrics.agent_shadow_dropped += 1
            return
        try:
            if not self._shadow_queue.empty():
                self.metrics.agent_shadow_deferred += 1
            self._shadow_queue.put_nowait(item)
            self.metrics.agent_shadow_queue_depth = self._shadow_queue.qsize()
        except asyncio.QueueFull:
            # Deterministic overload policy: retain older evidence, drop newest.
            self.metrics.agent_shadow_dropped += 1

    def _initialize_agent_shadow(self) -> None:
        if not (self.config.agent_graph_enabled and self.config.agent_graph_shadow):
            return
        try:
            # max(1) also protects direct RuntimeConfig construction in tests or
            # embeddings; asyncio.Queue(maxsize=0) would otherwise be unbounded.
            self._shadow_queue = asyncio.Queue(maxsize=max(1, self.config.agent_graph_max_pending_runs))
            if self.config.agent_graph_persist_traces:
                engine = create_agent_shadow_engine(self.config.agent_graph_database_url)
                bootstrap_agent_schema(engine)
                self._agent_trace_repository = AgentTraceRepository(engine, stats=self._agent_persistence_stats)
            self._shadow_worker_task = asyncio.create_task(self._agent_shadow_worker(), name="agent_shadow_worker")
            self.metrics.agent_shadow_worker_count = 1
        except Exception:
            self._shadow_queue = None
            self._agent_trace_repository = None
            self.metrics.agent_shadow_persistence_errors += 1
            logger.exception("agent_shadow_initialization_failed")

    async def _agent_shadow_worker(self) -> None:
        assert self._shadow_queue is not None
        while True:
            item = await self._shadow_queue.get()
            self.metrics.agent_shadow_queue_depth = self._shadow_queue.qsize()
            try:
                def run_one() -> Any:
                    graph = ShadowAgentOrchestrator(AgentGraphConfig(
                        enabled=True, shadow_mode=True, max_graph_steps=self.config.agent_graph_max_steps,
                        max_reflection_retries=self.config.agent_graph_max_reflection_retries,
                        stage_timeout_seconds=self.config.agent_graph_stage_timeout_seconds,
                        persist_traces=self.config.agent_graph_persist_traces), persistence=self._agent_trace_repository)
                    register_phase_b_handlers(graph)
                    return asyncio.run(graph.run_shadow(decision_id=item["decision_id"],
                        correlation_id=item["correlation_id"], execution_mode=self.config.execution_mode.value,
                        symbol=item["snapshot"].get("symbol"), legacy_decision=item["snapshot"], context=item["snapshot"]))
                result = await asyncio.to_thread(run_one)
                self.metrics.agent_shadow_runs += 1
                self.metrics.agent_shadow_stage_events += len(result.stage_results)
                phase_b = {event.stage.value: event for event in result.stage_results[:3]}
                market_status = phase_b["MARKET"].status.value.lower()
                if market_status in {"pass", "reject", "defer"}:
                    setattr(self.metrics, f"market_agent_{market_status}",
                            getattr(self.metrics, f"market_agent_{market_status}") + 1)
                signal_event = phase_b["SIGNAL"]
                if signal_event.status.value == "PASS":
                    self.metrics.signal_candidates_generated += 1
                else:
                    self.metrics.signal_no_candidate += 1
                quality_status = phase_b["QUALITY"].status.value.lower()
                if quality_status in {"pass", "reject", "defer"}:
                    setattr(self.metrics, f"quality_{quality_status}",
                            getattr(self.metrics, f"quality_{quality_status}") + 1)
                self.metrics.phase_b_errors += sum(event.status.value == "ERROR" for event in phase_b.values())
                self.metrics.agent_shadow_persistence_retries = self._agent_persistence_stats.retry_count
                self.metrics.agent_shadow_lock_wait_ms = self._agent_persistence_stats.lock_wait_ms
                if result.persistence_error:
                    self.metrics.agent_shadow_persistence_errors += 1
                if result.status.value == "ERROR":
                    self.metrics.agent_shadow_errors += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                self.metrics.agent_shadow_errors += 1
                logger.exception("agent_shadow_run_failed")
            finally:
                self._shadow_queue.task_done()

    def _persist_runtime_heartbeat(self, *, runtime_state: str = "OPERATING") -> None:
        engine = self._resolve_persistence_engine()
        if (
            engine is None
            or not self.metrics.persistence_enabled
            or self.config.execution_mode not in {ExecutionMode.PAPER, ExecutionMode.LIVE}
        ):
            return
        if runtime_state == "OPERATING" and self._execution_reconciliation_blocked():
            runtime_state = "RECOVERY_REQUIRED"
        if self.config.execution_mode == ExecutionMode.PAPER and self._burnin_run_id:
            self._restore_rejects_persisted()
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
                "mtf_contexts_built": self.metrics.mtf_contexts_built,
                "mtf_alignment_pass": self.metrics.mtf_alignment_pass,
                "mtf_alignment_reject": self.metrics.mtf_alignment_reject,
                "mtf_regime_missing": self.metrics.mtf_regime_missing,
                "mtf_setup_missing": self.metrics.mtf_setup_missing,
                "mtf_execution_missing": self.metrics.mtf_execution_missing,
                "mtf_execution_not_confirmed": self.metrics.mtf_execution_not_confirmed,
                "mtf_execution_confirmation_shadow": self.metrics.mtf_execution_confirmation_shadow,
                "mtf_execution_counter_regime": self.metrics.mtf_execution_counter_regime,
                "mtf_direction_mismatch": self.metrics.mtf_direction_mismatch,
                "mtf_stale_context": self.metrics.mtf_stale_context,
                "mtf_guided_candidates_generated": self.metrics.mtf_guided_candidates_generated,
                "mtf_legacy_candidates_shadowed": self.metrics.mtf_legacy_candidates_shadowed,
                "mtf_setup_continuation": self.metrics.mtf_setup_continuation,
                "mtf_setup_pullback": self.metrics.mtf_setup_pullback,
                "mtf_setup_reentry_ready": self.metrics.mtf_setup_reentry_ready,
                "mtf_setup_no_setup": self.metrics.mtf_setup_no_setup,
                "mtf_setup_overextended": self.metrics.mtf_setup_overextended,
                "mtf_setup_invalid": self.metrics.mtf_setup_invalid,
                "equal_execution_candles_skipped": self.metrics.equal_execution_candles_skipped,
                "replayed_execution_candles_skipped": self.metrics.replayed_execution_candles_skipped,
                "malformed_execution_candles_skipped": self.metrics.malformed_execution_candles_skipped,
                "finalized_signal_replays_skipped": self.metrics.finalized_signal_replays_skipped,
                "final_decision_lookup_failures": self.metrics.final_decision_lookup_failures,
                "top_selection_reject_reasons": dict(sorted(self._last_scan_rejection_summary.items(), key=lambda item: item[1], reverse=True)[:3]),
                "top_selection_advisory_reasons": dict(sorted(self._last_scan_advisory_summary.items(), key=lambda item: item[1], reverse=True)[:3]),
                "decision_gate_blockers": self._last_scan_gate_blockers,
                "agent_shadow_queue_depth": self.metrics.agent_shadow_queue_depth,
                "agent_shadow_dropped": self.metrics.agent_shadow_dropped,
                "agent_shadow_deferred": self.metrics.agent_shadow_deferred,
                "agent_shadow_persistence_retries": self.metrics.agent_shadow_persistence_retries,
                "agent_shadow_lock_wait_ms": self.metrics.agent_shadow_lock_wait_ms,
                "agent_shadow_worker_count": self.metrics.agent_shadow_worker_count,
                "market_agent_pass": self.metrics.market_agent_pass,
                "market_agent_reject": self.metrics.market_agent_reject,
                "market_agent_defer": self.metrics.market_agent_defer,
                "signal_candidates_generated": self.metrics.signal_candidates_generated,
                "signal_no_candidate": self.metrics.signal_no_candidate,
                "quality_pass": self.metrics.quality_pass,
                "quality_reject": self.metrics.quality_reject,
                "quality_defer": self.metrics.quality_defer,
                "phase_b_errors": self.metrics.phase_b_errors,
                "heartbeat_persistence_failures": self.metrics.heartbeat_persistence_failures,
                "heartbeat_persistence_recoveries": self.metrics.heartbeat_persistence_recoveries,
                "heartbeat_persistence_degraded": self.metrics.heartbeat_persistence_degraded,
                "heartbeat_persistence_failure_streak": self._heartbeat_persistence_failure_streak,
                "heartbeat_persistence_failure_threshold": self._heartbeat_persistence_failure_threshold,
            },
        )

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
        if self._market_data_health_status == "DEGRADED":
            flags.append("MARKET_DATA_DEGRADED")
        elif self._market_data_health_status == "UNAVAILABLE":
            flags.append("MARKET_DATA_UNAVAILABLE")
        effective_status = status or self._runtime_status
        if effective_status == "OPERATING" and self._execution_reconciliation_blocked():
            effective_status = "RECOVERY_REQUIRED"
        return RuntimeStateSnapshot(
            mode=self.config.execution_mode.value,
            requested_mode=self.config.execution_mode.value,
            actual_mode=self.config.execution_mode.value,
            runtime_status=effective_status,
            heartbeat_age_sec=hb_age,
            instance_id=self.runtime_instance_id,
            startup_id=self.startup_id,
            campaign_id=os.getenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID") or None,
            burnin_run_id=self._burnin_run_id,
            release_id=os.getenv("ALPHAFORGE_RELEASE_ID") or self.config.phase7_burnin_release_id,
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
            diagnostics_json={"metrics": self.metrics.__dict__ if hasattr(self.metrics, "__dict__") else str(self.metrics), "diagnostic_mode": self.config.diagnostic_mode, "local_only_reconciliation_override": self._exchange_read_only_status == "LOCAL_ONLY", "recovery_scope_decision": self._recovery_decision, "provider_failure_class": self._provider_failure_class, "provider_failure_count": self._provider_failure_count, "market_data": {"health_status": self._market_data_health_status, "failure_streak": self._market_data_failure_streak, **self._last_market_data_diagnostics}},
        )

    def _execution_reconciliation_blocked(self) -> bool:
        if self.config.execution_mode == ExecutionMode.BACKTEST:
            return False
        return bool(self._reconciliation_persistence_unhealthy or self._fail_closed_reason
                    or self._recovery_required or self._unknown_exchange_state
                    or self._resolver_provider_unavailable or self._resolver_provider_recovery_pending
                    or self._exchange_read_only_status == "UNAVAILABLE"
                    or self._reconciliation_status in {"EXCHANGE_STATE_UNKNOWN", "DIRTY", "PERSISTENCE_FAILED"})

    def _mark_resolver_provider_failure(self) -> None:
        self._resolver_provider_unavailable = True
        self._resolver_provider_recovery_pending = False
        self._fail_closed_reason = self._fail_closed_reason or "RESOLVER_PROVIDER_UNAVAILABLE"
        self._runtime_status = "RECOVERY_REQUIRED"

    def _mark_resolver_provider_recovered(self) -> None:
        if self._resolver_provider_unavailable:
            self._resolver_provider_unavailable = False
            self._resolver_provider_recovery_pending = True
            self._runtime_status = "RECOVERY_REQUIRED"

    def _pause_for_provider_failure(self, reason: str) -> None:
        engine = self._resolve_persistence_engine()
        campaign_id = self._campaign_id or os.getenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID")
        if engine is not None and campaign_id and self.config.execution_mode == ExecutionMode.PAPER:
            with engine.begin() as conn:
                pause_campaign_for_provider_failure(conn, campaign_id, reason=reason,
                    details={"failure_class": self._provider_failure_class,
                             "failure_count": self._provider_failure_count,
                             "grace_seconds": self.config.provider_transient_outage_grace_seconds})
        self.shutdown()

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
        provider = self.live_reconciliation_provider or self.exchange_snapshot_provider
        probe = build_readonly_reconciliation_probe(provider)
        decision = evaluate_runtime_recovery(engine, mode=self.config.execution_mode.value, campaign_id=os.getenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID") or None, burnin_run_id=self._burnin_run_id, release_id=os.getenv("ALPHAFORGE_RELEASE_ID") or self.config.phase7_burnin_release_id, instance_id=self.runtime_instance_id, startup_id=self.startup_id, reconciliation_probe=probe)
        self._recovery_decision = decision
        latest = decision.get("latest")
        if decision["blocked"]:
            self._recovery_required = True
            self._fail_closed_reason = decision.get("reason") or ("UNCLEAN_SHUTDOWN_RECOVERY_REQUIRED" if decision.get("prior_unclean") else "RUNTIME_RECOVERY_REQUIRED")
            save_runtime_recovery_event(engine, instance_id=self.runtime_instance_id, startup_id=self.startup_id, mode=self.config.execution_mode.value, status="RECOVERY_REQUIRED", reason=self._fail_closed_reason, diagnostics={"blocking_snapshot_id": (latest or {}).get("id"), "blocking_instance_id": (latest or {}).get("instance_id"), "blocking_startup_id": (latest or {}).get("startup_id"), "original_reason": decision.get("original_reason"), "current_exposure_check": decision["current_exposure_check"], "scope_decision": decision["scope"], "query_errors": decision.get("query_errors", [])})
        elif latest and decision.get("prior_unclean"):
            # Append-only audit: do not manufacture a clean shutdown or copy the
            # inherited failure into this runtime's state.
            save_runtime_recovery_event(engine, instance_id=self.runtime_instance_id, startup_id=self.startup_id, mode=self.config.execution_mode.value, status="RECOVERY_SCOPE_EVALUATED", reason="UNRELATED_HISTORY_NON_BLOCKING", diagnostics={"blocking_snapshot_id": latest.get("id"), "blocking_instance_id": latest.get("instance_id"), "blocking_startup_id": latest.get("startup_id"), "original_reason": decision.get("original_reason"), "current_exposure_check": decision["current_exposure_check"], "scope_decision": "UNRELATED_HISTORY_NON_BLOCKING", "reconciliation_probe": decision.get("reconciliation_probe")})
        now = time.time()
        with engine.connect() as conn:
            for row in load_active_positions(conn):
                symbol = str(row['symbol'])
                self._active_positions[symbol] = float(row.get('qty') or 0.0)
                self._active_position_sides[symbol] = str(row.get('side') or 'UNKNOWN').upper()
            for row in load_pending_orders(conn):
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
            if self.config.execution_mode == ExecutionMode.LIVE:
                # Phase 6 mutation disablement is a deterministic local guard and
                # must win over persisted recovery history.
                await self._reject_real_live_in_phase6()
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
            self._attach_phase8_campaign()
            self._start_or_resume_burnin_run()
            await self._run_reconciliation_once()
            if self._fail_closed_reason and not self.config.diagnostic_mode:
                self._persist_runtime_state_snapshot("RECOVERY_REQUIRED")
                raise RuntimeError(self._fail_closed_reason)
        elif self.config.execution_mode == ExecutionMode.BACKTEST:
            self._unknown_exchange_state = False
            self._exchange_read_only_status = "NOT_REQUIRED_BACKTEST"
            self._reconciliation_status = "NOT_REQUIRED_BACKTEST"
        campaign_id = os.getenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID")
        if campaign_id and self._burnin_run_id and self.config.execution_mode == ExecutionMode.PAPER:
            from alphaforge.burnin_campaign import mark_attached_campaign_operational
            engine = self._resolve_persistence_engine()
            if engine is None:
                raise RuntimeError("PHASE8_CAMPAIGN_PERSISTENCE_UNAVAILABLE")
            with engine.begin() as conn:
                mark_attached_campaign_operational(conn, campaign_id, self._burnin_run_id,
                                                   runtime_instance_id=self.runtime_instance_id)
        self._runtime_status = "OPERATING"
        self._persist_runtime_state_snapshot("OPERATING")
        self._register_signals()
        self._initialize_agent_shadow()
        self._tasks = [
            asyncio.create_task(self._market_scan_loop(), name="market_scan_loop"),
            asyncio.create_task(self._heartbeat_loop(), name="metrics_heartbeat"),
            asyncio.create_task(self._reconciliation_loop(), name="reconciliation_loop"),
            asyncio.create_task(self._reject_forward_outcome_loop(), name="reject_forward_outcome_loop"),
        ]
        for task in self._tasks:
            task.add_done_callback(self._on_task_done)
        try:
            await self._stop_event.wait()
        finally:
            self._runtime_status = "STOPPING"
            self._last_shutdown_time = canonical_utc_timestamp()
            finalize_status = (
                "FAILED" if self._fatal_task_exception
                else "RECOVERY_REQUIRED" if self._recovery_required
                else "COMPLETED"
            )
            self._finalize_burnin_run(status=finalize_status)
            self._generate_burnin_snapshot(reason="shutdown")
            try:
                self._persist_runtime_heartbeat(runtime_state="STOPPING")
            except OperationalError as exc:
                if not is_sqlite_busy_error(exc):
                    raise
                logger.warning("shutdown_heartbeat_persistence_skipped reason=SQLITE_BUSY")
            try:
                self._persist_runtime_state_snapshot(
                    "FAILED" if self._fatal_task_exception
                    else "RECOVERY_REQUIRED" if self._recovery_required
                    else "CLEAN_SHUTDOWN"
                )
            except OperationalError as exc:
                if not is_sqlite_busy_error(exc):
                    raise
                logger.warning("shutdown_runtime_state_persistence_skipped reason=SQLITE_BUSY")
            await self._shutdown_tasks()
        if self._fatal_task_exception is not None:
            reason = "MARKET_SCAN_LOOP_FAILED" if self._fatal_task_name == "market_scan_loop" else f"RUNTIME_TASK_FAILED:{self._fatal_task_name}"
            raise RuntimeError(reason) from self._fatal_task_exception

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            exc = task.exception()
            if exc is not None:
                self._fatal_task_exception = exc
                self._fatal_task_name = task.get_name()
                logger.exception("runtime_task_failed task=%s", task.get_name(), exc_info=exc)
                self.shutdown()

    def _git_commit(self) -> str:
        try:
            return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=os.getcwd(), text=True, timeout=2).strip()
        except Exception:
            return "UNKNOWN_GIT_COMMIT"


    def _phase8_execution_cost_config_hash(self) -> str:
        return self._phase8_runtime_hashes().get("execution_cost_config_hash", "")

    def _phase8_runtime_hashes(self, symbols: list[str] | None = None, intervals: list[str] | None = None) -> dict[str, Any]:
        cfg = self._canonical_filter_config()
        resolved_symbols = list(symbols if symbols is not None else (cfg.get("symbols") or cfg.get("active_symbols") or []))
        resolved_intervals = list(intervals if intervals is not None else (cfg.get("intervals") or cfg.get("timeframes") or []))
        configured_release = os.getenv("ALPHAFORGE_RELEASE_ID")
        release_id = configured_release or self.config.phase7_burnin_release_id
        ident = build_phase8_campaign_identity(self.config, resolved_symbols, resolved_intervals, release_id=release_id, paper_slippage_bps=self.paper_slippage_bps)
        return {**ident, "execution_mode": self.config.execution_mode.value, "release_id_source": "environment:ALPHAFORGE_RELEASE_ID" if configured_release else "runtime_config:phase7_burnin_release_id"}


    def _attach_phase8_campaign(self, campaign_id: str | None = None) -> None:
        campaign_id = campaign_id or os.getenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID")
        if not campaign_id:
            return
        engine = self._resolve_persistence_engine()
        if engine is None:
            self._fail_closed_reason = "PHASE8_CAMPAIGN_PERSISTENCE_UNAVAILABLE"
            raise RuntimeError(self._fail_closed_reason)
        observed = None
        mismatches: dict[str, dict[str, Any]] = {}
        reason_map = {
            "config_hash": "PHASE8_CAMPAIGN_CONFIG_DRIFT",
            "strategy_config_hash": "PHASE8_CAMPAIGN_STRATEGY_DRIFT",
            "universe_hash": "PHASE8_CAMPAIGN_UNIVERSE_DRIFT",
            "execution_cost_config_hash": "PHASE8_CAMPAIGN_EXECUTION_COST_DRIFT",
            "release_id": "PHASE8_CAMPAIGN_RELEASE_MISMATCH",
            "execution_mode": "PHASE8_CAMPAIGN_EXECUTION_MODE_INVALID",
        }
        with engine.begin() as conn:
            bootstrap_campaign_schema(conn)
            campaign, run, mapping, attachment_error = load_active_campaign_attachment(conn, campaign_id)
            if campaign is None:
                self._fail_closed_reason = attachment_error or "PHASE8_CAMPAIGN_NOT_FOUND"
                raise RuntimeError(self._fail_closed_reason)
            campaign_identity = campaign_attachment_identity(campaign)
            self._campaign_id = campaign_id
            self._campaign_intervals = tuple(str(value) for value in (campaign.get("intervals") or []))
            self._campaign_symbols = frozenset(str(value).upper() for value in (campaign.get("symbols") or []))
            provenance = dict(campaign.get("source_provenance") or {})
            observed_provider_scope = canonical_paper_source_exchanges(provenance)
            run_identity = run_attachment_identity(run) if run else {}
            runtime_identity = self._phase8_runtime_hashes(campaign.get("symbols") or [], campaign.get("intervals") or [])
            expected_provider_scope = tuple(runtime_identity["config_payload"]["paper_source_exchanges"])
            self._campaign_source_exchanges = frozenset(expected_provider_scope)
            provider_drift = observed_provider_scope != expected_provider_scope
            campaign_run_mismatches = identity_mismatches(campaign_identity, run_identity, ATTACHMENT_IDENTITY_FIELDS) if run else {"active_run": {"expected": campaign.get("active_run_id"), "observed": None}}
            # git_commit is persisted provenance and must agree campaign-to-run;
            # it is not a runtime-config field and is therefore not fabricated for runtime comparison.
            run_runtime_mismatches = identity_mismatches(run_identity, runtime_identity, RUNTIME_ATTACHMENT_IDENTITY_FIELDS) if run else {}
            campaign_runtime_mismatches = identity_mismatches(campaign_identity, runtime_identity, CAMPAIGN_RUNTIME_IDENTITY_FIELDS)
            reason = None
            try:
                paper_fee_valid = self.config.paper_fee_bps is not None and float(self.config.paper_fee_bps) >= 0
            except (TypeError, ValueError):
                paper_fee_valid = False
            if self.config.execution_mode is ExecutionMode.PAPER and not paper_fee_valid:
                reason = "PHASE8_PAPER_FEE_ASSUMPTION_INVALID"
            elif attachment_error:
                reason = attachment_error
            elif provider_drift:
                reason = "PHASE8_CAMPAIGN_PROVIDER_DRIFT"
            elif campaign_run_mismatches:
                reason = "PHASE8_CAMPAIGN_RUN_IDENTITY_MISMATCH"
            elif run_runtime_mismatches or campaign_runtime_mismatches:
                all_runtime_mismatches = {**run_runtime_mismatches, **campaign_runtime_mismatches}
                reason = reason_map[next(key for key in CAMPAIGN_RUNTIME_IDENTITY_FIELDS if key in all_runtime_mismatches)]
            if reason:
                fail_active_campaign_run(conn, campaign_id, reason, details={
                    "reason": reason,
                    "campaign_identity": campaign_identity,
                    "run_identity": run_identity,
                    "runtime_identity": runtime_identity,
                    "campaign_run_mismatches": campaign_run_mismatches,
                    "run_runtime_mismatches": run_runtime_mismatches or campaign_runtime_mismatches,
                    "provider_scope": {"expected": list(expected_provider_scope),
                                       "observed": list(observed_provider_scope)},
                    "identity_sources": {"campaign": "burnin_campaigns", "run": "burnin_runs", "runtime_release": runtime_identity.get("release_id_source")},
                    "active_run_mapping": mapping or {},
                })
                with contextlib.suppress(Exception): conn.commit()
                self._fail_closed_reason = reason
                raise RuntimeError(reason)
            if not self._campaign_symbols or not self._campaign_source_exchanges:
                reason = ("PHASE8_CAMPAIGN_UNIVERSE_INVALID" if not self._campaign_symbols
                          else "PHASE8_CAMPAIGN_PROVIDER_IDENTITY_INVALID")
                fail_active_campaign_run(conn, campaign_id, reason,
                                         details={"source_provenance": provenance,
                                                  "symbols": sorted(self._campaign_symbols)})
                self._fail_closed_reason = reason
                raise RuntimeError(reason)
            self._burnin_run_id = campaign.get("active_run_id") or self._burnin_run_id
            open_positions = conn.execute(text("SELECT signal_id,symbol,side,notional FROM burnin_pending_position_outcomes WHERE campaign_id=:cid AND status='OPEN'"), {"cid": campaign_id}).mappings().all()
            for position in open_positions:
                symbol = str(position.get("symbol") or "").upper()
                if symbol:
                    self._active_positions[symbol] = float(position.get("notional") or 0.0)
                    self._active_position_sides[symbol] = str(position.get("side") or "UNKNOWN").upper()
                    self._last_lifecycle_state_by_symbol[symbol] = LifecycleState.POSITION_OPENED.value
                    signal_id = str(position.get("signal_id") or "").strip()
                    if signal_id:
                        self._last_lifecycle_state_by_signal[signal_id] = LifecycleState.POSITION_OPENED.value
                        self._current_signal_id_by_symbol[symbol] = signal_id
            burnin_campaign_event(conn, campaign_id, "PHASE8_CAMPAIGN_ATTACHED", details={"observed": observed, "runtime_instance_id": self.runtime_instance_id, "active_run_id": self._burnin_run_id})
            self._restore_rejects_persisted(conn=conn)

    def _start_or_resume_burnin_run(self) -> None:
        if self.config.execution_mode not in {ExecutionMode.PAPER, ExecutionMode.LIVE_PRECHECK} or self._burnin_run_id:
            return
        engine = self._resolve_persistence_engine()
        if engine is None:
            self._burnin_evidence_incomplete = True
            if self.config.execution_mode == ExecutionMode.LIVE_PRECHECK:
                self._fail_closed_reason = "PHASE7_BURNIN_PERSISTENCE_UNAVAILABLE"
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
                    strategy_config_hash=burnin_config_hash({"min_signal_score": self.config.min_signal_score, "min_effective_rr": self.config.min_effective_rr, "min_rr": self.config.min_rr, "regime_direction_threshold": self.config.regime_direction_threshold, "setup_direction_threshold": self.config.setup_direction_threshold, "execution_direction_threshold": self.config.execution_direction_threshold}),
                    universe_hash=burnin_universe_hash(symbols, intervals),
                    source_provenance=source,
                    symbols=symbols,
                    intervals=intervals,
                )
                persist_burnin_run(conn, run)
            self._restore_rejects_persisted()
        except Exception as exc:
            self._burnin_evidence_incomplete = True
            self._fail_closed_reason = "PHASE7_BURNIN_PERSISTENCE_FAILURE"
            logger.exception("phase7_burnin_run_persistence_failed", exc_info=exc)

    def _phase7_costs_from_execution_ctx(self, execution_ctx: Mapping[str, Any]) -> dict[str, Any]:
        model = build_execution_cost_model(execution_ctx, include_missing_penalty=False)
        spread = execution_ctx.get("spread_pct")
        slip = execution_ctx.get("expected_slippage_pct")
        fee = execution_ctx.get("fee_pct")
        funding = execution_ctx.get("funding_rate_pct")
        latency_ms = execution_ctx.get("latency_ms")
        return {
            "spread_cost": None if spread is None else model.spread_penalty,
            "entry_slippage_cost": None if slip is None else model.slippage_penalty / 2.0,
            "exit_slippage_cost": None if slip is None else model.slippage_penalty / 2.0,
            "fee_cost": None if fee is None else model.fee_penalty,
            "funding_cost": None if funding is None else model.funding_penalty,
            "latency_cost": None if latency_ms is None else model.latency_penalty,
            "volatility_penalty": model.volatility_penalty,
            "liquidity_penalty": model.liquidity_penalty,
            "execution_cost_unit": "R",
        }

    @staticmethod
    def _canonical_market_regime(payload: Mapping[str, Any]) -> str:
        """Prefer complete MTF market-regime evidence over legacy decision labels."""
        mtf = payload.get("mtf") if isinstance(payload.get("mtf"), Mapping) else {}
        regime_layer = mtf.get("regime") if isinstance(mtf.get("regime"), Mapping) else {}
        mtf_regime = regime_layer.get("regime")
        mtf_evidence_status = str(regime_layer.get("evidence_status") or "").upper()
        if mtf_regime and mtf_evidence_status == "COMPLETE":
            return str(mtf_regime).upper()
        execution_ctx = payload.get("execution_ctx") if isinstance(payload.get("execution_ctx"), Mapping) else {}
        fallback = (
            payload.get("regime")
            or execution_ctx.get("volatility_regime")
            or payload.get("volatility_regime")
            or "UNKNOWN"
        )
        return str(fallback)

    def _persist_burnin_decision(self, payload: Mapping[str, Any], *,
                                 lifecycle_state: str | None = None,
                                 conn: Any | None = None) -> None:
        if self.config.execution_mode not in {ExecutionMode.PAPER, ExecutionMode.LIVE_PRECHECK}:
            return
        self._assert_campaign_candidate(str(payload.get("symbol") or ""),
                                        payload.get("source_exchange"), "DECISION_PERSISTENCE")
        if not self._burnin_run_id:
            self._start_or_resume_burnin_run()
        engine = self._resolve_persistence_engine()
        if (engine is None and conn is None) or not self._burnin_run_id:
            self._burnin_evidence_incomplete = True
            return
        try:
            execution_ctx = dict(payload.get("execution_ctx") or {})
            canonical_regime = self._canonical_market_regime(payload)
            source_regime = payload.get("regime")
            legacy_decision_regime = payload.get("legacy_decision_regime")
            if (legacy_decision_regime is None and source_regime is not None
                    and str(source_regime).upper() != str(canonical_regime).upper()):
                legacy_decision_regime = source_regime
            missing = [name for name in ("signal_id", "symbol", "decision") if not payload.get(name)]
            def persist(target: Any) -> None:
                campaign_id = self._campaign_id or os.getenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID")
                runtime_identity = campaign_id or f"standalone:{self._burnin_run_id}"
                metrics = {k: payload.get(k) for k in ("score", "rr", "candidate_rr",
                    "expected_fill", "executable_raw_rr", "remaining_execution_penalty",
                    "effective_rr", "confidence", "spread_pct", "expected_slippage_pct",
                    "latency_ms", "funding_rate_pct", "entry", "sl", "tp",
                    "entry_source", "stop_source", "target_source", "setup_timeframe",
                    "execution_timeframe", "structural_stop", "structural_target",
                    "all_failed_gates", "failed_gate_evidence", "stop_distance_pct",
                    "min_signal_score", "min_raw_rr", "min_effective_rr",
                    "min_stop_pct", "max_stop_pct")}
                metrics.update({"reject_decision_id": payload.get("reject_decision_id"),
                                "signal_id": payload.get("signal_id"),
                                "setup_identity": payload.get("setup_identity"),
                                "campaign_id": campaign_id, "runtime_identity": runtime_identity,
                                "geometry_status": payload.get("geometry_status"),
                                "geometry_reason": payload.get("geometry_reason"),
                                "geometry_source": payload.get("geometry_source"),
                                "canonical_market_regime": canonical_regime,
                                "legacy_decision_regime": legacy_decision_regime,
                                "mtf": payload.get("mtf"),
                                "base_exec_direction": payload.get("base_exec_direction"),
                                "resolved_state": payload.get("resolved_state"),
                                "final_direction": payload.get("final_direction"),
                                "override_reason": payload.get("override_reason"),
                                "mtf_execution_confirmation_mode": payload.get("mtf_execution_confirmation_mode", "ENFORCE"),
                                "shadow_mtf_execution_reason": payload.get("shadow_mtf_execution_reason"),
                                "authoritative_reject_reason": payload.get("authoritative_reject_reason"),
                                "enforce_counterfactual_reject_reason": payload.get("enforce_counterfactual_reject_reason")})
                if str(payload.get("decision") or "").upper() == "REJECTED":
                    metrics.update({
                        "primary_reject_reason": payload.get("primary_reject_reason"),
                        "reject_reasons": payload.get("reject_reasons"),
                    })
                setup_identity = payload.get("setup_identity")
                reject_decision_id = payload.get("reject_decision_id")
                observation_id = (
                    "reject_obs:" + canonical_hash({
                        "reject_decision_id": str(reject_decision_id),
                    })[:24]
                    if str(payload.get("decision") or "").upper() == "REJECTED"
                    and reject_decision_id else
                    self._setup_observation_id(
                        str(setup_identity), str(payload.get("decision")))
                    if setup_identity else
                    f"obs:{payload.get('signal_id')}:{payload.get('decision')}:{canonical_utc_timestamp()}"
                )
                persist_burnin_observation(target, observation_id=observation_id, burnin_run_id=self._burnin_run_id, release_id=os.getenv("ALPHAFORGE_RELEASE_ID", self.config.phase7_burnin_release_id), execution_mode=self.config.execution_mode.value, symbol=payload.get("symbol"), interval=payload.get("timeframe"), regime=canonical_regime, decision=payload.get("decision"), lifecycle_state=lifecycle_state, metrics=metrics, source_provenance={"provider": self.scanner_source or "UNKNOWN", "source_exchange": payload.get("source_exchange"), "campaign_id": campaign_id, "runtime_identity": runtime_identity}, missing_fields=missing)
                decision_upper = str(payload.get("decision") or "").upper()
                reject_reason = (
                    payload.get("primary_reject_reason")
                    or payload.get("reject_reason")
                    or payload.get("reason")
                    if decision_upper == "REJECTED"
                    else None
                )
                portfolio_diagnostics = payload.get("portfolio_diagnostics")
                portfolio_snapshot = (
                    dict(portfolio_diagnostics.get("snapshot") or {})
                    if isinstance(portfolio_diagnostics, Mapping)
                    else {}
                )
                diagnostics = {
                    "observation_id": observation_id,
                    "campaign_id": campaign_id,
                    "candidate_rr": payload.get("candidate_rr"),
                    "planned_entry": payload.get("entry"),
                    "expected_fill": payload.get("expected_fill"),
                    "executable_raw_rr": payload.get("executable_raw_rr"),
                    "remaining_execution_penalty": payload.get("remaining_execution_penalty"),
                    "execution_cost_semantics": payload.get("execution_cost_semantics"),
                    "geometry_status": payload.get("geometry_status"),
                    "geometry_reason": payload.get("geometry_reason"),
                    "geometry_source": payload.get("geometry_source"),
                    "all_failed_gates": payload.get("all_failed_gates"),
                    "failed_gate_evidence": payload.get("failed_gate_evidence"),
                    "reject_execution_basis": payload.get("reject_execution_basis"),
                    "no_submit_verified": self.config.execution_mode is ExecutionMode.LIVE_PRECHECK,
                    "execution_ctx": execution_ctx,
                }
                evidence_id = "runtime_decision:" + canonical_hash({
                    "burnin_run_id": self._burnin_run_id,
                    "observation_id": observation_id,
                })[:24]
                persisted_evidence = save_decision_evidence(
                    target,
                    evidence_id=evidence_id,
                    run_id=self._burnin_run_id,
                    profile_id=payload.get("profile_id"),
                    profile_name=payload.get("profile_name"),
                    mode=self.config.execution_mode.value,
                    timestamp=payload.get("decision_time") or payload.get("decision_timestamp"),
                    symbol=payload.get("symbol"),
                    side=payload.get("side"),
                    setup_type=payload.get("setup_type"),
                    setup_reason=payload.get("setup_reason"),
                    regime=canonical_regime,
                    lifecycle_state_before=payload.get("lifecycle_state_before"),
                    lifecycle_state_after=lifecycle_state,
                    decision=payload.get("decision"),
                    score=payload.get("score"),
                    raw_rr=payload.get("executable_raw_rr") if payload.get("executable_raw_rr") is not None else payload.get("rr"),
                    effective_rr=payload.get("effective_rr"),
                    min_effective_rr=payload.get("min_effective_rr") if payload.get("min_effective_rr") is not None else float(self.config.min_effective_rr),
                    expectancy=payload.get("expectancy"),
                    expectancy_bucket=payload.get("expectancy_bucket"),
                    reject_reason=reject_reason,
                    entry=payload.get("expected_fill") if payload.get("expected_fill") is not None else payload.get("entry"),
                    sl=payload.get("sl") if payload.get("sl") is not None else payload.get("structural_stop"),
                    tp=payload.get("tp") if payload.get("tp") is not None else payload.get("structural_target"),
                    volume_24h_usdt=execution_ctx.get("volume_24h_usdt"),
                    spread_pct=execution_ctx.get("spread_pct"),
                    funding_rate_pct=execution_ctx.get("funding_rate_pct"),
                    expected_slippage_pct=execution_ctx.get("expected_slippage_pct"),
                    liquidity_score=execution_ctx.get("liquidity_score"),
                    volatility_regime=execution_ctx.get("volatility_regime"),
                    cost_penalty=payload.get("remaining_execution_penalty") if payload.get("remaining_execution_penalty") is not None else execution_ctx.get("cost_penalty"),
                    total_cost_pct=execution_ctx.get("total_cost_pct"),
                    total_explicit_cost_pct=execution_ctx.get("total_explicit_cost_pct"),
                    spread_source=execution_ctx.get("spread_source"),
                    slippage_source=execution_ctx.get("slippage_source"),
                    fee_pct=execution_ctx.get("fee_pct"),
                    fee_source=execution_ctx.get("fee_source"),
                    funding_source=execution_ctx.get("funding_source"),
                    latency_ms=execution_ctx.get("latency_ms"),
                    latency_source=execution_ctx.get("latency_source"),
                    liquidity_status=execution_ctx.get("liquidity_status"),
                    volatility_penalty_pct=execution_ctx.get("volatility_penalty_pct"),
                    volatility_source=execution_ctx.get("volatility_source"),
                    reject_flags=payload.get("all_failed_gates") or payload.get("reject_reasons"),
                    unavailable_fields=execution_ctx.get("unavailable_fields"),
                    diagnostics_json=diagnostics,
                    portfolio_equity=portfolio_snapshot.get("equity"),
                    available_balance=portfolio_snapshot.get("available_balance"),
                    open_position_count=portfolio_snapshot.get("open_position_count"),
                    max_open_positions=portfolio_snapshot.get("max_open_positions"),
                    total_notional_exposure=portfolio_snapshot.get("total_notional_exposure"),
                    max_notional_exposure=portfolio_snapshot.get("max_notional_exposure"),
                    symbol_notional_exposure=portfolio_snapshot.get("symbol_notional_exposure"),
                    max_symbol_notional=portfolio_snapshot.get("max_symbol_notional"),
                    side_exposure_long=portfolio_snapshot.get("side_exposure_long"),
                    side_exposure_short=portfolio_snapshot.get("side_exposure_short"),
                    net_exposure=portfolio_snapshot.get("net_exposure"),
                    gross_exposure=portfolio_snapshot.get("gross_exposure"),
                    daily_realized_pnl=portfolio_snapshot.get("daily_realized_pnl"),
                    daily_loss_pct=portfolio_snapshot.get("daily_loss_pct"),
                    max_daily_loss_pct=portfolio_snapshot.get("max_daily_loss_pct"),
                    rolling_drawdown_pct=portfolio_snapshot.get("rolling_drawdown_pct"),
                    consecutive_loss_count=portfolio_snapshot.get("consecutive_loss_count"),
                    correlation_group=portfolio_snapshot.get("correlation_group"),
                    correlation_group_exposure=portfolio_snapshot.get("correlation_group_exposure"),
                    correlated_position_count=portfolio_snapshot.get("correlated_position_count"),
                    risk_flags=payload.get("risk_flags"),
                    portfolio_reject_reason=payload.get("portfolio_reject_reason"),
                    portfolio_risk_state=payload.get("portfolio_risk_state"),
                    portfolio_diagnostics_json=portfolio_diagnostics,
                    signal_id=payload.get("signal_id"),
                    order_id=payload.get("order_id"),
                    position_id=payload.get("position_id"),
                    lifecycle_id=payload.get("lifecycle_id"),
                    lifecycle_seq=payload.get("lifecycle_seq"),
                )
                if not persisted_evidence:
                    raise RuntimeError("DECISION_EVIDENCE_PERSISTENCE_FAILED")
                update_burnin_run_counters(target, self._burnin_run_id)
            if conn is not None:
                persist(conn)
            else:
                with engine.begin() as owned_conn:
                    persist(owned_conn)
            self.metrics.burnin_observations += 1
        except Exception as exc:
            self._burnin_evidence_incomplete = True
            self._fail_closed_reason = "PHASE7_BURNIN_PERSISTENCE_FAILURE"
            logger.exception("phase7_burnin_decision_persistence_failed", exc_info=exc)
            if conn is not None:
                raise

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
                canonical_run_rejects = self._canonical_persisted_reject_count(
                    conn, campaign_scope=False)
                conn.execute(text("""INSERT INTO burnin_execution_metrics(burnin_run_id,release_id,metric_window,spread_baseline,spread_current,slippage_baseline,slippage_current,latency_baseline,latency_current,fill_probability_baseline,fill_probability_current,timeout_rate,execution_rejects,stale_data_count,reconciliation_quality,status,generated_at,schema_version) VALUES (:bid,:rel,'CURRENT',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,0,:rejects,:stale,:recon,:status,:ts,'phase7_burnin_v1')"""), {"bid": self._burnin_run_id, "rel": os.getenv("ALPHAFORGE_RELEASE_ID", self.config.phase7_burnin_release_id), "rejects": canonical_run_rejects, "stale": len(self._stale_market_data_symbols), "recon": self._reconciliation_status, "status": "STABLE" if self._reconciliation_status == "CLEAN" else "INSUFFICIENT_EVIDENCE", "ts": now})
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
                current = conn.execute(text("SELECT status FROM burnin_runs WHERE burnin_run_id=:bid"), {"bid": self._burnin_run_id}).scalar_one_or_none()
                update_burnin_run_counters(conn, self._burnin_run_id,
                    status=status if current == "RUNNING" else None,
                    end_time=canonical_utc_timestamp() if current == "RUNNING" else None)
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
        readiness_campaign_id = self._campaign_id or os.getenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID")
        evaluator = LiveReadinessEvaluator(
            engine,
            evidence_mode=ExecutionMode.PAPER.value,
            campaign_id=readiness_campaign_id,
            burnin_run_id=self._burnin_run_id,
            release_id=os.getenv("ALPHAFORGE_RELEASE_ID") or self.config.phase7_burnin_release_id,
            runtime_instance_id=self.runtime_instance_id,
            min_effective_rr=self.config.min_effective_rr,
            require_run_scope=True,
        )
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
                    intended_orders=list(self._pending_orders.values()) if self.config.execution_mode in {ExecutionMode.LIVE, ExecutionMode.LIVE_PRECHECK} else [],
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
        raw_rr = signal_payload.get("risk_reward", signal_payload.get("rr", 0.0))
        rr_metrics = self._execution_rr_metrics(
            raw_rr, market_ctx, market_ctx.get("execution_ctx", market_ctx),
        )
        return {
            "decision": order_plan.decision,
            "reason": order_plan.reason,
            "order_type": order_plan.order_type,
            "confidence": float(order_plan.confidence),
            "score": float(getattr(score_ctx, "total_score", 0.0) or 0.0),
            "reject_reason": canonical_reject_reason(order_plan.reason) if order_plan.decision != "ACCEPTED" else "",
            "raw_rr": float(raw_rr or 0.0),
            "executable_raw_rr": rr_metrics["executable_raw_rr"],
            "expected_fill": rr_metrics["expected_fill"],
            "effective_rr": rr_metrics["effective_rr"],
            "explanation": explanation,
        }

    @staticmethod
    def _effective_rr_from_execution(raw_rr: Any, execution_ctx: Mapping[str, Any]) -> float:
        rr = float(raw_rr or 0.0)
        model = build_execution_cost_model(execution_ctx, include_missing_penalty=False)
        return round(max(rr - model.total_penalty, 0.0), 6)

    @staticmethod
    def _fill_adjusted_raw_rr(*, side: Any, fill: Any, stop: Any, target: Any) -> float | None:
        try:
            fill_price = float(fill)
            stop_price = float(stop)
            target_price = float(target)
        except (TypeError, ValueError):
            return None
        if not all(math.isfinite(value) for value in (fill_price, stop_price, target_price)):
            return None
        normalized_side = str(side or "").strip().upper()
        if normalized_side == "LONG":
            risk_distance = fill_price - stop_price
            reward_distance = target_price - fill_price
        elif normalized_side == "SHORT":
            risk_distance = stop_price - fill_price
            reward_distance = fill_price - target_price
        else:
            return None
        if risk_distance <= 0 or reward_distance <= 0:
            return 0.0
        executable_rr = reward_distance / risk_distance
        return executable_rr if math.isfinite(executable_rr) else 0.0

    def _expected_fill_price(self, market_ctx: Mapping[str, Any], execution_ctx: Mapping[str, Any]) -> tuple[float | None, float | None]:
        try:
            entry = float(market_ctx.get("entry"))
        except (TypeError, ValueError):
            return None, None
        if not math.isfinite(entry) or entry <= 0:
            return None, None
        if self.config.execution_mode is ExecutionMode.PAPER:
            try:
                if self.paper_slippage_bps is not None:
                    slippage_pct = max(float(self.paper_slippage_bps), 0.0) / 10_000.0
                elif execution_ctx.get("expected_slippage_pct") is not None:
                    slippage_pct = max(float(execution_ctx.get("expected_slippage_pct")), 0.0)
                else:
                    return None, None
            except (TypeError, ValueError):
                return None, None
        else:
            try:
                raw_slippage = execution_ctx.get("expected_slippage_pct")
                if raw_slippage is None:
                    return None, None
                slippage_pct = max(float(raw_slippage), 0.0)
            except (TypeError, ValueError):
                return None, None
        side = str(market_ctx.get("side") or "LONG").strip().upper()
        if side not in {"LONG", "SHORT"}:
            return None, None
        fill = entry * (1.0 + slippage_pct if side == "LONG" else 1.0 - slippage_pct)
        return round(fill, 8), slippage_pct

    def _execution_rr_metrics(self, raw_rr: Any, market_ctx: Mapping[str, Any], execution_ctx: Mapping[str, Any]) -> dict[str, Any]:
        candidate_rr = float(raw_rr or 0.0)
        expected_fill, fill_slippage_pct = self._expected_fill_price(market_ctx, execution_ctx)
        executable_raw_rr = self._fill_adjusted_raw_rr(
            side=market_ctx.get("side"), fill=expected_fill,
            stop=market_ctx.get("sl"), target=market_ctx.get("tp"),
        )
        model = build_execution_cost_model(execution_ctx, include_missing_penalty=False)
        if executable_raw_rr is None:
            executable_raw_rr = candidate_rr
            remaining_penalty = model.total_penalty
        else:
            # Entry slippage is already represented by expected_fill. Keep only
            # costs that the simulated/executable fill does not encode,
            # including the modelled exit-slippage half.
            remaining_penalty = max(model.total_penalty - model.slippage_penalty / 2.0, 0.0)
        effective_rr = max(executable_raw_rr - remaining_penalty, 0.0)
        cost_semantics = None
        if expected_fill is not None:
            try:
                cost_semantics = build_execution_cost_semantics(
                    entry=market_ctx.get("entry"),
                    expected_fill=expected_fill,
                    actual_fill=None,
                    side=market_ctx.get("side"),
                    expected_fill_provenance=(
                        PROVENANCE_MODELLED
                        if self.config.execution_mode is ExecutionMode.PAPER
                        else PROVENANCE_ESTIMATED
                    ),
                    decision_timestamp=market_ctx.get("decision_timestamp"),
                ).decision_time_dict()
            except ValueError:
                # Canonical metrics are unavailable when side/price evidence is
                # incomplete; existing effective-RR behavior remains authoritative.
                cost_semantics = None
        return {
            "candidate_rr": round(candidate_rr, 6),
            "expected_fill": expected_fill,
            "fill_slippage_pct": fill_slippage_pct,
            "expected_execution_cost_price": (
                cost_semantics.get("expected_execution_cost_price") if cost_semantics else None),
            "expected_execution_cost_pct": (
                cost_semantics.get("expected_execution_cost_pct") if cost_semantics else None),
            "expected_execution_cost_bps": (
                cost_semantics.get("expected_execution_cost_bps") if cost_semantics else None),
            "execution_cost_semantics": cost_semantics,
            "executable_raw_rr": round(executable_raw_rr, 6),
            "remaining_execution_penalty": round(remaining_penalty, 6),
            "effective_rr": round(effective_rr, 6),
        }

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
            "execution_context_complete": all(
                not execution_context_is_unavailable(c.get("execution_context"))
                for c in comparisons
            ),
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
        except Exception as exc:
            self._last_error = f"MARKET_SCAN_LOOP_FAILED:{exc.__class__.__name__}:{exc}"
            self._runtime_status = "FAILED"
            self._persist_runtime_state_snapshot("MARKET_SCAN_LOOP_FAILED")
            logger.exception("MARKET_SCAN_LOOP_FAILED")
            raise

    def _canonical_filter_config(self) -> dict[str, Any]:
        return runtime_filter_config(self.config, mode=self.config.execution_mode.value)

    async def _scan_once(self) -> None:
        self._sync_resolved_paper_positions()
        if self._kill_switch_active():
            self._last_scan_gate_blockers = ["KILL_SWITCH_ACTIVE"]
            return
        self.metrics.scans += 1
        candidates = await self.market_scanner()
        market_data_diagnostics = dict(getattr(candidates, "diagnostics", {}) or {})
        market_data_status = str(
            market_data_diagnostics.get("status")
            or ("AVAILABLE" if candidates else "VALID_EMPTY")
        ).upper()
        self._last_market_data_diagnostics = market_data_diagnostics
        if market_data_status == "UNAVAILABLE":
            self._market_data_failure_streak += 1
            self._market_data_health_status = (
                "UNAVAILABLE" if self._market_data_failure_streak >= 2 else "DEGRADED"
            )
        else:
            self._market_data_failure_streak = 0
            self._market_data_health_status = market_data_status
        if self._burnin_run_id and self._campaign_symbols:
            candidates = [candidate for candidate in candidates
                          if str(candidate.get("symbol") or "").upper() in self._campaign_symbols
                          and str(candidate.get("source_exchange") or "").lower() in self._campaign_source_exchanges]
            deduplicated: dict[str, dict[str, Any]] = {}
            for candidate in candidates:
                deduplicated.setdefault(str(candidate.get("symbol") or "").upper(), candidate)
            candidates = list(deduplicated.values())
        selector_config = {**self._canonical_filter_config(), "include_rejected": True}
        if (
            self.config.execution_mode is ExecutionMode.PAPER
            and self.config.require_mtf_alignment
            and self.config.mtf_guided_signal_generation_enabled
        ):
            # Binance selector trend/chop fields are coarse 24h absolute-change
            # proxies. Preserve them for ranking and diagnostics, but let the
            # canonical 1h/15m/1m stack make the PAPER regime/setup decision.
            selector_config["advisory_reasons"] = (
                "TOO_CHOPPY",
                "WEAK_TREND_AND_NO_RANGE_EDGE",
            )
        pre_selection = select_symbols(candidates, selector_config)
        selected = [row for row in pre_selection if row.tradable][: self.config.max_symbols_per_scan]
        reject_reasons: dict[str, int] = {}
        advisory_reasons: dict[str, int] = {}
        for row in pre_selection:
            for reason in row.reject_reasons:
                reject_reasons[reason] = reject_reasons.get(reason, 0) + 1
            for reason in row.diagnostics.get("advisory_reasons", []):
                advisory_reasons[reason] = advisory_reasons.get(reason, 0) + 1
        self._last_scan_rejection_summary = reject_reasons
        self._last_scan_advisory_summary = advisory_reasons
        if not candidates:
            self._last_scan_gate_blockers = (
                ["MARKET_DATA_UNAVAILABLE"]
                if self._market_data_health_status in {"DEGRADED", "UNAVAILABLE"}
                else ["NO_MARKET_CANDIDATES"]
            )
        elif not selected:
            self._last_scan_gate_blockers = ["NO_TRADABLE_SYMBOLS_AFTER_SELECTION"]
        else:
            self._last_scan_gate_blockers = []
        self.metrics.symbols_selected += len(selected)

        if selected and self.selected_candidate_enricher is not None:
            selected_inputs = [dict(row.diagnostics.get("inputs", {})) for row in selected]
            enriched = await self.selected_candidate_enricher(selected_inputs)
            if len(enriched) != len(selected_inputs):
                raise RuntimeError("selected_candidate_enricher changed candidate count")
            for row, before, after in zip(selected, selected_inputs, enriched):
                if (after.get("symbol"), after.get("source_exchange")) != (
                    before.get("symbol"), before.get("source_exchange")
                ):
                    raise RuntimeError("selected_candidate_enricher changed candidate identity")
                row.diagnostics["inputs"] = dict(after)
        self.metrics.last_scan_ts = canonical_utc_timestamp()

        for symbol_result in selected:
            inputs = symbol_result.diagnostics.get("inputs", {})
            self._assert_campaign_candidate(symbol_result.symbol, inputs.get("source_exchange"),
                                            "BEFORE_PROCESS_SYMBOL")
            raw_candle_ts = inputs.get("execution_candle_open_ts")
            candle_key = self._execution_candle_market_key(symbol_result.symbol, inputs)
            candle_ts = self._normalize_execution_candle_open_ts(raw_candle_ts)
            if raw_candle_ts is not None:
                if candle_key is None or candle_ts is None:
                    self.metrics.malformed_execution_candles_skipped += 1
                    logger.warning(
                        "execution_candle_identity_invalid symbol=%s source_exchange=%s timeframe=%s",
                        symbol_result.symbol, inputs.get("source_exchange"), inputs.get("timeframe"),
                    )
                    continue
                latest = self._latest_execution_candle_by_market.get(candle_key)
                if latest is not None and candle_ts <= latest:
                    if candle_ts == latest:
                        self.metrics.equal_execution_candles_skipped += 1
                    else:
                        self.metrics.replayed_execution_candles_skipped += 1
                    continue
            await self._process_symbol(symbol_result)
            if candle_key is not None and candle_ts is not None:
                self._latest_execution_candle_by_market[candle_key] = candle_ts

    def _assert_campaign_candidate(self, symbol: str, source_exchange: Any, stage: str) -> None:
        """Fail closed and durably diagnose an attached-campaign scope violation."""
        if not self._burnin_run_id or not self._campaign_symbols:
            return
        normalized_symbol = str(symbol or "").upper()
        normalized_source = str(source_exchange or "").lower()
        if normalized_symbol in self._campaign_symbols and normalized_source in self._campaign_source_exchanges:
            return
        campaign_id = self._campaign_id or os.getenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID")
        details = {"campaign_id": campaign_id, "burnin_run_id": self._burnin_run_id,
                   "declared_symbols": sorted(self._campaign_symbols),
                   "declared_source_exchanges": sorted(self._campaign_source_exchanges),
                   "observed_symbol": normalized_symbol, "observed_source_exchange": normalized_source,
                   "stage": stage}
        engine = self._resolve_persistence_engine()
        if engine is not None and campaign_id:
            with engine.begin() as conn:
                burnin_campaign_event(conn, campaign_id, "CAMPAIGN_UNIVERSE_RUNTIME_MISMATCH",
                                      details=details)
        self._fail_closed_reason = "CAMPAIGN_UNIVERSE_RUNTIME_MISMATCH"
        raise RuntimeError(self._fail_closed_reason)

    @staticmethod
    def _apply_state_direction_geometry(market_ctx: dict[str, Any], final_direction: str) -> str | None:
        """Bind an evidenced state direction to the canonical pre-entry geometry."""
        base_direction = str(market_ctx.get("side") or "").strip().upper()
        final_direction = str(final_direction or "").strip().upper()
        if final_direction not in {"LONG", "SHORT"}:
            return "MTF_STATE_NO_TRADE"
        if base_direction == final_direction:
            return None
        try:
            entry = float(market_ctx.get("entry"))
            stop_key = "sl" if market_ctx.get("sl") is not None else "stop"
            target_key = "tp" if market_ctx.get("tp") is not None else "target"
            stop = float(market_ctx.get(stop_key))
            target = float(market_ctx.get(target_key))
        except (TypeError, ValueError):
            return "MTF_STATE_GEOMETRY_UNAVAILABLE"
        if not all(math.isfinite(value) and value > 0 for value in (entry, stop, target)):
            return "MTF_STATE_GEOMETRY_UNAVAILABLE"
        valid_base_geometry = ((base_direction == "LONG" and stop < entry < target)
                               or (base_direction == "SHORT" and target < entry < stop))
        if not valid_base_geometry:
            return "MTF_STATE_GEOMETRY_UNAVAILABLE"
        mirrored_stop, mirrored_target = (2 * entry - stop), (2 * entry - target)

        if not all(
            math.isfinite(value) and value > 0
            for value in (mirrored_stop, mirrored_target)
        ):
            return "MTF_STATE_GEOMETRY_UNAVAILABLE"

        valid_final_geometry = (
            (
                final_direction == "LONG"
                and mirrored_stop < entry < mirrored_target
            )
            or (
                final_direction == "SHORT"
                and mirrored_target < entry < mirrored_stop
            )
        )
        if not valid_final_geometry:
            return "MTF_STATE_GEOMETRY_UNAVAILABLE"

        market_ctx.update({
            "side": final_direction,
            stop_key: mirrored_stop,
            target_key: mirrored_target,
            "state_direction_geometry_override": True,
        })
        # Keep aliases synchronized when providers supplied both forms.
        if "sl" in market_ctx and stop_key != "sl":
            market_ctx["sl"] = mirrored_stop
        if "stop" in market_ctx and stop_key != "stop":
            market_ctx["stop"] = mirrored_stop
        if "tp" in market_ctx and target_key != "tp":
            market_ctx["tp"] = mirrored_target
        if "target" in market_ctx and target_key != "target":
            market_ctx["target"] = mirrored_target
        return None

    async def _process_symbol(self, selection: SymbolSelectionResult) -> None:
        market_ctx = dict(selection.diagnostics.get("inputs", {}))
        self._assert_campaign_candidate(selection.symbol, market_ctx.get("source_exchange"),
                                        "PROCESS_SYMBOL")
        market_ctx.setdefault("mode", self.config.execution_mode.value)
        if self.config.execution_mode is ExecutionMode.PAPER:
            try:
                paper_slippage_bps = float(self.paper_slippage_bps)
                if paper_slippage_bps < 0:
                    raise ValueError
            except (TypeError, ValueError):
                paper_slippage_bps = None
            if market_ctx.get("expected_slippage_pct") in (None, ""):
                if paper_slippage_bps is not None:
                    market_ctx.update(
                        expected_slippage_pct=paper_slippage_bps / 10_000.0,
                        slippage_status="MODEL_ESTIMATE",
                        slippage_source="CONFIGURED_PAPER_ASSUMPTION",
                    )
                else:
                    market_ctx.update(
                        slippage_status="UNAVAILABLE",
                        slippage_source="UNAVAILABLE",
                    )

            try:
                paper_fee_bps = float(self.config.paper_fee_bps)
                if paper_fee_bps < 0:
                    raise ValueError
            except (TypeError, ValueError):
                paper_fee_bps = None
            if paper_fee_bps is not None:
                market_ctx.update(
                    fee_pct=paper_fee_bps / 10_000.0,
                    fee_status="CONFIGURED",
                    fee_source="CONFIGURED_PAPER_ASSUMPTION",
                )

            try:
                paper_latency_ms = float(self.config.paper_execution_latency_ms)
                if paper_latency_ms < 0:
                    raise ValueError
            except (TypeError, ValueError):
                paper_latency_ms = None

            if paper_latency_ms is not None:
                market_ctx.update(
                    latency_ms=paper_latency_ms,
                    latency_status="MODEL_ESTIMATE",
                    latency_source="CONFIGURED_PAPER_ASSUMPTION",
                )

        signal_id = self._resolve_signal_id(selection.symbol, market_ctx)
        finalized = self._canonical_final_decision_recorded(signal_id)
        if finalized is not False:
            if finalized:
                self.metrics.finalized_signal_replays_skipped += 1
            return
        execution_ctx = build_execution_context(market_ctx)
        market_ctx["execution_ctx"] = execution_ctx
        legacy_candidate = {key: market_ctx.get(key) for key in (
            "side", "entry", "sl", "tp", "rr", "setup_type", "setup_reason",
            "geometry_status", "geometry_reason", "geometry_source",
        )}
        geometry_required = (
            self.config.execution_mode is ExecutionMode.PAPER
            and self.selected_candidate_enricher is not None
            and str(market_ctx.get("source_exchange") or "").lower() == "binance"
            and str(market_ctx.get("timeframe") or "").lower() == "1m"
        )
        mtf = market_ctx.get("mtf")
        if self.config.execution_mode is ExecutionMode.PAPER and self.config.require_mtf_alignment:
            source_exchange = str(market_ctx.get("source_exchange") or "").strip().lower()
            if self.mtf_context_provider is not None and source_exchange == "binance":
                decision_ms = int(float(market_ctx.get("market_ts") or time.time()) * 1000)
                mtf = await self.mtf_context_provider.build(selection.symbol, market_ctx, execution_ctx=execution_ctx, decision_ts_ms=decision_ms,
                    regime_timeframe=self.config.regime_timeframe, setup_timeframe=self.config.setup_timeframe,
                    execution_timeframe=self.config.execution_timeframe,
                    state_direction_resolution_enabled=self.config.enable_state_direction_resolution)
                market_ctx["mtf"] = mtf
                self.metrics.mtf_contexts_built += 1
            elif source_exchange != "binance":
                mtf = {"regime": None, "setup": None, "execution": None,
                    "alignment": {"aligned": False, "direction": None,
                        "reasons": ["MTF_EXECUTION_UNAVAILABLE"],
                        "timeframes": {"regime": self.config.regime_timeframe,
                            "setup": self.config.setup_timeframe, "execution": self.config.execution_timeframe}},
                    "provider": None, "source_exchange": source_exchange or None,
                    "evidence_status": "INCOMPLETE", "provenance_error": "UNSUPPORTED_MTF_PROVIDER"}
                market_ctx["mtf"] = mtf
            alignment_value = (mtf or {}).get("alignment", {}) if isinstance(mtf, Mapping) else {}
            alignment = alignment_value if isinstance(alignment_value, Mapping) else {}
            generation_value = (mtf or {}).get("generation", {}) if isinstance(mtf, Mapping) else {}
            generation = generation_value if isinstance(generation_value, Mapping) else {}
            generated_candidate = generation.get("candidate")
            guided = (self.config.mtf_guided_signal_generation_enabled
                      and generation.get("mode") == "REGIME_GUIDED")
            aligned_side = str(alignment.get("direction") or "").strip().upper()
            candidate_side = str(market_ctx.get("side") or "").strip().upper()
            setup_layer = (mtf or {}).get("setup") if isinstance((mtf or {}).get("setup"), Mapping) else {}
            setup_phase = str(setup_layer.get("phase") or "UNKNOWN").upper()
            setup_identity = self._setup_opportunity_identity(selection.symbol, mtf)
            if setup_identity is not None:
                market_ctx["setup_identity"] = setup_identity
                mtf = {**dict(mtf or {}), "setup_identity": setup_identity}
                market_ctx["mtf"] = mtf
            base_exec_direction = str(
                alignment.get("base_exec_direction") or candidate_side
            ).strip().upper()

            if guided:
                metric_name = {"CONTINUATION": "mtf_setup_continuation",
                               "PULLBACK": "mtf_setup_pullback",
                               "REENTRY_READY": "mtf_setup_reentry_ready",
                               "NO_SETUP": "mtf_setup_no_setup",
                               "OVEREXTENDED": "mtf_setup_overextended",
                               "INVALID": "mtf_setup_invalid"}.get(setup_phase)
                if metric_name:
                    setattr(self.metrics, metric_name, getattr(self.metrics, metric_name) + 1)
            if guided and alignment.get("aligned"):
                generated_side = str(generated_candidate.get("side") or "").upper() if isinstance(generated_candidate, Mapping) else ""
                if (generation.get("evidence_status") != "COMPLETE"
                        or not isinstance(generated_candidate, Mapping)
                        or generated_side != aligned_side):
                    alignment = {**alignment, "aligned": False, "direction": None,
                        "reasons": list(dict.fromkeys([*(alignment.get("reasons") or []),
                                                       "MTF_GUIDED_GEOMETRY_UNAVAILABLE"]))}
                    mtf = {**dict(mtf or {}), "alignment": alignment}
                    market_ctx["mtf"] = mtf
                else:
                    legacy_conflict = candidate_side in {"LONG", "SHORT"} and candidate_side != generated_side
                    shadow_evidence = {
                        "disposition": ("COUNTER_REGIME_LEGACY_CANDIDATE" if legacy_conflict
                                        else "LEGACY_CANDIDATE_REPLACED"),
                        "candidate": legacy_candidate,
                        "would_have_been_side": candidate_side or None,
                        "generated_side": generated_side,
                        "forward_label_eligible_if_rejected": all(
                            legacy_candidate.get(key) is not None for key in ("entry", "sl", "tp")
                        ),
                    }
                    mtf = {**dict(mtf or {}), "shadow_evidence": shadow_evidence}
                    market_ctx.update(dict(generated_candidate))
                    regime_layer = mtf.get("regime") if isinstance(mtf.get("regime"), Mapping) else {}
                    market_ctx["regime"] = regime_layer.get("regime")
                    market_ctx["setup"] = generated_candidate.get("setup_type")
                    market_ctx["mtf"] = mtf
                    execution_ctx = build_execution_context(market_ctx)
                    market_ctx["execution_ctx"] = execution_ctx
                    self.metrics.mtf_guided_candidates_generated += 1
                    self.metrics.mtf_legacy_candidates_shadowed += int(legacy_conflict)
            elif (
                alignment.get("aligned")
                and candidate_side in {"LONG", "SHORT"}
                and (
                    (
                        self.config.enable_state_direction_resolution
                        and base_exec_direction != candidate_side
                    )
                    or (
                        not self.config.enable_state_direction_resolution
                        and aligned_side != candidate_side
                    )
                )
            ):
                if self.config.enable_state_direction_resolution:
                    alignment = {
                        **alignment,
                        "direction": None,
                        "final_direction": "NO_TRADE",
                        "override_reason": "BASE_EXEC_GEOMETRY_MISMATCH",
                    }
                # Compatibility path for legacy/custom providers that return
                # alignment evidence without the additive generation contract.
                alignment = {**alignment, "aligned": False,
                    "reasons": list(dict.fromkeys([*(alignment.get("reasons") or []), "MTF_DIRECTION_MISMATCH"]))}
                mtf = {**dict(mtf or {}), "alignment": alignment}
                market_ctx["mtf"] = mtf
            for field_name in ("base_exec_direction", "resolved_state", "final_direction", "override_reason"):
                market_ctx[field_name] = alignment.get(field_name)
            if not alignment.get("aligned"):
                reasons = list(alignment.get("reasons") or ["MTF_EXECUTION_UNAVAILABLE"])
                if (geometry_required
                        and str(legacy_candidate.get("geometry_status") or "").upper() != "COMPLETE"):
                    geometry_reason = str(legacy_candidate.get("geometry_reason") or "GEOMETRY_INCOMPLETE").upper()
                    if self._execution_candle_decision_identity(selection.symbol, market_ctx) is None:
                        self._persist_geometry_diagnostic(selection.symbol, market_ctx, geometry_reason)
                        return
                    reasons = list(dict.fromkeys([geometry_reason, *reasons]))
                shadow_mtf_execution_reason = (
                    "MTF_EXECUTION_NOT_CONFIRMED"
                    if (
                        self.config.mtf_execution_confirmation_mode == "SHADOW"
                        and reasons == ["MTF_EXECUTION_NOT_CONFIRMED"]
                    )
                    else None
                )
                if shadow_mtf_execution_reason:
                    market_ctx.update(
                        mtf_execution_confirmation_mode="SHADOW",
                        shadow_mtf_execution_reason=shadow_mtf_execution_reason,
                        enforce_counterfactual_reject_reason=shadow_mtf_execution_reason,
                        authoritative_reject_reason=None,
                    )
                    self.metrics.mtf_execution_not_confirmed += 1
                    self.metrics.mtf_execution_confirmation_shadow += 1
                reason = reasons[0]
                canonical_setup_reject = setup_phase in {"NO_SETUP", "INVALID", "OVEREXTENDED"}
                if canonical_setup_reject and setup_identity is not None:
                    reject_decision_id = f"reject:{self._setup_decision_key(setup_identity)}"
                    if self._setup_decision_recorded(setup_identity, decision="REJECTED"):
                        return
                    self._canonical_setup_reject_ids.add(setup_identity)
                    signal_id = f"runtime:{hashlib.sha256(setup_identity.encode('utf-8')).hexdigest()[:24]}"
                else:
                    reject_decision_id = None
                if not shadow_mtf_execution_reason:
                    self._prepare_state_direction_shadow(
                        symbol=selection.symbol, signal_id=signal_id, market_ctx=market_ctx,
                        mtf=dict(mtf or {}), execution_ctx=execution_ctx)
                    self.metrics.mtf_alignment_reject += 1
                    self.metrics.mtf_regime_missing += int("MTF_REGIME_UNAVAILABLE" in reasons)
                    self.metrics.mtf_setup_missing += int("MTF_SETUP_UNAVAILABLE" in reasons)
                    self.metrics.mtf_execution_missing += int("MTF_EXECUTION_UNAVAILABLE" in reasons)
                    self.metrics.mtf_execution_not_confirmed += int("MTF_EXECUTION_NOT_CONFIRMED" in reasons)
                    self.metrics.mtf_execution_counter_regime += int("MTF_EXECUTION_COUNTER_REGIME" in reasons)
                    self.metrics.mtf_stale_context += int("MTF_CONTEXT_STALE" in reasons)
                    self.metrics.mtf_direction_mismatch += int(any(
                        "MISMATCH" in item or item == "MTF_EXECUTION_COUNTER_REGIME" for item in reasons
                    ))
                    await self._emit_lifecycle_event(LifecycleState.SIGNAL_CREATED.value, selection.symbol, {"reason": "", "signal_id": signal_id})
                    reject_payload = {**market_ctx, "signal_id": signal_id, "symbol": selection.symbol,
                        "mode": self.config.execution_mode.value, "phase": "final", "decision": "REJECTED",
                        "reason": reason, "reject_reason": reason, "confidence": 0.0, "score": None,
                        "rr": market_ctx.get("rr"), "effective_rr": None, "explanation": "mtf_alignment_gate",
                        "execution_ctx": execution_ctx, "timeframe": self.config.execution_timeframe, "mtf": mtf,
                        "primary_reject_reason": reason, "reject_reasons": reasons,
                        "authoritative_reject_reason": reason}
                    if reject_decision_id is not None:
                        reject_payload["reject_decision_id"] = reject_decision_id
                    await self._persist_reject(reject_payload)
                    await self._emit_lifecycle_event(LifecycleState.SIGNAL_REJECTED.value, selection.symbol, reject_payload)
                    return
            if setup_identity is not None and self._setup_decision_recorded(
                    setup_identity, decision="ACCEPTED"):
                return
            self._prepare_state_direction_shadow(
                symbol=selection.symbol, signal_id=signal_id, market_ctx=market_ctx,
                mtf=dict(mtf or {}), execution_ctx=execution_ctx)
            if self.config.enable_state_direction_resolution and not guided:
                geometry_reason = self._apply_state_direction_geometry(
                    market_ctx,
                    aligned_side,
                )
                if geometry_reason:
                    alignment = {
                        **alignment,
                        "aligned": False,
                        "direction": None,
                        "final_direction": "NO_TRADE",
                        "override_reason": geometry_reason,
                        "reasons": list(
                            dict.fromkeys([
                                *(alignment.get("reasons") or []),
                                geometry_reason,
                            ])
                        ),
                    }
                    mtf = {**dict(mtf or {}), "alignment": alignment}
                    market_ctx.update(
                        mtf=mtf,
                        final_direction="NO_TRADE",
                        override_reason=geometry_reason,
                    )

                    self.metrics.mtf_alignment_reject += 1

                    await self._emit_lifecycle_event(
                        LifecycleState.SIGNAL_CREATED.value,
                        selection.symbol,
                        {"reason": "", "signal_id": signal_id},
                    )

                    reject_payload = {
                        **market_ctx,
                        "signal_id": signal_id,
                        "symbol": selection.symbol,
                        "mode": self.config.execution_mode.value,
                        "phase": "final",
                        "decision": "REJECTED",
                        "reason": geometry_reason,
                        "reject_reason": geometry_reason,
                        "primary_reject_reason": geometry_reason,
                        "reject_reasons": [geometry_reason],
                        "confidence": 0.0,
                        "score": None,
                        "rr": market_ctx.get("rr"),
                        "effective_rr": None,
                        "explanation": "mtf_state_direction_geometry_gate",
                        "execution_ctx": execution_ctx,
                        "timeframe": self.config.execution_timeframe,
                        "mtf": mtf,
                    }

                    await self._persist_reject(reject_payload)
                    await self._emit_lifecycle_event(
                        LifecycleState.SIGNAL_REJECTED.value,
                        selection.symbol,
                        reject_payload,
                    )
                    return
            if alignment.get("aligned"):
                self.metrics.mtf_alignment_pass += 1
        if geometry_required and str(market_ctx.get("geometry_status") or "").upper() != "COMPLETE":
            reason = str(market_ctx.get("geometry_reason") or "GEOMETRY_INCOMPLETE").upper()
            if self._execution_candle_decision_identity(selection.symbol, market_ctx) is None:
                self._persist_geometry_diagnostic(selection.symbol, market_ctx, reason)
                return
            await self._emit_lifecycle_event(LifecycleState.SIGNAL_CREATED.value, selection.symbol,
                                             {"reason": "", "signal_id": signal_id})
            reject_payload = {
                **market_ctx, "signal_id": signal_id, "symbol": selection.symbol,
                "mode": self.config.execution_mode.value, "phase": "final", "decision": "REJECTED",
                "reason": reason, "reject_reason": reason, "confidence": 0.0, "score": None,
                "side": None, "sl": None, "tp": None, "rr": None, "effective_rr": None,
                "explanation": "canonical_geometry_gate", "execution_ctx": execution_ctx,
                "timeframe": self.config.execution_timeframe,
            }
            await self._persist_reject(reject_payload)
            await self._emit_lifecycle_event(LifecycleState.SIGNAL_REJECTED.value,
                                             selection.symbol, reject_payload)
            return
        market_ctx.setdefault("decision_timestamp", canonical_utc_timestamp())
        raw_rr = market_ctx.get("rr")
        rr_metrics = self._execution_rr_metrics(raw_rr, market_ctx, execution_ctx)
        effective_rr = float(rr_metrics["effective_rr"] or 0.0)
        market_ctx.update(rr_metrics)
        execution_safety = None
        if self.config.execution_mode in {
            ExecutionMode.PAPER,
            ExecutionMode.LIVE_PRECHECK,
        }:
            execution_safety = evaluate_execution_safety(
                execution_ctx,
                effective_rr=effective_rr,
                min_effective_rr=self.config.min_effective_rr,
                thresholds=self._canonical_filter_config(),
                require_measured=self.config.execution_mode is ExecutionMode.LIVE_PRECHECK,
            )
            execution_ctx = {
                **execution_ctx,
                "safety_evidence_status": execution_safety.get("execution_evidence_status"),
                "safety_missing_fields": list(execution_safety.get("missing_fields") or []),
                "safety_fake_zero_fields": list(execution_safety.get("fake_zero_fields") or []),
                "safety_all_failed_gates": list(execution_safety.get("all_failed_gates") or []),
                "unavailable_fields": list(execution_safety.get("missing_fields") or []),
                "total_explicit_cost_pct": execution_safety.get("total_explicit_cost_pct"),
                "volatility_penalty_pct": execution_safety.get("volatility_penalty"),
            }
            market_ctx["execution_ctx"] = execution_ctx
            market_ctx["execution_safety"] = execution_safety

        # Guided MTF structural geometry is market evidence, not a target to be
        # widened until it passes policy.  Reject a sub-minimum structural stop
        # before AIBrain scoring so the causal geometry failure remains the
        # authoritative primary reason instead of being hidden by downstream
        # score/expectancy effects.  Wide-stop softening still depends on score
        # and effective RR, so that policy deliberately remains downstream.
        mtf_for_geometry = (
            market_ctx.get("mtf") if isinstance(market_ctx.get("mtf"), Mapping) else {}
        )
        generation_for_geometry = (
            mtf_for_geometry.get("generation")
            if isinstance(mtf_for_geometry.get("generation"), Mapping)
            else {}
        )
        guided_geometry = (
            self.config.execution_mode is ExecutionMode.PAPER
            and str(generation_for_geometry.get("mode") or "").upper() == "REGIME_GUIDED"
            and str(generation_for_geometry.get("evidence_status") or "").upper() == "COMPLETE"
            and isinstance(generation_for_geometry.get("candidate"), Mapping)
        )
        if guided_geometry:
            try:
                planned_entry = float(market_ctx.get("entry"))
                planned_stop = float(market_ctx.get("sl"))
            except (TypeError, ValueError):
                planned_entry = planned_stop = float("nan")
            if (
                math.isfinite(planned_entry)
                and planned_entry > 0.0
                and math.isfinite(planned_stop)
            ):
                stop_distance_pct = abs(planned_entry - planned_stop) / planned_entry * 100.0
                market_ctx.update(
                    stop_distance_pct=stop_distance_pct,
                    min_stop_pct=float(self.config.min_sl_pct),
                    max_stop_pct=float(self.config.max_sl_pct),
                )
                if stop_distance_pct < float(self.config.min_sl_pct):
                    reject_reason = "STOP_TOO_TIGHT"
                    reject_payload = {
                        "signal_id": signal_id,
                        "symbol": selection.symbol,
                        "mode": self.config.execution_mode.value,
                        "phase": "final",
                        "decision": "REJECTED",
                        "reason": reject_reason,
                        "reject_reason": reject_reason,
                        "primary_reject_reason": reject_reason,
                        "reject_reasons": [reject_reason],
                        "confidence": 0.0,
                        "score": None,
                        "rr": raw_rr,
                        "candidate_rr": rr_metrics["candidate_rr"],
                        "expected_fill": rr_metrics["expected_fill"],
                        "executable_raw_rr": rr_metrics["executable_raw_rr"],
                        "remaining_execution_penalty": rr_metrics["remaining_execution_penalty"],
                        "effective_rr": effective_rr,
                        "execution_cost_semantics": rr_metrics.get("execution_cost_semantics"),
                        "explanation": "guided_geometry_viability_gate",
                        "execution_ctx": execution_ctx,
                        "execution_safety": execution_safety,
                        "spread_pct": execution_ctx.get("spread_pct"),
                        "expected_slippage_pct": execution_ctx.get("expected_slippage_pct"),
                        "latency_ms": execution_ctx.get("latency_ms"),
                        "funding_rate_pct": execution_ctx.get("funding_rate_pct"),
                        "liquidity_score": execution_ctx.get("liquidity_score"),
                        "orderbook_imbalance": execution_ctx.get("orderbook_imbalance"),
                        "volatility_regime": execution_ctx.get("volatility_regime"),
                    }
                    await self._persist_reject({**market_ctx, **reject_payload})
                    await self._emit_lifecycle_event(
                        LifecycleState.SIGNAL_REJECTED.value,
                        selection.symbol,
                        {**market_ctx, **reject_payload},
                    )
                    return

        risk_reject = self._evaluate_runtime_risk(selection.symbol, market_ctx)
        await self._emit_lifecycle_event(LifecycleState.SIGNAL_CREATED.value, selection.symbol, {"reason": "", "signal_id": signal_id})
        if self._kill_switch_active():
            reject_payload = {"signal_id": signal_id, "symbol": selection.symbol, "mode": self.config.execution_mode.value, "phase": "final", "decision": "REJECTED", "reason": "KILL_SWITCH_ACTIVE", "confidence": 0.0, "score": 0.0, "rr": raw_rr, "effective_rr": effective_rr, "explanation": "runtime_control_gate", "execution_ctx": execution_ctx, "spread_pct": execution_ctx.get("spread_pct"), "expected_slippage_pct": execution_ctx.get("expected_slippage_pct"), "latency_ms": execution_ctx.get("latency_ms"), "funding_rate_pct": execution_ctx.get("funding_rate_pct"), "orderbook_imbalance": execution_ctx.get("orderbook_imbalance"), "volatility_regime": execution_ctx.get("volatility_regime")}
            await self._persist_reject({**market_ctx, **reject_payload})
            await self._emit_lifecycle_event(LifecycleState.SIGNAL_REJECTED.value, selection.symbol, {**reject_payload, "reject_reason": "KILL_SWITCH_ACTIVE"})
            return
        if risk_reject is not None:
            reject_payload = {"signal_id": signal_id, "symbol": selection.symbol, "mode": self.config.execution_mode.value, "phase": "final", "decision": "REJECTED", "reason": risk_reject, "confidence": 0.0, "score": 0.0, "rr": raw_rr, "effective_rr": effective_rr, "explanation": "runtime_risk_gate", "execution_ctx": execution_ctx, "spread_pct": execution_ctx.get("spread_pct"), "expected_slippage_pct": execution_ctx.get("expected_slippage_pct"), "latency_ms": execution_ctx.get("latency_ms"), "funding_rate_pct": execution_ctx.get("funding_rate_pct"), "orderbook_imbalance": execution_ctx.get("orderbook_imbalance"), "volatility_regime": execution_ctx.get("volatility_regime")}
            await self._persist_reject({**market_ctx, **reject_payload})
            await self._emit_lifecycle_event(LifecycleState.SIGNAL_REJECTED.value, selection.symbol, {**reject_payload, "reject_reason": risk_reject})
            return
        legacy_pre_ai_execution_reason = None
        if execution_safety is not None:
            failed_execution_gates = set(execution_safety.get("all_failed_gates") or [])
            legacy_pre_ai_execution_reason = next(
                (
                    gate
                    for gate in (
                        "SPREAD_TOO_HIGH",
                        "SLIPPAGE_TOO_HIGH",
                        "FUNDING_TOO_HIGH",
                    )
                    if gate in failed_execution_gates
                ),
                None,
            )
        if legacy_pre_ai_execution_reason is not None:
            reject_payload = {
                "signal_id": signal_id,
                "symbol": selection.symbol,
                "mode": self.config.execution_mode.value,
                "phase": "final",
                "decision": "REJECTED",
                "reason": legacy_pre_ai_execution_reason,
                "primary_reject_reason": legacy_pre_ai_execution_reason,
                "confidence": 0.0,
                "score": 0.0,
                "rr": raw_rr,
                "candidate_rr": rr_metrics["candidate_rr"],
                "expected_fill": rr_metrics["expected_fill"],
                "executable_raw_rr": rr_metrics["executable_raw_rr"],
                "effective_rr": effective_rr,
                "explanation": "legacy_pre_ai_execution_gate",
                "execution_ctx": execution_ctx,
                "execution_safety": execution_safety,
                "spread_pct": execution_ctx.get("spread_pct"),
                "expected_slippage_pct": execution_ctx.get("expected_slippage_pct"),
                "latency_ms": execution_ctx.get("latency_ms"),
                "funding_rate_pct": execution_ctx.get("funding_rate_pct"),
                "liquidity_score": execution_ctx.get("liquidity_score"),
                "orderbook_imbalance": execution_ctx.get("orderbook_imbalance"),
                "volatility_regime": execution_ctx.get("volatility_regime"),
            }
            await self._persist_reject({**market_ctx, **reject_payload})
            await self._emit_lifecycle_event(
                LifecycleState.SIGNAL_REJECTED.value,
                selection.symbol,
                {**reject_payload, "reject_reason": legacy_pre_ai_execution_reason},
            )
            return
        signal_payload = self._build_signal(selection, market_ctx, signal_id=signal_id)
        signal_payload["reject_decision_id"] = self._canonical_reject_decision_id({
            **market_ctx, "signal_id": signal_id, "symbol": selection.symbol,
        })
        market_ctx, regime_ctx, stats_ctx = self._build_scoring_context(
            signal_payload, market_ctx
        )
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
            reject_payload = {"signal_id": signal_id, "symbol": selection.symbol, "mode": self.config.execution_mode.value, "phase": "final", "decision": "REJECTED", "reason": "KILL_SWITCH_ACTIVE", "confidence": order_plan.confidence, "score": getattr(score_ctx, "total_score", None), "rr": signal_payload.get("risk_reward"), "effective_rr": effective_rr, "explanation": "runtime_control_gate", "execution_ctx": execution_ctx, "spread_pct": execution_ctx.get("spread_pct"), "expected_slippage_pct": execution_ctx.get("expected_slippage_pct"), "latency_ms": execution_ctx.get("latency_ms"), "funding_rate_pct": execution_ctx.get("funding_rate_pct"), "orderbook_imbalance": execution_ctx.get("orderbook_imbalance"), "volatility_regime": execution_ctx.get("volatility_regime")}
            await self._persist_reject({**market_ctx, **reject_payload})
            await self._emit_lifecycle_event(LifecycleState.SIGNAL_REJECTED.value, selection.symbol, {**reject_payload, "reject_reason": "KILL_SWITCH_ACTIVE"})
            return

        if order_plan.decision != "ACCEPTED":
            reject_reason = score_reject_reason(score_ctx)
            await self._persist_reject({
                **market_ctx,
                "signal_id": signal_id,
                "symbol": selection.symbol,
                "mode": self.config.execution_mode.value,
                "phase": "final",
                "decision": order_plan.decision,
                "reason": reject_reason,
                "confidence": order_plan.confidence,
                "score": getattr(score_ctx, "total_score", None),
                "score_components": getattr(score_ctx, "components", None),
                "rr": signal_payload.get("risk_reward"),
                "side": signal_payload.get("side", market_ctx.get("side")),
                "setup_type": signal_payload.get("setup", signal_payload.get("setup_type")),
                "entry": signal_payload.get("entry_price", market_ctx.get("entry")),
                "sl": signal_payload.get("stop_loss", market_ctx.get("sl")),
                "tp": signal_payload.get("take_profit", market_ctx.get("tp")),
                "regime": signal_payload.get("regime", market_ctx.get("regime")),
                "effective_rr": effective_rr,
                "explanation": explanation,
                "execution_ctx": execution_ctx,
                "spread_pct": execution_ctx.get("spread_pct"),
                "expected_slippage_pct": execution_ctx.get("expected_slippage_pct"),
                "latency_ms": execution_ctx.get("latency_ms"),
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
            reject_payload = {"signal_id": signal_id, "symbol": selection.symbol, "mode": self.config.execution_mode.value, "phase": "final", "decision": "REJECTED", "reason": reject_reason, "confidence": order_plan.confidence, "score": getattr(score_ctx, "total_score", None), "rr": signal_payload.get("risk_reward"), "effective_rr": effective_rr, "explanation": "canonical_effective_rr_gate", "execution_ctx": execution_ctx, "spread_pct": execution_ctx.get("spread_pct"), "expected_slippage_pct": execution_ctx.get("expected_slippage_pct"), "latency_ms": execution_ctx.get("latency_ms"), "funding_rate_pct": execution_ctx.get("funding_rate_pct"), "orderbook_imbalance": execution_ctx.get("orderbook_imbalance"), "volatility_regime": execution_ctx.get("volatility_regime")}
            await self._persist_reject({**market_ctx, **reject_payload})
            await self._emit_lifecycle_event(LifecycleState.SIGNAL_REJECTED.value, selection.symbol, {**reject_payload, "reject_reason": reject_reason})
            return

        if execution_safety is not None and not bool(execution_safety.get("accepted")):
            reject_reason = str(
                execution_safety.get("primary_reject_reason") or "BAD_EXECUTION"
            )
            reject_payload = {
                "signal_id": signal_id,
                "symbol": selection.symbol,
                "mode": self.config.execution_mode.value,
                "phase": "final",
                "decision": "REJECTED",
                "reason": reject_reason,
                "primary_reject_reason": reject_reason,
                "confidence": order_plan.confidence,
                "score": getattr(score_ctx, "total_score", None),
                "rr": signal_payload.get("risk_reward"),
                "candidate_rr": rr_metrics["candidate_rr"],
                "expected_fill": rr_metrics["expected_fill"],
                "executable_raw_rr": rr_metrics["executable_raw_rr"],
                "effective_rr": effective_rr,
                "explanation": "canonical_execution_safety_gate",
                "execution_ctx": execution_ctx,
                "execution_safety": execution_safety,
                "spread_pct": execution_ctx.get("spread_pct"),
                "expected_slippage_pct": execution_ctx.get("expected_slippage_pct"),
                "latency_ms": execution_ctx.get("latency_ms"),
                "funding_rate_pct": execution_ctx.get("funding_rate_pct"),
                "liquidity_score": execution_ctx.get("liquidity_score"),
                "orderbook_imbalance": execution_ctx.get("orderbook_imbalance"),
                "volatility_regime": execution_ctx.get("volatility_regime"),
            }
            await self._persist_reject({**market_ctx, **reject_payload})
            await self._emit_lifecycle_event(
                LifecycleState.SIGNAL_REJECTED.value,
                selection.symbol,
                {**reject_payload, "reject_reason": reject_reason},
            )
            return

        candidate_notional = market_ctx.get("notional") or market_ctx.get("notional_usdt") or market_ctx.get("order_notional")
        inferred_equity = market_ctx.get("equity", market_ctx.get("available_balance"))
        available_balance = market_ctx.get("available_balance", inferred_equity)
        portfolio_evidence_source = (
            "MARKET_CONTEXT" if inferred_equity is not None and candidate_notional is not None
            else "MISSING"
        )
        portfolio_now = time.time()
        historical_risk: dict[str, Any] = {
            "daily_realized_pnl": None,
            "rolling_peak_equity": None,
            "rolling_drawdown_pct": None,
            "consecutive_loss_count": None,
            "symbol_consecutive_loss_count": None,
            "trades_today_symbol": None,
            "trades_today_global": None,
            "persisted_cooldown_until": None,
            "risk_state_complete": None,
            "risk_state_source": None,
            "risk_state_missing_fields": [],
        }
        cooldown_until = dict(self._symbol_cooldown_until)
        if self.config.execution_mode is ExecutionMode.PAPER:
            attached_campaign_id = (
                self._campaign_id or os.getenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID")
            )
            if attached_campaign_id:
                historical_risk = self._paper_portfolio_risk_state(
                    selection.symbol,
                    now_ts=portfolio_now,
                )
                portfolio_evidence_source = str(
                    historical_risk.get("risk_state_source")
                    or "PAPER_RISK_STATE_UNKNOWN"
                )
                if (
                    historical_risk.get("risk_state_source")
                    == "BURNIN_CAMPAIGN_EVIDENCE"
                ):
                    inferred_equity = historical_risk.get("equity")
                    available_balance = historical_risk.get("available_balance")
                elif inferred_equity is None:
                    inferred_equity = historical_risk.get("equity")
                    available_balance = historical_risk.get("available_balance")
                persisted_cooldown_until = historical_risk.get(
                    "persisted_cooldown_until"
                )
                if persisted_cooldown_until is not None:
                    cooldown_until[selection.symbol] = max(
                        float(cooldown_until.get(selection.symbol, 0.0) or 0.0),
                        float(persisted_cooldown_until),
                    )
            elif inferred_equity is None and self.config.paper_initial_equity is not None:
                inferred_equity = self.config.paper_initial_equity
                available_balance = self.config.paper_initial_equity
                portfolio_evidence_source = "CONFIGURED_PAPER_ACCOUNT"
            if candidate_notional is None and self.config.paper_candidate_notional is not None:
                candidate_notional = self.config.paper_candidate_notional
                if not attached_campaign_id:
                    portfolio_evidence_source = "CONFIGURED_PAPER_ACCOUNT"
                market_ctx["notional"] = candidate_notional
        elif candidate_notional is None:
            candidate_notional = min(float(self.config.max_symbol_notional or 0.0), float(self.config.max_notional_exposure or 0.0)) * 0.1
        snapshot = snapshot_from_state(
            mode=self.config.execution_mode.value,
            symbol=selection.symbol,
            side=str(market_ctx.get("side", signal_payload.get("side", "LONG"))),
            candidate_notional=candidate_notional,
            equity=inferred_equity,
            available_balance=available_balance,
            open_positions={k: {"notional": v, "side": self._active_position_sides.get(k, "UNKNOWN")} for k, v in self._active_positions.items()},
            config=self.config,
            now=portfolio_now,
            cooldown_until=cooldown_until,
            daily_realized_pnl=historical_risk.get("daily_realized_pnl"),
            trades_today_symbol=historical_risk.get("trades_today_symbol"),
            trades_today_global=historical_risk.get("trades_today_global"),
            consecutive_loss_count=historical_risk.get("consecutive_loss_count"),
            symbol_consecutive_loss_count=historical_risk.get("symbol_consecutive_loss_count"),
            rolling_peak_equity=historical_risk.get("rolling_peak_equity"),
            rolling_drawdown_pct=historical_risk.get("rolling_drawdown_pct"),
            risk_state_complete=historical_risk.get("risk_state_complete"),
            risk_state_source=historical_risk.get("risk_state_source"),
            risk_state_missing_fields=list(
                historical_risk.get("risk_state_missing_fields") or []
            ),
        )
        portfolio_decision = evaluate_portfolio_risk({"symbol": selection.symbol, "side": market_ctx.get("side"), "entry": market_ctx.get("entry"), "quantity": market_ctx.get("quantity", market_ctx.get("qty")), "notional": candidate_notional}, snapshot, self.config, mode=self.config.execution_mode.value)
        portfolio_decision.diagnostics["accounting_source"] = portfolio_evidence_source
        if not portfolio_decision.accepted:
            reject_reason = portfolio_decision.reject_reason or "UNKNOWN_PORTFOLIO_RISK"
            reject_payload = {"signal_id": signal_id, "symbol": selection.symbol, "mode": self.config.execution_mode.value, "phase": "final", "decision": "REJECTED", "reason": reject_reason, "reject_reason": reject_reason, "confidence": order_plan.confidence, "score": getattr(score_ctx, "total_score", None), "rr": signal_payload.get("risk_reward"), "effective_rr": effective_rr, "explanation": "portfolio_risk_gate", "execution_ctx": execution_ctx, "portfolio_reject_reason": reject_reason, "portfolio_risk_state": portfolio_decision.risk_state, "portfolio_diagnostics": portfolio_decision.diagnostics, "risk_flags": portfolio_decision.risk_flags, "spread_pct": execution_ctx.get("spread_pct"), "expected_slippage_pct": execution_ctx.get("expected_slippage_pct"), "latency_ms": execution_ctx.get("latency_ms"), "funding_rate_pct": execution_ctx.get("funding_rate_pct"), "orderbook_imbalance": execution_ctx.get("orderbook_imbalance"), "volatility_regime": execution_ctx.get("volatility_regime")}
            await self._persist_reject({**market_ctx, **reject_payload})
            await self._emit_lifecycle_event(LifecycleState.SIGNAL_REJECTED.value, selection.symbol, reject_payload)
            return

        if self.config.execution_mode in {ExecutionMode.PAPER, ExecutionMode.LIVE_PRECHECK}:
            if market_ctx.get("setup_identity"):
                self._accepted_setup_identities.add(str(market_ctx["setup_identity"]))
            await self._emit_lifecycle_event(LifecycleState.WAITING_ENTRY_ZONE.value, selection.symbol, {})
            await self._emit_lifecycle_event(LifecycleState.ENTRY_TRIGGERED.value, selection.symbol, {})
            # PAPER execution emits ORDER_PLACED after its simulated order result;
            # LIVE_PRECHECK has no execution call, so its no-submit evidence ends here.
            if self.config.execution_mode is ExecutionMode.LIVE_PRECHECK:
                await self._emit_lifecycle_event(LifecycleState.ORDER_PLACED.value, selection.symbol, {})
        else:
            await self._emit_lifecycle_event(LifecycleEventType.ENTRY_PENDING.value, selection.symbol, {})
            await self._emit_lifecycle_event(LifecycleEventType.ENTRY_SUBMITTED.value, selection.symbol, {})
        accepted_burnin_payload = {
            "signal_id": signal_id,
            "decision_time": market_ctx["decision_timestamp"],
            "setup_identity": market_ctx.get("setup_identity"),
            "symbol": selection.symbol,
            "side": market_ctx.get("side"),
            "entry": market_ctx.get("entry"),
            "sl": market_ctx.get("sl"),
            "tp": market_ctx.get("tp"),
            "regime": signal_payload.get("regime") or market_ctx.get("regime"),
            "setup_type": signal_payload.get("setup") or signal_payload.get("setup_type"),
            "setup_reason": signal_payload.get("setup_reason"),
            "source_exchange": market_ctx.get("source_exchange"),
            "mode": self.config.execution_mode.value,
            "decision": "ACCEPTED",
            "score": getattr(score_ctx, "total_score", None),
            "rr": signal_payload.get("risk_reward"),
            "candidate_rr": rr_metrics["candidate_rr"],
            "expected_fill": rr_metrics["expected_fill"],
            "expected_execution_cost_price": rr_metrics["expected_execution_cost_price"],
            "expected_execution_cost_pct": rr_metrics["expected_execution_cost_pct"],
            "expected_execution_cost_bps": rr_metrics["expected_execution_cost_bps"],
            "execution_cost_semantics": rr_metrics["execution_cost_semantics"],
            "executable_raw_rr": rr_metrics["executable_raw_rr"],
            "remaining_execution_penalty": rr_metrics["remaining_execution_penalty"],
            "effective_rr": effective_rr,
            "stop_distance_pct": market_ctx.get("stop_distance_pct"),
            "min_stop_pct": float(self.config.min_sl_pct),
            "max_stop_pct": float(self.config.max_sl_pct),
            "min_effective_rr": float(self.config.min_effective_rr),
            "confidence": order_plan.confidence,
            "execution_ctx": execution_ctx,
            "timeframe": self.config.execution_timeframe,
            "mtf": mtf,
            "base_exec_direction": market_ctx.get("base_exec_direction"),
            "resolved_state": market_ctx.get("resolved_state"),
            "final_direction": market_ctx.get("final_direction"),
            "override_reason": market_ctx.get("override_reason"),
            "mtf_execution_confirmation_mode": market_ctx.get("mtf_execution_confirmation_mode", "ENFORCE"),
            "shadow_mtf_execution_reason": market_ctx.get("shadow_mtf_execution_reason"),
            "authoritative_reject_reason": None,
            "enforce_counterfactual_reject_reason": market_ctx.get("enforce_counterfactual_reject_reason"),
            "geometry_status": market_ctx.get("geometry_status"),
            "geometry_reason": market_ctx.get("geometry_reason"),
            "geometry_source": market_ctx.get("geometry_source"),
            "entry_source": market_ctx.get("entry_source"),
            "stop_source": market_ctx.get("stop_source"),
            "target_source": market_ctx.get("target_source"),
            "setup_timeframe": market_ctx.get("setup_timeframe"),
            "execution_timeframe": market_ctx.get("execution_timeframe"),
            "structural_stop": market_ctx.get("structural_stop"),
            "structural_target": market_ctx.get("structural_target"),
            "portfolio_risk_state": portfolio_decision.risk_state,
            "portfolio_diagnostics": portfolio_decision.diagnostics,
            "risk_flags": portfolio_decision.risk_flags,
        }
        self._record_state_direction_shadow(
            {**accepted_burnin_payload, "side": market_ctx.get("side"),
             "entry": market_ctx.get("entry"), "sl": market_ctx.get("sl"),
             "tp": market_ctx.get("tp")}, actual_decision="ACCEPTED")
        if self.config.execution_mode == ExecutionMode.LIVE_PRECHECK:
            await self._persist_live_precheck_evidence(selection.symbol, signal_payload, market_ctx, regime_ctx, stats_ctx, score_ctx, order_plan, explanation, effective_rr)
            self._persist_burnin_decision(
                accepted_burnin_payload,
                lifecycle_state=LifecycleState.ORDER_PLACED.value,
            )
            self._generate_burnin_snapshot(reason="periodic")
            return

        self._schedule_agent_shadow({"signal_id": signal_id, "symbol": selection.symbol,
            "mode": self.config.execution_mode.value, "decision": "ACCEPTED",
            "score": getattr(score_ctx, "total_score", None), "rr": signal_payload.get("risk_reward"),
            "score_components": getattr(score_ctx, "components", None),
            "side": signal_payload.get("side", market_ctx.get("side")),
            "setup_type": signal_payload.get("setup", signal_payload.get("setup_type")),
            "entry": signal_payload.get("entry_price", market_ctx.get("entry")),
            "sl": signal_payload.get("stop_loss", market_ctx.get("sl")),
            "tp": signal_payload.get("take_profit", market_ctx.get("tp")),
            "regime": signal_payload.get("regime", market_ctx.get("regime")),
            "effective_rr": effective_rr, "confidence": order_plan.confidence,
            "execution_ctx": execution_ctx,
            "base_exec_direction": market_ctx.get("base_exec_direction"),
            "resolved_state": market_ctx.get("resolved_state"),
            "final_direction": market_ctx.get("final_direction"),
            "override_reason": market_ctx.get("override_reason")})
        executed = await self._execute(symbol=selection.symbol, decision={
            "signal_id": signal_id,
            "decision_time": accepted_burnin_payload["decision_time"],
            "order_type": order_plan.order_type,
            "limit_price": order_plan.limit_price,
            "stop_price": order_plan.stop_price,
            "confidence": order_plan.confidence,
        }, market_ctx=market_ctx)
        if executed is False:
            return
        if self.config.execution_mode == ExecutionMode.PAPER:
            self._persist_burnin_decision(
                accepted_burnin_payload,
                lifecycle_state=LifecycleState.POSITION_OPENED.value,
            )

    def _authoritative_live_authorization(self) -> dict[str, bool]:
        report = self._qualification_report
        qualification_passed = bool(
            report is not None
            and bool(getattr(report, "qualified", False))
            and str(getattr(report, "verdict", "")).upper() in {"LIVE_READY", "CANARY_QUALIFIED"}
        )
        reconciliation_passed = bool(
            self._reconciliation_status == "CLEAN"
            and not self._reconciliation_persistence_unhealthy
            and not self._unknown_exchange_state
            and not self._unreconciled_symbols
            and not self._orphan_orders
            and not self._orphan_positions
            and not self._recovery_required
        )
        return {
            "live_trading_enabled": bool(self.config.live_trading_enabled),
            "operator_acknowledged": bool(self.config.operator_live_acknowledged),
            "qualification_passed": qualification_passed,
            "reconciliation_passed": reconciliation_passed,
            # This method re-reads RuntimeControlStore on every invocation.
            "kill_switch_active": bool(self._kill_switch_active()),
        }

    def _build_live_order_execution_context(self, symbol: str, market_ctx: Mapping[str, Any]) -> OrderExecutionContext:
        snapshot = self._authoritative_live_authorization()
        return OrderExecutionContext(
            mode=TradingMode.LIVE,
            timestamp=int(time.time() * 1000),
            symbol=symbol,
            balance=0.0,
            risk_pct=0.0,
            allow_live_orders=bool(self.config.allow_live_orders),
            market_ctx=dict(market_ctx),
            storage={
                "live_authorization": snapshot,
                # The final validator calls this bound authoritative provider;
                # it never trusts the potentially stale snapshot above.
                "live_authorization_provider": self._authoritative_live_authorization,
            },
        )

    def _canonical_execution_result(
        self,
        result: Mapping[str, Any],
        decision: Mapping[str, Any],
        market_ctx: Mapping[str, Any],
        *,
        mode: ExecutionMode,
    ) -> dict[str, Any]:
        """Attach canonical entry -> expected_fill -> actual_fill evidence.

        This is evidence-only normalization after an execution result exists.
        It must not change authorization, order selection, threshold decisions,
        or whether an order is submitted. Invalid/missing fill evidence remains
        explicit UNAVAILABLE rather than failing an already-completed submit.
        """
        normalized = dict(result)
        if mode not in {ExecutionMode.PAPER, ExecutionMode.LIVE}:
            return normalized

        execution_ctx = dict(market_ctx.get("execution_ctx") or {})
        expected_fill = normalized.get("expected_fill", market_ctx.get("expected_fill"))
        if expected_fill is None:
            expected_fill, _ = self._expected_fill_price(market_ctx, execution_ctx)

        actual_fill = normalized.get("actual_fill")
        fills = normalized.get("fills")
        if fills is not None:
            try:
                weighted_fill = weighted_average_fill_price(fills)
            except ValueError:
                normalized["execution_cost_semantics"] = None
                normalized["execution_cost_semantics_status"] = "UNAVAILABLE_INVALID_FILL_LEDGER"
                normalized["actual_fill_provenance"] = PROVENANCE_UNAVAILABLE
                return normalized
            if weighted_fill is not None:
                actual_fill = weighted_fill
                normalized["weighted_average_fill_price"] = weighted_fill
        if actual_fill is None:
            actual_fill = normalized.get("fill_price")

        expected_provenance = (
            PROVENANCE_MODELLED if mode is ExecutionMode.PAPER else PROVENANCE_ESTIMATED
        )
        actual_provenance = (
            PROVENANCE_MODELLED
            if mode is ExecutionMode.PAPER and actual_fill is not None
            else PROVENANCE_ACTUAL
            if mode is ExecutionMode.LIVE and actual_fill is not None
            else PROVENANCE_UNAVAILABLE
        )

        if expected_fill is None:
            normalized["execution_cost_semantics"] = None
            normalized["execution_cost_semantics_status"] = "UNAVAILABLE_EXPECTED_FILL"
            normalized["actual_fill_provenance"] = actual_provenance
            return normalized

        try:
            semantics = build_execution_cost_semantics(
                entry=market_ctx.get("entry"),
                expected_fill=expected_fill,
                actual_fill=actual_fill,
                side=market_ctx.get("side"),
                expected_fill_provenance=expected_provenance,
                actual_fill_provenance=actual_provenance,
                decision_timestamp=decision.get("decision_time"),
                fill_timestamp=(
                    normalized.get("fill_timestamp")
                    or normalized.get("filled_at")
                    if actual_fill is not None
                    else None
                ),
            )
        except ValueError:
            normalized["execution_cost_semantics"] = None
            normalized["execution_cost_semantics_status"] = "UNAVAILABLE_INVALID_PRICE_OR_SIDE"
            normalized["actual_fill_provenance"] = actual_provenance
            return normalized

        normalized["expected_fill"] = semantics.expected_fill
        normalized["actual_fill"] = semantics.actual_fill
        normalized["actual_fill_provenance"] = semantics.actual_fill_provenance
        normalized["execution_cost_semantics"] = semantics.as_dict()
        normalized["execution_cost_semantics_status"] = "AVAILABLE"
        return normalized

    async def _execute(self, symbol: str, decision: dict[str, Any], market_ctx: Mapping[str, Any]) -> bool | None:
        self._assert_campaign_candidate(symbol, market_ctx.get("source_exchange"), "PAPER_EXECUTION")
        if self._kill_switch_active():
            raise RuntimeError("KILL_SWITCH_ACTIVE")
        mode = self.config.execution_mode
        if mode == ExecutionMode.PAPER:
            # The earlier decision gate can become stale across scoring and
            # lifecycle awaits. No await occurs between this check and fill.
            if self._execution_reconciliation_blocked():
                reason = self._fail_closed_reason or "EXCHANGE_STATE_UNKNOWN"
                await self._emit_lifecycle_event(LifecycleState.CANCELLED.value, symbol,
                    {"reason": reason, "signal_id": decision.get("signal_id"), "execution_attempted": False})
                return False
            result = self._simulate_paper_execution(symbol, decision, market_ctx)
        elif mode == ExecutionMode.LIVE_PRECHECK:
            result = {"mode": mode.value, "status": "no_submit_verified", "symbol": symbol}
        elif mode == ExecutionMode.LIVE:
            if self.real_execution_adapter is None:
                raise RuntimeError("LIVE mode requires real_execution_adapter")
            authorization_ctx = self._build_live_order_execution_context(symbol, market_ctx)
            validate_live_order_authorization(authorization_ctx)
            result = await self.real_execution_adapter.submit(decision, market_ctx)
        else:
            result = {"mode": mode.value, "status": "simulated", "symbol": symbol}

        result = self._canonical_execution_result(result, decision, market_ctx, mode=mode)
        self.metrics.executions += 1
        order_id = str(result.get("order_id") or f"{symbol}:{canonical_utc_timestamp()}")
        result_status = str(result.get("status", "")).lower()
        campaign_attached = bool(self._campaign_id or os.getenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID"))
        paper_notional = None
        if mode == ExecutionMode.PAPER and campaign_attached and result_status not in {"rejected", "exchange_reject", "timeout", "error", "missing_ack"}:
            paper_notional = self._persist_pending_paper_position(symbol, order_id, decision, market_ctx, result)
        self._pending_orders[symbol] = {"order_id": order_id, "symbol": symbol, "status": result.get("status", "UNKNOWN"), "created_at": canonical_utc_timestamp()}
        await self._emit_lifecycle_event(LifecycleState.ORDER_PLACED.value, symbol, {"decision": decision, "result": dict(result)})
        if result_status == "no_submit_verified":
            return
        if result_status in {"rejected", "exchange_reject"}:
            await self._emit_lifecycle_event(LifecycleState.ORDER_REJECTED.value, symbol, {"reason": "exchange_rejected_order", "result": dict(result)})
            return
        elif result_status in {"timeout", "error", "missing_ack"}:
            await self._record_incident(symbol, LifecycleState.ENTRY_TIMEOUT.value, "execution_uncertain_state")
            await self._reconcile_symbol_state(symbol, result, market_ctx)
            return
        await self._emit_lifecycle_event(
            LifecycleState.POSITION_OPENED.value,
            symbol,
            {
                "result": dict(result),
                **({"fill_state": "partial"} if result_status == "partial_fill" else {}),
            },
        )
        self._generate_burnin_snapshot(reason="periodic")
        self._active_positions[symbol] = float(paper_notional or market_ctx.get("notional") or market_ctx.get("notional_usdt") or market_ctx.get("order_notional") or 0.0)
        self._active_position_sides[symbol] = str(market_ctx.get("side") or "UNKNOWN").upper()
        self._symbol_cooldown_until[symbol] = time.time() + self.config.symbol_cooldown_sec

    def _persist_pending_paper_position(self, symbol: str, trade_id: str, decision: Mapping[str, Any], market_ctx: Mapping[str, Any], result: Mapping[str, Any]) -> float:
        campaign_id = self._campaign_id or os.getenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID")
        engine = self._resolve_persistence_engine()
        if not campaign_id or not self._burnin_run_id or engine is None:
            raise RuntimeError("PAPER_POSITION_EVIDENCE_IDENTITY_UNAVAILABLE")
        execution_ctx = dict(market_ctx.get("execution_ctx") or build_execution_context(market_ctx))
        model = build_execution_cost_model(execution_ctx, include_missing_penalty=False)
        mtf = market_ctx.get("mtf") if isinstance(market_ctx.get("mtf"), Mapping) else {}
        setup = mtf.get("setup") if isinstance(mtf.get("setup"), Mapping) else {}
        execution = mtf.get("execution") if isinstance(mtf.get("execution"), Mapping) else {}
        regime = mtf.get("regime") if isinstance(mtf.get("regime"), Mapping) else {}
        actual_fill = result.get("actual_fill", result.get("fill_price"))
        expected_fill = result.get("expected_fill", market_ctx.get("expected_fill"))
        if expected_fill is None:
            expected_fill, _ = self._expected_fill_price(market_ctx, execution_ctx)
        if actual_fill is None or expected_fill is None:
            raise RuntimeError("PAPER_FILL_EVIDENCE_UNAVAILABLE")
        fill = float(actual_fill)
        planned_entry = float(market_ctx.get("entry"))
        requested_notional = market_ctx.get("notional") or market_ctx.get("notional_usdt") or market_ctx.get("order_notional")
        if requested_notional is None:
            requested_quantity = market_ctx.get("quantity") or market_ctx.get("qty")
            if requested_quantity is None:
                requested_notional = self.config.paper_candidate_notional
                if requested_notional is None:
                    raise RuntimeError("PAPER_POSITION_SIZE_UNAVAILABLE")
            else:
                requested_notional = fill * float(requested_quantity)
        notional = float(requested_notional)
        if not math.isfinite(fill) or fill <= 0 or not math.isfinite(notional) or notional <= 0:
            raise RuntimeError("PAPER_POSITION_SIZE_INVALID")
        quantity = notional / fill
        if str(result.get("status") or "").lower() == "partial_fill":
            fills = result.get("fills")
            if not isinstance(fills, list) or not fills:
                raise RuntimeError("PAPER_PARTIAL_FILL_QUANTITY_UNAVAILABLE")
            try:
                filled_quantity = sum(float(row["qty"]) for row in fills)
            except (KeyError, TypeError, ValueError):
                raise RuntimeError("PAPER_PARTIAL_FILL_QUANTITY_UNAVAILABLE") from None
            if not math.isfinite(filled_quantity) or filled_quantity <= 0 or filled_quantity > quantity:
                raise RuntimeError("PAPER_PARTIAL_FILL_QUANTITY_INVALID")
            quantity = filled_quantity
            notional = fill * quantity
        stop = float(market_ctx.get("sl"))
        risk_usd = abs(fill - stop) * quantity
        if not math.isfinite(risk_usd) or risk_usd <= 0:
            raise RuntimeError("PAPER_POSITION_RISK_INVALID")
        fill_timestamp = str(result.get("fill_timestamp") or canonical_utc_timestamp())
        cost_semantics = build_execution_cost_semantics(
            entry=planned_entry,
            expected_fill=expected_fill,
            actual_fill=fill,
            side=market_ctx.get("side"),
            expected_fill_provenance=PROVENANCE_MODELLED,
            actual_fill_provenance=PROVENANCE_MODELLED,
            decision_timestamp=decision.get("decision_time"),
            fill_timestamp=fill_timestamp,
        )
        provenance = {
            "provider": self.scanner_source or "UNKNOWN",
            "source_exchange": market_ctx.get("source_exchange"),
            "execution_timeframe": self.config.execution_timeframe,
            "execution_cost_unit": "USD",
            "execution_cost_model_unit": "R",
            "execution_cost_model": dict(model.__dict__),
            "entry_slippage_embedded_in_fill": True,
            "entry_slippage_additional_cost": 0.0,
            "fill_slippage_pct": market_ctx.get("fill_slippage_pct"),
            "candidate_rr": market_ctx.get("candidate_rr", market_ctx.get("rr")),
            "expected_fill": expected_fill,
            "actual_fill": fill,
            "actual_fill_provenance": PROVENANCE_MODELLED,
            "fill_timestamp": fill_timestamp,
            "fill_state": "PARTIAL" if str(result.get("status") or "").lower() == "partial_fill" else "FILLED",
            "filled_quantity": quantity,
            "execution_cost_semantics": cost_semantics.as_dict(),
            "executable_raw_rr": market_ctx.get("executable_raw_rr"),
            "remaining_execution_penalty": market_ctx.get("remaining_execution_penalty"),
            "effective_rr_at_entry": market_ctx.get(
                "effective_rr",
                self._execution_rr_metrics(market_ctx.get("rr"), market_ctx, execution_ctx)["effective_rr"],
            ),
            "setup_phase": setup.get("phase"),
            "execution_direction": execution.get("direction"),
            "mtf": mtf,
        }
        with engine.begin() as conn:
            decision_rows = conn.execute(text("""
                SELECT decision_id FROM order_decisions
                WHERE signal_id=:signal_id AND decision='ACCEPTED'
                  AND mode='PAPER' AND phase='ai_internal_real'
            """), {"signal_id": decision.get("signal_id")}).fetchall()
            decision_ids = {str(row[0]) for row in decision_rows if row[0]}
            source_decision_id = next(iter(decision_ids)) if len(decision_ids) == 1 else None
            persist_pending_position(
                conn, trade_id=trade_id, campaign_id=campaign_id, burnin_run_id=self._burnin_run_id,
                signal_id=decision.get("signal_id"), source_decision_id=source_decision_id,
                decision_time=decision.get("decision_time"), symbol=symbol, side=market_ctx.get("side"),
                setup_type=market_ctx.get("setup") or market_ctx.get("setup_type") or setup.get("phase"),
                entry_time=fill_timestamp, planned_entry=planned_entry, simulated_fill=fill,
                stop=market_ctx.get("sl"), target=market_ctx.get("tp"), quantity=quantity,
                notional=notional, entry_spread=model.spread_penalty * risk_usd / 2.0,
                entry_slippage=0.0, entry_fee=model.fee_penalty * risk_usd / 2.0,
                regime=regime.get("regime") or market_ctx.get("regime") or "UNKNOWN",
                source_provenance=provenance,
            )
        return notional

    @staticmethod
    def _portfolio_risk_dt(value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        try:
            if isinstance(value, (int, float)):
                raw = float(value)
                seconds = raw / 1000.0 if raw > 10_000_000_000 else raw
                return datetime.fromtimestamp(seconds, tz=timezone.utc)
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError, OSError):
            return None

    def _paper_portfolio_risk_state(
        self,
        symbol: str,
        *,
        now_ts: float,
    ) -> dict[str, Any]:
        """Reconstruct PAPER risk history from canonical campaign evidence.

        Accepted trade counts come from burnin_pending_position_outcomes so an
        open position counts immediately. Realized PnL, equity, drawdown and
        loss streaks come only from evidence-complete burnin_trade_outcomes.
        Continuation runs in the same campaign are intentionally included;
        unrelated campaigns/runs are excluded.
        """
        initial_equity = self.config.paper_initial_equity
        missing: list[str] = []
        try:
            initial = float(initial_equity) if initial_equity is not None else None
        except (TypeError, ValueError):
            initial = None
        if initial is None or not math.isfinite(initial) or initial <= 0:
            missing.append("paper_initial_equity")

        campaign_id = self._campaign_id or os.getenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID")
        if not campaign_id:
            return {
                "equity": initial,
                "available_balance": initial,
                "daily_realized_pnl": 0.0 if not missing else None,
                "rolling_peak_equity": initial if not missing else None,
                "rolling_drawdown_pct": 0.0 if not missing else None,
                "consecutive_loss_count": 0 if not missing else None,
                "symbol_consecutive_loss_count": 0 if not missing else None,
                "trades_today_symbol": 0 if not missing else None,
                "trades_today_global": 0 if not missing else None,
                "persisted_cooldown_until": None,
                "risk_state_complete": not missing,
                "risk_state_source": "RUNTIME_SESSION_UNSCOPED",
                "risk_state_missing_fields": missing,
            }

        engine = self._resolve_persistence_engine()
        if engine is None:
            missing.append("persistence_engine")
            return {
                "equity": None,
                "available_balance": None,
                "daily_realized_pnl": None,
                "rolling_peak_equity": None,
                "rolling_drawdown_pct": None,
                "consecutive_loss_count": None,
                "symbol_consecutive_loss_count": None,
                "trades_today_symbol": None,
                "trades_today_global": None,
                "persisted_cooldown_until": None,
                "risk_state_complete": False,
                "risk_state_source": "BURNIN_CAMPAIGN_EVIDENCE",
                "risk_state_missing_fields": missing,
            }

        try:
            with engine.connect() as conn:
                accepted_rows = conn.execute(text("""
                    SELECT p.trade_id,p.burnin_run_id,p.symbol,p.entry_time,p.status,
                           p.evidence_complete AS pending_evidence_complete,
                           cr.campaign_id AS lineage_campaign_id
                    FROM burnin_pending_position_outcomes p
                    LEFT JOIN burnin_campaign_runs cr
                      ON cr.campaign_id=p.campaign_id
                     AND cr.burnin_run_id=p.burnin_run_id
                    WHERE p.campaign_id=:campaign_id
                    ORDER BY p.entry_time,p.id
                """), {"campaign_id": campaign_id}).mappings().all()
                outcome_rows = conn.execute(text("""
                    SELECT t.trade_id,t.burnin_run_id,t.symbol,t.closed_at,t.net_pnl,
                           t.exit_reason,t.evidence_complete
                    FROM burnin_trade_outcomes t
                    JOIN burnin_campaign_runs cr
                      ON cr.burnin_run_id=t.burnin_run_id
                    WHERE cr.campaign_id=:campaign_id
                    ORDER BY t.closed_at,t.id
                """), {"campaign_id": campaign_id}).mappings().all()
        except Exception as exc:
            logger.error(
                "paper_portfolio_risk_state_load_failed campaign_id=%s error=%s",
                campaign_id,
                exc,
            )
            return {
                "equity": None,
                "available_balance": None,
                "daily_realized_pnl": None,
                "rolling_peak_equity": None,
                "rolling_drawdown_pct": None,
                "consecutive_loss_count": None,
                "symbol_consecutive_loss_count": None,
                "trades_today_symbol": None,
                "trades_today_global": None,
                "persisted_cooldown_until": None,
                "risk_state_complete": False,
                "risk_state_source": "BURNIN_CAMPAIGN_EVIDENCE",
                "risk_state_missing_fields": ["portfolio_risk_state_query_failed"],
            }

        now_dt = datetime.fromtimestamp(float(now_ts), tz=timezone.utc)
        today = now_dt.date()
        symbol_u = str(symbol or "").upper()
        accepted_ids: set[str] = set()
        accepted_status_by_trade: dict[str, str] = {}
        accepted_symbol_by_trade: dict[str, str] = {}
        accepted_run_by_trade: dict[str, str] = {}
        accepted_entry_by_trade: dict[str, datetime | None] = {}
        accepted_complete_by_trade: dict[str, bool] = {}
        closed_pending_ids: set[str] = set()
        trades_today_global = 0
        trades_today_symbol = 0
        latest_symbol_entry: datetime | None = None

        for row in accepted_rows:
            trade_id = str(row.get("trade_id") or "")
            row_symbol = str(row.get("symbol") or "").upper()
            entry_dt = self._portfolio_risk_dt(row.get("entry_time"))
            status = str(row.get("status") or "").upper()
            if not trade_id:
                missing.append("accepted_trade_id")
                continue
            if trade_id in accepted_ids:
                missing.append(f"duplicate_accepted_trade:{trade_id}")
                continue
            accepted_ids.add(trade_id)
            burnin_run_id = str(row.get("burnin_run_id") or "")
            lineage_campaign_id = str(row.get("lineage_campaign_id") or "")
            accepted_status_by_trade[trade_id] = status
            accepted_symbol_by_trade[trade_id] = row_symbol
            accepted_run_by_trade[trade_id] = burnin_run_id
            accepted_entry_by_trade[trade_id] = entry_dt
            accepted_complete_by_trade[trade_id] = (
                int(row.get("pending_evidence_complete") or 0) == 1
            )
            if lineage_campaign_id != str(campaign_id):
                missing.append(f"accepted_trade_lineage:{trade_id}")
            if not row_symbol:
                missing.append(f"accepted_trade_symbol:{trade_id}")
            if entry_dt is None or entry_dt > now_dt:
                missing.append(f"accepted_trade_entry_time:{trade_id}")
            else:
                if entry_dt.date() == today:
                    trades_today_global += 1
                    if row_symbol == symbol_u:
                        trades_today_symbol += 1
                if row_symbol == symbol_u and (
                    latest_symbol_entry is None or entry_dt > latest_symbol_entry
                ):
                    latest_symbol_entry = entry_dt
            if status == "CLOSED":
                closed_pending_ids.add(trade_id)
            elif status != "OPEN":
                missing.append(f"accepted_trade_status:{trade_id}:{status or 'UNKNOWN'}")

        if initial is None:
            current_equity = None
            peak_equity = None
        else:
            current_equity = initial
            peak_equity = initial

        daily_realized_pnl = 0.0
        consecutive_losses = 0
        symbol_consecutive_losses = 0
        outcome_ids: set[str] = set()

        for row in outcome_rows:
            trade_id = str(row.get("trade_id") or "")
            row_symbol = str(row.get("symbol") or "").upper()
            closed_dt = self._portfolio_risk_dt(row.get("closed_at"))
            evidence_complete = int(row.get("evidence_complete") or 0) == 1
            try:
                net_pnl = float(row.get("net_pnl"))
            except (TypeError, ValueError):
                net_pnl = None
            accepted_entry = accepted_entry_by_trade.get(trade_id)
            accepted_symbol = accepted_symbol_by_trade.get(trade_id)
            accepted_run = accepted_run_by_trade.get(trade_id)
            outcome_run = str(row.get("burnin_run_id") or "")
            if (
                not trade_id
                or trade_id not in accepted_ids
                or trade_id in outcome_ids
                or accepted_status_by_trade.get(trade_id) != "CLOSED"
                or not accepted_complete_by_trade.get(trade_id, False)
                or accepted_symbol != row_symbol
                or accepted_run != outcome_run
                or not evidence_complete
                or closed_dt is None
                or accepted_entry is None
                or closed_dt < accepted_entry
                or closed_dt > now_dt
                or net_pnl is None
                or not math.isfinite(net_pnl)
            ):
                missing.append(f"realized_trade_outcome:{trade_id or 'UNKNOWN'}")
                continue
            outcome_ids.add(trade_id)
            if current_equity is not None and peak_equity is not None:
                current_equity += net_pnl
                peak_equity = max(peak_equity, current_equity)
            if closed_dt.date() == today:
                daily_realized_pnl += net_pnl

            exit_reason = str(row.get("exit_reason") or "").upper()
            is_loss = net_pnl < 0 or exit_reason == "SL_HIT"
            is_win = net_pnl > 0 or exit_reason == "TP_HIT"
            if is_loss:
                consecutive_losses += 1
            elif is_win:
                consecutive_losses = 0
            if row_symbol == symbol_u:
                if is_loss:
                    symbol_consecutive_losses += 1
                elif is_win:
                    symbol_consecutive_losses = 0

        for trade_id in sorted(closed_pending_ids - outcome_ids):
            missing.append(f"closed_trade_outcome_missing:{trade_id}")

        rolling_drawdown_pct = None
        if current_equity is not None and peak_equity is not None and peak_equity > 0:
            rolling_drawdown_pct = max(0.0, (peak_equity - current_equity) / peak_equity)

        cooldown_until = None
        if latest_symbol_entry is not None:
            cooldown_until = latest_symbol_entry.timestamp() + float(self.config.symbol_cooldown_sec)

        complete = not missing
        return {
            "equity": current_equity if complete else None,
            "available_balance": current_equity if complete else None,
            "daily_realized_pnl": daily_realized_pnl if complete else None,
            "rolling_peak_equity": peak_equity if complete else None,
            "rolling_drawdown_pct": rolling_drawdown_pct if complete else None,
            "consecutive_loss_count": consecutive_losses if complete else None,
            "symbol_consecutive_loss_count": (
                symbol_consecutive_losses if complete else None
            ),
            "trades_today_symbol": trades_today_symbol if complete else None,
            "trades_today_global": trades_today_global if complete else None,
            "persisted_cooldown_until": cooldown_until,
            "risk_state_complete": complete,
            "risk_state_source": "BURNIN_CAMPAIGN_EVIDENCE",
            "risk_state_missing_fields": sorted(set(missing)),
        }

    def _sync_resolved_paper_positions(self) -> None:
        if self.config.execution_mode != ExecutionMode.PAPER or not self._campaign_id:
            return
        engine = self._resolve_persistence_engine()
        if engine is None:
            return
        with engine.connect() as conn:
            open_symbols = {str(row[0]).upper() for row in conn.execute(text("SELECT symbol FROM burnin_pending_position_outcomes WHERE campaign_id=:cid AND status='OPEN'"), {"cid": self._campaign_id}).fetchall()}
        for symbol in set(self._active_positions) - open_symbols:
            self._active_positions.pop(symbol, None)
            self._active_position_sides.pop(symbol, None)
            self._pending_orders.pop(symbol, None)
            self._symbol_cooldown_until.pop(symbol, None)
            self._last_lifecycle_state_by_symbol[symbol] = LifecycleState.POSITION_CLOSED.value

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
                execution_ctx_missing=execution_context_is_unavailable(execution_ctx),
                expected_slippage_pct=execution_ctx.get("expected_slippage_pct"),
                spread_pct=execution_ctx.get("spread_pct"),
                latency_ms=execution_ctx.get("latency_ms"),
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
        execution_ctx = dict(market_ctx.get("execution_ctx") or {})
        fill, slip = self._expected_fill_price(market_ctx, execution_ctx)
        if fill is None or slip is None:
            raise RuntimeError("PAPER_EXECUTABLE_FILL_UNAVAILABLE")
        fill_timestamp = canonical_utc_timestamp()
        try:
            cost_semantics = build_execution_cost_semantics(
                entry=market_ctx.get("entry"),
                expected_fill=fill,
                actual_fill=fill,
                side=market_ctx.get("side"),
                expected_fill_provenance=PROVENANCE_MODELLED,
                actual_fill_provenance=PROVENANCE_MODELLED,
                decision_timestamp=decision.get("decision_time"),
                fill_timestamp=fill_timestamp,
            ).as_dict()
        except ValueError:
            # Preserve non-campaign compatibility for legacy callers without a
            # side; canonical side-normalized evidence remains unavailable.
            cost_semantics = None
        return {
            "mode": ExecutionMode.PAPER.value,
            "symbol": symbol,
            "status": "filled",
            "order_type": decision.get("order_type", "MARKET"),
            "expected_slippage_pct": slip,
            "expected_fill": fill,
            "actual_fill": fill,
            "fill_price": fill,
            "fill_provenance": PROVENANCE_MODELLED,
            "fill_timestamp": fill_timestamp,
            "execution_cost_semantics": cost_semantics,
        }

    async def _persist_reject(self, payload: dict[str, Any]) -> None:
        self._assert_campaign_candidate(str(payload.get("symbol") or ""),
                                        payload.get("source_exchange"), "REJECT_PERSISTENCE")
        if (self.config.execution_mode in {ExecutionMode.PAPER, ExecutionMode.LIVE_PRECHECK}
                and not self._burnin_run_id):
            self._start_or_resume_burnin_run()
        payload = self._canonical_reject_payload(payload)
        self._reject_log.append(payload)
        engine = self._resolve_persistence_engine()
        canonical_persisted_count: int | None = None
        if engine is not None:
            with engine.begin() as conn:
                if not record_rejected_signal_review(conn, reject_decision_id=payload["reject_decision_id"], signal_id=payload["signal_id"], symbol=payload.get("symbol"), setup_type=payload.get("setup_type"), regime=payload.get("regime"), side=payload.get("side"), reject_reason=payload.get("reason"), score=payload.get("score"), raw_rr=payload.get("rr"), effective_rr=payload.get("effective_rr"), volume_24h_usdt=payload.get("volume_24h_usdt"), spread_pct=payload.get("spread_pct"), expected_slippage_pct=payload.get("expected_slippage_pct"), funding_rate_pct=payload.get("funding_rate_pct"), liquidity_score=payload.get("liquidity_score"), volatility_regime=payload.get("volatility_regime"), payload_json=payload):
                    raise RuntimeError("rejected_signal_review_persistence_failed")
                self._persist_burnin_decision(
                    {**payload, "decision": "REJECTED"},
                    lifecycle_state=LifecycleState.SIGNAL_REJECTED.value, conn=conn)
                self._persist_pending_reject(payload, conn=conn)
                if self._burnin_run_id:
                    canonical_persisted_count = self._canonical_persisted_reject_count(conn)
        else:
            self._persist_burnin_decision(
                {**payload, "decision": "REJECTED"},
                lifecycle_state=LifecycleState.SIGNAL_REJECTED.value)
        if engine is not None:
            self._persisted_reject_decision_ids.add(str(payload["reject_decision_id"]))
            self.metrics.rejects_persisted = (
                canonical_persisted_count
                if canonical_persisted_count is not None
                else len(self._persisted_reject_decision_ids)
            )
        if self.on_reject_persist is not None:
            maybe_coro = self.on_reject_persist(payload)
            if asyncio.iscoroutine(maybe_coro):
                await maybe_coro
        self._record_state_direction_shadow(payload, actual_decision="REJECTED")
        self._schedule_agent_shadow(payload)

    def _canonical_persisted_reject_count(self, conn: Any, *, campaign_scope: bool = True) -> int:
        """Count canonical durable rejects in the runtime's evidence scope."""
        if not self._burnin_run_id:
            return len(self._persisted_reject_decision_ids)
        campaign_id = self._campaign_id or os.getenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID")
        params: dict[str, Any]
        if campaign_scope and campaign_id:
            scope = ("o.burnin_run_id IN (SELECT burnin_run_id FROM burnin_campaign_runs "
                     "WHERE campaign_id=:campaign_id)")
            params = {"campaign_id": campaign_id}
        else:
            scope = "o.burnin_run_id=:burnin_run_id"
            params = {"burnin_run_id": self._burnin_run_id}
        return int(conn.execute(text(f"""
            SELECT COUNT(*)
            FROM burnin_observations o
            WHERE {scope}
              AND UPPER(COALESCE(o.decision, ''))='REJECTED'
              AND {canonical_decision_sql('o')}
        """), params).scalar_one() or 0)

    def _restore_rejects_persisted(self, *, conn: Any | None = None) -> int:
        """Restore the heartbeat counter from canonical DB evidence after attach/restart."""
        if not self._burnin_run_id:
            self.metrics.rejects_persisted = len(self._persisted_reject_decision_ids)
            return self.metrics.rejects_persisted
        if conn is not None:
            count = self._canonical_persisted_reject_count(conn)
        else:
            engine = self._resolve_persistence_engine()
            if engine is None:
                return self.metrics.rejects_persisted
            with engine.connect() as owned_conn:
                count = self._canonical_persisted_reject_count(owned_conn)
        self.metrics.rejects_persisted = count
        return count

    def _reject_gate_audit(self, payload: Mapping[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
        """Return non-authoritative multi-gate evidence for a final reject.

        This audit never changes the canonical primary reject reason or decision.
        It snapshots decision-time observed values against the runtime thresholds
        so downstream analysis can distinguish overlapping failures.
        """
        execution = dict(payload.get("execution_ctx") or {})
        failed: list[str] = []
        evidence: list[dict[str, Any]] = []

        def number(value: Any) -> float | None:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return None
            return parsed if math.isfinite(parsed) else None

        def add(gate: str, observed: Any, threshold: Any, comparison: str, source: str = "RUNTIME_THRESHOLD") -> None:
            gate = str(gate).upper()
            if gate not in failed:
                failed.append(gate)
            evidence.append({
                "gate": gate,
                "observed": observed,
                "threshold": threshold,
                "comparison": comparison,
                "source": source,
            })

        execution_safety = payload.get("execution_safety")
        if isinstance(execution_safety, Mapping):
            safety_evidence = execution_safety.get("failed_gate_evidence")
            if isinstance(safety_evidence, (list, tuple)):
                for row in safety_evidence:
                    if not isinstance(row, Mapping) or not row.get("gate"):
                        continue
                    add(
                        str(row.get("gate")),
                        row.get("observed"),
                        row.get("threshold"),
                        str(row.get("comparison") or "SAFETY_CONTRACT"),
                        str(row.get("source") or "EXECUTION_SAFETY_CONTRACT"),
                    )

        supplied = payload.get("all_failed_gates")
        if isinstance(supplied, (list, tuple)):
            for gate in supplied:
                if gate:
                    add(str(gate), None, None, "SUPPLIED", "UPSTREAM_DIAGNOSTIC")

        primary = canonical_reject_reason(
            payload.get("primary_reject_reason") or payload.get("reason") or payload.get("reject_reason")
        )
        if primary and primary != "UNKNOWN":
            add(primary, None, None, "PRIMARY", "AUTHORITATIVE_PRIMARY")

        market_time_evidence = payload.get("market_time_evidence")
        if isinstance(market_time_evidence, Mapping):
            market_time_reason = str(market_time_evidence.get("reason") or "").upper()
            if market_time_reason in {
                "INVALID_MARKET_TIMESTAMP",
                "MARKET_TIMESTAMP_UNIT_MISMATCH",
                "MARKET_TIMESTAMP_IN_FUTURE",
                "STALE_MARKET_DATA",
            }:
                add(
                    market_time_reason,
                    market_time_evidence.get("observed"),
                    market_time_evidence.get("reject_threshold"),
                    str(market_time_evidence.get("comparison") or "MARKET_TIME_CONTRACT"),
                    "MARKET_TIME_CONTRACT",
                )

        score = number(payload.get("score"))
        if score is not None and score < float(self.config.min_signal_score):
            add("LOW_SCORE", score, float(self.config.min_signal_score), "<")

        raw_rr = number(payload.get("candidate_rr", payload.get("rr", payload.get("raw_rr"))))
        if raw_rr is not None and raw_rr < float(self.config.min_rr):
            add("RR_TOO_LOW", raw_rr, float(self.config.min_rr), "<")

        effective_rr = number(payload.get("effective_rr"))
        if effective_rr is not None and effective_rr < float(self.config.min_effective_rr):
            add("LOW_EFFECTIVE_RR", effective_rr, float(self.config.min_effective_rr), "<")

        entry = number(payload.get("entry", payload.get("entry_price")))
        stop = number(payload.get("sl", payload.get("stop_loss", payload.get("stop"))))
        if entry is not None and entry > 0 and stop is not None:
            stop_distance_pct = abs(entry - stop) / entry * 100.0
            if stop_distance_pct < float(self.config.min_sl_pct):
                add("STOP_TOO_TIGHT", stop_distance_pct, float(self.config.min_sl_pct), "<")
            if stop_distance_pct > float(self.config.max_sl_pct):
                add("STOP_TOO_WIDE", stop_distance_pct, float(self.config.max_sl_pct), ">")

        spread = number(payload.get("spread_pct", execution.get("spread_pct")))
        if spread is not None and spread > float(self.config.max_spread_pct):
            add("SPREAD_TOO_HIGH", spread, float(self.config.max_spread_pct), ">")

        slippage = number(payload.get("expected_slippage_pct", execution.get("expected_slippage_pct")))
        if slippage is not None and slippage > float(self.config.max_expected_slippage_pct):
            add("SLIPPAGE_TOO_HIGH", slippage, float(self.config.max_expected_slippage_pct), ">")

        funding = number(payload.get("funding_rate_pct", execution.get("funding_rate_pct")))
        if funding is not None and abs(funding) > float(self.config.max_abs_funding_rate_pct):
            add("FUNDING_TOO_HIGH", abs(funding), float(self.config.max_abs_funding_rate_pct), ">")

        volume = number(payload.get("volume_24h_usdt"))
        if volume is not None and volume < float(self.config.min_liquidity_usd):
            add("THIN_LIQUIDITY", volume, float(self.config.min_liquidity_usd), "<")

        return failed, evidence

    def _canonical_reject_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        result=dict(payload); execution=dict(result.get("execution_ctx") or {}); signal_id=str(result.get("signal_id") or "")
        supplied_reasons = result.get("reject_reasons")
        reject_reasons = [canonical_reject_reason(value) for value in supplied_reasons
                          if value] if isinstance(supplied_reasons, (list, tuple)) else []
        primary_reject_reason = canonical_reject_reason(
            result.get("primary_reject_reason") or result.get("reason")
            or result.get("reject_reason") or (reject_reasons[0] if reject_reasons else None)
        )
        reject_reasons = list(dict.fromkeys([primary_reject_reason, *reject_reasons]))
        mtf = result.get("mtf") if isinstance(result.get("mtf"), Mapping) else {}
        regime_layer = mtf.get("regime") if isinstance(mtf.get("regime"), Mapping) else {}
        mtf_regime = regime_layer.get("regime")
        if mtf_regime and str(regime_layer.get("evidence_status") or "").upper() == "COMPLETE":
            source_regime = result.get("regime")
            if (source_regime is not None
                    and str(source_regime).upper() != str(mtf_regime).upper()):
                result["legacy_decision_regime"] = source_regime
            result["regime"] = str(mtf_regime).upper()
        generation = mtf.get("generation") if isinstance(mtf.get("generation"), Mapping) else {}
        guided_generation = generation.get("mode") == "REGIME_GUIDED"
        guided_candidate = (guided_generation and generation.get("evidence_status") == "COMPLETE"
                            and isinstance(generation.get("candidate"), Mapping))
        guided_without_candidate = guided_generation and not isinstance(generation.get("candidate"), Mapping)
        forward_label_subject = ("GUIDED_CANDIDATE" if guided_candidate else
                                 "LEGACY_SCANNER_SHADOW_CANDIDATE" if guided_generation else
                                 "LEGACY_CANDIDATE")
        all_failed_gates, failed_gate_evidence = self._reject_gate_audit(result)
        if guided_without_candidate:
            shadow_geometry = {key: result.get(key) for key in (
                "side", "entry", "entry_price", "sl", "stop", "stop_loss", "tp", "target",
                "take_profit", "rr", "raw_rr", "risk_reward", "effective_rr", "setup_type",
                "setup_reason", "geometry_status", "geometry_reason", "geometry_source",
            )}
            result["legacy_shadow_geometry"] = {
                **shadow_geometry,
                "attributable": False,
                "non_attributable_reason": "LEGACY_SHADOW_NOT_GUIDED_EQUIVALENT",
                "all_failed_gates": list(all_failed_gates),
                "failed_gate_evidence": list(failed_gate_evidence),
            }
            # The scanner-shadow gates are diagnostic only. Do not leak them
            # back into canonical multi-gate evidence for a missing guided candidate.
            all_failed_gates = []
            failed_gate_evidence = []
            for key in (
                "side", "entry", "entry_price", "sl", "stop", "stop_loss", "structural_stop",
                "tp", "target", "take_profit", "structural_target", "rr", "raw_rr",
                "risk_reward", "effective_rr", "setup_type", "setup_reason", "geometry_source",
            ):
                result[key] = None
            result["geometry_status"] = "UNAVAILABLE"
            result["geometry_reason"] = "GUIDED_CANDIDATE_UNAVAILABLE"
            result["reject_quality_attributable"] = False
            result["non_attributable_reason"] = "LEGACY_SHADOW_NOT_GUIDED_EQUIVALENT"
            if primary_reject_reason == "LOW_EFFECTIVE_RR":
                # LOW_EFFECTIVE_RR requires attributable canonical guided geometry.
                # Preserve the legacy scanner diagnosis only as shadow evidence.
                result["legacy_shadow_geometry"]["reject_reason"] = primary_reject_reason
                result["source_primary_reject_reason"] = primary_reject_reason
                primary_reject_reason = "MTF_GUIDED_GEOMETRY_UNAVAILABLE"
                reject_reasons = [
                    primary_reject_reason,
                    *[reason for reason in reject_reasons if reason != "LOW_EFFECTIVE_RR"],
                ]
                reject_reasons = list(dict.fromkeys(reject_reasons))
                result["reason"] = primary_reject_reason
                if result.get("reject_reason") is not None:
                    result["reject_reason"] = primary_reject_reason
                result["authoritative_reject_reason"] = primary_reject_reason
        all_failed_gates, failed_gate_evidence = self._reject_gate_audit(result)
        campaign_id = (self._campaign_id or os.getenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID")) if self._burnin_run_id else None
        runtime_identity = (campaign_id or f"standalone:{self._burnin_run_id}") if self._burnin_run_id else None
        supplied_reject_decision_id = result.get("reject_decision_id")
        reject_decision_id = self._canonical_reject_decision_id(result)
        if supplied_reject_decision_id and str(supplied_reject_decision_id) != reject_decision_id:
            result["source_reject_decision_id"] = str(supplied_reject_decision_id)
        result.update({
            "reject_decision_id":str(reject_decision_id), "decision":"REJECTED",
            "primary_reject_reason": primary_reject_reason, "reject_reasons": reject_reasons,
            "all_failed_gates": list(dict.fromkeys([*all_failed_gates, *reject_reasons])),
            "failed_gate_evidence": failed_gate_evidence,
            "authoritative_reject_reason": result.get("authoritative_reject_reason") or primary_reject_reason,
            "mtf_execution_confirmation_mode": result.get(
                "mtf_execution_confirmation_mode", self.config.mtf_execution_confirmation_mode
            ),
            "decision_timestamp":result.get("decision_timestamp") or canonical_utc_timestamp(),
            "timeframe":result.get("timeframe") or result.get("interval"),
            "entry":result.get("entry", result.get("entry_price")), "sl":result.get("sl", result.get("stop_loss", result.get("stop"))),
            "tp":result.get("tp", result.get("take_profit", result.get("target"))),
            "setup_type":result.get("setup_type", result.get("setup")), "regime":result.get("regime") or regime_layer.get("regime"),
            "spread_pct":result.get("spread_pct",execution.get("spread_pct")), "expected_slippage_pct":result.get("expected_slippage_pct",execution.get("expected_slippage_pct")),
            "funding_rate_pct":result.get("funding_rate_pct",execution.get("funding_rate_pct")), "liquidity_score":result.get("liquidity_score",execution.get("liquidity_score")),
            "volatility_regime":result.get("volatility_regime",execution.get("volatility_regime")),
            "campaign_id": campaign_id or result.get("campaign_id"),
            "runtime_identity": runtime_identity or result.get("runtime_identity"),
            "forward_label_subject": (forward_label_subject if guided_generation
                                      else result.get("forward_label_subject") or forward_label_subject),
        })
        try:
            entry_value = float(result.get("entry"))
            stop_value = float(result.get("sl"))
            stop_distance_pct = abs(entry_value - stop_value) / entry_value * 100.0 if entry_value > 0 else None
        except (TypeError, ValueError):
            stop_distance_pct = None
        result.update({
            "stop_distance_pct": stop_distance_pct,
            "min_signal_score": float(self.config.min_signal_score),
            "min_raw_rr": float(self.config.min_rr),
            "min_effective_rr": float(self.config.min_effective_rr),
            "min_stop_pct": float(self.config.min_sl_pct),
            "max_stop_pct": float(self.config.max_sl_pct),
        })
        return result

    def _canonical_reject_decision_id(self, payload: Mapping[str, Any]) -> str:
        campaign_id = (self._campaign_id or os.getenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID")) if self._burnin_run_id else None
        runtime_identity = (campaign_id or f"standalone:{self._burnin_run_id}") if self._burnin_run_id else None
        return "reject:" + canonical_hash({
            "runtime_identity": runtime_identity,
            "burnin_run_id": self._burnin_run_id,
            "signal_id": str(payload.get("signal_id") or ""),
            "setup_identity": payload.get("setup_identity"),
            "symbol": payload.get("symbol"),
            "decision": "REJECTED",
        })[:24]

    def _reject_campaign_id(self) -> str | None:
        if not self._burnin_run_id:
            return None
        return self._campaign_id or os.getenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID") or f"standalone:{self._burnin_run_id}"

    def _persist_pending_reject(self, payload: Mapping[str, Any], *, conn: Any | None = None) -> str | None:
        """Durably enqueue eligible PAPER rejects; incomplete geometry remains auditable."""
        if self.config.execution_mode != ExecutionMode.PAPER or not self._burnin_run_id:
            return None
        reject_reason = str(payload.get("reason") or payload.get("reject_reason") or "").upper()
        if reject_reason in {
            "INVALID_MARKET_TIMESTAMP",
            "MARKET_TIMESTAMP_UNIT_MISMATCH",
            "MARKET_TIMESTAMP_IN_FUTURE",
            "STALE_MARKET_DATA",
        }:
            return None
        engine, campaign_id = self._resolve_persistence_engine(), self._reject_campaign_id()
        if engine is None or campaign_id is None:
            return None
        execution_ctx = dict(payload.get("execution_ctx") or {})
        costs = self._phase7_costs_from_execution_ctx(execution_ctx)
        signal_id = str(payload.get("signal_id") or "")
        shadow_geometry = (payload.get("legacy_shadow_geometry")
                           if payload.get("forward_label_subject") == "LEGACY_SCANNER_SHADOW_CANDIDATE"
                           and isinstance(payload.get("legacy_shadow_geometry"), Mapping) else {})
        label_geometry = shadow_geometry or payload
        planned_entry = label_geometry.get("entry", label_geometry.get("entry_price"))
        expected_fill = None if shadow_geometry else payload.get("expected_fill")
        derived_rr_metrics: Mapping[str, Any] = {}
        if not shadow_geometry and expected_fill is None:
            try:
                expected_fill, _ = self._expected_fill_price(label_geometry, execution_ctx)
                if expected_fill is not None:
                    derived_rr_metrics = self._execution_rr_metrics(
                        payload.get("candidate_rr", payload.get("rr", payload.get("raw_rr"))),
                        label_geometry,
                        execution_ctx,
                    )
                    expected_fill = derived_rr_metrics.get("expected_fill", expected_fill)
            except (TypeError, ValueError):
                expected_fill = None
                derived_rr_metrics = {}
        try:
            executable_entry = float(expected_fill) if expected_fill is not None else None
            if executable_entry is not None and (not math.isfinite(executable_entry) or executable_entry <= 0):
                executable_entry = None
        except (TypeError, ValueError):
            executable_entry = None
        execution_aligned = executable_entry is not None and not shadow_geometry
        label_entry = executable_entry if execution_aligned else planned_entry
        embedded_entry_slippage_cost = costs.get("entry_slippage_cost")
        if execution_aligned and embedded_entry_slippage_cost is not None:
            # The entry-side slippage is already represented by expected_fill.
            # Keep the explicit field at zero to preserve the complete R-cost
            # schema while preventing a second deduction in reject resolution.
            costs = {**costs, "entry_slippage_cost": 0.0}
        attributable = payload.get("reject_quality_attributable") is not False and execution_aligned
        non_attributable_reason = payload.get("non_attributable_reason")
        if shadow_geometry:
            attributable = False
            non_attributable_reason = non_attributable_reason or "LEGACY_SHADOW_NOT_GUIDED_EQUIVALENT"
        elif not execution_aligned:
            attributable = False
            non_attributable_reason = non_attributable_reason or "EXECUTION_PARITY_BASIS_UNAVAILABLE"
        try:
            initial_risk = abs(float(planned_entry) - float(
                label_geometry.get("sl", label_geometry.get("stop_loss", label_geometry.get("stop")))
            ))
            fill_shift_initial_risk_ratio = (
                abs(float(executable_entry) - float(planned_entry)) / initial_risk
                if execution_aligned and initial_risk > 0 else None
            )
        except (TypeError, ValueError):
            fill_shift_initial_risk_ratio = None
        reject_execution_basis = (
            "EXPECTED_FILL_RUNTIME_PARITY" if execution_aligned
            else "LEGACY_SCANNER_SHADOW" if shadow_geometry
            else "PLANNED_ENTRY_LEGACY"
        )
        try:
            def persist(target: Any) -> str | None:
                return persist_pending_reject_label(
                    target, campaign_id=campaign_id, burnin_run_id=self._burnin_run_id,
                    reject_decision_id=str(payload.get("reject_decision_id") or ""), signal_id=signal_id or None,
                    symbol=payload.get("symbol"), side=label_geometry.get("side"),
                    decision_timestamp=payload.get("decision_timestamp") or canonical_utc_timestamp(), timeframe=payload.get("timeframe"),
                    entry=label_entry,
                    stop=label_geometry.get("sl", label_geometry.get("stop_loss", label_geometry.get("stop"))),
                    target=label_geometry.get("tp", label_geometry.get("take_profit", label_geometry.get("target"))),
                    horizon_bars=self.config.reject_forward_horizon_bars,
                    execution_cost_assumptions=costs, regime=payload.get("regime") or payload.get("volatility_regime"),
                    reject_reason=payload.get("reason") or payload.get("reject_reason"),
                    source_provenance={"provider": self.scanner_source or "UNKNOWN", "timeframe": payload.get("timeframe"),
                                       "forward_label_subject": payload.get("forward_label_subject"),
                                       "forward_label_side": label_geometry.get("side"),
                                       "reject_quality_attributable": attributable,
                                       "non_attributable_reason": non_attributable_reason,
                                       "reject_execution_basis": reject_execution_basis,
                                       "planned_entry": planned_entry,
                                       "executable_entry": executable_entry,
                                       "score": payload.get("score"),
                                       "candidate_raw_rr": payload.get("candidate_rr", payload.get("rr")),
                                       "executable_raw_rr": payload.get("executable_raw_rr", derived_rr_metrics.get("executable_raw_rr")),
                                       "remaining_execution_penalty": payload.get("remaining_execution_penalty", derived_rr_metrics.get("remaining_execution_penalty")),
                                       "effective_rr_at_decision": payload.get("effective_rr"),
                                       "counterfactual_effective_rr": (
                                           None if payload.get("effective_rr") is not None
                                           else derived_rr_metrics.get("effective_rr")
                                       ),
                                       "min_signal_score": payload.get("min_signal_score"),
                                       "min_raw_rr": payload.get("min_raw_rr"),
                                       "min_effective_rr": payload.get("min_effective_rr"),
                                       "min_stop_pct": payload.get("min_stop_pct"),
                                       "max_stop_pct": payload.get("max_stop_pct"),
                                       "entry_slippage_embedded_in_fill": bool(execution_aligned),
                                       "embedded_entry_slippage_cost": embedded_entry_slippage_cost,
                                       "fill_shift_initial_risk_ratio": fill_shift_initial_risk_ratio,
                                       "stop_distance_pct": payload.get("stop_distance_pct"),
                                       "all_failed_gates": payload.get("all_failed_gates"),
                                       "failed_gate_evidence": payload.get("failed_gate_evidence"),
                                       "execution_cost_semantics": payload.get("execution_cost_semantics", derived_rr_metrics.get("execution_cost_semantics")),
                                       "campaign_intervals": list(self._campaign_intervals),
                                       "regime_timeframe": self.config.regime_timeframe,
                                       "setup_timeframe": self.config.setup_timeframe,
                                       "execution_timeframe": self.config.execution_timeframe,
                                       "forward_label_evaluation_timeframe": self.config.execution_timeframe,
                                       "mtf": payload.get("mtf"),
                                       "setup_type": payload.get("setup_type"), "volatility_regime": payload.get("volatility_regime"),
                                       "liquidity_score": execution_ctx.get("liquidity_score")},
                )
            if conn is not None:
                return persist(conn)
            with engine.begin() as owned_conn:
                return persist(owned_conn)
        except Exception as exc:
            self._burnin_evidence_incomplete = True
            logger.exception("pending_reject_label_persistence_failed", exc_info=exc)
            if conn is not None:
                raise
            return None

    async def _reject_forward_outcome_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._resolve_reject_forward_outcomes_once()
            except Exception as exc:
                logger.warning("reject_forward_outcome_cycle_failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=max(1.0, self.config.reject_resolver_interval_sec))
            except asyncio.TimeoutError:
                pass

    async def _resolve_reject_forward_outcomes_once(self) -> dict[str, int]:
        # Attached campaigns already have the BurnInCampaignRunner resolver;
        # keeping standalone ownership exclusive avoids competing evaluators.
        if os.getenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID"):
            return {"resolved": 0, "pending": 0, "ambiguous": 0, "failed": 0}
        engine, campaign_id = self._resolve_persistence_engine(), self._reject_campaign_id()
        if engine is None or campaign_id is None:
            return {"resolved": 0, "pending": 0, "ambiguous": 0, "failed": 0}
        now = canonical_utc_timestamp()
        with engine.connect() as conn:
            due = conn.execute(text("SELECT symbol, timeframe, MIN(decision_timestamp), MAX(due_at) FROM burnin_pending_reject_labels WHERE campaign_id=:cid AND status IN ('PENDING','READY') AND due_at<=:now GROUP BY symbol,timeframe"), {"cid": campaign_id, "now": now}).fetchall()
        candles = {}
        for symbol, timeframe, start, end in due:
            if self.reject_candle_provider is None:
                from alphaforge.burnin_campaign import BinanceReadOnlyCandleProvider
                provider = BinanceReadOnlyCandleProvider(interval=timeframe or "1m", base_url=self.config.market_data_base_url)
                candles[(symbol,timeframe)] = await asyncio.to_thread(provider, symbol, start, end)
            else:
                try: candles[(symbol,timeframe)] = await asyncio.to_thread(self.reject_candle_provider, symbol, start, end, timeframe)
                except TypeError: candles[(symbol,timeframe)] = await asyncio.to_thread(self.reject_candle_provider, symbol, start, end)
        with engine.begin() as conn:
            return resolve_campaign_batch(conn, campaign_id, candles, now=now)

    async def _emit_lifecycle_event(self, event: str, symbol: str, details: Mapping[str, Any] | None = None) -> None:
        detail_payload = dict(details or {})
        current_signal_id = (
            None
            if event == LifecycleState.SIGNAL_CREATED.value
            else self._current_signal_id_by_symbol.get(symbol)
        )
        signal_id = self._resolve_signal_id(
            symbol,
            detail_payload,
            current_signal_id=current_signal_id,
        )
        previous_state = self._last_lifecycle_state_by_signal.get(signal_id)
        is_error_event = event == LifecycleState.ERROR.value
        transition_valid = not is_error_event and validate_transition(previous_state, event)
        lifecycle_state = event if transition_valid or is_error_event else LifecycleState.ERROR.value
        error_scope = {
            key: value for key, value in {
                "burnin_run_id": self._burnin_run_id,
                "campaign_id": self._campaign_id or os.getenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID"),
            }.items() if value
        }
        if is_error_event:
            detail_payload = {
                **detail_payload,
                **error_scope,
                "signal_id": signal_id,
                "attempted_state": detail_payload.get("attempted_state") or event,
                "previous_state": detail_payload.get("previous_state") or previous_state or "NONE",
            }
        elif not transition_valid:
            detail_payload = {
                **detail_payload,
                **error_scope,
                "failure_reason": "INVALID_LIFECYCLE_TRANSITION",
                "signal_id": signal_id,
                "attempted_state": event,
                "previous_state": previous_state or "NONE",
                "invalid_transition": {
                    "signal_id": signal_id,
                    "symbol": symbol,
                    "previous_lifecycle_state": previous_state,
                    "attempted_lifecycle_state": event,
                },
            }
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
        self._current_signal_id_by_symbol[symbol] = signal_id
        self._last_lifecycle_state_by_signal[signal_id] = lifecycle_state
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
                "attempted_state": phase,
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
    def _resolve_signal_id(
        symbol: str,
        payload: Mapping[str, Any],
        *,
        current_signal_id: str | None = None,
    ) -> str:
        if payload.get("signal_id"):
            return str(payload["signal_id"])
        decision = payload.get("decision")
        if isinstance(decision, Mapping) and decision.get("signal_id"):
            return str(decision["signal_id"])
        if current_signal_id:
            return str(current_signal_id)
        candle_identity = RuntimeOrchestrator._execution_candle_decision_identity(symbol, payload)
        if candle_identity is not None:
            return f"runtime:{hashlib.sha256(candle_identity.encode('utf-8')).hexdigest()[:24]}"
        fingerprint = "|".join([
            str(symbol),
            str(payload.get("side", "UNKNOWN")),
            str(payload.get("timeframe", "NA")),
            str(payload.get("entry") or payload.get("entry_price") or 0.0),
            str(payload.get("market_ts") or payload.get("timestamp") or canonical_utc_timestamp()),
        ])
        return f"runtime:{hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:24]}"

    @staticmethod
    def _execution_candle_decision_identity(symbol: str, payload: Mapping[str, Any]) -> str | None:
        key = RuntimeOrchestrator._execution_candle_market_key(symbol, payload)
        candle_ts = RuntimeOrchestrator._normalize_execution_candle_open_ts(
            payload.get("execution_candle_open_ts")
        )
        if key is None or candle_ts is None:
            return None
        return "|".join((*key, str(candle_ts)))

    @staticmethod
    def _execution_candle_market_key(symbol: str, payload: Mapping[str, Any]) -> tuple[str, str, str] | None:
        if RuntimeOrchestrator._normalize_execution_candle_open_ts(
                payload.get("execution_candle_open_ts")) is None:
            return None
        normalized_symbol = str(symbol or "").strip().upper()
        source_exchange = str(payload.get("source_exchange") or "").strip().lower()
        timeframe = str(payload.get("timeframe") or "").strip().lower()
        if not all((normalized_symbol, source_exchange, timeframe)):
            return None
        return normalized_symbol, source_exchange, timeframe

    @staticmethod
    def _normalize_execution_candle_open_ts(value: Any) -> int | None:
        """Normalize an evidenced execution-candle open time to epoch milliseconds."""
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, str):
            candidate = value.strip()
            if not candidate:
                return None
            if not candidate.lstrip("+-").replace(".", "", 1).isdigit():
                try:
                    parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    return None
                if parsed.tzinfo is None:
                    return None
                milliseconds = Decimal(str(parsed.astimezone(timezone.utc).timestamp())) * 1000
            else:
                try:
                    milliseconds = Decimal(candidate)
                except InvalidOperation:
                    return None
        else:
            try:
                milliseconds = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                return None
        if not milliseconds.is_finite() or milliseconds < 0:
            return None
        integral = milliseconds.to_integral_value()
        if milliseconds != integral:
            return None
        try:
            return int(integral)
        except (OverflowError, ValueError):
            return None

    def _canonical_final_decision_recorded(self, signal_id: str) -> bool | None:
        """Return durable PAPER finalization state; lookup failure is fail-closed.

        An accepted PAPER fill is authoritative even when its final evidence is
        in decision_evidence or the campaign position rather than a final-phase
        order_decisions row. The position also closes the crash window before
        the post-fill decision evidence transaction.
        """
        if self.config.execution_mode is not ExecutionMode.PAPER:
            return False
        engine = self._resolve_persistence_engine()
        if engine is None:
            return False
        try:
            with engine.connect() as conn:
                final_rows = conn.execute(text("""
                    SELECT decision_id, symbol, decision FROM order_decisions
                    WHERE signal_id=:signal_id
                      AND UPPER(COALESCE(mode, ''))='PAPER'
                      AND LOWER(COALESCE(phase, ''))='final'
                      AND UPPER(COALESCE(decision, '')) IN ('ACCEPTED', 'REJECTED')
                """), {"signal_id": signal_id}).fetchall()
                if not self._burnin_run_id:
                    if self._campaign_id or os.getenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID"):
                        return None
                    return bool(final_rows)
                for row in final_rows:
                    if str(row.decision).upper() == "REJECTED" and row.decision_id == self._canonical_reject_decision_id({
                        "signal_id": signal_id, "symbol": row.symbol,
                    }):
                        return True
                if conn.execute(text("""
                    SELECT 1 FROM decision_evidence
                    WHERE signal_id=:signal_id AND run_id=:burnin_run_id
                      AND UPPER(COALESCE(mode, ''))='PAPER'
                      AND ((UPPER(COALESCE(decision, ''))='ACCEPT'
                            AND lifecycle_state_after='POSITION_OPENED')
                           OR (UPPER(COALESCE(decision, ''))='REJECT'
                               AND lifecycle_state_after='SIGNAL_REJECTED'))
                    LIMIT 1
                """), {"signal_id": signal_id, "burnin_run_id": self._burnin_run_id}).first() is not None:
                    return True
                campaign_id = self._campaign_id or os.getenv("ALPHAFORGE_BURNIN_CAMPAIGN_ID")
                if not campaign_id:
                    return False
                return conn.execute(text("""
                    SELECT 1 FROM burnin_pending_position_outcomes
                    WHERE signal_id=:signal_id AND burnin_run_id=:burnin_run_id
                      AND campaign_id=:campaign_id
                    LIMIT 1
                """), {
                    "signal_id": signal_id, "burnin_run_id": self._burnin_run_id,
                    "campaign_id": campaign_id,
                }).first() is not None
        except Exception as exc:
            self.metrics.final_decision_lookup_failures += 1
            logger.exception(
                "paper_final_decision_lookup_failed signal_id=%s", signal_id, exc_info=exc,
            )
            return None

    @staticmethod
    def _setup_opportunity_identity(symbol: str, mtf: Any) -> str | None:
        """Identify one closed setup-timeframe opportunity independently of 1m scans."""
        if not isinstance(mtf, Mapping):
            return None
        setup = mtf.get("setup")
        if not isinstance(setup, Mapping):
            return None
        timeframe = str(setup.get("timeframe") or "").strip().lower()
        close_ms = setup.get("last_closed_candle_ms")
        if not timeframe or not isinstance(close_ms, int):
            return None
        return f"setup:{str(symbol).upper()}:{timeframe}:{close_ms}"

    def _setup_decision_recorded(self, setup_identity: str, *, decision: str) -> bool:
        normalized = str(decision).upper()
        memory = (self._accepted_setup_identities if normalized == "ACCEPTED"
                  else self._canonical_setup_reject_ids)
        if setup_identity in memory:
            return True
        engine = self._resolve_persistence_engine()
        if engine is None:
            return False
        observation_id = self._setup_observation_id(setup_identity, normalized)
        try:
            with engine.connect() as conn:
                exists = conn.execute(text(f"""SELECT 1 FROM burnin_observations o
                    WHERE o.burnin_run_id=:bid AND (
                      o.observation_id=:oid OR (
                        UPPER(COALESCE(o.decision,''))=:decision
                        AND json_extract(o.metrics_json,'$.setup_identity')=:setup_identity
                        AND {canonical_decision_sql('o')}
                      )) LIMIT 1"""), {
                    "bid": self._burnin_run_id, "oid": observation_id,
                    "decision": normalized, "setup_identity": setup_identity,
                }).first()
        except Exception:
            return False
        if exists:
            memory.add(setup_identity)
            return True
        return False

    def _setup_decision_key(self, setup_identity: str) -> str:
        scope = self._reject_campaign_id() or self.runtime_instance_id
        return "setup:" + canonical_hash({"scope": scope, "setup_identity": setup_identity})[:24]

    def _setup_observation_id(self, setup_identity: str, decision: str) -> str:
        return f"obs:{self._setup_decision_key(setup_identity)}:{str(decision).upper()}"

    def _persist_geometry_diagnostic(self, symbol: str, payload: Mapping[str, Any], reason: str) -> None:
        """Persist one non-canonical provider diagnostic per run/market/reason."""
        if not self._burnin_run_id:
            self._start_or_resume_burnin_run()
        engine = self._resolve_persistence_engine()
        if engine is None or not self._burnin_run_id:
            self._burnin_evidence_incomplete = True
            return
        identity = {"burnin_run_id": self._burnin_run_id, "symbol": symbol,
                    "source_exchange": payload.get("source_exchange"),
                    "timeframe": payload.get("timeframe"), "geometry_reason": reason}
        observation_id = "geometry_provider_diagnostic_" + canonical_hash(identity)[:20]
        with engine.begin() as conn:
            exists = conn.execute(text(
                "SELECT 1 FROM burnin_observations WHERE observation_id=:oid"
            ), {"oid": observation_id}).first()
            if exists:
                return
            persist_burnin_observation(
                conn, observation_id=observation_id, burnin_run_id=self._burnin_run_id,
                release_id=os.getenv("ALPHAFORGE_RELEASE_ID", self.config.phase7_burnin_release_id),
                execution_mode=self.config.execution_mode.value, symbol=symbol,
                interval=payload.get("timeframe"), regime=payload.get("regime") or "UNKNOWN",
                decision=None, lifecycle_state=None,
                metrics={"geometry_status": payload.get("geometry_status"),
                         "geometry_reason": reason, "geometry_source": payload.get("geometry_source")},
                source_provenance={"provider": self.scanner_source or "UNKNOWN",
                                   "source_exchange": payload.get("source_exchange")},
                missing_fields=("execution_candle_open_ts",),
                observation_kind=DIAGNOSTIC_OBSERVATION_KIND,
            )
        self.metrics.burnin_observations += 1

    def _evaluate_market_timestamp(
        self, market_ts_raw: Any, *, now: float
    ) -> tuple[str | None, dict[str, Any]]:
        """Validate provider market time in canonical epoch-seconds semantics."""
        configured_skew_ms = int(self.config.max_clock_skew_ms)
        allowed_future_skew_sec = configured_skew_ms / 1000.0
        stale_limit_sec = float(self.config.stale_market_data_sec)
        evidence: dict[str, Any] = {
            "raw_market_ts": market_ts_raw,
            "runtime_now_sec": now,
            "configured_max_clock_skew_ms": configured_skew_ms,
            "allowed_future_skew_sec": allowed_future_skew_sec,
            "stale_market_data_sec": stale_limit_sec,
            "policy_source": "CONFIG_REGISTRY:ALPHAFORGE_MAX_CLOCK_SKEW_MS",
            "expected_unit": "epoch_seconds",
        }

        if market_ts_raw in (None, "") or isinstance(market_ts_raw, bool):
            evidence.update({
                "status": "REJECT",
                "reason": "INVALID_MARKET_TIMESTAMP",
                "observed": market_ts_raw,
                "reject_threshold": "FINITE_EPOCH_SECONDS",
                "comparison": "VALID_TIMESTAMP_REQUIRED",
            })
            return "INVALID_MARKET_TIMESTAMP", evidence

        try:
            market_ts = float(market_ts_raw)
        except (TypeError, ValueError):
            evidence.update({
                "status": "REJECT",
                "reason": "INVALID_MARKET_TIMESTAMP",
                "observed": str(market_ts_raw),
                "reject_threshold": "FINITE_EPOCH_SECONDS",
                "comparison": "VALID_TIMESTAMP_REQUIRED",
            })
            return "INVALID_MARKET_TIMESTAMP", evidence

        evidence["market_ts_sec"] = market_ts
        if not math.isfinite(market_ts):
            evidence.update({
                "status": "REJECT",
                "reason": "INVALID_MARKET_TIMESTAMP",
                "observed": str(market_ts_raw),
                "reject_threshold": "FINITE_EPOCH_SECONDS",
                "comparison": "IS_FINITE",
            })
            return "INVALID_MARKET_TIMESTAMP", evidence

        # Detect the common seconds/milliseconds conversion error without
        # accepting or silently normalizing it. The 100x ratio is unit-shape
        # detection only; the allowed clock skew remains registry-authoritative.
        milliseconds_as_seconds = market_ts / 1000.0
        unit_detection_window_sec = max(stale_limit_sec, allowed_future_skew_sec, 1.0)
        if (
            now > 0.0
            and market_ts > now * 100.0
            and abs(milliseconds_as_seconds - now) <= unit_detection_window_sec
        ):
            evidence.update({
                "status": "REJECT",
                "reason": "MARKET_TIMESTAMP_UNIT_MISMATCH",
                "observed": market_ts,
                "normalized_candidate_seconds": milliseconds_as_seconds,
                "detected_unit": "epoch_milliseconds",
                "reject_threshold": "EPOCH_SECONDS",
                "comparison": "UNIT_MUST_MATCH",
            })
            return "MARKET_TIMESTAMP_UNIT_MISMATCH", evidence

        future_delta_sec = market_ts - now
        if future_delta_sec > allowed_future_skew_sec:
            evidence.update({
                "status": "REJECT",
                "reason": "MARKET_TIMESTAMP_IN_FUTURE",
                "observed": market_ts,
                "future_delta_sec": future_delta_sec,
                "reject_threshold": now + allowed_future_skew_sec,
                "comparison": "<=",
            })
            return "MARKET_TIMESTAMP_IN_FUTURE", evidence

        age_sec = now - market_ts
        if age_sec > stale_limit_sec:
            evidence.update({
                "status": "REJECT",
                "reason": "STALE_MARKET_DATA",
                "observed": market_ts,
                "age_sec": age_sec,
                "reject_threshold": now - stale_limit_sec,
                "comparison": ">=",
            })
            return "STALE_MARKET_DATA", evidence

        evidence.update({
            "status": "PASS",
            "reason": None,
            "observed": market_ts,
            "future_delta_sec": max(0.0, future_delta_sec),
            "age_sec": max(0.0, age_sec),
            "reject_threshold": now + allowed_future_skew_sec,
            "comparison": "<=",
        })
        return None, evidence

    def _evaluate_runtime_risk(self, symbol: str, market_ctx: MutableMapping[str, Any]) -> str | None:
        now = time.time()
        if self._reconciliation_persistence_unhealthy:
            return "RECONCILIATION_PERSISTENCE_FAILED"
        if self.metrics.heartbeat_persistence_degraded:
            return "RUNTIME_DB_UNAVAILABLE"
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

        market_time_reject, market_time_evidence = self._evaluate_market_timestamp(
            market_ctx.get("market_ts"), now=now
        )
        market_ctx["market_time_evidence"] = market_time_evidence
        if market_time_reject == "STALE_MARKET_DATA":
            self._stale_market_data_symbols.add(symbol)
        if market_time_reject is not None:
            return market_time_reject

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

    def _mark_heartbeat_persistence_recovery_required(self) -> bool:
        """Persist the controlled recovery transition before allowing runtime exit.

        Returning False means SQLite is still contended.  The runtime remains
        alive but fail-closed and retries this transition on the next heartbeat
        cycle, so the outer campaign supervisor never has to infer a terminal
        cause from an unexplained runtime exit.
        """
        self._runtime_status = "RECOVERY_REQUIRED"
        self._recovery_required = True
        self._fail_closed_reason = "SUSTAINED_HEARTBEAT_PERSISTENCE_FAILURE"
        engine = self._resolve_persistence_engine()
        if engine is None or not self._campaign_id:
            return True
        try:
            with engine.begin() as conn:
                terminalize_active_campaign_run(
                    conn,
                    self._campaign_id,
                    run_status="RECOVERY_REQUIRED",
                    campaign_status="RECOVERY_REQUIRED",
                    reason=self._fail_closed_reason,
                    event_type="RUNTIME_PERSISTENCE_RECOVERY_REQUIRED",
                    details={
                        "runtime_instance_id": self.runtime_instance_id,
                        "failure_streak": self._heartbeat_persistence_failure_streak,
                        "failure_threshold": self._heartbeat_persistence_failure_threshold,
                    },
                    clear_worker_metadata=False,
                )
            return True
        except OperationalError as exc:
            if not is_sqlite_busy_error(exc):
                raise
            logger.error(
                "heartbeat_persistence_recovery_state_not_persisted reason=SQLITE_BUSY "
                "failure_streak=%s threshold=%s",
                self._heartbeat_persistence_failure_streak,
                self._heartbeat_persistence_failure_threshold,
            )
            return False

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                self.metrics.last_heartbeat_ts = time.time()
                if (
                    self._recovery_required
                    and self._fail_closed_reason == "SUSTAINED_HEARTBEAT_PERSISTENCE_FAILURE"
                ):
                    if self._mark_heartbeat_persistence_recovery_required():
                        self.shutdown()
                        return
                    await asyncio.sleep(self.config.heartbeat_interval_sec)
                    continue

                was_degraded = self.metrics.heartbeat_persistence_degraded
                try:
                    self._persist_runtime_heartbeat()
                    self._persist_runtime_state_snapshot("OPERATING")
                except OperationalError as exc:
                    if not is_sqlite_busy_error(exc):
                        raise
                    self._heartbeat_persistence_failure_streak += 1
                    self.metrics.heartbeat_persistence_failures += 1
                    self.metrics.heartbeat_persistence_degraded = True
                    logger.warning(
                        "heartbeat_persistence_degraded reason=SQLITE_BUSY failure_streak=%s threshold=%s",
                        self._heartbeat_persistence_failure_streak,
                        self._heartbeat_persistence_failure_threshold,
                    )
                    if self._heartbeat_persistence_failure_streak >= self._heartbeat_persistence_failure_threshold:
                        if self._mark_heartbeat_persistence_recovery_required():
                            self.shutdown()
                            return
                    await asyncio.sleep(self.config.heartbeat_interval_sec)
                    continue

                if was_degraded:
                    self.metrics.heartbeat_persistence_recoveries += 1
                    logger.info(
                        "heartbeat_persistence_recovered prior_failure_streak=%s",
                        self._heartbeat_persistence_failure_streak,
                    )
                self._heartbeat_persistence_failure_streak = 0
                self.metrics.heartbeat_persistence_degraded = False
                logger.info(
                    "runtime_heartbeat=%s persistence_enabled=%s top_selection_reject_reasons=%s top_selection_advisory_reasons=%s decision_gate_blockers=%s",
                    self.metrics,
                    self.metrics.persistence_enabled,
                    dict(sorted(self._last_scan_rejection_summary.items(), key=lambda item: item[1], reverse=True)[:3]),
                    dict(sorted(self._last_scan_advisory_summary.items(), key=lambda item: item[1], reverse=True)[:3]),
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
            self._unknown_exchange_state = True
            self._exchange_read_only_status = "UNAVAILABLE"
            self._reconciliation_status = "EXCHANGE_STATE_UNKNOWN"
            self._fail_closed_reason = "EXCHANGE_STATE_UNKNOWN"
            self._provider_failure_class = TRANSIENT_TRANSPORT
            self._provider_failure_count += 1
            if self._transient_provider_outage_started_monotonic is None:
                self._transient_provider_outage_started_monotonic = time.monotonic()
            self._runtime_status = "RECOVERY_REQUIRED"
            await self._record_incident("GLOBAL", LifecycleEventType.RECONCILIATION_REPAIR.value, "reconciliation_timeout")
            engine = self._resolve_persistence_engine()
            if engine is not None:
                self._reconciliation_persistence_unhealthy = True
                state = self._build_runtime_state_snapshot(status="RECOVERY_REQUIRED")
                try:
                    persist_reconciliation_cycle(engine, cycle_id=f"recon:timeout:{uuid.uuid4().hex}",
                        findings=[], snapshot=state, diagnostics={"evidence_status": "INCOMPLETE",
                            "failure_class": TRANSIENT_TRANSPORT, "errors": ["reconciliation_timeout"]},
                        pending_failures=self._pending_reconciliation_persistence_failures)
                except ReconciliationPersistenceFailure as exc:
                    self._pending_reconciliation_persistence_failures.append({"cycle_id": f"recon-failure:{uuid.uuid4().hex}",
                        "failed_cycle_id": "recon:timeout", "timestamp": canonical_utc_timestamp(), "reason": str(exc)})
                    return
                self._pending_reconciliation_persistence_failures.clear()
                self._reconciliation_persistence_unhealthy = False
            if time.monotonic() - self._transient_provider_outage_started_monotonic >= self.config.provider_transient_outage_grace_seconds:
                self._pause_for_provider_failure("PROVIDER_TRANSIENT_OUTAGE_GRACE_EXPIRED")

    async def _reconcile_runtime_state(self) -> None:
        # A CLEAN observation is not tradable until its evidence commits.
        self._reconciliation_persistence_unhealthy = True
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
                try:
                    snapshot_source = dict(provider.snapshot())
                except Exception as exc:
                    if self.config.execution_mode == ExecutionMode.LIVE:
                        raise
                    snapshot_source = {"orders": [], "positions": [], "fills": [],
                        "evidence_status": "INCOMPLETE", "errors": [exc.__class__.__name__],
                        "failure_class": classify_provider_exception(exc),
                        "input_source": "READ_ONLY_PROVIDER_FAILURE"}
                complete = str(snapshot_source.get("evidence_status") or "INCOMPLETE").upper() == "COMPLETE"
                previous_unknown = bool(self._unknown_exchange_state or self._transient_provider_outage_started_monotonic is not None or self._resolver_provider_recovery_pending)
                self._unknown_exchange_state = not complete or previous_unknown
                self._exchange_read_only_status = "AVAILABLE" if complete else "UNAVAILABLE"
                if complete and self._fail_closed_reason in {
                    "EXCHANGE_STATE_UNKNOWN", "EXCHANGE_RECONCILIATION_UNAVAILABLE", "RECONCILIATION_PERSISTENCE_FAILED", "RESOLVER_PROVIDER_UNAVAILABLE"
                } and not self._resolver_provider_unavailable:
                    self._fail_closed_reason = None
                if complete and self._resolver_provider_unavailable:
                    self._fail_closed_reason = "RESOLVER_PROVIDER_UNAVAILABLE"
                if not complete:
                    if self.config.execution_mode == ExecutionMode.LIVE:
                        raise RuntimeError("LIVE mode blocked: reconciliation evidence incomplete")
                    self._provider_failure_class = classify_reconciliation_snapshot(snapshot_source)
                    self._provider_failure_count += 1
                    if self._provider_failure_class == TRANSIENT_TRANSPORT and self._transient_provider_outage_started_monotonic is None:
                        self._transient_provider_outage_started_monotonic = time.monotonic()
                    self._fail_closed_reason = "EXCHANGE_STATE_UNKNOWN"
                    self._reconciliation_status = "EXCHANGE_STATE_UNKNOWN"
                    self._runtime_status = "RECOVERY_REQUIRED"
                elif previous_unknown and not self._resolver_provider_unavailable:
                    snapshot_source["recovery_transition"] = "CLEAN_RECONCILIATION_COMMITTED"
        else:
            snapshot_source = {"orders": [], "positions": [], "fills": []}
        snapshot = self._reconciliation_engine.snapshot_from_source(snapshot_source)
        findings, recommendations, _metrics = self._reconciliation_engine.reconcile(
            intended_orders=list(self._pending_orders.values()) if self.config.execution_mode in {ExecutionMode.LIVE, ExecutionMode.LIVE_PRECHECK} else [],
            lifecycle_state_by_symbol=self._last_lifecycle_state_by_symbol,
            snapshot=snapshot,
            mode=self.config.execution_mode.value,
        )
        self._orphan_orders = [dict(getattr(f, "evidence", {}) or {"symbol": f.symbol, "type": f.finding_type}) for f in findings if "ORDER" in str(f.finding_type).upper() and getattr(f, "fail_closed", False)]
        self._orphan_positions = [dict(getattr(f, "evidence", {}) or {"symbol": f.symbol, "type": f.finding_type}) for f in findings if "POSITION" in str(f.finding_type).upper() and getattr(f, "fail_closed", False)]
        self._unreconciled_symbols = {str(f.symbol) for f in findings if getattr(f, "fail_closed", False)}
        if findings and self._fail_closed_reason is None:
            self._fail_closed_reason = "ORPHAN_ORDER_DETECTED" if self._orphan_orders else ("ORPHAN_POSITION_DETECTED" if self._orphan_positions else "UNRECONCILED_POSITION")
        if self._exchange_read_only_status == "AVAILABLE" and self._fail_closed_reason:
            self._reconciliation_status = "DIRTY"
        if not self._fail_closed_reason:
            self._unknown_exchange_state = False
            if self.config.execution_mode != ExecutionMode.BACKTEST:
                self._reconciliation_status = "CLEAN"
        engine = self._resolve_persistence_engine()
        if engine is not None:
            if self._reconciliation_status == "EXCHANGE_STATE_UNKNOWN" or snapshot_source.get("recovery_transition"):
                # Do not deduplicate repeated failures or the recovery commit
                # against an earlier provider snapshot with identical payload.
                snapshot_source["reconciliation_attempt_id"] = uuid.uuid4().hex
            identity_payload = {
                "instance_id": self.runtime_instance_id, "startup_id": self.startup_id,
                "mode": self.config.execution_mode.value, "source": snapshot_source,
                "status": self._reconciliation_status, "exchange_read_only_status": self._exchange_read_only_status,
                "findings": [{"type": f.finding_type, "symbol": f.symbol, "ref": f.lifecycle_ref,
                              "evidence": f.evidence, "fail_closed": f.fail_closed} for f in findings],
            }
            cycle_id = "recon:v1:" + hashlib.sha256(json.dumps(identity_payload, sort_keys=True, default=str).encode()).hexdigest()
            state = self._build_runtime_state_snapshot(status="RECONCILED" if not self._fail_closed_reason else "RECOVERY_REQUIRED")
            if self._pending_reconciliation_persistence_failures and not self._fail_closed_reason:
                state.last_error = None
            try:
                persist_reconciliation_cycle(engine, cycle_id=cycle_id, findings=findings, snapshot=state,
                                             diagnostics=snapshot_source,
                                             pending_failures=self._pending_reconciliation_persistence_failures,
                                             recovery_transition={"cycle_id": cycle_id, "provider_failure_count": self._provider_failure_count,
                                                 "previous_failure_class": self._provider_failure_class}
                                                 if snapshot_source.get("recovery_transition") and state.reconciliation_status == "CLEAN" else None)
            except ReconciliationPersistenceFailure as exc:
                self._reconciliation_status = "PERSISTENCE_FAILED"
                self._fail_closed_reason = "RECONCILIATION_PERSISTENCE_FAILED"
                if self.config.execution_mode != ExecutionMode.BACKTEST:
                    self._unknown_exchange_state = True
                self._last_error = str(exc)
                self._pending_reconciliation_persistence_failures.append({
                    "cycle_id": f"recon-failure:{uuid.uuid4().hex}", "failed_cycle_id": cycle_id,
                    "timestamp": canonical_utc_timestamp(), "reason": str(exc),
                })
                logger.error("reconciliation_persistence_failed cycle_id=%s reason=%s", cycle_id, exc)
                return
            had_persistence_failure = bool(self._pending_reconciliation_persistence_failures)
            self._pending_reconciliation_persistence_failures.clear()
            if had_persistence_failure and not self._fail_closed_reason:
                self._last_error = None
        self._reconciliation_persistence_unhealthy = False
        if self._exchange_read_only_status == "AVAILABLE":
            self._provider_failure_class = None
            self._provider_failure_count = 0
            self._transient_provider_outage_started_monotonic = None
        if self._reconciliation_status == "CLEAN" and not self._fail_closed_reason:
            self._resolver_provider_recovery_pending = False
        if self._reconciliation_status == "CLEAN" and not self._fail_closed_reason:
            self._runtime_status = "OPERATING"
        elif self._provider_failure_class == PERMANENT_AUTH_OR_PROTOCOL:
            self._pause_for_provider_failure("PROVIDER_PERMANENT_FAILURE")
        elif self._provider_failure_class == UNKNOWN and self._provider_failure_count >= 3:
            self._pause_for_provider_failure("PROVIDER_UNKNOWN_FAILURE_THRESHOLD")
        elif (self._provider_failure_class == TRANSIENT_TRANSPORT
              and self._transient_provider_outage_started_monotonic is not None
              and time.monotonic() - self._transient_provider_outage_started_monotonic >= self.config.provider_transient_outage_grace_seconds):
            self._pause_for_provider_failure("PROVIDER_TRANSIENT_OUTAGE_GRACE_EXPIRED")
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
        if self._shadow_queue is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._shadow_queue.join(), timeout=self.config.agent_graph_stage_timeout_seconds)
        tasks = [*self._tasks, *([self._shadow_worker_task] if self._shadow_worker_task else [])]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.metrics.agent_shadow_worker_count = 0

    def _register_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.shutdown)

    @staticmethod
    def _build_signal(selection: SymbolSelectionResult, market_ctx: Mapping[str, Any], *, signal_id: str | None = None) -> dict[str, Any]:
        return build_signal_payload(
            selection.symbol, market_ctx,
            signal_id=signal_id or RuntimeOrchestrator._resolve_signal_id(selection.symbol, market_ctx),
            default_mode="PAPER",
            regime_fallback=getattr(selection, "regime_hint", None),
        )


def execution_mode_from_env(raw_mode: str | None) -> ExecutionMode:
    mode = (str(raw_mode or "PAPER").strip() or "PAPER").upper()
    try:
        return ExecutionMode(mode)
    except ValueError as exc:
        raise ValueError(f"Unsupported EXECUTION_MODE={raw_mode!r}. Expected BACKTEST/PAPER/LIVE_PRECHECK/LIVE") from exc


def _runtime_config_from_app_config(cfg: Any, mode: ExecutionMode) -> RuntimeConfig:
    """Freeze typed application config into the runtime startup snapshot."""
    return RuntimeConfig(
        execution_mode=mode,
        min_signal_score=cfg.runtime.min_signal_score,
        scan_interval_sec=cfg.runtime.scan_interval_sec,
        heartbeat_interval_sec=cfg.runtime.heartbeat_interval_sec,
        reject_forward_horizon_bars=cfg.runtime.reject_forward_horizon_bars,
        reject_resolver_interval_sec=cfg.runtime.reject_resolver_interval_sec,
        max_symbols_per_scan=cfg.runtime.max_symbols_per_scan,
        max_reject_log_entries=cfg.runtime.max_reject_log_entries,
        max_concurrent_positions=cfg.runtime.max_concurrent_positions,
        symbol_cooldown_sec=cfg.runtime.symbol_cooldown_sec,
        max_notional_exposure=cfg.runtime.max_notional_exposure,
        max_symbol_notional=cfg.runtime.max_symbol_notional,
        max_daily_loss_pct=cfg.runtime.max_daily_loss_pct,
        stale_market_data_sec=cfg.runtime.stale_market_data_sec,
        max_clock_skew_ms=cfg.runtime.max_clock_skew_ms,
        min_rr=cfg.runtime.min_rr,
        min_effective_rr=cfg.runtime.min_effective_rr,
        max_spread_pct=cfg.runtime.max_spread_pct,
        max_expected_slippage_pct=cfg.runtime.max_expected_slippage_pct,
        max_total_cost_pct=cfg.runtime.max_total_cost_pct,
        min_liquidity_score=cfg.runtime.min_liquidity_score,
        max_volatility_penalty_pct=cfg.runtime.max_volatility_penalty_pct,
        reject_unknown_execution_context=cfg.runtime.reject_unknown_execution_context,
        max_latency_ms=cfg.runtime.max_latency_ms,
        paper_fee_bps=cfg.runtime.paper_fee_bps,
        paper_execution_latency_ms=cfg.runtime.paper_execution_latency_ms,
        market_data_base_url=cfg.exchange.binance.market_data_base_url,
        regime_timeframe=cfg.runtime.regime_timeframe,
        setup_timeframe=cfg.runtime.setup_timeframe,
        execution_timeframe=cfg.runtime.execution_timeframe,
        mtf_guided_signal_generation_enabled=cfg.runtime.mtf_guided_signal_generation_enabled,
        mtf_execution_confirmation_mode=cfg.runtime.mtf_execution_confirmation_mode,
        regime_direction_threshold=cfg.runtime.regime_direction_threshold,
        setup_direction_threshold=cfg.runtime.setup_direction_threshold,
        execution_direction_threshold=cfg.runtime.execution_direction_threshold,
        enable_state_direction_resolution=cfg.runtime.enable_state_direction_resolution,
        paper_decision_timeframe=cfg.runtime.execution_timeframe,
        require_mtf_alignment=False,
        max_abs_funding_rate_pct=cfg.runtime.max_abs_funding_rate_pct,
        min_liquidity_usd=cfg.runtime.min_liquidity_usd,
        min_sl_pct=cfg.runtime.min_sl_pct,
        max_sl_pct=cfg.runtime.max_sl_pct,
        min_atr_pct=cfg.runtime.min_atr_pct,
        max_atr_pct=cfg.runtime.max_atr_pct,
        block_unknown_expectancy=cfg.runtime.block_unknown_expectancy,
        block_chop_market=cfg.runtime.block_chop_market,
        require_regime_alignment=cfg.runtime.require_regime_alignment,
        enable_orderbook_filter=cfg.runtime.enable_orderbook_filter,
        stop_too_wide_hard_reject=cfg.runtime.stop_too_wide_hard_reject,
        stop_too_wide_soft_score_min=cfg.runtime.stop_too_wide_soft_score_min,
        stop_too_wide_soft_effective_rr_min=cfg.runtime.stop_too_wide_soft_effective_rr_min,
        stop_too_wide_max_risk_scale=cfg.runtime.stop_too_wide_max_risk_scale,
        stop_too_wide_extreme_mult=cfg.runtime.stop_too_wide_extreme_mult,
        max_trades_global_per_day=cfg.runtime.max_trades_global_per_day,
        max_trades_symbol_per_day=cfg.runtime.max_trades_symbol_per_day,
        symbol_loss_streak_limit=cfg.runtime.symbol_loss_streak_limit,
        global_loss_streak_limit=cfg.runtime.global_loss_streak_limit,
        global_kill_switch=cfg.runtime.global_kill_switch,
        require_live_qualification=cfg.runtime.require_live_qualification,
        enable_shadow_mode=cfg.runtime.enable_shadow_mode,
        enable_canary_mode=cfg.runtime.enable_canary_mode,
        operator_live_acknowledged=cfg.runtime.operator_live_acknowledged,
        allow_live_orders=cfg.runtime.allow_live_orders,
        live_trading_enabled=cfg.runtime.live_enabled,
        reconciliation_interval_sec=cfg.runtime.reconciliation_interval_sec,
        reconciliation_timeout_sec=cfg.runtime.reconciliation_timeout_sec,
        provider_transient_outage_grace_seconds=cfg.runtime.provider_transient_outage_grace_seconds,
        require_exchange_connectivity_for_live=cfg.runtime.require_exchange_connectivity_for_live,
        required_live_exchanges=cfg.runtime.required_live_exchanges,
        exchange_connectivity_timeout_sec=cfg.runtime.exchange_connectivity_timeout_sec,
        enable_binance_readonly_reconciliation=cfg.runtime.enable_binance_readonly_reconciliation,
    )


def _build_runtime_from_env(*, persistence_engine: Engine | None = None, session_factory: Any | None = None) -> RuntimeOrchestrator:
    cfg = load_config_from_env()
    mode = execution_mode_from_env(cfg.runtime.execution_mode)
    persistence_enabled = cfg.persistence.enabled
    resolved_database_url = (
        str(persistence_engine.url) if persistence_engine is not None
        else cfg.persistence.database_url
    )
    engine = persistence_engine or init_db(resolved_database_url)
    SessionLocal = session_factory or sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))
        table_names = [str(row[0]) for row in rows]
    logger.info("runtime_db_bootstrap persistence_enabled=%s resolved_db_url=%s schema_initialized=%s tables=%s", persistence_enabled, resolved_database_url, True, table_names)
    brain = AIBrain(session_factory=SessionLocal, min_accept_score=cfg.runtime.min_signal_score)
    config = _runtime_config_from_app_config(cfg, mode)
    config.agent_graph_enabled = cfg.runtime.agent_graph_enabled
    config.agent_graph_shadow = cfg.runtime.agent_graph_shadow
    config.agent_graph_max_steps = cfg.runtime.agent_graph_max_steps
    config.agent_graph_max_reflection_retries = cfg.runtime.agent_graph_max_reflection_retries
    config.agent_graph_stage_timeout_seconds = cfg.runtime.agent_graph_stage_timeout_seconds
    config.agent_graph_persist_traces = cfg.runtime.agent_graph_persist_traces
    config.agent_graph_max_pending_runs = cfg.runtime.agent_graph_max_pending_runs
    config.agent_graph_database_url = cfg.runtime.agent_graph_database_url

    async def _safe_market_scanner() -> list[dict[str, Any]]:
        now_ts = time.time()
        return [{"symbol": "BTCUSDT", "volume_24h_usdt": 125_000_000.0, "spread_pct": 0.0009, "funding_rate_pct": 0.00005, "liquidity_score": 0.86, "liquidity_quality": "HIGH", "volatility_pct": 0.011, "volatility_fit": "GOOD", "volatility_regime": "MODERATE", "trend_strength": 0.64, "momentum_confirmation": 0.7, "recent_volume_change_pct": 0.085, "chop_score": 0.27, "panic_score": 0.06, "fakeout_risk": 0.22, "spread_bps": 9.0, "expected_slippage_pct": 0.0006, "latency_ms": 55.0, "market_ts": now_ts, "entry": 67_250.0, "side": "LONG", "rr": 2.15, "timeframe": "5m", "tick_size": 0.1}]

    safe_scanner_requested = str(os.getenv("ALPHAFORGE_RUNTIME_SAFE_SCANNER", "0")).strip().lower() in {"1", "true", "yes", "on"}
    use_safe_scanner = safe_scanner_requested
    scanner_source = "SAFE_PLACEHOLDER" if use_safe_scanner else "EXCHANGE_PUBLIC_MARKET_DATA"
    config.require_mtf_alignment = mode == ExecutionMode.PAPER and not use_safe_scanner

    async def _runtime_market_scanner() -> list[dict[str, Any]]:
        if mode == ExecutionMode.BACKTEST and use_safe_scanner:
            logger.warning("market_data_source=SYNTHETIC_SMOKE_TEST backtest_runtime_scanner=_safe_market_scanner smoke_test_only=true")
            return await _safe_market_scanner()
        if use_safe_scanner:
            return await _safe_market_scanner()
        return await scan_exchange_markets(cfg)

    async def _selected_candidate_enricher(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if use_safe_scanner:
            return candidates
        return await enrich_selected_market_geometry(candidates, cfg)

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
                decision_id=payload.get("reject_decision_id"),
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
    if mode in {ExecutionMode.PAPER, ExecutionMode.LIVE, ExecutionMode.LIVE_PRECHECK} and cfg.runtime.enable_binance_readonly_reconciliation:
        api_key = str(os.getenv("BINANCE_API_KEY", "")).strip()
        api_secret = str(os.getenv("BINANCE_API_SECRET", "")).strip()
        if bool(api_key) ^ bool(api_secret):
            raise RuntimeError("LIVE mode blocked: Binance reconciliation credentials are partial")
        if not api_key or not api_secret:
            if mode == ExecutionMode.PAPER:
                api_key = api_secret = ""
            else:
                raise RuntimeError("LIVE mode blocked: Binance reconciliation credentials are missing")
        if api_key and api_secret:
            reconciliation = load_reconciliation_settings()
            live_reconciliation_provider = BinanceReadonlyReconciliationProvider(
                config=BinanceReadonlyReconciliationConfig(
                    base_url=reconciliation.base_url,
                    api_key=reconciliation.api_key,
                    api_secret=reconciliation.api_secret,
                    recv_window_ms=reconciliation.recv_window_ms,
                    request_timeout_sec=reconciliation.timeout_sec,
                    trade_lookback_ms=reconciliation.trade_lookback_ms,
                    position_epsilon=Decimal(reconciliation.position_epsilon),
                    max_fill_symbols=reconciliation.max_fill_symbols,
                )
            )

    state_direction_shadow_enabled = (
        mode is ExecutionMode.PAPER
        and str(os.getenv(
            "ALPHAFORGE_ENABLE_STATE_DIRECTION_SHADOW_EVALUATION", "false"
        )).strip().lower() in {"1", "true", "yes", "on"}
    )
    state_direction_shadow_store = StateDirectionShadowStore(os.getenv(
        "ALPHAFORGE_ADAPTIVE_SHADOW_DB_PATH", "data/runtime/alphaforge_adaptive_shadow.db"
    )) if state_direction_shadow_enabled else None
    orchestrator = RuntimeOrchestrator(
        config=config,
        ai_brain=brain,
        market_scanner=_runtime_market_scanner,
        selected_candidate_enricher=_selected_candidate_enricher,
        mtf_context_provider=BinanceMTFProvider(base_url=cfg.exchange.binance.market_data_base_url, timeout_sec=cfg.exchange.timeout_sec, regime_direction_threshold=config.regime_direction_threshold, setup_direction_threshold=config.setup_direction_threshold, execution_direction_threshold=config.execution_direction_threshold, guided_signal_generation_enabled=config.mtf_guided_signal_generation_enabled) if mode == ExecutionMode.PAPER and not use_safe_scanner else None,
        scanner_source=scanner_source,
        live_reconciliation_provider=live_reconciliation_provider,
        on_lifecycle_event=_persist_lifecycle,
        on_reject_persist=_persist_reject,
        state_direction_shadow_enabled=state_direction_shadow_enabled,
        state_direction_shadow_store=state_direction_shadow_store,
        persistence_engine=engine,
        control_store=RuntimeControlStore(engine),
    )
    orchestrator.metrics.persistence_enabled = persistence_enabled
    return orchestrator


async def main() -> None:
    from alphaforge.env_contract import bootstrap_environment
    bootstrap_environment()
    cfg = load_config_from_env()
    mode = execution_mode_from_env(cfg.runtime.execution_mode)
    if mode == ExecutionMode.PAPER and not cfg.runtime.paper_enabled:
        raise RuntimeError("PAPER mode blocked: ALPHAFORGE_ENABLE_PAPER_TRADING is false")
    if mode in {ExecutionMode.LIVE, ExecutionMode.LIVE_PRECHECK} and not cfg.runtime.live_enabled:
        raise RuntimeError("LIVE mode blocked: ALPHAFORGE_ENABLE_LIVE_TRADING is false")
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
