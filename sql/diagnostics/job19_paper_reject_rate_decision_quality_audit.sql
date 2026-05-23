SELECT COUNT(*) AS paper_decision_rows,
       COUNT(DISTINCT signal_id) AS distinct_signals,
       MIN(created_at) AS first_decision_ts,
       MAX(created_at) AS last_decision_ts
FROM order_decisions
WHERE mode = 'PAPER';
