-- JOB19 V1 — PAPER Runtime Reject-Rate and Decision-Quality Audit
-- Audit-only SQL pack. This file must not change thresholds, scoring, runtime
-- behavior, order submission, or lifecycle emission logic.
--
-- Usage:
--   sqlite3 path/to/runtime.sqlite < sql/diagnostics/job19_paper_reject_rate_decision_quality_audit.sql
--
-- Scope:
--   PAPER rows in order_decisions, trade_lifecycle_events, and signals.
--   These diagnostics classify evidence quality; they do not prove live readiness.

.headers on
.mode column

-- ---------------------------------------------------------------------------
-- 00. Database object availability
-- Proves whether the expected runtime audit tables exist in the selected DB.
-- ---------------------------------------------------------------------------
SELECT '00_TABLE_AVAILABILITY' AS diagnostic,
       name AS table_name,
       type
FROM sqlite_master
WHERE type IN ('table', 'view')
  AND name IN ('signals', 'order_decisions', 'trade_lifecycle_events', 'closed_trade_reviews')
ORDER BY name;

-- ---------------------------------------------------------------------------
-- 01. PAPER decision totals and rejection rate
-- Classifies sample size and selectivity. High rejection can be healthy only if
-- reject reasons, execution context, and score/RR variability are intact.
-- ---------------------------------------------------------------------------
SELECT '01_DECISION_TOTALS_REJECT_RATE' AS diagnostic,
       COUNT(*) AS total_decisions,
       COUNT(DISTINCT NULLIF(TRIM(signal_id), '')) AS distinct_signal_ids,
       SUM(CASE WHEN UPPER(COALESCE(decision, '')) = 'REJECTED' THEN 1 ELSE 0 END) AS rejected_decisions,
       SUM(CASE WHEN UPPER(COALESCE(decision, '')) IN ('ACCEPTED', 'APPROVED', 'ORDER_PLACED', 'EXECUTED') THEN 1 ELSE 0 END) AS accepted_like_decisions,
       ROUND(100.0 * SUM(CASE WHEN UPPER(COALESCE(decision, '')) = 'REJECTED' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 4) AS reject_rate_pct,
       MIN(created_at) AS first_decision_ts,
       MAX(created_at) AS last_decision_ts
FROM order_decisions
WHERE UPPER(COALESCE(mode, '')) = 'PAPER';

-- ---------------------------------------------------------------------------
-- 02. Decision distribution by decision/reject_reason/execution missing flag
-- Shows whether rejection concentration is explained or hidden behind UNKNOWN.
-- ---------------------------------------------------------------------------
SELECT '02_DECISION_REASON_DISTRIBUTION' AS diagnostic,
       COALESCE(NULLIF(TRIM(decision), ''), 'EMPTY_DECISION') AS decision_state,
       COALESCE(NULLIF(TRIM(reject_reason), ''), 'EMPTY_REJECT_REASON') AS reject_reason_state,
       COALESCE(CAST(execution_ctx_missing AS TEXT), 'NULL_EXECUTION_CTX_MISSING') AS execution_ctx_missing_state,
       COUNT(*) AS rows
FROM order_decisions
WHERE UPPER(COALESCE(mode, '')) = 'PAPER'
GROUP BY decision_state, reject_reason_state, execution_ctx_missing_state
ORDER BY rows DESC, decision_state, reject_reason_state;

-- ---------------------------------------------------------------------------
-- 03. Reject reason completeness
-- DATA_INTEGRITY_FAILURE if rejected rows have empty/NULL reject_reason.
-- ---------------------------------------------------------------------------
SELECT '03_REJECT_REASON_COMPLETENESS' AS diagnostic,
       COUNT(*) AS rejected_rows,
       SUM(CASE WHEN reject_reason IS NULL OR TRIM(reject_reason) = '' THEN 1 ELSE 0 END) AS missing_reject_reason_rows,
       SUM(CASE WHEN UPPER(TRIM(COALESCE(reject_reason, ''))) = 'UNKNOWN' THEN 1 ELSE 0 END) AS unknown_reject_reason_rows,
       ROUND(100.0 * SUM(CASE WHEN reject_reason IS NULL OR TRIM(reject_reason) = '' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 4) AS missing_reject_reason_pct,
       ROUND(100.0 * SUM(CASE WHEN UPPER(TRIM(COALESCE(reject_reason, ''))) = 'UNKNOWN' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 4) AS unknown_reject_reason_pct
FROM order_decisions
WHERE UPPER(COALESCE(mode, '')) = 'PAPER'
  AND UPPER(COALESCE(decision, '')) = 'REJECTED';

-- ---------------------------------------------------------------------------
-- 04. Missing audit-critical decision fields
-- DATA_INTEGRITY_FAILURE if signal_id/symbol/decision/score/RR fields are absent.
-- EXECUTION_CONTEXT_FAILURE if execution cost/liquidity fields are missing/zeroed.
-- ---------------------------------------------------------------------------
SELECT '04_MISSING_AUDIT_CRITICAL_FIELDS' AS diagnostic,
       COUNT(*) AS paper_decision_rows,
       SUM(CASE WHEN signal_id IS NULL OR TRIM(signal_id) = '' THEN 1 ELSE 0 END) AS missing_signal_id,
       SUM(CASE WHEN symbol IS NULL OR TRIM(symbol) = '' THEN 1 ELSE 0 END) AS missing_symbol,
       SUM(CASE WHEN decision IS NULL OR TRIM(decision) = '' THEN 1 ELSE 0 END) AS missing_decision,
       SUM(CASE WHEN score IS NULL THEN 1 ELSE 0 END) AS missing_score,
       SUM(CASE WHEN rr IS NULL THEN 1 ELSE 0 END) AS missing_raw_rr,
       SUM(CASE WHEN effective_rr IS NULL THEN 1 ELSE 0 END) AS missing_effective_rr,
       SUM(CASE WHEN expectancy_bucket IS NULL OR TRIM(expectancy_bucket) = '' OR UPPER(TRIM(expectancy_bucket)) = 'UNKNOWN' THEN 1 ELSE 0 END) AS missing_or_unknown_expectancy_bucket,
       SUM(CASE WHEN execution_ctx IS NULL OR TRIM(execution_ctx) = '' OR TRIM(execution_ctx) = '{}' THEN 1 ELSE 0 END) AS missing_execution_ctx_payload,
       SUM(CASE WHEN COALESCE(spread_pct, 0.0) = 0.0 THEN 1 ELSE 0 END) AS zero_spread_pct,
       SUM(CASE WHEN COALESCE(expected_slippage_pct, 0.0) = 0.0 THEN 1 ELSE 0 END) AS zero_expected_slippage_pct,
       SUM(CASE WHEN COALESCE(latency_ms, 0.0) = 0.0 THEN 1 ELSE 0 END) AS zero_latency_ms,
       SUM(CASE WHEN COALESCE(funding_rate_pct, 0.0) = 0.0 THEN 1 ELSE 0 END) AS zero_funding_rate_pct,
       SUM(CASE WHEN execution_regime IS NULL OR TRIM(execution_regime) = '' THEN 1 ELSE 0 END) AS missing_execution_regime,
       SUM(CASE WHEN volatility_regime IS NULL OR TRIM(volatility_regime) = '' THEN 1 ELSE 0 END) AS missing_volatility_regime
FROM order_decisions
WHERE UPPER(COALESCE(mode, '')) = 'PAPER';

-- ---------------------------------------------------------------------------
-- 05. Duplicate/inconsistent decisions by signal_id/symbol
-- DATA_INTEGRITY_FAILURE if the same signal has conflicting decisions/reasons.
-- ---------------------------------------------------------------------------
SELECT '05_DUPLICATE_OR_INCONSISTENT_SIGNAL_DECISIONS' AS diagnostic,
       signal_id,
       symbol,
       COUNT(*) AS rows_for_signal_symbol,
       COUNT(DISTINCT COALESCE(NULLIF(TRIM(decision), ''), 'EMPTY')) AS distinct_decisions,
       COUNT(DISTINCT COALESCE(NULLIF(TRIM(reject_reason), ''), 'EMPTY')) AS distinct_reject_reasons,
       MIN(created_at) AS first_seen,
       MAX(created_at) AS last_seen
FROM order_decisions
WHERE UPPER(COALESCE(mode, '')) = 'PAPER'
  AND signal_id IS NOT NULL
  AND TRIM(signal_id) <> ''
GROUP BY signal_id, symbol
HAVING COUNT(*) > 1
    OR COUNT(DISTINCT COALESCE(NULLIF(TRIM(decision), ''), 'EMPTY')) > 1
    OR COUNT(DISTINCT COALESCE(NULLIF(TRIM(reject_reason), ''), 'EMPTY')) > 1
ORDER BY rows_for_signal_symbol DESC, last_seen DESC
LIMIT 100;

-- ---------------------------------------------------------------------------
-- 06. Score / raw RR / effective RR variability
-- SCORING_OR_REGIME_PIPELINE_FAILURE if score/RR/effective_rr are constant or NULL.
-- ---------------------------------------------------------------------------
SELECT '06_SCORE_RR_EFFECTIVE_RR_VARIABILITY' AS diagnostic,
       COUNT(*) AS paper_decision_rows,
       COUNT(DISTINCT ROUND(score, 6)) AS distinct_score_values,
       MIN(score) AS min_score,
       MAX(score) AS max_score,
       ROUND(AVG(score), 6) AS avg_score,
       COUNT(DISTINCT ROUND(rr, 6)) AS distinct_raw_rr_values,
       MIN(rr) AS min_raw_rr,
       MAX(rr) AS max_raw_rr,
       ROUND(AVG(rr), 6) AS avg_raw_rr,
       COUNT(DISTINCT ROUND(effective_rr, 6)) AS distinct_effective_rr_values,
       MIN(effective_rr) AS min_effective_rr,
       MAX(effective_rr) AS max_effective_rr,
       ROUND(AVG(effective_rr), 6) AS avg_effective_rr
FROM order_decisions
WHERE UPPER(COALESCE(mode, '')) = 'PAPER';

-- ---------------------------------------------------------------------------
-- 07. Execution context availability and cost realism
-- EXECUTION_CONTEXT_FAILURE if costs/liquidity context are absent, all zero, or marked missing.
-- ---------------------------------------------------------------------------
SELECT '07_EXECUTION_CONTEXT_AVAILABILITY' AS diagnostic,
       COUNT(*) AS paper_decision_rows,
       SUM(CASE WHEN COALESCE(execution_ctx_missing, 0) = 1 THEN 1 ELSE 0 END) AS execution_ctx_missing_flag_rows,
       SUM(CASE WHEN execution_ctx IS NOT NULL AND TRIM(execution_ctx) NOT IN ('', '{}') THEN 1 ELSE 0 END) AS non_empty_execution_ctx_payload_rows,
       COUNT(DISTINCT ROUND(spread_pct, 8)) AS distinct_spread_pct_values,
       COUNT(DISTINCT ROUND(expected_slippage_pct, 8)) AS distinct_expected_slippage_pct_values,
       COUNT(DISTINCT ROUND(latency_ms, 4)) AS distinct_latency_ms_values,
       COUNT(DISTINCT ROUND(orderbook_imbalance, 8)) AS distinct_orderbook_imbalance_values,
       COUNT(DISTINCT ROUND(funding_rate_pct, 8)) AS distinct_funding_rate_pct_values,
       COUNT(DISTINCT COALESCE(NULLIF(TRIM(execution_regime), ''), 'EMPTY')) AS distinct_execution_regimes,
       COUNT(DISTINCT COALESCE(NULLIF(TRIM(volatility_regime), ''), 'EMPTY')) AS distinct_volatility_regimes
FROM order_decisions
WHERE UPPER(COALESCE(mode, '')) = 'PAPER';

-- ---------------------------------------------------------------------------
-- 08. Lifecycle consistency totals
-- Verifies whether PAPER lifecycle evidence exists and covers rejection/placement/result states.
-- ---------------------------------------------------------------------------
SELECT '08_LIFECYCLE_CONSISTENCY_TOTALS' AS diagnostic,
       COUNT(*) AS paper_lifecycle_rows,
       COUNT(DISTINCT NULLIF(TRIM(signal_id), '')) AS distinct_lifecycle_signal_ids,
       SUM(CASE WHEN UPPER(COALESCE(lifecycle_state, state, event_type, '')) LIKE '%REJECT%' THEN 1 ELSE 0 END) AS rejected_lifecycle_rows,
       SUM(CASE WHEN UPPER(COALESCE(lifecycle_state, state, event_type, '')) IN ('SIGNAL_CREATED', 'SIGNAL_ACCEPTED', 'WAITING_ENTRY_ZONE', 'ENTRY_TRIGGERED', 'ORDER_PLACED') THEN 1 ELSE 0 END) AS pre_result_lifecycle_rows,
       SUM(CASE WHEN UPPER(COALESCE(lifecycle_state, state, event_type, '')) IN ('TP_HIT', 'SL_HIT', 'OPEN_AT_END', 'CLOSED', 'CANCELLED', 'FAILED') THEN 1 ELSE 0 END) AS terminal_or_result_rows,
       MIN(event_ts) AS first_lifecycle_ts,
       MAX(event_ts) AS last_lifecycle_ts
FROM trade_lifecycle_events
WHERE UPPER(COALESCE(mode, '')) = 'PAPER';

-- ---------------------------------------------------------------------------
-- 09. Decisions with missing lifecycle evidence
-- DATA_INTEGRITY_FAILURE if accepted/rejected decisions lack lifecycle trail.
-- ---------------------------------------------------------------------------
SELECT '09_DECISIONS_WITHOUT_LIFECYCLE' AS diagnostic,
       d.signal_id,
       d.symbol,
       d.decision,
       d.reject_reason,
       d.created_at
FROM order_decisions d
LEFT JOIN trade_lifecycle_events l
  ON l.signal_id = d.signal_id
 AND UPPER(COALESCE(l.mode, '')) = 'PAPER'
WHERE UPPER(COALESCE(d.mode, '')) = 'PAPER'
  AND (d.signal_id IS NULL OR TRIM(d.signal_id) = '' OR l.signal_id IS NULL)
ORDER BY d.created_at DESC
LIMIT 100;

-- ---------------------------------------------------------------------------
-- 10. Lifecycle rows with missing decision evidence
-- DATA_INTEGRITY_FAILURE if lifecycle rows cannot be tied back to decision rows.
-- ---------------------------------------------------------------------------
SELECT '10_LIFECYCLE_WITHOUT_DECISION' AS diagnostic,
       l.signal_id,
       l.symbol,
       COALESCE(l.lifecycle_state, l.state, l.event_type) AS lifecycle_state,
       l.event_ts
FROM trade_lifecycle_events l
LEFT JOIN order_decisions d
  ON d.signal_id = l.signal_id
 AND UPPER(COALESCE(d.mode, '')) = 'PAPER'
WHERE UPPER(COALESCE(l.mode, '')) = 'PAPER'
  AND (l.signal_id IS NULL OR TRIM(l.signal_id) = '' OR d.signal_id IS NULL)
ORDER BY l.event_ts DESC
LIMIT 100;

-- ---------------------------------------------------------------------------
-- 11. Effective RR sanity vs raw RR and costs
-- SCORING_OR_REGIME_PIPELINE_FAILURE if effective_rr is not cost-adjusted or
-- exceeds raw_rr broadly without clear reason.
-- ---------------------------------------------------------------------------
SELECT '11_EFFECTIVE_RR_SANITY' AS diagnostic,
       COUNT(*) AS paper_decision_rows,
       SUM(CASE WHEN effective_rr IS NOT NULL AND rr IS NOT NULL AND effective_rr > rr THEN 1 ELSE 0 END) AS effective_rr_above_raw_rr_rows,
       SUM(CASE WHEN effective_rr IS NOT NULL AND rr IS NOT NULL AND ABS(effective_rr - rr) < 0.000001 THEN 1 ELSE 0 END) AS effective_rr_equals_raw_rr_rows,
       ROUND(100.0 * SUM(CASE WHEN effective_rr IS NOT NULL AND rr IS NOT NULL AND ABS(effective_rr - rr) < 0.000001 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 4) AS effective_rr_equals_raw_rr_pct,
       ROUND(AVG(COALESCE(rr, 0.0) - COALESCE(effective_rr, 0.0)), 6) AS avg_raw_minus_effective_rr
FROM order_decisions
WHERE UPPER(COALESCE(mode, '')) = 'PAPER';

-- ---------------------------------------------------------------------------
-- 12. Classification helper
-- This is a heuristic flag layer for triage, not a final verdict. Review the
-- underlying diagnostics before classifying the runtime.
-- ---------------------------------------------------------------------------
WITH stats AS (
    SELECT COUNT(*) AS total_rows,
           SUM(CASE WHEN UPPER(COALESCE(decision, '')) = 'REJECTED' THEN 1 ELSE 0 END) AS rejected_rows,
           SUM(CASE WHEN signal_id IS NULL OR TRIM(signal_id) = '' OR symbol IS NULL OR TRIM(symbol) = '' OR decision IS NULL OR TRIM(decision) = '' THEN 1 ELSE 0 END) AS missing_identity_rows,
           SUM(CASE WHEN UPPER(COALESCE(decision, '')) = 'REJECTED' AND (reject_reason IS NULL OR TRIM(reject_reason) = '') THEN 1 ELSE 0 END) AS rejected_missing_reason_rows,
           SUM(CASE WHEN COALESCE(execution_ctx_missing, 0) = 1 OR execution_ctx IS NULL OR TRIM(execution_ctx) IN ('', '{}') THEN 1 ELSE 0 END) AS execution_context_missing_rows,
           COUNT(DISTINCT ROUND(score, 6)) AS distinct_score_values,
           COUNT(DISTINCT ROUND(rr, 6)) AS distinct_raw_rr_values,
           COUNT(DISTINCT ROUND(effective_rr, 6)) AS distinct_effective_rr_values
    FROM order_decisions
    WHERE UPPER(COALESCE(mode, '')) = 'PAPER'
)
SELECT '12_CLASSIFICATION_HELPER' AS diagnostic,
       total_rows,
       rejected_rows,
       CASE
         WHEN total_rows < 100 THEN 'INSUFFICIENT_SAMPLE'
         WHEN missing_identity_rows > 0 OR rejected_missing_reason_rows > 0 THEN 'DATA_INTEGRITY_FAILURE'
         WHEN execution_context_missing_rows > 0 THEN 'EXECUTION_CONTEXT_FAILURE'
         WHEN distinct_score_values <= 1 OR distinct_raw_rr_values <= 1 OR distinct_effective_rr_values <= 1 THEN 'SCORING_OR_REGIME_PIPELINE_FAILURE'
         WHEN rejected_rows > 0 THEN 'HEALTHY_SELECTIVITY_CANDIDATE'
         ELSE 'SCORING_OR_REGIME_PIPELINE_FAILURE'
       END AS suggested_classification,
       missing_identity_rows,
       rejected_missing_reason_rows,
       execution_context_missing_rows,
       distinct_score_values,
       distinct_raw_rr_values,
       distinct_effective_rr_values
FROM stats;
