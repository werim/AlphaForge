from __future__ import annotations

import csv, hashlib, json, math, sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "phase7_burnin_v1"
ALLOWED_BURNIN_MODES = {"PAPER", "LIVE_PRECHECK"}
FORBIDDEN_PROVIDER_MARKERS = {"SYNTHETIC", "MOCK", "FAKE", "TEST_PROVIDER"}
REGIMES = {"TRENDING","MEAN_REVERTING","CHOPPY","PANIC","LOW_LIQUIDITY","BREAKOUT","SHORT_SQUEEZE","RANGE_COMPRESSION","NEWS_DRIVEN","UNKNOWN"}
CRITICAL_COST_FIELDS = ("spread_cost","entry_slippage_cost","exit_slippage_cost","fee_cost","funding_cost","latency_cost")
CANONICAL_DECISION_KIND = "CANONICAL_DECISION"
DIAGNOSTIC_OBSERVATION_KIND = "DIAGNOSTIC"
CANONICAL_REJECT_IDENTITY_MODE = "CANONICAL_LINK_REQUIRED"
LEGACY_REJECT_IDENTITY_MODE = "LEGACY_NO_IDENTITY_FALLBACK"

def qualification_reject_identity_mode(phases: Sequence[str], observation_metrics: Sequence[Any]) -> str:
    """Identify legacy evidence explicitly; uncertain/empty evidence fails closed."""
    if any(str(phase or "").upper() == "PHASE8" for phase in phases):
        return CANONICAL_REJECT_IDENTITY_MODE
    saw_observation = False
    for raw in observation_metrics:
        saw_observation = True
        try:
            metrics = json.loads(raw or "{}") if not isinstance(raw, Mapping) else dict(raw)
        except (TypeError, json.JSONDecodeError):
            return CANONICAL_REJECT_IDENTITY_MODE
        if any(metrics.get(key) is not None for key in (
            "observation_kind", "reject_decision_id", "signal_id", "setup_identity"
        )):
            return CANONICAL_REJECT_IDENTITY_MODE
    return LEGACY_REJECT_IDENTITY_MODE if saw_observation else CANONICAL_REJECT_IDENTITY_MODE

def canonical_decision_sql(alias: str = "") -> str:
    """SQL predicate separating decisions from auditable diagnostic observations."""
    prefix = f"{alias}." if alias else ""
    metrics = f"{prefix}metrics_json"
    observation_id = f"{prefix}observation_id"
    explicit_kind = (
        f"CASE WHEN json_valid(COALESCE({metrics}, '')) "
        f"THEN json_extract({metrics}, '$.observation_kind') END"
    )
    # Immutable rows created before observation_kind used this diagnostic ID.
    legacy_kind = (
        f"CASE WHEN {observation_id} LIKE 'incomplete_reject_geometry_%' "
        f"THEN '{DIAGNOSTIC_OBSERVATION_KIND}' ELSE '{CANONICAL_DECISION_KIND}' END"
    )
    kind = f"UPPER(COALESCE({explicit_kind}, {legacy_kind})) = '{CANONICAL_DECISION_KIND}'"
    # A decision is identified by reject_decision_id first, then signal_id.  The
    # observation id is only a final fallback for legacy accepted observations.
    # Keep the earliest physical row as the KPI row while retaining every row as
    # evidence in burnin_observations.
    table_alias = alias or "burnin_observations"
    identity = (f"COALESCE(json_extract({metrics}, '$.reject_decision_id'), "
                f"json_extract({metrics}, '$.signal_id'), {observation_id})")
    other_metrics = "canonical_other.metrics_json"
    other_identity = (f"COALESCE(json_extract({other_metrics}, '$.reject_decision_id'), "
                      f"json_extract({other_metrics}, '$.signal_id'), canonical_other.observation_id)")
    other_explicit_kind = (
        f"CASE WHEN json_valid(COALESCE({other_metrics}, '')) "
        f"THEN json_extract({other_metrics}, '$.observation_kind') END"
    )
    other_legacy_kind = (
        "CASE WHEN canonical_other.observation_id LIKE 'incomplete_reject_geometry_%' "
        f"THEN '{DIAGNOSTIC_OBSERVATION_KIND}' ELSE '{CANONICAL_DECISION_KIND}' END"
    )
    other_is_canonical = (
        f"UPPER(COALESCE({other_explicit_kind}, {other_legacy_kind})) "
        f"= '{CANONICAL_DECISION_KIND}'"
    )
    unique = ("NOT EXISTS (SELECT 1 FROM burnin_observations canonical_other "
              f"WHERE canonical_other.burnin_run_id={table_alias}.burnin_run_id "
              f"AND {other_is_canonical} AND {other_identity}={identity} "
              f"AND canonical_other.id<{table_alias}.id)")
    return f"({kind}) AND ({unique})"

def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def canonical_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()

def reject_decision_id_from_outcome(row: Mapping[str, Any]) -> str | None:
    """Recover the qualification identity carried by a reject outcome.

    New resolver rows carry it explicitly.  The outcome-id fallback preserves
    compatibility with existing ``rout_<reject_decision_id>`` rows and campaign
    aggregate copies.
    """
    try:
        payload = json.loads(row.get("payload_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    explicit = payload.get("reject_decision_id")
    if explicit:
        return str(explicit)
    outcome_id = str(row.get("reject_outcome_id") or "")
    if not outcome_id.startswith("rout_"):
        return None
    return outcome_id[5:].split(":agg:", 1)[0] or None

def config_hash(config: Mapping[str, Any]) -> str:
    return canonical_hash(dict(config))

def universe_hash(symbols: Sequence[str], intervals: Sequence[str] = ()) -> str:
    return canonical_hash({"symbols": sorted(map(str, symbols)), "intervals": sorted(map(str, intervals))})

@dataclass(slots=True)
class BurnInRun:
    burnin_run_id: str
    release_id: str
    phase: str = "PHASE7"
    execution_mode: str = "PAPER"
    parent_burnin_run_id: str | None = None
    parent_qualification_id: str | None = None
    continuation_sequence: int = 0
    start_time: str = field(default_factory=utc_now)
    end_time: str | None = None
    status: str = "RUNNING"
    git_commit: str | None = None
    config_hash: str | None = None
    strategy_config_hash: str | None = None
    universe_hash: str | None = None
    source_provenance: dict[str, Any] = field(default_factory=dict)
    symbols: list[str] = field(default_factory=list)
    intervals: list[str] = field(default_factory=list)
    expected_duration_seconds: float | None = None
    observed_duration_seconds: float | None = None
    sample_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    closed_trade_count: int = 0
    open_trade_count: int = 0
    data_completeness_status: str = "UNKNOWN"
    evidence_completeness_status: str = "UNKNOWN"
    generated_at: str = field(default_factory=utc_now)

    def validate(self) -> None:
        mode = self.execution_mode.upper()
        if mode not in ALLOWED_BURNIN_MODES:
            raise ValueError(f"burn-in evidence only supports PAPER or LIVE_PRECHECK, got {self.execution_mode}")
        if not self.source_provenance:
            raise ValueError("missing source provenance blocks burn-in evidence")
        provider = str(self.source_provenance.get("provider") or self.source_provenance.get("source") or "").upper()
        if provider in FORBIDDEN_PROVIDER_MARKERS:
            raise ValueError("synthetic provider evidence cannot qualify burn-in")
        for name in ("git_commit", "config_hash", "strategy_config_hash", "universe_hash"):
            if not getattr(self, name):
                raise ValueError(f"missing mutable provenance field: {name}")

DDL = [
"""CREATE TABLE IF NOT EXISTS burnin_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,burnin_run_id TEXT NOT NULL UNIQUE,release_id TEXT NOT NULL,phase TEXT NOT NULL,execution_mode TEXT NOT NULL,parent_burnin_run_id TEXT,parent_qualification_id TEXT,continuation_sequence INTEGER NOT NULL DEFAULT 0,start_time TEXT NOT NULL,end_time TEXT,status TEXT NOT NULL,git_commit TEXT NOT NULL,config_hash TEXT NOT NULL,strategy_config_hash TEXT NOT NULL,universe_hash TEXT NOT NULL,source_provenance_json TEXT NOT NULL,symbols_json TEXT NOT NULL,intervals_json TEXT NOT NULL,expected_duration_seconds REAL,observed_duration_seconds REAL,sample_count INTEGER NOT NULL,accepted_count INTEGER NOT NULL,rejected_count INTEGER NOT NULL,closed_trade_count INTEGER NOT NULL,open_trade_count INTEGER NOT NULL,data_completeness_status TEXT NOT NULL,evidence_completeness_status TEXT NOT NULL,generated_at TEXT NOT NULL,schema_version TEXT NOT NULL,UNIQUE(release_id, execution_mode, continuation_sequence))""",
"""CREATE TABLE IF NOT EXISTS burnin_observations (id INTEGER PRIMARY KEY AUTOINCREMENT,observation_id TEXT NOT NULL UNIQUE,burnin_run_id TEXT NOT NULL,release_id TEXT NOT NULL,observed_at TEXT NOT NULL,execution_mode TEXT NOT NULL,symbol TEXT,interval TEXT,regime TEXT,decision TEXT,lifecycle_state TEXT,evidence_complete INTEGER NOT NULL,missing_fields_json TEXT NOT NULL,metrics_json TEXT NOT NULL,source_provenance_json TEXT NOT NULL,schema_version TEXT NOT NULL)""",
"""CREATE TABLE IF NOT EXISTS burnin_trade_outcomes (id INTEGER PRIMARY KEY AUTOINCREMENT,outcome_id TEXT NOT NULL UNIQUE,burnin_run_id TEXT NOT NULL,release_id TEXT NOT NULL,trade_id TEXT,symbol TEXT,regime TEXT,closed_at TEXT,gross_r REAL,gross_pnl REAL,spread_cost REAL,entry_slippage_cost REAL,exit_slippage_cost REAL,fee_cost REAL,funding_cost REAL,latency_cost REAL,volatility_penalty REAL,liquidity_penalty REAL,total_execution_cost REAL,net_r REAL,net_pnl REAL,effective_rr_at_entry REAL,realized_effective_rr REAL,hold_duration_seconds REAL,mfe REAL,mae REAL,exit_reason TEXT,evidence_complete INTEGER NOT NULL,missing_cost_fields_json TEXT NOT NULL,payload_json TEXT NOT NULL,schema_version TEXT NOT NULL)""",
"""CREATE TABLE IF NOT EXISTS burnin_reject_outcomes (id INTEGER PRIMARY KEY AUTOINCREMENT,reject_outcome_id TEXT NOT NULL UNIQUE,burnin_run_id TEXT NOT NULL,release_id TEXT NOT NULL,reject_reason TEXT,symbol TEXT,regime TEXT,decision_time TEXT,hypothetical_entry REAL,hypothetical_stop REAL,hypothetical_target REAL,forward_label TEXT,would_tp INTEGER,would_sl INTEGER,timeout INTEGER,ambiguous INTEGER,hypothetical_gross_r REAL,hypothetical_net_r_after_costs REAL,avoided_loss REAL,missed_profit REAL,execution_invalidated INTEGER,evidence_horizon TEXT,evidence_complete INTEGER NOT NULL DEFAULT 0,payload_json TEXT NOT NULL,schema_version TEXT NOT NULL)""",
"""CREATE TABLE IF NOT EXISTS burnin_regime_metrics (id INTEGER PRIMARY KEY AUTOINCREMENT,burnin_run_id TEXT NOT NULL,release_id TEXT NOT NULL,regime TEXT NOT NULL,sample_count INTEGER,accepted_count INTEGER,rejected_count INTEGER,mean_net_r REAL,lower_confidence_bound_expectancy REAL,max_drawdown REAL,cost_drag REAL,slippage_distribution_json TEXT,reject_accuracy REAL,execution_failure_count INTEGER,status TEXT NOT NULL,generated_at TEXT NOT NULL,schema_version TEXT NOT NULL,UNIQUE(burnin_run_id,regime))""",
"""CREATE TABLE IF NOT EXISTS burnin_execution_metrics (id INTEGER PRIMARY KEY AUTOINCREMENT,burnin_run_id TEXT NOT NULL,release_id TEXT NOT NULL,metric_window TEXT NOT NULL,spread_baseline REAL,spread_current REAL,slippage_baseline REAL,slippage_current REAL,latency_baseline REAL,latency_current REAL,fill_probability_baseline REAL,fill_probability_current REAL,liquidity_depth_baseline REAL,liquidity_depth_current REAL,timeout_rate REAL,execution_rejects INTEGER,stale_data_count INTEGER,reconciliation_quality TEXT,funding_cost REAL,price_impact_proxy REAL,status TEXT NOT NULL,generated_at TEXT NOT NULL,schema_version TEXT NOT NULL)""",
"""CREATE TABLE IF NOT EXISTS burnin_calibration_metrics (id INTEGER PRIMARY KEY AUTOINCREMENT,burnin_run_id TEXT NOT NULL,release_id TEXT NOT NULL,scope TEXT NOT NULL,sample_count INTEGER,brier_score REAL,log_loss REAL,calibration_error REAL,expected_calibration_error REAL,reliability_buckets_json TEXT,observed_vs_predicted_json TEXT,status TEXT NOT NULL,generated_at TEXT NOT NULL,schema_version TEXT NOT NULL)""",
"""CREATE TABLE IF NOT EXISTS burnin_drawdown_events (id INTEGER PRIMARY KEY AUTOINCREMENT,drawdown_event_id TEXT NOT NULL UNIQUE,burnin_run_id TEXT NOT NULL,release_id TEXT NOT NULL,peak_equity REAL,trough_equity REAL,drawdown_start TEXT,drawdown_end TEXT,drawdown_pct REAL,drawdown_duration_seconds REAL,recovery_duration_seconds REAL,consecutive_losses INTEGER,rolling_loss_cluster_json TEXT,rolling_expectancy REAL,rolling_cost_drag REAL,rolling_slippage REAL,rolling_reject_accuracy REAL,resolved INTEGER NOT NULL,payload_json TEXT NOT NULL,schema_version TEXT NOT NULL)""",
"""CREATE TABLE IF NOT EXISTS burnin_qualification_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT,qualification_id TEXT NOT NULL UNIQUE,burnin_run_id TEXT NOT NULL,release_id TEXT NOT NULL,generated_at TEXT NOT NULL,status TEXT NOT NULL,sample_status TEXT NOT NULL,expectancy_status TEXT NOT NULL,execution_status TEXT NOT NULL,regime_status TEXT NOT NULL,reject_quality_status TEXT NOT NULL,calibration_status TEXT NOT NULL,drawdown_status TEXT NOT NULL,concentration_status TEXT NOT NULL,reconciliation_status TEXT NOT NULL,evidence_completeness_status TEXT NOT NULL,blockers_json TEXT NOT NULL,warnings_json TEXT NOT NULL,thresholds_json TEXT NOT NULL,metrics_json TEXT NOT NULL,evidence_hash TEXT NOT NULL,schema_version TEXT NOT NULL)""",
"""CREATE TABLE IF NOT EXISTS burnin_suspension_events (id INTEGER PRIMARY KEY AUTOINCREMENT,suspension_event_id TEXT NOT NULL UNIQUE,release_id TEXT NOT NULL,burnin_run_id TEXT NOT NULL,timestamp TEXT NOT NULL,reason_codes_json TEXT NOT NULL,observed_values_json TEXT NOT NULL,thresholds_json TEXT NOT NULL,evidence_payload_json TEXT NOT NULL,schema_version TEXT NOT NULL)""",
]

def bootstrap_burnin_schema(conn: Any) -> None:
    for stmt in DDL:
        conn.execute(stmt if isinstance(conn, sqlite3.Connection) else __import__('sqlalchemy').text(stmt))

def persist_burnin_run(conn: Any, run: BurnInRun) -> None:
    run.validate(); data=asdict(run)
    vals = {**data, "source_provenance_json": json.dumps(run.source_provenance, sort_keys=True), "symbols_json": json.dumps(run.symbols, sort_keys=True), "intervals_json": json.dumps(run.intervals, sort_keys=True), "schema_version": SCHEMA_VERSION}
    sql="""INSERT INTO burnin_runs(burnin_run_id,release_id,phase,execution_mode,parent_burnin_run_id,parent_qualification_id,continuation_sequence,start_time,end_time,status,git_commit,config_hash,strategy_config_hash,universe_hash,source_provenance_json,symbols_json,intervals_json,expected_duration_seconds,observed_duration_seconds,sample_count,accepted_count,rejected_count,closed_trade_count,open_trade_count,data_completeness_status,evidence_completeness_status,generated_at,schema_version) VALUES (:burnin_run_id,:release_id,:phase,:execution_mode,:parent_burnin_run_id,:parent_qualification_id,:continuation_sequence,:start_time,:end_time,:status,:git_commit,:config_hash,:strategy_config_hash,:universe_hash,:source_provenance_json,:symbols_json,:intervals_json,:expected_duration_seconds,:observed_duration_seconds,:sample_count,:accepted_count,:rejected_count,:closed_trade_count,:open_trade_count,:data_completeness_status,:evidence_completeness_status,:generated_at,:schema_version)"""
    conn.execute(sql if isinstance(conn, sqlite3.Connection) else __import__('sqlalchemy').text(sql), vals)

def execution_cost_complete(row: Mapping[str, Any]) -> tuple[bool, list[str]]:
    missing=[f for f in CRITICAL_COST_FIELDS if row.get(f) is None]
    return not missing, missing

def net_r_after_costs(row: Mapping[str, Any]) -> float | None:
    complete, _ = execution_cost_complete(row)
    if not complete or row.get("gross_r") is None: return None
    costs=sum(float(row.get(f) or 0.0) for f in CRITICAL_COST_FIELDS) + float(row.get("volatility_penalty") or 0.0) + float(row.get("liquidity_penalty") or 0.0)
    return float(row["gross_r"]) - costs

def confidence_interval(values: Sequence[float], z: float = 1.96) -> tuple[float | None,float | None,float | None]:
    vals=[float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not vals: return None,None,None
    mean=sum(vals)/len(vals)
    if len(vals) < 2: return mean, mean, mean
    var=sum((v-mean)**2 for v in vals)/(len(vals)-1); se=math.sqrt(var/len(vals))
    return mean, mean-z*se, mean+z*se

def latest_burnin_snapshot(conn: Any, burnin_run_id: str | None = None) -> dict[str, Any]:
    where="WHERE burnin_run_id = :bid" if burnin_run_id else ""
    params={"bid": burnin_run_id} if burnin_run_id else {}
    sql=f"SELECT * FROM burnin_qualification_snapshots {where} ORDER BY generated_at DESC, id DESC LIMIT 1"
    row=conn.execute(sql if isinstance(conn, sqlite3.Connection) else __import__('sqlalchemy').text(sql), params).fetchone()
    if row is None: return {"status":"UNAVAILABLE","reason":"NO_PHASE7_BURNIN_QUALIFICATION_EVIDENCE"}
    m=row if isinstance(row, sqlite3.Row) else row._mapping
    out=dict(m)
    for k in ("blockers_json","warnings_json","thresholds_json","metrics_json"):
        out[k[:-5] if k.endswith('_json') else k]=json.loads(out.get(k) or "{}" if k in {"thresholds_json","metrics_json"} else "[]")
    return out



def next_burnin_continuation_sequence(conn: Any, *, release_id: str, execution_mode: str) -> int:
    sql = """
        SELECT COALESCE(MAX(continuation_sequence), -1) + 1 AS next_sequence
        FROM burnin_runs
        WHERE release_id = :release_id AND execution_mode = :execution_mode
    """
    if isinstance(conn, sqlite3.Connection):
        row = conn.execute(sql.replace(":release_id", "?").replace(":execution_mode", "?"), (release_id, execution_mode)).fetchone()
    else:
        row = conn.execute(_sa_text(sql), {"release_id": release_id, "execution_mode": execution_mode}).fetchone()
    if row is None:
        return 0
    value = row._mapping["next_sequence"] if hasattr(row, "_mapping") else row[0]
    return int(value or 0)

def update_burnin_run_counters(conn: Any, burnin_run_id: str, *, status: str | None = None, end_time: str | None = None) -> dict[str, Any]:
    """Derive run counters from canonical SQL evidence to avoid drift."""
    row = conn.execute(_sa_text("SELECT release_id, phase, start_time, observed_duration_seconds FROM burnin_runs WHERE burnin_run_id=:bid"), {"bid": burnin_run_id}).fetchone()
    if row is None:
        return {"status": "MISSING_RUN"}
    mapping = row._mapping if hasattr(row, "_mapping") else row
    start_time = mapping["start_time"] if hasattr(mapping, "__getitem__") else None
    phase = str(mapping["phase"] if hasattr(mapping, "__getitem__") else "").upper()
    decision_predicate = canonical_decision_sql("o")
    obs = conn.execute(_sa_text(f"SELECT decision, evidence_complete, observed_at FROM burnin_observations o WHERE burnin_run_id=:bid AND {decision_predicate}"), {"bid": burnin_run_id}).fetchall()
    trades = conn.execute(_sa_text("SELECT evidence_complete, closed_at FROM burnin_trade_outcomes WHERE burnin_run_id=:bid"), {"bid": burnin_run_id}).fetchall()
    rejects = conn.execute(_sa_text("SELECT evidence_complete, forward_label FROM burnin_reject_outcomes WHERE burnin_run_id=:bid"), {"bid": burnin_run_id}).fetchall()
    def val(r, key):
        return (r._mapping[key] if hasattr(r, "_mapping") else r[key])
    sample_count = len(obs)
    accepted_count = sum(1 for r in obs if str(val(r, "decision") or "").upper() == "ACCEPTED")
    rejected_count = sum(1 for r in obs if str(val(r, "decision") or "").upper() == "REJECTED")
    closed_trade_count = len(trades)
    open_trade_count = max(0, accepted_count - closed_trade_count)
    incomplete_obs = sum(1 for r in obs if int(val(r, "evidence_complete") or 0) != 1)
    incomplete_trades = sum(1 for r in trades if int(val(r, "evidence_complete") or 0) != 1)
    incomplete_rejects = sum(1 for r in rejects if int(val(r, "evidence_complete") or 0) != 1)
    data_status = "PASS" if incomplete_obs == 0 else "INCOMPLETE"
    evidence_status = "PASS" if incomplete_obs == 0 and incomplete_trades == 0 and incomplete_rejects == 0 else "INCOMPLETE"
    times = [val(r, "observed_at") for r in obs if val(r, "observed_at")] + [val(r, "closed_at") for r in trades if val(r, "closed_at")]
    observed_duration = mapping["observed_duration_seconds"] if phase == "PHASE8" else None
    if phase != "PHASE8":
        parsed = [_parse_iso(t) for t in ([start_time] + times if start_time else times)]
        parsed = [t for t in parsed if t is not None]
        if len(parsed) >= 2:
            observed_duration = max(0.0, (max(parsed) - min(parsed)).total_seconds())
    sql = """UPDATE burnin_runs SET sample_count=:sample_count, accepted_count=:accepted_count, rejected_count=:rejected_count, closed_trade_count=:closed_trade_count, open_trade_count=:open_trade_count, observed_duration_seconds=COALESCE(:observed_duration_seconds, observed_duration_seconds), data_completeness_status=:data_status, evidence_completeness_status=:evidence_status, end_time=COALESCE(:end_time,end_time), status=COALESCE(:status,status), generated_at=:generated_at WHERE burnin_run_id=:bid"""
    params = {"bid": burnin_run_id, "sample_count": sample_count, "accepted_count": accepted_count, "rejected_count": rejected_count, "closed_trade_count": closed_trade_count, "open_trade_count": open_trade_count, "observed_duration_seconds": observed_duration, "data_status": data_status, "evidence_status": evidence_status, "end_time": end_time, "status": status, "generated_at": utc_now()}
    conn.execute(sql if isinstance(conn, sqlite3.Connection) else _sa_text(sql), params)
    return params

def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None

def export_burnin_evidence(db_path: str | Path, output_dir: str | Path, burnin_run_id: str) -> dict[str, Path]:
    out=Path(output_dir); out.mkdir(parents=True, exist_ok=True); conn=sqlite3.connect(str(db_path)); conn.row_factory=sqlite3.Row
    paths={}
    try:
        snap=latest_burnin_snapshot(conn,burnin_run_id); run=conn.execute("SELECT * FROM burnin_runs WHERE burnin_run_id=?",(burnin_run_id,)).fetchone()
        header={"burnin_run_id":burnin_run_id,"release_id": (run["release_id"] if run else snap.get("release_id")),"git_commit": (run["git_commit"] if run else None),"config_hash": (run["config_hash"] if run else None),"generated_at":utc_now(),"source_provenance": json.loads(run["source_provenance_json"] or "{}") if run else {},"schema_version":SCHEMA_VERSION}
        for name,payload in {"burnin_summary.json": {**header,"run":dict(run) if run else None}, "burnin_qualification.json": {**header,"qualification":snap}}.items():
            p=out/name; p.write_text(json.dumps(payload,indent=2,sort_keys=True,default=str)); paths[name]=p
        tables={"burnin_regime_metrics.csv":"burnin_regime_metrics","burnin_execution_metrics.csv":"burnin_execution_metrics","burnin_reject_quality.csv":"burnin_reject_outcomes","burnin_calibration.csv":"burnin_calibration_metrics","burnin_drawdowns.csv":"burnin_drawdown_events","burnin_suspension_events.csv":"burnin_suspension_events"}
        for fname,table in tables.items():
            rows=conn.execute(f"SELECT * FROM {table} WHERE burnin_run_id=? ORDER BY id",(burnin_run_id,)).fetchall(); p=out/fname
            with p.open('w',newline='') as fh:
                if rows:
                    w=csv.DictWriter(fh, fieldnames=list(dict(rows[0]).keys())); w.writeheader(); w.writerows([dict(r) for r in rows])
                else: csv.writer(fh).writerow([*header.keys()])
            paths[fname]=p
        return paths
    finally: conn.close()

def _sa_text(sql: str) -> Any:
    return sql if False else __import__('sqlalchemy').text(sql)

def persist_burnin_observation(conn: Any, *, observation_id: str, burnin_run_id: str, release_id: str, execution_mode: str, observed_at: str | None = None, symbol: str | None = None, interval: str | None = None, regime: str | None = None, decision: str | None = None, lifecycle_state: str | None = None, metrics: Mapping[str, Any] | None = None, source_provenance: Mapping[str, Any] | None = None, missing_fields: Sequence[str] = (), observation_kind: str = CANONICAL_DECISION_KIND) -> None:
    sql="""INSERT OR REPLACE INTO burnin_observations(observation_id,burnin_run_id,release_id,observed_at,execution_mode,symbol,interval,regime,decision,lifecycle_state,evidence_complete,missing_fields_json,metrics_json,source_provenance_json,schema_version) VALUES (:observation_id,:burnin_run_id,:release_id,:observed_at,:execution_mode,:symbol,:interval,:regime,:decision,:lifecycle_state,:evidence_complete,:missing_fields_json,:metrics_json,:source_provenance_json,:schema_version)"""
    observation_metrics = dict(metrics or {})
    observation_metrics["observation_kind"] = str(observation_kind).upper()
    params={"observation_id":observation_id,"burnin_run_id":burnin_run_id,"release_id":release_id,"observed_at":observed_at or utc_now(),"execution_mode":execution_mode,"symbol":symbol,"interval":interval,"regime":regime or "UNKNOWN","decision":decision,"lifecycle_state":lifecycle_state,"evidence_complete":0 if missing_fields else 1,"missing_fields_json":json.dumps(list(missing_fields),sort_keys=True),"metrics_json":json.dumps(observation_metrics,sort_keys=True,default=str),"source_provenance_json":json.dumps(dict(source_provenance or {}),sort_keys=True,default=str),"schema_version":SCHEMA_VERSION}
    conn.execute(sql if isinstance(conn, sqlite3.Connection) else _sa_text(sql), params)

def persist_burnin_trade_outcome(conn: Any, *, outcome_id: str, burnin_run_id: str, release_id: str, symbol: str, regime: str = "UNKNOWN", trade_id: str | None = None, closed_at: str | None = None, gross_r: float | None = None, gross_pnl: float | None = None, costs: Mapping[str, Any] | None = None, net_r: float | None = None, net_pnl: float | None = None, effective_rr_at_entry: float | None = None, realized_effective_rr: float | None = None, hold_duration_seconds: float | None = None, mfe: float | None = None, mae: float | None = None, exit_reason: str | None = None, payload: Mapping[str, Any] | None = None) -> None:
    c=dict(costs or {})
    complete, missing=execution_cost_complete({**c, "gross_r": gross_r})
    total_cost=None if missing else sum(float(c.get(f) or 0.0) for f in CRITICAL_COST_FIELDS)+float(c.get("volatility_penalty") or 0.0)+float(c.get("liquidity_penalty") or 0.0)
    if net_r is None and gross_r is not None and total_cost is not None: net_r=float(gross_r)-total_cost
    if net_pnl is None and gross_pnl is not None and total_cost is not None: net_pnl=float(gross_pnl)-total_cost
    sql="""INSERT OR REPLACE INTO burnin_trade_outcomes(outcome_id,burnin_run_id,release_id,trade_id,symbol,regime,closed_at,gross_r,gross_pnl,spread_cost,entry_slippage_cost,exit_slippage_cost,fee_cost,funding_cost,latency_cost,volatility_penalty,liquidity_penalty,total_execution_cost,net_r,net_pnl,effective_rr_at_entry,realized_effective_rr,hold_duration_seconds,mfe,mae,exit_reason,evidence_complete,missing_cost_fields_json,payload_json,schema_version) VALUES (:outcome_id,:burnin_run_id,:release_id,:trade_id,:symbol,:regime,:closed_at,:gross_r,:gross_pnl,:spread_cost,:entry_slippage_cost,:exit_slippage_cost,:fee_cost,:funding_cost,:latency_cost,:volatility_penalty,:liquidity_penalty,:total_execution_cost,:net_r,:net_pnl,:effective_rr_at_entry,:realized_effective_rr,:hold_duration_seconds,:mfe,:mae,:exit_reason,:evidence_complete,:missing_cost_fields_json,:payload_json,:schema_version)"""
    params={"outcome_id":outcome_id,"burnin_run_id":burnin_run_id,"release_id":release_id,"trade_id":trade_id,"symbol":symbol,"regime":regime,"closed_at":closed_at or utc_now(),"gross_r":gross_r,"gross_pnl":gross_pnl,"spread_cost":c.get("spread_cost"),"entry_slippage_cost":c.get("entry_slippage_cost"),"exit_slippage_cost":c.get("exit_slippage_cost"),"fee_cost":c.get("fee_cost"),"funding_cost":c.get("funding_cost"),"latency_cost":c.get("latency_cost"),"volatility_penalty":c.get("volatility_penalty"),"liquidity_penalty":c.get("liquidity_penalty"),"total_execution_cost":total_cost,"net_r":net_r,"net_pnl":net_pnl,"effective_rr_at_entry":effective_rr_at_entry,"realized_effective_rr":realized_effective_rr,"hold_duration_seconds":hold_duration_seconds,"mfe":mfe,"mae":mae,"exit_reason":exit_reason,"evidence_complete":1 if complete else 0,"missing_cost_fields_json":json.dumps(missing,sort_keys=True),"payload_json":json.dumps(dict(payload or {}),sort_keys=True,default=str),"schema_version":SCHEMA_VERSION}
    conn.execute(sql if isinstance(conn, sqlite3.Connection) else _sa_text(sql), params)

def persist_burnin_reject_outcome(conn: Any, *, reject_outcome_id: str, burnin_run_id: str, release_id: str, reject_reason: str, symbol: str, regime: str = "UNKNOWN", decision_time: str | None = None, hypothetical_entry: float | None = None, hypothetical_stop: float | None = None, hypothetical_target: float | None = None, forward_label: str | None = None, would_tp: bool | None = None, would_sl: bool | None = None, timeout: bool | None = None, ambiguous: bool | None = None, hypothetical_gross_r: float | None = None, hypothetical_net_r_after_costs: float | None = None, avoided_loss: float | None = None, missed_profit: float | None = None, execution_invalidated: bool | None = None, evidence_horizon: str | None = None, evidence_complete: bool | None = None, payload: Mapping[str, Any] | None = None) -> bool:
    sql="""INSERT INTO burnin_reject_outcomes(reject_outcome_id,burnin_run_id,release_id,reject_reason,symbol,regime,decision_time,hypothetical_entry,hypothetical_stop,hypothetical_target,forward_label,would_tp,would_sl,timeout,ambiguous,hypothetical_gross_r,hypothetical_net_r_after_costs,avoided_loss,missed_profit,execution_invalidated,evidence_horizon,evidence_complete,payload_json,schema_version) VALUES (:reject_outcome_id,:burnin_run_id,:release_id,:reject_reason,:symbol,:regime,:decision_time,:hypothetical_entry,:hypothetical_stop,:hypothetical_target,:forward_label,:would_tp,:would_sl,:timeout,:ambiguous,:hypothetical_gross_r,:hypothetical_net_r_after_costs,:avoided_loss,:missed_profit,:execution_invalidated,:evidence_horizon,:evidence_complete,:payload_json,:schema_version) ON CONFLICT(reject_outcome_id) DO NOTHING"""
    result=conn.execute(sql if isinstance(conn, sqlite3.Connection) else _sa_text(sql), {"reject_outcome_id":reject_outcome_id,"burnin_run_id":burnin_run_id,"release_id":release_id,"reject_reason":reject_reason,"symbol":symbol,"regime":regime,"decision_time":decision_time or utc_now(),"hypothetical_entry":hypothetical_entry,"hypothetical_stop":hypothetical_stop,"hypothetical_target":hypothetical_target,"forward_label":forward_label,"would_tp":None if would_tp is None else int(would_tp),"would_sl":None if would_sl is None else int(would_sl),"timeout":None if timeout is None else int(timeout),"ambiguous":None if ambiguous is None else int(ambiguous),"hypothetical_gross_r":hypothetical_gross_r,"hypothetical_net_r_after_costs":hypothetical_net_r_after_costs,"avoided_loss":avoided_loss,"missed_profit":missed_profit,"execution_invalidated":None if execution_invalidated is None else int(execution_invalidated),"evidence_horizon":evidence_horizon,"evidence_complete":int(evidence_complete if evidence_complete is not None else bool(forward_label and hypothetical_net_r_after_costs is not None and not execution_invalidated and not ambiguous)),"payload_json":json.dumps(dict(payload or {}),sort_keys=True,default=str),"schema_version":SCHEMA_VERSION})
    return bool(result.rowcount)
