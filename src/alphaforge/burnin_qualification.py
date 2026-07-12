from __future__ import annotations

import json, math, uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping
from sqlalchemy import text
from sqlalchemy.engine import Engine

from alphaforge.burnin import SCHEMA_VERSION, bootstrap_burnin_schema, canonical_hash, confidence_interval, utc_now

VERDICTS={"BURN_IN_INSUFFICIENT","BURN_IN_FAILED","CANARY_QUALIFIED","CANARY_SUSPENDED"}

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
    max_reject_false_rate: float = 0.35
    max_calibration_error: float = 0.12
    max_symbol_concentration: float = 0.35
    max_trade_contribution: float = 0.30
    max_regime_concentration: float = 0.55
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
    def evaluate(self, burnin_run_id: str) -> BurnInQualificationSnapshot:
        th=asdict(self.thresholds); blockers=[]; warnings=[]; metrics={}
        with self.engine.begin() as conn:
            bootstrap_burnin_schema(conn)
            run=conn.execute(text("SELECT * FROM burnin_runs WHERE burnin_run_id=:id"),{"id":burnin_run_id}).mappings().first()
            if not run:
                return self._snapshot(burnin_run_id,"UNKNOWN","BURN_IN_INSUFFICIENT",["NO_BURNIN_RUN"],[],th,{})
            mode=str(run["execution_mode"]).upper(); release_id=str(run["release_id"])
            if mode not in {"PAPER","LIVE_PRECHECK"}: blockers.append(f"INVALID_EXECUTION_MODE:{mode}")
            if mode == "LIVE": blockers.append("LIVE_EVIDENCE_FORBIDDEN_PHASE7")
            for k in ("git_commit","config_hash","strategy_config_hash","universe_hash","source_provenance_json"):
                if not run.get(k): blockers.append(f"MISSING_PROVENANCE:{k}")
            trades=conn.execute(text("SELECT * FROM burnin_trade_outcomes WHERE burnin_run_id=:id"),{"id":burnin_run_id}).mappings().all()
            rejects=conn.execute(text("SELECT * FROM burnin_reject_outcomes WHERE burnin_run_id=:id"),{"id":burnin_run_id}).mappings().all()
            regimes=conn.execute(text("SELECT * FROM burnin_regime_metrics WHERE burnin_run_id=:id"),{"id":burnin_run_id}).mappings().all()
            cal=conn.execute(text("SELECT * FROM burnin_calibration_metrics WHERE burnin_run_id=:id"),{"id":burnin_run_id}).mappings().all()
            execm=conn.execute(text("SELECT * FROM burnin_execution_metrics WHERE burnin_run_id=:id ORDER BY id DESC LIMIT 1"),{"id":burnin_run_id}).mappings().first()
            dds=conn.execute(text("SELECT * FROM burnin_drawdown_events WHERE burnin_run_id=:id"),{"id":burnin_run_id}).mappings().all()
            samples=int(run.get("sample_count") or 0); accepted=int(run.get("accepted_count") or 0); closed=int(run.get("closed_trade_count") or len(trades)); rejected_fwd=len(rejects)
            metrics.update(sample_count=samples,accepted_count=accepted,closed_trade_count=closed,rejected_forward_outcomes=rejected_fwd,observed_duration_seconds=run.get("observed_duration_seconds"))
            sample_status="PASS"
            for name,obs,limit in [("MINIMUM_DURATION",float(run.get("observed_duration_seconds") or 0),self.thresholds.minimum_duration_seconds),("MINIMUM_TOTAL_DECISIONS",samples,self.thresholds.minimum_total_decisions),("MINIMUM_ACCEPTED_TRADES",accepted,self.thresholds.minimum_accepted_trades),("MINIMUM_CLOSED_TRADES",closed,self.thresholds.minimum_closed_trades),("MINIMUM_REJECTED_FORWARD_OUTCOMES",rejected_fwd,self.thresholds.minimum_rejected_forward_outcomes)]:
                if obs < limit: sample_status="INSUFFICIENT"; blockers.append(f"{name}:{obs}<{limit}")
            complete=[r for r in trades if int(r.get("evidence_complete") or 0)==1 and r.get("net_r") is not None]
            incomplete=len(trades)-len(complete)
            if incomplete: blockers.append(f"INCOMPLETE_COST_EVIDENCE:{incomplete}")
            netrs=[float(r["net_r"]) for r in complete]; mean,lcb,ucb=confidence_interval(netrs)
            total_cost=sum(float(r.get("total_execution_cost") or 0) for r in complete); cost_drag=(total_cost/len(complete)) if complete else None
            metrics.update(mean_net_r=mean,lower_confidence_bound_expectancy=lcb,expectancy_confidence_interval=[lcb,ucb],total_cost_drag=total_cost,cost_drag_per_trade=cost_drag)
            expectancy_status="PASS" if lcb is not None and lcb >= self.thresholds.min_lower_confidence_bound_expectancy else "FAIL"
            if expectancy_status != "PASS": blockers.append("LOWER_CONFIDENCE_BOUND_EXPECTANCY_NOT_POSITIVE")
            if cost_drag is None or cost_drag > self.thresholds.max_cost_drag_per_trade: blockers.append("COST_DRAG_EXCESSIVE_OR_MISSING")
            regime_status="PASS"; material=0
            for r in regimes:
                reg=str(r.get("regime") or "UNKNOWN").upper(); sc=int(r.get("sample_count") or 0); status=str(r.get("status") or "").upper()
                if reg == "UNKNOWN" and status == "PASS": blockers.append("UNKNOWN_REGIME_CANNOT_PASS"); regime_status="FAIL"
                if sc >= self.thresholds.minimum_regime_sample: material += 1
                if sc >= self.thresholds.minimum_regime_sample and r.get("lower_confidence_bound_expectancy") is not None and float(r["lower_confidence_bound_expectancy"]) < 0:
                    blockers.append(f"NEGATIVE_MATERIAL_REGIME:{reg}"); regime_status="FAIL"
            if material < self.thresholds.minimum_regime_coverage: blockers.append("INSUFFICIENT_REGIME_COVERAGE"); regime_status="INSUFFICIENT"
            false_rejects=sum(1 for r in rejects if float(r.get("missed_profit") or 0)>0); reject_false_rate=(false_rejects/len(rejects)) if rejects else None
            net_reject_value=sum(float(r.get("avoided_loss") or 0)-float(r.get("missed_profit") or 0) for r in rejects)
            metrics.update(false_reject_rate=reject_false_rate,net_reject_value=net_reject_value)
            reject_status="PASS" if rejects and net_reject_value>=0 and (reject_false_rate or 0)<=self.thresholds.max_reject_false_rate else "FAIL"
            if reject_status != "PASS": blockers.append("REJECT_QUALITY_INSUFFICIENT")
            worst_cal=max([float(c.get("calibration_error") or 999) for c in cal], default=999); cal_samples=sum(int(c.get("sample_count") or 0) for c in cal)
            metrics.update(calibration_error=worst_cal,calibration_sample_count=cal_samples)
            cal_status="PASS" if cal_samples>=self.thresholds.minimum_calibration_sample and worst_cal<=self.thresholds.max_calibration_error else "INSUFFICIENT" if cal_samples<self.thresholds.minimum_calibration_sample else "FAIL"
            if cal_status != "PASS": blockers.append("CALIBRATION_QUALITY_INSUFFICIENT")
            max_dd=max([float(d.get("drawdown_pct") or 0) for d in dds], default=0.0); unresolved=sum(1 for d in dds if not int(d.get("resolved") or 0))
            metrics.update(max_drawdown_pct=max_dd,unresolved_drawdown_events=unresolved)
            dd_status="PASS" if max_dd<=self.thresholds.max_drawdown_pct and unresolved==0 else "FAIL"
            if dd_status != "PASS": blockers.append("DRAWDOWN_OR_LOSS_CLUSTER_BLOCKER")
            exec_status=str(execm.get("status") if execm else "INSUFFICIENT_EVIDENCE").upper()
            if exec_status in {"DEGRADED","SEVERELY_DEGRADED","INSUFFICIENT_EVIDENCE"}: blockers.append(f"EXECUTION_{exec_status}")
            by_symbol={}; by_regime={}; total_pos=sum(max(0.0,float(r.get("net_pnl") or r.get("net_r") or 0)) for r in complete)
            max_trade=max([max(0.0,float(r.get("net_pnl") or r.get("net_r") or 0)) for r in complete], default=0.0)
            for r in complete:
                val=max(0.0,float(r.get("net_pnl") or r.get("net_r") or 0)); by_symbol[str(r.get("symbol") or "UNKNOWN")]=by_symbol.get(str(r.get("symbol") or "UNKNOWN"),0)+val; by_regime[str(r.get("regime") or "UNKNOWN")]=by_regime.get(str(r.get("regime") or "UNKNOWN"),0)+val
            sym_conc=(max(by_symbol.values())/total_pos) if total_pos else 1.0; trade_conc=(max_trade/total_pos) if total_pos else 1.0; reg_conc=(max(by_regime.values())/total_pos) if total_pos else 1.0
            metrics.update(symbol_concentration=sym_conc,top_trade_contribution=trade_conc,regime_concentration=reg_conc)
            conc_status="PASS" if sym_conc<=self.thresholds.max_symbol_concentration and trade_conc<=self.thresholds.max_trade_contribution and reg_conc<=self.thresholds.max_regime_concentration else "FAIL"
            if conc_status != "PASS": blockers.append("CONCENTRATION_LIMIT_BREACH")
            rec_status="PASS" if "RECONCILIATION" not in " ".join(blockers) else "FAIL"
            evidence_status="PASS" if not any("MISSING_PROVENANCE" in b or "INCOMPLETE_COST" in b for b in blockers) else "FAIL"
            status="CANARY_QUALIFIED" if not blockers else ("BURN_IN_INSUFFICIENT" if any("MINIMUM" in b or "INSUFFICIENT" in b for b in blockers) else "BURN_IN_FAILED")
            snap=self._snapshot(burnin_run_id,release_id,status,blockers,warnings,th,metrics,sample_status,expectancy_status,exec_status,regime_status,reject_status,cal_status,dd_status,conc_status,rec_status,evidence_status)
            self.persist_snapshot(conn,snap)
            if status == "CANARY_QUALIFIED":
                susp=self.suspension_reasons(snap)
                if susp:
                    snap.status="CANARY_SUSPENDED"; snap.blockers.extend(susp); self.persist_snapshot(conn,snap); self.persist_suspension(conn,snap,susp)
            return snap
    def suspension_reasons(self,snap: BurnInQualificationSnapshot)->list[str]:
        m=snap.metrics; reasons=[]
        if (m.get("lower_confidence_bound_expectancy") is not None and m["lower_confidence_bound_expectancy"] < self.thresholds.min_lower_confidence_bound_expectancy): reasons.append("ROLLING_EXPECTANCY_BELOW_THRESHOLD")
        if float(m.get("max_drawdown_pct") or 0)>self.thresholds.max_drawdown_pct: reasons.append("DRAWDOWN_BREACH")
        return reasons
    def _snapshot(self,bid,release,status,blockers,warnings,thresholds,metrics,sample="INSUFFICIENT",exp="FAIL",exe="INSUFFICIENT_EVIDENCE",reg="INSUFFICIENT",rej="FAIL",cal="INSUFFICIENT",dd="FAIL",conc="FAIL",rec="UNKNOWN",evid="FAIL"):
        payload={"burnin_run_id":bid,"release_id":release,"status":status,"blockers":blockers,"warnings":warnings,"thresholds":thresholds,"metrics":metrics}
        return BurnInQualificationSnapshot(f"phase7q:{uuid.uuid4().hex}",bid,release,utc_now(),status,sample,exp,exe,reg,rej,cal,dd,conc,rec,evid,blockers,warnings,thresholds,metrics,canonical_hash(payload))
    def persist_snapshot(self,conn:Any,s:BurnInQualificationSnapshot)->None:
        conn.execute(text("""INSERT INTO burnin_qualification_snapshots(qualification_id,burnin_run_id,release_id,generated_at,status,sample_status,expectancy_status,execution_status,regime_status,reject_quality_status,calibration_status,drawdown_status,concentration_status,reconciliation_status,evidence_completeness_status,blockers_json,warnings_json,thresholds_json,metrics_json,evidence_hash,schema_version) VALUES (:qualification_id,:burnin_run_id,:release_id,:generated_at,:status,:sample_status,:expectancy_status,:execution_status,:regime_status,:reject_quality_status,:calibration_status,:drawdown_status,:concentration_status,:reconciliation_status,:evidence_completeness_status,:blockers_json,:warnings_json,:thresholds_json,:metrics_json,:evidence_hash,:schema_version)"""), {**asdict(s),"blockers_json":json.dumps(s.blockers,sort_keys=True),"warnings_json":json.dumps(s.warnings,sort_keys=True),"thresholds_json":json.dumps(s.thresholds,sort_keys=True),"metrics_json":json.dumps(s.metrics,sort_keys=True),"schema_version":SCHEMA_VERSION})
    def persist_suspension(self,conn:Any,s:BurnInQualificationSnapshot,reasons:list[str])->None:
        conn.execute(text("""INSERT INTO burnin_suspension_events(suspension_event_id,release_id,burnin_run_id,timestamp,reason_codes_json,observed_values_json,thresholds_json,evidence_payload_json,schema_version) VALUES (:id,:release,:bid,:ts,:reasons,:obs,:th,:payload,:schema)"""), {"id":f"phase7s:{uuid.uuid4().hex}","release":s.release_id,"bid":s.burnin_run_id,"ts":utc_now(),"reasons":json.dumps(reasons,sort_keys=True),"obs":json.dumps(s.metrics,sort_keys=True),"th":json.dumps(s.thresholds,sort_keys=True),"payload":json.dumps({"qualification_id":s.qualification_id,"status":s.status},sort_keys=True),"schema":SCHEMA_VERSION})
