SELECT lifecycle_state, COUNT(*) AS lifecycle_rows
FROM trade_lifecycle_events
WHERE mode = 'PAPER'
GROUP BY lifecycle_state
ORDER BY lifecycle_rows DESC;
