SELECT COALESCE(NULLIF(TRIM(phase), ''), 'final') AS decision_phase,
       decision,
       COUNT(*) AS total_rows
FROM order_decisions
WHERE mode = 'PAPER'
GROUP BY COALESCE(NULLIF(TRIM(phase), ''), 'final'), decision
ORDER BY total_rows DESC;
