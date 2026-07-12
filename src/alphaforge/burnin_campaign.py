from __future__ import annotations

import asyncio, csv, hashlib, json, os, signal, sqlite3, subprocess, sys, time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from alphaforge.burnin import BurnInRun, bootstrap_burnin_schema, canonical_hash, config_hash as make_config_hash, persist_burnin_run, utc_now, universe_hash as make_universe_hash, confidence_interval
from alphaforge.burnin_qualification import BurnInThresholds, BurnInQualificationSnapshot, BurnInQualificationEngine

CAMPAIGN_SCHEMA_VERSION = "phase8_campaign_v2"
CAMPAIGN_STATUSES = {"CREATED","RUNNING","PAUSED","RECOVERY_REQUIRED","COMPLETED","FAILED","QUALIFIED","SUSPENDED"}
TERMINAL_RUN_STATUSES = {"COMPLETED", "FAILED", "PAUSED", "RECOVERY_REQUIRED"}

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

def _row_dict(row: Any) -> dict[str, Any]:
    if row is None: return {}
    return dict(row) if isinstance(row, sqlite3.Row) else dict(row._mapping)

def bootstrap_campaign_schema(conn: Any) -> None:
    bootstrap_burnin_schema(conn)
    for stmt in PHASE8_DDL: _exec(conn, stmt)
    additive = [
        "ALTER TABLE burnin_qualification_snapshots ADD COLUMN campaign_id TEXT",
        "ALTER TABLE burnin_qualification_snapshots ADD COLUMN source_run_ids_json TEXT",
        "ALTER TABLE burnin_qualification_snapshots ADD COLUMN aggregate_evidence_hash TEXT",
        "ALTER TABLE burnin_campaigns ADD COLUMN worker_pid INTEGER",
        "ALTER TABLE burnin_campaigns ADD COLUMN worker_started_at TEXT",
        "ALTER TABLE burnin_campaigns ADD COLUMN last_runtime_status TEXT",
        "ALTER TABLE burnin_campaigns ADD COLUMN stale_worker_timeout_seconds REAL DEFAULT 120",
        "ALTER TABLE burnin_campaigns ADD COLUMN pending_backlog_bound INTEGER DEFAULT 0",
    ]
    for stmt in additive:
        try: _exec(conn, stmt)
        except Exception: pass

@dataclass(slots=True)
class BurnInCampaign:
    campaign_id: str; release_id: str; campaign_status: str = "CREATED"; created_at: str = field(default_factory=utc_now); started_at: str|None=None; completed_at: str|None=None; expected_duration_seconds: float|None=None; observed_duration_seconds: float|None=None; target_decisions: int|None=None; target_closed_trades: int|None=None; target_reject_forward_outcomes: int|None=None; active_run_id: str|None=None; config_hash: str=""; strategy_config_hash: str=""; universe_hash: str=""; git_commit: str=""; execution_cost_config_hash: str|None=None; source_provenance: dict[str,Any]=field(default_factory=dict); symbols: list[str]=field(default_factory=list); intervals: list[str]=field(default_factory=list); restart_count: int=0; last_heartbeat_at: str|None=None; last_error: str|None=None; qualification_status: str|None=None; latest_qualification_id: str|None=None; evidence_completeness_status: str="UNKNOWN"; schema_version: str=CAMPAIGN_SCHEMA_VERSION
    def validate(self) -> None:
        if self.campaign_status not in CAMPAIGN_STATUSES: raise ValueError("invalid campaign status")
        if not self.release_id or not self.campaign_id: raise ValueError("campaign_id and release_id required")
        for k in ("config_hash","strategy_config_hash","universe_hash","git_commit"):
            if not getattr(self,k): raise ValueError(f"missing provenance: {k}")
        if not self.source_provenance: raise ValueError("missing source provenance")

def git_commit() -> str:
    try: return subprocess.check_output(["git","rev-parse","HEAD"], cwd=Path.cwd(), text=True, timeout=2).strip()
    except Exception: return "UNKNOWN_GIT_COMMIT"

def campaign_id_for(release_id: str, payload: Mapping[str, Any]) -> str:
    return "camp_" + canonical_hash({"release_id": release_id, **payload})[:16]

def create_campaign(conn: Any, *, release_id: str, duration_days: float, symbols: Sequence[str], intervals: Sequence[str], config: Mapping[str,Any]|None=None, strategy_config: Mapping[str,Any]|None=None, source_provenance: Mapping[str,Any]|None=None, target_decisions:int=500, target_closed_trades:int=30, target_reject_forward_outcomes:int=50, execution_cost_config: Mapping[str, Any] | None = None) -> BurnInCampaign:
    bootstrap_campaign_schema(conn)
    prov=dict(source_provenance or {"provider":"PAPER_MARKET_DATA","source":"operator"})
    ch=make_config_hash(config or {"release_id": release_id, "symbols": list(symbols), "intervals": list(intervals)})
    sh=make_config_hash(strategy_config or {"strategy":"default"})
    uh=make_universe_hash(symbols, intervals)
    ech=make_config_hash(execution_cost_config or {})
    cid=campaign_id_for(release_id,{"config_hash":ch,"strategy_config_hash":sh,"universe_hash":uh,"execution_cost_config_hash":ech})
    c=BurnInCampaign(cid, release_id, expected_duration_seconds=float(duration_days)*86400, target_decisions=target_decisions, target_closed_trades=target_closed_trades, target_reject_forward_outcomes=target_reject_forward_outcomes, config_hash=ch, strategy_config_hash=sh, universe_hash=uh, execution_cost_config_hash=ech, git_commit=git_commit(), source_provenance=prov, symbols=list(symbols), intervals=list(intervals))
    c.validate()
    _exec(conn,"""INSERT INTO burnin_campaigns(campaign_id,release_id,campaign_status,created_at,started_at,completed_at,expected_duration_seconds,observed_duration_seconds,target_decisions,target_closed_trades,target_reject_forward_outcomes,active_run_id,config_hash,strategy_config_hash,universe_hash,git_commit,execution_cost_config_hash,source_provenance_json,symbols_json,intervals_json,restart_count,last_heartbeat_at,last_error,qualification_status,latest_qualification_id,evidence_completeness_status,schema_version) VALUES (:campaign_id,:release_id,:campaign_status,:created_at,:started_at,:completed_at,:expected_duration_seconds,:observed_duration_seconds,:target_decisions,:target_closed_trades,:target_reject_forward_outcomes,:active_run_id,:config_hash,:strategy_config_hash,:universe_hash,:git_commit,:execution_cost_config_hash,:source_provenance_json,:symbols_json,:intervals_json,:restart_count,:last_heartbeat_at,:last_error,:qualification_status,:latest_qualification_id,:evidence_completeness_status,:schema_version) ON CONFLICT(campaign_id) DO NOTHING""", {**asdict(c),"source_provenance_json":json.dumps(c.source_provenance,sort_keys=True),"symbols_json":json.dumps(c.symbols,sort_keys=True),"intervals_json":json.dumps(c.intervals,sort_keys=True)})
    event(conn,c.campaign_id,"CAMPAIGN_CREATED",details={"release_id":release_id})
    return c

def get_campaign(conn: Any, campaign_id: str) -> dict[str,Any]|None:
    row=_exec(conn,"SELECT * FROM burnin_campaigns WHERE campaign_id=:id",{"id":campaign_id}).fetchone()
    if not row: return None
    d=_row_dict(row)
    for src,dst,fb in [("source_provenance_json","source_provenance",{}),("symbols_json","symbols",[]),("intervals_json","intervals",[])]:
        try: d[dst]=json.loads(d.get(src) or json.dumps(fb))
        except Exception: d[dst]=fb
    return d

def event(conn: Any, campaign_id: str, event_type: str, *, burnin_run_id: str|None=None, details: Mapping[str,Any]|None=None) -> None:
    ts=utc_now(); eid="evt_"+canonical_hash({"campaign_id":campaign_id,"type":event_type,"run":burnin_run_id,"at":ts,"details":details or {}})[:24]
    _exec(conn,"INSERT OR IGNORE INTO burnin_campaign_events(event_id,campaign_id,burnin_run_id,event_type,event_time,details_json,schema_version) VALUES (:eid,:cid,:bid,:typ,:ts,:det,:sv)",{"eid":eid,"cid":campaign_id,"bid":burnin_run_id,"typ":event_type,"ts":ts,"det":json.dumps(dict(details or {}),sort_keys=True,default=str),"sv":CAMPAIGN_SCHEMA_VERSION})

def _check_drift(c: Mapping[str, Any], *, config_hash: str|None=None, strategy_config_hash: str|None=None, universe_hash: str|None=None, execution_cost_config_hash: str|None=None, release_id: str|None=None) -> str | None:
    checks=(('release_id',release_id,'CAMPAIGN_RELEASE_MISMATCH'),('config_hash',config_hash,'CAMPAIGN_CONFIG_DRIFT'),('strategy_config_hash',strategy_config_hash,'CAMPAIGN_STRATEGY_DRIFT'),('universe_hash',universe_hash,'CAMPAIGN_UNIVERSE_DRIFT'),('execution_cost_config_hash',execution_cost_config_hash,'CAMPAIGN_EXECUTION_COST_DRIFT'))
    for key, observed, reason in checks:
        if observed and observed != c.get(key): return reason
    return None

def start_or_resume_campaign(conn: Any, campaign_id: str, *, resume: bool=False, config_hash: str|None=None, strategy_config_hash: str|None=None, universe_hash: str|None=None, execution_cost_config_hash: str|None=None, release_id: str|None=None) -> dict[str,Any]:
    bootstrap_campaign_schema(conn); c=get_campaign(conn,campaign_id)
    if not c: raise KeyError("campaign not found")
    drift=_check_drift(c, config_hash=config_hash, strategy_config_hash=strategy_config_hash, universe_hash=universe_hash, execution_cost_config_hash=execution_cost_config_hash, release_id=release_id)
    if drift:
        _exec(conn,"UPDATE burnin_campaigns SET campaign_status='PAUSED', last_error=:e WHERE campaign_id=:id",{"id":campaign_id,"e":drift}); event(conn,campaign_id,drift); raise ValueError(drift)
    old=c.get("active_run_id"); seq=int((_exec(conn,"SELECT COALESCE(MAX(continuation_sequence),-1)+1 FROM burnin_campaign_runs WHERE campaign_id=:id",{"id":campaign_id}).fetchone()[0]) or 0)
    if old and resume:
        _exec(conn,"UPDATE burnin_runs SET status='RECOVERY_REQUIRED', end_time=COALESCE(end_time,:ts) WHERE burnin_run_id=:bid AND status='RUNNING'",{"bid":old,"ts":utc_now()})
        _exec(conn,"UPDATE burnin_campaign_runs SET status='RECOVERY_REQUIRED', ended_at=COALESCE(ended_at,:ts) WHERE burnin_run_id=:bid",{"bid":old,"ts":utc_now()})
        event(conn,campaign_id,"RECOVERY_REQUIRED",burnin_run_id=old)
    run_id=f"{campaign_id}_run_{seq:04d}"
    run=BurnInRun(run_id,c["release_id"],phase="PHASE8",execution_mode="PAPER",continuation_sequence=seq,start_time=utc_now(),status="RUNNING",git_commit=c["git_commit"],config_hash=c["config_hash"],strategy_config_hash=c["strategy_config_hash"],universe_hash=c["universe_hash"],source_provenance=c["source_provenance"],symbols=c["symbols"],intervals=c["intervals"],expected_duration_seconds=c.get("expected_duration_seconds"))
    persist_burnin_run(conn,run)
    _exec(conn,"INSERT INTO burnin_campaign_runs(campaign_id,burnin_run_id,continuation_sequence,status,started_at,created_at,schema_version) VALUES (:cid,:bid,:seq,'RUNNING',:ts,:ts,:sv)",{"cid":campaign_id,"bid":run_id,"seq":seq,"ts":run.start_time,"sv":CAMPAIGN_SCHEMA_VERSION})
    _exec(conn,"UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED', started_at=COALESCE(started_at,:ts), active_run_id=:bid, restart_count=restart_count+:inc, last_heartbeat_at=:ts, last_error=NULL WHERE campaign_id=:cid",{"cid":campaign_id,"bid":run_id,"ts":run.start_time,"inc":1 if resume else 0})
    event(conn,campaign_id,"CAMPAIGN_RESUMED" if resume else "CAMPAIGN_STARTED",burnin_run_id=run_id)
    return {"campaign_id":campaign_id,"burnin_run_id":run_id,"continuation_sequence":seq,"status":"RECOVERY_REQUIRED"}

def mark_worker_started(conn: Any, campaign_id: str, *, pid: int, runtime_status: str = "STARTING") -> None:
    c=get_campaign(conn,campaign_id)
    if not c or not c.get("active_run_id"): raise KeyError("campaign has no active run")
    ts=utc_now()
    _exec(conn,"UPDATE burnin_campaigns SET campaign_status='RUNNING', worker_pid=:pid, worker_started_at=:ts, last_heartbeat_at=:ts, last_runtime_status=:st, last_error=NULL WHERE campaign_id=:cid",{"cid":campaign_id,"pid":pid,"ts":ts,"st":runtime_status})
    event(conn,campaign_id,"WORKER_STARTED",burnin_run_id=c.get("active_run_id"),details={"pid":pid,"runtime_status":runtime_status})

def update_campaign_heartbeat(conn: Any, campaign_id: str, *, runtime_status: str = "OPERATING", error: str | None = None) -> None:
    c=get_campaign(conn,campaign_id)
    if not c: raise KeyError("campaign not found")
    start=c.get("started_at") or utc_now(); observed=None
    try:
        from datetime import datetime
        observed=(datetime.fromisoformat(utc_now().replace('Z','+00:00'))-datetime.fromisoformat(str(start).replace('Z','+00:00'))).total_seconds()
    except Exception: pass
    _exec(conn,"UPDATE burnin_campaigns SET last_heartbeat_at=:ts,last_runtime_status=:st,last_error=COALESCE(:err,last_error),observed_duration_seconds=COALESCE(:obs,observed_duration_seconds) WHERE campaign_id=:cid",{"cid":campaign_id,"ts":utc_now(),"st":runtime_status,"err":error,"obs":observed})

def pause_campaign(conn: Any, campaign_id: str, *, reason: str = "OPERATOR_PAUSE") -> None:
    c=get_campaign(conn,campaign_id)
    if not c: raise KeyError("campaign not found")
    _exec(conn,"UPDATE burnin_campaigns SET campaign_status='PAUSED', last_runtime_status='PAUSED', last_error=:reason, last_heartbeat_at=:ts WHERE campaign_id=:id",{"id":campaign_id,"reason":reason,"ts":utc_now()}); event(conn,campaign_id,"CAMPAIGN_PAUSED",burnin_run_id=c.get("active_run_id"),details={"reason":reason})

def mark_worker_failed(conn: Any, campaign_id: str, error: str) -> None:
    c=get_campaign(conn,campaign_id)
    if not c: raise KeyError("campaign not found")
    _exec(conn,"UPDATE burnin_campaigns SET campaign_status='FAILED', last_error=:err,last_runtime_status='FAILED',last_heartbeat_at=:ts WHERE campaign_id=:cid",{"cid":campaign_id,"err":error,"ts":utc_now()})
    event(conn,campaign_id,"WORKER_FAILED",burnin_run_id=c.get("active_run_id"),details={"error":error})

def detect_stale_worker(conn: Any, campaign_id: str, *, now_ts: float | None = None) -> dict[str, Any]:
    c=get_campaign(conn,campaign_id)
    if not c: return {"status":"UNAVAILABLE","reason":"NO_CAMPAIGN"}
    if c.get("campaign_status") != "RUNNING": return {"status":c.get("campaign_status"),"stale":False}
    hb=c.get("last_heartbeat_at"); timeout=float(c.get("stale_worker_timeout_seconds") or 120)
    if not hb: stale=True; age=None
    else:
        from datetime import datetime, timezone
        age=(now_ts or time.time())-datetime.fromisoformat(str(hb).replace('Z','+00:00')).timestamp(); stale=age>timeout
    if stale:
        _exec(conn,"UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED', last_error='STALE_WORKER_HEARTBEAT', last_runtime_status='STALE' WHERE campaign_id=:cid",{"cid":campaign_id})
        event(conn,campaign_id,"STALE_WORKER_HEARTBEAT",burnin_run_id=c.get("active_run_id"),details={"age_seconds":age,"timeout_seconds":timeout})
    return {"status":"RECOVERY_REQUIRED" if stale else "RUNNING","stale":stale,"age_seconds":age,"timeout_seconds":timeout}

def campaign_run_ids(conn: Any, campaign_id: str) -> list[str]:
    return [str(_row_dict(r)["burnin_run_id"]) for r in _exec(conn,"SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id=:id ORDER BY continuation_sequence",{"id":campaign_id}).fetchall()]

def _campaign_rows(conn: Any, table: str, run_ids: Sequence[str]) -> list[dict[str, Any]]:
    if not run_ids: return []
    ph=",".join([f":r{i}" for i in range(len(run_ids))]); params={f"r{i}":v for i,v in enumerate(run_ids)}
    return [_row_dict(r) for r in _exec(conn,f"SELECT * FROM {table} WHERE burnin_run_id IN ({ph}) ORDER BY id",params).fetchall()]

def aggregate_campaign(conn: Any, campaign_id: str) -> dict[str,Any]:
    c=get_campaign(conn,campaign_id)
    if not c: return {"status":"UNAVAILABLE","reason":"NO_CAMPAIGN"}
    runs=[_row_dict(r) for r in _exec(conn,"SELECT r.* FROM burnin_runs r JOIN burnin_campaign_runs cr ON cr.burnin_run_id=r.burnin_run_id WHERE cr.campaign_id=:id ORDER BY cr.continuation_sequence",{"id":campaign_id}).fetchall()]
    run_ids=[r["burnin_run_id"] for r in runs]
    if not run_ids: return {"status":"NO_EVIDENCE","campaign_id":campaign_id,"run_ids":[]}
    blockers=[]
    for r in runs:
        if r.get("release_id") != c.get("release_id"): blockers.append("CAMPAIGN_RELEASE_MISMATCH")
        if r.get("config_hash") != c.get("config_hash"): blockers.append("CAMPAIGN_CONFIG_DRIFT")
        if r.get("strategy_config_hash") != c.get("strategy_config_hash"): blockers.append("CAMPAIGN_STRATEGY_DRIFT")
        if r.get("universe_hash") != c.get("universe_hash"): blockers.append("CAMPAIGN_UNIVERSE_DRIFT")
        if str(r.get("execution_mode") or "").upper() != "PAPER": blockers.append("CAMPAIGN_NON_PAPER_EVIDENCE")
    obs=_campaign_rows(conn,"burnin_observations",run_ids); trades=_campaign_rows(conn,"burnin_trade_outcomes",run_ids); rejects=_campaign_rows(conn,"burnin_reject_outcomes",run_ids)
    regimes=_campaign_rows(conn,"burnin_regime_metrics",run_ids); execm=_campaign_rows(conn,"burnin_execution_metrics",run_ids); cal=_campaign_rows(conn,"burnin_calibration_metrics",run_ids); dds=_campaign_rows(conn,"burnin_drawdown_events",run_ids)
    closed=[r for r in trades if r.get("closed_at") and int(r.get("evidence_complete") or 0)==1]
    resolved=[r for r in rejects if int(r.get("evidence_complete") or 0)==1 and str(r.get("forward_label") or "").upper() in {"TP_BEFORE_SL","SL_BEFORE_TP","TIMEOUT","AMBIGUOUS"}]
    pending_rejects=int(_exec(conn,"SELECT COUNT(*) FROM burnin_pending_reject_labels WHERE campaign_id=:cid AND status IN ('PENDING','READY')",{"cid":campaign_id}).fetchone()[0])
    pending_positions=int(_exec(conn,"SELECT COUNT(*) FROM burnin_pending_position_outcomes WHERE campaign_id=:cid AND status='OPEN'",{"cid":campaign_id}).fetchone()[0])
    netrs=[float(r["net_r"]) for r in closed if r.get("net_r") is not None]
    mean,lcb,ucb=confidence_interval(netrs)
    metrics={"sample_count":len(obs),"accepted_count":sum(1 for r in obs if str(r.get("decision") or '').upper()=='ACCEPTED'),"rejected_count":sum(1 for r in obs if str(r.get("decision") or '').upper()=='REJECTED'),"closed_trade_count":len(closed),"open_position_count":pending_positions,"pending_reject_labels":pending_rejects,"completed_rejected_forward_outcomes":len(resolved),"ambiguous_rejected_forward_outcomes":sum(1 for r in resolved if str(r.get('forward_label')).upper()=='AMBIGUOUS'),"source_run_ids":run_ids,"mean_net_r":mean,"lower_confidence_bound_expectancy":lcb,"expectancy_confidence_interval":[lcb,ucb],"regime_metric_rows":len(regimes),"execution_metric_rows":len(execm),"calibration_metric_rows":len(cal),"drawdown_event_rows":len(dds),"net_r_by_run":{rid:[float(t["net_r"]) for t in closed if t.get("burnin_run_id")==rid and t.get("net_r") is not None] for rid in run_ids}}
    aggregate_payload={"campaign_id":campaign_id,"run_ids":run_ids,"metrics":metrics,"blockers":sorted(set(blockers)),"rows":{"observations":len(obs),"trades":len(trades),"rejects":len(rejects),"regimes":len(regimes),"execution":len(execm),"calibration":len(cal),"drawdowns":len(dds)}}
    evidence_hash=canonical_hash(aggregate_payload); metrics["evidence_hash"]=evidence_hash
    return {"status":"OK" if not blockers else "BLOCKED","campaign_id":campaign_id,"release_id":c["release_id"],"metrics":metrics,"blockers":sorted(set(blockers)),"evidence_hash":evidence_hash,"rows":aggregate_payload["rows"]}

def evaluate_campaign(conn: Any, campaign_id: str, thresholds: BurnInThresholds | None = None) -> BurnInQualificationSnapshot:
    thresholds=thresholds or BurnInThresholds(); th=asdict(thresholds); agg=aggregate_campaign(conn,campaign_id); c=get_campaign(conn,campaign_id)
    if not c: raise KeyError("campaign not found")
    blockers=list(agg.get("blockers") or []); warnings=[]; metrics=dict(agg.get("metrics") or {})
    sample_status="PASS"
    checks=[("MINIMUM_DURATION",float(c.get("observed_duration_seconds") or 0),float(c.get("expected_duration_seconds") or thresholds.minimum_duration_seconds)),("MINIMUM_TOTAL_DECISIONS",metrics.get("sample_count",0),int(c.get("target_decisions") or thresholds.minimum_total_decisions)),("MINIMUM_CLOSED_TRADES",metrics.get("closed_trade_count",0),int(c.get("target_closed_trades") or thresholds.minimum_closed_trades)),("MINIMUM_REJECTED_FORWARD_OUTCOMES",metrics.get("completed_rejected_forward_outcomes",0),int(c.get("target_reject_forward_outcomes") or thresholds.minimum_rejected_forward_outcomes))]
    for name, observed, limit in checks:
        if observed < limit: sample_status="INSUFFICIENT"; blockers.append(f"{name}:{observed}<{limit}")
    if metrics.get("pending_reject_labels",0) > int(c.get("pending_backlog_bound") or 0): blockers.append("PENDING_REJECT_BACKLOG")
    if metrics.get("open_position_count",0) > int(c.get("pending_backlog_bound") or 0): blockers.append("PENDING_POSITION_BACKLOG")
    if metrics.get("lower_confidence_bound_expectancy") is None or float(metrics.get("lower_confidence_bound_expectancy") or -999) < thresholds.min_lower_confidence_bound_expectancy: blockers.append("LOWER_CONFIDENCE_BOUND_EXPECTANCY_NOT_POSITIVE")
    evidence_status="PASS" if not any(str(b).startswith("CAMPAIGN_") or "PENDING_" in str(b) for b in blockers) else "FAIL"
    status="CANARY_QUALIFIED" if not blockers else ("BURN_IN_FAILED" if any(str(b).startswith("CAMPAIGN_") for b in blockers) else ("BURN_IN_INSUFFICIENT" if sample_status=="INSUFFICIENT" else "BURN_IN_FAILED"))
    qid="campaign_q_"+canonical_hash({"campaign_id":campaign_id,"evidence_hash":agg.get("evidence_hash"),"generated_at":utc_now()})[:24]
    return BurnInQualificationSnapshot(qualification_id=qid,burnin_run_id=str(c.get("active_run_id") or campaign_id),release_id=str(c["release_id"]),generated_at=utc_now(),status=status,sample_status=sample_status,expectancy_status="PASS" if "LOWER_CONFIDENCE_BOUND_EXPECTANCY_NOT_POSITIVE" not in blockers else "FAIL",execution_status="PASS" if metrics.get("execution_metric_rows",0) else "INSUFFICIENT_EVIDENCE",regime_status="PASS" if metrics.get("regime_metric_rows",0) else "INSUFFICIENT",reject_quality_status="PASS" if metrics.get("completed_rejected_forward_outcomes",0) else "INSUFFICIENT",calibration_status="PASS" if metrics.get("calibration_metric_rows",0) else "INSUFFICIENT",drawdown_status="PASS",concentration_status="PASS",reconciliation_status="PASS",evidence_completeness_status=evidence_status,blockers=sorted(set(blockers)),warnings=warnings,thresholds=th,metrics=metrics,evidence_hash=str(agg.get("evidence_hash")))

def qualify_campaign(engine: Engine, campaign_id: str, thresholds: BurnInThresholds|None=None) -> dict[str,Any]:
    with engine.begin() as conn:
        bootstrap_campaign_schema(conn)
        snap=evaluate_campaign(conn,campaign_id,thresholds)
        BurnInQualificationEngine(engine, thresholds).persist_snapshot(conn,snap)
        agg=aggregate_campaign(conn,campaign_id)
        _exec(conn,"UPDATE burnin_qualification_snapshots SET campaign_id=:cid, source_run_ids_json=:runs, aggregate_evidence_hash=:eh WHERE qualification_id=:qid",{"cid":campaign_id,"runs":json.dumps(snap.metrics.get("source_run_ids",[])),"eh":snap.evidence_hash,"qid":snap.qualification_id})
        _exec(conn,"UPDATE burnin_campaigns SET qualification_status=:s, latest_qualification_id=:qid, evidence_completeness_status=:ev WHERE campaign_id=:cid",{"cid":campaign_id,"s":snap.status,"qid":snap.qualification_id,"ev":snap.evidence_completeness_status})
        event(conn,campaign_id,"QUALIFICATION_SNAPSHOT",burnin_run_id=snap.burnin_run_id,details={"qualification_id":snap.qualification_id,"status":snap.status,"aggregate":agg.get("rows")})
    return {"campaign_id":campaign_id,"qualification_id":snap.qualification_id,"verdict":snap.status,"aggregate_evidence_hash":snap.evidence_hash,"source_run_ids":snap.metrics.get("source_run_ids",[]),"blockers":snap.blockers,"metrics":snap.metrics}

def check_campaign_completion(conn: Any, campaign_id: str) -> dict[str, Any]:
    c=get_campaign(conn,campaign_id)
    if not c: return {"status":"UNAVAILABLE","reason":"NO_CAMPAIGN"}
    agg=aggregate_campaign(conn,campaign_id); metrics=agg.get("metrics",{}); blockers=[]
    if float(c.get("observed_duration_seconds") or 0) < float(c.get("expected_duration_seconds") or 0): blockers.append("DURATION_NOT_MET")
    if int(metrics.get("sample_count") or 0) < int(c.get("target_decisions") or 0): blockers.append("DECISION_TARGET_NOT_MET")
    if int(metrics.get("closed_trade_count") or 0) < int(c.get("target_closed_trades") or 0): blockers.append("CLOSED_TRADE_TARGET_NOT_MET")
    if int(metrics.get("completed_rejected_forward_outcomes") or 0) < int(c.get("target_reject_forward_outcomes") or 0): blockers.append("REJECT_OUTCOME_TARGET_NOT_MET")
    bound=int(c.get("pending_backlog_bound") or 0)
    if int(metrics.get("pending_reject_labels") or 0) > bound or int(metrics.get("open_position_count") or 0) > bound: blockers.append("PENDING_BACKLOG_EXCEEDS_BOUND")
    if c.get("evidence_completeness_status") != "PASS": blockers.append("EVIDENCE_COMPLETENESS_NOT_PASS")
    if not c.get("latest_qualification_id"): blockers.append("FINAL_QUALIFICATION_MISSING")
    if blockers: return {"completed":False,"blockers":blockers}
    final="QUALIFIED" if c.get("qualification_status")=="CANARY_QUALIFIED" else ("SUSPENDED" if c.get("qualification_status")=="CANARY_SUSPENDED" else "COMPLETED")
    _exec(conn,"UPDATE burnin_campaigns SET campaign_status=:s, completed_at=COALESCE(completed_at,:ts) WHERE campaign_id=:cid",{"cid":campaign_id,"s":final,"ts":utc_now()}); event(conn,campaign_id,"CAMPAIGN_COMPLETED",details={"qualification_status":c.get("qualification_status")})
    return {"completed":True,"status":final,"blockers":[]}

def resolve_campaign_batch(conn: Any, campaign_id: str, *, candles_by_symbol: Mapping[str, Sequence[Mapping[str,Any]]] | None = None, failure_threshold: int = 5) -> dict[str, Any]:
    from alphaforge.burnin_resolver import resolve_pending_rejects
    bootstrap_campaign_schema(conn)
    c=get_campaign(conn,campaign_id)
    if not c: raise KeyError("campaign not found")
    before=int(_exec(conn,"SELECT COUNT(*) FROM burnin_pending_reject_labels WHERE campaign_id=:cid AND status IN ('PENDING','READY')",{"cid":campaign_id}).fetchone()[0])
    result=resolve_pending_rejects(conn, candles_by_symbol or {}, campaign_id=campaign_id)
    failed=int(result.get("failed",0))
    if failed > failure_threshold:
        pause_campaign(conn,campaign_id,reason="PENDING_OUTCOME_RESOLVER_FAILURE_THRESHOLD")
    event(conn,campaign_id,"RESOLVER_BATCH",burnin_run_id=c.get("active_run_id"),details={"before":before,"result":result})
    try:
        if hasattr(conn, "commit"): conn.commit()
    except Exception: pass
    return {"campaign_id":campaign_id,"before":before,"result":result}

class BurnInCampaignRunner:
    def __init__(self, engine: Engine, campaign_id: str) -> None:
        self.engine=engine; self.campaign_id=campaign_id; self.runtime=None
    async def run_foreground(self) -> None:
        with self.engine.begin() as conn:
            bootstrap_campaign_schema(conn)
            c=get_campaign(conn,self.campaign_id)
            if not c: raise KeyError("campaign not found")
            if not c.get("active_run_id") or c.get("campaign_status") in {"CREATED","PAUSED","RECOVERY_REQUIRED"}:
                start_or_resume_campaign(conn,self.campaign_id,resume=bool(c.get("active_run_id")))
            mark_worker_started(conn,self.campaign_id,pid=os.getpid())
        old_campaign=os.environ.get("ALPHAFORGE_BURNIN_CAMPAIGN_ID"); old_mode=os.environ.get("EXECUTION_MODE")
        os.environ["ALPHAFORGE_BURNIN_CAMPAIGN_ID"]=self.campaign_id; os.environ["EXECUTION_MODE"]="PAPER"; os.environ["ALPHAFORGE_EXECUTION_MODE"]="PAPER"
        try:
            from alphaforge.runtime import _build_runtime_from_env
            runtime=_build_runtime_from_env(); runtime.persistence_engine=self.engine; self.runtime=runtime
            with self.engine.begin() as conn: update_campaign_heartbeat(conn,self.campaign_id,runtime_status="OPERATING")
            await runtime.start()
        except Exception as exc:
            with self.engine.begin() as conn: mark_worker_failed(conn,self.campaign_id,f"{exc.__class__.__name__}:{exc}")
            raise
        finally:
            if old_campaign is None: os.environ.pop("ALPHAFORGE_BURNIN_CAMPAIGN_ID",None)
            else: os.environ["ALPHAFORGE_BURNIN_CAMPAIGN_ID"]=old_campaign
            if old_mode is None: os.environ.pop("EXECUTION_MODE",None)
            else: os.environ["EXECUTION_MODE"]=old_mode

def launch_campaign_worker(db_path: str | Path, campaign_id: str) -> subprocess.Popen[Any]:
    env={**os.environ,"PYTHONPATH":os.environ.get("PYTHONPATH","src"),"EXECUTION_MODE":"PAPER","ALPHAFORGE_EXECUTION_MODE":"PAPER","ALPHAFORGE_BURNIN_CAMPAIGN_ID":campaign_id}
    return subprocess.Popen([sys.executable,"-m","alphaforge.burnin_cli","--db",str(db_path),"worker","--campaign-id",campaign_id],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)

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
            counts[fname]=len(rows)
            with (root/fname).open("w",newline="") as fh:
                if rows: w=csv.DictWriter(fh,fieldnames=list(dict(rows[0]).keys())); w.writeheader(); w.writerows([dict(r) for r in rows])
                else: csv.writer(fh).writerow(["no_evidence"])
        qs=conn.execute("SELECT * FROM burnin_qualification_snapshots WHERE campaign_id=? OR burnin_run_id IN (%s) ORDER BY id" % (",".join("?" for _ in run_ids) or "''"), [campaign_id,*run_ids]).fetchall()
        (root/"qualification_snapshots.json").write_text(json.dumps([dict(r) for r in qs],indent=2,sort_keys=True,default=str)); counts["qualification_snapshots.json"]=len(qs)
        (root/"config.json").write_text(json.dumps({"config_hash":c["config_hash"],"strategy_config_hash":c["strategy_config_hash"],"universe_hash":c["universe_hash"],"execution_cost_config_hash":c.get("execution_cost_config_hash")},indent=2,sort_keys=True))
        (root/"provenance.json").write_text(json.dumps(c["source_provenance"],indent=2,sort_keys=True))
        manifest={"campaign_id":campaign_id,"release_id":c["release_id"],"git_commit":c["git_commit"],"source_run_ids":run_ids,"config_hashes":{"config_hash":c["config_hash"],"strategy_config_hash":c["strategy_config_hash"],"universe_hash":c["universe_hash"],"execution_cost_config_hash":c.get("execution_cost_config_hash")},"schema_versions":[CAMPAIGN_SCHEMA_VERSION],"generated_at":utc_now(),"evidence_hash":agg.get("evidence_hash"),"row_counts":counts,"completeness_status":c.get("evidence_completeness_status"),"qualification_verdict":c.get("qualification_status")}
        (root/"manifest.json").write_text(json.dumps(manifest,indent=2,sort_keys=True,default=str))
        checks={}
        for p in sorted(root.iterdir()):
            if p.name!="checksums.sha256" and p.is_file(): checks[p.name]=hashlib.sha256(p.read_bytes()).hexdigest()
        (root/"checksums.sha256").write_text("".join(f"{v}  {k}\n" for k,v in checks.items()))
        eid="export_"+canonical_hash({"campaign_id":campaign_id,"checks":checks})[:16]
        conn.execute("INSERT OR REPLACE INTO burnin_campaign_exports(export_id,campaign_id,output_dir,manifest_path,generated_at,evidence_hash,checksums_json,status,schema_version) VALUES (?,?,?,?,?,?,?,?,?)",(eid,campaign_id,str(root),str(root/"manifest.json"),manifest["generated_at"],str(manifest.get("evidence_hash")),json.dumps(checks,sort_keys=True),"EXPORTED",CAMPAIGN_SCHEMA_VERSION)); conn.commit()
        return {"campaign_id":campaign_id,"output_dir":str(root),"manifest":manifest,"checksums":checks}
    finally: conn.close()
