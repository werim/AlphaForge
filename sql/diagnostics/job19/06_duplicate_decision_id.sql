SELECT decision_id,
       COUNT(*) AS duplicate_rows
FROM order_decisions
WHERE mode = 'PAPER'
  AND decision_id IS NOT NULL
  AND TRIM(decision_id) <> ''
GROUP BY decision_id
HAVING COUNT(*) > 1
ORDER BY duplicate_rows DESC, decision_id;
