# AlphaForge Surgery Report — Accepted Diagnostics and Score Calibration Guardrails

## Why this patch was needed

The latest dashboard backtest showed negative expectancy despite a very high reject rate. Accepted Trade Diagnostics were also reporting core fields as `None`, so the dashboard could not prove whether accepted rows were connected to lifecycle/order artifacts or whether closed trades had exported PnL. Score=10 rows were mostly losing in shadow/accepted outcomes, so the immediate priority was diagnostics and calibration safeguards, not accepting more trades.

## Root cause

- Accepted-trade lifecycle persistence stored execution details inside `execution_ctx`, but the BACKTEST export query did not extract side, entry, SL, TP, exit, close reason, gross/net PnL, or fee evidence back into `order_lifecycle.csv`.
- Dashboard accepted diagnostics merged lifecycle rows by signal, but a later close row could replace the earlier execution context, dropping geometry captured on the entry/waiting row.
- Dashboard calibration artifacts did not expose score-bucket TP/SL saturation or DAILY_GLOBAL_TRADE_LIMIT near-miss context clearly enough to avoid blindly relaxing LOW_SCORE or daily limits.

## Files changed

- `backtest_order.py`
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_backtest_diagnostics_calibration.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

## Runtime behavior changes

- BACKTEST lifecycle exports now carry accepted trade geometry and realized outcome evidence when available in the real simulated lifecycle row.
- Dashboard accepted diagnostics now preserve merged execution context across entry and close lifecycle rows instead of allowing close-only context to erase geometry.
- Added diagnostic-only score saturation output and a disabled-by-default guardrail proposal for poor score-bucket calibration.
- Added diagnostic-only DAILY_GLOBAL_TRADE_LIMIT near-miss rows showing shadow outcome and same-day accepted trade context.

## Lifecycle changes

- No lifecycle state machine semantics changed.
- Accepted diagnostics remain derived from accepted lifecycle/order artifact rows and do not synthesize trades.

## Persistence changes

- No SQLite schema migration.
- Additional accepted trade evidence is stored/exported through existing `execution_ctx` payloads and JSON extraction.

## Export/schema changes

- `order_lifecycle.csv` gains additive extracted fields when available: side, entry, sl, tp, exit_price, close_price, close_reason, gross_pnl, net_pnl, net_pnl_usdt, fees.
- `lifecycle_calibration_summary.json` gains `score_saturation_diagnostics`, `daily_global_trade_limit_diagnostics`, and `dynamic_trade_limit_proposal`.
- Accepted diagnostics include aliases for `stop_loss`, `take_profit`, `exit_price`, plus gross PnL and fee/cost evidence.

## Tests added

- `tests/test_backtest_diagnostics_calibration.py` verifies accepted diagnostics are connected to lifecycle evidence, closed trades export net PnL status, score saturation appears, DAILY_GLOBAL_TRADE_LIMIT near-miss diagnostics export, and dynamic trade-limit behavior stays disabled by default.

## Tests executed

- `pytest -q tests/test_backtest_diagnostics_calibration.py` — passed.
- `pytest -q tests/test_dashboard_app.py::test_accepted_trade_diagnostics_completes_geometry_and_net_pnl_status ...` — skipped/collector unavailable because FastAPI is not installed in this container.

## Risks

- Existing historical artifacts that genuinely lack lifecycle/order evidence will still show unavailable fields; this is intentional and avoids fake diagnostics.
- Score-bucket diagnostics are descriptive, not sufficient proof to alter thresholds. LOW_SCORE remains unchanged.
- High effective RR alone is not enough because provided evidence shows high effective-RR near-misses and many score=10 rows still skew WOULD_SL after execution costs.

## Remaining limitations

- Score de-saturation and dynamic daily trade-limit rules are proposal-only and require additional out-of-sample evidence before enabling.
- Symbol/session correlation exposure is listed as a proposal requirement but not enforced by default.

## Migration concerns

- Additive CSV/JSON fields only; no breaking schema migration.

## Push recommendation

- Safe to push for BACKTEST/dashboard diagnostics. Do not use this as LIVE readiness evidence.

---

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

## 2026-06-27 - Mode-aware Configuration Surgery Report

### Why needed
Runtime/session risk caps were mixed into shared order-quality defaults, so changing PAPER/LIVE limits could alter BACKTEST decisions.

### Root cause
`evaluate_trade_quality` built a local hardcoded threshold dictionary that included both trade-quality filters and runtime risk limits. Environment parsing was split between config, runtime, order logic, and dashboard forms.

### Files changed
- `src/alphaforge/config_registry.py` adds the typed registry, effective source resolution, dashboard override writer, and config snapshots.
- `src/alphaforge/config/__init__.py` consumes the registry for runtime/backtest config.
- `src/alphaforge/order.py` consumes typed decision filters and disables runtime limits for BACKTEST by default.
- `src/alphaforge/dashboard/app.py` and templates add Settings.
- `.env.example`, `.gitignore`, README, tests updated.

### Runtime behavior
BACKTEST uses shared quality filters and BACKTEST-specific caps. PAPER/LIVE keep runtime/session caps and live qualification guards.

### Persistence/export
Dashboard overrides are local-only in `config/runtime_overrides.json`; backtest dashboard runs export `config_snapshot.json` when enabled.

### Tests executed
Narrow registry, dashboard, order, and backtest isolation tests were added/executed. Full-suite execution remains environment-dependent because FastAPI is optional in this container.

### Risks
Long-running runtimes require restart for risk-critical config changes. Some legacy tests expected BACKTEST daily symbol limits from runtime counters; this patch intentionally isolates those by default.

### Push recommendation
Push after CI confirms optional dashboard dependencies and any legacy BACKTEST filter-switch expectations are updated to use BACKTEST_* caps.
