"""Strictly read-only LIVE readiness evidence observer (v1)."""
from __future__ import annotations
import argparse, json, sqlite3, subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from alphaforge.release_gates import (
    rollback_verification_evidence_valid,
    runbook_verification_evidence_valid,
)

PASS, BLOCKED, NOT_OBSERVABLE, NEEDS_FIX = "PASS", "BLOCKED", "NOT_OBSERVABLE", "NEEDS_FIX"
IDS = ("GIT_IDENTITY","WORKTREE_CLEAN","SCHEMA_VALID","PREFLIGHT_PASS","CAMPAIGN_HEALTH","CONFIG_DRIFT","STRATEGY_DRIFT","RECONCILIATION","DUPLICATE_EXECUTION","CONTAMINATION","RUNTIME_ERRORS","LIVE_MUTATION_DISABLED","ACCEPTED_LIFECYCLE_EVIDENCE","REJECT_FORWARD_OUTCOME_EVIDENCE","EXPECTANCY_TEMPORAL_EVIDENCE","EXECUTION_COST_EVIDENCE","RECOVERY_DRILL","SOAK_EVIDENCE","ROLLBACK_EVIDENCE","RUNBOOK_EVIDENCE")
TABLES = {"burnin_campaigns","burnin_runs","burnin_campaign_runs","burnin_preflight_reports","runtime_state_snapshots","order_decisions","trade_lifecycle_events","burnin_observations","burnin_reject_outcomes","expectancy_evidence","burnin_qualification_snapshots","burnin_recovery_drills","live_rollback_validation_evidence","rollback_verification_events","runbook_evidence","runtime_control_state","runtime_control_audit_events","schema_migrations"}
@dataclass(frozen=True)
class Gate:
    gate_id:str; status:str; reason:str; evidence_source:str; observed_value:Any; expected_condition:str; timestamp:str; scope_identity:dict[str,Any]; blocker_severity:str="CRITICAL"
    def data(self): return asdict(self)
def at(v):
    try:
        d=datetime.fromisoformat(str(v).replace("Z","+00:00")); return d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError,ValueError): return None
def ts(v): return v.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
def obj(v):
    try:
        value=json.loads(v or "{}")
        return value if isinstance(value,dict) else {}
    except (TypeError,ValueError,json.JSONDecodeError):
        return {}
def authoritative_reject(row):
    payload=obj(row.get("payload_json"))
    return (payload.get("reject_quality_attributable") is not False
            and payload.get("forward_label_subject")!="LEGACY_SCANNER_SHADOW_CANDIDATE")
def authoritative_expectancy(row,authoritative_reject_ids):
    if str(row.get("evidence_type") or "").upper()!="REJECT_FORWARD":
        return True
    source=str(row.get("source_decision_id") or "")
    return bool(source and source in authoritative_reject_ids)
def observation_signal_id(row):
    return str(obj(row.get("metrics_json")).get("signal_id") or "").strip()
def scoped_rows(rows,signal_ids):
    return [row for row in rows if str(row.get("signal_id") or "").strip() in signal_ids]
def rollback_source_validation_valid(row, expected_validation_id):
    if not row or str(row.get("validation_id") or "") != str(expected_validation_id or ""):
        return False
    try:
        blockers=json.loads(row.get("blocking_reasons") or "[]")
    except (TypeError,ValueError,json.JSONDecodeError):
        blockers=["INVALID_BLOCKERS_JSON"]
    return (
        str(row.get("evidence_status") or "").upper()=="COMPLETE"
        and str(row.get("rollback_evidence_source") or "")=="DETERMINISTIC_VALIDATION"
        and bool(row.get("kill_switch_block_verified"))
        and bool(row.get("no_submit_on_kill_switch_verified"))
        and bool(row.get("fail_closed_reconciliation_verified"))
        and bool(row.get("repair_actions_non_mutating_verified"))
        and int(row.get("execution_mutation_attempt_count") or 0)==0
        and blockers==[]
    )
class DB:
    denied={sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE,sqlite3.SQLITE_CREATE_TABLE,sqlite3.SQLITE_DROP_TABLE,sqlite3.SQLITE_ALTER_TABLE,sqlite3.SQLITE_ATTACH,sqlite3.SQLITE_DETACH,sqlite3.SQLITE_TRANSACTION}
    def __init__(self,p): self.p=Path(p).resolve(); self.c=None
    def __enter__(self):
        if not self.p.is_file(): raise FileNotFoundError(self.p)
        self.c=sqlite3.connect(self.p.as_uri()+"?mode=ro",uri=True); self.c.row_factory=sqlite3.Row; self.c.execute("PRAGMA query_only=ON"); self.c.set_authorizer(lambda action,*_: sqlite3.SQLITE_DENY if action in self.denied else sqlite3.SQLITE_OK); return self
    def __exit__(self,*_): self.c.close()
    def rows(self,q,p=()): return [dict(x) for x in self.c.execute(q,p)]
    def one(self,q,p=()):
        r=self.rows(q,p); return r[0] if r else None
    def tables(self): return {x["name"] for x in self.rows("SELECT name FROM sqlite_master WHERE type='table'")}
def git(repo):
    def r(*a):
        p=subprocess.run(["git",*a],cwd=repo,text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,check=False); return p.stdout.strip() if p.returncode==0 else None
    return {"commit":r("rev-parse","HEAD"),"branch":r("branch","--show-current"),"dirty":bool(r("status","--porcelain"))}
class LiveReadinessAgent:
    def __init__(self,db_path,campaign_id,*,repo=None,now=None,git_metadata=git,heartbeat_max_age_seconds=300): self.path=Path(db_path); self.cid=campaign_id; self.repo=Path(repo or Path.cwd()); self.now=now or datetime.now(timezone.utc).replace(microsecond=0); self.gm=git_metadata; self.age=heartbeat_max_age_seconds; self.scope={"campaign_id":campaign_id,"burnin_run_id":None,"release_id":None}
    def g(self,i,s,r,src,o,e): return Gate(i,s,r,src,o,e,ts(self.now),dict(self.scope))
    def miss(self,i,src,e): return self.g(i,NOT_OBSERVABLE,"AUTHORITATIVE_EVIDENCE_MISSING",src,None,e)
    def latest(self,rs,k): return max(rs,key=lambda x:str(x.get(k) or ""),default=None)
    def status(self,db,i,t,w,p,k,e):
        if t not in db.tables(): return self.miss(i,t,e)
        r=self.latest(db.rows(f"SELECT * FROM {t} WHERE {w}",p),k)
        if not r:return self.miss(i,t,e)
        return self.g(i,PASS if str(r.get("status")).upper()==PASS else BLOCKED,f"{i}_PASS" if str(r.get("status")).upper()==PASS else f"{i}_NOT_PASS",t,r,e)
    def evaluate(self):
        with DB(self.path) as db:
            tabs=db.tables(); c=db.one("SELECT * FROM burnin_campaigns WHERE campaign_id=?",(self.cid,)) if "burnin_campaigns" in tabs else None
            if c:self.scope.update(burnin_run_id=c.get("active_run_id"),release_id=c.get("release_id"))
            m=self.gm(self.repo); src="git metadata + campaign provenance"
            g1=self.miss("GIT_IDENTITY",src,"campaign commit equals evaluator commit") if not c or not m.get("commit") else self.g("GIT_IDENTITY",PASS if c.get("git_commit")==m["commit"] else BLOCKED,"COMMIT_MATCH" if c.get("git_commit")==m["commit"] else "CAMPAIGN_COMMIT_MISMATCH",src,{"campaign":c.get("git_commit"),"checkout":m["commit"]},"campaign commit equals evaluator commit")
            g2=self.g("WORKTREE_CLEAN",PASS if not m.get("dirty") else BLOCKED,"CLEAN" if not m.get("dirty") else "WORKTREE_DIRTY","git status --porcelain",{"dirty":m.get("dirty")},"evaluator worktree is clean")
            absent=sorted(TABLES-tabs); g3=self.g("SCHEMA_VALID",PASS if not absent else NOT_OBSERVABLE,"CANONICAL_SCHEMA_PRESENT" if not absent else "REQUIRED_TABLES_MISSING","sqlite_master + schema_migrations",{"missing":absent},"canonical schema exists")
            g4=self.status(db,"PREFLIGHT_PASS","burnin_preflight_reports","campaign_id=?",(self.cid,),"generated_at","latest preflight PASS")
            h=at(c.get("last_heartbeat_at")) if c else None; healthy=c and str(c.get("campaign_status")).upper() in {"RUNNING","COMPLETED"} and not c.get("last_error")
            g5=self.miss("CAMPAIGN_HEALTH","burnin_campaigns heartbeat","fresh operational campaign") if not h or (self.now-h).total_seconds()>self.age else self.g("CAMPAIGN_HEALTH",PASS if healthy else BLOCKED,"CAMPAIGN_HEALTHY" if healthy else "CAMPAIGN_NOT_OPERATIONAL","burnin_campaigns heartbeat",c,"fresh operational campaign")
            run=db.one("SELECT * FROM burnin_runs WHERE burnin_run_id=?",(self.scope["burnin_run_id"],)) if self.scope["burnin_run_id"] else None; link=db.one("SELECT * FROM burnin_campaign_runs WHERE campaign_id=? AND burnin_run_id=?",(self.cid,self.scope["burnin_run_id"])) if run else None
            def drift(field,i):
                if not c or not run or not link:return self.miss(i,"campaign/run identity",f"{field} matches")
                ok=c.get(field)==run.get(field) and run.get("status")==link.get("status"); return self.g(i,PASS if ok else BLOCKED,"IDENTITY_MATCH" if ok else f"{i}_DETECTED","burnin_campaigns + burnin_runs + burnin_campaign_runs",{"campaign":c.get(field),"run":run.get(field)},f"{field} matches")
            g6,g7=drift("config_hash","CONFIG_DRIFT"),drift("strategy_config_hash","STRATEGY_DRIFT")
            s=self.latest(db.rows("SELECT * FROM runtime_state_snapshots WHERE campaign_id=? AND burnin_run_id=?",(self.cid,self.scope["burnin_run_id"])),"timestamp") if "runtime_state_snapshots" in tabs else None; clean=s and str(s.get("reconciliation_status")).upper()=="CLEAN" and not int(s.get("reconciliation_mismatch_count") or 0) and not int(s.get("recovery_action_required") or 0)
            g8=self.miss("RECONCILIATION","runtime_state_snapshots","clean reconciliation") if not s else self.g("RECONCILIATION",PASS if clean else BLOCKED,"RECONCILIATION_CLEAN" if clean else "RECONCILIATION_NOT_CLEAN","runtime_state_snapshots",s,"clean reconciliation")
            obs=db.rows("SELECT symbol,decision,execution_mode,metrics_json FROM burnin_observations WHERE burnin_run_id=?",(self.scope["burnin_run_id"],)) if "burnin_observations" in tabs else []
            scoped_signal_ids={sid for sid in (observation_signal_id(x) for x in obs) if sid}
            accepted_signal_ids={observation_signal_id(x) for x in obs if str(x.get("decision") or "").upper()=="ACCEPTED" and observation_signal_id(x)}
            decision_rows=db.rows("SELECT * FROM order_decisions WHERE UPPER(mode)='PAPER'") if "order_decisions" in tabs else []
            scoped_decisions=scoped_rows(decision_rows,scoped_signal_ids)
            counts={}
            for row in scoped_decisions:
                decision_id=str(row.get("decision_id") or "")
                if decision_id: counts[decision_id]=counts.get(decision_id,0)+1
            du=[{"decision_id":decision_id,"n":count} for decision_id,count in counts.items() if count>1]
            g9=self.miss("DUPLICATE_EXECUTION","burnin_observations + order_decisions","scoped decision identity exists") if not scoped_signal_ids else self.g("DUPLICATE_EXECUTION",BLOCKED if du else PASS,"DUPLICATE_EXECUTION_DETECTED" if du else "NO_DUPLICATES","burnin_observations + order_decisions",du,"no duplicate decision IDs in active run")
            try: declared={str(x).upper() for x in json.loads(c.get("symbols_json") or "[]")} if c else set()
            except (TypeError,ValueError): declared=set()
            outside=sorted({str(x["symbol"]).upper() for x in obs if x.get("symbol")}-declared); g10=self.miss("CONTAMINATION","campaign universe + burnin_observations","no out-of-universe observations") if not obs else self.g("CONTAMINATION",BLOCKED if outside else PASS,"OUT_OF_UNIVERSE_OBSERVATION" if outside else "NO_CONTAMINATION","campaign universe + burnin_observations",outside,"no out-of-universe observations")
            lifecycle_rows=db.rows("SELECT signal_id,lifecycle_state,mode FROM trade_lifecycle_events WHERE UPPER(mode)='PAPER'") if "trade_lifecycle_events" in tabs else []
            scoped_lifecycle=scoped_rows(lifecycle_rows,scoped_signal_ids)
            lifecycle_errors=[x for x in scoped_lifecycle if str(x.get("lifecycle_state") or "").upper() in {"ERROR","EXECUTION_ERROR"}]
            errs=(c and c.get("last_error")) or lifecycle_errors; g11=self.g("RUNTIME_ERRORS",BLOCKED if errs else PASS,"RUNTIME_ERRORS_PRESENT" if errs else "NO_RUNTIME_ERRORS","campaign + scoped lifecycle errors",errs,"no runtime errors in active run")
            ctl=self.latest(db.rows("SELECT * FROM runtime_control_state"),"updated_at") if "runtime_control_state" in tabs else None; acts=db.rows("SELECT action FROM runtime_control_audit_events WHERE UPPER(action) GLOB '*SUBMIT*' OR UPPER(action) GLOB '*CANCEL*' OR UPPER(action) GLOB '*AMEND*' OR UPPER(requested_mode)='LIVE'") if "runtime_control_audit_events" in tabs else []; mode=str((ctl or {}).get("mode_running") or (ctl or {}).get("mode_requested") or "").upper(); g12=self.miss("LIVE_MUTATION_DISABLED","runtime control evidence","PAPER mode/no mutation") if not ctl else self.g("LIVE_MUTATION_DISABLED",PASS if mode=="PAPER" and not acts else BLOCKED,"LIVE_MUTATION_GUARD_CONFIRMED" if mode=="PAPER" and not acts else "LIVE_MUTATION_OR_MODE_DETECTED","runtime control evidence",{"mode":mode,"actions":acts},"PAPER mode/no mutation")
            acc=[x for x in scoped_decisions if str(x.get("signal_id") or "") in accepted_signal_ids and str(x.get("decision") or "").upper() in {"ACCEPT","ACCEPTED"}]
            lifecycle_signals={str(x.get("signal_id") or "") for x in scoped_lifecycle}
            missing=[x["signal_id"] for x in acc if str(x.get("signal_id") or "") not in lifecycle_signals]
            g13=self.miss("ACCEPTED_LIFECYCLE_EVIDENCE","burnin_observations + decisions + lifecycle","accepted lifecycle exists in active run") if not accepted_signal_ids or not acc else self.g("ACCEPTED_LIFECYCLE_EVIDENCE",NEEDS_FIX if missing else PASS,"ACCEPTED_LIFECYCLE_MISSING" if missing else "ACCEPTED_LIFECYCLE_PRESENT","burnin_observations + decisions + lifecycle",missing,"accepted lifecycle exists in active run")
            rej=db.rows("SELECT * FROM burnin_reject_outcomes WHERE burnin_run_id=?",(self.scope["burnin_run_id"],)) if "burnin_reject_outcomes" in tabs else []
            good=[x for x in rej if int(x.get("evidence_complete") or 0) and x.get("hypothetical_net_r_after_costs") is not None and authoritative_reject(x)]
            authoritative_reject_ids={str(obj(x.get("payload_json")).get("reject_decision_id") or "") for x in good}
            authoritative_reject_ids.discard("")
            g14=self.g("REJECT_FORWARD_OUTCOME_EVIDENCE",PASS if good else NOT_OBSERVABLE,"COMPLETE_REJECT_OUTCOMES_PRESENT" if good else "AUTHORITATIVE_EVIDENCE_MISSING","burnin_reject_outcomes",{"rows":len(rej),"complete":len(good)},"complete attributable reject outcome exists")
            ev=db.rows("SELECT * FROM expectancy_evidence WHERE campaign_id=? AND run_id=?",(self.cid,self.scope["burnin_run_id"])) if "expectancy_evidence" in tabs else []
            valid=[x for x in ev if int(x.get("evidence_complete") or 0) and x.get("net_r") is not None and at(x.get("decision_time")) and at(x.get("resolved_at")) and at(x["decision_time"])<=at(x["resolved_at"])<=self.now and authoritative_expectancy(x,authoritative_reject_ids)]
            g15=self.g("EXPECTANCY_TEMPORAL_EVIDENCE",PASS if valid else NOT_OBSERVABLE,"TEMPORAL_EXPECTANCY_EVIDENCE_PRESENT" if valid else "AUTHORITATIVE_EVIDENCE_MISSING","expectancy_evidence",{"rows":len(ev),"valid":len(valid)},"timestamp-bounded attributable expectancy exists")
            cost=acc; keys=("spread_pct","expected_slippage_pct","latency_ms","funding_rate_pct"); absent=[x.get("decision_id") for x in cost if x.get("effective_rr") is None or int(x.get("execution_ctx_missing") or 0) or any(x.get(k) is None for k in keys)]; zero=[x.get("decision_id") for x in cost if all(float(x.get(k) or 0)==0 for k in keys)]; g16=self.miss("EXECUTION_COST_EVIDENCE","burnin_observations + order_decisions","measured non-placeholder costs for active run") if not cost else self.g("EXECUTION_COST_EVIDENCE",NOT_OBSERVABLE if absent else (NEEDS_FIX if zero else PASS),"EXECUTION_COST_CONTEXT_MISSING" if absent else ("PLACEHOLDER_ZERO_COSTS" if zero else "EXECUTION_COST_EVIDENCE_PRESENT"),"burnin_observations + order_decisions",{"missing":absent,"zero":zero},"measured non-placeholder costs for active run")
            g17=self.status(db,"RECOVERY_DRILL","burnin_recovery_drills","campaign_id=?",(self.cid,),"generated_at","recovery drill PASS")
            qualification_id=(c or {}).get("latest_qualification_id")
            q=db.one(
                "SELECT * FROM burnin_qualification_snapshots WHERE qualification_id=? AND campaign_id=? AND release_id=?",
                (qualification_id,self.cid,self.scope["release_id"]),
            ) if qualification_id and "burnin_qualification_snapshots" in tabs else None
            try: q_source_runs=json.loads((q or {}).get("source_run_ids_json") or "[]")
            except (TypeError,ValueError,json.JSONDecodeError): q_source_runs=[]
            q_scope_ok=bool(
                q
                and str(q.get("campaign_id") or "")==self.cid
                and str(q.get("release_id") or "")==str(self.scope["release_id"] or "")
                and str(self.scope["burnin_run_id"] or "") in {str(x) for x in q_source_runs}
                and bool(q.get("aggregate_evidence_hash"))
            )
            soak=bool(
                q_scope_ok
                and str((c or {}).get("qualification_status") or "").upper()=="CANARY_QUALIFIED"
                and str(q.get("status") or "").upper()=="CANARY_QUALIFIED"
                and str(q.get("sample_status") or "").upper()==PASS
                and str(q.get("evidence_completeness_status") or "").upper()==PASS
            )
            if not q:
                g18=self.miss("SOAK_EVIDENCE","burnin_campaigns.latest_qualification_id + burnin_qualification_snapshots","latest campaign-linked CANARY_QUALIFIED snapshot")
            else:
                reason="SOAK_QUALIFICATION_PASS" if soak else ("SOAK_QUALIFICATION_SCOPE_MISMATCH" if not q_scope_ok else "SOAK_QUALIFICATION_INCOMPLETE")
                g18=self.g("SOAK_EVIDENCE",PASS if soak else BLOCKED,reason,"burnin_campaigns.latest_qualification_id + burnin_qualification_snapshots",{"snapshot":q,"source_run_ids":q_source_runs,"campaign_qualification_status":(c or {}).get("qualification_status")},"latest campaign-linked CANARY_QUALIFIED snapshot with PASS sample/evidence")
            rb=self.latest(db.rows("SELECT * FROM rollback_verification_events WHERE release_id=?",(self.scope["release_id"],)),"verified_at") if "rollback_verification_events" in tabs else None
            rb_evidence=obj((rb or {}).get("evidence_json")); validation_id=rb_evidence.get("validation_id")
            v=db.one("SELECT * FROM live_rollback_validation_evidence WHERE validation_id=?",(validation_id,)) if validation_id and "live_rollback_validation_evidence" in tabs else None
            rb_ok=bool(rb and rollback_verification_evidence_valid(rb.get("status"),rb_evidence) and rollback_source_validation_valid(v,validation_id))
            g19=self.miss("ROLLBACK_EVIDENCE","rollback evidence tables","linked deterministic zero-mutation PASS rollback") if not rb else self.g("ROLLBACK_EVIDENCE",PASS if rb_ok else BLOCKED,"ROLLBACK_EVIDENCE_PASS" if rb_ok else "ROLLBACK_EVIDENCE_INVALID","rollback evidence tables",{"validation":v,"verification":rb,"verification_evidence":rb_evidence},"linked deterministic zero-mutation PASS rollback")
            run=self.latest(db.rows("SELECT * FROM runbook_evidence WHERE release_id=?",(self.scope["release_id"],)),"recorded_at") if "runbook_evidence" in tabs else None
            run_evidence=obj((run or {}).get("evidence_json")); run_ok=bool(run and runbook_verification_evidence_valid(run.get("status"),run_evidence))
            g20=self.miss("RUNBOOK_EVIDENCE","runbook_evidence","contract-verified runbook PASS") if not run else self.g("RUNBOOK_EVIDENCE",PASS if run_ok else BLOCKED,"RUNBOOK_EVIDENCE_PASS" if run_ok else "RUNBOOK_EVIDENCE_INVALID","runbook_evidence",{"row":run,"evidence":run_evidence},"contract-verified runbook PASS")
        gs=[g1,g2,g3,g4,g5,g6,g7,g8,g9,g10,g11,g12,g13,g14,g15,g16,g17,g18,g19,g20]; blockers=[x.data() for x in gs if x.status in {BLOCKED,NEEDS_FIX}]; gaps=[x.data() for x in gs if x.status==NOT_OBSERVABLE]
        return {"overall_status":BLOCKED if blockers else "READINESS_INCOMPLETE","generated_at":ts(self.now),"git_commit":self.gm(self.repo).get("commit"),"campaign_id":self.cid,"burnin_run_id":self.scope["burnin_run_id"],"release_id":self.scope["release_id"],"gates":[x.data() for x in gs],"blockers":blockers,"not_observable":gaps,"next_required_evidence":[f"{x.gate_id}:{x.reason}" for x in gs if x.status!=PASS],"authorization":"OBSERVATIONAL_ONLY_NOT_A_LIVE_AUTHORIZATION"}
def summary(r): return "LIVE readiness observer v1: "+r["overall_status"]+"\n"+"\n".join(f"- {x['gate_id']}: {x['status']} ({x['reason']})" for x in r["gates"] if x["status"]!=PASS)+"\nObservational only; not a LIVE authorization mechanism or execution controller.\n"
def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("--db",required=True); p.add_argument("--campaign-id",required=True); p.add_argument("--json",action="store_true"); p.add_argument("--output-dir"); a=p.parse_args(argv); r=LiveReadinessAgent(a.db,a.campaign_id).evaluate(); raw=json.dumps(r,indent=2,sort_keys=True,default=str); text=summary(r)
    if a.output_dir:
        out=Path(a.output_dir).resolve(); out.mkdir(parents=True,exist_ok=True); stem="live_readiness_"+a.campaign_id; (out/(stem+".json")).write_text(raw+"\n"); (out/(stem+".txt")).write_text(text)
    print(raw if a.json else text,end="" if a.json else ""); return 0
