-- JOB-06 Paper Runtime DB Audit Pack
-- Scope: PAPER runtime SQLite artifacts.
-- Usage example:
--   sqlite3 data/alphaforge_runtime.sqlite < sql/diagnostics/job06_paper_runtime_db_audit.sql
--
-- This pack is read-only and does not mutate schema or data.

-- =====================================================
-- 0. PAPER row availability
-- =====================================================
SELECT
    'order_decisions' AS table_name,
    COUNT(*) AS paper_rows
FROM order_decisions
WHERE mode = 'PAPER'
UNION ALL
SELECT
    'trade_lifecycle_events' AS table_name,
    COUNT(*) AS paper_rows
FROM trade_lifecycle_events
WHERE mode = 'PAPER';

-- =====================================================
-- 1. Final decision totals and reject rate
-- =====================================================
SELECT
    COUNT(*) AS final_decision_rows,
    SUM(CASE WHEN decision = 'REJECTED' THEN 1 ELSE 0 END) AS rejected_rows,
    SUM(CASE WHEN decision IN ('ACCEPTED', 'EXECUTED') THEN 1 ELSE 0 END) AS accepted_rows,
    ROUND(
        CAST(SUM(CASE WHEN decision = 'REJECTED' THEN 1 ELSE 0 END) AS REAL)
        / NULLIF(COUNT(*), 0),
        6
    ) AS reject_rate
FROM order_decisions
WHERE mode = 'PAPER'
  AND COALESCE(phase, 'final') = 'final';

-- =====================================================
-- 2. Final vs AI-internal split
-- =====================================================
SELECT
    COALESCE(phase, 'final') AS phase_class,
    decision,
    COUNT(*) AS rows
FROM order_decisions
WHERE mode = 'PAPER'
GROUP BY COALESCE(phase, 'final'), decision
ORDER BY phase_class, decision;

-- =====================================================
-- 3. Reject reason distribution
-- =====================================================
SELECT
    COALESCE(NULLIF(TRIM(reject_reason), ''), 'MISSING') AS reject_reason,
    COUNT(*) AS rows
FROM order_decisions
WHERE mode = 'PAPER'
  AND COALESCE(phase, 'final') = 'final'
  AND decision = 'REJECTED'
GROUP BY COALESCE(NULLIF(TRIM(reject_reason), ''), 'MISSING')
ORDER BY rows DESC, reject_reason;

-- =====================================================
-- 4. Missing audit-critical final decision fields
-- =====================================================
SELECT
    SUM(CASE WHEN signal_id IS NULL OR TRIM(signal_id) = '' THEN 1 ELSE 0 END) AS missing_signal_id,
    SUM(CASE WHEN symbol IS NULL OR TRIM(symbol) = '' THEN 1 ELSE 0 END) AS missing_symbol,
    SUM(CASE WHEN decision = 'REJECTED' AND (reject_reason IS NULL OR TRIM(reject_reason) = '') THEN 1 ELSE 0 END) AS missing_reject_reason,
    SUM(CASE WHEN decision = 'REJECTED' AND UPPER(COALESCE(reject_reason, '')) = 'UNKNOWN' THEN 1 ELSE 0 END) AS unknown_reject_reason,
    SUM(CASE WHEN score IS NULL THEN 1 ELSE 0 END) AS missing_score,
    SUM(CASE WHEN rr IS NULL THEN 1 ELSE 0 END) AS missing_rr,
    SUM(CASE WHEN effective_rr IS NULL THEN 1 ELSE 0 END) AS missing_effective_rr,
    SUM(CASE WHEN created_at IS NULL THEN 1 ELSE 0 END) AS missing_created_at
FROM order_decisions
WHERE mode = 'PAPER'
  AND COALESCE(phase, 'final') = 'final';

-- =====================================================
-- 5. Duplicate or conflicting final decisions
-- =====================================================
SELECT
    signal_id,
    COUNT(*) AS final_rows,
    COUNT(DISTINCT decision) AS distinct_decisions,
    GROUP_CONCAT(DISTINCT decision) AS decisions,
    GROUP_CONCAT(DISTINCT reject_reason) AS reject_reasons,
    GROUP_CONCAT(decision_id) AS decision_ids
FROM order_decisions
WHERE mode = 'PAPER'
  AND COALESCE(phase, 'final') = 'final'
GROUP BY signal_id
HAVING COUNT(*) > 1 OR COUNT(DISTINCT decision) > 1
ORDER BY final_rows DESC, signal_id;

-- =====================================================
-- 6. Score / RR / effective_rr variability
-- =====================================================
SELECT
    COUNT(*) AS final_rows,
    MIN(score) AS min_score,
    MAX(score) AS max_score,
    COUNT(DISTINCT ROUND(score, 4)) AS distinct_score_values,
    MIN(rr) AS min_raw_rr,
    MAX(rr) AS max_raw_rr,
    COUNT(DISTINCT ROUND(rr, 4)) AS distinct_raw_rr_values,
    MIN(effective_rr) AS min_effective_rr,
    MAX(effective_rr) AS max_effective_rr,
    COUNT(DISTINCT ROUND(effective_rr, 4)) AS distinct_effective_rr_values
FROM order_decisions
WHERE mode = 'PAPER'
  AND COALESCE(phase, 'final') = 'final';

-- =====================================================
-- 7. Suspicious raw_rr fallback under non-zero costs
-- =====================================================
SELECT
    decision_id,
    signal_id,
    symbol,
    decision,
    rr,
    effective_rr,
    spread_pct,
    expected_slippage_pct,
    latency_ms,
    funding_rate_pct,
    volatility_regime
FROM order_decisions
WHERE mode = 'PAPER'
  AND COALESCE(phase, 'final') = 'final'
  AND rr IS NOT NULL
  AND effective_rr IS NOT NULL
  AND ABS(CAST(rr AS REAL) - CAST(effective_rr AS REAL)) < 0.0000001
  AND (
      COALESCE(CAST(spread_pct AS REAL), 0.0) > 0.0
      OR COALESCE(CAST(expected_slippage_pct AS REAL), 0.0) > 0.0
      OR COALESCE(CAST(latency_ms AS REAL), 0.0) > 0.0
      OR COALESCE(CAST(funding_rate_pct AS REAL), 0.0) != 0.0
  )
ORDER BY symbol, signal_id
LIMIT 200;

-- =====================================================
-- 8. Execution context availability in lifecycle rows
-- =====================================================
SELECT
    lifecycle_state,
    COUNT(*) AS rows,
    SUM(CASE WHEN execution_ctx_missing = 1 THEN 1 ELSE 0 END) AS ctx_missing_rows,
    ROUND(
        CAST(SUM(CASE WHEN execution_ctx_missing = 1 THEN 1 ELSE 0 END) AS REAL)
        / NULLIF(COUNT(*), 0),
        6
    ) AS ctx_missing_ratio
FROM trade_lifecycle_events
WHERE mode = 'PAPER'
GROUP BY lifecycle_state
ORDER BY lifecycle_state;

-- =====================================================
-- 9. Lifecycle state distribution and terminal coverage
-- =====================================================
SELECT
    lifecycle_state,
    COUNT(*) AS rows
FROM trade_lifecycle_events
WHERE mode = 'PAPER'
GROUP BY lifecycle_state
ORDER BY rows DESC, lifecycle_state;

-- =====================================================
-- 10. Lifecycle chain per signal
-- =====================================================
SELECT
    signal_id,
    symbol,
    COUNT(*) AS lifecycle_rows,
    MIN(lifecycle_seq) AS min_seq,
    MAX(lifecycle_seq) AS max_seq,
    GROUP_CONCAT(lifecycle_state, ' -> ') AS lifecycle_chain
FROM trade_lifecycle_events
WHERE mode = 'PAPER'
GROUP BY signal_id, symbol
ORDER BY lifecycle_rows DESC, signal_id
LIMIT 200;

-- =====================================================
-- 11. Lifecycle ordering anomalies
-- =====================================================
SELECT
    signal_id,
    symbol,
    MIN(lifecycle_seq) AS min_seq,
    MAX(lifecycle_seq) AS max_seq,
    COUNT(*) AS rows,
    GROUP_CONCAT(lifecycle_state, ' -> ') AS lifecycle_chain
FROM trade_lifecycle_events
WHERE mode = 'PAPER'
GROUP BY signal_id, symbol
HAVING MIN(lifecycle_seq) <= 0
   OR MAX(lifecycle_seq) < COUNT(*)
ORDER BY signal_id;

-- =====================================================
-- 12. Lifecycle reject vs final rejected decision reconciliation
-- =====================================================
SELECT
    l.signal_id,
    l.symbol,
    l.lifecycle_state,
    l.reject_reason AS lifecycle_reject_reason,
    d.decision_id,
    d.reject_reason AS final_decision_reject_reason,
    d.effective_rr
FROM trade_lifecycle_events l
LEFT JOIN order_decisions d
    ON d.signal_id = l.signal_id
   AND d.mode = l.mode
   AND d.decision = 'REJECTED'
   AND COALESCE(d.phase, 'final') = 'final'
WHERE l.mode = 'PAPER'
  AND (l.lifecycle_state IN ('SIGNAL_REJECTED', 'ORDER_REJECTED') OR l.decision = 'REJECTED')
ORDER BY l.signal_id, l.lifecycle_seq;

-- =====================================================
-- 13. Classification helper output
-- =====================================================
SELECT
    CASE
        WHEN (SELECT COUNT(*) FROM order_decisions WHERE mode='PAPER' AND COALESCE(phase,'final')='final') < 30
            THEN 'INSUFFICIENT_SAMPLE'
        WHEN (SELECT COUNT(*) FROM order_decisions WHERE mode='PAPER' AND COALESCE(phase,'final')='final' AND (signal_id IS NULL OR TRIM(signal_id)='')) > 0
            THEN 'DATA_INTEGRITY_FAILURE'
        WHEN (SELECT COUNT(*) FROM order_decisions WHERE mode='PAPER' AND COALESCE(phase,'final')='final' AND decision='REJECTED' AND (reject_reason IS NULL OR TRIM(reject_reason)='' OR UPPER(reject_reason)='UNKNOWN')) > 0
            THEN 'DATA_INTEGRITY_FAILURE'
        WHEN (SELECT COUNT(*) FROM trade_lifecycle_events WHERE mode='PAPER' AND execution_ctx_missing=1) > 0
            THEN 'EXECUTION_CONTEXT_FAILURE'
        WHEN (SELECT COUNT(DISTINCT ROUND(score,4)) FROM order_decisions WHERE mode='PAPER' AND COALESCE(phase,'final')='final') <= 1
            THEN 'SCORING_OR_REGIME_PIPELINE_FAILURE'
        ELSE 'HEALTHY_SELECTIVITY'
    END AS paper_runtime_classification;
