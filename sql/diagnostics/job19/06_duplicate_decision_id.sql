SELECT decision_id, COUNT(*) AS row_count
FROM order_decisions
WHERE mode = 'PAPER' AND decision_id IS NOT NULL
GROUP BY decision_id
HAVING COUNT(*) > 1;
