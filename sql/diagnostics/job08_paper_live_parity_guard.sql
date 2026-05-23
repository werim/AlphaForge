-- JOB-08 Paper/Live Parity Guard Diagnostics

-- =====================================================
-- 1. Mode distribution
-- =====================================================
SELECT
    mode,
    COUNT(*) AS rows
FROM order_decisions
GROUP BY mode
ORDER BY mode;

-- =====================================================
-- 2. Final decision parity by signal shape
-- =====================================================
SELECT
    symbol,
    decision,
    COUNT(DISTINCT CASE WHEN mode='PAPER' THEN signal_id END) AS paper_signals,
    COUNT(DISTINCT CASE WHEN mode='LIVE' THEN signal_id END) AS live_signals,
    ROUND(AVG(CASE WHEN mode='PAPER' THEN effective_rr END), 6) AS avg_paper_effective_rr,
    ROUND(AVG(CASE WHEN mode='LIVE' THEN effective_rr END), 6) AS avg_live_effective_rr
FROM order_decisions
WHERE COALESCE(phase, 'final')='final'
GROUP BY symbol, decision
ORDER BY symbol, decision;

-- =====================================================
-- 3. Lifecycle parity coverage
-- =====================================================
SELECT
    lifecycle_state,
    COUNT(CASE WHEN mode='PAPER' THEN 1 END) AS paper_rows,
    COUNT(CASE WHEN mode='LIVE' THEN 1 END) AS live_rows
FROM trade_lifecycle_events
GROUP BY lifecycle_state
ORDER BY lifecycle_state;

-- =====================================================
-- 4. Reject reason divergence between PAPER and LIVE
-- =====================================================
SELECT
    symbol,
    reject_reason,
    COUNT(CASE WHEN mode='PAPER' THEN 1 END) AS paper_rows,
    COUNT(CASE WHEN mode='LIVE' THEN 1 END) AS live_rows
FROM order_decisions
WHERE decision='REJECTED'
  AND COALESCE(phase,'final')='final'
GROUP BY symbol, reject_reason
ORDER BY symbol, reject_reason;

-- =====================================================
-- 5. Suspicious parity drift
-- =====================================================
SELECT
    symbol,
    AVG(CASE WHEN mode='PAPER' THEN effective_rr END) AS paper_effective_rr,
    AVG(CASE WHEN mode='LIVE' THEN effective_rr END) AS live_effective_rr,
    ABS(
        COALESCE(AVG(CASE WHEN mode='PAPER' THEN effective_rr END),0)
        -
        COALESCE(AVG(CASE WHEN mode='LIVE' THEN effective_rr END),0)
    ) AS rr_gap
FROM order_decisions
WHERE COALESCE(phase,'final')='final'
GROUP BY symbol
HAVING rr_gap > 0.75
ORDER BY rr_gap DESC;
