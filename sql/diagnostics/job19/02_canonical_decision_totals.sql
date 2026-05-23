SELECT decision, COUNT(*) AS total_rows
FROM order_decisions
WHERE mode = 'PAPER' AND COALESCE(phase, 'final') = 'final'
GROUP BY decision;
