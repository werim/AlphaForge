SELECT COUNT(*) AS rows_with_valid_json_context,
       SUM(CASE WHEN json_type(execution_ctx, '$.volume_24h_usdt') IS NULL THEN 1 ELSE 0 END) AS missing_volume_24h_usdt,
       SUM(CASE WHEN json_extract(execution_ctx, '$.volume_24h_usdt') = 0 THEN 1 ELSE 0 END) AS zero_volume_needs_validation,
       SUM(CASE WHEN json_type(execution_ctx, '$.liquidity_score') IS NULL THEN 1 ELSE 0 END) AS missing_liquidity_score,
       SUM(CASE WHEN json_type(execution_ctx, '$.volatility_pct') IS NULL THEN 1 ELSE 0 END) AS missing_volatility_pct,
       SUM(CASE WHEN json_type(execution_ctx, '$.market_ts') IS NULL THEN 1 ELSE 0 END) AS missing_market_ts
FROM order_decisions
WHERE mode = 'PAPER'
  AND COALESCE(NULLIF(TRIM(phase), ''), 'final') = 'final'
  AND json_valid(COALESCE(execution_ctx, '')) = 1;
