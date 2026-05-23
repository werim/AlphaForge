SELECT CASE
         WHEN reject_reason IS NULL OR TRIM(reject_reason) = '' THEN 'EMPTY'
         WHEN UPPER(TRIM(reject_reason)) = 'UNKNOWN' THEN 'UNKNOWN'
         ELSE TRIM(reject_reason)
       END AS reject_reason_class,
       COUNT(*) AS rejected_rows
FROM order_decisions
WHERE mode = 'PAPER' AND decision = 'REJECTED'
GROUP BY reject_reason_class
ORDER BY rejected_rows DESC;
