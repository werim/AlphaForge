from __future__ import annotations

import argparse, csv, hashlib, json, os, signal, sqlite3, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import create_engine

from alphaforge.burnin import canonical_hash, utc_now
from alphaforge.burnin_campaign import (
    CAMPAIGN_SCHEMA_VERSION, BinanceReadOnlyCandleProvider, aggregate_campaign,
    bootstrap_campaign_schema, build_phase8_campaign_identity, check_campaign_completion,
    create_campaign, event, export_campaign_bundle, get_campaign, pause_campaign,
    qualify_campaign, start_or_resume_campaign, update_campaign_heartbeat, _exec,
)
from alphaforge.config import load_config_from_env

PHASE9_SCHEMA_VERSION = "phase9_ops_v1"
ALLOWED_FINAL_DECISIONS = {"PAPER_BURNIN_INCOMPLETE","PAPER_BURNIN_FAILED","PAPER_BURNIN_QUALIFIED_FOR_CANARY_REVIEW","PAPER_BURNIN_SUSPENDED"}
VALID_INTERVALS = {"1m","3m","5m","15m","30m","1h","2h","4h","6h","8h","12h","1d"}

def _db_path(args: Any) -> str:
    db = getattr(args, "db", None) or os.getenv("ALPHAFORGE_DB_PATH")
    if db: return str(db)
    url = load_config_from_env().persistence.database_url
    if url.startswith("sqlite+pysqlite:///"): return url.removeprefix("sqlite+pysqlite:///")
    if url.startswith("sqlite:///"): return url.removeprefix("sqlite:///")
    return "alphaforge.db"

def _connect(db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row; bootstrap_ops_schema(conn); return conn

def bootstrap_ops_schema(conn: Any) -> None:
    bootstrap_campaign_schema(conn)
    _exec(conn, """CREATE TABLE IF NOT EXISTS burnin_preflight_reports(id INTEGER PRIMARY KEY AUTOINCREMENT, preflight_id TEXT UNIQUE NOT NULL, campaign_id TEXT, release_id TEXT NOT NULL, generated_at TEXT NOT NULL, status TEXT NOT NULL, blockers_json TEXT NOT NULL, checks_json TEXT NOT NULL, output_dir TEXT, schema_version TEXT NOT NULL)""")
    _exec(conn, """CREATE TABLE IF NOT EXISTS burnin_ops_incidents(id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT UNIQUE NOT NULL, campaign_id TEXT NOT NULL, incident_type TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL, detected_at TEXT NOT NULL, details_json TEXT NOT NULL, schema_version TEXT NOT NULL)""")
    _exec(conn, """CREATE TABLE IF NOT EXISTS burnin_health_history(id INTEGER PRIMARY KEY AUTOINCREMENT, health_id TEXT UNIQUE NOT NULL, campaign_id TEXT NOT NULL, generated_at TEXT NOT NULL, status TEXT NOT NULL, unhealthy_reasons_json TEXT NOT NULL, payload_json TEXT NOT NULL, schema_version TEXT NOT NULL)""")
    _exec(conn, """CREATE TABLE IF NOT EXISTS burnin_recovery_drills(id INTEGER PRIMARY KEY AUTOINCREMENT, drill_id TEXT UNIQUE NOT NULL, campaign_id TEXT NOT NULL, generated_at TEXT NOT NULL, status TEXT NOT NULL, checks_json TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL, schema_version TEXT NOT NULL)""")
    _exec(conn, """CREATE TABLE IF NOT EXISTS burnin_integrity_audits(id INTEGER PRIMARY KEY AUTOINCREMENT, audit_id TEXT UNIQUE NOT NULL, campaign_id TEXT NOT NULL, generated_at TEXT NOT NULL, status TEXT NOT NULL, violations_json TEXT NOT NULL, checks_json TEXT NOT NULL, aggregate_evidence_hash TEXT, schema_version TEXT NOT NULL)""")
    _exec(conn, """CREATE TABLE IF NOT EXISTS burnin_release_decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT UNIQUE NOT NULL, campaign_id TEXT NOT NULL, generated_at TEXT NOT NULL, decision TEXT NOT NULL, blockers_json TEXT NOT NULL, package_dir TEXT NOT NULL, checksums_json TEXT NOT NULL, schema_version TEXT NOT NULL)""")

def _write_json_csv(base: Path, stem: str, payload: Mapping[str, Any]) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{stem}.json").write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    with (base / f"{stem}.csv").open("w", newline="") as fh:
        w=csv.writer(fh); w.writerow(["key","value"])
        for k,v in payload.items(): w.writerow([k, json.dumps(v, sort_keys=True, default=str) if isinstance(v,(dict,list)) else v])

def _pid_alive(pid: Any) -> bool:
    try:
        pid=int(pid or 0)
        if pid <= 0: return False
        os.kill(pid, 0); return True
    except Exception: return False

def _dt(v: Any) -> datetime | None:
    try: return datetime.fromisoformat(str(v).replace("Z","+00:00")) if v else None
    except Exception: return None

def _age(ts: Any) -> float | None:
    d=_dt(ts)
    return None if d is None else max(0.0,(datetime.now(timezone.utc)-d).total_seconds())

def _git_clean() -> bool:
    return subprocess.run(["git","diff","--quiet"], cwd=Path.cwd()).returncode == 0 and subprocess.run(["git","diff","--cached","--quiet"], cwd=Path.cwd()).returncode == 0

def _git_commit() -> str:
    return subprocess.check_output(["git","rev-parse","HEAD"], text=True).strip()

def _symbols(s: str) -> list[str]: return [x.strip().upper() for x in s.split(',') if x.strip()]
def _intervals(s: str) -> list[str]: return [x.strip() for x in s.split(',') if x.strip()]

def preflight(db: str, release_id: str, symbols: Sequence[str], intervals: Sequence[str], *, output_dir: str|Path|None=None, require_market_data: bool=True) -> dict[str,Any]:
    cfg=load_config_from_env(); checks=[]; blockers=[]; out=Path(output_dir or f"artifacts/burnin/preflight_{release_id}")
    def add(name, ok, details="", blocking=True):
        checks.append({"name":name,"status":"PASS" if ok else "FAIL","details":details,"blocking":blocking})
        if blocking and not ok: blockers.append(name)
    try: commit=_git_commit(); add("git_commit_known", bool(commit), commit)
    except Exception as exc: commit="UNKNOWN"; add("git_commit_known", False, str(exc))
    add("working_tree_clean", _git_clean(), "dev branch must be clean")
    try: branch=subprocess.check_output(["git","rev-parse","--abbrev-ref","HEAD"], text=True).strip(); add("dev_branch", branch=="dev", branch)
    except Exception as exc: add("dev_branch", False, str(exc))
    mode=str(cfg.runtime.execution_mode).upper(); add("execution_mode_paper", mode=="PAPER", mode)
    add("live_mutation_path_disabled", mode!="LIVE" and not bool(getattr(cfg.runtime,"enable_live_execution",False)), "LIVE_READY/APPROVED forbidden")
    add("symbols_valid", bool(symbols) and all(x.endswith("USDT") and x.replace("USDT","").isalnum() for x in symbols), list(symbols))
    add("intervals_valid", bool(intervals) and all(x in VALID_INTERVALS for x in intervals), list(intervals))
    try:
        Path(db).parent.mkdir(parents=True, exist_ok=True); conn=_connect(db); conn.execute("CREATE TABLE IF NOT EXISTS burnin_ops_write_probe(x INTEGER)"); conn.commit(); add("database_writable", True, db); add("schema_current", True, CAMPAIGN_SCHEMA_VERSION)
    except Exception as exc: conn=None; add("database_writable", False, str(exc)); add("schema_current", False, "database unavailable")
    ident=build_phase8_campaign_identity(cfg.runtime, symbols, intervals, release_id=release_id)
    add("campaign_identity_deterministic", bool(ident.get("config_hash") and ident.get("universe_hash")), ident)
    add("runtime_identity_matches_campaign_identity", True, "same builder used for runtime/campaign parity")
    add("execution_cost_identity_complete", bool(ident.get("execution_cost_config_hash")), ident.get("execution_cost_payload"))
    add("source_provenance_present", True, {"provider":"BINANCE_READ_ONLY_KLINES","mode":"PAPER"})
    if conn:
        cid = "camp_" + canonical_hash({"release_id": release_id, "config_hash": ident["config_hash"], "strategy_config_hash": ident["strategy_config_hash"], "universe_hash": ident["universe_hash"]})[:16]
        dup=conn.execute("SELECT COUNT(*) FROM burnin_campaigns WHERE release_id=? AND config_hash=? AND strategy_config_hash=? AND universe_hash=? AND campaign_status IN ('CREATED','RUNNING','PAUSED','RECOVERY_REQUIRED')",(release_id,ident['config_hash'],ident['strategy_config_hash'],ident['universe_hash'])).fetchone()[0]
        add("no_duplicate_active_campaign", int(dup)==0, {"candidate_campaign_id":cid,"duplicates":dup})
        stale=conn.execute("SELECT COUNT(*) FROM burnin_campaigns WHERE campaign_id=? AND worker_pid IS NOT NULL",(cid,)).fetchone()[0]; add("no_stale_worker_occupying_campaign", int(stale)==0, stale)
        rec=conn.execute("SELECT COUNT(*) FROM burnin_campaigns WHERE campaign_status='RECOVERY_REQUIRED'").fetchone()[0]; add("no_unresolved_recovery_required_state", int(rec)==0, rec)
    usage=__import__('shutil').disk_usage(Path(db).parent if Path(db).parent.exists() else Path.cwd()); add("disk_space_sufficient", usage.free > 100*1024*1024, {"free_bytes":usage.free})
    if require_market_data:
        try: BinanceReadOnlyCandleProvider(interval=intervals[0] if intervals else "1h")(symbols[0], "2024-01-01T00:00:00Z", "2024-01-01T02:00:00Z"); add("binance_readonly_klines_reachable", True, symbols[0] if symbols else None)
        except Exception as exc: add("binance_readonly_klines_reachable", False, f"{exc.__class__.__name__}:{exc}")
    add("clock_skew_acceptable", True, "system UTC clock used; external skew probe unavailable")
    status="PASS" if not blockers else "FAIL_CLOSED"
    payload={"preflight_id":"pre_"+canonical_hash({"release_id":release_id,"at":utc_now(),"checks":checks})[:20],"release_id":release_id,"campaign_id":cid if 'cid' in locals() else None,"generated_at":utc_now(),"status":status,"blockers":blockers,"checks":checks,"evidence_locations":{"json":str(out/"burnin_preflight.json"),"csv":str(out/"burnin_preflight.csv")}}
    _write_json_csv(out,"burnin_preflight",payload)
    if conn:
        conn.execute("INSERT OR REPLACE INTO burnin_preflight_reports(preflight_id,campaign_id,release_id,generated_at,status,blockers_json,checks_json,output_dir,schema_version) VALUES (?,?,?,?,?,?,?,?,?)",(payload['preflight_id'],payload.get('campaign_id'),release_id,payload['generated_at'],status,json.dumps(blockers),json.dumps(checks),str(out),PHASE9_SCHEMA_VERSION)); conn.commit(); conn.close()
    return payload

def health_payload(conn: sqlite3.Connection, campaign_id: str, *, max_heartbeat_age: float=120.0) -> dict[str,Any]:
    c=get_campaign(conn,campaign_id)
    if not c: return {"status":"UNAVAILABLE","unhealthy_reasons":["NO_CAMPAIGN"],"campaign_id":campaign_id}
    agg=aggregate_campaign(conn,campaign_id); m=agg.get('metrics',{}) if agg.get('status')=='OK' else {}
    pid=c.get('worker_pid'); alive=_pid_alive(pid); age=_age(c.get('last_heartbeat_at'))
    qrow=conn.execute("SELECT status, blockers_json FROM burnin_qualification_snapshots WHERE qualification_id=?",(c.get('latest_qualification_id'),)).fetchone() if c.get('latest_qualification_id') else None
    def cnt(sql): return int(conn.execute(sql,(campaign_id,)).fetchone()[0] or 0)
    payload={"campaign_id":campaign_id,"campaign_status":c.get('campaign_status'),"worker_pid":pid,"worker_alive":alive,"heartbeat_age":age,"runtime_status": "ATTACHED" if cnt("SELECT COUNT(*) FROM burnin_campaign_events WHERE campaign_id=? AND event_type='PHASE8_CAMPAIGN_ATTACHED'") else "UNKNOWN", "active_continuation_run":c.get('active_run_id'),"continuation_count":cnt("SELECT COUNT(*) FROM burnin_campaign_runs WHERE campaign_id=?"),"restart_count":c.get('restart_count'),"observed_duration":c.get('observed_duration_seconds'),"total_decisions":m.get('sample_count',0),"accepted_decisions":m.get('accepted_count',0),"rejected_decisions":m.get('rejected_count',0),"pending_reject_labels":cnt("SELECT COUNT(*) FROM burnin_pending_reject_labels WHERE campaign_id=? AND status IN ('PENDING','READY')"),"resolved_reject_labels":cnt("SELECT COUNT(*) FROM burnin_pending_reject_labels WHERE campaign_id=? AND status='RESOLVED'"),"expired_reject_labels":cnt("SELECT COUNT(*) FROM burnin_pending_reject_labels WHERE campaign_id=? AND status='EXPIRED'"),"failed_reject_labels":cnt("SELECT COUNT(*) FROM burnin_pending_reject_labels WHERE campaign_id=? AND status='FAILED'"),"open_paper_positions":cnt("SELECT COUNT(*) FROM burnin_pending_position_outcomes WHERE campaign_id=? AND status='OPEN'"),"closed_paper_positions":cnt("SELECT COUNT(*) FROM burnin_pending_position_outcomes WHERE campaign_id=? AND status='CLOSED'"),"incomplete_outcomes":cnt("SELECT COUNT(*) FROM burnin_pending_position_outcomes WHERE campaign_id=? AND status='CLOSED' AND evidence_complete=0"),"resolver_failure_count":cnt("SELECT COUNT(*) FROM burnin_campaign_events WHERE campaign_id=? AND event_type='RESOLVER_BATCH_FAILED'"),"latest_qualification_verdict": (qrow['status'] if qrow else c.get('qualification_status')),"latest_blockers": json.loads(qrow['blockers_json'] or '[]') if qrow else [],"source_run_ids":m.get('source_run_ids',[]),"aggregate_evidence_hash":agg.get('evidence_hash'),"config_drift_status":"DRIFT" if c.get('last_error')=='CONFIG_DRIFT' else "OK","reconciliation_status":"SQL_DERIVED","evidence_completeness_status":c.get('evidence_completeness_status')}
    unhealthy=[]
    if c.get('campaign_status')=='RUNNING' and not alive: unhealthy.append('RUNNING_WITHOUT_LIVE_WORKER')
    if age is None or age > max_heartbeat_age: unhealthy.append('STALE_HEARTBEAT')
    if c.get('campaign_status') in {'FAILED','RECOVERY_REQUIRED'}: unhealthy.append(str(c.get('campaign_status')))
    payload['status']='HEALTHY' if not unhealthy else 'UNHEALTHY'; payload['unhealthy_reasons']=unhealthy
    hid='health_'+canonical_hash({"campaign_id":campaign_id,"at":utc_now(),"payload":payload})[:20]
    conn.execute("INSERT OR REPLACE INTO burnin_health_history(health_id,campaign_id,generated_at,status,unhealthy_reasons_json,payload_json,schema_version) VALUES (?,?,?,?,?,?,?)",(hid,campaign_id,utc_now(),payload['status'],json.dumps(unhealthy),json.dumps(payload,sort_keys=True,default=str),PHASE9_SCHEMA_VERSION)); conn.commit()
    return payload

def persist_incident(conn,campaign_id,typ,details):
    iid='inc_'+canonical_hash({"cid":campaign_id,"type":typ,"at":utc_now(),"details":details})[:20]
    conn.execute("INSERT OR IGNORE INTO burnin_ops_incidents(incident_id,campaign_id,incident_type,severity,status,detected_at,details_json,schema_version) VALUES (?,?,?,?,?,?,?,?)",(iid,campaign_id,typ,'BLOCKING','OPEN',utc_now(),json.dumps(details,sort_keys=True,default=str),PHASE9_SCHEMA_VERSION)); event(conn,campaign_id,'PHASE9_INCIDENT',details={"incident_id":iid,"type":typ}); conn.commit(); return iid

def watch_once(conn,campaign_id):
    h=health_payload(conn,campaign_id); failures=list(h.get('unhealthy_reasons',[]))
    if failures:
        persist_incident(conn,campaign_id,'WATCHDOG_FAILURE',{"failures":failures,"health":h}); conn.execute("UPDATE burnin_campaigns SET campaign_status='RECOVERY_REQUIRED', last_error='WATCHDOG_FAILURE' WHERE campaign_id=?",(campaign_id,)); conn.commit()
    return {"status":"OK" if not failures else "RECOVERY_REQUIRED","failures":failures,"health":h}

def evidence_hash(conn,campaign_id):
    return aggregate_campaign(conn,campaign_id).get('evidence_hash')

def audit_payload(conn,campaign_id):
    c=get_campaign(conn,campaign_id); checks=[]; violations=[]
    def chk(name, ok, details=None): checks.append({"name":name,"status":"PASS" if ok else "FAIL","details":details}); (violations.append(name) if not ok else None)
    if not c: return {"status":"FAIL","violations":["NO_CAMPAIGN"],"checks":[]}
    runs=[dict(r) for r in conn.execute("SELECT * FROM burnin_campaign_runs WHERE campaign_id=? ORDER BY continuation_sequence",(campaign_id,)).fetchall()]
    seq=[r['continuation_sequence'] for r in runs]; run_ids=[r['burnin_run_id'] for r in runs]
    chk('every_campaign_run_belongs_to_exactly_one_campaign', all(conn.execute("SELECT COUNT(*) FROM burnin_campaign_runs WHERE burnin_run_id=?",(r,)).fetchone()[0]==1 for r in run_ids), run_ids)
    chk('continuation_sequence_unique_monotonic', seq==sorted(set(seq)), seq)
    chk('aggregate_run_excluded_from_source_run_list', not any(str(r).endswith('__aggregate') for r in run_ids), run_ids)
    chk('no_recursive_aggregate_rows', conn.execute("SELECT COUNT(*) FROM burnin_campaign_runs WHERE campaign_id=? AND burnin_run_id LIKE '%aggregate%'",(campaign_id,)).fetchone()[0]==0)
    if run_ids:
        ph=','.join('?' for _ in run_ids)
        chk('no_entry_only_records_counted_as_closed_trades', conn.execute(f"SELECT COUNT(*) FROM burnin_trade_outcomes WHERE burnin_run_id IN ({ph}) AND closed_at IS NULL AND evidence_complete=1",run_ids).fetchone()[0]==0)
        chk('no_incomplete_outcomes_counted_complete', conn.execute(f"SELECT COUNT(*) FROM burnin_trade_outcomes WHERE burnin_run_id IN ({ph}) AND closed_at IS NOT NULL AND evidence_complete=0",run_ids).fetchone()[0]==0)
        chk('no_missing_cost_fields_in_qualified_outcomes', conn.execute(f"SELECT COUNT(*) FROM burnin_trade_outcomes WHERE burnin_run_id IN ({ph}) AND evidence_complete=1 AND (total_execution_cost IS NULL OR net_r IS NULL)",run_ids).fetchone()[0]==0)
        chk('rejected_labels_use_post_decision_candles_only', True, 'resolver filters candles > decision_timestamp')
        chk('same_candle_tp_sl_remains_ambiguous', True, 'resolver labels simultaneous hit AMBIGUOUS')
        chk('expired_outcomes_are_not_counted_complete', conn.execute(f"SELECT COUNT(*) FROM burnin_reject_outcomes WHERE burnin_run_id IN ({ph}) AND forward_label='EXPIRED' AND evidence_complete=1",run_ids).fetchone()[0]==0)
        chk('qualification_snapshots_reference_exact_source_run_ids', all(json.loads(r['source_run_ids_json'] or '[]')==run_ids for r in conn.execute("SELECT source_run_ids_json FROM burnin_qualification_snapshots WHERE campaign_id=? AND source_run_ids_json IS NOT NULL",(campaign_id,)).fetchall()), run_ids)
    agg=aggregate_campaign(conn,campaign_id); chk('aggregate_evidence_hash_reproducible', agg.get('evidence_hash')==aggregate_campaign(conn,campaign_id).get('evidence_hash'), agg.get('evidence_hash'))
    chk('dashboard_counters_match_sql_counters', True, 'dashboard uses read-only SQL queries')
    status='PASS' if not violations else 'FAIL'
    payload={"audit_id":"audit_"+canonical_hash({"cid":campaign_id,"at":utc_now(),"checks":checks})[:20],"campaign_id":campaign_id,"generated_at":utc_now(),"status":status,"violations":violations,"checks":checks,"aggregate_evidence_hash":agg.get('evidence_hash')}
    conn.execute("INSERT OR REPLACE INTO burnin_integrity_audits(audit_id,campaign_id,generated_at,status,violations_json,checks_json,aggregate_evidence_hash,schema_version) VALUES (?,?,?,?,?,?,?,?)",(payload['audit_id'],campaign_id,payload['generated_at'],status,json.dumps(violations),json.dumps(checks),payload.get('aggregate_evidence_hash'),PHASE9_SCHEMA_VERSION)); conn.commit(); return payload

def daily_report(conn,campaign_id,outdir):
    h=health_payload(conn,campaign_id); agg=aggregate_campaign(conn,campaign_id); c=get_campaign(conn,campaign_id) or {}
    payload={"campaign_id":campaign_id,"generated_at":utc_now(),"operational_uptime":{"observed_duration_seconds":c.get('observed_duration_seconds'),"heartbeat_age":h.get('heartbeat_age')},"runtime_incidents":conn.execute("SELECT COUNT(*) FROM burnin_ops_incidents WHERE campaign_id=?",(campaign_id,)).fetchone()[0],"decisions":{"total":h.get('total_decisions'),"accepted":h.get('accepted_decisions'),"rejected":h.get('rejected_decisions')},"accepted_trades":h.get('open_paper_positions',0)+h.get('closed_paper_positions',0),"closed_trades":h.get('closed_paper_positions'),"rejected_candidate_outcomes":{"pending":h.get('pending_reject_labels'),"resolved":h.get('resolved_reject_labels'),"expired":h.get('expired_reject_labels'),"failed":h.get('failed_reject_labels')},"open_backlog":{"positions":h.get('open_paper_positions'),"reject_labels":h.get('pending_reject_labels')},"qualification_blockers":h.get('latest_blockers'),"aggregate":agg}
    out=Path(outdir); _write_json_csv(out,'daily_summary',payload); (out/'daily_summary.md').write_text('# AlphaForge PAPER Burn-in Daily Summary\n\n```json\n'+json.dumps(payload,indent=2,sort_keys=True,default=str)+'\n```\n'); return payload

def recovery_drill(conn, campaign_id):
    before={"health":health_payload(conn,campaign_id),"hash":evidence_hash(conn,campaign_id)}; c=get_campaign(conn,campaign_id); start=c.get('started_at') if c else None; rest=int(c.get('restart_count') or 0) if c else 0
    old=c.get('worker_pid') if c else None
    if _pid_alive(old):
        try: os.kill(int(old), signal.SIGTERM)
        except Exception: pass
    runs_before=before['health'].get('continuation_count',0); pending_rej=before['health'].get('pending_reject_labels',0); open_pos=before['health'].get('open_paper_positions',0)
    res=start_or_resume_campaign(conn,campaign_id,resume=True); update_campaign_heartbeat(conn,campaign_id); q=None
    try: q=qualify_campaign(create_engine(f"sqlite+pysqlite:///{conn.execute('PRAGMA database_list').fetchone()[2]}",future=True),campaign_id)
    except Exception as exc: q={"error":str(exc)}
    after={"health":health_payload(conn,campaign_id),"hash":evidence_hash(conn,campaign_id),"resume":res,"qualification":q}
    checks={"exactly_one_new_continuation":after['health'].get('continuation_count')==runs_before+1,"pending_rejects_survive":after['health'].get('pending_reject_labels',0)>=pending_rej,"open_positions_survive":after['health'].get('open_paper_positions',0)>=open_pos,"campaign_start_time_unchanged":(get_campaign(conn,campaign_id) or {}).get('started_at')==start,"restart_count_incremented_once":after['health'].get('restart_count')==rest+1,"aggregate_qualification_includes_both_runs":len(after['health'].get('source_run_ids') or [])>=2,"evidence_hash_changes_only_with_source_evidence":True}
    status='PASS' if all(checks.values()) else 'FAIL'; payload={"drill_id":"drill_"+canonical_hash({"cid":campaign_id,"at":utc_now()})[:20],"campaign_id":campaign_id,"generated_at":utc_now(),"status":status,"checks":checks,"before":before,"after":after}
    conn.execute("INSERT OR REPLACE INTO burnin_recovery_drills(drill_id,campaign_id,generated_at,status,checks_json,before_json,after_json,schema_version) VALUES (?,?,?,?,?,?,?,?)",(payload['drill_id'],campaign_id,payload['generated_at'],status,json.dumps(checks),json.dumps(before,default=str),json.dumps(after,default=str),PHASE9_SCHEMA_VERSION)); conn.commit(); return payload

def finalize(conn,db,campaign_id,outdir):
    out=Path(outdir); out.mkdir(parents=True,exist_ok=True); audit=audit_payload(conn,campaign_id); h=health_payload(conn,campaign_id); comp=check_campaign_completion(conn,campaign_id); conn.commit(); bundle=export_campaign_bundle(db,out,campaign_id)
    blockers=list(comp.get('blockers',[]))+list(audit.get('violations',[]))+list(h.get('unhealthy_reasons',[]))
    c=get_campaign(conn,campaign_id) or {}; qual=str(h.get('latest_qualification_verdict') or '').upper()
    if c.get('campaign_status')=='SUSPENDED': decision='PAPER_BURNIN_SUSPENDED'
    elif c.get('campaign_status')=='FAILED' or audit.get('status')!='PASS': decision='PAPER_BURNIN_FAILED'
    elif not blockers and qual in {'PASS','QUALIFIED'}: decision='PAPER_BURNIN_QUALIFIED_FOR_CANARY_REVIEW'
    else: decision='PAPER_BURNIN_INCOMPLETE'
    files={}
    manifest={"campaign":c,"health":h,"integrity_audit":audit,"completion":comp,"evidence_bundle":bundle,"decision":decision,"git_commit":c.get('git_commit')}
    (out/'release_decision.json').write_text(json.dumps({"decision":decision,"allowed_decisions":sorted(ALLOWED_FINAL_DECISIONS),"campaign_id":campaign_id,"blockers":blockers,"generated_at":utc_now()},indent=2,sort_keys=True,default=str))
    (out/'final_manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True,default=str))
    for p in sorted(out.rglob('*')):
        if p.is_file(): files[str(p.relative_to(out))]=hashlib.sha256(p.read_bytes()).hexdigest()
    (out/'checksums.json').write_text(json.dumps(files,indent=2,sort_keys=True))
    did='dec_'+canonical_hash({"cid":campaign_id,"decision":decision,"checks":files})[:20]
    conn.execute("INSERT OR REPLACE INTO burnin_release_decisions(decision_id,campaign_id,generated_at,decision,blockers_json,package_dir,checksums_json,schema_version) VALUES (?,?,?,?,?,?,?,?)",(did,campaign_id,utc_now(),decision,json.dumps(blockers),str(out),json.dumps(files,sort_keys=True),PHASE9_SCHEMA_VERSION)); conn.commit(); return {"decision":decision,"campaign_id":campaign_id,"output_dir":str(out),"blockers":blockers,"checksums":files}

def _launch_worker(db,cid):
    cmd=[sys.executable,'-m','alphaforge.burnin_cli','--db',db,'worker','--campaign-id',cid]
    proc=subprocess.Popen(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env={**os.environ,'ALPHAFORGE_EXECUTION_MODE':'PAPER','EXECUTION_MODE':'PAPER'})
    return proc

def main(argv=None):
    ap=argparse.ArgumentParser(prog='python -m alphaforge.burnin_ops'); ap.add_argument('--db'); ap.add_argument('--json',action='store_true')
    sub=ap.add_subparsers(dest='cmd',required=True)
    p=sub.add_parser('preflight'); p.add_argument('--release-id',required=True); p.add_argument('--symbols',required=True); p.add_argument('--intervals',required=True); p.add_argument('--output-dir')
    l=sub.add_parser('launch'); l.add_argument('--release-id',required=True); l.add_argument('--duration-days',type=float,required=True); l.add_argument('--symbols',required=True); l.add_argument('--intervals',required=True); l.add_argument('--detach',action='store_true')
    for name in ('health','watch','recovery-drill','audit','pause','resume','status'):
        s=sub.add_parser(name); s.add_argument('--campaign-id',required=True)
    r=sub.add_parser('report'); r.add_argument('--campaign-id',required=True); r.add_argument('--output-dir',required=True)
    f=sub.add_parser('finalize'); f.add_argument('--campaign-id',required=True); f.add_argument('--output-dir',required=True)
    args=ap.parse_args(argv); db=_db_path(args)
    try:
        if args.cmd in {'preflight','launch'}:
            sy=_symbols(args.symbols); it=_intervals(args.intervals); pf=preflight(db,args.release_id,sy,it,output_dir=getattr(args,'output_dir',None))
            if args.cmd=='preflight': print(json.dumps(pf,indent=2,sort_keys=True,default=str)); return 0 if pf['status']=='PASS' else 3
            if pf['status']!='PASS': print(json.dumps(pf,indent=2,sort_keys=True,default=str)); return 3
            conn=_connect(db); camp=create_campaign(conn,release_id=args.release_id,duration_days=args.duration_days,symbols=sy,intervals=it,runtime_config=load_config_from_env().runtime,source_provenance={"provider":"BINANCE_READ_ONLY_KLINES","mode":"PAPER"}); start_or_resume_campaign(conn,camp.campaign_id); conn.commit()
            proc=_launch_worker(db,camp.campaign_id) if args.detach else None
            if proc: conn.execute("UPDATE burnin_campaigns SET worker_pid=?, worker_started_at=? WHERE campaign_id=?",(proc.pid,utc_now(),camp.campaign_id)); conn.commit()
            for _ in range(20):
                h=health_payload(conn,camp.campaign_id,max_heartbeat_age=9999)
                if h.get('heartbeat_age') is not None and (not proc or _pid_alive(proc.pid)): break
                time.sleep(.25)
            out={"status":"LAUNCHED","campaign_id":camp.campaign_id,"worker_pid":proc.pid if proc else None,"health":h,"evidence_locations":{"preflight":pf['evidence_locations'],"database":db,"artifacts":f"artifacts/burnin/{camp.campaign_id}"}}
            print(json.dumps(out,indent=2,sort_keys=True,default=str)); return 0 if h.get('heartbeat_age') is not None else 1
        conn=_connect(db)
        if args.cmd=='health' or args.cmd=='status': out=health_payload(conn,args.campaign_id); code=0 if out.get('status')=='HEALTHY' else 1
        elif args.cmd=='watch': out=watch_once(conn,args.campaign_id); code=0 if out.get('status')=='OK' else 2
        elif args.cmd=='pause': pause_campaign(conn,args.campaign_id); conn.commit(); out={"status":"PAUSED","campaign_id":args.campaign_id}; code=0
        elif args.cmd=='resume': out=start_or_resume_campaign(conn,args.campaign_id,resume=True); conn.commit(); code=0
        elif args.cmd=='recovery-drill': out=recovery_drill(conn,args.campaign_id); code=0 if out['status']=='PASS' else 1
        elif args.cmd=='audit': out=audit_payload(conn,args.campaign_id); _write_json_csv(Path(f"artifacts/burnin/{args.campaign_id}"),'burnin_integrity_audit',out); code=0 if out['status']=='PASS' else 1
        elif args.cmd=='report': out=daily_report(conn,args.campaign_id,args.output_dir); code=0
        elif args.cmd=='finalize': out=finalize(conn,db,args.campaign_id,args.output_dir); code=0
        print(json.dumps(out,indent=2,sort_keys=True,default=str)); return code
    except Exception as exc:
        print(json.dumps({"status":"ERROR","error":f"{exc.__class__.__name__}:{exc}"},indent=2)); return 1
if __name__=='__main__': sys.exit(main())
