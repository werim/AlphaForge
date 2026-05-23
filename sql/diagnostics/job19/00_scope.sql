WITH paper AS (
  SELECT * FROM order_decisions
  WHERE UPPER(COALESCE(mode, '')) = 'PAPER'
)
SELECT '00_SCOPE' AS audit_section,
       COUNT(*) AS all_layer_rows,
       SUM(CASE WHEN LOWER(COALESCE(NULLIF(TRIM(phase), ''), 'final')) = 'final' THEN 1 ELSE 0 END) AS canonical_final_rows,
       MIN(created_at) AS first_paper_decision_ts,
       MAX(created_at) AS last_paper_decision_ts
FROM paper;
