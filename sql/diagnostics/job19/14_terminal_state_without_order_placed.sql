WITH terminal_events AS (
  SELECT signal_id, symbol, lifecycle_state, created_at
  FROM trade_lifecycle_events
  WHERE mode = 'PAPER'
    AND lifecycle_state IN ('TP_HIT', 'SL_HIT', 'CLOSED', 'CANCELLED', 'ENTRY_TIMEOUT')
), placed_orders AS (
  SELECT signal_id
  FROM trade_lifecycle_events
  WHERE mode = 'PAPER' AND lifecycle_state = 'ORDER_PLACED'
)
SELECT e.signal_id, e.symbol, e.lifecycle_state, e.created_at
FROM terminal_events AS e
LEFT JOIN placed_orders AS p ON p.signal_id = e.signal_id
WHERE p.signal_id IS NULL
ORDER BY e.created_at, e.signal_id;
