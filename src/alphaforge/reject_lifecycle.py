"""Canonical reject-queue lifecycle predicates for resolution and safety gates."""

# Only a complete, resolved label releases its protected dependencies. Claims,
# ambiguity, failed resolution and unknown state are still outstanding evidence.
UNRESOLVED_REJECT_SQL = "(status IS NULL OR UPPER(status)<>'RESOLVED' OR COALESCE(evidence_complete,0)<>1)"

# An expired claim can be retried, but an unknown claim timestamp cannot safely
# transfer ownership. Both candle collection and persistence use this predicate.
RECLAIMABLE_REJECT_SQL = "(status IN ('PENDING','READY') OR (status='RESOLVING' AND claimed_at<:stale))"


AMBIGUITY_MAINTENANCE_EVENT = "PAPER_PRESERVED_AMBIGUITY_MAINTENANCE"


def preserved_ambiguity_evidence(conn, campaign_id, run_id):
    """Validate immutable, complete-window ambiguity without assigning economics.

    This is maintenance evidence only. The normal unresolved predicate above is
    deliberately unchanged. Unknown, failed, claimed or incompletely linked rows
    cannot authorize maintenance.
    """
    import json
    from alphaforge.burnin import canonical_hash, canonical_decision_sql

    campaign = conn.execute("SELECT release_id,active_run_id,source_provenance_json FROM burnin_campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
    if not campaign or campaign[1] != run_id:
        raise ValueError("AMBIGUITY_MAINTENANCE_IDENTITY_UNAVAILABLE")
    provenance = json.loads(campaign[2])
    if provenance.get("storage_retention_mode") != "PAPER_STATS_ONLY" or not provenance.get("storage_policy_hash"):
        raise ValueError("AMBIGUITY_MAINTENANCE_REQUIRES_STATS_ONLY_CAMPAIGN")
    protected = []
    cursor = conn.execute(f"SELECT * FROM burnin_pending_reject_labels WHERE campaign_id=? AND {UNRESOLVED_REJECT_SQL} ORDER BY pending_label_id", (campaign_id,))
    names = [item[0] for item in cursor.description]
    for raw in cursor:
        label = dict(zip(names, raw))
        if (label["burnin_run_id"] != run_id or label["status"] != "AMBIGUOUS"
                or label["evidence_complete"] != 0 or not label["resolved_at"]):
            raise ValueError("NONTERMINAL_OR_UNVERIFIED_AMBIGUITY")
        oc = conn.execute("SELECT * FROM burnin_reject_outcomes WHERE reject_outcome_id=?", ("rout_" + label["reject_decision_id"],))
        outcome_row = oc.fetchone()
        if outcome_row is None:
            raise ValueError("AMBIGUITY_OUTCOME_MISSING")
        outcome = dict(zip([item[0] for item in oc.description], outcome_row))
        payload = json.loads(outcome["payload_json"])
        provenance = payload.get("market_data_provenance") or {}
        identity = {"campaign_id": campaign_id, "burnin_run_id": run_id,
                    "pending_label_id": label["pending_label_id"], "reject_decision_id": label["reject_decision_id"]}
        if (not all(payload.get(k) == v for k, v in identity.items())
                or outcome["burnin_run_id"] != run_id or outcome["release_id"] != campaign[0]
                or outcome["symbol"] != label["symbol"] or outcome["decision_time"] != label["decision_timestamp"]
                or outcome["evidence_horizon"] != label["due_at"]
                or any(outcome[k] != label[v] for k, v in (("hypothetical_entry", "entry"), ("hypothetical_stop", "stop"), ("hypothetical_target", "target")))
                or outcome["forward_label"] != "AMBIGUOUS" or outcome["ambiguous"] != 1
                or outcome["evidence_complete"] != 0 or outcome["execution_invalidated"] != 0
                or any(outcome[k] is not None for k in ("hypothetical_gross_r", "hypothetical_net_r_after_costs", "avoided_loss", "missed_profit"))
                or payload.get("reject_correct") is not None or payload.get("window_complete") is not True
                or payload.get("market_gaps") != [] or payload.get("missing_cost_fields") != []
                or provenance.get("provider") != "BINANCE_READ_ONLY_KLINES"
                or provenance.get("order_submission") != "DISABLED"):
            raise ValueError("AMBIGUITY_EVIDENCE_INVALID")
        dc = conn.execute("SELECT symbol,mode,decision FROM order_decisions WHERE decision_id=?", (label["reject_decision_id"],))
        decision = dc.fetchone()
        if not decision or tuple(decision) != (label["symbol"], "PAPER", "REJECTED"):
            raise ValueError("AMBIGUITY_DECISION_IDENTITY_INVALID")
        cc = conn.execute(f"SELECT * FROM burnin_observations o WHERE burnin_run_id=? AND decision='REJECTED' AND json_extract(metrics_json,'$.reject_decision_id')=? AND (json_extract(metrics_json,'$.campaign_id') IS NULL OR json_extract(metrics_json,'$.campaign_id')=?) AND {canonical_decision_sql('o')}", (run_id, label["reject_decision_id"], campaign_id))
        observations = cc.fetchall()
        if len(observations) != 1:
            raise ValueError("AMBIGUITY_CANONICAL_REJECT_IDENTITY_INVALID")
        observation = dict(zip([item[0] for item in cc.description], observations[0]))
        protected.append({"pending_label_id": label["pending_label_id"], "label": label, "outcome": outcome, "observation": observation, "decision": tuple(decision)})
    if not protected:
        raise ValueError("AMBIGUITY_EVIDENCE_MISSING")
    return {"pending_label_ids": [r["pending_label_id"] for r in protected],
            "evidence_hash": canonical_hash(protected), "preserved_count": len(protected),
            "continuation_allowed": False, "qualification_eligible": False}


def authorized_ambiguity_ids(conn, campaign_id, run_id, *, now=None):
    """Release only verified preserved rows for maintenance of a failed run."""
    import json
    import sqlite3
    import time
    from datetime import datetime
    from alphaforge.burnin import canonical_hash
    from alphaforge.runtime_state import CAMPAIGN_RECONCILIATION_MAX_AGE_SECONDS
    try:
        row = conn.execute("SELECT details_json FROM burnin_campaign_events WHERE campaign_id=? AND burnin_run_id=? AND event_type=? ORDER BY id DESC LIMIT 1", (campaign_id, run_id, AMBIGUITY_MAINTENANCE_EVENT)).fetchone()
        if row is None:
            return ()
        state = conn.execute("SELECT c.campaign_status,r.status,cr.status FROM burnin_campaigns c JOIN burnin_campaign_runs cr ON cr.campaign_id=c.campaign_id JOIN burnin_runs r ON r.burnin_run_id=cr.burnin_run_id WHERE c.campaign_id=? AND c.active_run_id=? AND cr.burnin_run_id=?", (campaign_id, run_id, run_id)).fetchone()
        if tuple(state or ()) != ("FAILED", "FAILED", "FAILED"):
            return ()
        recorded = json.loads(row[0])
        runtime = recorded["runtime_evidence"]
        observed = datetime.fromisoformat(runtime["observed_at"].replace("Z", "+00:00"))
        age = (time.time() if now is None else now) - observed.timestamp()
        if observed.tzinfo is None or not 0 <= age <= CAMPAIGN_RECONCILIATION_MAX_AGE_SECONDS:
            return ()
        sc = conn.execute("SELECT * FROM runtime_state_snapshots WHERE campaign_id=? AND burnin_run_id=? ORDER BY id DESC LIMIT 1", (campaign_id, run_id))
        sr = sc.fetchone()
        snapshot = dict(zip([item[0] for item in sc.description], sr)) if sr else {}
        if (not runtime.get("snapshot") or canonical_hash({key: snapshot.get(key) for key in runtime["snapshot"]}) != runtime["snapshot_hash"]
                or snapshot.get("reconciliation_status") != "CLEAN"
                or any(snapshot.get(key) != 0 for key in ("active_position_count", "pending_order_count", "unknown_exchange_state", "recovery_action_required"))):
            return ()
        current = preserved_ambiguity_evidence(conn, campaign_id, run_id)
        if recorded.get("preserved_ambiguity") != current or recorded.get("campaign_id") != campaign_id or recorded.get("run_id") != run_id:
            return ()
        return tuple(current["pending_label_ids"])
    except (ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        return ()
