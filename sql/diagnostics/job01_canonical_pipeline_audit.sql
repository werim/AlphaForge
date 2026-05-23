-- JOB-01 Canonical Pipeline Audit Diagnostics
-- Audit-only. No schema changes.

-- =====================================================
-- 1. Canonical final decision totals and reject rate
-- =====================================================
SELECT
    mode,
    COUNT(*) AS total_final_rows,
    SUM(CASE WHEN decision = 'REJECTED' THEN 1 ELSE 0 END) AS rejected_rows,
    ROUND(
        CAST(SUM(CASE WHEN decision = 'REJECTED' THEN 1 ELSE 0 END) AS REAL)
        / NULLIF(COUNT(*), 0),
        6
    ) AS reject_rate
FROM order_decisions
WHERE COALESCE(phase, 'final') = 'final'
GROUP BY mode
ORDER BY mode;

-- =====================================================
-- 2. Missing audit-critical fields
-- =====================================================
SELECT
    mode,
    SUM(CASE WHEN signal_id IS NULL OR TRIM(signal_id) = '' THEN 1 ELSE 0 END) AS missing_signal_id,
    SUM(CASE WHEN symbol IS NULL OR TRIM(symbol) = '' THEN 1 ELSE 0 END) AS missing_symbol,
    SUM(CASE WHEN decision = 'REJECTED' AND (reject_reason IS NULL OR TRIM(reject_reason) = '') THEN 1 ELSE 0 END) AS missing_reject_reason,
    SUM(CASE WHEN score IS NULL THEN 1 ELSE 0 END) AS missing_score,
    SUM(CASE WHEN rr IS NULL THEN 1 ELSE 0 END) AS missing_rr,
    SUM(CASE WHEN effective_rr IS NULL THEN 1 ELSE 0 END) AS missing_effective_rr
FROM order_decisions
GROUP BY mode
ORDER BY mode;

-- =====================================================
-- 3. Duplicate / inconsistent canonical final decisions
-- =====================================================
SELECT
    signal_id,
    mode,
    COUNT(*) AS final_rows,
    GROUP_CONCAT(DISTINCT decision) AS decisions,
    GROUP_CONCAT(DISTINCT reject_reason) AS reject_reasons
FROM order_decisions
WHERE COALESCE(phase, 'final') = 'final'
GROUP BY signal_id, mode
HAVING COUNT(*) > 1;

-- =====================================================
-- 4. Score / RR variability
-- =====================================================
SELECT
    mode,
    MIN(score) AS min_score,
    MAX(score) AS max_score,
    COUNT(DISTINCT ROUND(score, 4)) AS distinct_score_values,
    MIN(rr) AS min_rr,
    MAX(rr) AS max_rr,
    COUNT(DISTINCT ROUND(rr, 4)) AS distinct_rr_values,
    MIN(effective_rr) AS min_effective_rr,
    MAX(effective_rr) AS max_effective_rr,
    COUNT(DISTINCT ROUND(effective_rr, 4)) AS distinct_effective_rr_values
FROM order_decisions
WHERE COALESCE(phase, 'final') = 'final'
GROUP BY mode
ORDER BY mode;

-- =====================================================
-- 5. Execution context completeness
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
-- 6. Lifecycle state distribution
-- =====================================================
SELECT
    mode,
    lifecycle_state,
    COUNT(*) AS rows
FROM trade_lifecycle_events
GROUP BY mode, lifecycle_state
ORDER BY mode, rows DESC;

-- =====================================================
-- 7. Lifecycle ordering anomalies
-- =====================================================
SELECT
    signal_id,
    mode,
    MIN(lifecycle_seq) AS min_seq,
    MAX(lifecycle_seq) AS max_seq,
    COUNT(*) AS rows,
    GROUP_CONCAT(lifecycle_state, ' -> ') AS state_chain
FROM trade_lifecycle_events
GROUP BY signal_id, mode
HAVING COUNT(*) > 1
   AND (MIN(lifecycle_seq) <= 0 OR MAX(lifecycle_seq) < COUNT(*));

-- =====================================================
-- 8. Canonical final vs AI-internal row split
-- =====================================================
SELECT
    mode,
    COALESCE(phase, 'final') AS phase_class,
    COUNT(*) AS rows
FROM order_decisions
GROUP BY mode, COALESCE(phase, 'final')
ORDER BY mode, phase_class;
