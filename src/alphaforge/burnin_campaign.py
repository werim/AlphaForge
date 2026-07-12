from __future__ import annotations

import argparse, csv, hashlib, json, os, sqlite3, subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from alphaforge.burnin import BurnInRun, bootstrap_burnin_schema, canonical_hash, config_hash as make_config_hash, persist_burnin_run, utc_now, universe_hash as make_universe_hash
from alphaforge.burnin_qualification import BurnInQualificationEngine, BurnInThresholds

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
    for stmt in ["ALTER TABLE burnin_qualification_snapshots ADD COLUMN campaign_id TEXT", "ALTER TABLE burnin_qualification_snapshots ADD COLUMN source_run_ids_json TEXT", "ALTER TABLE burnin_qualification_snapshots ADD COLUMN aggregate_evidence_hash TEXT"]:
        try: _exec(conn, stmt)
        except Exception: pass

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

def create_campaign(conn: Any, *, release_id: str, duration_days: float, symbols: Sequence[str], intervals: Sequence[str], config: Mapping[str,Any]|None=None, strategy_config: Mapping[str,Any]|None=None, source_provenance: Mapping[str,Any]|None=None, target_decisions:int=500, target_closed_trades:int=30, target_reject_forward_outcomes:int=50) -> BurnInCampaign:
    bootstrap_campaign_schema(conn)
    prov=dict(source_provenance or {"provider":"PAPER_MARKET_DATA","source":"operator"})
    if not prov: raise ValueError("missing provenance")
    ch=make_config_hash(config or {"release_id": release_id, "symbols": list(symbols), "intervals": list(intervals)})
    sh=make_config_hash(strategy_config or {"strategy":"default"})
    uh=make_universe_hash(symbols, intervals)
    cid=campaign_id_for(release_id,{"config_hash":ch,"strategy_config_hash":sh,"universe_hash":uh})
    c=BurnInCampaign(cid, release_id, expected_duration_seconds=float(duration_days)*86400, target_decisions=target_decisions, target_closed_trades=target_closed_trades, target_reject_forward_outcomes=target_reject_forward_outcomes, config_hash=ch, strategy_config_hash=sh, universe_hash=uh, git_commit=git_commit(), source_provenance=prov, symbols=list(symbols), intervals=list(intervals))
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

def aggregate_campaign(conn: Any, campaign_id: str) -> dict[str,Any]:
    c=get_campaign(conn,campaign_id)
    if not c: return {"status":"UNAVAILABLE","reason":"NO_CAMPAIGN"}
    runs=[dict(r) for r in _exec(conn,"SELECT r.* FROM burnin_runs r JOIN burnin_campaign_runs cr ON cr.burnin_run_id=r.burnin_run_id WHERE cr.campaign_id=:id ORDER BY cr.continuation_sequence",{"id":campaign_id}).fetchall()]
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
        bootstrap_campaign_schema(conn); agg=aggregate_campaign(conn,campaign_id); c=get_campaign(conn,campaign_id)
        if not c or agg.get("status")!="OK": raise KeyError("campaign evidence missing")
        active=c.get("active_run_id") or (agg["metrics"]["source_run_ids"][-1] if agg["metrics"]["source_run_ids"] else campaign_id)
    snap=BurnInQualificationEngine(engine, thresholds).evaluate(active)
    with engine.begin() as conn:
        _exec(conn,"UPDATE burnin_qualification_snapshots SET campaign_id=:cid, source_run_ids_json=:runs, aggregate_evidence_hash=:eh WHERE qualification_id=:qid",{"cid":campaign_id,"runs":json.dumps(agg["metrics"]["source_run_ids"]),"eh":agg["evidence_hash"],"qid":snap.qualification_id})
        _exec(conn,"UPDATE burnin_campaigns SET qualification_status=:s, latest_qualification_id=:qid, evidence_completeness_status=:ev WHERE campaign_id=:cid",{"cid":campaign_id,"s":snap.status,"qid":snap.qualification_id,"ev":snap.evidence_completeness_status})
        event(conn,campaign_id,"QUALIFICATION_SNAPSHOT",burnin_run_id=active,details={"qualification_id":snap.qualification_id,"status":snap.status})
    return {"campaign_id":campaign_id,"qualification_id":snap.qualification_id,"verdict":snap.status,"aggregate_evidence_hash":agg["evidence_hash"]}

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
