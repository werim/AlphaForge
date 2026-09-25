"""Isolated, PAPER-only fault qualification for AlphaForge runtime invariants."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import os
import socket
import resource
import sys
import sqlite3
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from math import ceil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib import error

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from alphaforge.ai_brain import AIBrain

from alphaforge.binance_reconciliation_provider import (
    BinanceReadonlyReconciliationConfig,
    BinanceReadonlyReconciliationProvider,
)
from alphaforge.burnin import canonical_decision_sql, utc_now
from alphaforge.burnin_campaign import (
    BurnInCampaignRunner,
    ProviderFailure,
    create_campaign,
    event,
    export_campaign_bundle,
    get_campaign,
    qualify_campaign,
    start_or_resume_campaign,
    terminalize_active_campaign_run,
    update_campaign_heartbeat,
)
from alphaforge.burnin_ops import bootstrap_ops_schema, health_payload
from alphaforge.burnin_resolver import persist_pending_position
from alphaforge.config import AlphaForgeConfig
from alphaforge.exchange_market_scanner import scan_exchange_markets
from alphaforge.persistence import init_db
from alphaforge.provider_failures import (
    PERMANENT_AUTH_OR_PROTOCOL,
    TRANSIENT_TRANSPORT,
    UNKNOWN,
)
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator
from alphaforge.runtime_heartbeat import save_runtime_heartbeat
from alphaforge.runtime_state import persist_reconciliation_cycle


FAULT_NAMES = (
    "transient_dns_gaierror", "url_error", "timeout", "connection_reset",
    "http_429", "http_5xx", "permanent_authentication", "malformed_provider_response",
    "transient_grace_expiry", "sqlite_write_contention", "stale_heartbeat", "delayed_resolver_data",
    "reconciliation_unavailable", "worker_restart_recovery", "duplicate_persistence_replay",
)


@dataclass(slots=True)
class ScenarioResult:
    name: str
    injected_at: str
    recovered_at: str | None
    classification: str
    expected_behavior: list[str]
    observed_behavior: dict[str, Any]
    invariant_checks: dict[str, bool]
    db_evidence: list[str]
    verdict: str
    recovery_latency_seconds: float | None = None
    error: str | None = None


@dataclass(slots=True)
class HarnessContext:
    campaign_id: str
    burnin_run_id: str
    runtime: RuntimeOrchestrator
    provider_state: dict[str, Any]
    lifecycle_events: list[dict[str, Any]] = field(default_factory=list)


class _NoopBrain:
    session = None


async def _empty_scanner() -> list[Any]:
    return []


class AutonomousQualificationHarness:
    """Run deterministic faults against one newly-created qualification DB."""

    def __init__(self, *, mode: str = "FAST", output_root: str | Path | None = None,
                 soak_hours: float = 6.0, sleep: Callable[[float], None] = time.sleep,
                 market_data_source: str = "SYNTHETIC",
                 soak_probe_interval_seconds: float = 30.0) -> None:
        self.mode = mode.upper()
        if self.mode not in {"FAST", "SOAK"}:
            raise ValueError("mode must be FAST or SOAK")
        if self.mode == "SOAK" and not 6.0 <= soak_hours <= 24.0:
            raise ValueError("SOAK duration must be between 6 and 24 hours")
        self.soak_hours = float(soak_hours)
        self._sleep = sleep
        self.market_data_source = market_data_source.upper()
        if self.market_data_source not in {"PUBLIC", "SYNTHETIC"}:
            raise ValueError("market_data_source must be PUBLIC or SYNTHETIC")
        self.soak_probe_interval_seconds = max(1.0, float(soak_probe_interval_seconds))
        self._soak_observed = {"probe_count": 0, "market_data_rows": 0,
                               "empty_market_data_probes": 0, "reconciliation_clean_probes": 0,
                               "resolver_ok_probes": 0, "heartbeat_fresh_probes": 0,
                               "lineage_ok_probes": 0, "worker_alive_probes": 0,
                               "safe_status_probes": 0}
        self._market_data_probes: list[dict[str, Any]] = []
        self._market_data_empty_streak = 0
        self._market_data_max_empty_streak = 0
        self._market_data_outage_started: float | None = None
        self._market_data_recoveries: list[dict[str, Any]] = []
        self._soak_samples: list[dict[str, Any]] = []
        self._soak_monitor_errors: list[str] = []
        self._run_started_monotonic: float | None = None
        self._enforce_wall_clock = sleep is time.sleep
        parent = Path(output_root).expanduser().resolve() if output_root else None
        if parent is not None:
            parent.mkdir(parents=True, exist_ok=True)
        self.run_dir = Path(tempfile.mkdtemp(prefix="alphaforge-qualification-", dir=parent))
        self.artifact_dir = self.run_dir / "artifacts"
        self.artifact_dir.mkdir()
        self._soak_samples_path = self.artifact_dir / "soak-resource-samples.jsonl"
        self.db_path = self.run_dir / "qualification.sqlite3"
        if self.db_path.exists():
            raise RuntimeError("qualification database must be new")
        self.engine = init_db(f"sqlite+pysqlite:///{self.db_path}")
        self.run_id = "qualification:" + uuid.uuid4().hex
        self._soak_decision_signal_prefix = f"qualification:{self.run_id}:soak:"
        self._soak_decision_probe_done = False
        self._soak_decision_probe_error: str | None = None
        self.results: list[ScenarioResult] = []
        self.worker_lifecycle: list[dict[str, Any]] = []
        self._active_contexts: dict[str, HarnessContext] = {}
        self.started_at = utc_now()
        self._closed = False

    def _record_event(self, campaign_id: str, event_type: str, details: Mapping[str, Any]) -> int:
        with self.engine.begin() as conn:
            event(conn, campaign_id, event_type, details={"qualification_run_id": self.run_id, **dict(details)})
            return int(conn.execute(text(
                "SELECT id FROM burnin_campaign_events WHERE campaign_id=:cid ORDER BY id DESC LIMIT 1"
            ), {"cid": campaign_id}).scalar_one())

    def _new_context(self, scenario: str, provider: Any | None = None, *, grace: float = 300.0,
                     qualification_targets: bool = False) -> HarnessContext:
        release_id = f"AQH-{self.run_id[-8:]}-{scenario}"
        with self.engine.begin() as conn:
            campaign = create_campaign(
                conn, release_id=release_id, duration_days=1 / 86400,
                symbols=["BTCUSDT"], intervals=["1m"],
                target_decisions=500 if qualification_targets else 0,
                target_closed_trades=30 if qualification_targets else 0,
                target_reject_forward_outcomes=50 if qualification_targets else 0,
                source_provenance={"provider": "BINANCE_READ_ONLY_QUALIFICATION",
                                   "exchange": "BINANCE", "order_submission": "DISABLED",
                                   "qualification_run_id": self.run_id},
            )
            run = start_or_resume_campaign(conn, campaign.campaign_id)
        state = {"fault": None}
        lifecycle_events: list[dict[str, Any]] = []
        brain: Any = _NoopBrain()
        scanner_source = "QUALIFICATION_FAULT_HARNESS"
        if qualification_targets:
            SessionLocal = sessionmaker(
                bind=self.engine, expire_on_commit=False, future=True
            )
            # The SOAK decision probe is deliberately rejection-biased: a score
            # can never reach 2.0 because AIBrain scores are clipped to [0, 1].
            # This exercises canonical decision persistence without opening a
            # PAPER position or creating any exchange mutation.
            brain = AIBrain(session_factory=SessionLocal, min_accept_score=2.0)
            scanner_source = "QUALIFICATION_DETERMINISTIC_DECISION_PROBE"
        runtime = RuntimeOrchestrator(
            config=RuntimeConfig(execution_mode=ExecutionMode.PAPER,
                                 provider_transient_outage_grace_seconds=grace,
                                 live_trading_enabled=False, allow_live_orders=False),
            ai_brain=brain, market_scanner=_empty_scanner,
            scanner_source=scanner_source,
            persistence_engine=self.engine, live_reconciliation_provider=provider,
            on_lifecycle_event=lambda payload: lifecycle_events.append(dict(payload)),
        )
        runtime.metrics.persistence_enabled = True
        runtime._campaign_id = campaign.campaign_id
        runtime._burnin_run_id = run["burnin_run_id"]
        started = {"scenario": scenario, "runtime_instance_id": runtime.runtime_instance_id,
                   "reason": "QUALIFICATION_SCENARIO_STARTED"}
        self._record_event(campaign.campaign_id, "QUALIFICATION_WORKER_STARTED", started)
        self.worker_lifecycle.append({**started, "campaign_id": campaign.campaign_id,
                                      "burnin_run_id": run["burnin_run_id"], "at": utc_now()})
        context = HarnessContext(campaign.campaign_id, run["burnin_run_id"], runtime, state, lifecycle_events)
        self._active_contexts[runtime.runtime_instance_id] = context
        return context

    @staticmethod
    def _http_error(status: int) -> error.HTTPError:
        body = io.BytesIO(json.dumps({"code": -1000, "msg": f"qualification HTTP {status}"}).encode())
        return error.HTTPError("https://qualification.invalid", status, "injected", {}, body)

    def _provider(self, state: dict[str, Any]) -> BinanceReadonlyReconciliationProvider:
        def get_json(_url: str, _headers: Mapping[str, str], _timeout: float) -> Any:
            fault = state.get("fault")
            if fault == "gaierror":
                raise socket.gaierror(-3, "qualification dns failure")
            if fault == "urlerror":
                raise error.URLError(socket.gaierror(-3, "qualification URL failure"))
            if fault == "timeout":
                raise TimeoutError("qualification timeout")
            if fault == "connection_reset":
                raise ConnectionResetError("qualification reset")
            if fault == "http_429":
                raise self._http_error(429)
            if fault == "http_5xx":
                raise self._http_error(503)
            if fault == "auth":
                raise self._http_error(401)
            if fault == "malformed":
                return {"unexpected": "object"}
            return []
        return BinanceReadonlyReconciliationProvider(
            config=BinanceReadonlyReconciliationConfig(
                base_url="https://qualification.invalid", api_key="qualification-read-only",
                api_secret="qualification-secret", request_timeout_sec=0.05,
            ), http_get_json=get_json,
        )

    def _terminalize(self, ctx: HarnessContext, reason: str = "QUALIFICATION_SCENARIO_COMPLETE") -> None:
        with self.engine.begin() as conn:
            campaign = get_campaign(conn, ctx.campaign_id) or {}
            if campaign.get("campaign_status") in {"STARTING", "RUNNING"}:
                terminalize_active_campaign_run(
                    conn, ctx.campaign_id, run_status="COMPLETED", campaign_status="COMPLETED",
                    reason=reason, event_type="QUALIFICATION_WORKER_EXITED",
                    details={"runtime_instance_id": ctx.runtime.runtime_instance_id},
                )
            else:
                event(conn, ctx.campaign_id, "QUALIFICATION_WORKER_EXITED",
                      burnin_run_id=ctx.burnin_run_id,
                      details={"reason": reason, "runtime_instance_id": ctx.runtime.runtime_instance_id})
        ctx.runtime.shutdown()
        self._active_contexts.pop(ctx.runtime.runtime_instance_id, None)
        self.worker_lifecycle.append({"scenario": reason, "campaign_id": ctx.campaign_id,
                                      "burnin_run_id": ctx.burnin_run_id,
                                      "runtime_instance_id": ctx.runtime.runtime_instance_id,
                                      "reason": reason, "at": utc_now()})

    def _lineage(self, campaign_id: str) -> tuple[bool, dict[str, Any]]:
        with self.engine.connect() as conn:
            campaign = conn.execute(text(
                "SELECT campaign_status,active_run_id,last_error FROM burnin_campaigns WHERE campaign_id=:cid"
            ), {"cid": campaign_id}).mappings().one()
            run = conn.execute(text(
                "SELECT status FROM burnin_runs WHERE burnin_run_id=:bid"
            ), {"bid": campaign["active_run_id"]}).scalar_one()
            mapping = conn.execute(text(
                "SELECT status FROM burnin_campaign_runs WHERE campaign_id=:cid AND burnin_run_id=:bid"
            ), {"cid": campaign_id, "bid": campaign["active_run_id"]}).scalar_one()
        terminal = campaign["campaign_status"] in {"PAUSED", "COMPLETED", "FAILED", "RECOVERY_REQUIRED"}
        compatible = run == mapping and (not terminal or run != "RUNNING")
        return compatible, {"campaign": campaign["campaign_status"], "run": run,
                            "campaign_run": mapping, "last_error": campaign["last_error"]}

    def _evidence(self, ctx: HarnessContext) -> list[str]:
        refs: list[str] = []
        with self.engine.connect() as conn:
            for table, where, params in (
                ("burnin_campaign_events", "campaign_id=:cid", {"cid": ctx.campaign_id}),
                ("runtime_state_snapshots", "campaign_id=:cid", {"cid": ctx.campaign_id}),
                ("exchange_reconciliation_events", "instance_id=:rid", {"rid": ctx.runtime.runtime_instance_id}),
                ("runtime_recovery_events", "instance_id=:rid", {"rid": ctx.runtime.runtime_instance_id}),
            ):
                rows = conn.execute(text(f"SELECT id FROM {table} WHERE {where} ORDER BY id"),
                                    params).scalars().all()
                if rows:
                    refs.append(f"sqlite:{self.db_path}#{table}:ids={rows[0]}..{rows[-1]}")
        return refs

    def _finish_result(self, ctx: HarnessContext, *, name: str, injected_at: str,
                       recovered_at: str | None, classification: str,
                       expected: list[str], observed: dict[str, Any], checks: dict[str, bool],
                       started_monotonic: float, error_message: str | None = None) -> ScenarioResult:
        lineage_ok, lineage = self._lineage(ctx.campaign_id)
        checks["campaign_run_lineage_consistent"] = lineage_ok
        observed["lineage"] = lineage
        verdict = "PASS" if checks and all(checks.values()) and error_message is None else "FAIL"
        result = ScenarioResult(
            name=name, injected_at=injected_at, recovered_at=recovered_at,
            classification=classification, expected_behavior=expected,
            observed_behavior=observed, invariant_checks=checks,
            db_evidence=self._evidence(ctx), verdict=verdict,
            recovery_latency_seconds=(None if recovered_at is None else round(time.monotonic() - started_monotonic, 6)),
            error=error_message,
        )
        self.results.append(result)
        return result

    def _record_scenario_exception(self, name: str, classification: str, exc: Exception) -> None:
        """Keep product-path failures reportable instead of turning them into harness infrastructure errors."""
        self.results.append(ScenarioResult(
            name=name, injected_at=utc_now(), recovered_at=None, classification=classification,
            expected_behavior=["scenario completes and all invariants are evaluated"],
            observed_behavior={"exception": f"{exc.__class__.__name__}:{exc}"},
            invariant_checks={"scenario_completed": False}, db_evidence=[], verdict="FAIL",
            error=f"{exc.__class__.__name__}:{exc}",
        ))

    def _run_provider_fault(self, name: str, fault: str, classification: str,
                            *, permanent: bool = False) -> ScenarioResult:
        state = {"fault": fault}
        ctx = self._new_context(name)
        ctx.provider_state = state
        ctx.runtime.live_reconciliation_provider = self._provider(state)
        injected_at = utc_now(); started = time.monotonic()
        self._record_event(ctx.campaign_id, "QUALIFICATION_FAULT_INJECTED",
                           {"scenario": name, "fault": fault, "classification": classification})
        try:
            asyncio.run(ctx.runtime._run_reconciliation_once())
            snapshot = ctx.runtime._build_runtime_state_snapshot(status="OPERATING")
            before_executions = ctx.runtime.metrics.executions
            ctx.runtime._current_signal_id_by_symbol["BTCUSDT"] = f"{name}:inflight"
            ctx.runtime._last_lifecycle_state_by_signal[f"{name}:inflight"] = "ENTRY_TRIGGERED"
            executed = asyncio.run(ctx.runtime._execute(
                "BTCUSDT", {"signal_id": f"{name}:inflight"}, {"entry": 100.0}))
            blocked = executed is False and ctx.runtime.metrics.executions == before_executions
            checks = {
                "unknown_blocks_execution": blocked,
                "unsafe_inflight_cancelled": bool(ctx.lifecycle_events and ctx.lifecycle_events[-1]["lifecycle_event_type"] == "CANCELLED"),
                "unavailable_not_operating": snapshot.exchange_read_only_status != "UNAVAILABLE" or snapshot.runtime_status != "OPERATING",
                "classification_matches": ctx.runtime._provider_failure_class == classification,
            }
            if permanent:
                lineage_ok, lineage = self._lineage(ctx.campaign_id)
                checks.update({"immediate_terminal_escalation": ctx.runtime._stop_event.is_set(),
                               "terminal_state_atomic": lineage_ok and lineage["campaign"] == "PAUSED"})
                observed = {"runtime_status": snapshot.runtime_status,
                            "reconciliation_status": snapshot.reconciliation_status,
                            "exchange_read_only_status": snapshot.exchange_read_only_status,
                            "worker_stopped": ctx.runtime._stop_event.is_set()}
                self._terminalize(ctx, "QUALIFICATION_EXPECTED_TERMINAL_EXIT")
                return self._finish_result(ctx, name=name, injected_at=injected_at,
                    recovered_at=None, classification=classification,
                    expected=["execution blocked", "immediate fail-closed terminal transition"],
                    observed=observed, checks=checks, started_monotonic=started)
            checks["worker_alive_during_grace"] = not ctx.runtime._stop_event.is_set()
            state["fault"] = None
            asyncio.run(ctx.runtime._run_reconciliation_once())
            recovered_at = utc_now()
            with self.engine.connect() as conn:
                recovery_count = int(conn.execute(text(
                    "SELECT COUNT(*) FROM runtime_recovery_events WHERE instance_id=:rid AND status='RECOVERED'"
                ), {"rid": ctx.runtime.runtime_instance_id}).scalar_one())
                failure_events = int(conn.execute(text(
                    "SELECT COUNT(*) FROM exchange_reconciliation_events WHERE instance_id=:rid AND status='EXCHANGE_STATE_UNKNOWN'"
                ), {"rid": ctx.runtime.runtime_instance_id}).scalar_one())
            checks.update({
                "clean_commit_required": ctx.runtime._reconciliation_status == "CLEAN" and not ctx.runtime._execution_reconciliation_blocked(),
                "recovery_explicitly_persisted": recovery_count == 1,
                "failure_attempts_persisted": failure_events == 1,
            })
            observed = {"runtime_status": ctx.runtime._runtime_status,
                        "reconciliation_status": ctx.runtime._reconciliation_status,
                        "worker_stopped": ctx.runtime._stop_event.is_set(),
                        "failure_event_count": failure_events, "recovery_event_count": recovery_count}
            self._record_event(ctx.campaign_id, "QUALIFICATION_FAULT_RECOVERED",
                               {"scenario": name, "classification": classification})
            self._terminalize(ctx)
            return self._finish_result(ctx, name=name, injected_at=injected_at,
                recovered_at=recovered_at, classification=classification,
                expected=["execution blocked", "worker alive during grace", "committed CLEAN recovery"],
                observed=observed, checks=checks, started_monotonic=started)
        except Exception as exc:
            with contextlib.suppress(Exception): self._terminalize(ctx, "QUALIFICATION_SCENARIO_EXCEPTION")
            return self._finish_result(ctx, name=name, injected_at=injected_at,
                recovered_at=None, classification=classification, expected=["scenario completes"],
                observed={}, checks={"scenario_completed": False}, started_monotonic=started,
                error_message=f"{exc.__class__.__name__}:{exc}")

    def _run_reconciliation_unavailable(self) -> ScenarioResult:
        name = "reconciliation_unavailable"; ctx = self._new_context(name)
        injected_at = utc_now(); started = time.monotonic()
        self._record_event(ctx.campaign_id, "QUALIFICATION_FAULT_INJECTED", {"scenario": name})
        asyncio.run(ctx.runtime._run_reconciliation_once())
        unavailable = ctx.runtime._build_runtime_state_snapshot(status="OPERATING")
        ctx.runtime.live_reconciliation_provider = self._provider({"fault": None})
        asyncio.run(ctx.runtime._run_reconciliation_once())
        with self.engine.connect() as conn:
            recovered = int(conn.execute(text(
                "SELECT COUNT(*) FROM runtime_recovery_events WHERE instance_id=:rid AND status='RECOVERED'"
            ), {"rid": ctx.runtime.runtime_instance_id}).scalar_one())
        checks = {"unavailable_blocks": unavailable.fail_closed_reason == "EXCHANGE_RECONCILIATION_UNAVAILABLE",
                  "unavailable_not_operating": unavailable.runtime_status == "RECOVERY_REQUIRED",
                  "worker_alive": not ctx.runtime._stop_event.is_set(),
                  "clean_recovery_committed": recovered == 1 and ctx.runtime._reconciliation_status == "CLEAN"}
        observed = {"unavailable_snapshot": unavailable.to_record(), "recovery_events": recovered}
        recovered_at = utc_now(); self._terminalize(ctx)
        return self._finish_result(ctx, name=name, injected_at=injected_at, recovered_at=recovered_at,
            classification=UNKNOWN, expected=["fail closed", "remain alive", "recover after CLEAN commit"],
            observed=observed, checks=checks, started_monotonic=started)

    def _run_sqlite_contention(self) -> ScenarioResult:
        name = "sqlite_write_contention"; state = {"fault": "gaierror"}
        ctx = self._new_context(name); ctx.runtime.live_reconciliation_provider = self._provider(state)
        injected_at = utc_now(); started = time.monotonic()
        self._record_event(ctx.campaign_id, "QUALIFICATION_FAULT_INJECTED", {"scenario": name})
        asyncio.run(ctx.runtime._run_reconciliation_once())
        state["fault"] = None
        blocker = sqlite3.connect(self.db_path, timeout=0.01, check_same_thread=False)
        blocker.execute("BEGIN IMMEDIATE")
        releaser = threading.Thread(target=lambda: (time.sleep(0.9), blocker.rollback(), blocker.close()), daemon=True)
        releaser.start()
        asyncio.run(ctx.runtime._run_reconciliation_once())
        releaser.join(timeout=2)
        blocked_after_failure = ctx.runtime._execution_reconciliation_blocked()
        asyncio.run(ctx.runtime._run_reconciliation_once())
        with self.engine.connect() as conn:
            statuses = list(conn.execute(text(
                "SELECT status FROM exchange_reconciliation_events WHERE instance_id=:rid ORDER BY id"
            ), {"rid": ctx.runtime.runtime_instance_id}).scalars())
            recovery = int(conn.execute(text(
                "SELECT COUNT(*) FROM runtime_recovery_events WHERE instance_id=:rid AND status='RECOVERED'"
            ), {"rid": ctx.runtime.runtime_instance_id}).scalar_one())
        checks = {"contention_remains_fail_closed": blocked_after_failure,
                  "failed_write_audit_replayed": "PERSISTENCE_FAILED" in statuses,
                  "clean_recovery_committed": statuses[-1:] == ["CLEAN"] and recovery == 1,
                  "no_duplicate_failure_identity": len(statuses) == len(set(
                      self._reconciliation_cycle_ids(ctx.runtime.runtime_instance_id))) }
        observed = {"reconciliation_event_statuses": statuses, "recovery_events": recovery}
        recovered_at = utc_now(); self._terminalize(ctx)
        return self._finish_result(ctx, name=name, injected_at=injected_at, recovered_at=recovered_at,
            classification=TRANSIENT_TRANSPORT,
            expected=["stay fail closed during lock", "persist deferred failure", "recover after CLEAN commit"],
            observed=observed, checks=checks, started_monotonic=started)

    def _run_transient_grace_expiry(self) -> ScenarioResult:
        name = "transient_grace_expiry"; state = {"fault": "gaierror"}
        ctx = self._new_context(name, grace=300); ctx.runtime.live_reconciliation_provider = self._provider(state)
        injected_at = utc_now(); started = time.monotonic()
        self._record_event(ctx.campaign_id, "QUALIFICATION_FAULT_INJECTED",
                           {"scenario": name, "classification": TRANSIENT_TRANSPORT})
        asyncio.run(ctx.runtime._run_reconciliation_once())
        ctx.runtime._transient_provider_outage_started_monotonic -= 301
        asyncio.run(ctx.runtime._run_reconciliation_once())
        lineage_ok, lineage = self._lineage(ctx.campaign_id)
        checks = {"worker_stops_after_grace": ctx.runtime._stop_event.is_set(),
                  "outage_reason_persisted": lineage["last_error"] == "PROVIDER_TRANSIENT_OUTAGE_GRACE_EXPIRED",
                  "terminal_state_atomic": lineage_ok and lineage["campaign"] == lineage["run"] == lineage["campaign_run"] == "PAUSED"}
        observed = {"worker_stopped": ctx.runtime._stop_event.is_set(), "lineage_at_expiry": lineage}
        self._terminalize(ctx, "QUALIFICATION_EXPECTED_GRACE_EXPIRY_EXIT")
        return self._finish_result(ctx, name=name, injected_at=injected_at, recovered_at=None,
            classification=TRANSIENT_TRANSPORT,
            expected=["execution remains blocked", "atomic pause after elapsed grace"],
            observed=observed, checks=checks, started_monotonic=started)

    def _reconciliation_cycle_ids(self, instance_id: str) -> list[str]:
        with self.engine.connect() as conn:
            return [str(value) for value in conn.execute(text(
                "SELECT cycle_id FROM exchange_reconciliation_events WHERE instance_id=:rid ORDER BY id"
            ), {"rid": instance_id}).scalars()]

    def _run_stale_heartbeat(self) -> ScenarioResult:
        name = "stale_heartbeat"; ctx = self._new_context(name, self._provider({"fault": None}))
        injected_at = utc_now(); started = time.monotonic()
        self._record_event(ctx.campaign_id, "QUALIFICATION_FAULT_INJECTED", {"scenario": name})
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        save_runtime_heartbeat(self.engine, runtime_instance_id=ctx.runtime.runtime_instance_id,
            execution_mode="PAPER", scanner_source="QUALIFICATION_SYNTHETIC_READ_ONLY",
            runtime_state="RECOVERY_REQUIRED", heartbeat_ts=old,
            payload={"rejects_persisted": 0})
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT heartbeat_ts,runtime_state FROM runtime_heartbeats WHERE runtime_instance_id=:rid ORDER BY id DESC LIMIT 1"
            ), {"rid": ctx.runtime.runtime_instance_id}).mappings().one()
        checks = {"stale_detected": row["heartbeat_ts"] == old,
                  "stale_never_operating": row["runtime_state"] == "RECOVERY_REQUIRED"}
        self._terminalize(ctx, "QUALIFICATION_STALE_HEARTBEAT_OBSERVED")
        return self._finish_result(ctx, name=name, injected_at=injected_at, recovered_at=None,
            classification=UNKNOWN, expected=["stale heartbeat is explicit and not operating"],
            observed=dict(row), checks=checks, started_monotonic=started)

    def _run_delayed_resolver(self) -> ScenarioResult:
        name = "delayed_resolver_data"; ctx = self._new_context(name)
        injected_at = utc_now(); started = time.monotonic()
        self._record_event(ctx.campaign_id, "QUALIFICATION_FAULT_INJECTED",
                           {"scenario": name, "classification": TRANSIENT_TRANSPORT})
        with self.engine.begin() as conn:
            persist_pending_position(conn, trade_id=f"{name}:position", campaign_id=ctx.campaign_id,
                burnin_run_id=ctx.burnin_run_id, signal_id=f"{name}:signal", symbol="BTCUSDT",
                side="LONG", entry_time="2026-01-01T00:00:00Z", planned_entry=100,
                simulated_fill=100, stop=90, target=120, quantity=1, notional=100,
                entry_spread=0, entry_slippage=0, entry_fee=0, regime="TREND", source_provenance={})
        attempts = {"count": 0}
        def candles(*_args: Any) -> list[Any]:
            attempts["count"] += 1; time.sleep(0.02)
            if attempts["count"] == 1:
                raise ProviderFailure("PROVIDER_FAILURE:delayed") from TimeoutError("delayed candle data")
            return []
        runner = BurnInCampaignRunner(self.engine, ctx.campaign_id, candles,
            provider_transient_outage_grace_seconds=300)
        first = runner.resolver_tick()
        ctx.runtime._mark_resolver_provider_failure()
        signal_id = f"{name}:blocked"
        ctx.runtime._current_signal_id_by_symbol["ETHUSDT"] = signal_id
        ctx.runtime._last_lifecycle_state_by_signal[signal_id] = "ENTRY_TRIGGERED"
        blocked = asyncio.run(ctx.runtime._execute("ETHUSDT", {"signal_id": signal_id}, {"entry": 100})) is False
        second = runner.resolver_tick(); ctx.runtime._mark_resolver_provider_recovered()
        ctx.runtime.live_reconciliation_provider = self._provider({"fault": None})
        still_blocked = ctx.runtime._execution_reconciliation_blocked()
        asyncio.run(ctx.runtime._run_reconciliation_once())
        checks = {"delay_classified_transient": first.get("failure_class") == TRANSIENT_TRANSPORT,
                  "execution_blocked": blocked,
                  "unsafe_inflight_cancelled": bool(ctx.lifecycle_events and ctx.lifecycle_events[-1]["lifecycle_event_type"] == "CANCELLED"),
                  "resolver_retried": second.get("status") == "OK",
                  "resolver_success_not_sufficient": still_blocked,
                  "clean_commit_resumes": not ctx.runtime._execution_reconciliation_blocked()}
        observed = {"first_tick": first, "second_tick": second, "attempts": attempts["count"]}
        recovered_at = utc_now(); self._terminalize(ctx)
        return self._finish_result(ctx, name=name, injected_at=injected_at, recovered_at=recovered_at,
            classification=TRANSIENT_TRANSPORT,
            expected=["resolver remains alive", "execution blocked", "CLEAN reconciliation required"],
            observed=observed, checks=checks, started_monotonic=started)

    def _run_worker_restart(self) -> ScenarioResult:
        name = "worker_restart_recovery"; ctx = self._new_context(name)
        injected_at = utc_now(); started = time.monotonic()
        self._record_event(ctx.campaign_id, "QUALIFICATION_FAULT_INJECTED", {"scenario": name})
        with self.engine.begin() as conn:
            terminalize_active_campaign_run(conn, ctx.campaign_id,
                run_status="RECOVERY_REQUIRED", campaign_status="RECOVERY_REQUIRED",
                reason="QUALIFICATION_INJECTED_WORKER_EXIT", event_type="QUALIFICATION_WORKER_EXITED",
                details={"runtime_instance_id": ctx.runtime.runtime_instance_id})
            resumed = start_or_resume_campaign(conn, ctx.campaign_id, resume=True)
            event(conn, ctx.campaign_id, "QUALIFICATION_WORKER_RESTARTED",
                  burnin_run_id=resumed["burnin_run_id"], details={"reason": "QUALIFICATION_RECOVERY"})
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT continuation_sequence,status,burnin_run_id FROM burnin_campaign_runs WHERE campaign_id=:cid ORDER BY continuation_sequence"
            ), {"cid": ctx.campaign_id}).mappings().all()
            exits = int(conn.execute(text(
                "SELECT COUNT(*) FROM burnin_campaign_events WHERE campaign_id=:cid AND event_type='QUALIFICATION_WORKER_EXITED'"
            ), {"cid": ctx.campaign_id}).scalar_one())
        ctx.burnin_run_id = resumed["burnin_run_id"]
        checks = {"old_worker_exit_reason_persisted": exits == 1,
                  "new_continuation_created": len(rows) == 2 and rows[1]["continuation_sequence"] == 1,
                  "old_run_terminal": rows[0]["status"] == "RECOVERY_REQUIRED",
                  "new_run_running": rows[1]["status"] == "RUNNING"}
        observed = {"continuations": [dict(row) for row in rows], "worker_exit_events": exits}
        recovered_at = utc_now(); self._terminalize(ctx)
        return self._finish_result(ctx, name=name, injected_at=injected_at, recovered_at=recovered_at,
            classification=UNKNOWN, expected=["explicit old-worker exit", "new isolated continuation"],
            observed=observed, checks=checks, started_monotonic=started)

    def _run_duplicate_replay(self) -> ScenarioResult:
        name = "duplicate_persistence_replay"; ctx = self._new_context(name)
        injected_at = utc_now(); started = time.monotonic()
        self._record_event(ctx.campaign_id, "QUALIFICATION_FAULT_INJECTED", {"scenario": name})
        ctx.runtime._unknown_exchange_state = True
        ctx.runtime._reconciliation_status = "EXCHANGE_STATE_UNKNOWN"
        ctx.runtime._exchange_read_only_status = "UNAVAILABLE"
        ctx.runtime._fail_closed_reason = "EXCHANGE_STATE_UNKNOWN"
        snapshot = ctx.runtime._build_runtime_state_snapshot(status="RECOVERY_REQUIRED")
        cycle_id = f"qualification:replay:{uuid.uuid4().hex}"
        first = persist_reconciliation_cycle(self.engine, cycle_id=cycle_id, findings=[],
            snapshot=snapshot, diagnostics={"fault": name})
        second = persist_reconciliation_cycle(self.engine, cycle_id=cycle_id, findings=[],
            snapshot=snapshot, diagnostics={"fault": name})
        with self.engine.connect() as conn:
            count = int(conn.execute(text(
                "SELECT COUNT(*) FROM exchange_reconciliation_events WHERE cycle_id=:cycle"
            ), {"cycle": cycle_id}).scalar_one())
        checks = {"first_attempt_persisted": first, "replay_idempotent": second is False,
                  "single_failure_identity": count == 1}
        self._terminalize(ctx, "QUALIFICATION_REPLAY_VERIFIED")
        return self._finish_result(ctx, name=name, injected_at=injected_at, recovered_at=None,
            classification=UNKNOWN, expected=["duplicate persistence attempt is idempotent"],
            observed={"first": first, "second": second, "row_count": count, "cycle_id": cycle_id},
            checks=checks, started_monotonic=started)

    def _run_watchdog_reset_and_exports(self) -> tuple[dict[str, bool], dict[str, Any]]:
        ctx = self._new_context("watchdog_and_export_consistency")
        state = {"active": True}
        def candles(*_args: Any) -> list[Any]:
            if state["active"]:
                raise ProviderFailure("PROVIDER_FAILURE:URLError") from error.URLError("qualification")
            return []
        with self.engine.begin() as conn:
            persist_pending_position(conn, trade_id="qualification:watchdog", campaign_id=ctx.campaign_id,
                burnin_run_id=ctx.burnin_run_id, signal_id="qualification:watchdog", symbol="BTCUSDT",
                side="LONG", entry_time="2026-01-01T00:00:00Z", planned_entry=100,
                simulated_fill=100, stop=90, target=120, quantity=1, notional=100,
                entry_spread=0, entry_slippage=0, entry_fee=0, regime="TREND", source_provenance={})
        runner = BurnInCampaignRunner(self.engine, ctx.campaign_id, candles,
                                      provider_transient_outage_grace_seconds=300)
        for _ in range(3): runner.resolver_tick()
        state["active"] = False; runner.resolver_tick()
        state["active"] = True; runner.resolver_tick()
        conn = sqlite3.connect(self.db_path); conn.row_factory = sqlite3.Row
        bootstrap_ops_schema(conn)
        health = health_payload(conn, ctx.campaign_id, max_heartbeat_age=10**9,
                                max_open_positions=10**9)
        conn.close()
        state["active"] = False; runner.resolver_tick()
        qualification = qualify_campaign(self.engine, ctx.campaign_id)
        export = export_campaign_bundle(self.db_path, self.artifact_dir, ctx.campaign_id)
        root = Path(export["output_dir"])
        manifest = json.loads((root / "manifest.json").read_text())
        checksum_lines = (root / "checksums.sha256").read_text().splitlines()
        checksums_ok = all(hashlib.sha256((root / line.split("  ", 1)[1]).read_bytes()).hexdigest() == line.split("  ", 1)[0]
                           for line in checksum_lines if "  " in line)
        with self.engine.connect() as db:
            event_count = int(db.execute(text(
                "SELECT COUNT(*) FROM burnin_campaign_events WHERE campaign_id=:cid"
            ), {"cid": ctx.campaign_id}).scalar_one())
        checks = {"historical_failures_reset": health["provider_failure_count"] == 1,
                  "no_unrelated_provider_pause": "REPEATED_PROVIDER_FAILURES" not in health["unhealthy_reasons"],
                  "manifest_event_count_matches": manifest["row_counts"]["recovery_events.csv"] == event_count,
                  "export_checksums_valid": checksums_ok,
                  "qualification_readable": bool(qualification.get("qualification_id"))}
        observed = {"health": health, "qualification": qualification,
                    "manifest": str(root / "manifest.json"), "event_count": event_count}
        self._terminalize(ctx)
        return checks, observed

    def _run_reject_persistence(self) -> tuple[dict[str, bool], dict[str, Any]]:
        ctx = self._new_context("reject_persistence")
        payload = {"signal_id": "qualification:reject", "symbol": "BTCUSDT", "side": "LONG",
            "timeframe": "1m", "entry": 100.0, "sl": 99.0, "tp": 103.0,
            "reason": "LOW_CONFIDENCE", "regime": "TREND", "setup_type": "BREAKOUT",
            "decision_timestamp": utc_now(), "source_exchange": "binance",
            "execution_ctx": {"spread_pct": .001, "expected_slippage_pct": .001,
                              "fee_pct": .001, "funding_rate_pct": 0.0,
                              "market_data_latency_ms": 1, "liquidity_score": .9}}
        asyncio.run(ctx.runtime._persist_reject(payload))
        with self.engine.connect() as conn:
            rejected = int(conn.execute(text(f"""
                SELECT COUNT(*) FROM burnin_observations o
                WHERE burnin_run_id=:bid AND decision='REJECTED'
                  AND {canonical_decision_sql('o')}
            """), {"bid": ctx.burnin_run_id}).scalar_one())
            reviews = int(conn.execute(text(
                "SELECT COUNT(*) FROM rejected_signal_reviews WHERE signal_id='qualification:reject'"
            )).scalar_one())
        checks = {"rejected_equals_persisted": rejected == ctx.runtime.metrics.rejects_persisted == 1,
                  "review_persisted": reviews == 1}
        observed = {"rejected_decisions": rejected,
                    "rejects_persisted": ctx.runtime.metrics.rejects_persisted, "reviews": reviews}
        self._terminalize(ctx)
        return checks, observed

    def _normal_market_data(self) -> list[dict[str, Any]]:
        if self.market_data_source == "SYNTHETIC":
            return [{"symbol": "BTCUSDT", "source_exchange": "qualification_synthetic",
                     "market_ts": time.time(), "timeframe": "1m", "entry": 100.0,
                     "spread_pct": 0.001, "funding_rate_pct": 0.0,
                     "volume_24h_usdt": 100_000_000.0}]
        config = AlphaForgeConfig()
        config.exchange.hyperliquid.enabled = False
        return asyncio.run(scan_exchange_markets(config))

    def _record_market_data_probe(self, ctx: HarnessContext, rows: list[dict[str, Any]],
                                  scan_latency: float) -> None:
        """Audit public-feed availability while preserving scanner failure semantics."""
        now = time.monotonic()
        available = bool(rows)
        diagnostics = dict(getattr(rows, "diagnostics", {}) or {})
        status = "AVAILABLE" if available else str(
            diagnostics.get("status") or "UNAVAILABLE_UNKNOWN_CAUSE"
        )
        probe = {
            "at": utc_now(),
            "probe_index": len(self._market_data_probes),
            "row_count": len(rows),
            "status": status,
            "classification": UNKNOWN if not available else None,
            "latency_seconds": round(scan_latency, 6),
            "cause": diagnostics.get("cause"),
            "provider": diagnostics.get("provider"),
            "endpoint": diagnostics.get("endpoint"),
            "error_class": diagnostics.get("error_class"),
            "http_status": diagnostics.get("http_status"),
            "provider_diagnostics": diagnostics.get("providers"),
        }
        probe["db_event_id"] = self._record_event(ctx.campaign_id, "QUALIFICATION_MARKET_DATA_PROBE", probe)
        if available:
            if self._market_data_outage_started is not None:
                recovery = {"recovered_at": probe["at"],
                            "latency_seconds": round(now - self._market_data_outage_started, 3),
                            "empty_probe_count": self._market_data_empty_streak,
                            "probe_index": probe["probe_index"]}
                recovery["db_event_id"] = self._record_event(
                    ctx.campaign_id, "QUALIFICATION_MARKET_DATA_RECOVERED", recovery
                )
                self._market_data_recoveries.append(recovery)
            self._market_data_empty_streak = 0
            self._market_data_outage_started = None
        else:
            if self._market_data_outage_started is None:
                self._market_data_outage_started = now
            self._market_data_empty_streak += 1
            self._market_data_max_empty_streak = max(
                self._market_data_max_empty_streak, self._market_data_empty_streak
            )
        self._market_data_probes.append(probe)

    @staticmethod
    def _rss_high_water_bytes() -> int:
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return value if sys.platform == "darwin" else value * 1024

    def _soak_resource_sample(self, ctx: HarnessContext, *, scan_latency: float | None,
                              reconciliation_latency: float, resolver_status: str) -> dict[str, Any]:
        with self.engine.connect() as conn:
            heartbeat = conn.execute(text(
                "SELECT heartbeat_ts,runtime_state FROM runtime_heartbeats "
                "WHERE runtime_instance_id=:rid ORDER BY id DESC LIMIT 1"
            ), {"rid": ctx.runtime.runtime_instance_id}).mappings().one()
            pending_positions = int(conn.execute(text(
                "SELECT COUNT(*) FROM burnin_pending_position_outcomes "
                "WHERE campaign_id=:cid AND status='OPEN'"
            ), {"cid": ctx.campaign_id}).scalar_one())
            pending_rejects = int(conn.execute(text(
                "SELECT COUNT(*) FROM burnin_pending_reject_labels "
                "WHERE campaign_id=:cid AND status IN ('PENDING','READY','RESOLVING')"
            ), {"cid": ctx.campaign_id}).scalar_one())
            rejected = int(conn.execute(text(f"""
                SELECT COUNT(*) FROM burnin_observations o
                WHERE burnin_run_id=:bid AND decision='REJECTED'
                  AND {canonical_decision_sql('o')}
            """), {"bid": ctx.burnin_run_id}).scalar_one())
            sqlite_lock_exhaustions = int(conn.execute(text(
                "SELECT COUNT(*) FROM exchange_reconciliation_events "
                "WHERE status='PERSISTENCE_FAILED'"
            )).scalar_one())
        heartbeat_age = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(
            str(heartbeat["heartbeat_ts"]).replace("Z", "+00:00")
        )).total_seconds())
        lineage_ok, lineage = self._lineage(ctx.campaign_id)
        snapshot = ctx.runtime._build_runtime_state_snapshot(status="OPERATING")
        safe_status = not (
            snapshot.runtime_status == "OPERATING" and (
                snapshot.exchange_read_only_status == "UNAVAILABLE"
                or snapshot.reconciliation_status == "EXCHANGE_STATE_UNKNOWN"
                or snapshot.fail_closed_reason is not None
            )
        )
        db_bytes = sum(path.stat().st_size for path in self.run_dir.glob("qualification.sqlite3*"))
        artifact_bytes = sum(path.stat().st_size for path in self.artifact_dir.rglob("*") if path.is_file())
        sample = {
            "at": utc_now(), "elapsed_seconds": round(time.monotonic() - (self._run_started_monotonic or time.monotonic()), 3),
            "rss_high_water_bytes": self._rss_high_water_bytes(), "db_bytes": db_bytes,
            "artifact_bytes": artifact_bytes,
            "queue_depths": {
                "shadow": ctx.runtime.metrics.agent_shadow_queue_depth,
                "reconciliation_deferred": len(ctx.runtime._pending_reconciliation_persistence_failures),
            },
            "pending_resolver_backlog": pending_positions,
            "pending_reject_backlog": pending_rejects,
            "sqlite_lock_retry_exhaustions": sqlite_lock_exhaustions,
            "reconciliation_latency_seconds": round(reconciliation_latency, 6),
            "scan_latency_seconds": None if scan_latency is None else round(scan_latency, 6),
            "heartbeat_age_seconds": round(heartbeat_age, 3),
            "reconciliation_status": snapshot.reconciliation_status,
            "exchange_read_only_status": snapshot.exchange_read_only_status,
            "runtime_status": snapshot.runtime_status,
            "resolver_status": resolver_status,
            "paper_executions": ctx.runtime.metrics.executions,
            "lineage": lineage,
            "invariants": {
                "worker_alive": not ctx.runtime._stop_event.is_set(),
                "heartbeat_fresh": heartbeat_age <= 120.0,
                "reconciliation_clean": snapshot.reconciliation_status == "CLEAN",
                "resolver_healthy": resolver_status == "OK",
                "lineage_consistent": lineage_ok and lineage["campaign"] == lineage["run"] == lineage["campaign_run"] == "RUNNING",
                "reject_persistence_parity": rejected == ctx.runtime.metrics.rejects_persisted,
                "execution_safe_status": safe_status,
                "queue_bounded": max(ctx.runtime.metrics.agent_shadow_queue_depth,
                                     len(ctx.runtime._pending_reconciliation_persistence_failures)) <= 32,
            },
        }
        self._soak_samples.append(sample)
        with self._soak_samples_path.open("a") as handle:
            handle.write(json.dumps(sample, sort_keys=True, default=str) + "\n")
        return sample

    def _soak_resource_summary(self) -> dict[str, Any]:
        samples = self._soak_samples
        if not samples:
            return {"sample_count": 0, "growth_flags": ["NO_RESOURCE_SAMPLES"]}
        def trend(key: str) -> dict[str, int]:
            values = [int(item[key]) for item in samples]
            return {"first": values[0], "last": values[-1], "peak": max(values),
                    "growth": values[-1] - values[0]}
        rss = trend("rss_high_water_bytes")
        db = trend("db_bytes")
        artifacts = trend("artifact_bytes")
        queues = [max(item["queue_depths"].values()) for item in samples]
        backlog = [item["pending_resolver_backlog"] + item["pending_reject_backlog"] for item in samples]
        scans = [item["scan_latency_seconds"] for item in samples if item["scan_latency_seconds"] is not None]
        reconciliation = [item["reconciliation_latency_seconds"] for item in samples]
        last_hour = [item for item in samples if item["elapsed_seconds"] >= samples[-1]["elapsed_seconds"] - 3600]
        flags: list[str] = []
        # Audit rows are intentionally appended at every SOAK probe. A fixed
        # DB-size cap would fail healthy long runs for doing the right thing.
        db_growth_budget = max(64 * 1024 * 1024, 160 * 1024 * len(samples))
        if rss["growth"] > 128 * 1024 * 1024:
            flags.append("RSS_GROWTH_OVER_128_MIB")
        if db["growth"] > db_growth_budget:
            flags.append("DB_GROWTH_ABOVE_AUDIT_BUDGET")
        if artifacts["growth"] > 64 * 1024 * 1024:
            flags.append("ARTIFACT_GROWTH_OVER_64_MIB")
        if len(last_hour) >= 12 and all(
            later["rss_high_water_bytes"] > earlier["rss_high_water_bytes"]
            for earlier, later in zip(last_hour[-12:], last_hour[-11:])
        ) and last_hour[-1]["rss_high_water_bytes"] - last_hour[-12]["rss_high_water_bytes"] > 8 * 1024 * 1024:
            flags.append("SUSTAINED_MONOTONIC_RSS_GROWTH")
        if max(queues) > 32 or max(backlog) > 32:
            flags.append("UNBOUNDED_QUEUE_OR_BACKLOG")
        return {"sample_count": len(samples), "sample_artifact": str(self._soak_samples_path),
                "rss_high_water_bytes": rss, "db_bytes": db, "artifact_bytes": artifacts,
                "db_growth_budget_bytes": db_growth_budget,
                "queue_peak": max(queues), "backlog_peak": max(backlog),
                "sqlite_lock_retry_exhaustions": samples[-1]["sqlite_lock_retry_exhaustions"],
                "reconciliation_latency_seconds": {"mean": round(sum(reconciliation) / len(reconciliation), 6),
                                                  "max": max(reconciliation)},
                "scan_latency_seconds": {"count": len(scans),
                                         "mean": None if not scans else round(sum(scans) / len(scans), 6),
                                         "max": None if not scans else max(scans)},
                "growth_flags": flags}

    def _soak_wait(self, duration_seconds: float, ctx: HarnessContext) -> None:
        segments = max(1, ceil(duration_seconds / self.soak_probe_interval_seconds))
        interval_started = time.monotonic()
        resolver = BurnInCampaignRunner(
            self.engine, ctx.campaign_id, lambda *_args: [],
            provider_transient_outage_grace_seconds=300,
        )
        for index in range(segments):
            scan_latency = None
            rows: list[dict[str, Any]] | None = None
            if self._soak_observed["probe_count"] % 10 == 0:
                scan_started = time.monotonic()
                rows = self._normal_market_data()
                scan_latency = time.monotonic() - scan_started
                self._record_market_data_probe(ctx, rows, scan_latency)
                self._soak_observed["market_data_rows"] += len(rows)
                self._soak_observed["empty_market_data_probes"] += int(not rows)
            reconciliation_started = time.monotonic()
            asyncio.run(ctx.runtime._run_reconciliation_once())
            reconciliation_latency = time.monotonic() - reconciliation_started
            self._run_soak_decision_evidence_probe(ctx)
            resolver_result = resolver.resolver_tick()
            snapshot = ctx.runtime._build_runtime_state_snapshot(status="OPERATING")
            ctx.runtime._persist_runtime_state_snapshot(snapshot.runtime_status)
            ctx.runtime._persist_runtime_heartbeat(runtime_state=snapshot.runtime_status)
            with self.engine.begin() as conn:
                update_campaign_heartbeat(conn, ctx.campaign_id)
            sample = self._soak_resource_sample(
                ctx, scan_latency=scan_latency, reconciliation_latency=reconciliation_latency,
                resolver_status=str(resolver_result.get("status") or "UNKNOWN"),
            )
            self._soak_observed["probe_count"] += 1
            self._soak_observed["reconciliation_clean_probes"] += int(
                snapshot.reconciliation_status == "CLEAN"
            )
            for key, check in (
                ("resolver_ok_probes", "resolver_healthy"),
                ("heartbeat_fresh_probes", "heartbeat_fresh"),
                ("lineage_ok_probes", "lineage_consistent"),
                ("worker_alive_probes", "worker_alive"),
                ("safe_status_probes", "execution_safe_status"),
            ):
                self._soak_observed[key] += int(sample["invariants"][check])
            target = interval_started + duration_seconds * (index + 1) / segments
            delay = max(0.0, target - time.monotonic()) if self._enforce_wall_clock else duration_seconds / segments
            self._sleep(delay)

    def _soak_decision_probe_candidate(self) -> dict[str, Any]:
        now = time.time()
        return {
            "signal_id": f"{self._soak_decision_signal_prefix}0001",
            "symbol": "BTCUSDT",
            "source_exchange": "binance",
            "timeframe": "1m",
            "market_ts": now,
            "side": "LONG",
            "entry": 100.0,
            "sl": 99.5,
            "tp": 101.5,
            "rr": 3.0,
            "geometry_status": "COMPLETE",
            "geometry_source": "QUALIFICATION_DETERMINISTIC_DECISION_PROBE",
            "setup_type": "QUALIFICATION_PROBE",
            "setup_reason": "PIPELINE_COVERAGE",
            "setup_quality": 0.95,
            "volume_24h_usdt": 100_000_000.0,
            "spread_pct": 0.0002,
            "spread_status": "MEASURED",
            "spread_source": "QUALIFICATION_PROBE",
            "expected_slippage_pct": 0.0002,
            "slippage_status": "MODEL_ESTIMATE",
            "slippage_source": "QUALIFICATION_PROBE",
            "latency_ms": 50.0,
            "latency_status": "MODEL_ESTIMATE",
            "latency_source": "QUALIFICATION_PROBE",
            "market_data_latency_ms": 25.0,
            "market_data_latency_status": "MEASURED",
            "market_data_latency_source": "QUALIFICATION_PROBE",
            "liquidity_score": 0.95,
            "liquidity_status": "MEASURED",
            "liquidity_source": "QUALIFICATION_PROBE",
            "funding_rate_pct": 0.00001,
            "funding_status": "MEASURED",
            "funding_source": "QUALIFICATION_PROBE",
            "volatility_pct": 1.0,
            "volatility_regime": "NORMAL",
            "volatility_status": "MEASURED",
            "volatility_source": "QUALIFICATION_PROBE",
            "trend_strength": 0.90,
            "momentum_confirmation": 0.90,
            "liquidity_quality": 0.95,
            "volatility_fit": 0.90,
            "recent_volume_change_pct": 1.0,
            "chop_score": 0.10,
            "panic_score": 0.0,
            "spoof_risk": 0.0,
            "fakeout_risk": 0.10,
            "orderbook_imbalance": 0.50,
            "orderbook_status": "MEASURED",
            "orderbook_source": "QUALIFICATION_PROBE",
            "regime": "TREND",
            "mtf": {
                "regime": {
                    "regime": "TRENDING",
                    "evidence_status": "COMPLETE",
                    "alignment": 1.0,
                }
            },
            "qualification_decision_probe": True,
        }

    def _run_soak_decision_evidence_probe(self, ctx: HarnessContext) -> None:
        if self._soak_decision_probe_done:
            return
        self._soak_decision_probe_done = True
        candidate = self._soak_decision_probe_candidate()
        original_scanner = ctx.runtime.market_scanner

        async def probe_scanner() -> list[dict[str, Any]]:
            return [candidate]

        try:
            ctx.runtime.market_scanner = probe_scanner
            before_executions = ctx.runtime.metrics.executions
            asyncio.run(ctx.runtime._scan_once())
            if ctx.runtime.metrics.executions != before_executions:
                raise RuntimeError("QUALIFICATION_DECISION_PROBE_EXECUTED_TRADE")
            ctx.runtime._generate_burnin_snapshot(reason="soak_decision_evidence")
        except Exception as exc:
            self._soak_decision_probe_error = f"{exc.__class__.__name__}:{exc}"
            self._soak_monitor_errors.append(
                f"decision_evidence_probe:{self._soak_decision_probe_error}"
            )
        finally:
            ctx.runtime.market_scanner = original_scanner

    def _soak_decision_evidence(self, ctx: HarnessContext) -> dict[str, int]:
        params = {
            "bid": ctx.burnin_run_id,
            "prefix": f"{self._soak_decision_signal_prefix}%",
        }
        canonical = canonical_decision_sql("o")
        with self.engine.connect() as conn:
            def scalar(sql: str) -> int:
                return int(conn.execute(text(sql), params).scalar_one() or 0)

            evidence = {
                "canonical_decisions": scalar(
                    f"SELECT COUNT(*) FROM burnin_observations o "
                    f"WHERE o.burnin_run_id=:bid AND {canonical}"
                ),
                "accepted_decisions": scalar(
                    f"SELECT COUNT(*) FROM burnin_observations o "
                    f"WHERE o.burnin_run_id=:bid AND UPPER(COALESCE(o.decision,''))='ACCEPTED' "
                    f"AND {canonical}"
                ),
                "rejected_decisions": scalar(
                    f"SELECT COUNT(*) FROM burnin_observations o "
                    f"WHERE o.burnin_run_id=:bid AND UPPER(COALESCE(o.decision,''))='REJECTED' "
                    f"AND {canonical}"
                ),
                "decision_evidence": scalar(
                    "SELECT COUNT(*) FROM decision_evidence WHERE run_id=:bid"
                ),
                "order_decisions": scalar(
                    "SELECT COUNT(*) FROM order_decisions WHERE signal_id LIKE :prefix"
                ),
                "rejected_signal_reviews": scalar(
                    "SELECT COUNT(*) FROM rejected_signal_reviews WHERE signal_id LIKE :prefix"
                ),
                "pending_reject_labels": scalar(
                    "SELECT COUNT(*) FROM burnin_pending_reject_labels "
                    "WHERE burnin_run_id=:bid AND signal_id LIKE :prefix"
                ),
                "reject_outcomes": scalar(
                    "SELECT COUNT(*) FROM burnin_reject_outcomes WHERE burnin_run_id=:bid"
                ),
                "trade_outcomes": scalar(
                    "SELECT COUNT(*) FROM burnin_trade_outcomes WHERE burnin_run_id=:bid"
                ),
                "qualification_snapshots": scalar(
                    "SELECT COUNT(*) FROM burnin_qualification_snapshots WHERE burnin_run_id=:bid"
                ),
                "complete_mtf_regime_mismatches": scalar(
                    """
                    SELECT COUNT(*)
                    FROM burnin_observations o
                    WHERE o.burnin_run_id=:bid
                      AND UPPER(COALESCE(json_extract(o.metrics_json,'$.mtf.regime.evidence_status'),''))='COMPLETE'
                      AND json_extract(o.metrics_json,'$.mtf.regime.regime') IS NOT NULL
                      AND UPPER(COALESCE(o.regime,''))
                          <> UPPER(COALESCE(json_extract(o.metrics_json,'$.mtf.regime.regime'),''))
                    """
                ),
            }
        return evidence

    def _soak_decision_evidence_checks(
        self, ctx: HarnessContext, evidence: Mapping[str, int]
    ) -> dict[str, bool]:
        canonical_decisions = int(evidence.get("canonical_decisions") or 0)
        return {
            "canonical_decision_evidence_nonzero": canonical_decisions > 0,
            "decision_evidence_persisted":
                int(evidence.get("decision_evidence") or 0) >= canonical_decisions > 0,
            "order_decision_pipeline_exercised":
                int(evidence.get("order_decisions") or 0) > 0,
            "reject_path_evidence_persisted":
                int(evidence.get("rejected_decisions") or 0) > 0
                and int(evidence.get("rejected_signal_reviews") or 0) > 0,
            "reject_label_evidence_persisted":
                int(evidence.get("pending_reject_labels") or 0) > 0,
            "qualification_snapshot_scoped":
                int(evidence.get("qualification_snapshots") or 0) > 0,
            "canonical_regime_evidence_consistent":
                int(evidence.get("complete_mtf_regime_mismatches") or 0) == 0,
            "decision_probe_no_submit":
                self._soak_decision_probe_done
                and self._soak_decision_probe_error is None
                and ctx.runtime.metrics.executions == 0,
        }

    def _finish_soak_operation(self, ctx: HarnessContext, injected_at: str,
                               started_monotonic: float) -> ScenarioResult:
        observed = {**self._soak_observed, "market_data_source": self.market_data_source}
        observed["market_data_probes"] = list(self._market_data_probes)
        observed["market_data_recoveries"] = list(self._market_data_recoveries)
        observed["max_consecutive_empty_market_data_probes"] = self._market_data_max_empty_streak
        resources = self._soak_resource_summary()
        observed["resources"] = resources
        observed["monitor_errors"] = list(self._soak_monitor_errors)
        observed["qualification_scope"] = "END_TO_END_PAPER_NO_SUBMIT"
        decision_evidence = self._soak_decision_evidence(ctx)
        observed["decision_evidence"] = decision_evidence
        observed["decision_probe_error"] = self._soak_decision_probe_error
        all_samples_safe = all(all(sample["invariants"].values()) for sample in self._soak_samples)
        checks = {
            "normal_market_data_observed": self._soak_observed["market_data_rows"] > 0
                                           and self._market_data_empty_streak == 0
                                           and self._market_data_max_empty_streak <= 1
                                           and self._soak_observed["empty_market_data_probes"]
                                           <= max(1, len(self._market_data_probes) // 20),
            **self._soak_decision_evidence_checks(ctx, decision_evidence),
            "empty_market_data_never_executed": all(
                sample["paper_executions"] == 0 for sample in self._soak_samples
            ),
            "clean_reconciliation_between_faults": self._soak_observed["reconciliation_clean_probes"]
                                                    == self._soak_observed["probe_count"],
            "soak_worker_alive": not ctx.runtime._stop_event.is_set(),
            "continuous_safety_invariants": all_samples_safe,
            "resource_growth_bounded": not resources["growth_flags"],
            "no_monitor_errors": not self._soak_monitor_errors,
            "actual_wall_clock_duration": (not self._enforce_wall_clock
                                           or time.monotonic() - started_monotonic >= self.soak_hours * 3600),
        }
        recovered_at = utc_now()
        self._terminalize(ctx, "QUALIFICATION_SOAK_COMPLETE")
        return self._finish_result(
            ctx, name="soak_normal_market_data", injected_at=injected_at,
            recovered_at=recovered_at, classification=UNKNOWN,
            expected=["public feed gaps recover by the next five-minute probe, with no PAPER execution",
                      "same-run canonical PAPER decision evidence is persisted without order submission",
                      "clean reconciliation and heartbeat continue"],
            observed=observed, checks=checks, started_monotonic=started_monotonic,
        )

    def run(self) -> dict[str, Any]:
        self._run_started_monotonic = time.monotonic()
        env_keys = ("ALPHAFORGE_EXECUTION_MODE", "EXECUTION_MODE", "ALPHAFORGE_ALLOW_LIVE_ORDERS",
                    "LIVE_ORDER_SUBMISSION", "ALPHAFORGE_DB_URL", "ALPHAFORGE_BURNIN_DATABASE_PATH")
        old_env = {key: os.environ.get(key) for key in env_keys}
        os.environ.update({"ALPHAFORGE_EXECUTION_MODE": "PAPER", "EXECUTION_MODE": "PAPER",
                           "ALPHAFORGE_ALLOW_LIVE_ORDERS": "false", "LIVE_ORDER_SUBMISSION": "false",
                           "ALPHAFORGE_DB_URL": f"sqlite+pysqlite:///{self.db_path}",
                           "ALPHAFORGE_BURNIN_DATABASE_PATH": str(self.db_path)})
        try:
            soak_ctx = None
            soak_started = time.monotonic()
            soak_injected_at = self.started_at
            if self.mode == "SOAK":
                soak_ctx = self._new_context(
                    "soak_normal_market_data", self._provider({"fault": None}),
                    qualification_targets=True,
                )
            provider_cases = (
                ("transient_dns_gaierror", "gaierror", TRANSIENT_TRANSPORT, False),
                ("url_error", "urlerror", TRANSIENT_TRANSPORT, False),
                ("timeout", "timeout", TRANSIENT_TRANSPORT, False),
                ("connection_reset", "connection_reset", TRANSIENT_TRANSPORT, False),
                ("http_429", "http_429", TRANSIENT_TRANSPORT, False),
                ("http_5xx", "http_5xx", TRANSIENT_TRANSPORT, False),
                ("permanent_authentication", "auth", PERMANENT_AUTH_OR_PROTOCOL, True),
                ("malformed_provider_response", "malformed", PERMANENT_AUTH_OR_PROTOCOL, True),
            )
            if self.mode == "SOAK":
                interval = self.soak_hours * 3600 / (len(FAULT_NAMES) + 1)
            else:
                interval = 0.0
            for case in provider_cases:
                if interval and soak_ctx is not None: self._soak_wait(interval, soak_ctx)
                self._run_provider_fault(case[0], case[1], case[2], permanent=case[3])
            special_cases = (
                (self._run_transient_grace_expiry, TRANSIENT_TRANSPORT),
                (self._run_sqlite_contention, TRANSIENT_TRANSPORT),
                (self._run_stale_heartbeat, UNKNOWN),
                (self._run_delayed_resolver, TRANSIENT_TRANSPORT),
                (self._run_reconciliation_unavailable, UNKNOWN),
                (self._run_worker_restart, UNKNOWN),
                (self._run_duplicate_replay, UNKNOWN),
            )
            for method, classification in special_cases:
                if interval and soak_ctx is not None: self._soak_wait(interval, soak_ctx)
                try:
                    method()
                except Exception as exc:
                    self._record_scenario_exception(
                        method.__name__.removeprefix("_run_"), classification, exc,
                    )
            try:
                watchdog_checks, watchdog_observed = self._run_watchdog_reset_and_exports()
            except Exception as exc:
                watchdog_checks = {"scenario_completed": False}
                watchdog_observed = {"exception": f"{exc.__class__.__name__}:{exc}"}
            try:
                reject_checks, reject_observed = self._run_reject_persistence()
            except Exception as exc:
                reject_checks = {"scenario_completed": False}
                reject_observed = {"exception": f"{exc.__class__.__name__}:{exc}"}
            if soak_ctx is not None:
                self._soak_wait(interval, soak_ctx)
                self._finish_soak_operation(soak_ctx, soak_injected_at, soak_started)
            for context in list(self._active_contexts.values()):
                with contextlib.suppress(Exception):
                    self._terminalize(context, "QUALIFICATION_AUTOMATIC_TEARDOWN")
            return self._report(watchdog_checks, watchdog_observed, reject_checks, reject_observed)
        finally:
            for context in list(self._active_contexts.values()):
                with contextlib.suppress(Exception):
                    self._terminalize(context, "QUALIFICATION_AUTOMATIC_TEARDOWN")
            for key, value in old_env.items():
                if value is None: os.environ.pop(key, None)
                else: os.environ[key] = value
            self.close()

    def _report(self, watchdog_checks: dict[str, bool], watchdog_observed: dict[str, Any],
                reject_checks: dict[str, bool], reject_observed: dict[str, Any]) -> dict[str, Any]:
        all_checks = [value for result in self.results for value in result.invariant_checks.values()]
        all_checks.extend(watchdog_checks.values()); all_checks.extend(reject_checks.values())
        failures = [{"scenario": result.name, "check": key} for result in self.results
                    for key, passed in result.invariant_checks.items() if not passed]
        failures.extend({"scenario": "watchdog_and_export_consistency", "check": key}
                        for key, passed in watchdog_checks.items() if not passed)
        failures.extend({"scenario": "reject_persistence", "check": key}
                        for key, passed in reject_checks.items() if not passed)
        runtime_lifecycle: dict[str, dict[str, int]] = {}
        for item in self.worker_lifecycle:
            counts = runtime_lifecycle.setdefault(item["runtime_instance_id"], {"starts": 0, "exits": 0})
            if item.get("reason") == "QUALIFICATION_SCENARIO_STARTED": counts["starts"] += 1
            else: counts["exits"] += 1
        unexplained = [{"runtime_instance_id": rid, **counts} for rid, counts in runtime_lifecycle.items()
                       if counts != {"starts": 1, "exits": 1}]
        verdict = "PASS" if all(all_checks) and all(result.verdict == "PASS" for result in self.results) and not unexplained else "NEEDS_FIX"
        report = {
            "schema_version": "autonomous_qualification_v1", "overall_verdict": verdict,
            "qualification_run_id": self.run_id, "mode": self.mode,
            "started_at": self.started_at, "completed_at": utc_now(),
            "actual_wall_clock_duration_seconds": round(
                time.monotonic() - (self._run_started_monotonic or time.monotonic()), 3
            ),
            "isolation": {"database": str(self.db_path), "artifact_directory": str(self.artifact_dir),
                          "database_created_for_run": True, "paper_only": True,
                          "live_order_submission": False, "production_db_discovery": False,
                          "active_runtime_reuse": False,
                          "market_data_source": self.market_data_source,
                          "qualification_scope": (
                              "END_TO_END_PAPER_NO_SUBMIT" if self.mode == "SOAK"
                              else "FAULT_INVARIANT_HARNESS"
                          )},
            "tests_executed": [result.name for result in self.results] +
                              ["watchdog_and_export_consistency", "reject_persistence"],
            "faults_injected": [asdict(result) for result in self.results],
            "recovery_latency_seconds": {result.name: result.recovery_latency_seconds
                                         for result in self.results if result.recovery_latency_seconds is not None},
            "invariant_failures": failures, "unexplained_state_transitions": unexplained,
            "persistence_gaps": [result.name for result in self.results
                                 if not result.invariant_checks.get("failure_attempts_persisted", True)],
            "campaign_run_lineage_consistency": all(
                result.invariant_checks.get("campaign_run_lineage_consistent", False) for result in self.results),
            "worker_lifecycle": self.worker_lifecycle,
            "cross_scenario_checks": {"watchdog_and_export_consistency": watchdog_checks,
                                      "reject_persistence": reject_checks},
            "cross_scenario_evidence": {"watchdog_and_export_consistency": watchdog_observed,
                                        "reject_persistence": reject_observed},
            "evidence_references": {result.name: result.db_evidence for result in self.results},
            "soak_resources": self._soak_resource_summary() if self.mode == "SOAK" else None,
            "soak_sample_invariant_failures": [
                {"sample": index, "at": sample["at"], "check": name}
                for index, sample in enumerate(self._soak_samples)
                for name, passed in sample["invariants"].items() if not passed
            ],
            "remaining_risks": [
                "FAST uses deterministic accelerated time and does not prove wall-clock soak stability.",
                ("SOAK public market-data probes depend on external exchange availability."
                 if self.market_data_source == "PUBLIC"
                 else "Synthetic SOAK probes do not prove external exchange availability."),
                "A persistent SQLite outage cannot make new audit evidence durable until storage recovers.",
            ],
        }
        json_path = self.artifact_dir / "qualification-report.json"
        md_path = self.artifact_dir / "qualification-report.md"
        json_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
        md_path.write_text(self._markdown(report))
        report["report_paths"] = {"json": str(json_path), "markdown": str(md_path)}
        json_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
        return report

    @staticmethod
    def _markdown(report: Mapping[str, Any]) -> str:
        lines = ["# AlphaForge Autonomous Qualification", "",
                 f"- Overall verdict: **{report['overall_verdict']}**",
                 f"- Mode: `{report['mode']}`", f"- Run: `{report['qualification_run_id']}`",
                 f"- Actual wall-clock duration: {report['actual_wall_clock_duration_seconds']} seconds",
                 f"- Database: `{report['isolation']['database']}`",
                 f"- Artifacts: `{report['isolation']['artifact_directory']}`", "",
                 "## Fault injection results", "",
                 "| Fault | Class | Verdict | Recovery latency (s) |", "|---|---|---:|---:|"]
        for result in report["faults_injected"]:
            latency = "" if result["recovery_latency_seconds"] is None else str(result["recovery_latency_seconds"])
            lines.append(f"| {result['name']} | {result['classification']} | {result['verdict']} | {latency} |")
        lines.extend(["", "## Invariant matrix", "", "| Scenario | Invariant | Result |", "|---|---|---:|"])
        for result in report["faults_injected"]:
            for name, passed in result["invariant_checks"].items():
                lines.append(f"| {result['name']} | {name} | {'PASS' if passed else 'FAIL'} |")
        for group, checks in report["cross_scenario_checks"].items():
            for name, passed in checks.items():
                lines.append(f"| {group} | {name} | {'PASS' if passed else 'FAIL'} |")
        lines.extend(["", "## Invariant failures", ""])
        if report["invariant_failures"]:
            lines.extend(f"- `{item['scenario']}`: `{item['check']}`" for item in report["invariant_failures"])
        else:
            lines.append("- None.")
        lines.extend(["", "## Worker lifecycle", "",
                      f"- Recorded transitions: {len(report['worker_lifecycle'])}",
                      f"- Unexplained transitions: {len(report['unexplained_state_transitions'])}", "",
                      "## Persistence and lineage", "",
                      f"- Persistence gaps: {len(report['persistence_gaps'])}",
                      f"- Campaign/run lineage consistent: {report['campaign_run_lineage_consistency']}", "",
                      "## Evidence references", ""])
        if report["soak_resources"] is not None:
            resources = report["soak_resources"]
            lines.extend(["", "## SOAK resource stability", "",
                          f"- Samples: {resources['sample_count']}",
                          f"- RSS high-water growth: {resources['rss_high_water_bytes']['growth']} bytes",
                          f"- Database growth: {resources['db_bytes']['growth']} bytes",
                          f"- Artifact growth: {resources['artifact_bytes']['growth']} bytes",
                          f"- Peak queue depth: {resources['queue_peak']}",
                          f"- Peak resolver/reject backlog: {resources['backlog_peak']}",
                          f"- Growth flags: {', '.join(resources['growth_flags']) or 'none'}",
                          f"- Sample log: `{resources['sample_artifact']}`", ""])
        for scenario, refs in report["evidence_references"].items():
            lines.append(f"- `{scenario}`: " + (", ".join(f"`{ref}`" for ref in refs) if refs else "none"))
        lines.extend(["", "## Remaining risks", ""])
        lines.extend(f"- {risk}" for risk in report["remaining_risks"])
        return "\n".join(lines) + "\n"

    def close(self) -> None:
        if not self._closed:
            self.engine.dispose()
            self._closed = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m alphaforge.autonomous_qualification")
    parser.add_argument("--mode", choices=("fast", "soak"), default="fast")
    parser.add_argument("--output-root")
    parser.add_argument("--soak-hours", type=float, default=6.0)
    parser.add_argument("--market-data", choices=("public", "synthetic"), default="public")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        harness = AutonomousQualificationHarness(mode=args.mode, output_root=args.output_root,
                                                 soak_hours=args.soak_hours,
                                                 market_data_source=(args.market_data if args.mode == "soak"
                                                                     else "synthetic"))
        report = harness.run()
    except Exception as exc:
        payload = {"overall_verdict": "BLOCKED", "error": f"{exc.__class__.__name__}:{exc}"}
        print(json.dumps(payload, sort_keys=True))
        return 2
    if args.json:
        print(json.dumps(report, sort_keys=True, default=str))
    else:
        print(f"overall_verdict: {report['overall_verdict']}")
        print(f"json_report: {report['report_paths']['json']}")
        print(f"markdown_report: {report['report_paths']['markdown']}")
    return 0 if report["overall_verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
