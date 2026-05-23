-- JOB19 PAPER Runtime Reject-Rate and Decision-Quality Canonical Summary
-- Read-only entrypoint for run_sql_audits.py. Detailed component reports remain
-- under sql/diagnostics/job19/. Do not add sqlite3 dot commands to this file.
WITH final_decisions AS (
    SELECT *
    FROM order_decisions
    WHERE UPPER(COALESCE(mode, '')) = 'PAPER'
      AND COALESCE(phase, 'final') = 'final'
),
decision_stats AS (
    SELECT
        COUNT(*) AS final_decisions,
        SUM(CASE WHEN UPPER(COALESCE(decision, '')) = 'REJECTED' THEN 1 ELSE 0 END) AS rejected_decisions,
        SUM(CASE WHEN UPPER(COALESCE(decision, '')) IN ('ACCEPTED', 'EXECUTED') THEN 1 ELSE 0 END) AS accepted_decisions,
        SUM(CASE WHEN signal_id IS NULL OR TRIM(signal_id) = '' OR symbol IS NULL OR TRIM(symbol) = '' THEN 1 ELSE 0 END) AS missing_identity_rows,
        SUM(CASE WHEN UPPER(COALESCE(decision, '')) = 'REJECTED' AND (reject_reason IS NULL OR TRIM(reject_reason) = '' OR UPPER(TRIM(reject_reason)) = 'UNKNOWN') THEN 1 ELSE 0 END) AS invalid_reject_reason_rows,
        SUM(CASE WHEN COALESCE(execution_ctx_missing, 0) = 1 OR execution_ctx IS NULL OR TRIM(execution_ctx) IN ('', '{}') THEN 1 ELSE 0 END) AS execution_context_unverified_rows,
        COUNT(DISTINCT ROUND(score, 6)) AS distinct_score_values,
        COUNT(DISTINCT ROUND(rr, 6)) AS distinct_raw_rr_values,
        COUNT(DISTINCT ROUND(effective_rr, 6)) AS distinct_effective_rr_values,
        MIN(score) AS min_score,
        MAX(score) AS max_score,
        MIN(rr) AS min_raw_rr,
        MAX(rr) AS max_raw_rr,
        MIN(effective_rr) AS min_effective_rr,
        MAX(effective_rr) AS max_effective_rr
    FROM final_decisions
),
lifecycle_stats AS (
    SELECT
        COUNT(*) AS lifecycle_rows,
        SUM(CASE WHEN lifecycle_state = 'ERROR' THEN 1 ELSE 0 END) AS lifecycle_error_rows,
        SUM(CASE WHEN lifecycle_state IN ('SIGNAL_REJECTED', 'ORDER_REJECTED') THEN 1 ELSE 0 END) AS lifecycle_reject_rows
    FROM trade_lifecycle_events
    WHERE UPPER(COALESCE(mode, '')) = 'PAPER'
),
rejected_without_lifecycle AS (
    SELECT COUNT(*) AS rows_missing_reject_lifecycle
    FROM final_decisions d
    LEFT JOIN trade_lifecycle_events l
      ON l.signal_id = d.signal_id
     AND UPPER(COALESCE(l.mode, '')) = 'PAPER'
     AND l.lifecycle_state IN ('SIGNAL_REJECTED', 'ORDER_REJECTED')
    WHERE UPPER(COALESCE(d.decision, '')) = 'REJECTED'
      AND l.signal_id IS NULL
)
SELECT
    'JOB19_CANONICAL_PAPER_SUMMARY' AS diagnostic,
    decision_stats.*,
    ROUND(100.0 * decision_stats.rejected_decisions / NULLIF(decision_stats.final_decisions, 0), 4) AS reject_rate_pct,
    lifecycle_stats.lifecycle_rows,
    lifecycle_stats.lifecycle_error_rows,
    ROUND(100.0 * lifecycle_stats.lifecycle_error_rows / NULLIF(lifecycle_stats.lifecycle_rows, 0), 4) AS lifecycle_error_pct,
    lifecycle_stats.lifecycle_reject_rows,
    rejected_without_lifecycle.rows_missing_reject_lifecycle,
    CASE
      WHEN decision_stats.final_decisions < 30 THEN 'INSUFFICIENT_SAMPLE'
      WHEN decision_stats.missing_identity_rows > 0 OR decision_stats.invalid_reject_reason_rows > 0 THEN 'DATA_INTEGRITY_FAILURE'
      WHEN lifecycle_stats.lifecycle_rows = 0 OR lifecycle_stats.lifecycle_error_rows > 0 OR rejected_without_lifecycle.rows_missing_reject_lifecycle > 0 THEN 'LIFECYCLE_INTEGRITY_FAILURE'
      WHEN decision_stats.execution_context_unverified_rows > 0 THEN 'EXECUTION_CONTEXT_UNVERIFIED'
      WHEN decision_stats.accepted_decisions = 0 THEN 'NO_ACCEPTED_SAMPLE'
      WHEN decision_stats.distinct_score_values <= 1 OR decision_stats.distinct_effective_rr_values <= 1 THEN 'SCORING_OR_RR_PIPELINE_SUSPECT'
      ELSE 'HEALTHY_SELECTIVITY_CANDIDATE'
    END AS suggested_classification
FROM decision_stats, lifecycle_stats, rejected_without_lifecycle;
