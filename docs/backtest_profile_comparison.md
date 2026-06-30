# BACKTEST Profile Comparison Runner

AlphaForge now supports an optional **BACKTEST-only** profile comparison runner from the dashboard. Default dashboard backtests remain single-profile unless `Run profile comparison` is selected.

## Profiles

- `DEFAULT_FILTERS`: default BACKTEST optional filters.
- `ALL_FILTERS_OFF`: disables optional BACKTEST filters while hard safety gates remain active. This is a **diagnostic stress test, not strategy performance**.
- `STRICT_FILTERS`: all optional BACKTEST filters enabled.
- `CUSTOM_CURRENT_UI`: current dashboard filter checkbox state.
- `SCORE_SATURATION_GUARD_DIAGNOSTIC`: default filters with diagnostic labeling for score saturation review.
- `STOP_WIDTH_GUARD_DIAGNOSTIC`: default filters with diagnostic labeling for stop-width review.
- `TRADE_FREQUENCY_GUARD_DIAGNOSTIC`: default filters plus exported diagnostic throttle labels for max 1/2/3 trades per day and pause-after-2-SL review. The scaffold is diagnostic and does not change default thresholds.

Each profile runs under `data/backtest/dashboard/<run_id>/profiles/<profile>/` over the same symbols, timeframe, date window, balance, max-symbol limit, and historical data source selected by the dashboard request.

## Artifacts

Comparison mode writes:

- `backtest_filter_profile_comparison.json`
- `backtest_profile_leaderboard.json`
- `backtest_profile_leaderboard.csv`
- per-profile runner artifacts under `profiles/<profile>/`

The comparison JSON includes candidates, accepted trades, rejected signals, reject rate, win/loss/open counts, net PnL, return, drawdown availability, loss streaks, profit factor, expectancy, trades/day, effective-RR distributions, score=10 outcome split, reject reasons, objective components, warnings, artifact paths, and bucket-level net-PnL diagnostics.

## Objective score

The objective score is exported as components:

```text
objective_score = net_pnl
  - max_drawdown_penalty
  - loss_streak_penalty
  - overtrade_penalty
  - execution_cost_penalty
  - low_sample_penalty
```

If max drawdown is unavailable, AlphaForge marks `max_drawdown_status = UNAVAILABLE`, emits `DRAWDOWN_UNAVAILABLE`, and uses a documented zero drawdown fallback penalty rather than fabricating drawdown.

## Multi-window scaffold

The artifact includes 30/90/180/365-day window statuses. The selected dashboard window is marked `RUN`; other windows are marked `NOT_RUN` until a future multi-window scheduler executes them.

## Safety

Profile comparison is BACKTEST-only. It does not add PAPER/LIVE order paths, does not change default thresholds, does not optimize for accepted trade count, and does not imply LIVE readiness. LIVE remains **NOT READY**.
