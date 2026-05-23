SELECT COUNT(*) AS total_final_decisions,
       SUM(decision = 'ACCEPTED') AS accepted_count,
       SUM(decision = 'REJECTED') AS rejected_count,
       ROUND(100.0 * SUM(decision = 'REJECTED') / NULLIF(COUNT(*), 0), 2) AS rejection_rate_pct
FROM order_decisions
WHERE mode = 'PAPER'
  AND COALESCE(phase, 'final') = 'final';
