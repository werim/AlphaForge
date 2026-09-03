from __future__ import annotations
import json, math, sqlite3, uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Mapping, Sequence
from alphaforge.burnin import DIAGNOSTIC_OBSERVATION_KIND, canonical_hash, persist_burnin_reject_outcome, persist_burnin_trade_outcome, persist_burnin_observation, utc_now, CRITICAL_COST_FIELDS
from alphaforge.burnin_campaign import CAMPAIGN_SCHEMA_VERSION, bootstrap_campaign_schema, _exec


def _dt(v): return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
def _side(side): return str(side or "LONG").upper()
def _hit(side, high, low, stop, target):
    if _side(side) == "SHORT": return (high >= stop, low <= target)
    return (low <= stop, high >= target)


def timeframe_seconds(value: str | None) -> float | None:
    raw=str(value or "").strip().lower()
    if len(raw) < 2: return None
    units={"s":1,"m":60,"h":3600,"d":86400,"w":604800}
    try:
        seconds=float(raw[:-1])*units[raw[-1]]
        return seconds if math.isfinite(seconds) and seconds > 0 else None
    except (ValueError, KeyError): return None


def _geometry_errors(side, entry, stop, target):
    errors=[]
    values={"entry":entry,"stop":stop,"target":target}
    parsed={}
    for name,value in values.items():
        try: parsed[name]=float(value)
        except (TypeError,ValueError): errors.append(name); continue
        if not math.isfinite(parsed[name]): errors.append(name)
    if errors: return sorted(set(errors))
    e,s,t=parsed["entry"],parsed["stop"],parsed["target"]
    if e <= 0: errors.append("entry_non_positive")
    if e == s: errors.append("zero_risk")
    if _side(side) == "LONG" and not (s < e < t): errors.append("directionally_invalid_geometry")
    if _side(side) == "SHORT" and not (t < e < s): errors.append("directionally_invalid_geometry")
    if _side(side) not in {"LONG","SHORT"}: errors.append("side")
    return sorted(set(errors))


def persist_pending_reject_label(conn: Any, *, campaign_id: str, burnin_run_id: str, reject_decision_id: str, signal_id: str|None, symbol: str|None, side: str|None, decision_timestamp: str|None, entry: float|None, stop: float|None, target: float|None, horizon_seconds: float|None=None, horizon_bars: int|None=None, timeframe: str|None=None, execution_cost_assumptions: Mapping[str,Any]|None, regime: str|None, reject_reason: str|None, source_provenance: Mapping[str,Any]) -> str | None:
    bootstrap_campaign_schema(conn)
    interval_seconds=timeframe_seconds(timeframe)
    if horizon_bars is not None and interval_seconds is not None:
        horizon_seconds=float(horizon_bars)*interval_seconds
    critical={"symbol":symbol,"side":side,"decision_timestamp":decision_timestamp,"horizon_seconds":horizon_seconds,"execution_cost_assumptions":execution_cost_assumptions}
    missing=[k for k,v in critical.items() if v is None or v == "" or (k == "execution_cost_assumptions" and not isinstance(v,Mapping))]
    missing += _geometry_errors(side,entry,stop,target)
    if horizon_bars is not None and (not isinstance(horizon_bars,int) or horizon_bars <= 0): missing.append("horizon_bars")
    if timeframe is not None and interval_seconds is None: missing.append("timeframe")
    if missing:
        row=_exec(conn,"SELECT release_id FROM burnin_runs WHERE burnin_run_id=:bid",{"bid":burnin_run_id}).fetchone(); release_id=row[0] if row else "UNKNOWN"
        persist_burnin_observation(conn,observation_id="incomplete_reject_geometry_"+canonical_hash({"reject_decision_id":reject_decision_id})[:20],burnin_run_id=burnin_run_id,release_id=release_id,execution_mode="PAPER",observed_at=decision_timestamp or utc_now(),symbol=symbol,interval=timeframe,regime=regime or "UNKNOWN",decision="REJECTED",lifecycle_state="SIGNAL_REJECTED",metrics={"reject_decision_id":reject_decision_id,"reject_reason":reject_reason,"campaign_id":campaign_id},source_provenance=source_provenance,missing_fields=sorted(set(missing)),observation_kind=DIAGNOSTIC_OBSERVATION_KIND)
        return None
    due_at=datetime.fromtimestamp(_dt(decision_timestamp).timestamp()+float(horizon_seconds),timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
    pid="prej_"+canonical_hash({"reject_decision_id":reject_decision_id})[:20]
    _exec(conn,"""INSERT OR IGNORE INTO burnin_pending_reject_labels(pending_label_id,campaign_id,burnin_run_id,reject_decision_id,signal_id,symbol,side,decision_timestamp,timeframe,horizon_bars,entry,stop,target,horizon_seconds,execution_cost_assumptions_json,regime,reject_reason,source_provenance_json,due_at,status,created_at,schema_version) VALUES (:pid,:cid,:bid,:rid,:sid,:sym,:side,:ts,:tf,:bars,:entry,:stop,:target,:hor,:costs,:reg,:reason,:prov,:due,'PENDING',:now,:sv)""",{"pid":pid,"cid":campaign_id,"bid":burnin_run_id,"rid":reject_decision_id,"sid":signal_id,"sym":symbol,"side":side,"ts":decision_timestamp,"tf":timeframe,"bars":horizon_bars,"entry":entry,"stop":stop,"target":target,"hor":horizon_seconds,"costs":json.dumps(dict(execution_cost_assumptions or {}),sort_keys=True),"reg":regime,"reason":reject_reason,"prov":json.dumps(dict(source_provenance),sort_keys=True),"due":due_at,"now":utc_now(),"sv":CAMPAIGN_SCHEMA_VERSION})
    return pid


def _candles_for(source,r):
    if not isinstance(source,Mapping): return source
    return source.get((r["symbol"],r.get("timeframe"))) or source.get(r["symbol"],[])


def _normalized_candles(candles,r):
    unique={}
    for c in candles:
        try:
            ts=_dt(c.get("timestamp") or c.get("open_time") or c.get("time")); high=float(c["high"]); low=float(c["low"])
            if math.isfinite(high) and math.isfinite(low) and high >= low and _dt(r["decision_timestamp"]) < ts <= _dt(r["due_at"]): unique[ts]=dict(c,timestamp=ts.isoformat())
        except (TypeError,ValueError,KeyError): continue
    return [unique[k] for k in sorted(unique)]


def _window_complete(candles,r,terminal_index):
    interval=timeframe_seconds(r.get("timeframe"))
    bars=r.get("horizon_bars")
    if interval is None or bars is None: return True, []  # legacy rows retain pre-timeframe semantics
    expected=int(bars) if terminal_index is None else terminal_index+1
    observed=candles if terminal_index is None else candles[:terminal_index+1]
    gaps=[]; previous=_dt(r["decision_timestamp"])
    for candle in observed:
        current=_dt(candle["timestamp"])
        if (current-previous).total_seconds() > interval*1.5: gaps.append((previous.isoformat(),current.isoformat()))
        previous=current
    complete=len(observed) >= expected and not gaps
    if terminal_index is None and observed:
        complete=complete and (_dt(r["due_at"])-_dt(observed[-1]["timestamp"])).total_seconds() <= interval*0.5
    return complete,gaps


def _sync_review(conn,r,outcome):
    try:
        payload=json.loads(outcome.get("payload_json") or "{}")
        correct=payload.get("reject_correct") if outcome.get("evidence_complete") and not outcome.get("execution_invalidated") and not outcome.get("ambiguous") else None
        _exec(conn,"""UPDATE rejected_signal_reviews SET forward_window_bars=:bars,would_have_hit_tp=:tp,would_have_hit_sl=:sl,max_favorable_excursion_pct=:mfe,max_adverse_excursion_pct=:mae,reject_correct=:correct,execution_invalidated=:invalid,outcome_ambiguous=:amb,evidence_complete=:complete WHERE id=(SELECT id FROM rejected_signal_reviews WHERE (reject_decision_id=:rid OR (reject_decision_id IS NULL AND signal_id=:sid)) AND COALESCE(evidence_complete,0) != 1 ORDER BY reject_decision_id IS NULL,id LIMIT 1)""",{"bars":r.get("horizon_bars") or payload.get("forward_window_bars") or (round(float(r.get("horizon_seconds") or 0)/60) or None),"tp":outcome.get("would_tp"),"sl":outcome.get("would_sl"),"mfe":payload.get("mfe_pct"),"mae":payload.get("mae_pct"),"correct":correct,"invalid":outcome.get("execution_invalidated"),"amb":outcome.get("ambiguous"),"complete":outcome.get("evidence_complete"),"rid":r["reject_decision_id"],"sid":r.get("signal_id")})
    except Exception as exc:
        if "no such table" not in str(exc).lower() and "no such column" not in str(exc).lower(): raise


def resolve_campaign_batch(conn: Any,campaign_id: str,candles_by_symbol: Mapping[str,Sequence[Mapping[str,Any]]]|Sequence[Mapping[str,Any]],*,now: str|None=None,claim_timeout_seconds:float=300) -> dict[str,int]:
    bootstrap_campaign_schema(conn); now=now or utc_now(); stale=(_dt(now)-timedelta(seconds=claim_timeout_seconds)).isoformat()
    rows=_exec(conn,"SELECT * FROM burnin_pending_reject_labels WHERE campaign_id=:cid AND due_at<=:now AND (status IN ('PENDING','READY') OR (status='RESOLVING' AND claimed_at<:stale)) ORDER BY decision_timestamp,id",{"cid":campaign_id,"now":now,"stale":stale}).fetchall()
    counts={"resolved":0,"pending":0,"ambiguous":0,"failed":0,"claimed_elsewhere":0,"canonical":0}
    for row in rows:
        r=dict(row) if isinstance(row,sqlite3.Row) else dict(row._mapping); token=uuid.uuid4().hex; old=r["status"]
        claimed=_exec(conn,"UPDATE burnin_pending_reject_labels SET status='RESOLVING',claim_token=:token,claimed_at=:now WHERE pending_label_id=:pid AND status=:old AND COALESCE(claimed_at,'')=COALESCE(:claimed,'')",{"token":token,"now":now,"pid":r["pending_label_id"],"old":old,"claimed":r.get("claimed_at")}).rowcount
        if not claimed: counts["claimed_elsewhere"]+=1; continue
        existing=_exec(conn,"SELECT * FROM burnin_reject_outcomes WHERE reject_outcome_id=:id",{"id":"rout_"+r["reject_decision_id"]}).fetchone()
        if existing:
            outcome=dict(existing) if isinstance(existing,sqlite3.Row) else dict(existing._mapping); _sync_review(conn,r,outcome)
            status="AMBIGUOUS" if outcome.get("ambiguous") else ("RESOLVED" if outcome.get("evidence_complete") else "FAILED")
            _exec(conn,"UPDATE burnin_pending_reject_labels SET status=:s,evidence_complete=:ec,resolved_at=COALESCE(resolved_at,:now),last_error=CASE WHEN :ec=1 THEN NULL ELSE COALESCE(last_error,'CANONICAL_INCOMPLETE') END WHERE pending_label_id=:pid AND claim_token=:token",{"s":status,"ec":outcome.get("evidence_complete") or 0,"now":utc_now(),"pid":r["pending_label_id"],"token":token}); counts["canonical"]+=1; continue
        candles=_normalized_candles(_candles_for(candles_by_symbol,r),r)
        if not candles:
            _exec(conn,"UPDATE burnin_pending_reject_labels SET status='PENDING',claim_token=NULL,claimed_at=NULL,last_error='NO_CANDLES_IN_MARKET_WINDOW' WHERE pending_label_id=:pid AND claim_token=:token",{"pid":r["pending_label_id"],"token":token}); counts["pending"]+=1; continue
        label="TIMEOUT"; ambiguous=False; gross=0.0; terminal=None; entry=float(r["entry"]); sign=-1 if _side(r["side"])=="SHORT" else 1
        for i,c in enumerate(candles):
            sl,tp=_hit(r["side"],float(c["high"]),float(c["low"]),float(r["stop"]),float(r["target"]))
            if sl and tp: label="AMBIGUOUS"; ambiguous=True; gross=None; terminal=i; break
            if tp: label="TP_BEFORE_SL"; gross=abs((float(r["target"])-entry)/(entry-float(r["stop"]))); terminal=i; break
            if sl: label="SL_BEFORE_TP"; gross=-1.0; terminal=i; break
        observed=candles if terminal is None else candles[:terminal+1]
        favorable=[((float(c["high"])-entry)/entry if sign>0 else (entry-float(c["low"]))/entry)*100 for c in observed]
        adverse=[((entry-float(c["low"]))/entry if sign>0 else (float(c["high"])-entry)/entry)*100 for c in observed]
        mfe=max(favorable,default=0.0); mae=max(adverse,default=0.0); complete,gaps=_window_complete(candles,r,terminal)
        if not complete:
            diagnostic=json.dumps({"market_gaps":gaps,"observed_bars":len(observed),"required_bars":r.get("horizon_bars")},sort_keys=True)
            _exec(conn,"UPDATE burnin_pending_reject_labels SET status='PENDING',claim_token=NULL,claimed_at=NULL,evidence_complete=0,last_error=:err WHERE pending_label_id=:pid AND claim_token=:token",{"err":"INCOMPLETE_MARKET_WINDOW:"+diagnostic,"pid":r["pending_label_id"],"token":token}); counts["pending"]+=1; continue
        costs=json.loads(r.get("execution_cost_assumptions_json") or "{}"); missing=[f for f in CRITICAL_COST_FIELDS if costs.get(f) is None]
        invalid=bool(missing); total=None if invalid or gross is None else sum(float(costs[f]) for f in CRITICAL_COST_FIELDS); net=None if total is None else gross-total
        reject_correct=None if invalid or ambiguous or net is None or not complete else bool(net<=0)
        market_provenance=next((c.get("source_provenance") for c in observed if c.get("source_provenance")),None)
        payload={"pending_label_id":r["pending_label_id"],"forward_window_bars":r.get("horizon_bars"),"missing_cost_fields":missing,"window_complete":complete,"market_gaps":gaps,"mfe_pct":mfe,"mae_pct":mae,"reject_correct":reject_correct,"execution_cost_assumptions":costs,"market_data_provenance":market_provenance}
        evidence_complete=bool(complete and not invalid and not ambiguous and net is not None)
        inserted=persist_burnin_reject_outcome(conn,reject_outcome_id="rout_"+r["reject_decision_id"],burnin_run_id=r["burnin_run_id"],release_id=_release(conn,r["burnin_run_id"]),reject_reason=r.get("reject_reason") or "UNKNOWN",symbol=r["symbol"],regime=r.get("regime") or "UNKNOWN",decision_time=r["decision_timestamp"],hypothetical_entry=r["entry"],hypothetical_stop=r["stop"],hypothetical_target=r["target"],forward_label=label,would_tp=label=="TP_BEFORE_SL",would_sl=label=="SL_BEFORE_TP",timeout=label=="TIMEOUT",ambiguous=ambiguous,hypothetical_gross_r=gross,hypothetical_net_r_after_costs=net,avoided_loss=max(0,-net) if net is not None else None,missed_profit=max(0,net) if net is not None else None,execution_invalidated=invalid,evidence_horizon=r["due_at"],evidence_complete=evidence_complete,payload=payload)
        outcome_row=_exec(conn,"SELECT * FROM burnin_reject_outcomes WHERE reject_outcome_id=:id",{"id":"rout_"+r["reject_decision_id"]}).fetchone(); outcome=dict(outcome_row) if isinstance(outcome_row,sqlite3.Row) else dict(outcome_row._mapping); _sync_review(conn,r,outcome)
        status="AMBIGUOUS" if outcome.get("ambiguous") else ("RESOLVED" if outcome.get("evidence_complete") else "FAILED"); error=None if status=="RESOLVED" else ("AMBIGUOUS" if ambiguous else "MISSING_COSTS" if invalid else "INCOMPLETE_MARKET_WINDOW")
        _exec(conn,"UPDATE burnin_pending_reject_labels SET status=:s,evidence_complete=:ec,resolved_at=:now,last_error=:err WHERE pending_label_id=:pid AND claim_token=:token",{"s":status,"ec":outcome.get("evidence_complete") or 0,"now":utc_now(),"err":error,"pid":r["pending_label_id"],"token":token})
        counts["ambiguous" if status=="AMBIGUOUS" else "resolved" if status=="RESOLVED" else "failed"]+=1
    return counts


def _release(conn,bid):
    row=_exec(conn,"SELECT release_id FROM burnin_runs WHERE burnin_run_id=:bid",{"bid":bid}).fetchone(); return row[0] if row else "UNKNOWN"

def persist_pending_position(conn: Any, **kw) -> str:
    bootstrap_campaign_schema(conn); pid='ppos_'+canonical_hash({'trade_id':kw['trade_id']})[:20]
    vals={**kw,'pid':pid,'prov':json.dumps(dict(kw.get('source_provenance') or {}),sort_keys=True),'now':utc_now(),'sv':CAMPAIGN_SCHEMA_VERSION}
    _exec(conn,"""INSERT OR IGNORE INTO burnin_pending_position_outcomes(pending_position_id,trade_id,campaign_id,burnin_run_id,signal_id,symbol,side,entry_time,planned_entry,simulated_fill,stop,target,quantity,notional,entry_spread,entry_slippage,entry_fee,regime,source_provenance_json,status,created_at,schema_version) VALUES (:pid,:trade_id,:campaign_id,:burnin_run_id,:signal_id,:symbol,:side,:entry_time,:planned_entry,:simulated_fill,:stop,:target,:quantity,:notional,:entry_spread,:entry_slippage,:entry_fee,:regime,:prov,'OPEN',:now,:sv)""", vals)
    return pid

def resolve_position_closure(conn: Any, *, trade_id: str, exit_time: str, exit_price: float, exit_reason: str, exit_costs: Mapping[str,Any], mfe: float|None=None, mae: float|None=None, ambiguous: bool=False) -> dict[str,Any]:
    bootstrap_campaign_schema(conn); row=_exec(conn,"SELECT * FROM burnin_pending_position_outcomes WHERE trade_id=:t",{"t":trade_id}).fetchone()
    if not row: raise KeyError('position not found')
    r=dict(row) if isinstance(row, sqlite3.Row) else dict(row._mapping)
    if r.get('status') == 'CLOSED': return {'status':'IDEMPOTENT','trade_id':trade_id}
    fill=float(r.get('simulated_fill') or r.get('planned_entry')); qty=float(r.get('quantity') or 1); side=_side(r.get('side')); sign=-1 if side=='SHORT' else 1
    gross_pnl=(float(exit_price)-fill)*qty*sign; risk=abs(fill-float(r.get('stop') or fill)) or 1.0; gross_r=(float(exit_price)-fill)*sign/risk
    missing=[k for k in ('exit_spread','exit_slippage','exit_fee','funding','latency_impact_penalty') if exit_costs.get(k) is None]
    total=None if missing else sum(float(exit_costs.get(k) or 0) for k in ('exit_spread','exit_slippage','exit_fee','funding','latency_impact_penalty')) + float(r.get('entry_spread') or 0)+float(r.get('entry_slippage') or 0)+float(r.get('entry_fee') or 0)
    try: provenance=json.loads(r.get('source_provenance_json') or '{}')
    except (TypeError,json.JSONDecodeError): provenance={}
    net_r=None if total is None else gross_r-total
    net_pnl=None if total is None else gross_pnl-total
    if total is not None and provenance.get('execution_cost_unit') == 'R':
        net_pnl=gross_pnl-(total*risk*qty)
    hold=(_dt(exit_time)-_dt(r['entry_time'])).total_seconds()
    evidence_missing=[*missing, *(['ambiguous_intrabar_sequence'] if ambiguous else [])]
    _exec(conn,"""UPDATE burnin_pending_position_outcomes SET status='CLOSED',exit_time=:xt,exit_price=:xp,exit_reason=:xr,gross_pnl=:gp,gross_r=:gr,exit_spread=:es,exit_slippage=:esl,exit_fee=:ef,funding=:fu,latency_impact_penalty=:li,total_execution_cost=:tc,net_pnl=:np,net_r=:nr,hold_duration_seconds=:hold,mfe=:mfe,mae=:mae,evidence_complete=:ec,missing_fields_json=:mf,resolved_at=:now WHERE trade_id=:tid""",{"tid":trade_id,"xt":exit_time,"xp":exit_price,"xr":exit_reason,"gp":gross_pnl,"gr":gross_r,"es":exit_costs.get('exit_spread'),"esl":exit_costs.get('exit_slippage'),"ef":exit_costs.get('exit_fee'),"fu":exit_costs.get('funding'),"li":exit_costs.get('latency_impact_penalty'),"tc":total,"np":net_pnl,"nr":net_r,"hold":hold,"mfe":mfe,"mae":mae,"ec":0 if evidence_missing else 1,"mf":json.dumps(evidence_missing),"now":utc_now()})
    outcome_payload={'pending_position_id':r['pending_position_id'],'signal_id':r.get('signal_id'),'source_provenance':provenance,'phase':provenance.get('setup_phase'),'execution':provenance.get('execution_direction'),'ambiguous_intrabar_sequence':ambiguous}
    persist_burnin_trade_outcome(conn,outcome_id='tout_'+trade_id,burnin_run_id=r['burnin_run_id'],release_id=_release(conn,r['burnin_run_id']),trade_id=trade_id,symbol=r['symbol'],regime=r.get('regime') or 'UNKNOWN',closed_at=exit_time,gross_r=gross_r,gross_pnl=gross_pnl,costs={'spread_cost':None if exit_costs.get('exit_spread') is None else (r.get('entry_spread') or 0)+exit_costs.get('exit_spread'),'entry_slippage_cost':r.get('entry_slippage'),'exit_slippage_cost':exit_costs.get('exit_slippage'),'fee_cost':None if exit_costs.get('exit_fee') is None else (r.get('entry_fee') or 0)+exit_costs.get('exit_fee'),'funding_cost':exit_costs.get('funding'),'latency_cost':exit_costs.get('latency_impact_penalty'),'volatility_penalty':exit_costs.get('volatility_penalty'),'liquidity_penalty':exit_costs.get('liquidity_penalty')},net_r=net_r,net_pnl=net_pnl,effective_rr_at_entry=provenance.get('effective_rr_at_entry'),realized_effective_rr=net_r,hold_duration_seconds=hold,mfe=mfe,mae=mae,exit_reason=exit_reason,payload=outcome_payload)
    if ambiguous:
        _exec(conn,"UPDATE burnin_trade_outcomes SET evidence_complete=0,missing_cost_fields_json=:mf WHERE outcome_id=:oid",{'mf':json.dumps(evidence_missing),'oid':'tout_'+trade_id})
    return {'status':'CLOSED','trade_id':trade_id,'evidence_complete':not evidence_missing,'net_r':net_r,'exit_reason':exit_reason}


def resolve_campaign_positions(conn: Any, campaign_id: str, candles_by_trade: Mapping[Any,Sequence[Mapping[str,Any]]], *, now: str|None=None) -> dict[str,int]:
    """Resolve campaign PAPER positions from exchange 1m candles without inventing intrabar order."""
    bootstrap_campaign_schema(conn); now=now or utc_now()
    rows=_exec(conn,"SELECT * FROM burnin_pending_position_outcomes WHERE campaign_id=:cid AND status='OPEN' ORDER BY entry_time,id",{'cid':campaign_id}).fetchall()
    counts={'closed':0,'tp':0,'sl':0,'ambiguous':0,'pending':0}
    for raw in rows:
        r=dict(raw) if isinstance(raw,sqlite3.Row) else dict(raw._mapping)
        candles=candles_by_trade.get((r['symbol'],'position',r['trade_id'])) or candles_by_trade.get(r['trade_id']) or []
        normalized=[]
        for candle in candles:
            try:
                ts=_dt(candle.get('timestamp') or candle.get('open_time') or candle.get('time'))
                high=float(candle['high']); low=float(candle['low'])
                if math.isfinite(high) and math.isfinite(low) and high >= low and _dt(r['entry_time']) < ts <= _dt(now):
                    normalized.append((ts,high,low))
            except (TypeError,ValueError,KeyError): continue
        normalized.sort(key=lambda item:item[0])
        fill=float(r.get('simulated_fill') or r['planned_entry']); stop=float(r['stop']); target=float(r['target']); risk=abs(fill-stop) or 1.0
        sign=-1 if _side(r['side'])=='SHORT' else 1
        favorable=[]; adverse=[]; terminal=None
        for ts,high,low in normalized:
            favorable.append(((high-fill)*sign)/risk if sign>0 else ((fill-low)/risk))
            adverse.append(((fill-low)/risk) if sign>0 else ((high-fill)/risk))
            sl,tp=_hit(r['side'],high,low,stop,target)
            if sl or tp:
                terminal=(ts,sl,tp); break
        if terminal is None:
            counts['pending']+=1; continue
        try: provenance=json.loads(r.get('source_provenance_json') or '{}')
        except (TypeError,json.JSONDecodeError): provenance={}
        model=provenance.get('execution_cost_model') if isinstance(provenance.get('execution_cost_model'),Mapping) else {}
        def half(name):
            value=model.get(name)
            return None if value is None else float(value)/2.0
        exit_costs={'exit_spread':half('spread_penalty'),'exit_slippage':half('slippage_penalty'),'exit_fee':half('fee_penalty'),'funding':model.get('funding_penalty'),'latency_impact_penalty':model.get('latency_penalty'),'volatility_penalty':model.get('volatility_penalty'),'liquidity_penalty':model.get('liquidity_penalty')}
        ts,sl,tp=terminal; ambiguous=bool(sl and tp); reason='AMBIGUOUS_INTRABAR' if ambiguous else ('SL_HIT' if sl else 'TP_HIT'); price=stop if sl else target
        resolve_position_closure(conn,trade_id=r['trade_id'],exit_time=ts.isoformat().replace('+00:00','Z'),exit_price=price,exit_reason=reason,exit_costs=exit_costs,mfe=max(favorable,default=0.0),mae=max(adverse,default=0.0),ambiguous=ambiguous)
        counts['closed']+=1; counts['ambiguous' if ambiguous else 'sl' if sl else 'tp']+=1
    return counts


def resolve_pending_rejects(conn: Any, candles_by_symbol: Mapping[str, Sequence[Mapping[str,Any]]] | Sequence[Mapping[str,Any]], *, now: str|None=None) -> dict[str,int]:
    # Backward-compatible manual resolver across all campaigns. Campaign workers should call resolve_campaign_batch.
    bootstrap_campaign_schema(conn)
    row = _exec(conn, "SELECT campaign_id FROM burnin_pending_reject_labels WHERE status IN ('PENDING','READY') ORDER BY due_at LIMIT 1").fetchone()
    if not row:
        return {"resolved":0,"pending":0,"ambiguous":0,"failed":0}
    cid = row[0] if isinstance(row, sqlite3.Row) else row[0]
    return resolve_campaign_batch(conn, cid, candles_by_symbol, now=now)
