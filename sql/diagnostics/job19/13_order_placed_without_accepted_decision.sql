WITH placed_orders AS (
  SELECT signal_id, symbol, order_id, created_at
  FROM trade_lifecycle_events
  WHERE mode = 'PAPER' AND lifecycle_state = 'ORDER_PLACED'
), accepted_final AS (
  SELECT signal_id
  FROM order_decisions
  WHERE mode = 'PAPER'
    AND COALESCE(NULLIF(TRIM(phase), ''), 'final') = 'final'
    AND decision IN ('ACCEPTED', 'APPROVED')
)
SELECT e.signal_id, e.symbol, e.order_id, e.created_at
FROM placed_orders AS e
LEFT JOIN accepted_final AS d ON d.signal_id = e.signal_id
WHERE d.signal_id IS NULL
ORDER BY e.created_at, e.signal_id;
