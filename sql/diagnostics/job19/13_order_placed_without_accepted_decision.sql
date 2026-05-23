SELECT e.signal_id, e.symbol, e.order_id, e.created_at
FROM trade_lifecycle_events AS e
LEFT JOIN order_decisions AS d
  ON d.signal_id = e.signal_id
 AND d.mode = 'PAPER'
 AND d.decision = 'ACCEPTED'
 AND COALESCE(d.phase, 'final') = 'final'
WHERE e.mode = 'PAPER'
  AND e.lifecycle_state = 'ORDER_PLACED'
  AND d.signal_id IS NULL;
