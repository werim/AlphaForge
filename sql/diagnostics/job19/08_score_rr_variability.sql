SELECT score, rr, effective_rr, COUNT(*) AS row_count
FROM order_decisions
WHERE mode = 'PAPER'
  AND COALESCE(phase, 'final') = 'final'
GROUP BY score, rr, effective_rr;
