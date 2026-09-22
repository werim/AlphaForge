"""Level 1 diagnosis for the isolated System Audit Fabric.

Consumes only the separate audit store (plus an optional read-only operational
source ingestion step). It never participates in order flow or mutates trading
configuration. Per-decision matrix cells are observations; policy/systemic
findings require explicit evidence and cohort support where appropriate.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from statistics import mean, stdev
from typing import Any, Mapping, Sequence

from alphaforge.burnin import canonical_hash, utc_now
from alphaforge.system_audit_store import bootstrap_audit_schema

SCHEMA_VERSION = "system_audit_diag_v1"
SYSTEMIC_MIN_SAMPLE = 30
SEVERITIES = ("INFO", "WATCH", "WARNING", "CRITICAL", "BLOCKING")

DDL = (
    """CREATE TABLE IF NOT EXISTS audit_analysis_runs(
        audit_run_id TEXT PRIMARY KEY,evidence_hash TEXT NOT NULL UNIQUE,
        decision_count INTEGER NOT NULL,outcome_count INTEGER NOT NULL,
        operational_snapshot_count INTEGER NOT NULL,generated_at TEXT NOT NULL,
        schema_version TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS audit_decision_classifications(
        classification_id TEXT PRIMARY KEY,audit_run_id TEXT NOT NULL,envelope_id TEXT NOT NULL,
        outcome_snapshot_id TEXT,observed_cell TEXT NOT NULL,outcome_kind TEXT,net_r REAL,
        value_contribution REAL,expected_vs_realized_r_delta REAL,fill_model_error_pct REAL,
        attribution_json TEXT NOT NULL,evidence_json TEXT NOT NULL,created_at TEXT NOT NULL,
        schema_version TEXT NOT NULL,UNIQUE(audit_run_id,envelope_id))""",
    """CREATE TABLE IF NOT EXISTS audit_cohort_stats(
        cohort_stat_id TEXT PRIMARY KEY,audit_run_id TEXT NOT NULL,dimension TEXT NOT NULL,
        cohort_key TEXT NOT NULL,sample_count INTEGER NOT NULL,positive_count INTEGER NOT NULL,
        negative_count INTEGER NOT NULL,mean_net_r REAL,total_net_r REAL,
        lower_confidence_bound REAL,upper_confidence_bound REAL,
        mean_expected_vs_realized_r_delta REAL,mean_abs_fill_model_error_pct REAL,
        created_at TEXT NOT NULL,schema_version TEXT NOT NULL,
        UNIQUE(audit_run_id,dimension,cohort_key))""",
    """CREATE TABLE IF NOT EXISTS audit_system_metrics(
        audit_run_id TEXT PRIMARY KEY,classified_decisions INTEGER NOT NULL,
        good_accept_count INTEGER NOT NULL,false_accept_count INTEGER NOT NULL,
        good_reject_count INTEGER NOT NULL,false_reject_count INTEGER NOT NULL,
        unresolved_count INTEGER NOT NULL,good_accept_value REAL NOT NULL,
        false_accept_cost REAL NOT NULL,good_reject_protection REAL NOT NULL,
        false_reject_opportunity_cost REAL NOT NULL,decision_policy_net_value REAL NOT NULL,
        created_at TEXT NOT NULL,schema_version TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS audit_findings(
        finding_id TEXT PRIMARY KEY,audit_run_id TEXT NOT NULL,subsystem TEXT NOT NULL,
        finding_type TEXT NOT NULL,severity TEXT NOT NULL,envelope_id TEXT,
        cohort_dimension TEXT,cohort_key TEXT,evidence_json TEXT NOT NULL,
        explanation TEXT NOT NULL,created_at TEXT NOT NULL,schema_version TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS ix_audit_findings_run_severity
       ON audit_findings(audit_run_id,severity,subsystem)""",
    """CREATE TABLE IF NOT EXISTS audit_operational_snapshots(
        snapshot_id TEXT PRIMARY KEY,burnin_run_id TEXT,source_table TEXT NOT NULL,
        source_identity TEXT NOT NULL,source_hash TEXT NOT NULL,runtime_instance_id TEXT,
        observed_at TEXT,status TEXT,payload_json TEXT NOT NULL,ingested_at TEXT NOT NULL,
        schema_version TEXT NOT NULL,UNIQUE(source_table,source_identity,source_hash))""",
)

ANALYSIS_TABLES = (
    "audit_analysis_runs","audit_decision_classifications","audit_cohort_stats",
    "audit_system_metrics","audit_findings","audit_operational_snapshots",
)


def _trigger(table: str, operation: str) -> str:
    return f"""CREATE TRIGGER IF NOT EXISTS trg_{table}_no_{operation.lower()}
        BEFORE {operation} ON {table}
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_SYSTEM_AUDIT_ANALYSIS'); END"""


def bootstrap_diagnostics_schema(conn: sqlite3.Connection) -> None:
    bootstrap_audit_schema(conn)
    for statement in DDL:
        conn.execute(statement)
    for table in ANALYSIS_TABLES:
        conn.execute(_trigger(table, "UPDATE"))
        conn.execute(_trigger(table, "DELETE"))


def _json(raw: Any, default: Any) -> Any:
    if isinstance(raw, type(default)):
        return raw
    try:
        value = json.loads(raw if raw not in (None, "") else json.dumps(default))
    except (TypeError, json.JSONDecodeError):
        return default
    return value if isinstance(value, type(default)) else default


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _database_files(conn: sqlite3.Connection) -> set[str]:
    return {str(row[2]) for row in conn.execute("PRAGMA database_list") if row[2]}


def _assert_source_isolated(source: sqlite3.Connection, audit: sqlite3.Connection) -> None:
    if source is audit or _database_files(source) & _database_files(audit):
        raise ValueError("operational source and audit output must be separate")


def ingest_operational_evidence(
    source: sqlite3.Connection,
    audit: sqlite3.Connection,
    *,
    burnin_run_id: str,
) -> dict[str, int]:
    """Copy persisted runtime evidence without writing to the source database."""
    source.row_factory = sqlite3.Row
    audit.row_factory = sqlite3.Row
    _assert_source_isolated(source, audit)
    bootstrap_diagnostics_schema(audit)
    before = int(audit.execute("SELECT COUNT(*) FROM audit_operational_snapshots").fetchone()[0])
    instance_ids: set[str] = set()

    if _table_exists(source, "runtime_state_snapshots"):
        cols = _columns(source, "runtime_state_snapshots")
        if "burnin_run_id" in cols:
            for row in source.execute(
                "SELECT * FROM runtime_state_snapshots WHERE burnin_run_id=? ORDER BY id",
                (burnin_run_id,),
            ):
                payload = dict(row)
                instance = str(payload.get("instance_id") or "")
                if instance:
                    instance_ids.add(instance)
                _store_operational(
                    audit, burnin_run_id, "runtime_state_snapshots", payload,
                    instance_column="instance_id", time_column="timestamp",
                    status_column="runtime_status",
                )

    if not instance_ids:
        instance_ids = {
            str(row[0]) for row in audit.execute(
                "SELECT DISTINCT runtime_instance_id FROM audit_decision_envelopes "
                "WHERE burnin_run_id=? AND runtime_instance_id IS NOT NULL",
                (burnin_run_id,),
            ) if row[0]
        }

    for table, instance_column, time_column, status_column in (
        ("runtime_heartbeats", "runtime_instance_id", "heartbeat_ts", "runtime_state"),
        ("exchange_reconciliation_events", "instance_id", "event_ts", "status"),
    ):
        if not instance_ids or not _table_exists(source, table):
            continue
        cols = _columns(source, table)
        if instance_column not in cols:
            continue
        placeholders = ",".join("?" for _ in instance_ids)
        for row in source.execute(
            f"SELECT * FROM {table} WHERE {instance_column} IN ({placeholders}) ORDER BY id",
            tuple(sorted(instance_ids)),
        ):
            _store_operational(
                audit, burnin_run_id, table, dict(row),
                instance_column=instance_column, time_column=time_column,
                status_column=status_column,
            )
    audit.commit()
    after = int(audit.execute("SELECT COUNT(*) FROM audit_operational_snapshots").fetchone()[0])
    return {"operational_snapshots_added": after-before, "total_operational_snapshots": after}


def _store_operational(
    audit: sqlite3.Connection,
    burnin_run_id: str,
    table: str,
    payload: Mapping[str, Any],
    *,
    instance_column: str,
    time_column: str,
    status_column: str,
) -> None:
    identity = str(payload.get("id") or canonical_hash(dict(payload)))
    source_hash = canonical_hash(dict(payload))
    audit.execute(
        """INSERT OR IGNORE INTO audit_operational_snapshots(
            snapshot_id,burnin_run_id,source_table,source_identity,source_hash,
            runtime_instance_id,observed_at,status,payload_json,ingested_at,schema_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "aops_"+canonical_hash([table,identity,source_hash])[:28],
            burnin_run_id,table,identity,source_hash,payload.get(instance_column),
            payload.get(time_column),payload.get(status_column),
            json.dumps(dict(payload),sort_keys=True,default=str),utc_now(),SCHEMA_VERSION,
        ),
    )


def _latest_envelopes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute("""
        SELECT e.* FROM audit_decision_envelopes e
        JOIN (
          SELECT source_evidence_id,MAX(source_version) max_version
          FROM audit_decision_envelopes GROUP BY source_evidence_id
        ) v ON v.source_evidence_id=e.source_evidence_id AND v.max_version=e.source_version
        ORDER BY e.decision_time,e.envelope_id
    """)]


def _latest_outcome(conn: sqlite3.Connection, envelope_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM audit_outcomes WHERE envelope_id=? ORDER BY rowid DESC LIMIT 1",
        (envelope_id,),
    ).fetchone()
    return dict(row) if row else None


def _latest_operational(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table_exists(conn, "audit_operational_snapshots"):
        return []
    return [dict(row) for row in conn.execute("""
        SELECT o.* FROM audit_operational_snapshots o
        JOIN (
          SELECT source_table,MAX(rowid) max_rowid
          FROM audit_operational_snapshots GROUP BY source_table
        ) v ON v.max_rowid=o.rowid
        ORDER BY o.source_table
    """)]


def _confidence(values: Sequence[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    avg = mean(values)
    if len(values) < 2:
        return avg, avg
    se = stdev(values) / math.sqrt(len(values))
    return avg - 1.96*se, avg + 1.96*se


def _normalize_weights(raw: Mapping[str, float]) -> dict[str, float]:
    positive = {k:max(0.0,float(v)) for k,v in raw.items() if float(v)>0}
    total = sum(positive.values())
    if total <= 0:
        return {"UNEXPLAINED_VARIANCE":1.0}
    return {k:round(v/total,6) for k,v in sorted(positive.items())}


def _classification(
    envelope: Mapping[str, Any],
    outcome: Mapping[str, Any] | None,
) -> dict[str, Any]:
    decision = str(envelope.get("decision") or "").upper()
    authoritative = bool(outcome and outcome.get("authoritative"))
    net_r = _finite(outcome.get("net_r")) if outcome else None
    if not authoritative or net_r is None:
        cell = "UNRESOLVED"
    elif decision == "ACCEPT":
        cell = "GOOD_ACCEPT" if net_r >= 0 else "FALSE_ACCEPT"
    elif decision == "REJECT":
        cell = "FALSE_REJECT" if net_r > 0 else "GOOD_REJECT"
    else:
        cell = "UNRESOLVED"

    expected = _finite(outcome.get("expected_effective_rr")) if outcome else None
    realized = _finite(outcome.get("realized_effective_rr")) if outcome else None
    r_delta = None if expected is None or realized is None else realized-expected
    fill_error = None
    if outcome and str(outcome.get("outcome_kind")) == "ACCEPTED_ACTUAL":
        payload = _json(outcome.get("outcome_payload_json"), {})
        pending = payload.get("pending_position") if isinstance(payload.get("pending_position"),dict) else {}
        expected_fill = _finite(envelope.get("entry"))
        simulated_fill = _finite(pending.get("simulated_fill"))
        if expected_fill not in (None,0.0) and simulated_fill is not None:
            fill_error = (simulated_fill-expected_fill)/expected_fill

    attribution: dict[str,float] = {}
    if cell in {"GOOD_REJECT","FALSE_REJECT"}:
        gates = [str(x).upper() for x in _json(envelope.get("gate_outputs_json"),[]) if str(x)]
        primary = str(envelope.get("reject_reason") or "").upper()
        if primary:
            attribution[primary] = 1.0
        for gate in gates:
            attribution[gate] = max(attribution.get(gate,0.0), 1.0 if gate==primary else 0.5)
    elif cell == "FALSE_ACCEPT":
        payload = _json(envelope.get("decision_payload_json"), {})
        decision_row = payload.get("decision_evidence") if isinstance(payload.get("decision_evidence"),dict) else {}
        risk_flags = [str(x).upper() for x in _json(decision_row.get("risk_flags"),[]) if str(x)]
        if fill_error is not None and abs(fill_error) > 1e-9:
            attribution["EXECUTION_FILL_MODEL_ERROR"] = max(abs(fill_error),0.05)
        for flag in risk_flags:
            attribution[f"RISK:{flag}"] = 0.5
        if not attribution:
            attribution["UNEXPLAINED_VARIANCE"] = 1.0
    else:
        attribution["OBSERVED_OUTCOME_ONLY"] = 1.0

    value = 0.0
    if cell == "GOOD_ACCEPT" and net_r is not None:
        value = max(net_r,0.0)
    elif cell == "FALSE_ACCEPT" and net_r is not None:
        value = -abs(min(net_r,0.0))
    elif cell == "GOOD_REJECT" and net_r is not None:
        value = abs(min(net_r,0.0))
    elif cell == "FALSE_REJECT" and net_r is not None:
        value = -max(net_r,0.0)

    return {
        "observed_cell":cell,"net_r":net_r,"value_contribution":value,
        "expected_vs_realized_r_delta":r_delta,"fill_model_error_pct":fill_error,
        "attribution":_normalize_weights(attribution),
    }


def _add_finding(
    conn: sqlite3.Connection, *, audit_run_id: str, subsystem: str,
    finding_type: str, severity: str, explanation: str,
    envelope_id: str | None=None, cohort_dimension: str | None=None,
    cohort_key: str | None=None, evidence: Mapping[str,Any] | None=None,
) -> None:
    if severity not in SEVERITIES:
        raise ValueError("unsupported audit severity")
    evidence_payload = dict(evidence or {})
    finding_id = "afnd_"+canonical_hash([
        audit_run_id,subsystem,finding_type,envelope_id,cohort_dimension,cohort_key,evidence_payload
    ])[:28]
    conn.execute(
        """INSERT OR IGNORE INTO audit_findings(
            finding_id,audit_run_id,subsystem,finding_type,severity,envelope_id,
            cohort_dimension,cohort_key,evidence_json,explanation,created_at,schema_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            finding_id,audit_run_id,subsystem,finding_type,severity,envelope_id,
            cohort_dimension,cohort_key,json.dumps(evidence_payload,sort_keys=True,default=str),
            explanation,utc_now(),SCHEMA_VERSION,
        ),
    )


def _data_findings(conn: sqlite3.Connection, audit_run_id: str, envelopes: Sequence[Mapping[str,Any]]) -> None:
    for e in envelopes:
        payload = _json(e.get("decision_payload_json"), {})
        observation = payload.get("burnin_observation") if isinstance(payload.get("burnin_observation"),dict) else None
        missing = [
            name for name,value in (
                ("decision_time",e.get("decision_time")),("git_commit",e.get("git_commit")),
                ("config_hash",e.get("config_hash")),("strategy_config_hash",e.get("strategy_config_hash")),
            ) if value in (None,"")
        ]
        if missing:
            _add_finding(
                conn,audit_run_id=audit_run_id,subsystem="DATA",
                finding_type="DECISION_LINEAGE_INCOMPLETE",severity="BLOCKING",
                envelope_id=e.get("envelope_id"),evidence={"missing":missing},
                explanation="Immutable decision-time lineage is incomplete; causal audit fails closed for this decision.",
            )
        if observation is None:
            _add_finding(
                conn,audit_run_id=audit_run_id,subsystem="DATA",
                finding_type="BURNIN_OBSERVATION_NOT_LINKED",severity="WATCH",
                envelope_id=e.get("envelope_id"),
                explanation="No burn-in observation snapshot is linked; audit is limited to decision_evidence.",
            )
        elif int(observation.get("evidence_complete") or 0) != 1:
            _add_finding(
                conn,audit_run_id=audit_run_id,subsystem="DATA",
                finding_type="DECISION_EVIDENCE_INCOMPLETE",severity="BLOCKING",
                envelope_id=e.get("envelope_id"),
                evidence={"missing_fields":_json(observation.get("missing_fields_json"),[])},
                explanation="Canonical decision observation explicitly reports incomplete evidence.",
            )


def _risk_findings(conn: sqlite3.Connection, audit_run_id: str, envelopes: Sequence[Mapping[str,Any]]) -> None:
    for e in envelopes:
        if str(e.get("decision") or "").upper() != "ACCEPT":
            continue
        payload = _json(e.get("decision_payload_json"), {})
        row = payload.get("decision_evidence") if isinstance(payload.get("decision_evidence"),dict) else {}
        checks = (
            ("open_position_count","max_open_positions","ACCEPTED_OVER_POSITION_LIMIT"),
            ("total_notional_exposure","max_notional_exposure","ACCEPTED_OVER_NOTIONAL_LIMIT"),
            ("symbol_notional_exposure","max_symbol_notional","ACCEPTED_OVER_SYMBOL_NOTIONAL_LIMIT"),
            ("daily_loss_pct","max_daily_loss_pct","ACCEPTED_AFTER_DAILY_LOSS_LIMIT"),
        )
        had_risk_evidence = False
        for observed_key,limit_key,finding in checks:
            observed,limit = _finite(row.get(observed_key)),_finite(row.get(limit_key))
            had_risk_evidence = had_risk_evidence or observed is not None or limit is not None
            if observed is not None and limit is not None and observed > limit:
                _add_finding(
                    conn,audit_run_id=audit_run_id,subsystem="RISK",finding_type=finding,
                    severity="CRITICAL",envelope_id=e.get("envelope_id"),
                    evidence={"observed":observed,"limit":limit},
                    explanation="Accepted decision exceeded a persisted decision-time portfolio risk limit.",
                )
        flags = [str(x).upper() for x in _json(row.get("risk_flags"),[]) if str(x)]
        if flags:
            had_risk_evidence = True
            _add_finding(
                conn,audit_run_id=audit_run_id,subsystem="RISK",
                finding_type="ACCEPTED_WITH_RISK_FLAGS",severity="WARNING",
                envelope_id=e.get("envelope_id"),evidence={"risk_flags":flags},
                explanation="Accepted decision carried persisted portfolio risk flags.",
            )
        if not had_risk_evidence and not row.get("portfolio_risk_state"):
            _add_finding(
                conn,audit_run_id=audit_run_id,subsystem="RISK",
                finding_type="PORTFOLIO_RISK_EVIDENCE_MISSING",severity="WATCH",
                envelope_id=e.get("envelope_id"),
                explanation="Accepted decision has no decision-time portfolio risk evidence.",
            )


def _operational_findings(
    conn: sqlite3.Connection, audit_run_id: str, operational: Sequence[Mapping[str,Any]]
) -> None:
    if not operational:
        _add_finding(
            conn,audit_run_id=audit_run_id,subsystem="RELIABILITY",
            finding_type="OPERATIONAL_EVIDENCE_MISSING",severity="WATCH",
            explanation="No isolated runtime/heartbeat/reconciliation snapshot was ingested; reliability is unknown.",
        )
        return
    by_table = {str(row.get("source_table")):row for row in operational}
    state = by_table.get("runtime_state_snapshots")
    if state:
        payload = _json(state.get("payload_json"), {})
        if bool(payload.get("recovery_action_required")):
            _add_finding(
                conn,audit_run_id=audit_run_id,subsystem="RELIABILITY",
                finding_type="RUNTIME_RECOVERY_REQUIRED",severity="CRITICAL",
                evidence={"fail_closed_reason":payload.get("fail_closed_reason")},
                explanation="Persisted runtime state requires recovery.",
            )
        if payload.get("fail_closed_reason"):
            _add_finding(
                conn,audit_run_id=audit_run_id,subsystem="RELIABILITY",
                finding_type="RUNTIME_FAIL_CLOSED",severity="CRITICAL",
                evidence={"reason":payload.get("fail_closed_reason")},
                explanation="Runtime persisted a fail-closed reason.",
            )
        orphan_orders = int(payload.get("orphan_order_count") or 0)
        orphan_positions = int(payload.get("orphan_position_count") or 0)
        if orphan_orders or orphan_positions:
            _add_finding(
                conn,audit_run_id=audit_run_id,subsystem="RELIABILITY",
                finding_type="ORPHAN_EXPOSURE",severity="CRITICAL",
                evidence={"orphan_orders":orphan_orders,"orphan_positions":orphan_positions},
                explanation="Runtime state contains orphan order/position evidence.",
            )
        stale = _json(payload.get("stale_market_data_symbols"), [])
        if stale:
            _add_finding(
                conn,audit_run_id=audit_run_id,subsystem="DATA",
                finding_type="STALE_MARKET_DATA",severity="BLOCKING",
                evidence={"symbols":stale},
                explanation="Persisted runtime state explicitly reports stale market data.",
            )
        if bool(payload.get("unknown_exchange_state")):
            _add_finding(
                conn,audit_run_id=audit_run_id,subsystem="RELIABILITY",
                finding_type="UNKNOWN_EXCHANGE_STATE",severity="CRITICAL",
                explanation="Exchange state was persisted as unknown.",
            )
    recon = by_table.get("exchange_reconciliation_events")
    if recon and str(recon.get("status") or "").upper() not in {"CLEAN","PASS","OK"}:
        _add_finding(
            conn,audit_run_id=audit_run_id,subsystem="EXECUTION",
            finding_type="RECONCILIATION_NOT_CLEAN",severity="CRITICAL",
            evidence={"status":recon.get("status")},
            explanation="Latest persisted reconciliation snapshot is not clean.",
        )


def _cohort_groups(
    classifications: Sequence[Mapping[str,Any]],
    envelopes: Mapping[str,Mapping[str,Any]],
) -> dict[tuple[str,str],list[Mapping[str,Any]]]:
    groups: dict[tuple[str,str],list[Mapping[str,Any]]] = defaultdict(list)
    for c in classifications:
        if c.get("net_r") is None or c.get("observed_cell") == "UNRESOLVED":
            continue
        e = envelopes[str(c["envelope_id"])]
        dimensions = {
            "decision":str(e.get("decision") or "UNKNOWN"),
            "symbol":str(e.get("symbol") or "UNKNOWN"),
            "regime":str(e.get("regime") or "UNKNOWN"),
            "setup_type":str(e.get("setup_type") or "UNKNOWN"),
            "side":str(e.get("side") or "UNKNOWN"),
            "reject_reason":str(e.get("reject_reason") or "NONE"),
            "regime_setup":f"{e.get('regime') or 'UNKNOWN'}|{e.get('setup_type') or 'UNKNOWN'}",
        }
        for dimension,key in dimensions.items():
            groups[(dimension,key)].append(c)
    return groups


def _persist_cohorts(
    conn: sqlite3.Connection, audit_run_id: str,
    classifications: Sequence[Mapping[str,Any]],
    envelopes: Mapping[str,Mapping[str,Any]],
) -> None:
    for (dimension,key),rows in sorted(_cohort_groups(classifications,envelopes).items()):
        values = [float(r["net_r"]) for r in rows if r.get("net_r") is not None]
        lower,upper = _confidence(values)
        deltas = [float(r["expected_vs_realized_r_delta"]) for r in rows
                  if r.get("expected_vs_realized_r_delta") is not None]
        fill_errors = [abs(float(r["fill_model_error_pct"])) for r in rows
                       if r.get("fill_model_error_pct") is not None]
        stat_id = "acoh_"+canonical_hash([audit_run_id,dimension,key,values,deltas,fill_errors])[:28]
        conn.execute(
            """INSERT OR IGNORE INTO audit_cohort_stats(
                cohort_stat_id,audit_run_id,dimension,cohort_key,sample_count,positive_count,
                negative_count,mean_net_r,total_net_r,lower_confidence_bound,upper_confidence_bound,
                mean_expected_vs_realized_r_delta,mean_abs_fill_model_error_pct,created_at,schema_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                stat_id,audit_run_id,dimension,key,len(values),sum(v>=0 for v in values),
                sum(v<0 for v in values),mean(values) if values else None,sum(values),lower,upper,
                mean(deltas) if deltas else None,mean(fill_errors) if fill_errors else None,
                utc_now(),SCHEMA_VERSION,
            ),
        )
        if len(values) < SYSTEMIC_MIN_SAMPLE:
            continue
        avg = mean(values)
        if upper is not None and upper < 0:
            _add_finding(
                conn,audit_run_id=audit_run_id,subsystem="EXPECTANCY",
                finding_type="CONFIDENT_NEGATIVE_EXPECTANCY_COHORT",severity="WARNING",
                cohort_dimension=dimension,cohort_key=key,
                evidence={"sample_count":len(values),"mean_net_r":avg,"lower":lower,"upper":upper},
                explanation="Comparable cohort has confidently negative post-cost expectancy; this is not a one-trade verdict.",
            )
        elif avg < 0:
            _add_finding(
                conn,audit_run_id=audit_run_id,subsystem="EXPECTANCY",
                finding_type="NEGATIVE_EXPECTANCY_COHORT",severity="WATCH",
                cohort_dimension=dimension,cohort_key=key,
                evidence={"sample_count":len(values),"mean_net_r":avg,"lower":lower,"upper":upper},
                explanation="Comparable cohort mean is negative but uncertainty still matters.",
            )
        if fill_errors and mean(fill_errors) > 0.001:
            _add_finding(
                conn,audit_run_id=audit_run_id,subsystem="EXECUTION",
                finding_type="SYSTEMATIC_FILL_MODEL_ERROR",severity="WARNING",
                cohort_dimension=dimension,cohort_key=key,
                evidence={"sample_count":len(fill_errors),"mean_abs_fill_error_pct":mean(fill_errors)},
                explanation="Expected-fill versus simulated/actual-fill evidence shows systematic model error.",
            )


def run_system_diagnostics(conn: sqlite3.Connection) -> dict[str,Any]:
    """Build deterministic Level 1 diagnosis from isolated immutable evidence."""
    conn.row_factory = sqlite3.Row
    bootstrap_diagnostics_schema(conn)
    envelopes = _latest_envelopes(conn)
    operational = _latest_operational(conn)
    outcome_by_envelope: dict[str,dict[str,Any]] = {}
    outcomes: list[dict[str,Any]] = []
    for envelope in envelopes:
        outcome = _latest_outcome(conn,str(envelope["envelope_id"]))
        if outcome:
            outcome_by_envelope[str(envelope["envelope_id"])] = outcome
            outcomes.append(outcome)

    evidence_hash = canonical_hash({
        "envelopes":[(e["envelope_id"],e["source_hash"],e["source_version"]) for e in envelopes],
        "outcomes":[(o["outcome_snapshot_id"],o["source_hash"],o["authoritative"]) for o in outcomes],
        "operational":[(o["snapshot_id"],o["source_hash"]) for o in operational],
    })
    audit_run_id = "arun_"+evidence_hash[:28]
    conn.execute(
        """INSERT OR IGNORE INTO audit_analysis_runs(
            audit_run_id,evidence_hash,decision_count,outcome_count,operational_snapshot_count,
            generated_at,schema_version) VALUES (?,?,?,?,?,?,?)""",
        (audit_run_id,evidence_hash,len(envelopes),len(outcomes),len(operational),utc_now(),SCHEMA_VERSION),
    )

    classifications: list[dict[str,Any]] = []
    counts = {name:0 for name in ("GOOD_ACCEPT","FALSE_ACCEPT","GOOD_REJECT","FALSE_REJECT","UNRESOLVED")}
    values = {
        "good_accept_value":0.0,"false_accept_cost":0.0,
        "good_reject_protection":0.0,"false_reject_opportunity_cost":0.0,
    }
    for envelope in envelopes:
        outcome = outcome_by_envelope.get(str(envelope["envelope_id"]))
        classified = _classification(envelope,outcome)
        classification_id = "acls_"+canonical_hash([
            audit_run_id,envelope["envelope_id"],
            outcome.get("outcome_snapshot_id") if outcome else None,classified,
        ])[:28]
        evidence = {
            "decision":envelope.get("decision"),"reject_reason":envelope.get("reject_reason"),
            "outcome_status":outcome.get("outcome_status") if outcome else None,
            "authoritative":bool(outcome and outcome.get("authoritative")),
        }
        conn.execute(
            """INSERT OR IGNORE INTO audit_decision_classifications(
                classification_id,audit_run_id,envelope_id,outcome_snapshot_id,observed_cell,
                outcome_kind,net_r,value_contribution,expected_vs_realized_r_delta,
                fill_model_error_pct,attribution_json,evidence_json,created_at,schema_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                classification_id,audit_run_id,envelope["envelope_id"],
                outcome.get("outcome_snapshot_id") if outcome else None,
                classified["observed_cell"],outcome.get("outcome_kind") if outcome else None,
                classified["net_r"],classified["value_contribution"],
                classified["expected_vs_realized_r_delta"],classified["fill_model_error_pct"],
                json.dumps(classified["attribution"],sort_keys=True),
                json.dumps(evidence,sort_keys=True),utc_now(),SCHEMA_VERSION,
            ),
        )
        row = {"envelope_id":envelope["envelope_id"],**classified}
        classifications.append(row)
        cell = classified["observed_cell"]
        counts[cell] += 1
        net_r = classified["net_r"]
        if cell=="GOOD_ACCEPT" and net_r is not None: values["good_accept_value"] += max(net_r,0.0)
        elif cell=="FALSE_ACCEPT" and net_r is not None: values["false_accept_cost"] += abs(min(net_r,0.0))
        elif cell=="GOOD_REJECT" and net_r is not None: values["good_reject_protection"] += abs(min(net_r,0.0))
        elif cell=="FALSE_REJECT" and net_r is not None: values["false_reject_opportunity_cost"] += max(net_r,0.0)

    dpv = (
        values["good_accept_value"]+values["good_reject_protection"]
        -values["false_accept_cost"]-values["false_reject_opportunity_cost"]
    )
    conn.execute(
        """INSERT OR IGNORE INTO audit_system_metrics(
            audit_run_id,classified_decisions,good_accept_count,false_accept_count,
            good_reject_count,false_reject_count,unresolved_count,good_accept_value,
            false_accept_cost,good_reject_protection,false_reject_opportunity_cost,
            decision_policy_net_value,created_at,schema_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            audit_run_id,len(classifications),counts["GOOD_ACCEPT"],counts["FALSE_ACCEPT"],
            counts["GOOD_REJECT"],counts["FALSE_REJECT"],counts["UNRESOLVED"],
            values["good_accept_value"],values["false_accept_cost"],
            values["good_reject_protection"],values["false_reject_opportunity_cost"],
            dpv,utc_now(),SCHEMA_VERSION,
        ),
    )

    envelope_map = {str(e["envelope_id"]):e for e in envelopes}
    _data_findings(conn,audit_run_id,envelopes)
    _risk_findings(conn,audit_run_id,envelopes)
    _operational_findings(conn,audit_run_id,operational)
    _persist_cohorts(conn,audit_run_id,classifications,envelope_map)
    if any(c["observed_cell"] in {"GOOD_ACCEPT","FALSE_ACCEPT"} for c in classifications):
        _add_finding(
            conn,audit_run_id=audit_run_id,subsystem="POSITION_MANAGEMENT",
            finding_type="COUNTERFACTUAL_MANAGEMENT_EVIDENCE_NOT_INGESTED",severity="INFO",
            explanation="Entry/outcome evidence is auditable, but trailing/partial-exit counterfactuals are absent; no management policy claim is made.",
        )
    conn.commit()
    finding_count = int(conn.execute(
        "SELECT COUNT(*) FROM audit_findings WHERE audit_run_id=?",(audit_run_id,)
    ).fetchone()[0])
    return {
        "status":"PASS","audit_run_id":audit_run_id,"decision_count":len(envelopes),
        "outcome_count":len(outcomes),"finding_count":finding_count,
        "observed_matrix":{**counts},"decision_policy_net_value":round(dpv,10),
    }


def safe_run_system_diagnostics(conn: sqlite3.Connection) -> dict[str,Any]:
    """Non-throwing audit wrapper; production runtime never depends on it."""
    try:
        return run_system_diagnostics(conn)
    except Exception as exc:
        return {"status":"ERROR","error_type":exc.__class__.__name__,"message":str(exc)}
