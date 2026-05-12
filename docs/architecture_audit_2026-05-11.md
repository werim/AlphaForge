# AlphaForge architecture audit (2026-05-11)

## Root-cause snapshot

1. BACKTEST signal processing uses `run_order_cycle` (shared with PAPER/LIVE) for signal-quality rejection, but then applies an extra, backtest-local execution rejection step (`_execution_reject_flags`) that is **not** the same as PAPER/LIVE `before_real_order` execution gate.
2. Lifecycle is mostly complete in `backtest_order.py` (signal created/rejected, waiting, triggered, placed, closed), but SQL lifecycle persistence is not integrated in this CLI flow (CSV-first only).
3. `expectancy_bucket` is derived by `_bucket_expectancy`; it is only `UNKNOWN` when expectancy is missing/non-numeric. It is not hardcoded globally.
4. Backtest execution context is populated from candles + metadata when available; however, rejected CSV rows still coerce missing context to numeric defaults (`0.0`, `1.0`) in several places, which can hide data availability issues.
5. `src/alphaforge/persistence.py` contains compatibility stubs that can mask persistence wiring gaps (`save_order_decision`, `save_trade_lifecycle_event` no-op behavior).

## Code map (authoritative locations)

- Signal generation (backtest): `backtest_order._build_market_ctx`, `backtest_order.scan_symbol_backtest`
- Trade-quality scoring/reject: `src/alphaforge/order.evaluate_trade_quality`, `src/alphaforge/order.compute_adaptive_thresholds`
- AI score path (separate engine): `src/alphaforge/ai_brain.AIBrain.score_signal`
- RR source in backtest: computed in `_build_market_ctx` from breakout/body/risk geometry
- Adaptive RR thresholds: `compute_adaptive_thresholds` (min RR threshold adaptation, not TP/SL recomputation)
- Reject decision pipeline: `src/alphaforge/order.run_order_cycle` + `evaluate_trade_quality`
- Execution validation context build: `src/alphaforge/execution.build_execution_context`
- Backtest-local execution reject: `backtest_order._execution_reject_flags`
- Lifecycle events: `backtest_order.process_backtest_result`, `simulate_candidate`, `finalize`
- CSV export: `backtest_order.main` export loop for `order_lifecycle.csv`, `rejected_orders.csv`
- PAPER/LIVE flow: `src/alphaforge/order.execute_order_candidate`, and `AIBrain.before_virtual_order` / `before_real_order` hooks

## Placeholder/constant diagnosis

- `score = 0.8`: historical issue documented in `docs/backtest_lifecycle_review.md`; current path computes dynamic score in `_build_market_ctx`.
- `rr = 2.0`: historical issue documented in `docs/backtest_lifecycle_review.md`; current path computes dynamic RR in `_build_market_ctx`.
- `expectancy_bucket = UNKNOWN`: fallback default in dataclasses and `_bucket_expectancy(None)` behavior.
- `spread_pct = 0.0`: fallback in `build_execution_context` and some rejected-row serialization.
- `volume_24h_usdt = 0.0`: fallback when metadata absent (or derived from candle volume when possible).
- `funding_rate_pct = 0.0`: fallback default when funding unavailable.
- empty `reject_reason`: occurs for accepted lifecycle rows by design; rejections populate it.
- empty `cancel_reason`: only populated for timeout/cancel states; blank otherwise.

## Minimal next patch set (recommended)

1. **Unify execution rejection logic across modes**
   - Add shared execution gate helper in `src/alphaforge/order.py`.
   - Reuse it in backtest instead of `_execution_reject_flags` bespoke logic.
2. **Explicit availability markers in rejected exports**
   - Stop coercing unavailable execution fields to `0.0` in rejected rows.
   - Use explicit sentinel strings (e.g., `UNAVAILABLE_BACKTEST`) + diagnostics flag.
3. **Backtest lifecycle-to-SQL optional sink**
   - Add optional DB writer in `backtest_order.py` that mirrors lifecycle CSV rows into `trade_lifecycle_events` when DB URL/session provided.
4. **Expectancy bucket enrichment**
   - When `recent_stats` includes expectancy tables, map to numeric expectancy before bucketing; preserve `UNKNOWN` only when truly missing.
5. **Test additions (small, deterministic)**
   - Assert rejected rows preserve sentinel availability values.
   - Assert backtest execution rejection uses shared order-layer function.
   - Assert lifecycle contains `SIGNAL_REJECTED`/`ORDER_REJECTED` rows in CSV output fixture run.

## Why these are minimal

- No interface break to existing CLI outputs.
- No live API dependency added.
- No threshold loosening; only wiring consistency and observability improvements.
