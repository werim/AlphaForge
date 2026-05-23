WITH rejected_final AS (
  SELECT signal_id, symbol, decision_id, reject_reason, created_at
  FROM order_decisions
  WHERE mode = 'PAPER'
    AND COALESCE(NULLIF(TRIM(phase), ''), 'final') = 'final'
    AND decision = 'REJECTED'
), rejection_events AS (
  SELECT signal_id
  FROM trade_lifecycle_events
  WHERE mode = 'PAPER'
    AND lifecycle_state IN ('SIGNAL_REJECTED', 'ORDER_REJECTED')
)
SELECT d.signal_id, d.symbol, d.decision_id, d.reject_reason
FROM rejected_final AS d
LEFT JOIN rejection_events AS e ON e.signal_id = d.signal_id
WHERE e.signal_id IS NULL
ORDER BY d.created_at, d.signal_id;
