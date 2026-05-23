WITH final_paper AS (
  SELECT signal_id, decision, created_at
  FROM order_decisions
  WHERE mode = 'PAPER'
    AND COALESCE(NULLIF(TRIM(phase), ''), 'final') = 'final'
    AND signal_id IS NOT NULL
    AND TRIM(signal_id) <> ''
)
SELECT signal_id,
       COUNT(*) AS final_rows,
       COUNT(DISTINCT decision) AS distinct_decisions,
       GROUP_CONCAT(DISTINCT decision) AS decisions,
       MIN(created_at) AS first_seen_at,
       MAX(created_at) AS last_seen_at
FROM final_paper
GROUP BY signal_id
HAVING COUNT(*) > 1 OR COUNT(DISTINCT decision) > 1
ORDER BY final_rows DESC, last_seen_at DESC;
