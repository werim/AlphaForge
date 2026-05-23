-- JOB-05 Execution Context Population Diagnostics

-- =====================================================
-- 1. Execution context completeness by mode
-- =====================================================
SELECT
    mode,
    COUNT(*) AS lifecycle_rows,
    SUM(CASE WHEN execution_ctx_missing = 1 THEN 1 ELSE 0 END) AS ctx_missing_rows,
    ROUND(
        CAST(SUM(CASE WHEN execution_ctx_missing = 1 THEN 1 ELSE 0 END) AS REAL)
        / NULLIF(COUNT(*), 0),
        6
    ) AS ctx_missing_ratio
FROM trade_lifecycle_events
GROUP BY mode
ORDER BY mode;

-- =====================================================
-- 2. Missing execution fields detail
-- =====================================================
SELECT
    mode,
    SUM(CASE WHEN spread_pct IS NULL OR spread_pct = 0 THEN 1 ELSE 0 END) AS missing_or_zero_spread,
    SUM(CASE WHEN expected_slippage_pct IS NULL OR expected_slippage_pct = 0 THEN 1 ELSE 0 END) AS missing_or_zero_slippage,
    SUM(CASE WHEN latency_ms IS NULL OR latency_ms = 0 THEN 1 ELSE 0 END) AS missing_or_zero_latency,
    SUM(CASE WHEN liquidity_score IS NULL THEN 1 ELSE 0 END) AS missing_liquidity_score,
    SUM(CASE WHEN funding_rate_pct IS NULL THEN 1 ELSE 0 END) AS missing_funding_rate,
    SUM(CASE WHEN orderbook_imbalance IS NULL THEN 1 ELSE 0 END) AS missing_orderbook_imbalance,
    SUM(CASE WHEN volatility_regime IS NULL OR TRIM(volatility_regime) = '' THEN 1 ELSE 0 END) AS missing_volatility_regime
FROM trade_lifecycle_events
GROUP BY mode
ORDER BY mode;

-- =====================================================
-- 3. Placeholder/unavailable marker distribution
-- =====================================================
SELECT
    mode,
    COUNT(*) AS unavailable_rows
FROM trade_lifecycle_events
WHERE
    spread_source IN ('UNKNOWN', 'UNAVAILABLE', 'UNAVAILABLE_BACKTEST')
    OR volatility_regime IN ('UNKNOWN', 'UNAVAILABLE', 'UNAVAILABLE_BACKTEST')
GROUP BY mode
ORDER BY mode;

-- =====================================================
-- 4. Suspicious optimistic rows
-- execution context missing but high effective_rr
-- =====================================================
SELECT
    signal_id,
    symbol,
    mode,
    lifecycle_state,
    effective_rr,
    execution_ctx_missing,
    spread_pct,
    expected_slippage_pct,
    latency_ms,
    liquidity_score,
    funding_rate_pct
FROM trade_lifecycle_events
WHERE execution_ctx_missing = 1
  AND effective_rr >= 2.0
ORDER BY effective_rr DESC
LIMIT 200;

-- =====================================================
-- 5. Execution completeness by lifecycle state
-- =====================================================
SELECT
    mode,
    lifecycle_state,
    COUNT(*) AS rows,
    SUM(CASE WHEN execution_ctx_missing = 1 THEN 1 ELSE 0 END) AS ctx_missing_rows
FROM trade_lifecycle_events
GROUP BY mode, lifecycle_state
ORDER BY mode, lifecycle_state;
