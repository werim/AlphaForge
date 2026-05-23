SELECT symbol, COUNT(*) AS total_rows
FROM order_decisions
WHERE mode = 'PAPER'
GROUP BY symbol;
