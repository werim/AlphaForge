"""Offline, shadow-only decision calibration. Source campaign connections are read-only.

This module has no runtime/order imports and cannot mutate a trading threshold.
The output database is deliberately separate from the campaign database.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from alphaforge.burnin import canonical_decision_sql, canonical_hash, confidence_interval, utc_now

SCHEMA_VERSION = "adaptive_shadow_v2"
MIN_SAMPLE = 30
RECENT_WINDOW = 20
LONG_WINDOW = 100
COOLDOWN_SECONDS = 24 * 3600

DDL = (
    """CREATE TABLE IF NOT EXISTS adaptive_decision_observations (
      id INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, campaign_id TEXT NOT NULL,
      burnin_run_id TEXT NOT NULL, decision_id TEXT NOT NULL, actual_decision TEXT NOT NULL,
      reason_or_gate TEXT NOT NULL, symbol TEXT, side TEXT, regime TEXT, setup_type TEXT,
      outcome_label TEXT, net_r REAL, attributable INTEGER NOT NULL, evidence_complete INTEGER NOT NULL,
      execution_invalidated INTEGER NOT NULL, observed_at TEXT NOT NULL, context_json TEXT NOT NULL,
      schema_version TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS adaptive_decision_calibration (
      calibration_key TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, decision_dimension TEXT NOT NULL,
      reason_or_gate TEXT NOT NULL, scope_json TEXT NOT NULL, sample_count INTEGER NOT NULL,
      observation_count INTEGER NOT NULL, tp_count INTEGER NOT NULL, sl_count INTEGER NOT NULL,
      ambiguous_count INTEGER NOT NULL, timeout_count INTEGER NOT NULL, execution_invalidated_count INTEGER NOT NULL,
      evidence_attributable_count INTEGER NOT NULL, avg_net_r REAL, total_net_r REAL,
      ewma_net_r REAL, recent_expectancy REAL, long_expectancy REAL,
      lower_confidence_bound REAL, upper_confidence_bound REAL, base_value REAL, shadow_value REAL,
      calibration_state TEXT NOT NULL, guardrails_json TEXT NOT NULL, evidence_hash TEXT NOT NULL,
      last_adjusted_at TEXT, updated_at TEXT NOT NULL, schema_version TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS adaptive_decision_calibration_events (
      event_id TEXT PRIMARY KEY, calibration_key TEXT NOT NULL, evidence_hash TEXT NOT NULL,
      previous_state TEXT, proposed_state TEXT NOT NULL, previous_value REAL, proposed_value REAL,
      sample_count INTEGER NOT NULL, recent_expectancy REAL, long_expectancy REAL,
      lower_confidence_bound REAL, upper_confidence_bound REAL, reason TEXT NOT NULL,
      blocked_by_guardrail INTEGER NOT NULL, evidence_window_json TEXT NOT NULL,
      created_at TEXT NOT NULL, schema_version TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS adaptive_shadow_decisions (
      decision_key TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, burnin_run_id TEXT NOT NULL,
      decision_id TEXT NOT NULL, actual_decision TEXT NOT NULL, adaptive_shadow_decision TEXT NOT NULL,
      calibration_key TEXT, reason_or_gate TEXT NOT NULL, observed_net_r REAL,
      retrospective_in_sample INTEGER NOT NULL DEFAULT 1, context_json TEXT NOT NULL,
      updated_at TEXT NOT NULL, schema_version TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS state_direction_shadow_decisions (
      shadow_decision_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL,
      symbol TEXT NOT NULL, decision_timestamp TEXT NOT NULL,
      signal_id TEXT, base_exec_direction TEXT, regime_direction TEXT,
      setup_direction TEXT, resolved_state TEXT NOT NULL,
      shadow_final_direction TEXT NOT NULL, shadow_reason TEXT NOT NULL,
      actual_decision TEXT NOT NULL, actual_side TEXT, actual_reject_reason TEXT,
      entry REAL, base_sl REAL, base_tp REAL, shadow_geometry_type TEXT NOT NULL,
      shadow_sl REAL, shadow_tp REAL, geometry_valid INTEGER NOT NULL,
      spread REAL, expected_slippage REAL, latency REAL, fee_assumption REAL,
      funding_assumption REAL, raw_rr_if_executed REAL,
      effective_rr_if_executed REAL, execution_costs_json TEXT NOT NULL,
      regime TEXT, setup_phase TEXT, timeframe TEXT, horizon_bars INTEGER,
      due_at TEXT, source_provenance_json TEXT NOT NULL,
      evidence_complete INTEGER NOT NULL, updated_at TEXT NOT NULL,
      schema_version TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS state_direction_shadow_outcomes (
      shadow_outcome_id TEXT PRIMARY KEY, shadow_decision_id TEXT NOT NULL UNIQUE,
      outcome_status TEXT NOT NULL, forward_label TEXT, mfe REAL, mae REAL,
      gross_r REAL, cost_adjusted_net_r REAL, total_cost_drag REAL,
      avoided_loss REAL, missed_profit REAL, ambiguous INTEGER NOT NULL,
      evidence_complete INTEGER NOT NULL, resolved_at TEXT,
      evidence_json TEXT NOT NULL, schema_version TEXT NOT NULL,
      FOREIGN KEY(shadow_decision_id) REFERENCES state_direction_shadow_decisions(shadow_decision_id))""",
)


@dataclass(frozen=True)
class HealthEvidence:
    """External health attestations; missing values fail closed for relaxation."""

    execution_quality_ok: bool | None = None
    volatility_stable: bool | None = None
    drawdown_clear: bool | None = None
    evidence_complete: bool | None = None

    def allows_relaxation(self) -> bool:
        return all(value is True for value in (
            self.execution_quality_ok, self.volatility_stable,
            self.drawdown_clear, self.evidence_complete,
        ))


def bootstrap_shadow_schema(conn: sqlite3.Connection) -> None:
    for statement in DDL:
        conn.execute(statement)


def _json(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _load_evidence(source: sqlite3.Connection, campaign_id: str) -> list[dict[str, Any]]:
    """One current record per canonical decision; outcome links require exact ownership."""
    runs = {r[0] for r in source.execute(
        "SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id=?", (campaign_id,)
    )}
    if not runs:
        raise ValueError("campaign has no source runs")
    observations = [_row(r) for r in source.execute(f"""SELECT o.* FROM burnin_observations o
        JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id
        WHERE cr.campaign_id=? AND {canonical_decision_sql('o')} ORDER BY o.id""", (campaign_id,))]
    pending_rejects = defaultdict(list)
    for raw in source.execute("SELECT * FROM burnin_pending_reject_labels WHERE campaign_id=?", (campaign_id,)):
        p = _row(raw)
        pending_rejects[(p["burnin_run_id"], p["reject_decision_id"])].append(p)
    reject_outcomes = defaultdict(list)
    for raw in source.execute("""SELECT o.* FROM burnin_reject_outcomes o
        JOIN burnin_campaign_runs cr ON cr.burnin_run_id=o.burnin_run_id
        WHERE cr.campaign_id=?""", (campaign_id,)):
        outcome = _row(raw)
        payload = _json(outcome["payload_json"])
        reject_outcomes[(outcome["burnin_run_id"], payload.get("reject_decision_id"))].append(outcome)
    positions = defaultdict(list)
    for raw in source.execute("SELECT * FROM burnin_pending_position_outcomes WHERE campaign_id=?", (campaign_id,)):
        p = _row(raw)
        positions[(p["burnin_run_id"], p["signal_id"])].append(p)
    trade_outcomes = defaultdict(list)
    for raw in source.execute("""SELECT t.* FROM burnin_trade_outcomes t
        JOIN burnin_campaign_runs cr ON cr.burnin_run_id=t.burnin_run_id
        WHERE cr.campaign_id=?""", (campaign_id,)):
        t = _row(raw)
        trade_outcomes[(t["burnin_run_id"], t["trade_id"])].append(t)

    signal_counts = defaultdict(int)
    for observation in observations:
        if str(observation["decision"]).upper() == "ACCEPTED":
            signal_id = _json(observation["metrics_json"]).get("signal_id")
            signal_counts[(observation["burnin_run_id"], signal_id)] += 1
    evidence = []
    linked_reject_outcome_ids: set[str] = set()
    for observation in observations:
        metrics = _json(observation["metrics_json"])
        decision = str(observation["decision"] or "GATED").upper()
        run_id = observation["burnin_run_id"]
        identity = metrics.get("reject_decision_id") if decision == "REJECTED" else metrics.get("signal_id")
        # An observation_id fallback is useful diagnostically but cannot attribute an outcome.
        decision_id = str(identity or observation["observation_id"])
        reason = str(metrics.get("primary_reject_reason") or metrics.get("reject_reason") or
                     ("ACCEPTED" if decision == "ACCEPTED" else decision)).upper()
        entry = dict(campaign_id=campaign_id, burnin_run_id=run_id, decision_id=decision_id,
                     actual_decision=decision, reason_or_gate=reason, symbol=observation["symbol"],
                     side=None, regime=observation["regime"], setup_type=None, outcome_label=None,
                     net_r=None, attributable=False, evidence_complete=False,
                     execution_invalidated=False, observed_at=observation["observed_at"],
                     context={"metrics": metrics, "observation_id": observation["observation_id"]})
        if decision == "REJECTED" and identity:
            pending = pending_rejects[(run_id, identity)]
            matches = []
            if len(pending) == 1:
                p = pending[0]
                entry["side"] = p["side"]
                entry["setup_type"] = metrics.get("setup_type") or _json(p["source_provenance_json"]).get("setup_type")
                if reason == "REJECTED" and p["reject_reason"]:
                    reason = str(p["reject_reason"]).upper()
                    entry["reason_or_gate"] = reason
                for outcome in reject_outcomes[(run_id, identity)]:
                    payload = _json(outcome["payload_json"])
                    if (payload.get("campaign_id") == campaign_id
                            and payload.get("burnin_run_id") == run_id
                            and payload.get("reject_decision_id") == identity
                            and payload.get("pending_label_id") == p["pending_label_id"]
                            and outcome["reject_reason"] == p["reject_reason"]
                            and str(p["reject_reason"] or "").upper() == reason
                            and outcome["symbol"] == observation["symbol"]
                            and payload.get("reject_quality_attributable") is not False
                            and payload.get("forward_label_subject") != "LEGACY_SCANNER_SHADOW_CANDIDATE"
                            and _json(p["source_provenance_json"]).get("forward_label_subject") != "LEGACY_SCANNER_SHADOW_CANDIDATE"):
                        matches.append(outcome)
            if len(matches) == 1:
                o = matches[0]
                linked_reject_outcome_ids.add(o["reject_outcome_id"])
                entry.update(outcome_label=o["forward_label"], net_r=_finite(o["hypothetical_net_r_after_costs"]),
                             evidence_complete=bool(o["evidence_complete"]),
                             execution_invalidated=bool(o["execution_invalidated"]),
                             observed_at=o["evidence_horizon"] or o["decision_time"] or entry["observed_at"])
                entry["attributable"] = True
                entry["context"]["outcome_id"] = o["reject_outcome_id"]
                entry["context"]["hypothetical_gross_r"] = o["hypothetical_gross_r"]
                entry["context"]["reject_correct"] = _json(o["payload_json"]).get("reject_correct")
                entry["context"]["execution_cost_assumptions"] = _json(p["execution_cost_assumptions_json"])
        elif decision == "ACCEPTED" and identity and signal_counts[(run_id, identity)] == 1:
            linked = positions[(run_id, identity)]
            if len(linked) == 1 and linked[0]["symbol"] == observation["symbol"]:
                p = linked[0]
                entry["side"] = p["side"]
                entry["setup_type"] = metrics.get("setup_type") or _json(p["source_provenance_json"]).get("setup_type")
                entry["context"]["pending_position_id"] = p["pending_position_id"]
                entry["context"]["entry_quality"] = {
                    "planned_entry": p["planned_entry"], "simulated_fill": p["simulated_fill"]}
                trades = trade_outcomes[(run_id, p["trade_id"])]
                if len(trades) == 1 and trades[0]["symbol"] == observation["symbol"]:
                    t = trades[0]
                    payload = _json(t["payload_json"])
                    if (payload.get("signal_id") == identity
                            and payload.get("pending_position_id") == p["pending_position_id"]):
                        entry.update(outcome_label=t["exit_reason"], net_r=_finite(t["net_r"]),
                                     evidence_complete=bool(t["evidence_complete"]),
                                     observed_at=t["closed_at"] or entry["observed_at"],
                                     attributable=True)
                        entry["context"]["outcome_id"] = t["outcome_id"]
                        entry["context"]["costs"] = {field: t[field] for field in (
                            "spread_cost", "entry_slippage_cost", "exit_slippage_cost", "fee_cost",
                            "funding_cost", "latency_cost", "volatility_penalty", "liquidity_penalty",
                            "total_execution_cost")}
                        entry["context"]["hold_duration_seconds"] = t["hold_duration_seconds"]
                        entry["context"]["mfe"] = t["mfe"]
                        entry["context"]["mae"] = t["mae"]
                        entry["context"]["drawdown_contribution"] = None  # No per-trade attribution exists.
                else:
                    entry["outcome_label"] = p["status"]
        evidence.append(entry)
    # Preserve orphan/legacy outcomes as diagnostics; never use them in calibration.
    for outcomes in reject_outcomes.values():
        for o in outcomes:
            if o["reject_outcome_id"] in linked_reject_outcome_ids:
                continue
            payload = _json(o["payload_json"])
            evidence.append(dict(campaign_id=campaign_id, burnin_run_id=o["burnin_run_id"],
                decision_id="diagnostic:" + o["reject_outcome_id"], actual_decision="DIAGNOSTIC",
                reason_or_gate=str(o["reject_reason"] or "UNKNOWN"), symbol=o["symbol"],
                side=None, regime=o["regime"], setup_type=None, outcome_label=o["forward_label"],
                net_r=_finite(o["hypothetical_net_r_after_costs"]), attributable=False,
                evidence_complete=bool(o["evidence_complete"]),
                execution_invalidated=bool(o["execution_invalidated"]),
                observed_at=o["evidence_horizon"] or o["decision_time"],
                context={"outcome_id":o["reject_outcome_id"], "claimed_decision_id":payload.get("reject_decision_id"),
                         "diagnostic_reason":"UNLINKED_OR_NON_ATTRIBUTABLE_REJECT_OUTCOME", "metrics":{}}))
    return evidence


def _eligible(e: Mapping[str, Any]) -> bool:
    label = str(e["outcome_label"] or "").upper()
    return (bool(e["attributable"]) and bool(e["evidence_complete"])
            and not e["execution_invalidated"] and e["net_r"] is not None
            and label not in {"AMBIGUOUS", "CANCELLED", "NO_FILL", "OPEN", "OPEN_AT_END"})


def _scopes(e: Mapping[str, Any]) -> list[dict[str, Any]]:
    reason = e["reason_or_gate"]
    scopes = []
    if e["symbol"] and e["regime"] and e["side"]:
        scopes.append(dict(reason=reason, symbol=e["symbol"], regime=e["regime"], side=e["side"]))
    if e["symbol"] and e["regime"]:
        scopes.append(dict(reason=reason, symbol=e["symbol"], regime=e["regime"]))
    if e["regime"]:
        scopes.append(dict(reason=reason, regime=e["regime"]))
    scopes.append(dict(reason=reason))
    return scopes


def _key(campaign_id: str, path: str, scope: Mapping[str, Any]) -> str:
    return canonical_hash([SCHEMA_VERSION, campaign_id, path, dict(scope)])


def _statistics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda e: (str(e["observed_at"]), e["decision_id"]))
    qualified = [e for e in ordered if _eligible(e)]
    values = [float(e["net_r"]) for e in qualified]
    recent = values[-RECENT_WINDOW:]
    long = values[-LONG_WINDOW:]
    _, lower, upper = confidence_interval(long, z=2.58)
    ewma = None
    if values:
        # Twenty observations give roughly the same time scale as RECENT_WINDOW.
        alpha = 2 / (RECENT_WINDOW + 1)
        for value in values:
            ewma = value if ewma is None else alpha * value + (1 - alpha) * ewma
    labels = [str(e["outcome_label"] or "").upper() for e in ordered]
    return dict(sample_count=len(values), observation_count=len(rows),
                tp_count=sum(x in {"TP_BEFORE_SL", "TP_HIT", "TP"} for x in labels),
                sl_count=sum(x in {"SL_BEFORE_TP", "SL_HIT", "SL"} for x in labels),
                ambiguous_count=sum(x == "AMBIGUOUS" for x in labels),
                timeout_count=sum(x == "TIMEOUT" for x in labels),
                execution_invalidated_count=sum(bool(e["execution_invalidated"]) for e in rows),
                evidence_attributable_count=sum(bool(e["attributable"]) for e in rows),
                avg_net_r=sum(values)/len(values) if values else None,
                total_net_r=sum(values), ewma_net_r=ewma,
                recent_expectancy=sum(recent)/len(recent) if recent else None,
                long_expectancy=sum(long)/len(long) if long else None,
                lower_confidence_bound=lower, upper_confidence_bound=upper,
                recent_sample_count=len(recent), long_sample_count=len(long))


def _state(stats: Mapping[str, Any], health: HealthEvidence, *, path: str,
           rows: list[dict[str, Any]]) -> tuple[str, list[str]]:
    if stats["sample_count"] < MIN_SAMPLE or stats["recent_sample_count"] < 10:
        return "INSUFFICIENT_SAMPLE", ["MINIMUM_SAMPLE"]
    lower, upper = stats["lower_confidence_bound"], stats["upper_confidence_bound"]
    recent, long = stats["recent_expectancy"], stats["long_expectancy"]
    if lower is None or upper is None or recent is None or long is None:
        return "INSUFFICIENT_SAMPLE", ["INCOMPLETE_EXPECTANCY"]
    if upper < 0 and recent < 0 and long < 0:
        return "TIGHT", ["CONFIDENTLY_NEGATIVE_NET_R"]
    if lower > 0 and recent > 0 and long > 0:
        # A small number of diagnostic/invalidated outcomes may be displayed, but
        # they cannot improve the eligible mean and prohibit a relaxation.
        if any(not _eligible(e) for e in rows):
            return "FROZEN", ["INCOMPLETE_OR_NON_ATTRIBUTABLE_EVIDENCE"]
        if not health.allows_relaxation():
            return "FROZEN", ["SYSTEM_HEALTH_UNPROVEN_OR_DEGRADED"]
        return "RELAXED", ["CONFIDENTLY_POSITIVE_NET_R"]
    return "NORMAL", ["CONFIDENCE_OVERLAPS_ZERO_OR_WINDOWS_DISAGREE"]


def _threshold(path: str, reason: str, base: Mapping[str, float]) -> tuple[str | None, float | None]:
    if path == "ACCEPTED":
        name = "MIN_TRADE_SCORE"
    elif reason in {"LOW_SCORE", "LOW_CONFIDENCE", "MIN_TRADE_SCORE"}:
        name = "MIN_TRADE_SCORE"
    elif reason in {"LOW_EFFECTIVE_RR", "RR_TOO_LOW"}:
        name = "MIN_EFFECTIVE_RR"
    else:
        return None, None
    value = _finite(base.get(name))
    if name == "MIN_TRADE_SCORE" and value is not None and not 0 <= value <= 1:
        return name, None  # PAPER score units only; BACKTEST score units differ.
    return name, value


def _shadow_value(name: str | None, base: float | None, state: str) -> float | None:
    if name is None or base is None or state not in {"TIGHT", "RELAXED"}:
        return base
    step, bound = (0.02, 0.05) if name == "MIN_TRADE_SCORE" else (0.05, 0.15)
    direction = 1 if state == "TIGHT" else -1
    return round(max(base-bound, min(base+bound, base+direction*step)), 8)


def _timestamp(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def calibrate_campaign(source: sqlite3.Connection, output: sqlite3.Connection, campaign_id: str,
                       *, base_thresholds: Mapping[str, float] | None = None,
                       health: HealthEvidence | None = None, now: str | None = None) -> dict[str, int]:
    """Read source evidence and write only to a separate shadow database.

    Results are retrospective/in-sample; they are proposals, not causal PnL estimates.
    """
    if source is output:
        raise ValueError("source and shadow output must be separate connections")
    source.row_factory = sqlite3.Row
    output.row_factory = sqlite3.Row
    source_files = {r[2] for r in source.execute("PRAGMA database_list") if r[2]}
    output_files = {r[2] for r in output.execute("PRAGMA database_list") if r[2]}
    if source_files & output_files:
        raise ValueError("source and shadow output must be separate database files")
    if output.execute("SELECT 1 FROM sqlite_master WHERE name IN ('burnin_campaigns','burnin_runs') LIMIT 1").fetchone():
        raise ValueError("shadow output contains campaign tables")
    health = health or HealthEvidence()
    now = now or utc_now()
    # Hold one consistent read snapshot even when a campaign writer is active.
    own_read_transaction = not source.in_transaction
    if own_read_transaction:
        source.execute("BEGIN")
    try:
        evidence = _load_evidence(source, campaign_id)
    finally:
        if own_read_transaction:
            source.rollback()
    bootstrap_shadow_schema(output)
    # Immutable event versions preserve an unresolved decision later receiving an outcome.
    for e in evidence:
        payload = {k: v for k, v in e.items() if k != "context"}
        payload["context"] = e["context"]
        event_id = canonical_hash(payload)
        output.execute("""INSERT OR IGNORE INTO adaptive_decision_observations
            (event_id,campaign_id,burnin_run_id,decision_id,actual_decision,reason_or_gate,
             symbol,side,regime,setup_type,outcome_label,net_r,attributable,evidence_complete,
             execution_invalidated,observed_at,context_json,schema_version)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, campaign_id, e["burnin_run_id"], e["decision_id"], e["actual_decision"],
             e["reason_or_gate"], e["symbol"], e["side"], e["regime"], e["setup_type"],
             e["outcome_label"], e["net_r"], int(e["attributable"]), int(e["evidence_complete"]),
             int(e["execution_invalidated"]), e["observed_at"], json.dumps(e["context"], sort_keys=True),
             SCHEMA_VERSION))
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for e in evidence:
        if e["actual_decision"] == "DIAGNOSTIC":
            continue
        for scope in _scopes(e):
            groups[(e["actual_decision"], _key(campaign_id, e["actual_decision"], scope))].append(e)
    scoped = {}
    for (path, key), rows in groups.items():
        scope = next(s for s in _scopes(rows[0]) if _key(campaign_id, path, s) == key)
        stats = _statistics(rows)
        state, reasons = _state(stats, health, path=path, rows=rows)
        name, base = _threshold(path, scope["reason"], base_thresholds or {})
        previous = output.execute("SELECT * FROM adaptive_decision_calibration WHERE calibration_key=?", (key,)).fetchone()
        previous = dict(previous) if previous else None
        evidence_hash = canonical_hash(sorted((e["burnin_run_id"], e["decision_id"], e["outcome_label"], e["net_r"], e["attributable"], e["evidence_complete"], e["execution_invalidated"]) for e in rows))
        blocked = False
        if state in {"TIGHT", "RELAXED"} and previous:
            previous_time = _timestamp(previous["last_adjusted_at"])
            current_time = _timestamp(now)
            if previous["calibration_state"] in {"TIGHT", "RELAXED"} and previous["calibration_state"] != state:
                state, blocked = "NORMAL", True
                reasons.append("HYSTERESIS_OPPOSITE_STATE")
            elif (previous_time and current_time and
                  0 <= (current_time-previous_time).total_seconds() < COOLDOWN_SECONDS and
                  previous["calibration_state"] != state):
                state, blocked = previous["calibration_state"], True
                reasons.append("ADJUSTMENT_COOLDOWN")
        value = _shadow_value(name, base, state)
        if state == "RELAXED" and (name is None or base is None):
            reasons.append("NO_SAFE_NUMERIC_GATE")
        adjusted = now if state in {"TIGHT", "RELAXED"} and (not previous or previous["calibration_state"] != state) else (previous["last_adjusted_at"] if previous else None)
        output.execute("""INSERT INTO adaptive_decision_calibration
          (calibration_key,campaign_id,decision_dimension,reason_or_gate,scope_json,sample_count,
           observation_count,tp_count,sl_count,ambiguous_count,timeout_count,execution_invalidated_count,
           evidence_attributable_count,avg_net_r,total_net_r,ewma_net_r,recent_expectancy,long_expectancy,
           lower_confidence_bound,upper_confidence_bound,base_value,shadow_value,calibration_state,
           guardrails_json,evidence_hash,last_adjusted_at,updated_at,schema_version)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(calibration_key) DO UPDATE SET
           sample_count=excluded.sample_count,observation_count=excluded.observation_count,
           tp_count=excluded.tp_count,sl_count=excluded.sl_count,ambiguous_count=excluded.ambiguous_count,
           timeout_count=excluded.timeout_count,execution_invalidated_count=excluded.execution_invalidated_count,
           evidence_attributable_count=excluded.evidence_attributable_count,avg_net_r=excluded.avg_net_r,
           total_net_r=excluded.total_net_r,ewma_net_r=excluded.ewma_net_r,
           recent_expectancy=excluded.recent_expectancy,long_expectancy=excluded.long_expectancy,
           lower_confidence_bound=excluded.lower_confidence_bound,upper_confidence_bound=excluded.upper_confidence_bound,
           base_value=excluded.base_value,shadow_value=excluded.shadow_value,
           calibration_state=excluded.calibration_state,guardrails_json=excluded.guardrails_json,
           evidence_hash=excluded.evidence_hash,last_adjusted_at=excluded.last_adjusted_at,
           updated_at=excluded.updated_at""",
          (key,campaign_id,path,scope["reason"],json.dumps(scope,sort_keys=True),
           stats["sample_count"],stats["observation_count"],stats["tp_count"],stats["sl_count"],
           stats["ambiguous_count"],stats["timeout_count"],stats["execution_invalidated_count"],
           stats["evidence_attributable_count"],stats["avg_net_r"],stats["total_net_r"],
           stats["ewma_net_r"],stats["recent_expectancy"],stats["long_expectancy"],
           stats["lower_confidence_bound"],stats["upper_confidence_bound"],base,value,state,
           json.dumps({"reasons":reasons,"health":asdict(health)},sort_keys=True),
           evidence_hash,adjusted,now,SCHEMA_VERSION))
        if (not previous or previous["evidence_hash"] != evidence_hash
                or previous["calibration_state"] != state
                or previous["shadow_value"] != value or previous["base_value"] != base
                or _json(previous["guardrails_json"]).get("reasons") != reasons):
            event_id = canonical_hash([key, evidence_hash, previous["calibration_state"] if previous else None,
                                       state, previous["shadow_value"] if previous else None, value, reasons])
            output.execute("""INSERT OR IGNORE INTO adaptive_decision_calibration_events
              (event_id,calibration_key,evidence_hash,previous_state,proposed_state,previous_value,
               proposed_value,sample_count,recent_expectancy,long_expectancy,lower_confidence_bound,
               upper_confidence_bound,reason,blocked_by_guardrail,evidence_window_json,created_at,schema_version)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (event_id,key,evidence_hash,previous["calibration_state"] if previous else None,state,
               previous["shadow_value"] if previous else None,value,stats["sample_count"],
               stats["recent_expectancy"],stats["long_expectancy"],stats["lower_confidence_bound"],
               stats["upper_confidence_bound"],";".join(reasons),int(blocked or state == "FROZEN"),
               json.dumps({"recent":RECENT_WINDOW,"long":LONG_WINDOW},sort_keys=True),now,SCHEMA_VERSION))
        scoped[key] = (stats, state, value, name)
    # A decision uses its most specific adequately sampled scope, otherwise fallback.
    for e in evidence:
        if e["actual_decision"] == "DIAGNOSTIC":
            continue
        path = e["actual_decision"]
        keys = [_key(campaign_id,path,s) for s in _scopes(e)]
        chosen = next((key for key in keys if scoped[key][0]["sample_count"] >= MIN_SAMPLE), keys[-1])
        _, state, value, name = scoped[chosen]
        shadow = "UNCHANGED"
        metrics = e["context"]["metrics"]
        observed = _finite(metrics.get("score" if name == "MIN_TRADE_SCORE" else "effective_rr"))
        if state == "TIGHT" and path == "ACCEPTED" and name and value is not None and observed is not None and observed < value:
            shadow = "WOULD_REJECT_UNDER_TIGHTENED_THRESHOLD"
        elif (state == "RELAXED" and path == "REJECTED" and name and value is not None
              and observed is not None and observed >= value and observed < (base_thresholds or {}).get(name, value)
              and _eligible(e) and health.allows_relaxation()):
            # A single observed failed gate is not proof that all other gates pass.
            shadow = "WOULD_PASS_CALIBRATED_GATE_OTHER_GATES_UNVERIFIED"
        decision_key = canonical_hash([campaign_id,e["burnin_run_id"],e["decision_id"]])
        output.execute("""INSERT INTO adaptive_shadow_decisions
          (decision_key,campaign_id,burnin_run_id,decision_id,actual_decision,adaptive_shadow_decision,
           calibration_key,reason_or_gate,observed_net_r,retrospective_in_sample,context_json,updated_at,schema_version)
          VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?) ON CONFLICT(decision_key) DO UPDATE SET
           adaptive_shadow_decision=excluded.adaptive_shadow_decision,calibration_key=excluded.calibration_key,
           observed_net_r=excluded.observed_net_r,context_json=excluded.context_json,updated_at=excluded.updated_at""",
          (decision_key,campaign_id,e["burnin_run_id"],e["decision_id"],path,shadow,chosen,
           e["reason_or_gate"],e["net_r"],json.dumps(e["context"],sort_keys=True),now,SCHEMA_VERSION))
    output.commit()
    return {"decisions":sum(e["actual_decision"] != "DIAGNOSTIC" for e in evidence),
            "diagnostic_outcomes":sum(e["actual_decision"] == "DIAGNOSTIC" for e in evidence),
            "scopes":len(groups),
            "attributable_outcomes":sum(_eligible(e) for e in evidence)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only campaign evidence to a separate shadow SQLite DB")
    parser.add_argument("--source",required=True,type=Path)
    parser.add_argument("--output",required=True,type=Path)
    parser.add_argument("--campaign-id",required=True)
    parser.add_argument("--base-score",type=float)
    parser.add_argument("--base-effective-rr",type=float)
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        parser.error("source and output paths must differ")
    if args.output.exists():
        with sqlite3.connect(f"file:{args.output.resolve()}?mode=ro",uri=True) as existing:
            if existing.execute("SELECT 1 FROM sqlite_master WHERE name IN ('burnin_campaigns','burnin_runs') LIMIT 1").fetchone():
                parser.error("output contains campaign tables; refusing to modify it")
    # No CLI health attestation: analysis may suggest tightening, but cannot relax.
    with sqlite3.connect(f"file:{args.source.resolve()}?mode=ro",uri=True) as source:
        source.row_factory=sqlite3.Row
        with sqlite3.connect(args.output) as output:
            output.row_factory=sqlite3.Row
            result=calibrate_campaign(source,output,args.campaign_id,base_thresholds={
                "MIN_TRADE_SCORE":args.base_score,
                "MIN_EFFECTIVE_RR":args.base_effective_rr})
    print(json.dumps(result,sort_keys=True))


if __name__ == "__main__":
    main()
