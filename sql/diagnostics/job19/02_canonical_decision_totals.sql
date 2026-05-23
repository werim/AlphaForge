WITH final_paper AS (
  SELECT decision
  FROM order_decisions
  WHERE UPPER(COALESCE(mode, '')) = 'PAPER'
    AND LOWER(COALESCE(NULLIF(TRIM(phase), ''), 'final')) = 'final'
)
SELECT '02_CANONICAL_DECISION_TOTALS' AS audit_section,
       COUNT(*) AS total_final_decisions,
       SUM(CASE WHEN UPPER(COALESCE(decision, '')) IN ('ACCEPTED', 'APPROVED') THEN 1 ELSE 0 END) AS accepted_count,
       SUM(CASE WHEN UPPER(COALESCE(decision, '')) = 'REJECTED' THEN 1 ELSE 0 END) AS rejected_count,
       ROUND(100.0 * SUM(CASE WHEN UPPER(COALESCE(decision, '')) = 'REJECTED' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 2) AS rejection_rate_pct
FROM final_paper;
