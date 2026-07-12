from __future__ import annotations

import json, math, uuid
from dataclasses import asdict, dataclass
from typing import Any
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from alphaforge.burnin import SCHEMA_VERSION, bootstrap_burnin_schema, canonical_hash, confidence_interval, utc_now, update_burnin_run_counters
from alphaforge.release_gates import latest_valid_operator_ack, release_gate_status, latest_release_snapshot
from alphaforge.runtime_state import latest_runtime_state_snapshot
from alphaforge.live_readiness import LiveReadinessEvaluator

VERDICTS={"BURN_IN_INSUFFICIENT","BURN_IN_FAILED","CANARY_QUALIFIED","CANARY_SUSPENDED"}
REQUIRED_TABLES={"burnin_runs","burnin_trade_outcomes","burnin_reject_outcomes","burnin_regime_metrics","burnin_execution_metrics","burnin_calibration_metrics","burnin_drawdown_events","burnin_qualification_snapshots","burnin_suspension_events"}

@dataclass(slots=True)
class BurnInThresholds:
    minimum_duration_seconds: float = 7*24*3600
    minimum_total_decisions: int = 500
    minimum_accepted_trades: int = 50
    minimum_closed_trades: int = 30
    minimum_rejected_forward_outcomes: int = 50
    minimum_regime_sample: int = 20
    minimum_regime_coverage: int = 3
    minimum_calibration_sample: int = 50
    min_lower_confidence_bound_expectancy: float = 0.01
    max_drawdown_pct: float = 0.08
    max_cost_drag_per_trade: float = 0.20
    max_slippage_degradation_ratio: float = 1.5
    max_spread_degradation_ratio: float = 1.5
    max_latency_degradation_ratio: float = 1.5
    max_fill_degradation_ratio: float = 0.25
    max_reject_false_rate: float = 0.35
    max_calibration_error: float = 0.12
    max_symbol_concentration: float = 0.35
    max_trade_contribution: float = 0.30
    max_regime_concentration: float = 0.55
    max_stale_data_rate: float = 0.05
    max_runtime_error_count: int = 0
    require_operator_ack: bool = True
    require_phase1_6_gates: bool = True

@dataclass(slots=True)
class BurnInQualificationSnapshot:
    qualification_id: str
    burnin_run_id: str
    release_id: str
    generated_at: str
    status: str
    sample_status: str
    expectancy_status: str
    execution_status: str
    regime_status: str
    reject_quality_status: str
    calibration_status: str
    drawdown_status: str
    concentration_status: str
    reconciliation_status: str
    evidence_completeness_status: str
    blockers: list[str]
    warnings: list[str]
    thresholds: dict[str, Any]
    metrics: dict[str, Any]
    evidence_hash: str

class BurnInQualificationEngine:
    def __init__(self, engine: Engine, thresholds: BurnInThresholds | None = None) -> None:
        self.engine=engine; self.thresholds=thresholds or BurnInThresholds()
    def bootstrap(self) -> None:
        with self.engine.begin() as conn: bootstrap_burnin_schema(conn)
    def _has_schema(self)->bool:
        try:
            names=set(inspect(self.engine).get_table_names())
            return REQUIRED_TABLES.issubset(names)
        except SQLAlchemyError:
            return False
    def evaluate(self, burnin_run_id: str) -> BurnInQualificationSnapshot:
        th=asdict(self.thresholds)
        if not self._has_schema():
            return self._snapshot(burnin_run_id,"UNKNOWN","BURN_IN_INSUFFICIENT",["BURNIN_SCHEMA_OR_EVIDENCE_MISSING"],[],th,{})
        blockers: list[str]=[]; warnings: list[str]=[]; metrics: dict[str, Any]={}
        with self.engine.begin() as conn:
            run=conn.execute(text("SELECT * FROM burnin_runs WHERE burnin_run_id=:id"),{"id":burnin_run_id}).mappings().first()
            if not run:
                snap=self._snapshot(burnin_run_id,"UNKNOWN","BURN_IN_INSUFFICIENT",["BURNIN_SCHEMA_OR_EVIDENCE_MISSING","NO_BURNIN_RUN"],[],th,{})
                self.persist_snapshot(conn,snap); return snap
            release_id=str(run["release_id"]); phase=str(run.get("phase") or "PHASE7")
            mode=str(run["execution_mode"]).upper()
            if mode not in {"PAPER","LIVE_PRECHECK"}: blockers.append(f"INVALID_EXECUTION_MODE:{mode}")
            for k in ("git_commit","config_hash","strategy_config_hash","universe_hash","source_provenance_json"):
                if not run.get(k): blockers.append(f"MISSING_PROVENANCE:{k}")
            trades=conn.execute(text("SELECT * FROM burnin_trade_outcomes WHERE burnin_run_id=:id"),{"id":burnin_run_id}).mappings().all()
            rejects=conn.execute(text("SELECT * FROM burnin_reject_outcomes WHERE burnin_run_id=:id"),{"id":burnin_run_id}).mappings().all()
            regimes=conn.execute(text("SELECT * FROM burnin_regime_metrics WHERE burnin_run_id=:id"),{"id":burnin_run_id}).mappings().all()
            cal=conn.execute(text("SELECT * FROM burnin_calibration_metrics WHERE burnin_run_id=:id"),{"id":burnin_run_id}).mappings().all()
            execm=conn.execute(text("SELECT * FROM burnin_execution_metrics WHERE burnin_run_id=:id ORDER BY id DESC LIMIT 1"),{"id":burnin_run_id}).mappings().first()
            dds=conn.execute(text("SELECT * FROM burnin_drawdown_events WHERE burnin_run_id=:id"),{"id":burnin_run_id}).mappings().all()
            derived = update_burnin_run_counters(conn, burnin_run_id)
            obs_counts = conn.execute(text("SELECT SUM(CASE WHEN UPPER(COALESCE(decision,''))='ACCEPTED' THEN 1 ELSE 0 END) AS accepted, SUM(CASE WHEN UPPER(COALESCE(decision,''))='REJECTED' THEN 1 ELSE 0 END) AS rejected, COUNT(*) AS samples FROM burnin_observations WHERE burnin_run_id=:id"), {"id": burnin_run_id}).mappings().first() or {}
            samples=int(obs_counts.get("samples") or 0); accepted=int(obs_counts.get("accepted") or 0); rejected_count=int(obs_counts.get("rejected") or 0); closed=len(trades)
            valid_labels={"TP_BEFORE_SL","SL_BEFORE_TP","TIMEOUT","AMBIGUOUS"}
            completed_rejects=[r for r in rejects if int(r.get("evidence_complete") or 0)==1 and str(r.get("forward_label") or "").upper() in valid_labels and r.get("hypothetical_net_r_after_costs") is not None]
            ambiguous_rejects=[r for r in completed_rejects if str(r.get("forward_label") or "").upper()=="AMBIGUOUS"]
            incomplete_rejects=[r for r in rejects if r not in completed_rejects]
            pending_rejects=max(0, rejected_count-len(rejects))
            rejected_fwd=len(completed_rejects)
            persisted_mismatch = any(int(run.get(k) or 0) != int(v or 0) for k,v in {"sample_count":samples,"accepted_count":accepted,"rejected_count":rejected_count,"closed_trade_count":closed}.items())
            if persisted_mismatch:
                blockers.append("BURNIN_COUNTER_RECONCILIATION_FAILED")
            sample_status="PASS"
            for name,obs,limit in [("MINIMUM_DURATION",float(run.get("observed_duration_seconds") or 0),self.thresholds.minimum_duration_seconds),("MINIMUM_TOTAL_DECISIONS",samples,self.thresholds.minimum_total_decisions),("MINIMUM_ACCEPTED_TRADES",accepted,self.thresholds.minimum_accepted_trades),("MINIMUM_CLOSED_TRADES",closed,self.thresholds.minimum_closed_trades),("MINIMUM_REJECTED_FORWARD_OUTCOMES",rejected_fwd,self.thresholds.minimum_rejected_forward_outcomes)]:
                if obs < limit: sample_status="INSUFFICIENT"; blockers.append(f"{name}:{obs}<{limit}")
            metrics.update(sample_count=samples,accepted_count=accepted,rejected_count=rejected_count,closed_trade_count=closed,open_trade_count=max(0,accepted-closed),completed_rejected_forward_outcomes=len(completed_rejects),pending_rejected_forward_outcomes=pending_rejects,ambiguous_rejected_forward_outcomes=len(ambiguous_rejects),incomplete_rejected_forward_outcomes=len(incomplete_rejects),rejected_forward_outcomes=rejected_fwd,observed_duration_seconds=derived.get("observed_duration_seconds") or run.get("observed_duration_seconds"))
            self._compute_expectancy(trades, blockers, metrics)
            expectancy_status="PASS" if (metrics.get("lower_confidence_bound_expectancy") is not None and metrics["lower_confidence_bound_expectancy"]>=self.thresholds.min_lower_confidence_bound_expectancy and not any(b.startswith("INCOMPLETE_COST") or b=="COST_DRAG_EXCESSIVE_OR_MISSING" for b in blockers)) else "FAIL"
            regime_status=self._check_regimes(regimes, blockers, metrics)
            reject_status=self._compute_reject_quality(completed_rejects, trades, blockers, metrics)
            cal_status=self._compute_calibration(cal, blockers, metrics)
            dd_status=self._compute_drawdown(dds, blockers, metrics)
            exec_status=self._compute_execution(execm, blockers, metrics)
            conc_status=self._compute_concentration(trades, blockers, metrics)
            rec_status=self._check_reconciliation(blockers, metrics)
            self._check_phase_gates(release_id, blockers, metrics)
            evidence_status="PASS" if not any(b in {"BURNIN_SCHEMA_OR_EVIDENCE_MISSING"} or b.startswith("MISSING_PROVENANCE") or b.startswith("INCOMPLETE_COST") for b in blockers) else "FAIL"
            missing_markers=("MISSING","INSUFFICIENT","NO_","BURNIN_SCHEMA")
            status="CANARY_QUALIFIED" if not blockers else ("BURN_IN_INSUFFICIENT" if any(any(m in b for m in missing_markers) for b in blockers) or sample_status=="INSUFFICIENT" else "BURN_IN_FAILED")
            snap=self._snapshot(burnin_run_id,release_id,status,blockers,warnings,th,metrics,sample_status,expectancy_status,exec_status,regime_status,reject_status,cal_status,dd_status,conc_status,rec_status,evidence_status)
            self.persist_snapshot(conn,snap)
            suspension=self.suspension_reasons(snap)
            if snap.status == "CANARY_QUALIFIED" and suspension:
                snap.status="CANARY_SUSPENDED"; snap.blockers.extend(suspension); self.persist_snapshot(conn,snap)
            if suspension:
                self.persist_suspension(conn,snap,suspension)
            return snap
    def _compute_expectancy(self,trades,blockers,metrics):
        complete=[r for r in trades if int(r.get("evidence_complete") or 0)==1 and r.get("net_r") is not None]
        incomplete=len(trades)-len(complete)
        if incomplete: blockers.append(f"INCOMPLETE_COST_EVIDENCE:{incomplete}")
        netrs=sorted(float(r["net_r"]) for r in complete); mean,lcb,ucb=confidence_interval(netrs)
        wins=[v for v in netrs if v>0]; losses=[v for v in netrs if v<0]
        pos=sum(wins); neg=abs(sum(losses)); median=(None if not netrs else (netrs[len(netrs)//2] if len(netrs)%2 else (netrs[len(netrs)//2-1]+netrs[len(netrs)//2])/2))
        total_cost=sum(float(r.get("total_execution_cost") or 0) for r in complete); cost_drag=(total_cost/len(complete)) if complete else None
        avg_win=(pos/len(wins)) if wins else None; avg_loss=(neg/len(losses)) if losses else None
        payoff=(avg_win/avg_loss) if avg_win is not None and avg_loss else None
        hit=(len(wins)/len(netrs)) if netrs else None; breakeven=(1/(1+payoff)) if payoff else None
        spread=sum(float(r.get("spread_cost") or 0) for r in complete); slip=sum(float(r.get("entry_slippage_cost") or 0)+float(r.get("exit_slippage_cost") or 0) for r in complete); funding=sum(float(r.get("funding_cost") or 0) for r in complete); latency=sum(float(r.get("latency_cost") or 0) for r in complete)
        metrics.update(mean_net_r=mean,median_net_r=median,net_expectancy=mean,profit_factor_after_costs=(pos/neg if neg else (None if not pos else math.inf)),payoff_ratio_after_costs=payoff,hit_rate=hit,break_even_hit_rate=breakeven,lower_confidence_bound_expectancy=lcb,expectancy_confidence_interval=[lcb,ucb],total_cost_drag=total_cost,cost_drag_per_trade=cost_drag,slippage_damage_ratio=(slip/total_cost if total_cost else None),funding_damage_ratio=(funding/total_cost if total_cost else None),latency_damage_ratio=(latency/total_cost if total_cost else None),spread_cost_total=spread)
        if lcb is None or lcb < self.thresholds.min_lower_confidence_bound_expectancy: blockers.append("LOWER_CONFIDENCE_BOUND_EXPECTANCY_NOT_POSITIVE")
        if cost_drag is None or cost_drag > self.thresholds.max_cost_drag_per_trade: blockers.append("COST_DRAG_EXCESSIVE_OR_MISSING")
    def _check_regimes(self,regimes,blockers,metrics):
        material=0; status="PASS"; by={}
        for r in regimes:
            reg=str(r.get("regime") or "UNKNOWN").upper(); sc=int(r.get("sample_count") or 0); by[reg]=dict(r)
            if reg=="UNKNOWN" and str(r.get("status") or "").upper()=="PASS": blockers.append("UNKNOWN_REGIME_CANNOT_PASS"); status="FAIL"
            if sc>=self.thresholds.minimum_regime_sample: material+=1
            if sc>=self.thresholds.minimum_regime_sample and r.get("lower_confidence_bound_expectancy") is not None and float(r["lower_confidence_bound_expectancy"])<0: blockers.append(f"NEGATIVE_MATERIAL_REGIME:{reg}"); status="FAIL"
        metrics["regime_metrics"]=by
        if material<self.thresholds.minimum_regime_coverage: blockers.append("INSUFFICIENT_REGIME_COVERAGE"); return "INSUFFICIENT"
        return status
    def _compute_reject_quality(self,rejects,trades,blockers,metrics):
        false=sum(1 for r in rejects if float(r.get("missed_profit") or 0)>0); avoided=sum(1 for r in rejects if float(r.get("avoided_loss") or 0)>0); invalid=sum(1 for r in rejects if int(r.get("execution_invalidated") or 0)==1)
        net=sum(float(r.get("avoided_loss") or 0)-float(r.get("missed_profit") or 0) for r in rejects); by_reason={}; by_regime={}
        for r in rejects:
            val=float(r.get("avoided_loss") or 0)-float(r.get("missed_profit") or 0); by_reason[str(r.get("reject_reason") or "UNKNOWN")]=by_reason.get(str(r.get("reject_reason") or "UNKNOWN"),0)+val; by_regime[str(r.get("regime") or "UNKNOWN")]=by_regime.get(str(r.get("regime") or "UNKNOWN"),0)+val
        harmful=sum(1 for t in trades if float(t.get("net_r") or 0)<0); false_rate=(false/len(rejects)) if rejects else None
        metrics.update(reject_precision=(avoided/len(rejects) if rejects else None),avoided_loss_rate=(avoided/len(rejects) if rejects else None),false_reject_rate=false_rate,harmful_accept_rate=(harmful/len(trades) if trades else None),net_reject_value=net,reject_value_by_reason=by_reason,reject_value_by_regime=by_regime,execution_quality_reject_value=invalid)
        if not rejects or net<0 or false_rate is None or false_rate>self.thresholds.max_reject_false_rate: blockers.append("REJECT_QUALITY_INSUFFICIENT"); return "FAIL"
        return "PASS"
    def _compute_calibration(self,cal,blockers,metrics):
        samples=sum(int(c.get("sample_count") or 0) for c in cal); worst=max([float(c.get("calibration_error") if c.get("calibration_error") is not None else 999) for c in cal], default=999)
        brier=[float(c.get("brier_score")) for c in cal if c.get("brier_score") is not None]; logloss=[float(c.get("log_loss")) for c in cal if c.get("log_loss") is not None]; ece=[float(c.get("expected_calibration_error")) for c in cal if c.get("expected_calibration_error") is not None]
        buckets={}; observed={}
        for c in cal:
            try: buckets.update(json.loads(c.get("reliability_buckets_json") or "{}"))
            except Exception: pass
            try: observed.update(json.loads(c.get("observed_vs_predicted_json") or "{}"))
            except Exception: pass
        metrics.update(calibration_sample_count=samples,calibration_error=worst,brier_score=(sum(brier)/len(brier) if brier else None),log_loss=(sum(logloss)/len(logloss) if logloss else None),expected_calibration_error=(max(ece) if ece else None),reliability_buckets=buckets,observed_vs_predicted=observed)
        if samples<self.thresholds.minimum_calibration_sample: blockers.append("CALIBRATION_SAMPLE_INSUFFICIENT"); return "INSUFFICIENT"
        if worst>self.thresholds.max_calibration_error: blockers.append("CALIBRATION_QUALITY_INSUFFICIENT"); return "FAIL"
        return "PASS"
    def _compute_drawdown(self,dds,blockers,metrics):
        maxdd=max([float(d.get("drawdown_pct") or 0) for d in dds], default=0.0); unresolved=sum(1 for d in dds if not int(d.get("resolved") or 0)); rolling=min([float(d.get("rolling_expectancy")) for d in dds if d.get("rolling_expectancy") is not None], default=None)
        metrics.update(max_drawdown_pct=maxdd,unresolved_drawdown_events=unresolved,rolling_expectancy=rolling,loss_cluster_state="UNRESOLVED" if unresolved else "RESOLVED",recovery_status="INSUFFICIENT" if unresolved else "RECOVERED")
        if maxdd>self.thresholds.max_drawdown_pct or unresolved: blockers.append("DRAWDOWN_OR_LOSS_CLUSTER_BLOCKER"); return "FAIL"
        return "PASS"
    def _compute_execution(self,execm,blockers,metrics):
        if not execm: blockers.append("EXECUTION_INSUFFICIENT_EVIDENCE"); return "INSUFFICIENT_EVIDENCE"
        def ratio(cur,base): return None if cur is None or base in (None,0) else float(cur)/float(base)
        sr=ratio(execm.get("spread_current"),execm.get("spread_baseline")); slr=ratio(execm.get("slippage_current"),execm.get("slippage_baseline")); lr=ratio(execm.get("latency_current"),execm.get("latency_baseline")); fill_delta=None if execm.get("fill_probability_current") is None or execm.get("fill_probability_baseline") is None else float(execm.get("fill_probability_baseline"))-float(execm.get("fill_probability_current"))
        stale=float(execm.get("stale_data_count") or 0)/max(1,int(execm.get("execution_rejects") or 0)+1)
        metrics.update(spread_degradation_ratio=sr,slippage_degradation_ratio=slr,latency_degradation_ratio=lr,fill_probability_change=fill_delta,timeout_rate=execm.get("timeout_rate"),stale_data_rate=stale,execution_status=execm.get("status"))
        reasons=[]
        if str(execm.get("status") or "").upper() in {"DEGRADED","SEVERELY_DEGRADED","INSUFFICIENT_EVIDENCE"}: reasons.append(f"EXECUTION_{str(execm.get('status')).upper()}")
        if sr and sr>self.thresholds.max_spread_degradation_ratio: reasons.append("SPREAD_DEGRADATION")
        if slr and slr>self.thresholds.max_slippage_degradation_ratio: reasons.append("SLIPPAGE_SPIKE")
        if lr and lr>self.thresholds.max_latency_degradation_ratio: reasons.append("LATENCY_DEGRADATION")
        if fill_delta and fill_delta>self.thresholds.max_fill_degradation_ratio: reasons.append("FILL_DEGRADATION")
        if stale>self.thresholds.max_stale_data_rate: reasons.append("STALE_DATA_CLUSTER")
        blockers.extend(reasons)
        return "PASS" if not reasons else "FAIL"
    def _compute_concentration(self,trades,blockers,metrics):
        complete=[r for r in trades if r.get("net_r") is not None]
        total=sum(max(0.0,float(r.get("net_pnl") if r.get("net_pnl") is not None else r.get("net_r") or 0)) for r in complete)
        bysym={}; byreg={}; bycluster={}; top=0.0
        for r in complete:
            val=max(0.0,float(r.get("net_pnl") if r.get("net_pnl") is not None else r.get("net_r") or 0)); top=max(top,val); bysym[str(r.get("symbol") or "UNKNOWN")]=bysym.get(str(r.get("symbol") or "UNKNOWN"),0)+val; byreg[str(r.get("regime") or "UNKNOWN")]=byreg.get(str(r.get("regime") or "UNKNOWN"),0)+val
            try: cluster=json.loads(r.get("payload_json") or "{}").get("correlation_cluster","UNKNOWN")
            except Exception: cluster="UNKNOWN"
            bycluster[cluster]=bycluster.get(cluster,0)+val
        sym=(max(bysym.values())/total) if total else 1.0; trade=(top/total) if total else 1.0; reg=(max(byreg.values())/total) if total else 1.0; cluster=(max(bycluster.values())/total) if total else 1.0
        metrics.update(symbol_contribution=bysym,regime_contribution=byreg,correlated_cluster_contribution=bycluster,symbol_concentration=sym,top_trade_contribution=trade,regime_concentration=reg,correlated_cluster_concentration=cluster)
        reasons=[]
        if sym>self.thresholds.max_symbol_concentration: reasons.append("SYMBOL_CONCENTRATION_BREACH")
        if trade>self.thresholds.max_trade_contribution: reasons.append("TRADE_CONCENTRATION_BREACH")
        if reg>self.thresholds.max_regime_concentration: reasons.append("REGIME_CONCENTRATION_BREACH")
        blockers.extend(reasons)
        return "PASS" if not reasons else "FAIL"
    def _check_reconciliation(self,blockers,metrics):
        try: snap=latest_runtime_state_snapshot(self.engine) or {}
        except Exception: snap={}
        status=str(snap.get("reconciliation_status") or "").upper(); metrics["runtime_reconciliation_status"]=status or None
        if not snap or not status or status=="UNKNOWN": blockers.append("RECONCILIATION_EVIDENCE_MISSING"); return "NO_EVIDENCE"
        if status not in {"CLEAN","NOT_REQUIRED_BACKTEST"}: blockers.append("RECONCILIATION_NOT_CLEAN"); return "FAIL"
        return "PASS"
    def _check_phase_gates(self,release_id,blockers,metrics):
        if not (self.thresholds.require_operator_ack or self.thresholds.require_phase1_6_gates): return
        phase="PHASE6"
        if self.thresholds.require_operator_ack and latest_valid_operator_ack(self.engine, release_id=release_id, phase=phase) is None: blockers.append("OPERATOR_ACK_MISSING_OR_EXPIRED")
        gate=release_gate_status(self.engine, release_id=release_id, phase=phase); metrics["phase1_6_release_gate"]=gate
        persisted = latest_release_snapshot(self.engine, release_id=release_id, phase=phase)
        evidence=gate.get("evidence") or {}
        if persisted is not None:
            evidence = {**evidence, **(persisted.evidence or {})}
            metrics["phase1_6_persisted_release_gate"] = asdict(persisted)
        mutation=gate.get("mutation_attempt_count")
        if self.thresholds.require_phase1_6_gates:
            if not gate.get("passed"): blockers.append("PHASE1_6_GATES_NOT_PASSING"); blockers.append("RELEASE_GATE_NOT_READY")
            if mutation is None: blockers.append("CANARY_EVIDENCE_MISSING")
            elif int(mutation)>0: blockers.append("MUTATION_ATTEMPT_DETECTED")
            if not gate.get("rollback_verified"): blockers.append("ROLLBACK_NOT_VERIFIED")
            if not gate.get("runbook_verified"): blockers.append("RUNBOOK_NOT_VERIFIED")
            if not bool((evidence.get("full_tests") or evidence.get("tests_passing_evidence") or {}).get("status") == "PASS" or gate.get("full_tests_passed", False)): blockers.append("FULL_TEST_EVIDENCE_MISSING")
    def suspension_reasons(self,snap: BurnInQualificationSnapshot)->list[str]:
        m=snap.metrics; b=set(snap.blockers); reasons=[]
        mapping={"SPREAD_DEGRADATION":"SPREAD_DEGRADATION","SLIPPAGE_SPIKE":"SLIPPAGE_SPIKE","LATENCY_DEGRADATION":"LATENCY_DEGRADATION","FILL_DEGRADATION":"FILL_DEGRADATION","REJECT_QUALITY_INSUFFICIENT":"REJECT_QUALITY_COLLAPSE","CALIBRATION_QUALITY_INSUFFICIENT":"CALIBRATION_DRIFT","RECONCILIATION_NOT_CLEAN":"RECONCILIATION_FAILURE","STALE_DATA_CLUSTER":"STALE_DATA_CLUSTER","MUTATION_ATTEMPT_DETECTED":"MUTATION_ATTEMPT","OPERATOR_ACK_MISSING_OR_EXPIRED":"OPERATOR_ACK_EXPIRY","ROLLBACK_NOT_VERIFIED":"ROLLBACK_INVALIDATION","RUNBOOK_NOT_VERIFIED":"RUNBOOK_INVALIDATION","SYMBOL_CONCENTRATION_BREACH":"SYMBOL_CONCENTRATION_BREACH","TRADE_CONCENTRATION_BREACH":"TRADE_CONCENTRATION_BREACH","REGIME_CONCENTRATION_BREACH":"REGIME_CONCENTRATION_BREACH"}
        if m.get("lower_confidence_bound_expectancy") is not None and m["lower_confidence_bound_expectancy"] < self.thresholds.min_lower_confidence_bound_expectancy: reasons.append("ROLLING_EXPECTANCY_BREACH")
        if float(m.get("max_drawdown_pct") or 0)>self.thresholds.max_drawdown_pct: reasons.append("DRAWDOWN_BREACH")
        if int(m.get("runtime_error_count") or 0)>self.thresholds.max_runtime_error_count: reasons.append("RUNTIME_ERROR_CLUSTER")
        if "PERSISTENCE_FAILURE" in b: reasons.append("PERSISTENCE_FAILURE")
        for src,dst in mapping.items():
            if src in b: reasons.append(dst)
        return sorted(set(reasons))
    def _snapshot(self,bid,release,status,blockers,warnings,thresholds,metrics,sample="INSUFFICIENT",exp="FAIL",exe="INSUFFICIENT_EVIDENCE",reg="INSUFFICIENT",rej="FAIL",cal="INSUFFICIENT",dd="FAIL",conc="FAIL",rec="UNKNOWN",evid="FAIL"):
        payload={"burnin_run_id":bid,"release_id":release,"status":status,"blockers":blockers,"warnings":warnings,"thresholds":thresholds,"metrics":metrics}
        return BurnInQualificationSnapshot(f"phase7q:{uuid.uuid4().hex}",bid,release,utc_now(),status,sample,exp,exe,reg,rej,cal,dd,conc,rec,evid,blockers,warnings,thresholds,metrics,canonical_hash(payload))
    def persist_snapshot(self,conn:Any,s:BurnInQualificationSnapshot)->None:
        conn.execute(text("""INSERT INTO burnin_qualification_snapshots(qualification_id,burnin_run_id,release_id,generated_at,status,sample_status,expectancy_status,execution_status,regime_status,reject_quality_status,calibration_status,drawdown_status,concentration_status,reconciliation_status,evidence_completeness_status,blockers_json,warnings_json,thresholds_json,metrics_json,evidence_hash,schema_version) VALUES (:qualification_id,:burnin_run_id,:release_id,:generated_at,:status,:sample_status,:expectancy_status,:execution_status,:regime_status,:reject_quality_status,:calibration_status,:drawdown_status,:concentration_status,:reconciliation_status,:evidence_completeness_status,:blockers_json,:warnings_json,:thresholds_json,:metrics_json,:evidence_hash,:schema_version)"""), {**asdict(s),"blockers_json":json.dumps(s.blockers,sort_keys=True),"warnings_json":json.dumps(s.warnings,sort_keys=True),"thresholds_json":json.dumps(s.thresholds,sort_keys=True),"metrics_json":json.dumps(s.metrics,sort_keys=True,default=str),"schema_version":SCHEMA_VERSION})
    def persist_suspension(self,conn:Any,s:BurnInQualificationSnapshot,reasons:list[str])->None:
        for reason in reasons:
            conn.execute(text("""INSERT INTO burnin_suspension_events(suspension_event_id,release_id,burnin_run_id,timestamp,reason_codes_json,observed_values_json,thresholds_json,evidence_payload_json,schema_version) VALUES (:id,:release,:bid,:ts,:reasons,:obs,:th,:payload,:schema)"""), {"id":f"phase7s:{uuid.uuid4().hex}","release":s.release_id,"bid":s.burnin_run_id,"ts":utc_now(),"reasons":json.dumps([reason],sort_keys=True),"obs":json.dumps(s.metrics,sort_keys=True,default=str),"th":json.dumps(s.thresholds,sort_keys=True),"payload":json.dumps({"qualification_id":s.qualification_id,"status":s.status},sort_keys=True),"schema":SCHEMA_VERSION})
