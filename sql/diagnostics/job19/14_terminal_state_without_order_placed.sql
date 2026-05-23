SELECT e.signal_id, e.symbol, e.lifecycle_state, e.created_at
FROM trade_lifecycle_events AS e
LEFT JOIN trade_lifecycle_events AS p
  ON p.signal_id = e.signal_id
 AND p.mode = 'PAPER'
 AND p.lifecycle_state = 'ORDER_PLACED'
WHERE e.mode = 'PAPER'
  AND e.lifecycle_state IN ('TP_HIT', 'SL_HIT', 'CLOSED', 'CANCELLED', 'ENTRY_TIMEOUT')
  AND p.signal_id IS NULL;
