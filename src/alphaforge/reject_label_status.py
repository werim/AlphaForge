"""Read-only, SQL-first integrity report for PAPER reject forward labels."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

REQUIRED = {
    "burnin_observations": {"observation_id", "burnin_run_id", "execution_mode", "decision", "evidence_complete", "missing_fields_json", "metrics_json"},
    "rejected_signal_reviews": {"reject_decision_id", "signal_id", "reject_reason", "raw_rr", "effective_rr", "reject_correct", "execution_invalidated", "outcome_ambiguous", "evidence_complete", "max_favorable_excursion_pct", "max_adverse_excursion_pct"},
    "burnin_pending_reject_labels": {"pending_label_id", "campaign_id", "burnin_run_id", "reject_decision_id", "signal_id", "status", "due_at", "claimed_at", "timeframe", "horizon_bars", "horizon_seconds", "entry", "stop", "target", "execution_cost_assumptions_json"},
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

    # Run membership is the authoritative scope.  Campaigns include only their
    # explicit continuation lineage; standalone validation includes one run.
    if identity.startswith("standalone:"):
        run_ids = [identity.removeprefix("standalone:")]
    elif "burnin_campaign_runs" in tables:
        run_ids = [r[0] for r in conn.execute(
            "SELECT burnin_run_id FROM burnin_campaign_runs WHERE campaign_id=?", (identity,))]
    else:
        run_ids = []
    run_ph = ",".join("?" for _ in run_ids) or "NULL"
    p = [dict(r) for r in conn.execute(
        f"SELECT * FROM burnin_pending_reject_labels WHERE burnin_run_id IN ({run_ph}) AND campaign_id=?",
        [*run_ids, identity])]
    observations = [dict(r) for r in conn.execute(
        f"""SELECT * FROM burnin_observations WHERE burnin_run_id IN ({run_ph})
        AND UPPER(COALESCE(execution_mode,''))='PAPER'
        AND UPPER(COALESCE(decision,''))='REJECTED'""", run_ids)]

    def decoded(value: Any, fallback: Any) -> Any:
        try:
            result = json.loads(value or "")
            return result
        except (TypeError, ValueError):
            return fallback

    observation_ids = []
    legacy_unattributed_observations = 0
    incomplete_observation_ids: set[str] = set()
    for row in observations:
        metrics = decoded(row.get("metrics_json"), {})
        reject_id = metrics.get("reject_decision_id") if isinstance(metrics, dict) else None
        if reject_id:
            observation_ids.append(str(reject_id))
            missing_fields = decoded(row.get("missing_fields_json"), [])
            if (str(row.get("observation_id") or "").startswith("incomplete_reject_geometry_") or
                    row.get("evidence_complete") != 1 or bool(missing_fields)):
                incomplete_observation_ids.add(str(reject_id))
        else:
            legacy_unattributed_observations += 1

    decision_ids = sorted(set(observation_ids) | {r["reject_decision_id"] for r in p})
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
    outcomes_by_decision: dict[str, list[dict[str, Any]]] = {}
    for outcome in canonical:
        outcomes_by_decision.setdefault(outcome["reject_outcome_id"].removeprefix("rout_"), []).append(outcome)
    outcome_by_decision = {rid: rows[0] for rid, rows in outcomes_by_decision.items() if len(rows) == 1}

    review_ids = [r["reject_decision_id"] for r in reviews if r["reject_decision_id"] is not None]
    duplicate_review_ids = sum(n - 1 for n in {x: review_ids.count(x) for x in set(review_ids)}.values() if n > 1)
    payload_pending = []
    for outcome in outcomes:
        try: payload_pending.append(json.loads(outcome.get("payload_json") or "{}").get("pending_label_id"))
        except (TypeError, ValueError): payload_pending.append(None)
    duplicate_outcomes = sum(n - 1 for n in {x: payload_pending.count(x) for x in set(payload_pending) if x}.values() if n > 1)
    orphan_reviews = sum(1 for r in reviews if int(r["id"]) not in matched_review_rows and
                         str(r.get("reject_decision_id") or "") not in incomplete_observation_ids)
    orphan_pending = sum(1 for r in p if r["reject_decision_id"] not in review_by_pending and
                         r["reject_decision_id"] not in ambiguous_review_links)
    orphan_outcomes = sum(1 for o in outcomes if o["reject_outcome_id"] not in expected_outcome)
    states = {s: sum(1 for r in p if str(r["status"]).upper() == s) for s in STATUSES}
    stale = [r for r in p if str(r["status"]).upper() == "RESOLVING" and
             (_iso(r["claimed_at"]) is None or (generated - _iso(r["claimed_at"])).total_seconds() > stale_claim_seconds)]
    unresolved = [r for r in p if str(r["status"]).upper() not in {"RESOLVED", "AMBIGUOUS", "FAILED"}]
    immature = [r for r in p if str(r["status"]).upper() in {"PENDING", "READY", "RESOLVING"}]
    overdue = [r for r in unresolved if _iso(r["due_at"]) and _iso(r["due_at"]) < generated]
    ages = [(generated - dt).total_seconds() for r in unresolved if (dt := _iso(r.get("created_at") or r.get("decision_timestamp")))]
    resolved_times = [_iso(r.get("resolved_at")) for r in p if str(r["status"]).upper() == "RESOLVED"]

    invalid_correct = []
    missing_excursions = []
    resolved_without = []
    immutable = []
    geometry = []
    missing_cost_ids: set[str] = set()
    state_inconsistencies: list[str] = []
    for row in p:
        rid = row["reject_decision_id"]; review = review_by_pending.get(rid); outcome = outcome_by_decision.get(rid)
        outcome_rows = outcomes_by_decision.get(rid, [])
        pending_status = str(row.get("status") or "").upper()
        if pending_status not in STATUSES:
            state_inconsistencies.append(rid + ":UNKNOWN_STATUS")
        elif pending_status == "RESOLVED" and row.get("evidence_complete") != 1:
            state_inconsistencies.append(rid + ":RESOLVED_PENDING_INCOMPLETE")
        elif pending_status != "RESOLVED" and row.get("evidence_complete") == 1:
            state_inconsistencies.append(rid + ":NON_RESOLVED_PENDING_COMPLETE")
        elif pending_status in {"PENDING", "READY", "RESOLVING"} and outcome_rows:
            state_inconsistencies.append(rid + ":UNRESOLVED_WITH_OUTCOME")
        elif pending_status == "RESOLVED" and (len(outcome_rows) != 1 or
                outcome_rows[0].get("evidence_complete") != 1 or
                outcome_rows[0].get("ambiguous") == 1 or
                outcome_rows[0].get("execution_invalidated") == 1):
            state_inconsistencies.append(rid + ":INVALID_RESOLVED_OUTCOME")
        elif pending_status == "AMBIGUOUS" and (len(outcome_rows) != 1 or
                outcome_rows[0].get("ambiguous") != 1):
            state_inconsistencies.append(rid + ":INVALID_AMBIGUOUS_OUTCOME")
        elif pending_status == "FAILED" and any(o.get("evidence_complete") == 1 for o in outcome_rows):
            state_inconsistencies.append(rid + ":FAILED_WITH_COMPLETE_OUTCOME")
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
        costs = decoded(row.get("execution_cost_assumptions_json"), {})
        required_costs = ("spread_cost", "entry_slippage_cost", "exit_slippage_cost",
                          "fee_cost", "funding_cost", "latency_cost")
        if not isinstance(costs, dict) or any(costs.get(key) is None for key in required_costs):
            missing_cost_ids.add(rid)

    review_decision_ids = {str(r["reject_decision_id"]) for r in reviews if r.get("reject_decision_id")}
    denominator_ids = set(decision_ids) | review_decision_ids
    pending_counts = {rid: sum(row["reject_decision_id"] == rid for row in p) for rid in denominator_ids}
    # An incomplete-geometry audit row is intentionally part of the reject
    # population, but cannot be label eligible.  All other scoped rejects are
    # expected to own one pending identity; absence is a structural failure.
    label_eligible_ids = denominator_ids - incomplete_observation_ids - missing_cost_ids
    unlabeled_ids = {rid for rid in label_eligible_ids if pending_counts.get(rid, 0) == 0}
    duplicate_pending_ids = {rid for rid, count in pending_counts.items() if count > 1}
    resolved_count = sum(str(row["status"]).upper() == "RESOLVED" for row in p)
    failed_count = sum(str(row["status"]).upper() == "FAILED" for row in p)
    ambiguous_count = sum(str(row["status"]).upper() == "AMBIGUOUS" for row in p)
    execution_invalidated_count = sum(
        review.get("execution_invalidated") == 1 for review in review_by_pending.values())
    accuracy_eligible_count = sum(
        review["evidence_complete"] == 1 and review["execution_invalidated"] != 1 and
        review["outcome_ambiguous"] != 1 and review["reject_correct"] is not None
        for review in review_by_pending.values())
    total_rejected = len(denominator_ids)
    coverage = {
        "total_rejected_decisions": total_rejected,
        "reviews_count": len(reviews),
        "label_eligible_rejects": len(label_eligible_ids),
        "pending_labels": len(p),
        "unlabeled_rejects": len(unlabeled_ids),
        "incomplete_geometry_rejects": len(incomplete_observation_ids),
        "missing_execution_cost_rejects": len(missing_cost_ids),
        "resolved_labels": resolved_count,
        "failed_labels": failed_count,
        "ambiguous_labels": ambiguous_count,
        "execution_invalidated_labels": execution_invalidated_count,
        "accuracy_eligible_labels": accuracy_eligible_count,
        "label_coverage_ratio": (sum(pending_counts.get(rid, 0) == 1 for rid in label_eligible_ids) /
                                 len(label_eligible_ids)) if label_eligible_ids else None,
        "mature_coverage_ratio": (accuracy_eligible_count / len(label_eligible_ids))
                                 if label_eligible_ids else None,
        "legacy_unattributed_observations": legacy_unattributed_observations,
    }

    reasons: set[str] = set()
    if duplicate_review_ids or duplicate_outcomes or ambiguous_review_links: reasons.add("DUPLICATE_REJECT_IDENTITY")
    if legacy_unattributed_observations: reasons.add("LEGACY_UNATTRIBUTED_REJECT_EVIDENCE")
    if ambiguous_review_links: reasons.add("AMBIGUOUS_REVIEW_LINKAGE")
    if orphan_reviews: reasons.add("ORPHAN_REJECT_REVIEW")
    if orphan_pending: reasons.add("ORPHAN_PENDING_LABEL")
    if orphan_outcomes: reasons.add("ORPHAN_REJECT_OUTCOME")
    if invalid_correct: reasons.add("INVALID_REJECT_CORRECT_LABEL")
    if missing_excursions or immutable or geometry: reasons.add("INVALID_FINALIZED_EVIDENCE")
    if stale: reasons.add("STALE_RESOLVER_CLAIM")
    if overdue: reasons.add("OVERDUE_PENDING_LABELS")
    if resolved_without: reasons.add("RESOLVED_WITHOUT_OUTCOME")
    if state_inconsistencies: reasons.add("PENDING_OUTCOME_STATE_INCONSISTENCY")
    if immature: reasons.add("IMMATURE_LABELS_PRESENT")
    if unlabeled_ids: reasons.add("MISSING_ELIGIBLE_PENDING_LABEL")
    if duplicate_pending_ids: reasons.add("DUPLICATE_PENDING_LABEL_OWNERSHIP")
    if incomplete_observation_ids: reasons.add("INCOMPLETE_REJECT_GEOMETRY")
    if missing_cost_ids: reasons.add("MISSING_EXECUTION_COST_EVIDENCE")
    if failed_count: reasons.add("FAILED_LABELS_PRESENT")
    if ambiguous_count: reasons.add("AMBIGUOUS_LABELS_PRESENT")
    if execution_invalidated_count: reasons.add("EXECUTION_INVALIDATED_LABELS_PRESENT")
    if not canonical: reasons.add("NO_FORWARD_OUTCOMES_YET")
    eligible = [r for r in review_by_pending.values() if r["evidence_complete"] == 1 and r["execution_invalidated"] != 1 and r["outcome_ambiguous"] != 1 and r["reject_correct"] is not None]
    if not eligible: reasons.add("INSUFFICIENT_MATURE_EVIDENCE")
    if coverage["mature_coverage_ratio"] is not None and coverage["mature_coverage_ratio"] < 1.0:
        reasons.add("INCOMPLETE_MATURE_COVERAGE")
    failures = {"DUPLICATE_REJECT_IDENTITY", "AMBIGUOUS_REVIEW_LINKAGE", "ORPHAN_REJECT_REVIEW", "ORPHAN_PENDING_LABEL", "ORPHAN_REJECT_OUTCOME", "INVALID_REJECT_CORRECT_LABEL", "INVALID_FINALIZED_EVIDENCE", "RESOLVED_WITHOUT_OUTCOME", "MISSING_ELIGIBLE_PENDING_LABEL", "DUPLICATE_PENDING_LABEL_OWNERSHIP", "PENDING_OUTCOME_STATE_INCONSISTENCY"}
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
        "coverage": coverage,
        "integrity": {"rejected_reviews": len(reviews), "distinct_reject_decision_ids": len(set(r["reject_decision_id"] for r in reviews if r["reject_decision_id"] is not None)), "duplicate_reject_decision_ids": duplicate_review_ids,
            "pending_labels": len(p), "reject_outcomes": len(canonical), "reviews_without_eligible_pending_labels": orphan_reviews,
            "pending_labels_without_reviews": orphan_pending, "ambiguous_review_linkages": len(ambiguous_review_links),
            "outcomes_without_pending_labels": orphan_outcomes, "duplicate_canonical_outcomes": duplicate_outcomes,
            "pending_outcome_state_inconsistencies": len(state_inconsistencies)},
        "resolver_state": {**states, "stale_resolving_claims": len(stale), "overdue_pending_labels": len(overdue),
            "oldest_unresolved_label_age_seconds": max(ages) if ages else None,
            "latest_successful_resolution_timestamp": max((x for x in resolved_times if x), default=None)},
        "evidence_correctness": {"invalid_reject_correct_labels": len(invalid_correct), "finalized_evidence_missing_mfe_mae": len(missing_excursions),
            "resolved_without_canonical_outcome": len(resolved_without), "immutable_canonical_outcome_inconsistencies": len(immutable), "invalid_forward_geometry": len(geometry)},
        "reject_quality": quality}
