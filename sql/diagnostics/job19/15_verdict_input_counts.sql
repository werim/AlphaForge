SELECT decision, COUNT(*) AS row_total
FROM order_decisions
WHERE mode = 'PAPER'
GROUP BY decision;
