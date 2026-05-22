-- JOB19: PAPER Runtime Reject-Rate Integrity and Decision-Quality Audit
-- Purpose: diagnose decision quality without changing thresholds, score/RR logic, or runtime behavior.
-- Target: SQLite DB bootstrapped by src/alphaforge/persistence.py on current dev.
--
-- Default scope: all persisted PAPER rows. To audit a single completed run, replace the
-- NULL parameters below with inclusive ISO-8601 timestamps from that session.
-- Example: VALUES ('2026-05-21T19:00:00+00:00', '2026-05-21T23:59:59+00:00');

.headers on
.mode column

DROP TABLE IF EXISTS temp.job19_parameters;
CREATE TEMP TABLE job19_parameters (
    start_ts TEXT,
    end_ts TEXT
);
INSERT INTO job19_parameters(start_ts, end_ts) VALUES (NULL, NULL);

DROP VIEW IF EXISTS temp.job19_paper_decisions_all_layers;
CREATE TEMP VIEW job19_paper_decisions_all_layers AS
SELECT d.*
FROM order_decisions AS d
CROSS JOIN job19_parameters AS p
WHERE UPPER(COALESCE(d.mode, '')) = 'PAPER'
  AND (p.start_ts IS NULL OR d.created_at >= p.start_ts)
  AND (p.end_ts IS NULL OR d.created_at <= p.end_ts);

DROP VIEW IF EXISTS temp.job19_paper_final_decisions;
CREATE TEMP VIEW job19_paper_final_decisions AS
SELECT *
FROM job19_paper_decisions_all_layers
WHERE LOWER(COALESCE(NULLIF(TRIM(phase), ''), 'final')) = 'final';

DROP VIEW IF EXISTS temp.job19_paper_lifecycle;
CREATE TEMP VIEW job19_paper_lifecycle AS
SELECT e.*,
       UPPER(COALESCE(NULLIF(TRIM(e.lifecycle_state), ''), NULLIF(TRIM(e.state), ''), e.event_type, '')) AS normalized_state
FROM trade_lifecycle_events AS e
CROSS JOIN job19_parameters AS p
WHERE UPPER(COALESCE(e.mode, '')) = 'PAPER'
  AND (p.start_ts IS NULL OR e.created_at >= p.start_ts)
  AND (p.end_ts IS NULL OR e.created_at <= p.end_ts);

SELECT '00_SCOPE' AS audit_section,
       (SELECT start_ts FROM job19_parameters) AS requested_start_ts,
       (SELECT end_ts FROM job19_parameters) AS requested_end_ts,
       COUNT(*) AS all_layer_rows,
       SUM(CASE WHEN LOWER(COALESCE(NULLIF(TRIM(phase), ''), 'final')) = 'final' THEN 1 ELSE 0 END) AS canonical_final_rows,
       MIN(created_at) AS first_paper_decision_ts,
       MAX(created_at) AS last_paper_decision_ts
FROM job19_paper_decisions_all_layers;

SELECT '01_PHASE_DECOMPOSITION' AS audit_section,
       COALESCE(NULLIF(TRIM(phase), ''), 'final') AS phase,
       decision,
       COUNT(*) AS rows
FROM job19_paper_decisions_all_layers
GROUP BY COALESCE(NULLIF(TRIM(phase), ''), 'final'), decision
ORDER BY rows DESC, phase, decision;

SELECT '02_CANONICAL_DECISION_TOTALS' AS audit_section,
       COUNT(*) AS total_final_decisions,
       SUM(CASE WHEN UPPER(COALESCE(decision, '')) = 'ACCEPTED' THEN 1 ELSE 0 END) AS accepted_count,
       SUM(CASE WHEN UPPER(COALESCE(decision, '')) = 'REJECTED' THEN 1 ELSE 0 END) AS rejected_count,
       ROUND(
           1.0 * SUM(CASE WHEN UPPER(COALESCE(decision, '')) = 'REJECTED' THEN 1 ELSE 0 END)
           / NULLIF(COUNT(*), 0),
           6
       ) AS rejection_rate
FROM job19_paper_final_decisions;

SELECT '03_FINAL_DECISIONS_BY_SYMBOL' AS audit_section,
       symbol,
       decision,
       COUNT(*) AS rows
FROM job19_paper_final_decisions
GROUP BY symbol, decision
ORDER BY rows DESC, symbol, decision;

SELECT '04_REJECT_REASON_QUALITY' AS audit_section,
       CASE
           WHEN reject_reason IS NULL OR TRIM(reject_reason) = '' THEN '<EMPTY>'
           WHEN UPPER(TRIM(reject_reason)) = 'UNKNOWN' THEN '<UNKNOWN>'
           ELSE TRIM(reject_reason)
       END AS reject_reason_class,
       COUNT(*) AS rejected_rows,
       ROUND(1.0 * COUNT(*) / NULLIF((SELECT COUNT(*) FROM job19_paper_final_decisions WHERE UPPER(COALESCE(decision, '')) = 'REJECTED'), 0), 6) AS share_of_rejects
FROM job19_paper_final_decisions
WHERE UPPER(COALESCE(decision, '')) = 'REJECTED'
GROUP BY reject_reason_class
ORDER BY rejected_rows DESC, reject_reason_class;

SELECT '05_REJECTED_FIELD_COMPLETENESS' AS audit_section,
       COUNT(*) AS rejected_rows,
       SUM(CASE WHEN decision_id IS NULL OR TRIM(decision_id) = '' THEN 1 ELSE 0 END) AS missing_decision_id,
       SUM(CASE WHEN signal_id IS NULL OR TRIM(signal_id) = '' THEN 1 ELSE 0 END) AS missing_signal_id,
       SUM(CASE WHEN symbol IS NULL OR TRIM(symbol) = '' THEN 1 ELSE 0 END) AS missing_symbol,
       SUM(CASE WHEN mode IS NULL OR TRIM(mode) = '' THEN 1 ELSE 0 END) AS missing_mode,
       SUM(CASE WHEN reject_reason IS NULL OR TRIM(reject_reason) = '' OR UPPER(TRIM(reject_reason)) = 'UNKNOWN' THEN 1 ELSE 0 END) AS empty_or_unknown_reason,
       SUM(CASE WHEN score IS NULL THEN 1 ELSE 0 END) AS missing_score,
       SUM(CASE WHEN rr IS NULL THEN 1 ELSE 0 END) AS missing_rr,
       SUM(CASE WHEN effective_rr IS NULL THEN 1 ELSE 0 END) AS missing_effective_rr,
       SUM(CASE WHEN expectancy_bucket IS NULL OR TRIM(expectancy_bucket) = '' OR UPPER(TRIM(expectancy_bucket)) = 'UNKNOWN' THEN 1 ELSE 0 END) AS empty_or_unknown_expectancy_bucket,
       SUM(CASE WHEN created_at IS NULL OR TRIM(created_at) = '' THEN 1 ELSE 0 END) AS missing_created_at
FROM job19_paper_final_decisions
WHERE UPPER(COALESCE(decision, '')) = 'REJECTED';

SELECT '06_DUPLICATE_DECISION_ID' AS audit_section,
       decision_id,
       COUNT(*) AS duplicate_rows
FROM job19_paper_decisions_all_layers
WHERE decision_id IS NOT NULL AND TRIM(decision_id) <> ''
GROUP BY decision_id
HAVING COUNT(*) > 1
ORDER BY duplicate_rows DESC, decision_id;

SELECT '07_CONFLICTING_FINAL_SIGNAL_DECISIONS' AS audit_section,
       signal_id,
       COUNT(*) AS final_rows,
       COUNT(DISTINCT UPPER(COALESCE(decision, ''))) AS distinct_decisions,
       GROUP_CONCAT(DISTINCT UPPER(COALESCE(decision, ''))) AS decisions
FROM job19_paper_final_decisions
WHERE signal_id IS NOT NULL AND TRIM(signal_id) <> ''
GROUP BY signal_id
HAVING COUNT(DISTINCT UPPER(COALESCE(decision, ''))) > 1
    OR COUNT(*) > 1
ORDER BY final_rows DESC, signal_id;

SELECT '08_SCORE_RR_EFFECTIVE_RR_VARIABILITY' AS audit_section,
       COUNT(*) AS final_rows,
       COUNT(DISTINCT score) AS distinct_score_values,
       MIN(score) AS min_score,
       MAX(score) AS max_score,
       ROUND(AVG(score), 8) AS avg_score,
       COUNT(DISTINCT rr) AS distinct_rr_values,
       MIN(rr) AS min_rr,
       MAX(rr) AS max_rr,
       ROUND(AVG(rr), 8) AS avg_rr,
       COUNT(DISTINCT effective_rr) AS distinct_effective_rr_values,
       MIN(effective_rr) AS min_effective_rr,
       MAX(effective_rr) AS max_effective_rr,
       ROUND(AVG(effective_rr), 8) AS avg_effective_rr,
       CASE WHEN COUNT(*) > 1 AND COUNT(DISTINCT score) <= 1 THEN 'FLAG_CONSTANT_SCORE' ELSE 'OK_OR_INSUFFICIENT_SAMPLE' END AS score_variability_flag,
       CASE WHEN COUNT(*) > 1 AND COUNT(DISTINCT rr) <= 1 THEN 'OBSERVE_CONSTANT_RR' ELSE 'OK_OR_INSUFFICIENT_SAMPLE' END AS rr_variability_flag
FROM job19_paper_final_decisions;

SELECT '09_DIRECT_EXECUTION_CONTEXT_AVAILABILITY' AS audit_section,
       COUNT(*) AS final_rows,
       SUM(CASE WHEN execution_ctx_missing = 1 THEN 1 ELSE 0 END) AS explicitly_missing_execution_ctx,
       SUM(CASE WHEN execution_ctx IS NULL OR TRIM(execution_ctx) = '' THEN 1 ELSE 0 END) AS blank_execution_ctx,
       SUM(CASE WHEN spread_pct IS NULL THEN 1 ELSE 0 END) AS null_spread_pct,
       SUM(CASE WHEN spread_pct = 0 THEN 1 ELSE 0 END) AS zero_spread_pct_needs_validation,
       SUM(CASE WHEN expected_slippage_pct IS NULL THEN 1 ELSE 0 END) AS null_expected_slippage_pct,
       SUM(CASE WHEN expected_slippage_pct = 0 THEN 1 ELSE 0 END) AS zero_expected_slippage_needs_validation,
       SUM(CASE WHEN latency_ms IS NULL THEN 1 ELSE 0 END) AS null_latency_ms,
       SUM(CASE WHEN latency_ms = 0 THEN 1 ELSE 0 END) AS zero_latency_needs_validation,
       SUM(CASE WHEN orderbook_imbalance IS NULL THEN 1 ELSE 0 END) AS null_orderbook_imbalance,
       SUM(CASE WHEN funding_rate_pct IS NULL THEN 1 ELSE 0 END) AS null_funding_rate_pct,
       SUM(CASE WHEN volatility_regime IS NULL OR TRIM(volatility_regime) = '' THEN 1 ELSE 0 END) AS missing_volatility_regime,
       SUM(CASE WHEN execution_regime IS NULL OR TRIM(execution_regime) = '' THEN 1 ELSE 0 END) AS missing_execution_regime
FROM job19_paper_final_decisions;

-- JSON execution context is used only for fields that are not first-class columns on order_decisions.
-- Zero is reported separately, never assumed to be a measured valid value.
SELECT '10_JSON_EXECUTION_CONTEXT_AVAILABILITY' AS audit_section,
       COUNT(*) AS rows_with_valid_json_context,
       SUM(CASE WHEN json_type(execution_ctx, '$.volume_24h_usdt') IS NULL THEN 1 ELSE 0 END) AS missing_volume_24h_usdt,
       SUM(CASE WHEN json_extract(execution_ctx, '$.volume_24h_usdt') = 0 THEN 1 ELSE 0 END) AS zero_volume_needs_validation,
       SUM(CASE WHEN json_type(execution_ctx, '$.liquidity_score') IS NULL THEN 1 ELSE 0 END) AS missing_liquidity_score,
       SUM(CASE WHEN json_type(execution_ctx, '$.volatility_pct') IS NULL THEN 1 ELSE 0 END) AS missing_volatility_pct,
       SUM(CASE WHEN json_type(execution_ctx, '$.market_ts') IS NULL THEN 1 ELSE 0 END) AS missing_market_ts
FROM job19_paper_final_decisions
WHERE json_valid(COALESCE(execution_ctx, '')) = 1;

SELECT '11_LIFECYCLE_STATE_DISTRIBUTION' AS audit_section,
       normalized_state,
       COUNT(*) AS rows
FROM job19_paper_lifecycle
GROUP BY normalized_state
ORDER BY rows DESC, normalized_state;

SELECT '12_REJECTED_WITHOUT_REJECTION_EVENT' AS audit_section,
       d.signal_id,
       d.symbol,
       d.decision_id,
       d.reject_reason
FROM job19_paper_final_decisions AS d
WHERE UPPER(COALESCE(d.decision, '')) = 'REJECTED'
  AND NOT EXISTS (
      SELECT 1
      FROM job19_paper_lifecycle AS e
      WHERE e.signal_id = d.signal_id
        AND e.normalized_state IN ('SIGNAL_REJECTED', 'ORDER_REJECTED')
  )
ORDER BY d.created_at, d.signal_id;

SELECT '13_ORDER_PLACED_WITHOUT_ACCEPTED_FINAL_DECISION' AS audit_section,
       e.signal_id,
       e.symbol,
       e.order_id,
       e.created_at
FROM job19_paper_lifecycle AS e
WHERE e.normalized_state = 'ORDER_PLACED'
  AND NOT EXISTS (
      SELECT 1
      FROM job19_paper_final_decisions AS d
      WHERE d.signal_id = e.signal_id
        AND UPPER(COALESCE(d.decision, '')) = 'ACCEPTED'
  )
ORDER BY e.created_at, e.signal_id;

SELECT '14_TERMINAL_STATE_WITHOUT_ORDER_PLACED' AS audit_section,
       e.signal_id,
       e.symbol,
       e.normalized_state,
       e.created_at
FROM job19_paper_lifecycle AS e
WHERE e.normalized_state IN ('TP_HIT', 'SL_HIT', 'CLOSED', 'CANCELLED', 'ENTRY_TIMEOUT')
  AND NOT EXISTS (
      SELECT 1
      FROM job19_paper_lifecycle AS p
      WHERE p.signal_id = e.signal_id
        AND p.normalized_state = 'ORDER_PLACED'
  )
ORDER BY e.created_at, e.signal_id;

SELECT '15_VERDICT_INPUT_COUNTS' AS audit_section,
       (SELECT COUNT(*) FROM job19_paper_final_decisions) AS canonical_final_decisions,
       (SELECT COUNT(*) FROM job19_paper_final_decisions WHERE UPPER(COALESCE(decision, '')) = 'REJECTED') AS rejected_decisions,
       (SELECT COUNT(*) FROM job19_paper_final_decisions WHERE UPPER(COALESCE(decision, '')) = 'REJECTED' AND (reject_reason IS NULL OR TRIM(reject_reason) = '' OR UPPER(TRIM(reject_reason)) = 'UNKNOWN')) AS rejected_with_bad_reason,
       (SELECT COUNT(*) FROM job19_paper_final_decisions WHERE execution_ctx_missing = 1 OR execution_ctx IS NULL OR TRIM(execution_ctx) = '') AS decisions_with_missing_execution_context,
       (SELECT COUNT(*) FROM job19_paper_final_decisions WHERE signal_id IS NULL OR TRIM(signal_id) = '') AS decisions_with_missing_signal_id,
       (SELECT COUNT(*) FROM job19_paper_final_decisions WHERE effective_rr IS NULL) AS decisions_with_missing_effective_rr;
