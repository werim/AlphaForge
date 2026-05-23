SELECT execution_ctx_missing, COUNT(*) AS total_rows
FROM order_decisions
WHERE mode = 'PAPER'
GROUP BY execution_ctx_missing;
