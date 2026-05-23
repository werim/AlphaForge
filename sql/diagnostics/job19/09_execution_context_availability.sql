WITH final_paper AS (
  SELECT execution_ctx, execution_ctx_missing, spread_pct, expected_slippage_pct,
         latency_ms, orderbook_imbalance, funding_rate_pct, volatility_regime,
         execution_regime
  FROM order_decisions
  WHERE mode = 'PAPER'
    AND COALESCE(NULLIF(TRIM(phase), ''), 'final') = 'final'
)
SELECT COUNT(*) AS final_rows,
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
FROM final_paper;
