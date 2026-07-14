from __future__ import annotations

import argparse, asyncio, contextlib, csv, hashlib, json, os, sqlite3, subprocess
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from alphaforge.burnin import BurnInRun, bootstrap_burnin_schema, canonical_hash, config_hash as make_config_hash, persist_burnin_run, utc_now, universe_hash as make_universe_hash, update_burnin_run_counters
from alphaforge.burnin_qualification import BurnInQualificationEngine, BurnInThresholds
from alphaforge.config import runtime_filter_config

CAMPAIGN_SCHEMA_VERSION = "phase8_campaign_v1"
CAMPAIGN_STATUSES = {"CREATED","RUNNING","PAUSED","RECOVERY_REQUIRED","COMPLETED","FAILED","QUALIFIED","SUSPENDED"}

PHASE8_DDL = [
"""CREATE TABLE IF NOT EXISTS burnin_campaigns (id INTEGER PRIMARY KEY AUTOINCREMENT,campaign_id TEXT NOT NULL UNIQUE,release_id TEXT NOT NULL,campaign_status TEXT NOT NULL,created_at TEXT NOT NULL,started_at TEXT,completed_at TEXT,expected_duration_seconds REAL,observed_duration_seconds REAL,target_decisions INTEGER,target_closed_trades INTEGER,target_reject_forward_outcomes INTEGER,active_run_id TEXT,config_hash TEXT NOT NULL,strategy_config_hash TEXT NOT NULL,universe_hash TEXT NOT NULL,git_commit TEXT NOT NULL,execution_cost_config_hash TEXT,source_provenance_json TEXT NOT NULL,symbols_json TEXT NOT NULL,intervals_json TEXT NOT NULL,restart_count INTEGER NOT NULL DEFAULT 0,last_heartbeat_at TEXT,last_error TEXT,qualification_status TEXT,latest_qualification_id TEXT,evidence_completeness_status TEXT NOT NULL DEFAULT 'UNKNOWN',schema_version TEXT NOT NULL,UNIQUE(campaign_id, release_id))""",
"""CREATE TABLE IF NOT EXISTS burnin_campaign_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,campaign_id TEXT NOT NULL,burnin_run_id TEXT NOT NULL,continuation_sequence INTEGER NOT NULL,status TEXT NOT NULL,started_at TEXT NOT NULL,ended_at TEXT,created_at TEXT NOT NULL,schema_version TEXT NOT NULL,UNIQUE(campaign_id,burnin_run_id),UNIQUE(campaign_id,continuation_sequence))""",
"""CREATE TABLE IF NOT EXISTS burnin_campaign_events (id INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL UNIQUE,campaign_id TEXT NOT NULL,burnin_run_id TEXT,event_type TEXT NOT NULL,event_time TEXT NOT NULL,details_json TEXT NOT NULL,schema_version TEXT NOT NULL)""",
"""CREATE TABLE IF NOT EXISTS burnin_pending_reject_labels (id INTEGER PRIMARY KEY AUTOINCREMENT,pending_label_id TEXT NOT NULL UNIQUE,campaign_id TEXT NOT NULL,burnin_run_id TEXT NOT NULL,reject_decision_id TEXT NOT NULL,signal_id TEXT,symbol TEXT NOT NULL,side TEXT NOT NULL,decision_timestamp TEXT NOT NULL,entry REAL,stop REAL,target REAL,horizon_seconds REAL,execution_cost_assumptions_json TEXT NOT NULL,regime TEXT,reject_reason TEXT,source_provenance_json TEXT NOT NULL,due_at TEXT NOT NULL,status TEXT NOT NULL,evidence_complete INTEGER NOT NULL DEFAULT 0,last_error TEXT,created_at TEXT NOT NULL,resolved_at TEXT,schema_version TEXT NOT NULL,UNIQUE(reject_decision_id))""",
"""CREATE TABLE IF NOT EXISTS burnin_pending_position_outcomes (id INTEGER PRIMARY KEY AUTOINCREMENT,pending_position_id TEXT NOT NULL UNIQUE,trade_id TEXT NOT NULL,campaign_id TEXT NOT NULL,burnin_run_id TEXT NOT NULL,signal_id TEXT,symbol TEXT NOT NULL,side TEXT NOT NULL,entry_time TEXT NOT NULL,planned_entry REAL,simulated_fill REAL,stop REAL,target REAL,quantity REAL,notional REAL,entry_spread REAL,entry_slippage REAL,entry_fee REAL,regime TEXT,source_provenance_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'OPEN',exit_time TEXT,exit_price REAL,exit_reason TEXT,gross_pnl REAL,gross_r REAL,exit_spread REAL,exit_slippage REAL,exit_fee REAL,funding REAL,latency_impact_penalty REAL,total_execution_cost REAL,net_pnl REAL,net_r REAL,hold_duration_seconds REAL,mfe REAL,mae REAL,evidence_complete INTEGER NOT NULL DEFAULT 0,missing_fields_json TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL,resolved_at TEXT,schema_version TEXT NOT NULL,UNIQUE(trade_id))""",
"""CREATE TABLE IF NOT EXISTS burnin_campaign_exports (id INTEGER PRIMARY KEY AUTOINCREMENT,export_id TEXT NOT NULL UNIQUE,campaign_id TEXT NOT NULL,output_dir TEXT NOT NULL,manifest_path TEXT NOT NULL,generated_at TEXT NOT NULL,evidence_hash TEXT NOT NULL,checksums_json TEXT NOT NULL,status TEXT NOT NULL,schema_version TEXT NOT NULL)""",
]

def _exec(conn: Any, sql: str, params: Mapping[str, Any] | None = None):
    return conn.execute(sql if isinstance(conn, sqlite3.Connection) else text(sql), params or {})

def bootstrap_campaign_schema(conn: Any) -> None:
    bootstrap_burnin_schema(conn)
    for stmt in PHASE8_DDL: _exec(conn, stmt)
    # additive qualification columns; ignore on older SQLite if duplicate
    for stmt in ["ALTER TABLE burnin_qualification_snapshots ADD COLUMN campaign_id TEXT", "ALTER TABLE burnin_qualification_snapshots ADD COLUMN source_run_ids_json TEXT", "ALTER TABLE burnin_qualification_snapshots ADD COLUMN aggregate_evidence_hash TEXT", "ALTER TABLE burnin_campaigns ADD COLUMN worker_pid INTEGER", "ALTER TABLE burnin_campaigns ADD COLUMN worker_started_at TEXT"]:
        try: _exec(conn, stmt)
        except Exception: pass


def build_phase8_campaign_identity(runtime_config: Any, symbols: Sequence[str], intervals: Sequence[str], *, release_id: str | None = None) -> dict[str, Any]:
    """Canonical Phase 8 identity shared by CLI campaign creation and runtime attachment."""
    mode = getattr(getattr(runtime_config, "execution_mode", "PAPER"), "value", getattr(runtime_config, "execution_mode", "PAPER"))
    config_payload = dict(runtime_filter_config(runtime_config, mode=str(mode or "PAPER")))
    config_payload["symbols"] = sorted(map(str, symbols))
    config_payload["intervals"] = sorted(map(str, intervals))
    strategy_payload = {
        "min_signal_score": getattr(runtime_config, "min_signal_score", None),
        "min_effective_rr": getattr(runtime_config, "min_effective_rr", None),
        "min_rr": getattr(runtime_config, "min_rr", None),
    }
    execution_cost_payload = {
        "min_effective_rr": getattr(runtime_config, "min_effective_rr", None),
        "min_rr": getattr(runtime_config, "min_rr", None),
        "max_spread_pct": getattr(runtime_config, "max_spread_pct", None),
        "max_expected_slippage_pct": getattr(runtime_config, "max_expected_slippage_pct", None),
        "max_abs_funding_rate_pct": getattr(runtime_config, "max_abs_funding_rate_pct", None),
        "min_liquidity_usd": getattr(runtime_config, "min_liquidity_usd", None),
        "paper_slippage_bps": getattr(runtime_config, "paper_slippage_bps", None),
    }
    rid = release_id or os.getenv("ALPHAFORGE_RELEASE_ID", getattr(runtime_config, "phase7_burnin_release_id", "default"))
    return {
        "release_id": rid,
        "config_hash": make_config_hash(config_payload),
        "strategy_config_hash": make_config_hash(strategy_payload),
        "universe_hash": make_universe_hash(symbols, intervals),
        "execution_cost_config_hash": make_config_hash(execution_cost_payload),
        "config_payload": config_payload,
        "strategy_payload": strategy_payload,
        "execution_cost_payload": execution_cost_payload,
    }

@dataclass(slots=True)
class BurnInCampaign:
    campaign_id: str; release_id: str; campaign_status: str = "CREATED"; created_at: str = field(default_factory=utc_now); started_at: str|None=None; completed_at: str|None=None; expected_duration_seconds: float|None=None; observed_duration_seconds: float|None=None; target_decisions: int|None=None; target_closed_trades: int|None=None; target_reject_forward_outcomes: int|None=None; active_run_id: str|None=None; config_hash: str=""; strategy_config_hash: str=""; universe_hash: str=""; git_commit: str=""; execution_cost_config_hash: str|None=None; source_provenance: dict[str,Any]=field(default_factory=dict); symbols: list[str]=field(default_factory=list); intervals: list[str]=field(default_factory=list); restart_count: int=0; last_heartbeat_at: str|None=None; last_error: str|None=None; qualification_status: str|None=None; latest_qualification_id: str|None=None; evidence_completeness_status: str="UNKNOWN"; schema_version: str=CAMPAIGN_SCHEMA_VERSION
    def validate(self):
        if self.campaign_status not in CAMPAIGN_STATUSES: raise ValueError("invalid campaign status")
        if not self.release_id or not self.campaign_id: raise ValueError("campaign_id and release_id required")
        for k in ("config_hash","strategy_config_hash","universe_hash","git_commit"):
            if not getattr(self,k): raise ValueError(f"missing provenance: {k}")
        if not self.source_provenance: raise ValueError("missing source provenance")

def git_commit() -> str:
    try: return subprocess.check_output(["git","rev-parse","HEAD"], cwd=Path.cwd(), text=True).strip()
    except Exception: return "UNKNOWN"

def campaign_id_for(release_id: str, payload: Mapping[str, Any]) -> str:
    return "camp_" + canonical_hash({"release_id": release_id, **payload})[:16]

def create_campaign(conn: Any, *, release_id: str, duration_days: float, symbols: Sequence[str], intervals: Sequence[str], config: Mapping[str,Any]|None=None, strategy_config: Mapping[str,Any]|None=None, source_provenance: Mapping[str,Any]|None=None, execution_cost_config: Mapping[str,Any]|None=None, runtime_config: Any | None=None, target_decisions:int=500, target_closed_trades:int=30, target_reject_forward_outcomes:int=50) -> BurnInCampaign:
    bootstrap_campaign_schema(conn)
    prov=dict(source_provenance or {"provider":"PAPER_MARKET_DATA","source":"operator"})
    if not prov: raise ValueError("missing provenance")
    if runtime_config is not None:
        ident = build_phase8_campaign_identity(runtime_config, symbols, intervals, release_id=release_id)
        ch=ident["config_hash"]; sh=ident["strategy_config_hash"]; uh=ident["universe_hash"]; ech=ident["execution_cost_config_hash"]
    else:
        ch=make_config_hash(config or {"release_id": release_id, "symbols": list(symbols), "intervals": list(intervals)})
        sh=make_config_hash(strategy_config or {"strategy":"default"})
        uh=make_universe_hash(symbols, intervals)
        ech=make_config_hash(execution_cost_config) if execution_cost_config is not None else None
    cid=campaign_id_for(release_id,{"config_hash":ch,"strategy_config_hash":sh,"universe_hash":uh})
    c=BurnInCampaign(cid, release_id, expected_duration_seconds=float(duration_days)*86400, target_decisions=target_decisions, target_closed_trades=target_closed_trades, target_reject_forward_outcomes=target_reject_forward_outcomes, config_hash=ch, strategy_config_hash=sh, universe_hash=uh, git_commit=git_commit(), execution_cost_config_hash=ech, source_provenance=prov, symbols=list(symbols), intervals=list(intervals))
    c.validate()
    _exec(conn,"""INSERT INTO burnin_campaigns(campaign_id,release_id,campaign_status,created_at,started_at,completed_at,expected_duration_seconds,observed_duration_seconds,target_decisions,target_closed_trades,target_reject_forward_outcomes,active_run_id,config_hash,strategy_config_hash,universe_hash,git_commit,execution_cost_config_hash,source_provenance_json,symbols_json,intervals_json,restart_count,last_heartbeat_at,last_error,qualification_status,latest_qualification_id,evidence_completeness_status,schema_version) VALUES (:campaign_id,:release_id,:campaign_status,:created_at,:started_at,:completed_at,:expected_duration_seconds,:observed_duration_seconds,:target_decisions,:target_closed_trades,:target_reject_forward_outcomes,:active_run_id,:config_hash,:strategy_config_hash,:universe_hash,:git_commit,:execution_cost_config_hash,:source_provenance_json,:symbols_json,:intervals_json,:restart_count,:last_heartbeat_at,:last_error,:qualification_status,:latest_qualification_id,:evidence_completeness_status,:schema_version) ON CONFLICT(campaign_id) DO NOTHING""", {**asdict(c),"source_provenance_json":json.dumps(c.source_provenance,sort_keys=True),"symbols_json":json.dumps(c.symbols,sort_keys=True),"intervals_json":json.dumps(c.intervals,sort_keys=True)})
    event(conn,c.campaign_id,"CAMPAIGN_CREATED",details={"release_id":release_id})
    return c

def get_campaign(conn: Any, campaign_id: str) -> dict[str,Any]|None:
    row=_exec(conn,"SELECT * FROM burnin_campaigns WHERE campaign_id=:id",{"id":campaign_id}).fetchone()
    if not row: return None
    m=row if isinstance(row, sqlite3.Row) else row._mapping
    d=dict(m); 
    for src,dst,fb in [("source_provenance_json","source_provenance",{}),("symbols_json","symbols",[]),("intervals_json","intervals",[])]:
        try: d[dst]=json.loads(d.get(src) or json.dumps(fb))
        except Exception: d[dst]=fb
    return d

def event(conn: Any, campaign_id: str, event_type: str, *, burnin_run_id: str|None=None, details: Mapping[str,Any]|None=None) -> None:
    eid="evt_"+canonical_hash({"campaign_id":campaign_id,"type":event_type,"run":burnin_run_id,"at":utc_now(),"details":details or {}})[:24]
    _exec(conn,"INSERT OR IGNORE INTO burnin_campaign_events(event_id,campaign_id,burnin_run_id,event_type,event_time,details_json,schema_version) VALUES (:eid,:cid,:bid,:typ,:ts,:det,:sv)",{"eid":eid,"cid":campaign_id,"bid":burnin_run_id,"typ":event_type,"ts":utc_now(),"det":json.dumps(dict(details or {}),sort_keys=True,default=str),"sv":CAMPAIGN_SCHEMA_VERSION})

def start_or_resume_campaign(conn: Any, campaign_id: str, *, resume: bool=False, config_hash: str|None=None, strategy_config_hash: str|None=None, universe_hash: str|None=None) -> dict[str,Any]:
    bootstrap_campaign_schema(conn); c=get_campaign(conn,campaign_id)
    if not c: raise KeyError("campaign not found")
    for k,v in {"config_hash":config_hash,"strategy_config_hash":strategy_config_hash,"universe_hash":universe_hash}.items():
        if v and v != c[k]:
            _exec(conn,"UPDATE burnin_campaigns SET campaign_status='PAUSED', last_error=:e WHERE campaign_id=:id",{"id":campaign_id,"e":"CONFIG_DRIFT"}); event(conn,campaign_id,"CONFIG_DRIFT",details={k:{"expected":c[k],"observed":v}}); raise ValueError("CONFIG_DRIFT")
    old=c.get("active_run_id"); seq=int((_exec(conn,"SELECT COALESCE(MAX(continuation_sequence),-1)+1 FROM burnin_campaign_runs WHERE campaign_id=:id",{"id":campaign_id}).fetchone()[0]) or 0)
    if old and resume:
        _exec(conn,"UPDATE burnin_runs SET status='RECOVERY_REQUIRED', end_time=COALESCE(end_time,:ts) WHERE burnin_run_id=:bid AND status='RUNNING'",{"bid":old,"ts":utc_now()}); event(conn,campaign_id,"RECOVERY_REQUIRED",burnin_run_id=old)
    run_id=f"{campaign_id}_run_{seq:04d}"
    run=BurnInRun(run_id,c["release_id"],phase="PHASE8",execution_mode="PAPER",continuation_sequence=seq,start_time=utc_now(),status="RUNNING",git_commit=c["git_commit"],config_hash=c["config_hash"],strategy_config_hash=c["strategy_config_hash"],universe_hash=c["universe_hash"],source_provenance=c["source_provenance"],symbols=c["symbols"],intervals=c["intervals"],expected_duration_seconds=c.get("expected_duration_seconds"))
    persist_burnin_run(conn,run)
    _exec(conn,"INSERT INTO burnin_campaign_runs(campaign_id,burnin_run_id,continuation_sequence,status,started_at,created_at,schema_version) VALUES (:cid,:bid,:seq,'RUNNING',:ts,:ts,:sv)",{"cid":campaign_id,"bid":run_id,"seq":seq,"ts":run.start_time,"sv":CAMPAIGN_SCHEMA_VERSION})
    _exec(conn,"UPDATE burnin_campaigns SET campaign_status='RUNNING', started_at=COALESCE(started_at,:ts), active_run_id=:bid, restart_count=restart_count+:inc, last_heartbeat_at=:ts, last_error=NULL WHERE campaign_id=:cid",{"cid":campaign_id,"bid":run_id,"ts":run.start_time,"inc":1 if resume else 0})
    event(conn,campaign_id,"CAMPAIGN_RESUMED" if resume else "CAMPAIGN_STARTED",burnin_run_id=run_id)
    return {"campaign_id":campaign_id,"burnin_run_id":run_id,"continuation_sequence":seq,"status":"RUNNING"}

def pause_campaign(conn: Any, campaign_id: str) -> None:
    c=get_campaign(conn,campaign_id); 
    if not c: raise KeyError("campaign not found")
    _exec(conn,"UPDATE burnin_campaigns SET campaign_status='PAUSED', last_heartbeat_at=:ts WHERE campaign_id=:id",{"id":campaign_id,"ts":utc_now()}); event(conn,campaign_id,"CAMPAIGN_PAUSED",burnin_run_id=c.get("active_run_id"))


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row) if isinstance(row, sqlite3.Row) else dict(row._mapping)

def _json_avg(rows: list[Mapping[str, Any]], key: str) -> str | None:
    values: list[float] = []
    for r in rows:
        if r.get(key) is not None:
            values.append(float(r[key]))
    return None if not values else str(sum(values) / len(values))

def materialize_campaign_aggregate(conn: Any, campaign_id: str) -> str:
    """Build a synthetic aggregate run so the unmodified Phase 7 engine evaluates the full campaign.

    The aggregate run is not a shortcut: it is populated from all compatible continuation rows and then
    evaluated by BurnInQualificationEngine, preserving Phase 7 blockers for costs, regimes, calibration,
    drawdown, execution, concentration, release gates, operator ack, rollback/runbook/full-test evidence,
    reconciliation, and mutation attempts.
    """
    c = get_campaign(conn, campaign_id)
    if not c:
        raise KeyError("campaign not found")
    runs = [_row_dict(r) for r in _exec(conn, "SELECT r.* FROM burnin_runs r JOIN burnin_campaign_runs cr ON cr.burnin_run_id=r.burnin_run_id WHERE cr.campaign_id=:cid ORDER BY cr.continuation_sequence", {"cid": campaign_id}).fetchall()]
    if not runs:
        raise KeyError("campaign has no runs")
    incompatible = [r["burnin_run_id"] for r in runs if r.get("release_id") != c["release_id"] or r.get("config_hash") != c["config_hash"] or r.get("strategy_config_hash") != c["strategy_config_hash"] or r.get("universe_hash") != c["universe_hash"]]
    if incompatible:
        event(conn, campaign_id, "CONFIG_DRIFT", details={"incompatible_run_ids": incompatible})
        _exec(conn, "UPDATE burnin_campaigns SET campaign_status='PAUSED', last_error='CONFIG_DRIFT' WHERE campaign_id=:cid", {"cid": campaign_id})
        raise ValueError("CONFIG_DRIFT")
    agg_id = f"{campaign_id}__aggregate"
    source_ids = [r["burnin_run_id"] for r in runs]
    ph = ",".join([f":r{i}" for i in range(len(source_ids))])
    params = {f"r{i}": v for i, v in enumerate(source_ids)}
    # Clear prior materialization; source continuation rows remain immutable.
    for table in ("burnin_observations","burnin_trade_outcomes","burnin_reject_outcomes","burnin_regime_metrics","burnin_execution_metrics","burnin_calibration_metrics","burnin_drawdown_events"):
        _exec(conn, f"DELETE FROM {table} WHERE burnin_run_id=:agg", {"agg": agg_id})
    _exec(conn, "DELETE FROM burnin_runs WHERE burnin_run_id=:agg", {"agg": agg_id})
    agg_sequence = 900000 + (int(canonical_hash({"campaign_id": campaign_id})[:8], 16) % 90000)
    run = BurnInRun(agg_id, c["release_id"], phase="PHASE8", execution_mode="PAPER", continuation_sequence=agg_sequence, start_time=c.get("started_at") or c.get("created_at") or utc_now(), status="COMPLETED", git_commit=c["git_commit"], config_hash=c["config_hash"], strategy_config_hash=c["strategy_config_hash"], universe_hash=c["universe_hash"], source_provenance=c["source_provenance"], symbols=c["symbols"], intervals=c["intervals"], expected_duration_seconds=c.get("expected_duration_seconds"), observed_duration_seconds=c.get("observed_duration_seconds"))
    persist_burnin_run(conn, run)
    # Copy row-level evidence with aggregate-safe unique IDs.
    _exec(conn, f"""INSERT INTO burnin_observations(observation_id,burnin_run_id,release_id,observed_at,execution_mode,symbol,interval,regime,decision,lifecycle_state,evidence_complete,missing_fields_json,metrics_json,source_provenance_json,schema_version)
        SELECT observation_id || ':agg:' || :cid, :agg, release_id, observed_at, execution_mode, symbol, interval, regime, decision, lifecycle_state, evidence_complete, missing_fields_json, metrics_json, source_provenance_json, schema_version FROM burnin_observations WHERE burnin_run_id IN ({ph})""", {**params, "agg": agg_id, "cid": campaign_id})
    _exec(conn, f"""INSERT INTO burnin_trade_outcomes(outcome_id,burnin_run_id,release_id,trade_id,symbol,regime,closed_at,gross_r,gross_pnl,spread_cost,entry_slippage_cost,exit_slippage_cost,fee_cost,funding_cost,latency_cost,volatility_penalty,liquidity_penalty,total_execution_cost,net_r,net_pnl,effective_rr_at_entry,realized_effective_rr,hold_duration_seconds,mfe,mae,exit_reason,evidence_complete,missing_cost_fields_json,payload_json,schema_version)
        SELECT outcome_id || ':agg:' || :cid, :agg, release_id, trade_id, symbol, regime, closed_at, gross_r, gross_pnl, spread_cost, entry_slippage_cost, exit_slippage_cost, fee_cost, funding_cost, latency_cost, volatility_penalty, liquidity_penalty, total_execution_cost, net_r, net_pnl, effective_rr_at_entry, realized_effective_rr, hold_duration_seconds, mfe, mae, exit_reason, evidence_complete, missing_cost_fields_json, payload_json, schema_version FROM burnin_trade_outcomes WHERE burnin_run_id IN ({ph}) AND closed_at IS NOT NULL""", {**params, "agg": agg_id, "cid": campaign_id})
    _exec(conn, f"""INSERT INTO burnin_reject_outcomes(reject_outcome_id,burnin_run_id,release_id,reject_reason,symbol,regime,decision_time,hypothetical_entry,hypothetical_stop,hypothetical_target,forward_label,would_tp,would_sl,timeout,ambiguous,hypothetical_gross_r,hypothetical_net_r_after_costs,avoided_loss,missed_profit,execution_invalidated,evidence_horizon,evidence_complete,payload_json,schema_version)
        SELECT reject_outcome_id || ':agg:' || :cid, :agg, release_id, reject_reason, symbol, regime, decision_time, hypothetical_entry, hypothetical_stop, hypothetical_target, forward_label, would_tp, would_sl, timeout, ambiguous, hypothetical_gross_r, hypothetical_net_r_after_costs, avoided_loss, missed_profit, execution_invalidated, evidence_horizon, evidence_complete, payload_json, schema_version FROM burnin_reject_outcomes WHERE burnin_run_id IN ({ph})""", {**params, "agg": agg_id, "cid": campaign_id})
    # Aggregate regime metrics by regime; use conservative min LCB/max drawdown and weighted mean net R.
    regs = [_row_dict(r) for r in _exec(conn, f"SELECT * FROM burnin_regime_metrics WHERE burnin_run_id IN ({ph})", params).fetchall()]
    by_reg: dict[str, list[dict[str, Any]]] = {}
    for r in regs: by_reg.setdefault(str(r.get("regime") or "UNKNOWN"), []).append(r)
    for reg, rows in by_reg.items():
        sc = sum(int(r.get("sample_count") or 0) for r in rows); ac=sum(int(r.get("accepted_count") or 0) for r in rows); rc=sum(int(r.get("rejected_count") or 0) for r in rows)
        mean = None if sc == 0 else sum(float(r.get("mean_net_r") or 0) * int(r.get("sample_count") or 0) for r in rows) / sc
        lcbs=[float(r["lower_confidence_bound_expectancy"]) for r in rows if r.get("lower_confidence_bound_expectancy") is not None]
        maxdds=[float(r["max_drawdown"]) for r in rows if r.get("max_drawdown") is not None]
        _exec(conn, "INSERT INTO burnin_regime_metrics(burnin_run_id,release_id,regime,sample_count,accepted_count,rejected_count,mean_net_r,lower_confidence_bound_expectancy,max_drawdown,cost_drag,slippage_distribution_json,reject_accuracy,execution_failure_count,status,generated_at,schema_version) VALUES (:agg,:rel,:reg,:sc,:ac,:rc,:mean,:lcb,:mdd,NULL,'{}',NULL,0,:status,:ts,:sv)", {"agg": agg_id,"rel": c["release_id"],"reg": reg,"sc": sc,"ac": ac,"rc": rc,"mean": mean,"lcb": min(lcbs) if lcbs else None,"mdd": max(maxdds) if maxdds else None,"status":"PASS" if all(str(r.get('status') or '').upper()=='PASS' for r in rows) else "FAIL","ts":utc_now(),"sv":CAMPAIGN_SCHEMA_VERSION})
    # Execution: latest window is used; degraded statuses/ratios remain visible to Phase 7 checks.
    exec_rows=[_row_dict(r) for r in _exec(conn, f"SELECT * FROM burnin_execution_metrics WHERE burnin_run_id IN ({ph}) ORDER BY generated_at DESC, id DESC", params).fetchall()]
    if exec_rows:
        r=exec_rows[0]; _exec(conn, "INSERT INTO burnin_execution_metrics(burnin_run_id,release_id,metric_window,spread_baseline,spread_current,slippage_baseline,slippage_current,latency_baseline,latency_current,fill_probability_baseline,fill_probability_current,liquidity_depth_baseline,liquidity_depth_current,timeout_rate,execution_rejects,stale_data_count,reconciliation_quality,funding_cost,price_impact_proxy,status,generated_at,schema_version) VALUES (:agg,:rel,:mw,:sb,:sc,:slb,:slc,:lb,:lc,:fpb,:fpc,:ldb,:ldc,:to,:er,:sd,:rq,:fc,:pi,:st,:ts,:sv)", {"agg":agg_id,"rel":c["release_id"],"mw":r.get("metric_window"),"sb":r.get("spread_baseline"),"sc":r.get("spread_current"),"slb":r.get("slippage_baseline"),"slc":r.get("slippage_current"),"lb":r.get("latency_baseline"),"lc":r.get("latency_current"),"fpb":r.get("fill_probability_baseline"),"fpc":r.get("fill_probability_current"),"ldb":r.get("liquidity_depth_baseline"),"ldc":r.get("liquidity_depth_current"),"to":r.get("timeout_rate"),"er":sum(int(x.get("execution_rejects") or 0) for x in exec_rows),"sd":sum(int(x.get("stale_data_count") or 0) for x in exec_rows),"rq":r.get("reconciliation_quality"),"fc":r.get("funding_cost"),"pi":r.get("price_impact_proxy"),"st":"FAIL" if any(str(x.get("status") or '').upper() in {"DEGRADED","SEVERELY_DEGRADED","INSUFFICIENT_EVIDENCE","FAIL"} for x in exec_rows) else r.get("status"),"ts":utc_now(),"sv":CAMPAIGN_SCHEMA_VERSION})
    # Calibration: sample-weighted average scores and worst error so bad calibration blocks.
    cal=[_row_dict(r) for r in _exec(conn, f"SELECT * FROM burnin_calibration_metrics WHERE burnin_run_id IN ({ph})", params).fetchall()]
    if cal:
        samples=sum(int(r.get("sample_count") or 0) for r in cal)
        def wavg(k):
            vals=[(float(r[k]), int(r.get("sample_count") or 0)) for r in cal if r.get(k) is not None]
            den=sum(w for _,w in vals); return None if den == 0 else sum(v*w for v,w in vals)/den
        worst=max([float(r.get("calibration_error") if r.get("calibration_error") is not None else 999) for r in cal], default=999)
        ece=max([float(r["expected_calibration_error"]) for r in cal if r.get("expected_calibration_error") is not None], default=None)
        _exec(conn, "INSERT INTO burnin_calibration_metrics(burnin_run_id,release_id,scope,sample_count,brier_score,log_loss,calibration_error,expected_calibration_error,reliability_buckets_json,observed_vs_predicted_json,status,generated_at,schema_version) VALUES (:agg,:rel,'campaign',:samples,:brier,:logloss,:ce,:ece,'{}','{}',:status,:ts,:sv)", {"agg":agg_id,"rel":c["release_id"],"samples":samples,"brier":wavg("brier_score"),"logloss":wavg("log_loss"),"ce":worst,"ece":ece,"status":"PASS" if all(str(r.get('status') or '').upper()=='PASS' for r in cal) else "FAIL","ts":utc_now(),"sv":CAMPAIGN_SCHEMA_VERSION})
    _exec(conn, f"""INSERT INTO burnin_drawdown_events(drawdown_event_id,burnin_run_id,release_id,peak_equity,trough_equity,drawdown_start,drawdown_end,drawdown_pct,drawdown_duration_seconds,recovery_duration_seconds,consecutive_losses,rolling_loss_cluster_json,rolling_expectancy,rolling_cost_drag,rolling_slippage,rolling_reject_accuracy,resolved,payload_json,schema_version)
        SELECT drawdown_event_id || ':agg:' || :cid, :agg, release_id, peak_equity, trough_equity, drawdown_start, drawdown_end, drawdown_pct, drawdown_duration_seconds, recovery_duration_seconds, consecutive_losses, rolling_loss_cluster_json, rolling_expectancy, rolling_cost_drag, rolling_slippage, rolling_reject_accuracy, resolved, payload_json, schema_version FROM burnin_drawdown_events WHERE burnin_run_id IN ({ph})""", {**params,"agg":agg_id,"cid":campaign_id})
    update_burnin_run_counters(conn, agg_id, status="COMPLETED")
    return agg_id

def aggregate_campaign(conn: Any, campaign_id: str) -> dict[str,Any]:
    c=get_campaign(conn,campaign_id)
    if not c: return {"status":"UNAVAILABLE","reason":"NO_CAMPAIGN"}
    runs=[_row_dict(r) for r in _exec(conn,"SELECT r.* FROM burnin_runs r JOIN burnin_campaign_runs cr ON cr.burnin_run_id=r.burnin_run_id WHERE cr.campaign_id=:id ORDER BY cr.continuation_sequence",{"id":campaign_id}).fetchall()]
    run_ids=[r["burnin_run_id"] if isinstance(r,dict) else r["burnin_run_id"] for r in runs]
    if not run_ids: return {"status":"NO_EVIDENCE","campaign_id":campaign_id,"run_ids":[]}
    ph=",".join([f":r{i}" for i in range(len(run_ids))]); p={f"r{i}":v for i,v in enumerate(run_ids)}
    obs=_exec(conn,f"SELECT decision FROM burnin_observations WHERE burnin_run_id IN ({ph})",p).fetchall(); trades=_exec(conn,f"SELECT * FROM burnin_trade_outcomes WHERE burnin_run_id IN ({ph})",p).fetchall(); rejects=_exec(conn,f"SELECT * FROM burnin_reject_outcomes WHERE burnin_run_id IN ({ph})",p).fetchall()
    def gv(r,k): return (r[k] if isinstance(r, sqlite3.Row) else r._mapping[k])
    closed=[r for r in trades if gv(r,"closed_at") and int(gv(r,"evidence_complete") or 0)==1]
    resolved=[r for r in rejects if int(gv(r,"evidence_complete") or 0)==1]
    metrics={"sample_count":len(obs),"accepted_count":sum(1 for r in obs if str(gv(r,"decision") or '').upper()=='ACCEPTED'),"rejected_count":sum(1 for r in obs if str(gv(r,"decision") or '').upper()=='REJECTED'),"closed_trade_count":len(closed),"completed_rejected_forward_outcomes":len(resolved),"ambiguous_rejected_forward_outcomes":sum(1 for r in resolved if str(gv(r,'forward_label')).upper()=='AMBIGUOUS'),"source_run_ids":run_ids}
    metrics["evidence_hash"]=canonical_hash({"campaign_id":campaign_id,"metrics":metrics})
    return {"status":"OK","campaign_id":campaign_id,"release_id":c["release_id"],"metrics":metrics,"evidence_hash":metrics["evidence_hash"]}

def qualify_campaign(engine: Engine, campaign_id: str, thresholds: BurnInThresholds|None=None) -> dict[str,Any]:
    with engine.begin() as conn:
        bootstrap_campaign_schema(conn)
        aggregate_run_id = materialize_campaign_aggregate(conn, campaign_id)
        agg = aggregate_campaign(conn, campaign_id)
        c = get_campaign(conn, campaign_id)
        if not c or agg.get("status") != "OK":
            raise KeyError("campaign evidence missing")
    snap = BurnInQualificationEngine(engine, thresholds).evaluate(aggregate_run_id)
    with engine.begin() as conn:
        _exec(conn,"UPDATE burnin_qualification_snapshots SET campaign_id=:cid, source_run_ids_json=:runs, aggregate_evidence_hash=:eh WHERE qualification_id=:qid",{"cid":campaign_id,"runs":json.dumps(agg["metrics"]["source_run_ids"]),"eh":agg["evidence_hash"],"qid":snap.qualification_id})
        _exec(conn,"UPDATE burnin_campaigns SET qualification_status=:s, latest_qualification_id=:qid, evidence_completeness_status=:ev WHERE campaign_id=:cid",{"cid":campaign_id,"s":snap.status,"qid":snap.qualification_id,"ev":snap.evidence_completeness_status})
        event(conn,campaign_id,"QUALIFICATION_SNAPSHOT",burnin_run_id=aggregate_run_id,details={"qualification_id":snap.qualification_id,"status":snap.status})
    return {"campaign_id":campaign_id,"qualification_id":snap.qualification_id,"verdict":snap.status,"aggregate_evidence_hash":agg["evidence_hash"],"aggregate_run_id":aggregate_run_id}

def export_campaign_bundle(db_path: str|Path, output_dir: str|Path, campaign_id: str) -> dict[str,Any]:
    conn=sqlite3.connect(str(db_path)); conn.row_factory=sqlite3.Row
    try:
        bootstrap_campaign_schema(conn); c=get_campaign(conn,campaign_id)
        if not c: raise KeyError("campaign not found")
        root=Path(output_dir)/f"burnin_campaign_{campaign_id}"; root.mkdir(parents=True,exist_ok=True)
        agg=aggregate_campaign(conn,campaign_id); run_ids=agg.get("metrics",{}).get("source_run_ids",[])
        (root/"campaign.json").write_text(json.dumps(c,indent=2,sort_keys=True,default=str))
        tables={"runs.csv":"burnin_campaign_runs","observations.csv":"burnin_observations","trade_outcomes.csv":"burnin_trade_outcomes","reject_outcomes.csv":"burnin_reject_outcomes","pending_rejects.csv":"burnin_pending_reject_labels","pending_positions.csv":"burnin_pending_position_outcomes","regime_metrics.csv":"burnin_regime_metrics","execution_metrics.csv":"burnin_execution_metrics","calibration_metrics.csv":"burnin_calibration_metrics","drawdowns.csv":"burnin_drawdown_events","suspension_events.csv":"burnin_suspension_events","recovery_events.csv":"burnin_campaign_events"}
        counts={}
        for fname,table in tables.items():
            if table in {"burnin_campaign_runs","burnin_campaign_events","burnin_pending_reject_labels","burnin_pending_position_outcomes"}: rows=conn.execute(f"SELECT * FROM {table} WHERE campaign_id=? ORDER BY id",(campaign_id,)).fetchall()
            else:
                q=",".join("?" for _ in run_ids) or "''"; rows=conn.execute(f"SELECT * FROM {table} WHERE burnin_run_id IN ({q}) ORDER BY id",run_ids).fetchall()
            counts[fname]=len(rows); 
            with (root/fname).open("w",newline="") as fh:
                if rows: w=csv.DictWriter(fh,fieldnames=list(dict(rows[0]).keys())); w.writeheader(); w.writerows([dict(r) for r in rows])
                else: csv.writer(fh).writerow(["no_evidence"])
        qs=conn.execute("SELECT * FROM burnin_qualification_snapshots WHERE campaign_id=? OR burnin_run_id IN (%s) ORDER BY id" % (",".join("?" for _ in run_ids) or "''"), [campaign_id,*run_ids]).fetchall()
        (root/"qualification_snapshots.json").write_text(json.dumps([dict(r) for r in qs],indent=2,sort_keys=True,default=str)); counts["qualification_snapshots.json"]=len(qs)
        (root/"config.json").write_text(json.dumps({"config_hash":c["config_hash"],"strategy_config_hash":c["strategy_config_hash"],"universe_hash":c["universe_hash"]},indent=2,sort_keys=True))
        (root/"provenance.json").write_text(json.dumps(c["source_provenance"],indent=2,sort_keys=True))
        manifest={"campaign_id":campaign_id,"release_id":c["release_id"],"git_commit":c["git_commit"],"source_run_ids":run_ids,"config_hashes":{"config_hash":c["config_hash"],"strategy_config_hash":c["strategy_config_hash"],"universe_hash":c["universe_hash"]},"schema_versions":[CAMPAIGN_SCHEMA_VERSION],"generated_at":utc_now(),"evidence_hash":agg.get("evidence_hash"),"row_counts":counts,"completeness_status":c.get("evidence_completeness_status"),"qualification_verdict":c.get("qualification_status")}
        (root/"manifest.json").write_text(json.dumps(manifest,indent=2,sort_keys=True,default=str))
        checks={}
        for p in sorted(root.iterdir()):
            if p.name!="checksums.sha256" and p.is_file(): checks[p.name]=hashlib.sha256(p.read_bytes()).hexdigest()
        (root/"checksums.sha256").write_text("".join(f"{v}  {k}\n" for k,v in checks.items()))
        eid="export_"+canonical_hash({"campaign_id":campaign_id,"checks":checks})[:16]
        conn.execute("INSERT OR REPLACE INTO burnin_campaign_exports(export_id,campaign_id,output_dir,manifest_path,generated_at,evidence_hash,checksums_json,status,schema_version) VALUES (?,?,?,?,?,?,?,?,?)",(eid,campaign_id,str(root),str(root/"manifest.json"),manifest["generated_at"],str(manifest.get("evidence_hash")),json.dumps(checks,sort_keys=True),"EXPORTED",CAMPAIGN_SCHEMA_VERSION)); conn.commit()
        return {"campaign_id":campaign_id,"output_dir":str(root),"manifest":manifest,"checksums":checks}
    finally: conn.close()


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None

def update_campaign_heartbeat(conn: Any, campaign_id: str) -> dict[str, Any]:
    c = get_campaign(conn, campaign_id)
    if not c:
        raise KeyError("campaign not found")
    now = utc_now()
    start = _parse_utc(c.get("started_at") or c.get("created_at"))
    cur = _parse_utc(now)
    observed = 0.0 if start is None or cur is None else max(0.0, (cur - start).total_seconds())
    _exec(conn, "UPDATE burnin_campaigns SET last_heartbeat_at=:now, observed_duration_seconds=COALESCE(:observed, observed_duration_seconds) WHERE campaign_id=:cid", {"cid": campaign_id, "now": now, "observed": observed})
    event(conn, campaign_id, "CAMPAIGN_HEARTBEAT", details={"observed_duration_seconds": observed})
    return {"campaign_id": campaign_id, "last_heartbeat_at": now, "observed_duration_seconds": observed}

def check_campaign_completion(conn: Any, campaign_id: str) -> dict[str, Any]:
    c = get_campaign(conn, campaign_id)
    if not c:
        raise KeyError("campaign not found")
    agg = aggregate_campaign(conn, campaign_id)
    metrics = agg.get("metrics", {}) if agg.get("status") == "OK" else {}
    pending_rejects = int((_exec(conn, "SELECT COUNT(*) FROM burnin_pending_reject_labels WHERE campaign_id=:cid AND status IN ('PENDING','READY')", {"cid": campaign_id}).fetchone()[0]) or 0)
    pending_positions = int((_exec(conn, "SELECT COUNT(*) FROM burnin_pending_position_outcomes WHERE campaign_id=:cid AND status='OPEN'", {"cid": campaign_id}).fetchone()[0]) or 0)
    blockers: list[str] = []
    if c.get("expected_duration_seconds") is not None and float(c.get("observed_duration_seconds") or 0) < float(c["expected_duration_seconds"]): blockers.append("DURATION_INCOMPLETE")
    if c.get("target_decisions") is not None and int(metrics.get("sample_count") or 0) < int(c["target_decisions"]): blockers.append("DECISION_SAMPLE_INCOMPLETE")
    if c.get("target_closed_trades") is not None and int(metrics.get("closed_trade_count") or 0) < int(c["target_closed_trades"]): blockers.append("CLOSED_TRADE_SAMPLE_INCOMPLETE")
    if c.get("target_reject_forward_outcomes") is not None and int(metrics.get("completed_rejected_forward_outcomes") or 0) < int(c["target_reject_forward_outcomes"]): blockers.append("REJECT_FORWARD_SAMPLE_INCOMPLETE")
    if pending_rejects or pending_positions: blockers.append("PENDING_OUTCOME_BACKLOG")
    if str(c.get("evidence_completeness_status") or "UNKNOWN").upper() != "PASS": blockers.append("EVIDENCE_COMPLETENESS_NOT_PASS")
    if not c.get("latest_qualification_id"): blockers.append("FINAL_QUALIFICATION_MISSING")
    if not blockers:
        _exec(conn, "UPDATE burnin_campaigns SET campaign_status='COMPLETED', completed_at=COALESCE(completed_at,:now) WHERE campaign_id=:cid AND campaign_status NOT IN ('FAILED','SUSPENDED')", {"cid": campaign_id, "now": utc_now()})
        event(conn, campaign_id, "CAMPAIGN_COMPLETED", details={"metrics": metrics})
    else:
        event(conn, campaign_id, "CAMPAIGN_COMPLETION_CHECK", details={"blockers": blockers, "metrics": metrics, "pending_rejects": pending_rejects, "pending_positions": pending_positions})
    return {"campaign_id": campaign_id, "complete": not blockers, "blockers": blockers, "metrics": metrics}


class CampaignCandleProviderError(RuntimeError): pass
class MarketDataUnavailable(CampaignCandleProviderError): pass
class MarketDataStale(CampaignCandleProviderError): pass
class ProviderFailure(CampaignCandleProviderError): pass

class BinanceReadOnlyCandleProvider:
    """Read-only canonical candle provider for PAPER burn-in forward labels."""
    def __init__(self, *, interval: str = "1m", max_staleness_seconds: float | None = None, fetcher: Any | None = None) -> None:
        self.interval = interval; self.max_staleness_seconds = max_staleness_seconds; self.fetcher = fetcher
        self.source_provenance = {"provider": "BINANCE_READ_ONLY_KLINES", "exchange": "BINANCE", "market_type": "USD_M_FUTURES", "interval": interval, "order_submission": "DISABLED"}

    def __call__(self, symbol: str, start: str, end: str) -> list[dict[str, Any]]:
        from alphaforge.historical_market_data import fetch_binance_klines_paginated, HistoricalDataError
        start_dt = _parse_utc(start); end_dt = _parse_utc(end)
        if start_dt is None or end_dt is None or end_dt <= start_dt:
            raise MarketDataUnavailable("MARKET_DATA_UNAVAILABLE")
        start_ms = int(start_dt.timestamp() * 1000) + 1
        end_ms = int(end_dt.timestamp() * 1000)
        try:
            candles = fetch_binance_klines_paginated(symbol, self.interval, start_ms, end_ms, fetcher=self.fetcher)
        except HistoricalDataError as exc:
            msg = str(exc)
            if "No candles" in msg or "shorter than one complete candle" in msg:
                return []
            raise MarketDataUnavailable(f"MARKET_DATA_UNAVAILABLE:{msg}") from exc
        except Exception as exc:
            raise ProviderFailure(f"PROVIDER_FAILURE:{exc.__class__.__name__}") from exc
        if self.max_staleness_seconds is not None and candles:
            newest = max(c.timestamp for c in candles) / 1000.0
            if (end_dt.timestamp() - newest) > self.max_staleness_seconds:
                raise MarketDataStale("MARKET_DATA_STALE")
        return [{"timestamp": datetime.fromtimestamp(c.timestamp/1000, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z"), "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume, "source_provenance": self.source_provenance} for c in candles]


class BurnInCampaignRunner:
    """Operational campaign worker loop for resolver/maintenance progress without enabling LIVE."""
    def __init__(self, engine: Engine, campaign_id: str, candle_provider: Any, *, runtime_factory: Any | None = None, resolver_interval_seconds: float = 30.0, qualification_interval_seconds: float = 300.0, maintenance_interval_seconds: float = 30.0, resolver_failure_threshold: int = 3, thresholds: BurnInThresholds | None = None) -> None:
        self.engine = engine; self.campaign_id = campaign_id; self.candle_provider = candle_provider; self.runtime_factory = runtime_factory; self.resolver_interval_seconds = resolver_interval_seconds; self.qualification_interval_seconds = qualification_interval_seconds; self.maintenance_interval_seconds = maintenance_interval_seconds; self.resolver_failure_threshold = resolver_failure_threshold; self.thresholds = thresholds; self.resolver_failure_count = 0; self._stop_event: asyncio.Event | None = None

    def resolver_tick(self) -> dict[str, Any]:
        from alphaforge.burnin_resolver import resolve_campaign_batch
        try:
            with self.engine.begin() as conn:
                bootstrap_campaign_schema(conn)
                due = _exec(conn, "SELECT symbol, MIN(decision_timestamp) AS start_ts, MAX(due_at) AS end_ts, COUNT(*) AS count FROM burnin_pending_reject_labels WHERE campaign_id=:cid AND status IN ('PENDING','READY') AND due_at <= :now GROUP BY symbol", {"cid": self.campaign_id, "now": utc_now()}).fetchall()
                candles: dict[str, Any] = {}
                for row in due:
                    r = _row_dict(row); candles[r["symbol"]] = self.candle_provider(r["symbol"], r["start_ts"], r["end_ts"])
                counts = resolve_campaign_batch(conn, self.campaign_id, candles, now=utc_now())
                event(conn, self.campaign_id, "RESOLVER_BATCH", details={"counts": counts})
            q = qualify_campaign(self.engine, self.campaign_id, self.thresholds)
            with self.engine.begin() as conn:
                event(conn, self.campaign_id, "RESOLVER_QUALIFICATION_TRIGGERED", details=q)
            self.resolver_failure_count = 0
            return {"status": "OK", "resolver_counts": counts, "qualification": q}
        except Exception as exc:
            self.resolver_failure_count += 1
            with self.engine.begin() as conn:
                bootstrap_campaign_schema(conn)
                event(conn, self.campaign_id, "RESOLVER_BATCH_FAILED", details={"error": str(exc), "failure_count": self.resolver_failure_count})
                if self.resolver_failure_count >= self.resolver_failure_threshold:
                    _exec(conn, "UPDATE burnin_campaigns SET campaign_status='PAUSED', last_error=:err WHERE campaign_id=:cid", {"cid": self.campaign_id, "err": "RESOLVER_FAILURE_THRESHOLD"})
                    event(conn, self.campaign_id, "CAMPAIGN_PAUSED", details={"reason": "RESOLVER_FAILURE_THRESHOLD"})
            if self.resolver_failure_count >= self.resolver_failure_threshold:
                return {"status": "PAUSED", "error": str(exc), "failure_count": self.resolver_failure_count}
            return {"status": "FAILED", "error": str(exc), "failure_count": self.resolver_failure_count}

    async def _resolver_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            await asyncio.sleep(max(0.0, self.resolver_interval_seconds))
            result = self.resolver_tick()
            if result.get("status") == "PAUSED":
                self._stop_event.set(); return

    async def _maintenance_loop(self) -> None:
        assert self._stop_event is not None
        last_qualification = 0.0
        while not self._stop_event.is_set():
            await asyncio.sleep(max(0.0, self.maintenance_interval_seconds))
            with self.engine.begin() as conn:
                bootstrap_campaign_schema(conn)
                heartbeat = update_campaign_heartbeat(conn, self.campaign_id)
                completion = check_campaign_completion(conn, self.campaign_id)
                status = (get_campaign(conn, self.campaign_id) or {}).get("campaign_status")
            now_mono = asyncio.get_running_loop().time()
            if now_mono - last_qualification >= self.qualification_interval_seconds:
                qualify_campaign(self.engine, self.campaign_id, self.thresholds); last_qualification = now_mono
            if completion.get("complete") or status in {"COMPLETED","FAILED","QUALIFIED","SUSPENDED","PAUSED"}:
                self._stop_event.set(); return

    async def run_foreground(self) -> dict[str, Any]:
        old_campaign = os.environ.get("ALPHAFORGE_BURNIN_CAMPAIGN_ID")
        old_alpha_mode = os.environ.get("ALPHAFORGE_EXECUTION_MODE")
        old_mode = os.environ.get("EXECUTION_MODE")
        os.environ["ALPHAFORGE_BURNIN_CAMPAIGN_ID"] = self.campaign_id
        os.environ["ALPHAFORGE_EXECUTION_MODE"] = "PAPER"
        os.environ["EXECUTION_MODE"] = "PAPER"
        self._stop_event = asyncio.Event()
        runtime = None
        tasks: list[asyncio.Task[Any]] = []
        try:
            if self.runtime_factory is None:
                from alphaforge.runtime import _build_runtime_from_env
                self.runtime_factory = _build_runtime_from_env
            runtime = self.runtime_factory()
            setattr(runtime, "persistence_engine", self.engine)
            attach = getattr(runtime, "_attach_phase8_campaign", None)
            if callable(attach): attach(self.campaign_id)
            else: raise RuntimeError("PHASE8_RUNTIME_ATTACH_UNAVAILABLE")
            tasks.append(asyncio.create_task(runtime.start(), name="phase8_runtime_start"))
            tasks.append(asyncio.create_task(self._resolver_loop(), name="phase8_resolver_loop"))
            tasks.append(asyncio.create_task(self._maintenance_loop(), name="phase8_maintenance_loop"))
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc
            return {"status": "STOPPED", "campaign_id": self.campaign_id}
        finally:
            if self._stop_event is not None: self._stop_event.set()
            for task in tasks:
                if not task.done(): task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if runtime is not None and hasattr(runtime, "shutdown"):
                with contextlib.suppress(Exception): runtime.shutdown()
            for key, value in (("ALPHAFORGE_BURNIN_CAMPAIGN_ID", old_campaign),("ALPHAFORGE_EXECUTION_MODE", old_alpha_mode),("EXECUTION_MODE", old_mode)):
                if value is None: os.environ.pop(key, None)
                else: os.environ[key] = value
