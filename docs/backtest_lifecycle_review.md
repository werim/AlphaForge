# Backtest lifecycle review notes

This document captures why the current backtest order lifecycle export has mostly static values and limited lifecycle stages.

- `backtest_order.scan_symbol_backtest` seeds `market_ctx` with hardcoded values (`score=0.8`, `rr=2.0`, `expectancy=0.1`, `regime='TREND'`).
- `backtest_order.simulate_candidate` emits a single final lifecycle row per accepted candidate using `status_before='CREATED'` and final outcome statuses (`TP_HIT`, `SL_HIT`, `OPEN_AT_END`, `TIMEOUT`).
- Rejections are exported to `rejected_orders.csv`, not `order_lifecycle.csv`.
- Backtest path uses `run_order_cycle` (lightweight quality gate), not `before_virtual_order` / `before_real_order` (AI persistence/execution-aware path).

## Minimal patch direction

1. Replace static `market_ctx` fields in `scan_symbol_backtest` with computed per-candle features, or call a shared signal builder that PAPER/LIVE also uses.
2. Emit lifecycle events (signal created/rejected, waiting, triggered, placed, closed) into `order_lifecycle.csv` instead of only terminal outcomes.
3. Merge rejected rows into lifecycle export (with `status_after=SIGNAL_REJECTED`, `reject_reason` populated).
4. Populate `expectancy_bucket` from expectancy stats lookup if available; otherwise explicit sentinel like `UNAVAILABLE_BACKTEST`.
5. Populate execution context columns (`volume_24h_usdt`, `spread_pct`, `funding_rate_pct`) from fetched market data or explicit unavailable sentinel.
