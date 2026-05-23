-- JOB-05 Execution Context Population Diagnostics
-- Execution cost columns are persisted on order_decisions, not trade_lifecycle_events.
-- Lifecycle rows carry the context-missing marker and state evidence only.

-- 0. Evidence surface schema inventory
PRAGMA table_info(order_decisions);
PRAGMA table_info(trade_lifecycle_events);

-- 1. Decision execution context completeness by mode
SELECT
    mode,
    COUNT(*) AS decision_rows,
    SUM(CASE WHEN COALESCE(execution_ctx_missing, 0) = 1 THEN 1 ELSE 0 END) AS ctx_missing_rows,
    SUM(CASE WHEN execution_ctx IS NULL OR TRIM(execution_ctx) IN ('', '{}') THEN 1 ELSE 0 END) AS empty_ctx_payload_rows,
    ROUND(CAST(SUM(CASE WHEN COALESCE(execution_ctx_missing, 0) = 1 THEN 1 ELSE 0 END) AS REAL) / NULLIF(COUNT(*), 0), 6) AS ctx_missing_ratio
FROM order_decisions
GROUP BY mode
ORDER BY mode;

-- 2. Persisted execution measurement coverage by mode
SELECT
    mode,
    COUNT(*) AS decision_rows,
    SUM(CASE WHEN COALESCE(spread_pct, 0.0) = 0.0 THEN 1 ELSE 0 END) AS zero_spread_rows,
    SUM(CASE WHEN COALESCE(expected_slippage_pct, 0.0) = 0.0 THEN 1 ELSE 0 END) AS zero_slippage_rows,
    SUM(CASE WHEN COALESCE(latency_ms, 0.0) = 0.0 THEN 1 ELSE 0 END) AS zero_latency_rows,
    SUM(CASE WHEN volatility_regime IS NULL OR TRIM(volatility_regime) = '' THEN 1 ELSE 0 END) AS missing_volatility_regime_rows,
    COUNT(DISTINCT ROUND(spread_pct, 8)) AS distinct_spread_values,
    COUNT(DISTINCT ROUND(expected_slippage_pct, 8)) AS distinct_slippage_values,
    COUNT(DISTINCT ROUND(latency_ms, 4)) AS distinct_latency_values
FROM order_decisions
GROUP BY mode
ORDER BY mode;

-- 3. Cost-bearing rows where effective RR equals raw RR
SELECT
    mode,
    COUNT(*) AS cost_bearing_equal_rr_rows
FROM order_decisions
WHERE rr IS NOT NULL
  AND effective_rr IS NOT NULL
  AND ABS(CAST(rr AS REAL) - CAST(effective_rr AS REAL)) < 0.0000001
  AND (
      COALESCE(spread_pct, 0.0) > 0.0
      OR COALESCE(expected_slippage_pct, 0.0) > 0.0
      OR COALESCE(latency_ms, 0.0) > 0.0
      OR ABS(COALESCE(funding_rate_pct, 0.0)) > 0.0
  )
GROUP BY mode
ORDER BY mode;

-- 4. Lifecycle context marker and error distribution
SELECT
    mode,
    lifecycle_state,
    COUNT(*) AS lifecycle_rows,
    SUM(CASE WHEN COALESCE(execution_ctx_missing, 0) = 1 THEN 1 ELSE 0 END) AS ctx_missing_rows
FROM trade_lifecycle_events
GROUP BY mode, lifecycle_state
ORDER BY mode, lifecycle_rows DESC, lifecycle_state;
