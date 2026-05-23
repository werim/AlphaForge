WITH final_paper AS (
  SELECT score, rr, effective_rr
  FROM order_decisions
  WHERE mode = 'PAPER'
    AND COALESCE(NULLIF(TRIM(phase), ''), 'final') = 'final'
)
SELECT COUNT(*) AS final_rows,
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
FROM final_paper;
