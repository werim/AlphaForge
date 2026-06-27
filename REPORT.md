# AlphaForge Surgery Report — Canonical Runtime Env Filters

## Why this patch was needed

`.env.example` exposed execution, decision, risk, symbol-selection, lifecycle, and BACKTEST filters, but several shared filter values were parsed only partially or consumed unevenly across BACKTEST/PAPER/LIVE paths. That made some variables effectively cosmetic unless a caller manually passed matching legacy config keys.

## Root cause

- Shared thresholds were split between `RuntimeSettings`, `RuntimeConfig`, symbol-selector defaults, order-quality defaults, and runtime-risk checks.
- `MIN_EFFECTIVE_RR`, slippage, bps spread/slippage aliases, and `MIN_LIQUIDITY_USD` were not first-class runtime settings.
- Runtime symbol selection did not pass the canonical runtime filter map into `select_symbols`.
- PAPER/LIVE runtime risk checked spread/funding/stale data but did not share the complete canonical map for slippage/liquidity/effective-RR gating.

## Files changed

- `src/alphaforge/config/__init__.py`
- `src/alphaforge/runtime.py`
- `src/alphaforge/order.py`
- `tests/test_env_filters_canonical.py`
- `tests/test_backtest_paper_pre_submit_parity.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

## Runtime behavior changes

- Added first-class runtime settings for raw RR, effective RR, expected slippage, minimum liquidity, and bps/percent spread/slippage aliases.
- Added `runtime_filter_config(...)` as the single canonical config map consumed by shared BACKTEST/PAPER decision tests and runtime symbol selection.
- Runtime scans now pass canonical spread, funding, and liquidity filters into symbol selection instead of relying on selector defaults.
- Runtime risk now emits specific reject reasons for `SPREAD_TOO_HIGH`, `SLIPPAGE_TOO_HIGH`, `FUNDING_TOO_HIGH`, `THIN_LIQUIDITY`, `STALE_MARKET_DATA`, `SYMBOL_COOLDOWN`, and `MAX_CONCURRENT_POSITIONS`.
- PAPER/LIVE_PRECHECK/LIVE accepted AI plans are still blocked before order placement if execution-adjusted RR is below canonical `min_effective_rr`.

## Lifecycle changes

- Effective-RR rejects are emitted as `SIGNAL_REJECTED` before PAPER/LIVE_PRECHECK/LIVE order-placement lifecycle transitions.
- Runtime-risk rejects continue to persist rejected decisions and lifecycle events with explicit reject reasons.

## Persistence changes

- No schema migration. Existing rejected-decision persistence receives clearer reject reasons and execution context payloads.
- Dashboard selected-backtest artifacts remain consumers of persisted/exported outcomes; no dashboard decision authority was added.

## Export/schema changes

- No database schema change.
- No CSV column removal.

## Mapping table

| ENV VAR | canonical config field | consumer function(s) | affected modes | test proving effect |
|---|---|---|---|---|
| `ALPHAFORGE_MIN_SIGNAL_SCORE` / `ALPHAFORGE_MIN_ACCEPT_SCORE` | `RuntimeSettings.min_signal_score` / `RuntimeConfig.min_signal_score` / `runtime_filter_config()["MIN_TRADE_SCORE"]` | `AIBrain(... min_accept_score=...)`, `evaluate_trade_quality` through canonical map | BACKTEST, PAPER, LIVE path | `test_env_score_threshold_changes_backtest_and_paper_decisions` |
| `ALPHAFORGE_MIN_RR` | `RuntimeSettings.min_rr` / `RuntimeConfig.min_rr` / `runtime_filter_config()["MIN_RR"]` | `evaluate_trade_quality` raw RR gate | BACKTEST, PAPER, LIVE path | `test_backtest_paper_parity_low_effective_rr` plus canonical map coverage |
| `MIN_EFFECTIVE_RR` / `ALPHAFORGE_MIN_EFFECTIVE_RR` | `RuntimeSettings.min_effective_rr` / `RuntimeConfig.min_effective_rr` / `runtime_filter_config()["MIN_EFFECTIVE_RR"]` | `_effective_rr`, `evaluate_paper_style_pre_submit`, `RuntimeOrchestrator._process_symbol` | BACKTEST, PAPER, LIVE path | `test_env_min_effective_rr_changes_shared_pre_submit_decision` |
| `ALPHAFORGE_MAX_SPREAD_PCT` / `MAX_SPREAD_PCT` / `MAX_SPREAD_BPS` | `RuntimeSettings.max_spread_pct` / `RuntimeConfig.max_spread_pct` | `select_symbols`, `evaluate_trade_quality`, `_evaluate_runtime_risk` | BACKTEST, PAPER, LIVE path | `test_env_spread_funding_liquidity_symbol_filters_are_canonical`, `test_runtime_risk_uses_canonical_spread_slippage_funding_liquidity_and_stale` |
| `ALPHAFORGE_MAX_EXPECTED_SLIPPAGE_PCT` / `MAX_EXPECTED_SLIPPAGE_PCT` / `MAX_SLIPPAGE_BPS` | `RuntimeSettings.max_expected_slippage_pct` / `RuntimeConfig.max_expected_slippage_pct` | `evaluate_trade_quality`, `_evaluate_runtime_risk`, `_effective_rr` cost evidence | BACKTEST, PAPER, LIVE path | `test_runtime_risk_uses_canonical_spread_slippage_funding_liquidity_and_stale` |
| `ALPHAFORGE_MAX_ABS_FUNDING_RATE_PCT` | `RuntimeSettings.max_abs_funding_rate_pct` / `RuntimeConfig.max_abs_funding_rate_pct` | `select_symbols`, `_evaluate_runtime_risk` | BACKTEST, PAPER, LIVE path | `test_env_spread_funding_liquidity_symbol_filters_are_canonical`, `test_runtime_risk_uses_canonical_spread_slippage_funding_liquidity_and_stale` |
| `MIN_LIQUIDITY_USD` | `RuntimeSettings.min_liquidity_usd` / `RuntimeConfig.min_liquidity_usd` | `select_symbols`, `_evaluate_runtime_risk` | BACKTEST, PAPER, LIVE path | `test_env_spread_funding_liquidity_symbol_filters_are_canonical`, `test_runtime_risk_uses_canonical_spread_slippage_funding_liquidity_and_stale` |
| `ALPHAFORGE_SYMBOL_COOLDOWN_SEC` | `RuntimeSettings.symbol_cooldown_sec` / `RuntimeConfig.symbol_cooldown_sec` / `runtime_filter_config()["SYMBOL_COOLDOWN_MINUTES"]` | `evaluate_trade_quality`, `_evaluate_runtime_risk` | BACKTEST, PAPER, LIVE path | full-suite `tests/test_trade_quality.py::test_symbol_cooldown_rejected`, `tests/test_runtime.py` |
| `ALPHAFORGE_STALE_MARKET_DATA_SEC` | `RuntimeSettings.stale_market_data_sec` / `RuntimeConfig.stale_market_data_sec` | `_evaluate_runtime_risk` | PAPER, LIVE path; BACKTEST when routed through runtime orchestrator | `test_runtime_risk_uses_canonical_spread_slippage_funding_liquidity_and_stale` |
| `ALPHAFORGE_MAX_CONCURRENT_POSITIONS` / `ALPHAFORGE_MAX_OPEN_POSITIONS` | `RuntimeSettings.max_concurrent_positions` / `RuntimeConfig.max_concurrent_positions` | `_evaluate_runtime_risk` | PAPER, LIVE path; BACKTEST runtime path | full-suite runtime coverage |
| `ALPHAFORGE_MAX_SYMBOLS_PER_SCAN` / `ALPHAFORGE_BACKTEST_TOP_N` | `RuntimeSettings.max_symbols_per_scan` / `BacktestSettings.top_n` | `RuntimeOrchestrator._scan_once`, backtest universe controls | BACKTEST/PAPER/LIVE scan path | `test_max_symbols_is_runtime_config_selection_cap` |
| `ALPHAFORGE_BACKTEST_FILTER_*` switches | `BacktestSettings.filter_switches` | `evaluate_trade_quality`, `select_symbol`, backtest scanner command path | BACKTEST only by design | existing `tests/test_backtest_filter_switches.py` |

## Tests added

- `tests/test_env_filters_canonical.py` proves score, effective RR, spread, funding, liquidity, stale-data, slippage, and max-symbol filters change real decisions/selection/runtime-risk behavior through the canonical config path.
- Updated BACKTEST/PAPER parity low-effective-RR fixture to avoid spread failing first under stricter canonical spread settings.

## Tests executed

- `python -m pytest -q tests/test_env_filters_canonical.py tests/test_backtest_paper_pre_submit_parity.py tests/test_runtime.py` — passed.
- `python -m pytest -q` — passed: 410 passed, 10 skipped.

## Risks

- Stricter canonical spread/slippage/liquidity settings can increase reject rates when runtime callers use the canonical map.
- Direct legacy calls that pass an empty config to `evaluate_trade_quality` retain compatibility defaults to avoid breaking old tests/scripts; production runtime paths should use `runtime_filter_config`.
- Historical BACKTEST execution context may still be incomplete; missing execution evidence remains flagged/fail-closed in execution evidence checks rather than silently converted to measured zero.

## Remaining limitations

- Some older legacy `.env.example` flags such as experimental orderbook/spoof/absorption toggles remain dependent on available market-data fields and are not expanded into unsafe synthetic data.
- This patch does not claim exchange execution readiness.

## Migration concerns

- No DB migration required.
- Backtest comparability can change when runs opt into canonical runtime filter maps because spread/slippage/liquidity defaults are stricter than older direct-call compatibility defaults.

## Push recommendation

- Safe to push for BACKTEST/PAPER hardening after review; LIVE remains NOT READY.
