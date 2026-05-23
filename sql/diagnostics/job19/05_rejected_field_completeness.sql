SELECT COUNT(*) AS rejected_rows,
       SUM(CASE WHEN decision_id IS NULL OR TRIM(decision_id) = '' THEN 1 ELSE 0 END) AS missing_decision_id,
       SUM(CASE WHEN signal_id IS NULL OR TRIM(signal_id) = '' THEN 1 ELSE 0 END) AS missing_signal_id,
       SUM(CASE WHEN symbol IS NULL OR TRIM(symbol) = '' THEN 1 ELSE 0 END) AS missing_symbol,
       SUM(CASE WHEN mode IS NULL OR TRIM(mode) = '' THEN 1 ELSE 0 END) AS missing_mode,
       SUM(CASE WHEN reject_reason IS NULL OR TRIM(reject_reason) = '' OR UPPER(TRIM(reject_reason)) = 'UNKNOWN' THEN 1 ELSE 0 END) AS missing_or_unknown_reason,
       SUM(CASE WHEN score IS NULL THEN 1 ELSE 0 END) AS missing_score,
       SUM(CASE WHEN rr IS NULL THEN 1 ELSE 0 END) AS missing_rr,
       SUM(CASE WHEN effective_rr IS NULL THEN 1 ELSE 0 END) AS missing_effective_rr,
       SUM(CASE WHEN expectancy_bucket IS NULL OR TRIM(expectancy_bucket) = '' OR UPPER(TRIM(expectancy_bucket)) = 'UNKNOWN' THEN 1 ELSE 0 END) AS missing_or_unknown_expectancy_bucket,
       SUM(CASE WHEN created_at IS NULL OR TRIM(created_at) = '' THEN 1 ELSE 0 END) AS missing_created_at
FROM order_decisions
WHERE mode = 'PAPER' AND decision = 'REJECTED';
