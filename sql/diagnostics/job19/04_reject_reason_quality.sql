SELECT reject_reason,
       COUNT(*) AS total_rows
FROM order_decisions
WHERE mode = 'PAPER'
GROUP BY reject_reason;
