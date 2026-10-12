"""Canonical reject-queue lifecycle predicates for resolution and safety gates."""

# Only a complete, resolved label releases its protected dependencies. Claims,
# ambiguity, failed resolution and unknown state are still outstanding evidence.
UNRESOLVED_REJECT_SQL = "(status IS NULL OR UPPER(status)<>'RESOLVED' OR COALESCE(evidence_complete,0)<>1)"

# An expired claim can be retried, but an unknown claim timestamp cannot safely
# transfer ownership. Both candle collection and persistence use this predicate.
RECLAIMABLE_REJECT_SQL = "(status IN ('PENDING','READY') OR (status='RESOLVING' AND claimed_at<:stale))"
