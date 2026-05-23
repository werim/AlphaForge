SELECT COALESCE(phase, 'final') AS phase, decision, COUNT(*) AS rows_count
FROM order_decisions
WHERE mode = 'PAPER'
GROUP BY COALESCE(phase, 'final'), decision;
