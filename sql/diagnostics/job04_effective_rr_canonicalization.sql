-- JOB-04 Effective RR Canonicalization Diagnostics

-- =====================================================
-- 1. raw_rr vs effective_rr variability
-- =====================================================
SELECT
    mode,
    COUNT(*) AS rows,
    MIN(rr) AS min_raw_rr,
    MAX(rr) AS max_raw_rr,
    MIN(effective_rr) AS min_effective_rr,
    MAX(effective_rr) AS max_effective_rr,
    COUNT(DISTINCT ROUND(rr, 4)) AS distinct_raw_rr,
    COUNT(DISTINCT ROUND(effective_rr, 4)) AS distinct_effective_rr
FROM order_decisions
WHERE COALESCE(phase, 'final') = 'final'
GROUP BY mode
ORDER BY mode;

-- =====================================================
-- 2. Suspicious raw_rr fallback cases
-- =====================================================
SELECT
    decision_id,
    signal_id,
    symbol,
    mode,
    rr,
    effective_rr,
    spread_pct,
    expected_slippage_pct,
    latency_ms,
    funding_rate_pct,
    volatility_regime
FROM order_decisions
WHERE COALESCE(phase, 'final') = 'final'
  AND rr IS NOT NULL
  AND effective_rr IS NOT NULL
  AND ABS(CAST(rr AS REAL) - CAST(effective_rr AS REAL)) < 0.0000001
  AND (
      COALESCE(CAST(spread_pct AS REAL), 0.0) > 0.0
      OR COALESCE(CAST(expected_slippage_pct AS REAL), 0.0) > 0.0
      OR COALESCE(CAST(latency_ms AS REAL), 0.0) > 0.0
      OR COALESCE(CAST(funding_rate_pct AS REAL), 0.0) != 0.0
  )
ORDER BY mode, symbol, signal_id;

-- =====================================================
-- 3. Effective RR below survival threshold
-- =====================================================
SELECT
    mode,
    COUNT(*) AS low_effective_rr_rows,
    ROUND(AVG(effective_rr), 6) AS avg_effective_rr
FROM order_decisions
WHERE COALESCE(phase, 'final') = 'final'
  AND effective_rr < 1.1
GROUP BY mode
ORDER BY mode;

-- =====================================================
-- 4. Execution context completeness by RR bucket
-- =====================================================
SELECT
    mode,
    CASE
        WHEN effective_rr >= 2.0 THEN 'HIGH'
        WHEN effective_rr >= 1.1 THEN 'MEDIUM'
        ELSE 'LOW'
    END AS effective_rr_bucket,
    COUNT(*) AS rows,
    SUM(CASE WHEN execution_ctx_missing = 1 THEN 1 ELSE 0 END) AS execution_ctx_missing_rows
FROM trade_lifecycle_events
GROUP BY mode, effective_rr_bucket
ORDER BY mode, effective_rr_bucket;
