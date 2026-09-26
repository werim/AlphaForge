"""Isolated append-only evidence store for the System Audit Fabric.

The audit fabric is deliberately downstream of AlphaForge runtime/campaign evidence.
This module never writes to the source connection and never participates in the
trading loop.  It copies canonical decision-time evidence into a separate SQLite
store where later audit levels can diagnose and recommend without mutating policy.
"""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Any, Mapping, Sequence

from alphaforge.burnin import (
    canonical_hash,
    canonical_reject_outcome_link_matches,
    utc_now,
)

SCHEMA_VERSION = "system_audit_v1"

IMMUTABLE_TABLES = (
    "audit_decision_envelopes",
    "audit_outcomes",
    "shadow_decisions",
    "shadow_outcomes",
)

DDL = (
    """CREATE TABLE IF NOT EXISTS audit_decision_envelopes (
        envelope_id TEXT PRIMARY KEY,
        source_evidence_id TEXT NOT NULL,
        source_hash TEXT NOT NULL,
        source_version INTEGER NOT NULL,
        campaign_id TEXT,
        burnin_run_id TEXT NOT NULL,
        release_id TEXT,
        git_commit TEXT,
        config_hash TEXT,
        strategy_config_hash TEXT,
        runtime_instance_id TEXT,
        mode TEXT NOT NULL,
        decision_time TEXT,
        signal_id TEXT,
        decision_id TEXT,
        symbol TEXT,
        side TEXT,
        setup_type TEXT,
        regime TEXT,
        decision TEXT NOT NULL,
        reject_reason TEXT,
        score REAL,
        confidence REAL,
        raw_rr REAL,
        effective_rr REAL,
        min_effective_rr REAL,
        entry REAL,
        stop REAL,
        target REAL,
        spread_pct REAL,
        expected_slippage_pct REAL,
        fee_pct REAL,
        funding_rate_pct REAL,
        latency_ms REAL,
        liquidity_score REAL,
        volume_24h_usdt REAL,
        volatility_regime TEXT,
        gate_outputs_json TEXT NOT NULL,
        gate_thresholds_json TEXT NOT NULL,
        gate_margins_json TEXT NOT NULL,
        decision_payload_json TEXT NOT NULL,
        source_provenance_json TEXT NOT NULL,
        ingested_at TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        UNIQUE(source_evidence_id, source_hash)
    )""",
    """CREATE INDEX IF NOT EXISTS ix_audit_envelope_scope
       ON audit_decision_envelopes(burnin_run_id, mode, decision_time)""",
    """CREATE INDEX IF NOT EXISTS ix_audit_envelope_signal
       ON audit_decision_envelopes(signal_id, decision)""",
    """CREATE TABLE IF NOT EXISTS audit_outcomes (
        outcome_snapshot_id TEXT PRIMARY KEY,
        envelope_id TEXT NOT NULL,
        outcome_kind TEXT NOT NULL,
        source_outcome_id TEXT NOT NULL,
        source_hash TEXT NOT NULL,
        outcome_status TEXT,
        gross_r REAL,
        net_r REAL,
        execution_cost REAL,
        expected_effective_rr REAL,
        realized_effective_rr REAL,
        mfe REAL,
        mae REAL,
        hold_duration_seconds REAL,
        ambiguous INTEGER NOT NULL,
        evidence_complete INTEGER NOT NULL,
        attributable INTEGER NOT NULL,
        execution_invalidated INTEGER NOT NULL,
        authoritative INTEGER NOT NULL,
        resolved_at TEXT,
        outcome_payload_json TEXT NOT NULL,
        ingested_at TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        UNIQUE(source_outcome_id, source_hash),
        FOREIGN KEY(envelope_id) REFERENCES audit_decision_envelopes(envelope_id)
    )""",
    """CREATE INDEX IF NOT EXISTS ix_audit_outcomes_envelope
       ON audit_outcomes(envelope_id, outcome_kind)""",
    """CREATE TABLE IF NOT EXISTS shadow_decisions (
        shadow_decision_id TEXT PRIMARY KEY,
        envelope_id TEXT NOT NULL,
        source_reject_decision_id TEXT,
        eligibility TEXT NOT NULL,
        eligibility_reason TEXT NOT NULL,
        side TEXT,
        entry REAL,
        stop REAL,
        target REAL,
        raw_rr REAL,
        effective_rr REAL,
        reject_reasons_json TEXT NOT NULL,
        execution_context_json TEXT NOT NULL,
        attributable INTEGER NOT NULL,
        execution_aligned INTEGER NOT NULL,
        authoritative INTEGER NOT NULL,
        source_hash TEXT NOT NULL,
        ingested_at TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        UNIQUE(envelope_id),
        FOREIGN KEY(envelope_id) REFERENCES audit_decision_envelopes(envelope_id)
    )""",
    """CREATE TABLE IF NOT EXISTS shadow_outcomes (
        shadow_outcome_id TEXT PRIMARY KEY,
        shadow_decision_id TEXT NOT NULL,
        source_outcome_id TEXT NOT NULL,
        source_hash TEXT NOT NULL,
        forward_label TEXT,
        gross_r REAL,
        net_r REAL,
        total_cost_drag REAL,
        avoided_loss REAL,
        missed_profit REAL,
        ambiguous INTEGER NOT NULL,
        evidence_complete INTEGER NOT NULL,
        attributable INTEGER NOT NULL,
        execution_invalidated INTEGER NOT NULL,
        authoritative INTEGER NOT NULL,
        resolved_at TEXT,
        evidence_json TEXT NOT NULL,
        ingested_at TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        UNIQUE(source_outcome_id, source_hash),
        FOREIGN KEY(shadow_decision_id) REFERENCES shadow_decisions(shadow_decision_id)
    )""",
)


def _immutable_trigger_sql(table: str, operation: str) -> str:
    suffix = operation.lower()
    return f"""CREATE TRIGGER IF NOT EXISTS trg_{table}_no_{suffix}
        BEFORE {operation} ON {table}
        BEGIN
          SELECT RAISE(ABORT, 'IMMUTABLE_SYSTEM_AUDIT_EVIDENCE');
        END"""


def bootstrap_audit_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=ON")
    for statement in DDL:
        conn.execute(statement)
    for table in IMMUTABLE_TABLES:
        conn.execute(_immutable_trigger_sql(table, "UPDATE"))
        conn.execute(_immutable_trigger_sql(table, "DELETE"))


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return list(raw)
    if isinstance(raw, tuple):
        return list(raw)
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _row(row: sqlite3.Row | Sequence[Any], columns: Sequence[str] | None = None) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    if columns is None:
        raise TypeError("columns are required for tuple rows")
    return dict(zip(columns, row))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _database_files(conn: sqlite3.Connection) -> set[str]:
    return {str(row[2]) for row in conn.execute("PRAGMA database_list") if row[2]}


def _assert_isolated(source: sqlite3.Connection, audit: sqlite3.Connection) -> None:
    if source is audit:
        raise ValueError("source and audit connections must be separate")
    if _database_files(source) & _database_files(audit):
        raise ValueError("source and audit connections must use separate database files")
    if any(_table_exists(audit, name) for name in ("burnin_runs", "burnin_campaigns", "decision_evidence")):
        raise ValueError("audit output must not be a runtime/campaign database")


def _normalized_decision(value: Any) -> str:
    raw = str(value or "").strip().upper()
    return {"ACCEPTED": "ACCEPT", "REJECTED": "REJECT"}.get(raw, raw)


def _run_ids(
    source: sqlite3.Connection,
    *,
    campaign_id: str | None,
    burnin_run_id: str | None,
) -> list[str]:
    if burnin_run_id:
        run = str(burnin_run_id)
        if campaign_id and _table_exists(source, "burnin_campaign_runs"):
            linked = source.execute(
                "SELECT 1 FROM burnin_campaign_runs WHERE campaign_id=? AND burnin_run_id=?",
                (campaign_id, run),
            ).fetchone()
            if not linked:
                raise ValueError("burn-in run does not belong to campaign")
        return [run]
    if not campaign_id:
        raise ValueError("campaign_id or burnin_run_id is required")
    if not _table_exists(source, "burnin_campaign_runs"):
        raise ValueError("campaign run linkage is unavailable")
    runs = [
        str(row[0])
        for row in source.execute(
            "SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id=? "
            "ORDER BY continuation_sequence, id",
            (campaign_id,),
        )
    ]
    if not runs:
        raise ValueError("campaign has no source runs")
    return runs


def _placeholders(values: Sequence[Any]) -> str:
    return ",".join("?" for _ in values)


def _run_metadata(source: sqlite3.Connection, run_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    if not _table_exists(source, "burnin_runs"):
        return {}
    sql = (
        "SELECT burnin_run_id,release_id,execution_mode,git_commit,config_hash,"
        "strategy_config_hash,source_provenance_json FROM burnin_runs "
        f"WHERE burnin_run_id IN ({_placeholders(run_ids)})"
    )
    return {str(row["burnin_run_id"]): dict(row) for row in source.execute(sql, tuple(run_ids))}


def _campaign_by_run(source: sqlite3.Connection, run_ids: Sequence[str]) -> dict[str, str]:
    if not _table_exists(source, "burnin_campaign_runs"):
        return {}
    sql = (
        "SELECT campaign_id,burnin_run_id FROM burnin_campaign_runs "
        f"WHERE burnin_run_id IN ({_placeholders(run_ids)})"
    )
    return {str(row["burnin_run_id"]): str(row["campaign_id"]) for row in source.execute(sql, tuple(run_ids))}


def _observation_index(source: sqlite3.Connection, run_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    if not _table_exists(source, "burnin_observations"):
        return {}
    sql = (
        "SELECT * FROM burnin_observations "
        f"WHERE burnin_run_id IN ({_placeholders(run_ids)}) ORDER BY id"
    )
    return {str(row["observation_id"]): dict(row) for row in source.execute(sql, tuple(run_ids))}


def _decision_rows(source: sqlite3.Connection, run_ids: Sequence[str]) -> list[dict[str, Any]]:
    if not _table_exists(source, "decision_evidence"):
        raise ValueError("canonical decision_evidence table is required")
    sql = (
        "SELECT * FROM decision_evidence "
        f"WHERE run_id IN ({_placeholders(run_ids)}) "
        "AND UPPER(COALESCE(decision,'')) IN ('ACCEPT','ACCEPTED','REJECT','REJECTED') "
        "ORDER BY id"
    )
    return [dict(row) for row in source.execute(sql, tuple(run_ids))]


def _gate_evidence(row: Mapping[str, Any], diagnostics: Mapping[str, Any]) -> tuple[list[str], dict[str, Any], list[dict[str, Any]]]:
    gates = [
        str(value).upper()
        for value in (_json_list(row.get("reject_flags")) or diagnostics.get("all_failed_gates") or [])
        if str(value or "").strip()
    ]
    thresholds: dict[str, Any] = {}
    margins: list[dict[str, Any]] = []
    failed = diagnostics.get("failed_gate_evidence")
    if not isinstance(failed, list):
        failed = []
    for item in failed:
        if not isinstance(item, Mapping):
            continue
        gate = str(item.get("gate") or "").upper()
        if not gate:
            continue
        observed = _finite(item.get("observed"))
        threshold = _finite(item.get("threshold"))
        thresholds[gate] = threshold
        margins.append({
            "gate": gate,
            "observed": observed,
            "threshold": threshold,
            "signed_delta_observed_minus_threshold": (
                None if observed is None or threshold is None else observed - threshold
            ),
        })
        if gate not in gates:
            gates.append(gate)
    min_effective_rr = _finite(row.get("min_effective_rr"))
    if min_effective_rr is not None:
        thresholds.setdefault("MIN_EFFECTIVE_RR", min_effective_rr)
    return gates, thresholds, margins


def _geometry_complete(side: Any, entry: Any, stop: Any, target: Any) -> bool:
    side_u = str(side or "").upper()
    entry_f, stop_f, target_f = _finite(entry), _finite(stop), _finite(target)
    if side_u not in {"LONG", "SHORT"} or None in {entry_f, stop_f, target_f}:
        return False
    if side_u == "LONG":
        return bool(stop_f < entry_f < target_f)
    return bool(target_f < entry_f < stop_f)


def _shadow_eligibility(
    envelope: Mapping[str, Any],
    diagnostics: Mapping[str, Any],
    pending: Mapping[str, Any] | None,
) -> tuple[str, str, bool, bool]:
    has_any_geometry = any(
        envelope.get(name) is not None for name in ("side", "entry", "stop", "target")
    )
    geometry_ok = _geometry_complete(
        envelope.get("side"), envelope.get("entry"), envelope.get("stop"), envelope.get("target")
    )
    if not geometry_ok:
        return (
            ("DIAGNOSTIC_SHADOW", "INCOMPLETE_CANONICAL_GEOMETRY")
            if has_any_geometry
            else ("NON_SIMULATABLE", "CANONICAL_GEOMETRY_UNAVAILABLE")
        ) + (False, False)

    if pending is None:
        return "DIAGNOSTIC_SHADOW", "CANONICAL_REJECT_LINK_UNAVAILABLE", False, False

    provenance = _json_object(pending.get("source_provenance_json"))
    subject = str(
        provenance.get("forward_label_subject")
        or diagnostics.get("forward_label_subject")
        or ""
    ).upper()
    attributable = (
        provenance.get("reject_quality_attributable") is not False
        and diagnostics.get("reject_quality_attributable") is not False
        and subject != "LEGACY_SCANNER_SHADOW_CANDIDATE"
    )
    basis = str(
        provenance.get("reject_execution_basis")
        or diagnostics.get("reject_execution_basis")
        or ""
    ).upper()
    execution_aligned = basis == "EXPECTED_FILL_RUNTIME_PARITY"
    if not attributable:
        return "DIAGNOSTIC_SHADOW", "NON_ATTRIBUTABLE_OR_LEGACY_SHADOW", False, execution_aligned
    if not execution_aligned:
        return "DIAGNOSTIC_SHADOW", "NON_EXECUTION_ALIGNED_GEOMETRY", True, False
    return "EXECUTABLE_SHADOW", "CANONICAL_EXECUTION_ALIGNED_GEOMETRY", True, True


def _insert_envelope(
    audit: sqlite3.Connection,
    *,
    row: Mapping[str, Any],
    run_meta: Mapping[str, Any],
    campaign_id: str | None,
    observation: Mapping[str, Any] | None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    diagnostics = _json_object(row.get("diagnostics_json"))
    provenance = _json_object(observation.get("source_provenance_json")) if observation else {}
    gates, thresholds, margins = _gate_evidence(row, diagnostics)
    source_payload = {
        "decision_evidence": dict(row),
        "burnin_observation": dict(observation) if observation else None,
        "burnin_run": dict(run_meta),
    }
    source_hash = canonical_hash(source_payload)
    source_evidence_id = str(row.get("evidence_id") or f"decision-evidence-row:{row.get('id')}")
    existing = audit.execute(
        "SELECT envelope_id FROM audit_decision_envelopes "
        "WHERE source_evidence_id=? AND source_hash=?",
        (source_evidence_id, source_hash),
    ).fetchone()
    if existing:
        return str(existing[0]), diagnostics, dict(row)

    version = int(
        audit.execute(
            "SELECT COALESCE(MAX(source_version),0)+1 FROM audit_decision_envelopes "
            "WHERE source_evidence_id=?",
            (source_evidence_id,),
        ).fetchone()[0]
    )
    envelope_id = "aenv_" + canonical_hash([source_evidence_id, source_hash])[:28]
    decision = _normalized_decision(row.get("decision"))
    decision_id = str(
        diagnostics.get("reject_decision_id")
        or diagnostics.get("observation_id")
        or row.get("signal_id")
        or source_evidence_id
    )
    runtime_instance_id = (
        provenance.get("runtime_instance_id")
        or provenance.get("instance_id")
    )
    audit.execute(
        """INSERT INTO audit_decision_envelopes(
            envelope_id,source_evidence_id,source_hash,source_version,campaign_id,burnin_run_id,
            release_id,git_commit,config_hash,strategy_config_hash,runtime_instance_id,mode,
            decision_time,signal_id,decision_id,symbol,side,setup_type,regime,decision,reject_reason,
            score,confidence,raw_rr,effective_rr,min_effective_rr,entry,stop,target,spread_pct,
            expected_slippage_pct,fee_pct,funding_rate_pct,latency_ms,liquidity_score,volume_24h_usdt,
            volatility_regime,gate_outputs_json,gate_thresholds_json,gate_margins_json,
            decision_payload_json,source_provenance_json,ingested_at,schema_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            envelope_id, source_evidence_id, source_hash, version, campaign_id,
            str(row.get("run_id") or ""), run_meta.get("release_id"), run_meta.get("git_commit"),
            run_meta.get("config_hash"), run_meta.get("strategy_config_hash"), runtime_instance_id,
            str(row.get("mode") or run_meta.get("execution_mode") or "UNKNOWN").upper(),
            row.get("timestamp"), row.get("signal_id"), decision_id, row.get("symbol"), row.get("side"),
            row.get("setup_type"), row.get("regime"), decision, row.get("reject_reason"),
            _finite(row.get("score")), _finite(diagnostics.get("confidence")), _finite(row.get("raw_rr")),
            _finite(row.get("effective_rr")), _finite(row.get("min_effective_rr")),
            _finite(row.get("entry")), _finite(row.get("sl")), _finite(row.get("tp")),
            _finite(row.get("spread_pct")), _finite(row.get("expected_slippage_pct")),
            _finite(row.get("fee_pct")), _finite(row.get("funding_rate_pct")),
            _finite(row.get("latency_ms")), _finite(row.get("liquidity_score")),
            _finite(row.get("volume_24h_usdt")), row.get("volatility_regime"),
            json.dumps(gates, sort_keys=True), json.dumps(thresholds, sort_keys=True),
            json.dumps(margins, sort_keys=True), json.dumps(source_payload, sort_keys=True, default=str),
            json.dumps(provenance, sort_keys=True, default=str), utc_now(), SCHEMA_VERSION,
        ),
    )
    envelope = dict(
        envelope_id=envelope_id, source_evidence_id=source_evidence_id, source_hash=source_hash,
        campaign_id=campaign_id, burnin_run_id=str(row.get("run_id") or ""),
        decision=decision, signal_id=row.get("signal_id"), reject_reason=row.get("reject_reason"),
        side=row.get("side"), entry=row.get("entry"), stop=row.get("sl"), target=row.get("tp"),
        raw_rr=row.get("raw_rr"), effective_rr=row.get("effective_rr"),
    )
    return envelope_id, diagnostics, envelope


def _insert_outcome(
    audit: sqlite3.Connection,
    *,
    envelope_id: str,
    kind: str,
    source_outcome_id: str,
    payload: Mapping[str, Any],
    status: Any,
    gross_r: Any,
    net_r: Any,
    execution_cost: Any,
    expected_effective_rr: Any,
    realized_effective_rr: Any,
    mfe: Any,
    mae: Any,
    hold_duration_seconds: Any,
    ambiguous: bool,
    evidence_complete: bool,
    attributable: bool,
    execution_invalidated: bool,
    authoritative: bool,
    resolved_at: Any,
) -> str:
    source_hash = canonical_hash(dict(payload))
    snapshot_id = "aout_" + canonical_hash([kind, source_outcome_id, source_hash])[:28]
    audit.execute(
        """INSERT OR IGNORE INTO audit_outcomes(
            outcome_snapshot_id,envelope_id,outcome_kind,source_outcome_id,source_hash,outcome_status,
            gross_r,net_r,execution_cost,expected_effective_rr,realized_effective_rr,mfe,mae,
            hold_duration_seconds,ambiguous,evidence_complete,attributable,execution_invalidated,
            authoritative,resolved_at,outcome_payload_json,ingested_at,schema_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            snapshot_id,envelope_id,kind,source_outcome_id,source_hash,status,_finite(gross_r),
            _finite(net_r),_finite(execution_cost),_finite(expected_effective_rr),
            _finite(realized_effective_rr),_finite(mfe),_finite(mae),_finite(hold_duration_seconds),
            int(bool(ambiguous)),int(bool(evidence_complete)),int(bool(attributable)),
            int(bool(execution_invalidated)),int(bool(authoritative)),resolved_at,
            json.dumps(dict(payload),sort_keys=True,default=str),utc_now(),SCHEMA_VERSION,
        ),
    )
    return snapshot_id


def _accepted_outcome(
    source: sqlite3.Connection,
    *,
    envelope_id: str,
    row: Mapping[str, Any],
    run_id: str,
    audit: sqlite3.Connection,
) -> bool:
    if not (_table_exists(source, "burnin_pending_position_outcomes")
            and _table_exists(source, "burnin_trade_outcomes")):
        return False
    positions = [
        dict(value) for value in source.execute(
            "SELECT * FROM burnin_pending_position_outcomes "
            "WHERE burnin_run_id=? AND signal_id=? ORDER BY id",
            (run_id, row.get("signal_id")),
        )
    ]
    if len(positions) != 1:
        return False
    pending = positions[0]
    outcomes = [
        dict(value) for value in source.execute(
            "SELECT * FROM burnin_trade_outcomes WHERE burnin_run_id=? AND trade_id=?",
            (run_id, pending.get("trade_id")),
        )
    ]
    if len(outcomes) != 1:
        return False
    outcome = outcomes[0]
    payload = _json_object(outcome.get("payload_json"))
    attributable = (
        str(payload.get("signal_id") or "") == str(row.get("signal_id") or "")
        and str(payload.get("pending_position_id") or "") == str(pending.get("pending_position_id") or "")
    )
    missing = _json_list(outcome.get("missing_cost_fields_json"))
    ambiguous = bool(payload.get("ambiguous_intrabar_sequence")) or "ambiguous_intrabar_sequence" in missing
    complete = bool(outcome.get("evidence_complete")) and not ambiguous
    authoritative = attributable and complete
    _insert_outcome(
        audit,
        envelope_id=envelope_id,
        kind="ACCEPTED_ACTUAL",
        source_outcome_id=str(outcome.get("outcome_id")),
        payload={"outcome": outcome, "pending_position": pending},
        status=outcome.get("exit_reason"),
        gross_r=outcome.get("gross_r"),
        net_r=outcome.get("net_r"),
        execution_cost=outcome.get("total_execution_cost"),
        expected_effective_rr=outcome.get("effective_rr_at_entry"),
        realized_effective_rr=outcome.get("realized_effective_rr"),
        mfe=outcome.get("mfe"),
        mae=outcome.get("mae"),
        hold_duration_seconds=outcome.get("hold_duration_seconds"),
        ambiguous=ambiguous,
        evidence_complete=complete,
        attributable=attributable,
        execution_invalidated=False,
        authoritative=authoritative,
        resolved_at=outcome.get("closed_at"),
    )
    return True


def _reject_shadow(
    source: sqlite3.Connection,
    *,
    envelope_id: str,
    row: Mapping[str, Any],
    envelope: Mapping[str, Any],
    diagnostics: Mapping[str, Any],
    run_id: str,
    campaign_id: str | None,
    audit: sqlite3.Connection,
) -> tuple[bool, bool]:
    pending_rows: list[dict[str, Any]] = []
    if _table_exists(source, "burnin_pending_reject_labels"):
        pending_rows = [
            dict(value) for value in source.execute(
                "SELECT * FROM burnin_pending_reject_labels "
                "WHERE burnin_run_id=? AND signal_id=? ORDER BY id",
                (run_id, row.get("signal_id")),
            )
        ]
    pending = pending_rows[0] if len(pending_rows) == 1 else None
    eligibility, eligibility_reason, attributable, execution_aligned = _shadow_eligibility(
        envelope, diagnostics, pending
    )
    reject_reasons = _json_list(row.get("reject_flags")) or diagnostics.get("all_failed_gates") or []
    reject_decision_id = pending.get("reject_decision_id") if pending else diagnostics.get("reject_decision_id")
    shadow_decision_id = "ashd_" + canonical_hash([envelope_id, eligibility, reject_decision_id])[:28]
    authoritative_decision = eligibility == "EXECUTABLE_SHADOW" and attributable and execution_aligned
    audit.execute(
        """INSERT OR IGNORE INTO shadow_decisions(
            shadow_decision_id,envelope_id,source_reject_decision_id,eligibility,eligibility_reason,
            side,entry,stop,target,raw_rr,effective_rr,reject_reasons_json,execution_context_json,
            attributable,execution_aligned,authoritative,source_hash,ingested_at,schema_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            shadow_decision_id,envelope_id,reject_decision_id,eligibility,eligibility_reason,
            envelope.get("side"),_finite(envelope.get("entry")),_finite(envelope.get("stop")),
            _finite(envelope.get("target")),_finite(envelope.get("raw_rr")),
            _finite(envelope.get("effective_rr")),json.dumps(reject_reasons,sort_keys=True),
            json.dumps(diagnostics.get("execution_ctx") or {},sort_keys=True,default=str),
            int(attributable),int(execution_aligned),int(authoritative_decision),
            str(envelope.get("source_hash") or ""),utc_now(),SCHEMA_VERSION,
        ),
    )
    if pending is None or not _table_exists(source, "burnin_reject_outcomes"):
        return True, False

    outcomes = [
        dict(value) for value in source.execute(
            "SELECT * FROM burnin_reject_outcomes "
            "WHERE burnin_run_id=? AND reject_outcome_id=?",
            (run_id, "rout_" + str(pending.get("reject_decision_id"))),
        )
    ]
    if len(outcomes) != 1:
        return True, False
    outcome = outcomes[0]
    payload = _json_object(outcome.get("payload_json"))
    exact_identity = canonical_reject_outcome_link_matches(
        outcome,
        campaign_id=campaign_id or pending.get("campaign_id"),
        burnin_run_id=run_id,
        reject_decision_id=str(pending.get("reject_decision_id")),
        pending_label_id=str(pending.get("pending_label_id")),
    )
    subject = str(payload.get("forward_label_subject") or "").upper()
    outcome_attributable = (
        exact_identity
        and payload.get("reject_quality_attributable") is not False
        and subject != "LEGACY_SCANNER_SHADOW_CANDIDATE"
    )
    outcome_execution_aligned = (
        payload.get("execution_aligned") is True
        and str(payload.get("reject_execution_basis") or "").upper()
        == "EXPECTED_FILL_RUNTIME_PARITY"
    )
    ambiguous = bool(outcome.get("ambiguous"))
    complete = bool(outcome.get("evidence_complete")) and not ambiguous
    invalidated = bool(outcome.get("execution_invalidated"))
    authoritative = (
        authoritative_decision and outcome_attributable and outcome_execution_aligned
        and complete and not invalidated
    )
    gross_r = _finite(outcome.get("hypothetical_gross_r"))
    net_r = _finite(outcome.get("hypothetical_net_r_after_costs"))
    total_cost_drag = (
        None if gross_r is None or net_r is None else gross_r - net_r
    )
    source_payload = {"outcome": outcome, "pending_reject": pending}
    _insert_outcome(
        audit,
        envelope_id=envelope_id,
        kind="REJECT_SHADOW",
        source_outcome_id=str(outcome.get("reject_outcome_id")),
        payload=source_payload,
        status=outcome.get("forward_label"),
        gross_r=gross_r,
        net_r=net_r,
        execution_cost=total_cost_drag,
        expected_effective_rr=payload.get("effective_rr_at_decision"),
        realized_effective_rr=net_r,
        mfe=payload.get("mfe_pct"),
        mae=payload.get("mae_pct"),
        hold_duration_seconds=None,
        ambiguous=ambiguous,
        evidence_complete=complete,
        attributable=outcome_attributable,
        execution_invalidated=invalidated,
        authoritative=authoritative,
        resolved_at=outcome.get("evidence_horizon"),
    )
    source_hash = canonical_hash(source_payload)
    shadow_outcome_id = "asho_" + canonical_hash(
        [shadow_decision_id, outcome.get("reject_outcome_id"), source_hash]
    )[:28]
    audit.execute(
        """INSERT OR IGNORE INTO shadow_outcomes(
            shadow_outcome_id,shadow_decision_id,source_outcome_id,source_hash,forward_label,
            gross_r,net_r,total_cost_drag,avoided_loss,missed_profit,ambiguous,evidence_complete,
            attributable,execution_invalidated,authoritative,resolved_at,evidence_json,ingested_at,
            schema_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            shadow_outcome_id,shadow_decision_id,str(outcome.get("reject_outcome_id")),source_hash,
            outcome.get("forward_label"),gross_r,net_r,total_cost_drag,_finite(outcome.get("avoided_loss")),
            _finite(outcome.get("missed_profit")),int(ambiguous),int(complete),int(outcome_attributable),
            int(invalidated),int(authoritative),outcome.get("evidence_horizon"),
            json.dumps(source_payload,sort_keys=True,default=str),utc_now(),SCHEMA_VERSION,
        ),
    )
    return True, True


def ingest_audit_evidence(
    source: sqlite3.Connection,
    audit: sqlite3.Connection,
    *,
    campaign_id: str | None = None,
    burnin_run_id: str | None = None,
) -> dict[str, int]:
    """Copy canonical evidence into an isolated append-only audit database.

    The source connection is SELECT-only in this function. Re-running the same
    source snapshot is idempotent. If an upstream canonical row changes later,
    a new immutable source-hash version is appended rather than rewriting the
    prior audit evidence.
    """
    source.row_factory = sqlite3.Row
    audit.row_factory = sqlite3.Row
    _assert_isolated(source, audit)
    run_ids = _run_ids(source, campaign_id=campaign_id, burnin_run_id=burnin_run_id)
    run_meta = _run_metadata(source, run_ids)
    campaign_map = _campaign_by_run(source, run_ids)
    observations = _observation_index(source, run_ids)
    rows = _decision_rows(source, run_ids)
    bootstrap_audit_schema(audit)

    before = {
        table: int(audit.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in IMMUTABLE_TABLES
    }
    for row in rows:
        run_id = str(row.get("run_id") or "")
        diagnostics = _json_object(row.get("diagnostics_json"))
        observation_id = diagnostics.get("observation_id")
        observation = observations.get(str(observation_id)) if observation_id else None
        resolved_campaign = (
            str(campaign_id) if campaign_id
            else diagnostics.get("campaign_id")
            or campaign_map.get(run_id)
        )
        envelope_id, diagnostics, envelope = _insert_envelope(
            audit,
            row=row,
            run_meta=run_meta.get(run_id, {}),
            campaign_id=str(resolved_campaign) if resolved_campaign else None,
            observation=observation,
        )
        if _normalized_decision(row.get("decision")) == "ACCEPT":
            _accepted_outcome(
                source, envelope_id=envelope_id, row=row, run_id=run_id, audit=audit
            )
        elif _normalized_decision(row.get("decision")) == "REJECT":
            _reject_shadow(
                source,
                envelope_id=envelope_id,
                row=row,
                envelope=envelope,
                diagnostics=diagnostics,
                run_id=run_id,
                campaign_id=str(resolved_campaign) if resolved_campaign else None,
                audit=audit,
            )
    audit.commit()
    after = {
        table: int(audit.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in IMMUTABLE_TABLES
    }
    return {
        "source_decisions": len(rows),
        "envelopes_added": after["audit_decision_envelopes"] - before["audit_decision_envelopes"],
        "outcomes_added": after["audit_outcomes"] - before["audit_outcomes"],
        "shadow_decisions_added": after["shadow_decisions"] - before["shadow_decisions"],
        "shadow_outcomes_added": after["shadow_outcomes"] - before["shadow_outcomes"],
        "total_envelopes": after["audit_decision_envelopes"],
        "total_outcomes": after["audit_outcomes"],
    }
