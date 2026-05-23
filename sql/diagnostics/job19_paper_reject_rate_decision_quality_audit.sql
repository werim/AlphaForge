-- JOB19 V1: PAPER Runtime Reject-Rate and Decision-Quality Audit
-- Scope: audit-only diagnostics; no runtime behavior changes.
-- Runtime DB resolution path reference:
--   src/alphaforge/config/__init__.py::_resolve_database_url
--   src/alphaforge/runtime.py::build_runtime_from_env

-- 0) Optional sample-size and mode sanity
SELECT
  mode,
  COUNT(*) AS decision_rows,
  COUNT(DISTINCT signal_id) AS distinct_signals,
  MIN(created_at) AS first_decision_ts,
  MAX(created_at) AS last_decision_ts
FROM order_decisions
GROUP BY mode
ORDER BY decision_rows DESC;

-- 1) Decision totals and rejection rate (PAPER only)
SELECT
  COUNT(*) AS total_decisions,
  SUM(CASE WHEN UPPER(COALESCE(decision, '')) = 'REJECTED' THEN 1 ELSE 0 END) AS rejected_decisions,
  SUM(CASE WHEN UPPER(COALESCE(decision, '')) IN ('ACCEPTED', 'APPROVED') THEN 1 ELSE 0 END) AS accepted_decisions,
  ROUND(
    100.0 * SUM(CASE WHEN UPPER(COALESCE(decision, '')) = 'REJECTED' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0),
    2
  ) AS rejection_rate_pct
FROM order_decisions
WHERE UPPER(COALESCE(mode, '')) = 'PAPER';

-- 2) reject_reason completeness for rejected PAPER decisions
SELECT
  COUNT(*) AS rejected_rows,
  SUM(CASE WHEN NULLIF(TRIM(COALESCE(reject_reason, '')), '') IS NULL THEN 1 ELSE 0 END) AS missing_reject_reason_rows,
  ROUND(
    100.0 * SUM(CASE WHEN NULLIF(TRIM(COALESCE(reject_reason, '')), '') IS NULL THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0),
    2
  ) AS missing_reject_reason_pct
FROM order_decisions
WHERE UPPER(COALESCE(mode, '')) = 'PAPER'
  AND UPPER(COALESCE(decision, '')) = 'REJECTED';

-- 3) Missing audit-critical fields across PAPER decision rows
SELECT
  SUM(CASE WHEN NULLIF(TRIM(COALESCE(signal_id, '')), '') IS NULL THEN 1 ELSE 0 END) AS missing_signal_id,
  SUM(CASE WHEN NULLIF(TRIM(COALESCE(symbol, '')), '') IS NULL THEN 1 ELSE 0 END) AS missing_symbol,
  SUM(CASE WHEN NULLIF(TRIM(COALESCE(decision, '')), '') IS NULL THEN 1 ELSE 0 END) AS missing_decision,
  SUM(CASE WHEN created_at IS NULL THEN 1 ELSE 0 END) AS missing_created_at,
  SUM(CASE WHEN score IS NULL THEN 1 ELSE 0 END) AS missing_score,
  SUM(CASE WHEN rr IS NULL THEN 1 ELSE 0 END) AS missing_raw_rr,
  SUM(CASE WHEN effective_rr IS NULL THEN 1 ELSE 0 END) AS missing_effective_rr,
  SUM(CASE WHEN execution_ctx IS NULL OR TRIM(COALESCE(execution_ctx, '')) = '' THEN 1 ELSE 0 END) AS missing_execution_ctx,
  SUM(CASE WHEN execution_ctx_missing IS NULL THEN 1 ELSE 0 END) AS missing_execution_ctx_missing_flag
FROM order_decisions
WHERE UPPER(COALESCE(mode, '')) = 'PAPER';

-- 4) Duplicate/inconsistent signal decisions in PAPER
SELECT
  signal_id,
  COUNT(*) AS rows_for_signal,
  COUNT(DISTINCT UPPER(COALESCE(decision, ''))) AS distinct_decision_labels,
  GROUP_CONCAT(DISTINCT UPPER(COALESCE(decision, ''))) AS decision_labels,
  MIN(created_at) AS first_seen_at,
  MAX(created_at) AS last_seen_at
FROM order_decisions
WHERE UPPER(COALESCE(mode, '')) = 'PAPER'
  AND NULLIF(TRIM(COALESCE(signal_id, '')), '') IS NOT NULL
GROUP BY signal_id
HAVING COUNT(*) > 1 OR COUNT(DISTINCT UPPER(COALESCE(decision, ''))) > 1
ORDER BY rows_for_signal DESC, last_seen_at DESC;

-- 5) score / raw_rr / effective_rr variability (PAPER)
SELECT
  COUNT(*) AS rows_considered,
  COUNT(DISTINCT ROUND(COALESCE(score, -999999.0), 6)) AS distinct_score_values,
  COUNT(DISTINCT ROUND(COALESCE(rr, -999999.0), 6)) AS distinct_raw_rr_values,
  COUNT(DISTINCT ROUND(COALESCE(effective_rr, -999999.0), 6)) AS distinct_effective_rr_values,
  MIN(score) AS min_score,
  MAX(score) AS max_score,
  MIN(rr) AS min_raw_rr,
  MAX(rr) AS max_raw_rr,
  MIN(effective_rr) AS min_effective_rr,
  MAX(effective_rr) AS max_effective_rr
FROM order_decisions
WHERE UPPER(COALESCE(mode, '')) = 'PAPER';

-- 6) Execution-context availability on PAPER decision rows
SELECT
  COUNT(*) AS paper_rows,
  SUM(CASE WHEN execution_ctx IS NULL OR TRIM(COALESCE(execution_ctx, '')) = '' THEN 1 ELSE 0 END) AS rows_without_execution_ctx,
  SUM(CASE WHEN execution_ctx_missing = 1 THEN 1 ELSE 0 END) AS rows_flagged_execution_ctx_missing,
  ROUND(100.0 * SUM(CASE WHEN execution_ctx IS NULL OR TRIM(COALESCE(execution_ctx, '')) = '' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 2) AS no_execution_ctx_pct
FROM order_decisions
WHERE UPPER(COALESCE(mode, '')) = 'PAPER';

-- 7) Lifecycle consistency checks for PAPER
WITH paper_events AS (
  SELECT
    signal_id,
    lifecycle_state,
    event_ts,
    lifecycle_seq,
    previous_lifecycle_state
  FROM trade_lifecycle_events
  WHERE UPPER(COALESCE(mode, '')) = 'PAPER'
),
expected_states AS (
  SELECT 'SIGNAL_CREATED' AS state
  UNION ALL SELECT 'SIGNAL_VALIDATED'
  UNION ALL SELECT 'SIGNAL_REJECTED'
  UNION ALL SELECT 'WAITING_ENTRY_ZONE'
  UNION ALL SELECT 'ENTRY_TRIGGERED'
  UNION ALL SELECT 'ORDER_PLACED'
  UNION ALL SELECT 'PARTIAL_FILL'
  UNION ALL SELECT 'FILLED'
  UNION ALL SELECT 'TP_HIT'
  UNION ALL SELECT 'SL_HIT'
  UNION ALL SELECT 'CANCELLED'
  UNION ALL SELECT 'OPEN_AT_END'
)
SELECT
  COUNT(*) AS paper_lifecycle_rows,
  COUNT(DISTINCT signal_id) AS distinct_signals,
  SUM(CASE WHEN lifecycle_state IS NULL OR TRIM(COALESCE(lifecycle_state, '')) = '' THEN 1 ELSE 0 END) AS missing_lifecycle_state,
  SUM(CASE WHEN event_ts IS NULL THEN 1 ELSE 0 END) AS missing_event_ts,
  SUM(CASE WHEN lifecycle_seq IS NULL THEN 1 ELSE 0 END) AS missing_lifecycle_seq,
  SUM(
    CASE WHEN lifecycle_state IS NOT NULL
          AND UPPER(TRIM(lifecycle_state)) NOT IN (SELECT state FROM expected_states)
         THEN 1 ELSE 0 END
  ) AS unexpected_state_rows
FROM paper_events;

-- 7b) Lifecycle ordering anomalies (sequence/time monotonicity per signal)
WITH ordered AS (
  SELECT
    signal_id,
    lifecycle_state,
    lifecycle_seq,
    event_ts,
    LAG(lifecycle_seq) OVER (PARTITION BY signal_id ORDER BY lifecycle_seq, event_ts) AS prev_seq,
    LAG(event_ts) OVER (PARTITION BY signal_id ORDER BY lifecycle_seq, event_ts) AS prev_ts
  FROM trade_lifecycle_events
  WHERE UPPER(COALESCE(mode, '')) = 'PAPER'
)
SELECT
  signal_id,
  lifecycle_state,
  lifecycle_seq,
  event_ts,
  prev_seq,
  prev_ts
FROM ordered
WHERE (prev_seq IS NOT NULL AND lifecycle_seq IS NOT NULL AND lifecycle_seq < prev_seq)
   OR (prev_ts IS NOT NULL AND event_ts IS NOT NULL AND event_ts < prev_ts)
ORDER BY signal_id, lifecycle_seq, event_ts;
