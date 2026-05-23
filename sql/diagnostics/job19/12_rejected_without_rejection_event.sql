SELECT d.signal_id, d.symbol, d.decision_id, d.reject_reason
FROM order_decisions AS d
LEFT JOIN trade_lifecycle_events AS e
  ON e.signal_id = d.signal_id
 AND e.mode = 'PAPER'
 AND e.lifecycle_state = 'SIGNAL_REJECTED'
WHERE d.mode = 'PAPER'
  AND d.decision = 'REJECTED'
  AND COALESCE(d.phase, 'final') = 'final'
  AND e.signal_id IS NULL;
