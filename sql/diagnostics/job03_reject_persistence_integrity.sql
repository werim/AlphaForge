-- JOB-03 Reject Persistence Integrity Diagnostics
-- Audit-only SQLite diagnostics. No schema changes.
-- Intended for AlphaForge runtime/backtest SQLite artifacts.

-- =====================================================
-- 1. Final rejected decision completeness
-- =====================================================
SELECT
    mode,
    COUNT(*) AS final_rejected_rows,
    SUM(CASE WHEN signal_id IS NULL OR TRIM(signal_id) = '' THEN 1 ELSE 0 END) AS missing_signal_id,
    SUM(CASE WHEN symbol IS NULL OR TRIM(symbol) = '' THEN 1 ELSE 0 END) AS missing_symbol,
    SUM(CASE WHEN reject_reason IS NULL OR TRIM(reject_reason) = '' THEN 1 ELSE 0 END) AS missing_reject_reason,
    SUM(CASE WHEN UPPER(COALESCE(reject_reason, '')) = 'UNKNOWN' THEN 1 ELSE 0 END) AS unknown_reject_reason,
    SUM(CASE WHEN score IS NULL THEN 1 ELSE 0 END) AS missing_score,
    SUM(CASE WHEN rr IS NULL THEN 1 ELSE 0 END) AS missing_rr,
    SUM(CASE WHEN effective_rr IS NULL THEN 1 ELSE 0 END) AS missing_effective_rr,
    SUM(CASE WHEN created_at IS NULL THEN 1 ELSE 0 END) AS missing_created_at
FROM order_decisions
WHERE decision = 'REJECTED'
  AND COALESCE(phase, 'final') = 'final'
GROUP BY mode
ORDER BY mode;

-- =====================================================
-- 2. AI-internal rows must not be counted as final
-- =====================================================
SELECT
    mode,
    COALESCE(phase, 'final') AS phase_class,
    decision,
    COUNT(*) AS rows
FROM order_decisions
GROUP BY mode, COALESCE(phase, 'final'), decision
ORDER BY mode, phase_class, decision;

-- =====================================================
-- 3. Duplicate final rejected rows per signal/mode
-- =====================================================
SELECT
    mode,
    signal_id,
    COUNT(*) AS final_rejected_rows,
    GROUP_CONCAT(decision_id) AS decision_ids,
    GROUP_CONCAT(DISTINCT reject_reason) AS reject_reasons
FROM order_decisions
WHERE decision = 'REJECTED'
  AND COALESCE(phase, 'final') = 'final'
GROUP BY mode, signal_id
HAVING COUNT(*) > 1
ORDER BY final_rejected_rows DESC, mode, signal_id;

-- =====================================================
-- 4. Conflicting final decisions for same signal/mode
-- =====================================================
SELECT
    mode,
    signal_id,
    COUNT(*) AS final_rows,
    COUNT(DISTINCT decision) AS distinct_decisions,
    GROUP_CONCAT(DISTINCT decision) AS decisions,
    GROUP_CONCAT(DISTINCT reject_reason) AS reject_reasons
FROM order_decisions
WHERE COALESCE(phase, 'final') = 'final'
GROUP BY mode, signal_id
HAVING COUNT(DISTINCT decision) > 1
ORDER BY mode, signal_id;

-- =====================================================
-- 5. Rejected lifecycle completeness
-- =====================================================
SELECT
    mode,
    lifecycle_state,
    COUNT(*) AS rejected_lifecycle_rows,
    SUM(CASE WHEN signal_id IS NULL OR TRIM(signal_id) = '' THEN 1 ELSE 0 END) AS missing_signal_id,
    SUM(CASE WHEN symbol IS NULL OR TRIM(symbol) = '' THEN 1 ELSE 0 END) AS missing_symbol,
    SUM(CASE WHEN reject_reason IS NULL OR TRIM(reject_reason) = '' THEN 1 ELSE 0 END) AS missing_reject_reason,
    SUM(CASE WHEN UPPER(COALESCE(reject_reason, '')) = 'UNKNOWN' THEN 1 ELSE 0 END) AS unknown_reject_reason,
    SUM(CASE WHEN score IS NULL THEN 1 ELSE 0 END) AS missing_score,
    SUM(CASE WHEN rr IS NULL THEN 1 ELSE 0 END) AS missing_rr,
    SUM(CASE WHEN effective_rr IS NULL THEN 1 ELSE 0 END) AS missing_effective_rr,
    SUM(CASE WHEN execution_ctx_missing = 1 THEN 1 ELSE 0 END) AS execution_ctx_missing_rows
FROM trade_lifecycle_events
WHERE lifecycle_state IN ('SIGNAL_REJECTED', 'ORDER_REJECTED', 'SYMBOL_REJECTED')
   OR decision = 'REJECTED'
GROUP BY mode, lifecycle_state
ORDER BY mode, lifecycle_state;

-- =====================================================
-- 6. Rejected rows with raw RR == effective RR despite costs present
-- This flags possible raw_rr fallback masquerading as effective_rr.
-- =====================================================
SELECT
    decision_id,
    signal_id,
    symbol,
    mode,
    phase,
    rr,
    effective_rr,
    spread_pct,
    expected_slippage_pct,
    latency_ms,
    funding_rate_pct,
    volatility_regime
FROM order_decisions
WHERE decision = 'REJECTED'
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
ORDER BY mode, symbol, signal_id;

-- =====================================================
-- 7. UNKNOWN / blank reason examples for forensic drilldown
-- =====================================================
SELECT
    decision_id,
    signal_id,
    symbol,
    mode,
    phase,
    decision,
    reject_reason,
    explanation,
    created_at
FROM order_decisions
WHERE decision = 'REJECTED'
  AND (reject_reason IS NULL OR TRIM(reject_reason) = '' OR UPPER(TRIM(reject_reason)) = 'UNKNOWN')
ORDER BY created_at DESC
LIMIT 100;

-- =====================================================
-- 8. Backtest/PAPER rejected lifecycle vs final decision reconciliation
-- =====================================================
SELECT
    l.mode,
    l.signal_id,
    l.symbol,
    l.lifecycle_state,
    l.reject_reason AS lifecycle_reject_reason,
    d.decision_id,
    d.reject_reason AS decision_reject_reason,
    d.phase
FROM trade_lifecycle_events l
LEFT JOIN order_decisions d
    ON d.signal_id = l.signal_id
   AND d.mode = l.mode
   AND d.decision = 'REJECTED'
   AND COALESCE(d.phase, 'final') = 'final'
WHERE l.lifecycle_state IN ('SIGNAL_REJECTED', 'ORDER_REJECTED')
ORDER BY l.mode, l.signal_id, l.lifecycle_seq;
