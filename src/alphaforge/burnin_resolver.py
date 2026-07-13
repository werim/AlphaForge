from __future__ import annotations
import json, sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from alphaforge.burnin import canonical_hash, persist_burnin_reject_outcome, persist_burnin_trade_outcome, persist_burnin_observation, utc_now, CRITICAL_COST_FIELDS
from alphaforge.burnin_campaign import CAMPAIGN_SCHEMA_VERSION, bootstrap_campaign_schema, _exec

def _dt(v): return datetime.fromisoformat(str(v).replace('Z','+00:00'))
def _side(side): return str(side or 'LONG').upper()
def _hit(side, high, low, stop, target):
    if _side(side) == 'SHORT': return (high >= stop if stop is not None else False, low <= target if target is not None else False)
    return (low <= stop if stop is not None else False, high >= target if target is not None else False)

def persist_pending_reject_label(conn: Any, *, campaign_id: str, burnin_run_id: str, reject_decision_id: str, signal_id: str|None, symbol: str|None, side: str|None, decision_timestamp: str|None, entry: float|None, stop: float|None, target: float|None, horizon_seconds: float|None, execution_cost_assumptions: Mapping[str,Any]|None, regime: str|None, reject_reason: str|None, source_provenance: Mapping[str,Any]) -> str | None:
    bootstrap_campaign_schema(conn)
    critical = {"symbol": symbol, "side": side, "decision_timestamp": decision_timestamp, "entry": entry, "stop": stop, "target": target, "horizon_seconds": horizon_seconds, "execution_cost_assumptions": execution_cost_assumptions}
    missing = [k for k, v in critical.items() if v is None or v == "" or (k == "execution_cost_assumptions" and not isinstance(v, Mapping))]
    if missing:
        row = _exec(conn, "SELECT release_id FROM burnin_runs WHERE burnin_run_id=:bid", {"bid": burnin_run_id}).fetchone()
        release_id = row[0] if row else "UNKNOWN"
        persist_burnin_observation(conn, observation_id='incomplete_reject_geometry_'+canonical_hash({'reject_decision_id': reject_decision_id})[:20], burnin_run_id=burnin_run_id, release_id=release_id, execution_mode='PAPER', observed_at=decision_timestamp or utc_now(), symbol=symbol, regime=regime or 'UNKNOWN', decision='REJECTED', lifecycle_state='SIGNAL_REJECTED', metrics={'reject_decision_id': reject_decision_id, 'reject_reason': reject_reason, 'campaign_id': campaign_id}, source_provenance=source_provenance, missing_fields=missing)
        return None
    due_at=datetime.fromtimestamp(_dt(decision_timestamp).timestamp()+float(horizon_seconds), timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z')
    pid='prej_'+canonical_hash({'reject_decision_id':reject_decision_id})[:20]
    _exec(conn,"""INSERT OR IGNORE INTO burnin_pending_reject_labels(pending_label_id,campaign_id,burnin_run_id,reject_decision_id,signal_id,symbol,side,decision_timestamp,entry,stop,target,horizon_seconds,execution_cost_assumptions_json,regime,reject_reason,source_provenance_json,due_at,status,created_at,schema_version) VALUES (:pid,:cid,:bid,:rid,:sid,:sym,:side,:ts,:entry,:stop,:target,:hor,:costs,:reg,:reason,:prov,:due,'PENDING',:now,:sv)""",{"pid":pid,"cid":campaign_id,"bid":burnin_run_id,"rid":reject_decision_id,"sid":signal_id,"sym":symbol,"side":side,"ts":decision_timestamp,"entry":entry,"stop":stop,"target":target,"hor":horizon_seconds,"costs":json.dumps(dict(execution_cost_assumptions or {}),sort_keys=True),"reg":regime,"reason":reject_reason,"prov":json.dumps(dict(source_provenance),sort_keys=True),"due":due_at,"now":utc_now(),"sv":CAMPAIGN_SCHEMA_VERSION})
    return pid

def resolve_campaign_batch(conn: Any, campaign_id: str, candles_by_symbol: Mapping[str, Sequence[Mapping[str,Any]]] | Sequence[Mapping[str,Any]], *, now: str|None=None) -> dict[str,int]:
    bootstrap_campaign_schema(conn); now=now or utc_now(); rows=_exec(conn,"SELECT * FROM burnin_pending_reject_labels WHERE campaign_id=:cid AND status IN ('PENDING','READY') AND due_at <= :now ORDER BY decision_timestamp,id", {"cid": campaign_id, "now": now}).fetchall(); counts={"resolved":0,"pending":0,"ambiguous":0,"failed":0,"expired":0}
    for row in rows:
        r=dict(row) if isinstance(row, sqlite3.Row) else dict(row._mapping); candles = candles_by_symbol.get(r['symbol'], []) if isinstance(candles_by_symbol, Mapping) else candles_by_symbol
        usable=[c for c in candles if _dt(c.get('timestamp') or c.get('open_time') or c.get('time')) > _dt(r['decision_timestamp']) and _dt(c.get('timestamp') or c.get('open_time') or c.get('time')) <= _dt(r['due_at'])]
        if _dt(now) < _dt(r['due_at']) and not usable: counts['pending']+=1; continue
        expired = _dt(now) >= _dt(r['due_at']) and not usable
        label='EXPIRED' if expired else 'TIMEOUT'; ambiguous=False; gross=None if expired else 0.0
        for c in usable:
            sl,tp=_hit(r['side'], float(c['high']), float(c['low']), r['stop'], r['target'])
            if sl and tp: label='AMBIGUOUS'; ambiguous=True; gross=None; break
            if tp: label='TP_BEFORE_SL'; gross=abs((float(r['target'])-float(r['entry']))/(float(r['entry'])-float(r['stop']))) if r['entry']!=r['stop'] else None; break
            if sl: label='SL_BEFORE_TP'; gross=-1.0; break
        costs=json.loads(r.get('execution_cost_assumptions_json') or '{}'); missing=[f for f in CRITICAL_COST_FIELDS if costs.get(f) is None]
        total=None if missing or gross is None else sum(float(costs.get(f) or 0) for f in CRITICAL_COST_FIELDS)
        net=None if total is None or gross is None else gross-total
        persist_burnin_reject_outcome(conn,reject_outcome_id='rout_'+r['reject_decision_id'],burnin_run_id=r['burnin_run_id'],release_id=_release(conn,r['burnin_run_id']),reject_reason=r.get('reject_reason') or 'UNKNOWN',symbol=r['symbol'],regime=r.get('regime') or 'UNKNOWN',decision_time=r['decision_timestamp'],hypothetical_entry=r['entry'],hypothetical_stop=r['stop'],hypothetical_target=r['target'],forward_label=label,would_tp=label=='TP_BEFORE_SL',would_sl=label=='SL_BEFORE_TP',timeout=label=='TIMEOUT',ambiguous=ambiguous,hypothetical_gross_r=gross,hypothetical_net_r_after_costs=net,avoided_loss=max(0.0,-net) if net is not None else None,missed_profit=max(0.0,net) if net is not None else None,execution_invalidated=bool(missing),evidence_horizon=r['due_at'],payload={'pending_label_id':r['pending_label_id'],'missing_cost_fields':missing})
        status='EXPIRED' if expired else ('AMBIGUOUS' if ambiguous else ('RESOLVED' if not missing else 'FAILED'))
        _exec(conn,"UPDATE burnin_pending_reject_labels SET status=:s,evidence_complete=:ec,resolved_at=:ts,last_error=:err WHERE pending_label_id=:pid",{"s":status,"ec":1 if not missing else 0,"ts":utc_now(),"err":None if not missing else 'MISSING_COSTS',"pid":r['pending_label_id']})
        counts['expired' if expired else ('ambiguous' if ambiguous else ('resolved' if not missing else 'failed'))]+=1
    return counts

def _release(conn,bid):
    row=_exec(conn,"SELECT release_id FROM burnin_runs WHERE burnin_run_id=:bid",{"bid":bid}).fetchone(); return row[0] if row else 'UNKNOWN'

def persist_pending_position(conn: Any, **kw) -> str:
    bootstrap_campaign_schema(conn); pid='ppos_'+canonical_hash({'trade_id':kw['trade_id']})[:20]
    vals={**kw,'pid':pid,'prov':json.dumps(dict(kw.get('source_provenance') or {}),sort_keys=True),'now':utc_now(),'sv':CAMPAIGN_SCHEMA_VERSION}
    _exec(conn,"""INSERT OR IGNORE INTO burnin_pending_position_outcomes(pending_position_id,trade_id,campaign_id,burnin_run_id,signal_id,symbol,side,entry_time,planned_entry,simulated_fill,stop,target,quantity,notional,entry_spread,entry_slippage,entry_fee,regime,source_provenance_json,status,created_at,schema_version) VALUES (:pid,:trade_id,:campaign_id,:burnin_run_id,:signal_id,:symbol,:side,:entry_time,:planned_entry,:simulated_fill,:stop,:target,:quantity,:notional,:entry_spread,:entry_slippage,:entry_fee,:regime,:prov,'OPEN',:now,:sv)""", vals)
    return pid

def resolve_position_closure(conn: Any, *, trade_id: str, exit_time: str, exit_price: float, exit_reason: str, exit_costs: Mapping[str,Any], mfe: float|None=None, mae: float|None=None) -> dict[str,Any]:
    bootstrap_campaign_schema(conn); row=_exec(conn,"SELECT * FROM burnin_pending_position_outcomes WHERE trade_id=:t",{"t":trade_id}).fetchone()
    if not row: raise KeyError('position not found')
    r=dict(row) if isinstance(row, sqlite3.Row) else dict(row._mapping)
    if r.get('status') == 'CLOSED': return {'status':'IDEMPOTENT','trade_id':trade_id}
    fill=float(r.get('simulated_fill') or r.get('planned_entry')); qty=float(r.get('quantity') or 1); side=_side(r.get('side')); sign=-1 if side=='SHORT' else 1
    gross_pnl=(float(exit_price)-fill)*qty*sign; risk=abs(fill-float(r.get('stop') or fill)) or 1.0; gross_r=(float(exit_price)-fill)*sign/risk
    missing=[k for k in ('exit_spread','exit_slippage','exit_fee','funding','latency_impact_penalty') if exit_costs.get(k) is None]
    total=None if missing else sum(float(exit_costs.get(k) or 0) for k in ('exit_spread','exit_slippage','exit_fee','funding','latency_impact_penalty')) + float(r.get('entry_spread') or 0)+float(r.get('entry_slippage') or 0)+float(r.get('entry_fee') or 0)
    net_pnl=None if total is None else gross_pnl-total; net_r=None if total is None else gross_r-total
    hold=(_dt(exit_time)-_dt(r['entry_time'])).total_seconds()
    _exec(conn,"""UPDATE burnin_pending_position_outcomes SET status='CLOSED',exit_time=:xt,exit_price=:xp,exit_reason=:xr,gross_pnl=:gp,gross_r=:gr,exit_spread=:es,exit_slippage=:esl,exit_fee=:ef,funding=:fu,latency_impact_penalty=:li,total_execution_cost=:tc,net_pnl=:np,net_r=:nr,hold_duration_seconds=:hold,mfe=:mfe,mae=:mae,evidence_complete=:ec,missing_fields_json=:mf,resolved_at=:now WHERE trade_id=:tid""",{"tid":trade_id,"xt":exit_time,"xp":exit_price,"xr":exit_reason,"gp":gross_pnl,"gr":gross_r,"es":exit_costs.get('exit_spread'),"esl":exit_costs.get('exit_slippage'),"ef":exit_costs.get('exit_fee'),"fu":exit_costs.get('funding'),"li":exit_costs.get('latency_impact_penalty'),"tc":total,"np":net_pnl,"nr":net_r,"hold":hold,"mfe":mfe,"mae":mae,"ec":0 if missing else 1,"mf":json.dumps(missing),"now":utc_now()})
    persist_burnin_trade_outcome(conn,outcome_id='tout_'+trade_id,burnin_run_id=r['burnin_run_id'],release_id=_release(conn,r['burnin_run_id']),trade_id=trade_id,symbol=r['symbol'],regime=r.get('regime') or 'UNKNOWN',closed_at=exit_time,gross_r=gross_r,gross_pnl=gross_pnl,costs={'spread_cost':None if exit_costs.get('exit_spread') is None else (r.get('entry_spread') or 0)+exit_costs.get('exit_spread'),'entry_slippage_cost':r.get('entry_slippage'),'exit_slippage_cost':exit_costs.get('exit_slippage'),'fee_cost':None if exit_costs.get('exit_fee') is None else (r.get('entry_fee') or 0)+exit_costs.get('exit_fee'),'funding_cost':exit_costs.get('funding'),'latency_cost':exit_costs.get('latency_impact_penalty')},net_r=net_r,net_pnl=net_pnl,hold_duration_seconds=hold,mfe=mfe,mae=mae,exit_reason=exit_reason,payload={'pending_position_id':r['pending_position_id'],'planned_rr_ignored':True})
    return {'status':'CLOSED','trade_id':trade_id,'evidence_complete':not missing,'net_r':net_r}


def resolve_pending_rejects(conn: Any, candles_by_symbol: Mapping[str, Sequence[Mapping[str,Any]]] | Sequence[Mapping[str,Any]], *, now: str|None=None) -> dict[str,int]:
    # Backward-compatible manual resolver across all campaigns. Campaign workers should call resolve_campaign_batch.
    bootstrap_campaign_schema(conn)
    row = _exec(conn, "SELECT campaign_id FROM burnin_pending_reject_labels WHERE status IN ('PENDING','READY') ORDER BY due_at LIMIT 1").fetchone()
    if not row:
        return {"resolved":0,"pending":0,"ambiguous":0,"failed":0,"expired":0}
    cid = row[0] if isinstance(row, sqlite3.Row) else row[0]
    return resolve_campaign_batch(conn, cid, candles_by_symbol, now=now)
