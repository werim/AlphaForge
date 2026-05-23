SELECT signal_id,
       COUNT(*) AS final_rows,
       COUNT(DISTINCT decision) AS decision_labels
FROM order_decisions
WHERE mode = 'PAPER'
  AND COALESCE(phase, 'final') = 'final'
  AND signal_id IS NOT NULL
GROUP BY signal_id
HAVING COUNT(*) > 1 OR COUNT(DISTINCT decision) > 1;
