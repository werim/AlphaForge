SELECT decision_id, signal_id, symbol, reject_reason, score, rr, effective_rr, created_at
FROM order_decisions
WHERE mode = 'PAPER' AND decision = 'REJECTED';
