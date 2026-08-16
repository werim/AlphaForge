"""Read-only, SQL-first integrity report for PAPER reject forward labels."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

REQUIRED = {
    "rejected_signal_reviews": {"reject_decision_id", "signal_id", "reject_reason", "raw_rr", "effective_rr", "reject_correct", "execution_invalidated", "outcome_ambiguous", "evidence_complete", "max_favorable_excursion_pct", "max_adverse_excursion_pct"},
    "burnin_pending_reject_labels": {"pending_label_id", "campaign_id", "burnin_run_id", "reject_decision_id", "signal_id", "status", "due_at", "claimed_at", "timeframe", "horizon_bars", "horizon_seconds", "entry", "stop", "target"},
    "burnin_reject_outcomes": {"reject_outcome_id", "burnin_run_id", "reject_reason", "evidence_complete", "execution_invalidated", "ambiguous", "hypothetical_net_r_after_costs", "missed_profit", "avoided_loss", "payload_json"},
}
STATUSES = ("PENDING", "READY", "RESOLVING", "RESOLVED", "AMBIGUOUS", "FAILED")


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)
    except ValueError:
        return None


def reject_label_status(conn: sqlite3.Connection, identity: str, *, now: str | None = None,
                        stale_claim_seconds: float = 300.0) -> dict[str, Any]:
    """Validate one canonical campaign/standalone identity without writing."""
    conn.row_factory = sqlite3.Row
    generated = _iso(now) if now else datetime.now(timezone.utc)
    assert generated is not None
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = {table: sorted(cols - {r[1] for r in conn.execute(f"PRAGMA table_info({table})")})
               for table, cols in REQUIRED.items() if table not in tables or
               (cols - {r[1] for r in conn.execute(f"PRAGMA table_info({table})")})}
    base = {"status": "FAIL", "read_only": True, "identity": {"canonical_id": identity,
            "type": "STANDALONE" if identity.startswith("standalone:") else "CAMPAIGN"},
            "generated_at": generated.isoformat().replace("+00:00", "Z")}
    if missing:
        return {**base, "reason_codes": ["SCHEMA_INCOMPLETE"], "schema_limitations": missing,
                "integrity": None, "resolver_state": None, "evidence_correctness": None,
                "reject_quality": []}

    p = [dict(r) for r in conn.execute("SELECT * FROM burnin_pending_reject_labels WHERE campaign_id=?", (identity,))]
    decision_ids = [r["reject_decision_id"] for r in p]
    signal_ids = sorted({r["signal_id"] for r in p if r.get("signal_id") is not None})
    placeholders = ",".join("?" for _ in decision_ids) or "NULL"
    signal_placeholders = ",".join("?" for _ in signal_ids) or "NULL"
    reviews = [dict(r) for r in conn.execute(
        f"""SELECT * FROM rejected_signal_reviews
        WHERE reject_decision_id IN ({placeholders})
           OR (reject_decision_id IS NULL AND signal_id IN ({signal_placeholders}))
           OR (json_valid(payload_json) AND (json_extract(payload_json,'$.campaign_id')=?
               OR json_extract(payload_json,'$.runtime_identity')=?))""",
        [*decision_ids, *signal_ids, identity, identity])]
    run_ids = sorted({r["burnin_run_id"] for r in p})
    run_ph = ",".join("?" for _ in run_ids) or "NULL"
    outcomes = [dict(r) for r in conn.execute(
        f"SELECT * FROM burnin_reject_outcomes WHERE burnin_run_id IN ({run_ph})", run_ids)]
    # Mirror burnin_resolver._sync_review exactly: explicit decision identity
    # wins; signal identity is eligible only on a legacy NULL-decision review.
    # Keep the mapping external and read-only rather than backfilling old rows.
    review_by_pending: dict[str, dict[str, Any]] = {}
    matched_review_rows: set[int] = set()
    ambiguous_review_links: list[str] = []
    for pending in p:
        exact = [r for r in reviews if r["reject_decision_id"] == pending["reject_decision_id"]]
        candidates = exact
        if not exact:
            candidates = [r for r in reviews if r["reject_decision_id"] is None and
                          r.get("signal_id") == pending.get("signal_id")]
        if len(candidates) == 1:
            review_by_pending[pending["reject_decision_id"]] = candidates[0]
            matched_review_rows.add(int(candidates[0]["id"]))
        elif len(candidates) > 1:
            ambiguous_review_links.append(pending["reject_decision_id"])
    review_owners: dict[int, list[str]] = {}
    for reject_id, review in review_by_pending.items():
        review_owners.setdefault(int(review["id"]), []).append(reject_id)
    for owners in review_owners.values():
        if len(owners) > 1:
            ambiguous_review_links.extend(owners)
            for reject_id in owners:
                review_by_pending.pop(reject_id, None)
    ambiguous_review_links = sorted(set(ambiguous_review_links))
    matched_review_rows = {int(review["id"]) for review in review_by_pending.values()}
    expected_outcome = {"rout_" + r["reject_decision_id"]: r for r in p}
    canonical = [o for o in outcomes if o["reject_outcome_id"] in expected_outcome]
    outcome_by_decision = {o["reject_outcome_id"].removeprefix("rout_"): o for o in canonical}

    review_ids = [r["reject_decision_id"] for r in reviews if r["reject_decision_id"] is not None]
    duplicate_review_ids = sum(n - 1 for n in {x: review_ids.count(x) for x in set(review_ids)}.values() if n > 1)
    payload_pending = []
    for outcome in outcomes:
        try: payload_pending.append(json.loads(outcome.get("payload_json") or "{}").get("pending_label_id"))
        except (TypeError, ValueError): payload_pending.append(None)
    duplicate_outcomes = sum(n - 1 for n in {x: payload_pending.count(x) for x in set(payload_pending) if x}.values() if n > 1)
    orphan_reviews = sum(1 for r in reviews if int(r["id"]) not in matched_review_rows)
    orphan_pending = sum(1 for r in p if r["reject_decision_id"] not in review_by_pending and
                         r["reject_decision_id"] not in ambiguous_review_links)
    orphan_outcomes = sum(1 for o in outcomes if o["reject_outcome_id"] not in expected_outcome)
    states = {s: sum(1 for r in p if str(r["status"]).upper() == s) for s in STATUSES}
    stale = [r for r in p if str(r["status"]).upper() == "RESOLVING" and
             (_iso(r["claimed_at"]) is None or (generated - _iso(r["claimed_at"])).total_seconds() > stale_claim_seconds)]
    unresolved = [r for r in p if str(r["status"]).upper() not in {"RESOLVED", "AMBIGUOUS", "FAILED"}]
    overdue = [r for r in unresolved if _iso(r["due_at"]) and _iso(r["due_at"]) < generated]
    ages = [(generated - dt).total_seconds() for r in unresolved if (dt := _iso(r.get("created_at") or r.get("decision_timestamp")))]
    resolved_times = [_iso(r.get("resolved_at")) for r in p if str(r["status"]).upper() == "RESOLVED"]

    invalid_correct = []
    missing_excursions = []
    resolved_without = []
    immutable = []
    geometry = []
    for row in p:
        rid = row["reject_decision_id"]; review = review_by_pending.get(rid); outcome = outcome_by_decision.get(rid)
        if review and review["reject_correct"] is not None and (review["evidence_complete"] != 1 or review["execution_invalidated"] == 1 or review["outcome_ambiguous"] == 1): invalid_correct.append(rid)
        if review and review["evidence_complete"] == 1 and (review["max_favorable_excursion_pct"] is None or review["max_adverse_excursion_pct"] is None): missing_excursions.append(rid)
        if str(row["status"]).upper() == "RESOLVED" and not outcome: resolved_without.append(rid)
        if outcome and review and ((review["evidence_complete"] or 0) != (outcome["evidence_complete"] or 0) or
                                   (review["execution_invalidated"] or 0) != (outcome["execution_invalidated"] or 0) or
                                   (review["outcome_ambiguous"] or 0) != (outcome["ambiguous"] or 0)):
            immutable.append(rid)
        try:
            e, s, t = float(row["entry"]), float(row["stop"]), float(row["target"])
            side = str(row.get("side") or "").upper()
            if e <= 0 or e == s or (side == "LONG" and not s < e < t) or (side == "SHORT" and not t < e < s) or side not in {"LONG", "SHORT"}: geometry.append(rid)
        except (TypeError, ValueError): geometry.append(rid)

    reasons: set[str] = set()
    if duplicate_review_ids or duplicate_outcomes or ambiguous_review_links: reasons.add("DUPLICATE_REJECT_IDENTITY")
    if ambiguous_review_links: reasons.add("AMBIGUOUS_REVIEW_LINKAGE")
    if orphan_reviews: reasons.add("ORPHAN_REJECT_REVIEW")
    if orphan_pending: reasons.add("ORPHAN_PENDING_LABEL")
    if orphan_outcomes: reasons.add("ORPHAN_REJECT_OUTCOME")
    if invalid_correct: reasons.add("INVALID_REJECT_CORRECT_LABEL")
    if missing_excursions or immutable or geometry: reasons.add("INVALID_FINALIZED_EVIDENCE")
    if stale: reasons.add("STALE_RESOLVER_CLAIM")
    if overdue: reasons.add("OVERDUE_PENDING_LABELS")
    if resolved_without: reasons.add("RESOLVED_WITHOUT_OUTCOME")
    if not canonical: reasons.add("NO_FORWARD_OUTCOMES_YET")
    eligible = [r for r in review_by_pending.values() if r["evidence_complete"] == 1 and r["execution_invalidated"] != 1 and r["outcome_ambiguous"] != 1 and r["reject_correct"] is not None]
    if not eligible: reasons.add("INSUFFICIENT_MATURE_EVIDENCE")
    failures = {"DUPLICATE_REJECT_IDENTITY", "AMBIGUOUS_REVIEW_LINKAGE", "ORPHAN_REJECT_REVIEW", "ORPHAN_PENDING_LABEL", "ORPHAN_REJECT_OUTCOME", "INVALID_REJECT_CORRECT_LABEL", "INVALID_FINALIZED_EVIDENCE", "RESOLVED_WITHOUT_OUTCOME"}
    status = "FAIL" if reasons & failures else "INCOMPLETE" if reasons else "PASS"

    quality = []
    for reason in sorted({str(r.get("reject_reason") or "UNKNOWN") for r in p}):
        labels = [r for r in p if str(r.get("reject_reason") or "UNKNOWN") == reason]
        ids = {r["reject_decision_id"] for r in labels}; rs = [review_by_pending[x] for x in ids if x in review_by_pending]
        os = [outcome_by_decision[x] for x in ids if x in outcome_by_decision]
        valid = [r for r in rs if r["evidence_complete"] == 1 and r["execution_invalidated"] != 1 and r["outcome_ambiguous"] != 1 and r["reject_correct"] is not None]
        def avg(rows: list[dict[str, Any]], key: str) -> float | None:
            values = [float(r[key]) for r in rows if r.get(key) is not None]
            return sum(values) / len(values) if values else None
        quality.append({"reject_reason": reason, "rejected_count": len(rs), "pending_count": sum(str(r["status"]).upper() in {"PENDING", "READY", "RESOLVING"} for r in labels),
            "resolved_count": sum(str(r["status"]).upper() == "RESOLVED" for r in labels), "evidence_complete_count": sum(r["evidence_complete"] == 1 for r in rs),
            "execution_invalidated_count": sum(r["execution_invalidated"] == 1 for r in rs), "ambiguous_count": sum(r["outcome_ambiguous"] == 1 for r in rs),
            "reject_correct_count": sum(r["reject_correct"] == 1 for r in valid), "reject_incorrect_count": sum(r["reject_correct"] == 0 for r in valid),
            "reject_accuracy": (sum(r["reject_correct"] == 1 for r in valid) / len(valid)) if valid else None,
            "average_mfe_pct": avg(valid, "max_favorable_excursion_pct"), "average_mae_pct": avg(valid, "max_adverse_excursion_pct"),
            "average_raw_rr": avg(rs, "raw_rr"), "average_effective_rr": avg(rs, "effective_rr"),
            "average_hypothetical_net_r_after_costs": avg([o for o in os if o["evidence_complete"] == 1 and o["execution_invalidated"] != 1 and o["ambiguous"] != 1], "hypothetical_net_r_after_costs"),
            "missed_profitable_rejects": sum((o.get("missed_profit") or 0) > 0 for o in os if o["evidence_complete"] == 1 and o["execution_invalidated"] != 1 and o["ambiguous"] != 1),
            "avoided_losing_trades": sum((o.get("avoided_loss") or 0) > 0 for o in os if o["evidence_complete"] == 1 and o["execution_invalidated"] != 1 and o["ambiguous"] != 1)})

    return {**base, "status": status, "reason_codes": sorted(reasons), "schema_limitations": [],
        "integrity": {"rejected_reviews": len(reviews), "distinct_reject_decision_ids": len(set(r["reject_decision_id"] for r in reviews if r["reject_decision_id"] is not None)), "duplicate_reject_decision_ids": duplicate_review_ids,
            "pending_labels": len(p), "reject_outcomes": len(canonical), "reviews_without_eligible_pending_labels": orphan_reviews,
            "pending_labels_without_reviews": orphan_pending, "ambiguous_review_linkages": len(ambiguous_review_links),
            "outcomes_without_pending_labels": orphan_outcomes, "duplicate_canonical_outcomes": duplicate_outcomes},
        "resolver_state": {**states, "stale_resolving_claims": len(stale), "overdue_pending_labels": len(overdue),
            "oldest_unresolved_label_age_seconds": max(ages) if ages else None,
            "latest_successful_resolution_timestamp": max((x for x in resolved_times if x), default=None)},
        "evidence_correctness": {"invalid_reject_correct_labels": len(invalid_correct), "finalized_evidence_missing_mfe_mae": len(missing_excursions),
            "resolved_without_canonical_outcome": len(resolved_without), "immutable_canonical_outcome_inconsistencies": len(immutable), "invalid_forward_geometry": len(geometry)},
        "reject_quality": quality}
