SELECT lifecycle_state, COUNT(*) AS total_rows
FROM trade_lifecycle_events
WHERE mode = 'PAPER'
GROUP BY lifecycle_state;
