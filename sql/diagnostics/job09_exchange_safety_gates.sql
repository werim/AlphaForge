-- JOB-09 Exchange Safety Gates Diagnostics
-- Execution measurements are recorded on order_decisions. Lifecycle is queried
-- separately for evidence integrity rather than assumed to carry spread fields.

-- 0. Evidence schema inventory
PRAGMA table_info(order_decisions);
PRAGMA table_info(trade_lifecycle_events);

-- 1. Execution-context availability on persisted decisions
SELECT
    mode,
    COUNT(*) AS decision_rows,
    SUM(CASE WHEN COALESCE(execution_ctx_missing, 0) = 1 THEN 1 ELSE 0 END) AS execution_ctx_missing_rows,
    SUM(CASE WHEN execution_ctx IS NULL OR TRIM(execution_ctx) IN ('', '{}') THEN 1 ELSE 0 END) AS empty_execution_ctx_payload_rows
FROM order_decisions
GROUP BY mode
ORDER BY mode;

-- 2. Stale or dangerous persisted decision context
SELECT
    signal_id,
    symbol,
    mode,
    decision,
    reject_reason,
    spread_pct,
    funding_rate_pct,
    latency_ms,
    execution_ctx_missing
FROM order_decisions
WHERE
    COALESCE(spread_pct, 0.0) > 0.0025
    OR ABS(COALESCE(funding_rate_pct, 0.0)) > 0.0010
    OR COALESCE(latency_ms, 0.0) > 750
ORDER BY latency_ms DESC, spread_pct DESC;

-- 3. Suspicious optimistic execution rows
SELECT
    decision_id,
    signal_id,
    symbol,
    mode,
    decision,
    reject_reason,
    rr,
    effective_rr,
    spread_pct,
    expected_slippage_pct,
    latency_ms
FROM order_decisions
WHERE effective_rr >= 2.0
  AND (
      COALESCE(spread_pct, 0.0) > 0.0025
      OR COALESCE(latency_ms, 0.0) > 750
      OR COALESCE(expected_slippage_pct, 0.0) > 0.0
  )
ORDER BY effective_rr DESC, latency_ms DESC;

-- 4. Reject reason concentration
SELECT
    mode,
    COALESCE(NULLIF(TRIM(reject_reason), ''), 'MISSING') AS reject_reason,
    COUNT(*) AS rows
FROM order_decisions
WHERE decision = 'REJECTED'
GROUP BY mode, COALESCE(NULLIF(TRIM(reject_reason), ''), 'MISSING')
ORDER BY rows DESC;

-- 5. Lifecycle ERROR concentration: safety evidence cannot be called healthy while state evidence is corrupted
SELECT
    mode,
    COUNT(*) AS lifecycle_rows,
    SUM(CASE WHEN lifecycle_state = 'ERROR' THEN 1 ELSE 0 END) AS lifecycle_error_rows,
    ROUND(
        CAST(SUM(CASE WHEN lifecycle_state = 'ERROR' THEN 1 ELSE 0 END) AS REAL)
        / NULLIF(COUNT(*), 0),
        6
    ) AS lifecycle_error_ratio
FROM trade_lifecycle_events
GROUP BY mode
ORDER BY mode;
