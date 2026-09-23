from __future__ import annotations
import json, math, sqlite3, uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Mapping, Sequence
from alphaforge.burnin import DIAGNOSTIC_OBSERVATION_KIND, canonical_decision_sql, canonical_hash, canonical_reject_outcome_link_matches, persist_burnin_reject_outcome, persist_burnin_trade_outcome, persist_burnin_observation, utc_now, CRITICAL_COST_FIELDS
from alphaforge.burnin_campaign import CAMPAIGN_SCHEMA_VERSION, bootstrap_campaign_schema, _exec
from alphaforge.expectancy_evidence import record_expectancy_evidence


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


def _canonical_reject_identity_error(conn: Any, campaign_id: str, burnin_run_id: str,
                                     reject_decision_id: str | None) -> str | None:
    reject_id = str(reject_decision_id or "").strip()
    if not reject_id:
        return "MISSING_REJECT_DECISION_ID"
    standalone_id = f"standalone:{burnin_run_id}"
    if campaign_id != standalone_id:
        campaign_runs = _exec(conn, """SELECT COUNT(*) FROM burnin_campaign_runs
            WHERE campaign_id=:cid AND burnin_run_id=:bid""",
            {"cid": campaign_id, "bid": burnin_run_id}).fetchone()
        if not campaign_runs or int(campaign_runs[0] or 0) != 1:
            return "RUN_NOT_IN_CAMPAIGN"
    canonical = _exec(conn, f"""SELECT COUNT(*) FROM burnin_observations o
        WHERE o.burnin_run_id=:bid AND UPPER(COALESCE(o.decision,''))='REJECTED'
        AND json_extract(o.metrics_json,'$.reject_decision_id')=:rid
        AND (json_extract(o.metrics_json,'$.campaign_id') IS NULL
             OR json_extract(o.metrics_json,'$.campaign_id')=:cid)
        AND {canonical_decision_sql('o')}""",
        {"cid": campaign_id, "bid": burnin_run_id, "rid": reject_id}).fetchone()
    if not canonical or int(canonical[0] or 0) != 1:
        return "CANONICAL_REJECT_NOT_FOUND"
    return None


def _persist_reject_identity_diagnostic(conn: Any, *, campaign_id: str,
                                        burnin_run_id: str,
                                        reject_decision_id: str | None,
                                        signal_id: str | None,
                                        decision_timestamp: str | None,
                                        symbol: str | None, timeframe: str | None,
                                        regime: str | None, reject_reason: str | None,
                                        source_provenance: Mapping[str, Any],
                                        identity_error: str) -> None:
    run = _exec(conn, "SELECT release_id FROM burnin_runs WHERE burnin_run_id=:bid",
                {"bid": burnin_run_id}).fetchone()
    if not run:
        return
    identity = {"campaign_id": campaign_id, "burnin_run_id": burnin_run_id,
                "reject_decision_id": reject_decision_id, "identity_error": identity_error}
    persist_burnin_observation(
        conn,
        observation_id="invalid_reject_identity_" + canonical_hash(identity)[:20],
        burnin_run_id=burnin_run_id, release_id=run[0], execution_mode="PAPER",
        observed_at=decision_timestamp or utc_now(), symbol=symbol, interval=timeframe,
        regime=regime or "UNKNOWN", decision="REJECTED",
        lifecycle_state="SIGNAL_REJECTED",
        metrics={"reject_decision_id": reject_decision_id, "signal_id": signal_id,
                 "reject_reason": reject_reason, "campaign_id": campaign_id,
                 "canonical_reject_identity_status": "INVALID",
                 "canonical_reject_identity_error": identity_error,
                 "reject_quality_attributable": False},
        source_provenance=source_provenance,
        missing_fields=[f"canonical_reject_identity:{identity_error}"],
        observation_kind=DIAGNOSTIC_OBSERVATION_KIND,
    )


def persist_pending_reject_label(conn: Any, *, campaign_id: str, burnin_run_id: str, reject_decision_id: str, signal_id: str|None, symbol: str|None, side: str|None, decision_timestamp: str|None, entry: float|None, stop: float|None, target: float|None, horizon_seconds: float|None=None, horizon_bars: int|None=None, timeframe: str|None=None, execution_cost_assumptions: Mapping[str,Any]|None, regime: str|None, reject_reason: str|None, source_provenance: Mapping[str,Any]) -> str | None:
    bootstrap_campaign_schema(conn)
    identity_error = _canonical_reject_identity_error(
        conn, campaign_id, burnin_run_id, reject_decision_id)
    if identity_error:
        _persist_reject_identity_diagnostic(
            conn, campaign_id=campaign_id, burnin_run_id=burnin_run_id,
            reject_decision_id=reject_decision_id, signal_id=signal_id,
            decision_timestamp=decision_timestamp, symbol=symbol, timeframe=timeframe,
            regime=regime, reject_reason=reject_reason,
            source_provenance=source_provenance, identity_error=identity_error)
        return None
    contract_ineligible = _exec(conn, """SELECT 1 FROM burnin_observations
        WHERE burnin_run_id=:bid
          AND observation_id LIKE 'incomplete_reject_geometry_%'
          AND json_extract(metrics_json,'$.reject_decision_id')=:rid
          AND evidence_complete=0 LIMIT 1""",
        {"bid": burnin_run_id, "rid": reject_decision_id}).fetchone()
    if contract_ineligible:
        return None
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
        persist_burnin_observation(conn,observation_id="incomplete_reject_geometry_"+canonical_hash({"campaign_id":campaign_id,"burnin_run_id":burnin_run_id,"reject_decision_id":reject_decision_id})[:20],burnin_run_id=burnin_run_id,release_id=release_id,execution_mode="PAPER",observed_at=decision_timestamp or utc_now(),symbol=symbol,interval=timeframe,regime=regime or "UNKNOWN",decision="REJECTED",lifecycle_state="SIGNAL_REJECTED",metrics={"reject_decision_id":reject_decision_id,"reject_reason":reject_reason,"campaign_id":campaign_id},source_provenance=source_provenance,missing_fields=sorted(set(missing)),observation_kind=DIAGNOSTIC_OBSERVATION_KIND)
        return None
    due_at=datetime.fromtimestamp(_dt(decision_timestamp).timestamp()+float(horizon_seconds),timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
    pid="prej_"+str(reject_decision_id)
    _exec(conn,"""INSERT OR IGNORE INTO burnin_pending_reject_labels(pending_label_id,campaign_id,burnin_run_id,reject_decision_id,signal_id,symbol,side,decision_timestamp,timeframe,horizon_bars,entry,stop,target,horizon_seconds,execution_cost_assumptions_json,regime,reject_reason,source_provenance_json,due_at,status,created_at,schema_version) VALUES (:pid,:cid,:bid,:rid,:sid,:sym,:side,:ts,:tf,:bars,:entry,:stop,:target,:hor,:costs,:reg,:reason,:prov,:due,'PENDING',:now,:sv)""",{"pid":pid,"cid":campaign_id,"bid":burnin_run_id,"rid":reject_decision_id,"sid":signal_id,"sym":symbol,"side":side,"ts":decision_timestamp,"tf":timeframe,"bars":horizon_bars,"entry":entry,"stop":stop,"target":target,"hor":horizon_seconds,"costs":json.dumps(dict(execution_cost_assumptions or {}),sort_keys=True),"reg":regime,"reason":reject_reason,"prov":json.dumps(dict(source_provenance),sort_keys=True),"due":due_at,"now":utc_now(),"sv":CAMPAIGN_SCHEMA_VERSION})
    persisted = _exec(conn, """SELECT pending_label_id,campaign_id,burnin_run_id
        FROM burnin_pending_reject_labels WHERE reject_decision_id=:rid""",
        {"rid": reject_decision_id}).fetchone()
    if not persisted or persisted[0] != pid or persisted[1] != campaign_id or persisted[2] != burnin_run_id:
        _persist_reject_identity_diagnostic(
            conn, campaign_id=campaign_id, burnin_run_id=burnin_run_id,
            reject_decision_id=reject_decision_id, signal_id=signal_id,
            decision_timestamp=decision_timestamp, symbol=symbol, timeframe=timeframe,
            regime=regime, reject_reason=reject_reason,
            source_provenance=source_provenance,
            identity_error="PENDING_LABEL_IDENTITY_CONFLICT")
        return None
    return pid


def _candles_for(source,r):
    if not isinstance(source,Mapping): return source
    return source.get((r["symbol"],r.get("timeframe"))) or source.get(r["symbol"],[])


def _normalize_candle_window(candles, r):
    """Canonical candle normalization shared by reject and accepted-position resolvers."""
    try:
        window_start = _dt(r["decision_timestamp"])
        window_end = _dt(r["due_at"])
        if window_end <= window_start:
            return [], ["window_bounds:non_positive"]
    except (TypeError, ValueError, KeyError):
        return [], ["window_bounds:malformed"]

    unique = {}
    input_errors = []
    for index, candle in enumerate(candles or []):
        if not isinstance(candle, Mapping):
            input_errors.append(f"candle[{index}]:not_mapping")
            continue
        if candle.get("is_closed") is False or candle.get("closed") is False:
            continue
        raw_ts = candle.get("timestamp") or candle.get("open_time") or candle.get("time")
        try:
            if raw_ts is None or raw_ts == "":
                raise ValueError("missing timestamp")
            ts = _dt(raw_ts)
            in_window = window_start < ts <= window_end
        except (TypeError, ValueError):
            input_errors.append(f"candle[{index}]:malformed_timestamp")
            continue
        if not in_window:
            continue
        try:
            high = float(candle["high"])
            low = float(candle["low"])
            if not math.isfinite(high) or not math.isfinite(low) or high < low:
                raise ValueError("invalid high/low")
        except (TypeError, ValueError, KeyError):
            input_errors.append(f"candle[{index}]:malformed_ohlc")
            continue

        existing = unique.get(ts)
        if existing is not None:
            if float(existing["high"]) != high or float(existing["low"]) != low:
                input_errors.append(f"duplicate_conflict:{ts.isoformat()}")
            continue
        unique[ts] = dict(candle, timestamp=ts.isoformat(), high=high, low=low)

    return [unique[key] for key in sorted(unique)], sorted(set(input_errors))


def _normalized_candles(candles, r):
    normalized, _ = _normalize_candle_window(candles, r)
    return normalized


def _window_complete(candles, r, terminal_index, *, input_errors=None):
    interval = timeframe_seconds(r.get("timeframe"))
    bars = r.get("horizon_bars")
    if input_errors:
        return False, []
    if interval is None or bars is None:
        return True, []  # legacy rows retain pre-timeframe semantics
    expected = int(bars) if terminal_index is None else terminal_index + 1
    observed = candles if terminal_index is None else candles[:terminal_index + 1]
    gaps = []
    previous = _dt(r["decision_timestamp"])
    for index, candle in enumerate(observed):
        current = _dt(candle["timestamp"])
        delta = (current - previous).total_seconds()
        # The first forward candle must be the next interval boundary; later
        # candles use the existing 1.5x tolerance while still rejecting a
        # missing full bar.
        max_gap = interval if index == 0 else interval * 1.5
        if delta <= 0 or delta > max_gap:
            gaps.append((previous.isoformat(), current.isoformat()))
        previous = current
    complete = len(observed) >= expected and not gaps
    if terminal_index is None and observed:
        complete = complete and (_dt(r["due_at"]) - _dt(observed[-1]["timestamp"])).total_seconds() <= interval * 0.5
    return complete, gaps


def _sync_review(conn,r,outcome):
    try:
        payload=json.loads(outcome.get("payload_json") or "{}")
        correct=payload.get("reject_correct") if outcome.get("evidence_complete") and not outcome.get("execution_invalidated") and not outcome.get("ambiguous") else None
        _exec(conn,"""UPDATE rejected_signal_reviews SET forward_window_bars=:bars,would_have_hit_tp=:tp,would_have_hit_sl=:sl,max_favorable_excursion_pct=:mfe,max_adverse_excursion_pct=:mae,reject_correct=:correct,execution_invalidated=:invalid,outcome_ambiguous=:amb,evidence_complete=:complete WHERE id=(SELECT id FROM rejected_signal_reviews WHERE reject_decision_id=:rid AND COALESCE(evidence_complete,0) != 1 ORDER BY id LIMIT 1)""",{"bars":r.get("horizon_bars") or payload.get("forward_window_bars") or (round(float(r.get("horizon_seconds") or 0)/60) or None),"tp":outcome.get("would_tp"),"sl":outcome.get("would_sl"),"mfe":payload.get("mfe_pct"),"mae":payload.get("mae_pct"),"correct":correct,"invalid":outcome.get("execution_invalidated"),"amb":outcome.get("ambiguous"),"complete":outcome.get("evidence_complete"),"rid":r["reject_decision_id"]})
    except Exception as exc:
        if "no such table" not in str(exc).lower() and "no such column" not in str(exc).lower(): raise


def resolve_campaign_batch(conn: Any,campaign_id: str,candles_by_symbol: Mapping[str,Sequence[Mapping[str,Any]]]|Sequence[Mapping[str,Any]],*,now: str|None=None,claim_timeout_seconds:float=300) -> dict[str,int]:
    bootstrap_campaign_schema(conn); now=now or utc_now(); stale=(_dt(now)-timedelta(seconds=claim_timeout_seconds)).isoformat()
    rows=_exec(conn,"SELECT * FROM burnin_pending_reject_labels WHERE campaign_id=:cid AND due_at<=:now AND (status IN ('PENDING','READY') OR (status='RESOLVING' AND claimed_at<:stale)) ORDER BY decision_timestamp,id",{"cid":campaign_id,"now":now,"stale":stale}).fetchall()
    counts={"resolved":0,"pending":0,"ambiguous":0,"failed":0,"claimed_elsewhere":0,"canonical":0}
    for row in rows:
        r=dict(row) if isinstance(row,sqlite3.Row) else dict(row._mapping); token=uuid.uuid4().hex; old=r["status"]
        identity_error = _canonical_reject_identity_error(
            conn, campaign_id, r["burnin_run_id"], r.get("reject_decision_id"))
        if identity_error:
            _exec(conn, """UPDATE burnin_pending_reject_labels
                SET status='FAILED',evidence_complete=0,resolved_at=:now,
                    last_error=:error,claim_token=NULL,claimed_at=NULL
                WHERE pending_label_id=:pid""",
                {"now": utc_now(), "error": f"CANONICAL_REJECT_IDENTITY_INVALID:{identity_error}",
                 "pid": r["pending_label_id"]})
            counts["failed"] += 1
            continue
        claimed=_exec(conn,"UPDATE burnin_pending_reject_labels SET status='RESOLVING',claim_token=:token,claimed_at=:now WHERE pending_label_id=:pid AND status=:old AND COALESCE(claimed_at,'')=COALESCE(:claimed,'')",{"token":token,"now":now,"pid":r["pending_label_id"],"old":old,"claimed":r.get("claimed_at")}).rowcount
        if not claimed: counts["claimed_elsewhere"]+=1; continue
        existing=_exec(conn,"SELECT * FROM burnin_reject_outcomes WHERE reject_outcome_id=:id",{"id":"rout_"+r["reject_decision_id"]}).fetchone()
        if existing:
            outcome=dict(existing) if isinstance(existing,sqlite3.Row) else dict(existing._mapping)
            if not canonical_reject_outcome_link_matches(
                    outcome, campaign_id=campaign_id, burnin_run_id=r["burnin_run_id"],
                    reject_decision_id=r["reject_decision_id"],
                    pending_label_id=r["pending_label_id"]):
                _exec(conn, """UPDATE burnin_pending_reject_labels
                    SET status='FAILED',evidence_complete=0,resolved_at=:now,
                        last_error='CANONICAL_OUTCOME_IDENTITY_CONFLICT'
                    WHERE pending_label_id=:pid AND claim_token=:token""",
                    {"now": utc_now(), "pid": r["pending_label_id"], "token": token})
                counts["failed"] += 1
                continue
            _sync_review(conn,r,outcome)
            status="AMBIGUOUS" if outcome.get("ambiguous") else ("RESOLVED" if outcome.get("evidence_complete") else "FAILED")
            _exec(conn,"UPDATE burnin_pending_reject_labels SET status=:s,evidence_complete=:ec,resolved_at=COALESCE(resolved_at,:now),last_error=CASE WHEN :ec=1 THEN NULL ELSE COALESCE(last_error,'CANONICAL_INCOMPLETE') END WHERE pending_label_id=:pid AND claim_token=:token",{"s":status,"ec":outcome.get("evidence_complete") or 0,"now":utc_now(),"pid":r["pending_label_id"],"token":token}); counts["canonical"]+=1; continue
        raw_candles=_candles_for(candles_by_symbol,r)
        candles=_normalized_candles(raw_candles,r)
        if not candles:
            _exec(conn,"UPDATE burnin_pending_reject_labels SET status='PENDING',claim_token=NULL,claimed_at=NULL,last_error='NO_CANDLES_IN_MARKET_WINDOW' WHERE pending_label_id=:pid AND claim_token=:token",{"pid":r["pending_label_id"],"token":token}); counts["pending"]+=1; continue
        evaluated=evaluate_forward_outcome(side=r["side"],entry=r["entry"],stop=r["stop"],
            target=r["target"],decision_timestamp=r["decision_timestamp"],due_at=r["due_at"],
            timeframe=r.get("timeframe"),horizon_bars=r.get("horizon_bars"),candles=raw_candles)
        label=evaluated["forward_label"]; ambiguous=evaluated["ambiguous"]
        gross=evaluated["gross_r"]; mfe=evaluated["mfe"]; mae=evaluated["mae"]
        complete=evaluated["window_complete"]; gaps=evaluated["market_gaps"]
        observed=candles if evaluated["terminal_index"] is None else candles[:evaluated["terminal_index"]+1]
        if not complete:
            diagnostic=json.dumps({"market_gaps":gaps,"observed_bars":evaluated["observed_bars"],"required_bars":r.get("horizon_bars")},sort_keys=True)
            _exec(conn,"UPDATE burnin_pending_reject_labels SET status='PENDING',claim_token=NULL,claimed_at=NULL,evidence_complete=0,last_error=:err WHERE pending_label_id=:pid AND claim_token=:token",{"err":"INCOMPLETE_MARKET_WINDOW:"+diagnostic,"pid":r["pending_label_id"],"token":token}); counts["pending"]+=1; continue
        costs=json.loads(r.get("execution_cost_assumptions_json") or "{}"); missing=[f for f in CRITICAL_COST_FIELDS if costs.get(f) is None]
        invalid=bool(missing)
        total=None if invalid or gross is None else (
            sum(float(costs[f]) for f in CRITICAL_COST_FIELDS)
            + float(costs.get("volatility_penalty") or 0.0)
            + float(costs.get("liquidity_penalty") or 0.0)
        )
        net=None if total is None else gross-total
        try: source_provenance=json.loads(r.get("source_provenance_json") or "{}")
        except (TypeError,json.JSONDecodeError): source_provenance={}
        subject=source_provenance.get("forward_label_subject")
        basis=str(source_provenance.get("reject_execution_basis") or "PLANNED_ENTRY_LEGACY")
        execution_aligned=basis == "EXPECTED_FILL_RUNTIME_PARITY"
        infrastructure_reject=str(r.get("reject_reason") or "").upper() in {
            "EXCHANGE_STATE_UNKNOWN", "EXCHANGE_RECONCILIATION_UNAVAILABLE", "RUNTIME_RECOVERY_REQUIRED"
        }
        explicit_attributable=source_provenance.get("reject_quality_attributable")
        outcome_attributable=(explicit_attributable is not False
                              and subject != "LEGACY_SCANNER_SHADOW_CANDIDATE"
                              and not infrastructure_reject)
        execution_authoritative=outcome_attributable and execution_aligned
        reject_correct=None if invalid or ambiguous or net is None or not complete or not outcome_attributable else bool(net<=0)
        market_provenance=next((c.get("source_provenance") for c in observed if c.get("source_provenance")),None)
        payload={
            "pending_label_id":r["pending_label_id"],
            "reject_decision_id":r["reject_decision_id"],
            "campaign_id":campaign_id,
            "burnin_run_id":r["burnin_run_id"],
            "forward_window_bars":r.get("horizon_bars"),
            "missing_cost_fields":missing,
            "window_complete":complete,
            "market_gaps":gaps,
            "mfe_pct":mfe,
            "mae_pct":mae,
            "reject_correct":reject_correct,
            "execution_cost_assumptions":costs,
            "execution_cost_unit":costs.get("execution_cost_unit"),
            "market_data_provenance":market_provenance,
            "forward_label_subject":subject,
            "reject_quality_attributable":outcome_attributable,
            "reject_execution_authoritative":execution_authoritative,
            "reject_execution_basis":basis,
            "execution_aligned":execution_aligned,
            "planned_entry":source_provenance.get("planned_entry"),
            "executable_entry":source_provenance.get("executable_entry", r.get("entry")),
            "candidate_raw_rr":source_provenance.get("candidate_raw_rr"),
            "executable_raw_rr":source_provenance.get("executable_raw_rr"),
            "remaining_execution_penalty":source_provenance.get("remaining_execution_penalty"),
            "effective_rr_at_decision":source_provenance.get("effective_rr_at_decision"),
            "counterfactual_effective_rr":source_provenance.get("counterfactual_effective_rr"),
            "entry_slippage_embedded_in_fill":source_provenance.get("entry_slippage_embedded_in_fill"),
            "embedded_entry_slippage_cost":source_provenance.get("embedded_entry_slippage_cost"),
            "fill_shift_initial_risk_ratio":source_provenance.get("fill_shift_initial_risk_ratio"),
            "stop_distance_pct":source_provenance.get("stop_distance_pct"),
            "all_failed_gates":source_provenance.get("all_failed_gates"),
            "failed_gate_evidence":source_provenance.get("failed_gate_evidence"),
            "execution_cost_semantics":source_provenance.get("execution_cost_semantics"),
            "non_attributable_reason":(
                None if outcome_attributable else
                source_provenance.get("non_attributable_reason") or
                ("INFRASTRUCTURE_UNAVAILABILITY" if infrastructure_reject else
                 "LEGACY_SHADOW_NOT_GUIDED_EQUIVALENT" if subject == "LEGACY_SCANNER_SHADOW_CANDIDATE"
                 else "NON_ATTRIBUTABLE_REJECT")
            ),
            "non_execution_authoritative_reason":(
                None if execution_authoritative else
                "LEGACY_PLANNED_ENTRY_BASIS" if outcome_attributable and not execution_aligned
                else source_provenance.get("non_attributable_reason") or
                ("INFRASTRUCTURE_UNAVAILABILITY" if infrastructure_reject else
                 "LEGACY_SHADOW_NOT_GUIDED_EQUIVALENT" if subject == "LEGACY_SCANNER_SHADOW_CANDIDATE"
                 else "NON_ATTRIBUTABLE_REJECT")
            ),
        }
        evidence_complete=bool(complete and not invalid and not ambiguous and net is not None)
        inserted=persist_burnin_reject_outcome(conn,reject_outcome_id="rout_"+r["reject_decision_id"],burnin_run_id=r["burnin_run_id"],release_id=_release(conn,r["burnin_run_id"]),reject_reason=r.get("reject_reason") or "UNKNOWN",symbol=r["symbol"],regime=r.get("regime") or "UNKNOWN",decision_time=r["decision_timestamp"],hypothetical_entry=r["entry"],hypothetical_stop=r["stop"],hypothetical_target=r["target"],forward_label=label,would_tp=label=="TP_BEFORE_SL",would_sl=label=="SL_BEFORE_TP",timeout=label=="TIMEOUT",ambiguous=ambiguous,hypothetical_gross_r=gross,hypothetical_net_r_after_costs=net,avoided_loss=max(0,-net) if net is not None else None,missed_profit=max(0,net) if net is not None else None,execution_invalidated=invalid,evidence_horizon=r["due_at"],evidence_complete=evidence_complete,payload=payload)
        outcome_row=_exec(conn,"SELECT * FROM burnin_reject_outcomes WHERE reject_outcome_id=:id",{"id":"rout_"+r["reject_decision_id"]}).fetchone(); outcome=dict(outcome_row) if isinstance(outcome_row,sqlite3.Row) else dict(outcome_row._mapping)
        if not canonical_reject_outcome_link_matches(
                outcome, campaign_id=campaign_id, burnin_run_id=r["burnin_run_id"],
                reject_decision_id=r["reject_decision_id"],
                pending_label_id=r["pending_label_id"]):
            _exec(conn, """UPDATE burnin_pending_reject_labels
                SET status='FAILED',evidence_complete=0,resolved_at=:now,
                    last_error='CANONICAL_OUTCOME_IDENTITY_CONFLICT'
                WHERE pending_label_id=:pid AND claim_token=:token""",
                {"now": utc_now(), "pid": r["pending_label_id"], "token": token})
            counts["failed"] += 1
            continue
        _sync_review(conn,r,outcome)
        status="AMBIGUOUS" if outcome.get("ambiguous") else ("RESOLVED" if outcome.get("evidence_complete") else "FAILED"); error=None if status=="RESOLVED" else ("AMBIGUOUS" if ambiguous else "MISSING_COSTS" if invalid else "INCOMPLETE_MARKET_WINDOW")
        resolved_at=utc_now()
        _exec(conn,"UPDATE burnin_pending_reject_labels SET status=:s,evidence_complete=:ec,resolved_at=:now,last_error=:err WHERE pending_label_id=:pid AND claim_token=:token",{"s":status,"ec":outcome.get("evidence_complete") or 0,"now":resolved_at,"err":error,"pid":r["pending_label_id"],"token":token})
        record_expectancy_evidence(conn,evidence_id='reject:'+r['reject_decision_id'],source_decision_id=r.get('reject_decision_id'),evidence_type='REJECT_FORWARD',decision_time=r.get('decision_timestamp'),resolved_at=resolved_at,symbol=r.get('symbol'),side=r.get('side'),setup_type=None,regime=r.get('regime'),reject_reason=r.get('reject_reason'),net_r=net,run_id=r.get('burnin_run_id'),campaign_id=campaign_id,release_id=_release(conn,r['burnin_run_id']),evidence_complete=status=='RESOLVED' and execution_authoritative)
        counts["ambiguous" if status=="AMBIGUOUS" else "resolved" if status=="RESOLVED" else "failed"]+=1
    return counts


def _release(conn,bid):
    row=_exec(conn,"SELECT release_id FROM burnin_runs WHERE burnin_run_id=:bid",{"bid":bid}).fetchone(); return row[0] if row else "UNKNOWN"

def persist_pending_position(conn: Any, **kw) -> str:
    bootstrap_campaign_schema(conn); pid='ppos_'+canonical_hash({'trade_id':kw['trade_id']})[:20]
    vals={**kw,'source_decision_id':kw.get('source_decision_id'),'decision_time':kw.get('decision_time'),'setup_type':kw.get('setup_type'),'pid':pid,'prov':json.dumps(dict(kw.get('source_provenance') or {}),sort_keys=True),'now':utc_now(),'sv':CAMPAIGN_SCHEMA_VERSION}
    _exec(conn,"""INSERT OR IGNORE INTO burnin_pending_position_outcomes(pending_position_id,trade_id,campaign_id,burnin_run_id,signal_id,source_decision_id,decision_time,symbol,side,setup_type,entry_time,planned_entry,simulated_fill,stop,target,quantity,notional,entry_spread,entry_slippage,entry_fee,regime,source_provenance_json,status,created_at,schema_version) VALUES (:pid,:trade_id,:campaign_id,:burnin_run_id,:signal_id,:source_decision_id,:decision_time,:symbol,:side,:setup_type,:entry_time,:planned_entry,:simulated_fill,:stop,:target,:quantity,:notional,:entry_spread,:entry_slippage,:entry_fee,:regime,:prov,'OPEN',:now,:sv)""", vals)
    return pid

def resolve_position_closure(conn: Any, *, trade_id: str, exit_time: str, exit_price: float, exit_reason: str, exit_costs: Mapping[str,Any], mfe: float|None=None, mae: float|None=None, ambiguous: bool=False) -> dict[str,Any]:
    bootstrap_campaign_schema(conn); row=_exec(conn,"SELECT * FROM burnin_pending_position_outcomes WHERE trade_id=:t",{"t":trade_id}).fetchone()
    if not row: raise KeyError('position not found')
    r=dict(row) if isinstance(row, sqlite3.Row) else dict(row._mapping)
    if r.get('status') == 'CLOSED': return {'status':'IDEMPOTENT','trade_id':trade_id}
    fill=float(r.get('simulated_fill') or r.get('planned_entry')); qty=float(r['quantity']); side=_side(r.get('side')); sign=-1 if side=='SHORT' else 1
    risk_per_unit=abs(fill-float(r['stop'])) if r.get('stop') is not None else 0.0
    risk_usd=risk_per_unit*qty
    gross_pnl=(float(exit_price)-fill)*qty*sign
    gross_r=gross_pnl/risk_usd if risk_usd > 0 else None
    missing=[k for k in ('exit_spread','exit_slippage','exit_fee','funding','latency_impact_penalty') if exit_costs.get(k) is None]
    missing += [k for k in ('entry_spread','entry_slippage','entry_fee') if r.get(k) is None]
    if risk_usd <= 0: missing.append('risk_usd')
    try: provenance=json.loads(r.get('source_provenance_json') or '{}')
    except (TypeError,json.JSONDecodeError): provenance={}
    raw_costs={
        'spread_cost': None if r.get('entry_spread') is None or exit_costs.get('exit_spread') is None else float(r['entry_spread'])+float(exit_costs['exit_spread']),
        'entry_slippage_cost': r.get('entry_slippage'),
        'exit_slippage_cost': exit_costs.get('exit_slippage'),
        'fee_cost': None if r.get('entry_fee') is None or exit_costs.get('exit_fee') is None else float(r['entry_fee'])+float(exit_costs['exit_fee']),
        'funding_cost': exit_costs.get('funding'),
        'latency_cost': exit_costs.get('latency_impact_penalty'),
        'volatility_penalty': exit_costs.get('volatility_penalty'),
        'liquidity_penalty': exit_costs.get('liquidity_penalty'),
    }
    multiplier=risk_usd if provenance.get('execution_cost_unit') == 'R' else 1.0
    costs_usd={key: None if value is None else float(value)*multiplier for key,value in raw_costs.items()}
    total=None if missing else sum(float(value or 0) for value in costs_usd.values())
    net_pnl=None if total is None else gross_pnl-total
    net_r=None if net_pnl is None or risk_usd <= 0 else net_pnl/risk_usd
    hold=(_dt(exit_time)-_dt(r['entry_time'])).total_seconds()
    evidence_missing=[*missing, *(['ambiguous_intrabar_sequence'] if ambiguous else [])]
    resolved_at=utc_now()
    _exec(conn,"""UPDATE burnin_pending_position_outcomes SET status='CLOSED',exit_time=:xt,exit_price=:xp,exit_reason=:xr,gross_pnl=:gp,gross_r=:gr,exit_spread=:es,exit_slippage=:esl,exit_fee=:ef,funding=:fu,latency_impact_penalty=:li,total_execution_cost=:tc,net_pnl=:np,net_r=:nr,hold_duration_seconds=:hold,mfe=:mfe,mae=:mae,evidence_complete=:ec,missing_fields_json=:mf,resolved_at=:now WHERE trade_id=:tid""",{"tid":trade_id,"xt":exit_time,"xp":exit_price,"xr":exit_reason,"gp":gross_pnl,"gr":gross_r,"es":exit_costs.get('exit_spread'),"esl":exit_costs.get('exit_slippage'),"ef":exit_costs.get('exit_fee'),"fu":exit_costs.get('funding'),"li":exit_costs.get('latency_impact_penalty'),"tc":total,"np":net_pnl,"nr":net_r,"hold":hold,"mfe":mfe,"mae":mae,"ec":0 if evidence_missing else 1,"mf":json.dumps(evidence_missing),"now":resolved_at})
    outcome_payload={'pending_position_id':r['pending_position_id'],'signal_id':r.get('signal_id'),'source_provenance':provenance,'phase':provenance.get('setup_phase'),'execution':provenance.get('execution_direction'),'ambiguous_intrabar_sequence':ambiguous,'quantity':qty,'notional':r.get('notional'),'simulated_fill':fill,'cost_unit':'USD'}
    persist_burnin_trade_outcome(conn,outcome_id='tout_'+trade_id,burnin_run_id=r['burnin_run_id'],release_id=_release(conn,r['burnin_run_id']),trade_id=trade_id,symbol=r['symbol'],regime=r.get('regime') or 'UNKNOWN',closed_at=exit_time,gross_r=gross_r,gross_pnl=gross_pnl,costs=costs_usd,net_r=net_r,net_pnl=net_pnl,effective_rr_at_entry=provenance.get('effective_rr_at_entry'),realized_effective_rr=net_r,hold_duration_seconds=hold,mfe=mfe,mae=mae,exit_reason=exit_reason,payload=outcome_payload)
    record_expectancy_evidence(conn,evidence_id='accepted:'+trade_id,source_decision_id=r.get('source_decision_id'),evidence_type='ACCEPTED_TRADE',decision_time=r.get('decision_time'),resolved_at=resolved_at,symbol=r.get('symbol'),side=r.get('side'),setup_type=r.get('setup_type'),regime=r.get('regime'),reject_reason=None,net_r=net_r,run_id=r.get('burnin_run_id'),campaign_id=r.get('campaign_id'),release_id=_release(conn,r['burnin_run_id']),evidence_complete=not evidence_missing)
    if ambiguous:
        _exec(conn,"UPDATE burnin_trade_outcomes SET evidence_complete=0,missing_cost_fields_json=:mf WHERE outcome_id=:oid",{'mf':json.dumps(evidence_missing),'oid':'tout_'+trade_id})
    return {'status':'CLOSED','trade_id':trade_id,'evidence_complete':not evidence_missing,'net_r':net_r,'exit_reason':exit_reason}


def resolve_campaign_positions(conn: Any, campaign_id: str, candles_by_trade: Mapping[Any,Sequence[Mapping[str,Any]]], *, now: str|None=None) -> dict[str,int]:
    """Resolve PAPER positions only across a complete canonical 1m entry-to-terminal path."""
    bootstrap_campaign_schema(conn); now=now or utc_now()
    rows=_exec(conn,"SELECT * FROM burnin_pending_position_outcomes WHERE campaign_id=:cid AND status='OPEN' ORDER BY entry_time,id",{'cid':campaign_id}).fetchall()
    counts={'closed':0,'tp':0,'sl':0,'ambiguous':0,'pending':0}
    for raw in rows:
        r=dict(raw) if isinstance(raw,sqlite3.Row) else dict(raw._mapping)
        candles=candles_by_trade.get((r['symbol'],'position',r['trade_id'])) or candles_by_trade.get(r['trade_id']) or []
        window_row={
            'decision_timestamp':r['entry_time'],
            'due_at':now,
            'timeframe':'1m',
            'horizon_bars':1,
        }
        normalized,input_errors=_normalize_candle_window(candles,window_row)

        fill=float(r.get('simulated_fill') or r['planned_entry']); stop=float(r['stop']); target=float(r['target']); risk=abs(fill-stop) or 1.0
        sign=-1 if _side(r['side'])=='SHORT' else 1
        favorable=[]; adverse=[]; terminal=None
        for index,candle in enumerate(normalized):
            high=float(candle['high']); low=float(candle['low'])
            favorable.append(((high-fill)*sign)/risk if sign>0 else ((fill-low)/risk))
            adverse.append(((fill-low)/risk) if sign>0 else ((high-fill)/risk))
            sl,tp=_hit(r['side'],high,low,stop,target)
            if sl or tp:
                terminal=(index,candle,sl,tp)
                break

        if terminal is None:
            if input_errors:
                _exec(conn,"UPDATE burnin_pending_position_outcomes SET evidence_complete=0,missing_fields_json=:mf WHERE trade_id=:tid AND status='OPEN'",{
                    'mf':json.dumps(input_errors,sort_keys=True),'tid':r['trade_id']})
            counts['pending']+=1
            continue

        terminal_index,terminal_candle,sl,tp=terminal
        window_row['horizon_bars']=terminal_index+1
        complete,gaps=_window_complete(
            normalized,window_row,terminal_index,input_errors=input_errors)
        if not complete:
            diagnostics=['incomplete_market_window',*input_errors]
            diagnostics += [f"market_gap:{start}->{end}" for start,end in gaps]
            _exec(conn,"UPDATE burnin_pending_position_outcomes SET evidence_complete=0,missing_fields_json=:mf WHERE trade_id=:tid AND status='OPEN'",{
                'mf':json.dumps(sorted(set(diagnostics)),sort_keys=True),'tid':r['trade_id']})
            counts['pending']+=1
            continue

        observed=normalized[:terminal_index+1]
        favorable=[]; adverse=[]
        for candle in observed:
            high=float(candle['high']); low=float(candle['low'])
            favorable.append(((high-fill)*sign)/risk if sign>0 else ((fill-low)/risk))
            adverse.append(((fill-low)/risk) if sign>0 else ((high-fill)/risk))

        try: provenance=json.loads(r.get('source_provenance_json') or '{}')
        except (TypeError,json.JSONDecodeError): provenance={}
        model=provenance.get('execution_cost_model') if isinstance(provenance.get('execution_cost_model'),Mapping) else {}
        risk_usd=abs(fill-stop)*float(r['quantity'])
        def half(name):
            value=model.get(name)
            return None if value is None else float(value)/2.0
        exit_costs={'exit_spread':half('spread_penalty'),'exit_slippage':half('slippage_penalty'),'exit_fee':half('fee_penalty'),'funding':model.get('funding_penalty'),'latency_impact_penalty':model.get('latency_penalty'),'volatility_penalty':model.get('volatility_penalty'),'liquidity_penalty':model.get('liquidity_penalty')}
        if provenance.get('execution_cost_model_unit') == 'R' and provenance.get('execution_cost_unit') == 'USD':
            exit_costs={key: None if value is None else float(value)*risk_usd for key,value in exit_costs.items()}
        ts=_dt(terminal_candle['timestamp']); ambiguous=bool(sl and tp); reason='AMBIGUOUS_INTRABAR' if ambiguous else ('SL_HIT' if sl else 'TP_HIT'); price=stop if sl else target
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
def evaluate_forward_outcome(*, side: str, entry: Any, stop: Any, target: Any,
                             decision_timestamp: str, due_at: str,
                             timeframe: str | None, horizon_bars: int | None,
                             candles: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Pure canonical TP/SL/timeout evaluator; persistence belongs to callers."""
    errors = _geometry_errors(side, entry, stop, target)
    if errors:
        return {"forward_label": None, "mfe": None, "mae": None,
                "gross_r": None, "ambiguous": False, "evidence_complete": False,
                "window_complete": False,
                "terminal_index": None,
                "market_gaps": [], "missing_fields": errors, "observed_bars": 0}
    row = {"side": side, "entry": float(entry), "stop": float(stop),
           "target": float(target), "decision_timestamp": decision_timestamp,
           "due_at": due_at, "timeframe": timeframe, "horizon_bars": horizon_bars}
    normalized, input_errors = _normalize_candle_window(candles, row)
    label = "TIMEOUT"
    ambiguous = False
    gross = 0.0
    terminal = None
    sign = -1 if _side(side) == "SHORT" else 1
    for index, candle in enumerate(normalized):
        sl_hit, tp_hit = _hit(side, float(candle["high"]), float(candle["low"]),
                              float(stop), float(target))
        if sl_hit and tp_hit:
            label, ambiguous, gross, terminal = "AMBIGUOUS", True, None, index
            break
        if tp_hit:
            label = "TP_BEFORE_SL"
            gross = abs((float(target) - float(entry)) / (float(entry) - float(stop)))
            terminal = index
            break
        if sl_hit:
            label, gross, terminal = "SL_BEFORE_TP", -1.0, index
            break
    observed = normalized if terminal is None else normalized[:terminal + 1]
    favorable = [((float(c["high"])-float(entry))/float(entry) if sign > 0 else (float(entry)-float(c["low"]))/float(entry))*100 for c in observed]
    adverse = [((float(entry)-float(c["low"]))/float(entry) if sign > 0 else (float(c["high"])-float(entry))/float(entry))*100 for c in observed]
    complete, gaps = _window_complete(normalized, row, terminal, input_errors=input_errors)
    return {"forward_label": label, "mfe": max(favorable, default=0.0),
            "mae": max(adverse, default=0.0), "gross_r": gross,
            "ambiguous": ambiguous, "evidence_complete": bool(complete and not ambiguous),
            "window_complete": complete,
            "terminal_index": terminal,
            "market_gaps": gaps, "missing_fields": input_errors, "observed_bars": len(observed)}
