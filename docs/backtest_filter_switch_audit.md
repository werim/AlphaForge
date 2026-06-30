# BACKTEST Filter Switch Audit

Date: 2026-06-30

## Root cause

A filters-off dashboard run is not a strategy-quality run. It is a damage diagnostic. Disabling optional BACKTEST filters can let many weak candidates through while always-on hard safety gates still reject invalid, negative-expectancy, or execution-impossible setups. The previous dashboard made the switches real but did not export enough machine-readable evidence to prove which switches were optional, which gates remained non-disableable, or whether the page was showing `ALL_OFF`, `DEFAULT`, or `CUSTOM` behavior.

## Optional BACKTEST switch mapping

| Filter | Env var | Dashboard field | Internal flag | Reject reason(s) | Applied in | Disable truly bypasses? | PAPER/LIVE impact |
|---|---|---|---|---|---|---|---|
| LOW_SCORE | `ALPHAFORGE_BACKTEST_FILTER_LOW_SCORE_ENABLED` | `filter_LOW_SCORE` | `DISABLED_BACKTEST_FILTERS` / `disabled_backtest_filters` | `LOW_SCORE` | `src/alphaforge/order.py:evaluate_trade_quality` | Yes, BACKTEST only | None |
| TOO_CHOPPY | `ALPHAFORGE_BACKTEST_FILTER_TOO_CHOPPY_ENABLED` | `filter_TOO_CHOPPY` | `disabled_backtest_filters` | `TOO_CHOPPY` | `src/alphaforge/symbol_selector.py:select_symbol` | Yes, BACKTEST selector only | None |
| WEAK_TREND_AND_NO_RANGE_EDGE | `ALPHAFORGE_BACKTEST_FILTER_WEAK_TREND_NO_RANGE_ENABLED` | `filter_WEAK_TREND_AND_NO_RANGE_EDGE` | `disabled_backtest_filters` | `WEAK_TREND_AND_NO_RANGE_EDGE` | `src/alphaforge/symbol_selector.py:select_symbol` | Yes, BACKTEST selector only | None |
| STOP_TOO_WIDE | `ALPHAFORGE_BACKTEST_FILTER_STOP_TOO_WIDE_ENABLED` | `filter_STOP_TOO_WIDE` | `DISABLED_BACKTEST_FILTERS` / `disabled_backtest_filters` | `STOP_TOO_WIDE` | `src/alphaforge/order.py:evaluate_trade_quality` | Yes, BACKTEST only | None |
| RR_TOO_LOW | `ALPHAFORGE_BACKTEST_FILTER_RR_TOO_LOW_ENABLED` | `filter_RR_TOO_LOW` | `DISABLED_BACKTEST_FILTERS` / `disabled_backtest_filters` | `RR_TOO_LOW` | `src/alphaforge/order.py:evaluate_trade_quality` | Yes, BACKTEST only | None |
| DAILY_SYMBOL_TRADE_LIMIT | `ALPHAFORGE_BACKTEST_FILTER_DAILY_SYMBOL_TRADE_LIMIT_ENABLED` | `filter_DAILY_SYMBOL_TRADE_LIMIT` | `DISABLED_BACKTEST_FILTERS` / `disabled_backtest_filters` | `DAILY_SYMBOL_TRADE_LIMIT` | `src/alphaforge/order.py:evaluate_trade_quality` | Yes, BACKTEST only | None |
| REGIME_MISMATCH | `ALPHAFORGE_BACKTEST_FILTER_REGIME_MISMATCH_ENABLED` | `filter_REGIME_MISMATCH` | `DISABLED_BACKTEST_FILTERS` / `disabled_backtest_filters` | `REGIME_MISMATCH` | `src/alphaforge/order.py:evaluate_trade_quality` | Yes, BACKTEST only | None |
| PANIC_CONDITIONS | `ALPHAFORGE_BACKTEST_FILTER_PANIC_CONDITIONS_ENABLED` | `filter_PANIC_CONDITIONS` | `disabled_backtest_filters` | `PANIC_CONDITIONS` | `src/alphaforge/symbol_selector.py:select_symbol` | Yes, BACKTEST selector only | None |

Naming note: the dashboard switch is `DAILY_SYMBOL_TRADE_LIMIT`. `DAILY_GLOBAL_TRADE_LIMIT` remains a runtime gate when runtime limits are active and is not controlled by the symbol-limit switch.

## Always-on hard safety gates

These remain active even when all optional BACKTEST switches are disabled:

- `NEGATIVE_EXPECTANCY`
- `EXPECTANCY_MISSING`
- invalid candidate / missing reject reason safeguards
- execution and cost sanity rejects such as low effective RR, excessive spread/slippage, and volatility sanity failures
- order-geometry failures such as too-tight stops or impossible entry/SL/TP construction
- `DAILY_GLOBAL_TRADE_LIMIT` when runtime limits are active

## New artifacts

Every backtest now writes:

- `backtest_filter_state.json`
- `backtest_filter_state.csv`
- `backtest_filter_profile_comparison.json`
- `accepted_trade_loss_diagnostics.json`
- `accepted_trade_loss_diagnostics.csv`

`backtest_filter_state.json` includes filter profile, enabled/disabled optional filters, hard safety gates, source, symbols, timeframe, last-days window, and an all-off warning when applicable.

## Interpretation of all-off evidence

The referenced 30-day BTCUSDT/ETHUSDT 1h run accepted far more trades and lost heavily after costs. That is evidence that the disabled optional filters are protective in aggregate; it is not evidence to loosen thresholds. Score=10 saturation remains diagnostic-only until default, all-off, and custom filter profiles are compared over 30/90/180/365 days.

## LIVE readiness

LIVE remains **NOT READY**. This patch changes BACKTEST diagnostics and artifacts only. It does not add live order paths and does not modify PAPER/LIVE switch semantics.
