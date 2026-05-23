WITH final_paper AS (
  SELECT *
  FROM order_decisions
  WHERE mode = 'PAPER'
    AND COALESCE(NULLIF(TRIM(phase), ''), 'final') = 'final'
)
SELECT COUNT(*) AS canonical_final_decisions,
       SUM(CASE WHEN decision = 'REJECTED' THEN 1 ELSE 0 END) AS rejected_decisions,
       SUM(CASE WHEN decision = 'REJECTED' AND (reject_reason IS NULL OR TRIM(reject_reason) = '' OR UPPER(TRIM(reject_reason)) = 'UNKNOWN') THEN 1 ELSE 0 END) AS rejected_with_bad_reason,
       SUM(CASE WHEN execution_ctx_missing = 1 OR execution_ctx IS NULL OR TRIM(execution_ctx) = '' THEN 1 ELSE 0 END) AS decisions_with_missing_execution_context,
       SUM(CASE WHEN signal_id IS NULL OR TRIM(signal_id) = '' THEN 1 ELSE 0 END) AS decisions_with_missing_signal_id,
       SUM(CASE WHEN effective_rr IS NULL THEN 1 ELSE 0 END) AS decisions_with_missing_effective_rr
FROM final_paper;
