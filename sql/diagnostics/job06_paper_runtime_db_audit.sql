-- JOB-06 Paper Runtime DB Audit Pack
-- Read-only PAPER evidence pack. HEALTHY_SELECTIVITY is never emitted while
-- lifecycle integrity, execution evidence, accepted sampling, or RR realism is absent.

-- 0. PAPER row availability
SELECT 'order_decisions' AS table_name, COUNT(*) AS paper_rows
FROM order_decisions WHERE mode = 'PAPER'
UNION ALL
SELECT 'trade_lifecycle_events' AS table_name, COUNT(*) AS paper_rows
FROM trade_lifecycle_events WHERE mode = 'PAPER';

-- 1. Final decision totals and reject rate
SELECT
    COUNT(*) AS final_decision_rows,
    SUM(CASE WHEN decision = 'REJECTED' THEN 1 ELSE 0 END) AS rejected_rows,
    SUM(CASE WHEN decision IN ('ACCEPTED', 'EXECUTED') THEN 1 ELSE 0 END) AS accepted_rows,
    ROUND(CAST(SUM(CASE WHEN decision = 'REJECTED' THEN 1 ELSE 0 END) AS REAL) / NULLIF(COUNT(*), 0), 6) AS reject_rate
FROM order_decisions
WHERE mode = 'PAPER' AND COALESCE(phase, 'final') = 'final';

-- 2. Final reject-reason distribution
SELECT COALESCE(NULLIF(TRIM(reject_reason), ''), 'MISSING') AS reject_reason, COUNT(*) AS rows
FROM order_decisions
WHERE mode = 'PAPER' AND COALESCE(phase, 'final') = 'final' AND decision = 'REJECTED'
GROUP BY COALESCE(NULLIF(TRIM(reject_reason), ''), 'MISSING')
ORDER BY rows DESC, reject_reason;

-- 3. Missing audit-critical final-decision fields
SELECT
    SUM(CASE WHEN signal_id IS NULL OR TRIM(signal_id) = '' THEN 1 ELSE 0 END) AS missing_signal_id,
    SUM(CASE WHEN symbol IS NULL OR TRIM(symbol) = '' THEN 1 ELSE 0 END) AS missing_symbol,
    SUM(CASE WHEN decision = 'REJECTED' AND (reject_reason IS NULL OR TRIM(reject_reason) = '') THEN 1 ELSE 0 END) AS missing_reject_reason,
    SUM(CASE WHEN decision = 'REJECTED' AND UPPER(COALESCE(reject_reason, '')) = 'UNKNOWN' THEN 1 ELSE 0 END) AS unknown_reject_reason,
    SUM(CASE WHEN score IS NULL THEN 1 ELSE 0 END) AS missing_score,
    SUM(CASE WHEN rr IS NULL THEN 1 ELSE 0 END) AS missing_rr,
    SUM(CASE WHEN effective_rr IS NULL THEN 1 ELSE 0 END) AS missing_effective_rr
FROM order_decisions
WHERE mode = 'PAPER' AND COALESCE(phase, 'final') = 'final';

-- 4. Duplicate or conflicting final decisions
SELECT signal_id, COUNT(*) AS final_rows, COUNT(DISTINCT decision) AS distinct_decisions,
       GROUP_CONCAT(DISTINCT decision) AS decisions, GROUP_CONCAT(DISTINCT reject_reason) AS reject_reasons
FROM order_decisions
WHERE mode = 'PAPER' AND COALESCE(phase, 'final') = 'final'
GROUP BY signal_id
HAVING COUNT(*) > 1 OR COUNT(DISTINCT decision) > 1
ORDER BY final_rows DESC, signal_id;

-- 5. Score / raw RR / effective RR variability
SELECT
    COUNT(*) AS final_rows,
    MIN(score) AS min_score, MAX(score) AS max_score, COUNT(DISTINCT ROUND(score, 4)) AS distinct_score_values,
    MIN(rr) AS min_raw_rr, MAX(rr) AS max_raw_rr, COUNT(DISTINCT ROUND(rr, 4)) AS distinct_raw_rr_values,
    MIN(effective_rr) AS min_effective_rr, MAX(effective_rr) AS max_effective_rr,
    COUNT(DISTINCT ROUND(effective_rr, 4)) AS distinct_effective_rr_values
FROM order_decisions
WHERE mode = 'PAPER' AND COALESCE(phase, 'final') = 'final';

-- 6. Execution context coverage is measured on order_decisions
SELECT
    COUNT(*) AS final_rows,
    SUM(CASE WHEN COALESCE(execution_ctx_missing, 0) = 1 THEN 1 ELSE 0 END) AS ctx_missing_rows,
    SUM(CASE WHEN execution_ctx IS NULL OR TRIM(execution_ctx) IN ('', '{}') THEN 1 ELSE 0 END) AS empty_ctx_payload_rows,
    COUNT(DISTINCT ROUND(spread_pct, 8)) AS distinct_spread_values,
    COUNT(DISTINCT ROUND(expected_slippage_pct, 8)) AS distinct_slippage_values,
    COUNT(DISTINCT ROUND(latency_ms, 4)) AS distinct_latency_values
FROM order_decisions
WHERE mode = 'PAPER' AND COALESCE(phase, 'final') = 'final';

-- 7. Suspicious raw-RR fallback where persisted execution costs exist
SELECT decision_id, signal_id, symbol, decision, rr, effective_rr, spread_pct,
       expected_slippage_pct, latency_ms, funding_rate_pct, volatility_regime
FROM order_decisions
WHERE mode = 'PAPER' AND COALESCE(phase, 'final') = 'final'
  AND rr IS NOT NULL AND effective_rr IS NOT NULL
  AND ABS(CAST(rr AS REAL) - CAST(effective_rr AS REAL)) < 0.0000001
  AND (COALESCE(spread_pct, 0.0) > 0.0 OR COALESCE(expected_slippage_pct, 0.0) > 0.0
       OR COALESCE(latency_ms, 0.0) > 0.0 OR ABS(COALESCE(funding_rate_pct, 0.0)) > 0.0)
ORDER BY symbol, signal_id LIMIT 200;

-- 8. Lifecycle state distribution, with explicit corruption ratio
SELECT
    COUNT(*) AS lifecycle_rows,
    SUM(CASE WHEN lifecycle_state = 'ERROR' THEN 1 ELSE 0 END) AS lifecycle_error_rows,
    ROUND(CAST(SUM(CASE WHEN lifecycle_state = 'ERROR' THEN 1 ELSE 0 END) AS REAL) / NULLIF(COUNT(*), 0), 6) AS lifecycle_error_ratio,
    SUM(CASE WHEN lifecycle_state IN ('SIGNAL_REJECTED', 'ORDER_REJECTED') THEN 1 ELSE 0 END) AS lifecycle_reject_rows
FROM trade_lifecycle_events WHERE mode = 'PAPER';

-- 9. Lifecycle state detail
SELECT lifecycle_state, COUNT(*) AS rows
FROM trade_lifecycle_events WHERE mode = 'PAPER'
GROUP BY lifecycle_state ORDER BY rows DESC, lifecycle_state;

-- 10. Rejected final decisions without rejected lifecycle evidence
SELECT d.signal_id, d.symbol, d.reject_reason, d.created_at
FROM order_decisions d
LEFT JOIN trade_lifecycle_events l
  ON l.signal_id = d.signal_id AND l.mode = d.mode
 AND l.lifecycle_state IN ('SIGNAL_REJECTED', 'ORDER_REJECTED')
WHERE d.mode = 'PAPER' AND COALESCE(d.phase, 'final') = 'final' AND d.decision = 'REJECTED'
  AND l.signal_id IS NULL
ORDER BY d.created_at DESC LIMIT 200;

-- 11. PRE_RR_GATE-style early rejects still carrying NULL RR evidence
SELECT reject_reason, COUNT(*) AS final_rows,
       SUM(CASE WHEN rr IS NULL THEN 1 ELSE 0 END) AS null_rr_rows,
       SUM(CASE WHEN effective_rr IS NULL THEN 1 ELSE 0 END) AS null_effective_rr_rows
FROM order_decisions
WHERE mode = 'PAPER' AND COALESCE(phase, 'final') = 'final' AND decision = 'REJECTED'
GROUP BY reject_reason ORDER BY final_rows DESC;

-- 12. Fail-closed classification helper
WITH final_stats AS (
    SELECT COUNT(*) AS final_rows,
           SUM(CASE WHEN decision = 'REJECTED' THEN 1 ELSE 0 END) AS rejected_rows,
           SUM(CASE WHEN decision IN ('ACCEPTED', 'EXECUTED') THEN 1 ELSE 0 END) AS accepted_rows,
           SUM(CASE WHEN signal_id IS NULL OR TRIM(signal_id) = '' OR symbol IS NULL OR TRIM(symbol) = '' THEN 1 ELSE 0 END) AS missing_identity_rows,
           SUM(CASE WHEN decision = 'REJECTED' AND (reject_reason IS NULL OR TRIM(reject_reason) = '' OR UPPER(reject_reason) = 'UNKNOWN') THEN 1 ELSE 0 END) AS bad_reject_reason_rows,
           SUM(CASE WHEN COALESCE(execution_ctx_missing, 0) = 1 OR execution_ctx IS NULL OR TRIM(execution_ctx) IN ('', '{}') THEN 1 ELSE 0 END) AS bad_execution_context_rows,
           COUNT(DISTINCT ROUND(score, 4)) AS distinct_scores,
           COUNT(DISTINCT ROUND(effective_rr, 4)) AS distinct_effective_rr
    FROM order_decisions WHERE mode = 'PAPER' AND COALESCE(phase, 'final') = 'final'
), lifecycle_stats AS (
    SELECT COUNT(*) AS lifecycle_rows,
           SUM(CASE WHEN lifecycle_state = 'ERROR' THEN 1 ELSE 0 END) AS error_rows
    FROM trade_lifecycle_events WHERE mode = 'PAPER'
), unmatched AS (
    SELECT COUNT(*) AS rejected_without_lifecycle
    FROM order_decisions d
    LEFT JOIN trade_lifecycle_events l
      ON l.signal_id = d.signal_id AND l.mode = d.mode
     AND l.lifecycle_state IN ('SIGNAL_REJECTED', 'ORDER_REJECTED')
    WHERE d.mode = 'PAPER' AND COALESCE(d.phase, 'final') = 'final' AND d.decision = 'REJECTED'
      AND l.signal_id IS NULL
)
SELECT
    CASE
      WHEN final_stats.final_rows < 30 THEN 'INSUFFICIENT_SAMPLE'
      WHEN final_stats.missing_identity_rows > 0 OR final_stats.bad_reject_reason_rows > 0 THEN 'DATA_INTEGRITY_FAILURE'
      WHEN lifecycle_stats.lifecycle_rows = 0 OR lifecycle_stats.error_rows > 0 OR unmatched.rejected_without_lifecycle > 0 THEN 'LIFECYCLE_INTEGRITY_FAILURE'
      WHEN final_stats.bad_execution_context_rows > 0 THEN 'EXECUTION_CONTEXT_UNVERIFIED'
      WHEN final_stats.accepted_rows = 0 THEN 'NO_ACCEPTED_SAMPLE'
      WHEN final_stats.distinct_scores <= 1 OR final_stats.distinct_effective_rr <= 1 THEN 'SCORING_OR_RR_PIPELINE_SUSPECT'
      ELSE 'HEALTHY_SELECTIVITY_CANDIDATE'
    END AS paper_runtime_classification,
    final_stats.*, lifecycle_stats.lifecycle_rows, lifecycle_stats.error_rows, unmatched.rejected_without_lifecycle
FROM final_stats, lifecycle_stats, unmatched;
