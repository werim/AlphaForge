-- JOB-09 Exchange Safety Gates Diagnostics

-- =====================================================
-- 1. Exchange connectivity evidence
-- =====================================================
SELECT
    mode,
    COUNT(*) AS rows,
    SUM(CASE WHEN execution_ctx_missing = 1 THEN 1 ELSE 0 END) AS execution_ctx_missing_rows
FROM trade_lifecycle_events
GROUP BY mode
ORDER BY mode;

-- =====================================================
-- 2. Stale or dangerous market context
-- =====================================================
SELECT
    signal_id,
    symbol,
    mode,
    lifecycle_state,
    spread_pct,
    funding_rate_pct,
    latency_ms,
    execution_ctx_missing
FROM trade_lifecycle_events
WHERE
    spread_pct > 0.0025
    OR ABS(COALESCE(funding_rate_pct, 0)) > 0.0010
    OR latency_ms > 750
ORDER BY latency_ms DESC;

-- =====================================================
-- 3. Suspicious optimistic execution rows
-- =====================================================
SELECT
    signal_id,
    symbol,
    effective_rr,
    spread_pct,
    expected_slippage_pct,
    latency_ms
FROM trade_lifecycle_events
WHERE effective_rr >= 2.0
  AND (
      spread_pct > 0.0025
      OR latency_ms > 750
  )
ORDER BY effective_rr DESC;

-- =====================================================
-- 4. Reject reason concentration
-- =====================================================
SELECT
    reject_reason,
    COUNT(*) AS rows
FROM order_decisions
WHERE decision='REJECTED'
GROUP BY reject_reason
ORDER BY rows DESC;
