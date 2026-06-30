## 2026-06-30 - Failed BACKTEST diagnostic rendering guard

### Why the patch was needed
After the diagnostic evidence chain was ungated for completed BACKTEST visibility, failed selected BACKTEST pages rendered empty selected diagnostic sections alongside `SELECTED_BACKTEST_UNAVAILABLE_DUE_TO_FAILURE`. That implied selected BACKTEST diagnostics existed when the run had failed closed.

### Root cause
The template fix removed the completed-run gate too broadly. Successful runs need the full evidence chain, but failed selected BACKTEST runs must show only the failure warning and must not render selected BACKTEST diagnostic sections or substitute PAPER/runtime evidence.

### Exact template condition changed
The selected BACKTEST diagnostic evidence chain is wrapped in `{% if backtest_result.status == 'COMPLETED' %}`. The failure warning remains in a separate `{% if backtest_result.status != 'COMPLETED' %}` block.

### Files changed
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_dashboard_app.py`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime behavior changes
No BACKTEST decision behavior changed. Completed selected BACKTEST runs render the full evidence chain. Failed selected BACKTEST runs render `SELECTED_BACKTEST_UNAVAILABLE_DUE_TO_FAILURE` and do not render selected BACKTEST diagnostic evidence tables.

### Lifecycle changes
No lifecycle state-machine changes.

### Persistence changes
None. No SQLite or artifact schema changes.

### Export/schema changes
None.

### Tests added/executed
Extended the failed BACKTEST HTML regression to assert absent sections: Signal Quality Diagnostics, Top Near-Miss Rejected Signals, Later Gate Diagnostics, Accepted Trade Diagnostics, Backtest Top Rejection Reasons, and LOW_SCORE Shadow Comparison.

### Risks and limitations
This is a dashboard-template-only rendering fix. It does not tune thresholds, weaken gates, change artifact parsing, or alter PAPER/LIVE runtime behavior.

### Migration concerns
None.

### Push recommendation
Safe to push after dashboard and full pytest validation. LIVE remains NOT READY.

## 2026-06-30 - Dashboard complete BACKTEST diagnostics visibility fix

### Why the patch was needed
After accepted diagnostics, top rejection reasons, and LOW_SCORE shadow comparison were restored, the same BACKTEST evidence test exposed the next hidden populated section: Top Near-Miss Rejected Signals. The model already contained near-miss rows with `WOULD_SL` and cost penalty `0.14`.

### Root cause
A completed-only template branch still wrapped the remaining BACKTEST diagnostic chain. Moving individual tables out one at a time only exposed the next hidden table. The branch needed to stop gating diagnostic evidence tables while preserving the failure warning for failed runs and each table's empty state.

### Exact template sections moved / ungated
- Signal Quality Diagnostics
- Top Quality-Improvement Candidates
- Later Gate Diagnostics
- Score Saturation Diagnostics
- DAILY_GLOBAL_TRADE_LIMIT Near-Miss Diagnostics
- Top Near-Miss Rejected Signals

Accepted Trade Diagnostics, Backtest Top Rejection Reasons, and LOW_SCORE Shadow Comparison were already in the visible evidence area and remain there.

### Files changed
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_dashboard_app.py`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime behavior changes
No BACKTEST decision behavior changed. The dashboard now keeps the selected-backtest failure warning but renders all BACKTEST diagnostic evidence sections in the visible Backtest Result path, relying on their existing empty-state rows when data is absent.

### Lifecycle changes
No lifecycle state-machine changes.

### Persistence changes
None. No SQLite or artifact schema changes.

### Export/schema changes
None.

### Tests added/executed
Kept the existing Top Near-Miss Rejected Signals assertion and added focused rendered HTML assertions for `WOULD_SL` and `0.14`.

### Risks and limitations
This is a dashboard-template-only rendering fix. It does not tune thresholds, weaken gates, change artifact parsing, or alter PAPER/LIVE runtime behavior.

### Migration concerns
None.

### Push recommendation
Safe to push after dashboard and full pytest validation. LIVE remains NOT READY.

## 2026-06-30 - Dashboard LOW_SCORE shadow comparison rendering fix

### Why the patch was needed
After accepted diagnostics and top rejection reasons were restored, the same BACKTEST evidence test still showed that populated `result.low_score_shadow_comparison` was missing from rendered `/backtest/run` HTML even though the model contained WOULD_TP and WOULD_SL counts.

### Root cause
The LOW_SCORE Shadow Comparison section was still inside the completed-only diagnostics branch. The populated model data was therefore not guaranteed to render in the same always-visible Backtest Result evidence path as summary metrics, accepted diagnostics, and top rejection reasons.

### Files changed
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_dashboard_app.py`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime behavior changes
No BACKTEST decision behavior changed. The dashboard now renders LOW_SCORE Shadow Comparison immediately after Backtest Top Rejection Reasons in the visible Backtest Result evidence area. The section explicitly labels rows diagnostic-only and keeps an empty-state row when no LOW_SCORE shadow diagnostics exist.

### Lifecycle changes
No lifecycle state-machine changes.

### Persistence changes
None. No SQLite or artifact schema changes.

### Export/schema changes
None.

### Tests added/executed
Kept the existing `LOW_SCORE Shadow Comparison` rendered HTML assertion and added focused assertions for `WOULD_TP` and `WOULD_SL`.

### Risks and limitations
This is a dashboard-template-only rendering fix. It does not tune thresholds, weaken gates, or alter PAPER/LIVE runtime behavior. LOW_SCORE shadow evidence remains diagnostic-only.

### Migration concerns
None.

### Push recommendation
Safe to push after dashboard and full pytest validation. LIVE remains NOT READY.

## 2026-06-30 - Dashboard top rejection reasons rendering fix

### Why the patch was needed
After accepted diagnostics rendering was restored, the same BACKTEST evidence test still showed that populated `result.top_rejection_reasons` were missing from rendered `/backtest/run` HTML even though the main reject-rate summary and accepted diagnostics were visible.

### Root cause
The Backtest Top Rejection Reasons table was still inside the completed-only diagnostics branch. The populated model data was therefore not guaranteed to render in the same always-visible Backtest Result evidence path as the summary metrics and accepted diagnostics.

### Files changed
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_dashboard_app.py`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime behavior changes
No BACKTEST decision behavior changed. The dashboard now renders the Backtest Top Rejection Reasons table immediately after Accepted Trade Diagnostics in the visible Backtest Result evidence area, with the existing `No rejected_orders.csv diagnostics available.` empty state preserved.

### Lifecycle changes
No lifecycle state-machine changes.

### Persistence changes
None. No SQLite or artifact schema changes.

### Export/schema changes
None.

### Tests added/executed
Kept existing assertions for `Backtest Top Rejection Reasons` and `LOW_SCORE`, and added a focused rendered HTML assertion for `REGIME_MISMATCH` from the fixture.

### Risks and limitations
This is a dashboard-template-only rendering fix. It does not tune thresholds, weaken gates, or alter PAPER/LIVE runtime behavior.

### Migration concerns
None.

### Push recommendation
Safe to push after dashboard and full pytest validation. LIVE remains NOT READY.

## 2026-06-30 - Dashboard accepted diagnostics rendering fix

### Why the patch was needed
The BACKTEST artifact/model hydration path populated `result.accepted_trade_diagnostics`, including accepted signal `s4`, but the rendered `/backtest/run` HTML could still omit the `Accepted Trade Diagnostics` section.

### Root cause
The accepted diagnostics table lived inside the completed diagnostics branch. The BACKTEST result summary table rendered regardless, so core metrics such as reject rate appeared, but accepted diagnostics were still coupled to broader completed-only diagnostic rendering instead of the presence of accepted diagnostics/empty-state dashboard evidence.

### Files changed
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_dashboard_app.py`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime behavior changes
No BACKTEST decision behavior changed. The dashboard now renders the Accepted Trade Diagnostics table immediately after the main Backtest Result artifact table, with the existing empty-state row when no accepted diagnostics are available.

### Lifecycle changes
No lifecycle state machine changes. Existing accepted lifecycle diagnostics are displayed more reliably.

### Persistence changes
None. No SQLite or artifact schema changes.

### Export/schema changes
None.

### Tests added/executed
Added focused rendered HTML assertions for `Accepted Trade Diagnostics`, accepted signal `s4`, accepted symbol `BTCUSDT`, and accepted result `SL_HIT`.

### Risks and limitations
This is a dashboard-template-only fix. It does not tune thresholds, weaken gates, or alter PAPER/LIVE runtime behavior.

### Migration concerns
None.

### Push recommendation
Safe to push after dashboard and full pytest validation. LIVE remains NOT READY.

## 2026-06-30 - Dashboard selected profile artifact parsing and metric consistency

### Why the patch was needed
The latest dashboard profile-comparison run wrote valid DEFAULT_FILTERS artifacts under `profiles/DEFAULT_FILTERS/`, but the main Backtest Result panel returned early after writing leaderboard artifacts and therefore never parsed the selected profile directory for accepted/rejected counts, win/loss/open, net PnL, reject reasons, calibration diagnostics, distributions, or execution-cost summaries.

### Root cause
Comparison mode produced per-profile artifacts and leaderboard metrics but did not hydrate the primary dashboard result object from the selected profile artifact directory. Average trades/day also used a missing/legacy `last_days` field and defaulted to a one-day denominator. Separately, accepted reason counting in `backtest_order.py` counted accepted lifecycle events rather than unique accepted trades, and lifecycle-based quality summaries could miss accepted lifecycle IDs when `SIGNAL_CREATED` rows existed.

### Files changed
- `src/alphaforge/dashboard/backtest_control.py`
- `backtest_order.py`
- `tests/test_backtest_profile_comparison.py`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime behavior changes
Comparison-mode dashboard runs still execute BACKTEST-only profile sub-runs. After writing comparison and leaderboard artifacts, the main result panel now defaults to `DEFAULT_FILTERS` and parses `profiles/DEFAULT_FILTERS/` for the same metrics used by single-profile runs. PAPER/LIVE runtime loops and live order paths are unchanged.

### Lifecycle changes
No lifecycle state-machine transitions changed. Dashboard parsing now surfaces selected-profile lifecycle calibration summaries and reject diagnostics instead of leaving them unavailable. Accepted reason summaries now count unique accepted trade/signal IDs, avoiding repeated lifecycle-event inflation.

### Persistence changes
No SQLite schema migration. The patch reads existing per-profile CSV/JSON artifacts and continues to write existing dashboard comparison artifacts. Quality-summary fields are corrected to align with canonical order summary counts.

### Export/schema changes
No breaking artifact schema changes. `accepted_reason_breakdown` semantics are corrected to unique accepted trades/signals. Profile comparison `avg_trades_per_day` now uses the requested/effective full window when present.

### Tests added/executed
Added targeted regression tests for selected-profile main-panel hydration, profile rejected-order diagnostics, profile calibration accepted diagnostics, requested-window average trades/day, lifecycle quality-summary canonical accepted counts, and unique accepted-reason breakdown.

### Risks and limitations
The selected profile currently defaults to `DEFAULT_FILTERS`; an explicit dashboard selector can be added later without changing artifact parsing. This patch does not tune thresholds, weaken hard gates, or assert LIVE readiness.

### Migration concerns
None for SQLite. Downstream artifact consumers should interpret `accepted_reason_breakdown` as unique accepted trade/signal counts rather than lifecycle-event counts.

### Push recommendation
Safe to push after targeted dashboard/backtest tests pass. LIVE remains NOT READY.

## 2026-06-30 - BACKTEST daily timeframe support and truthful interval errors

### Why the patch was needed
Dashboard-launched BACKTEST runs for `Timeframe=1d` failed closed with `Unsupported interval=1d`, but the dashboard mapped that backend capability error to a misleading Binance data-shortage message.

### Root cause
The historical data interval map only recognized intraday intervals up to `1h`, while the dashboard allowed `4h` and `1d`. The dashboard error classifier treated all historical data exceptions as insufficient-data failures and did not persist enough failed-run metadata to audit the requested timeframe or filter state.

### Files changed
- `src/alphaforge/historical_market_data.py`
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `backtest_order.py`
- `tests/test_historical_market_data.py`
- `tests/test_dashboard_app.py`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime behavior changes
BACKTEST historical loading now supports Binance-compatible `4h` and `1d` klines. Unsupported intervals raise `UNSUPPORTED_TIMEFRAME` with the requested interval, supported intervals, and source function. Dashboard validation remains BACKTEST-scoped and uses backend-supported intervals. PAPER/LIVE behavior is unchanged.

### Lifecycle changes
No lifecycle state transition semantics changed. Failed pre-run BACKTEST panels now show `SELECTED_BACKTEST_UNAVAILABLE_DUE_TO_FAILURE` instead of implying selected BACKTEST diagnostics exist.

### Persistence changes
No SQLite schema migration. Dashboard BACKTEST runs now write additive `backtest_run_metadata.json` evidence containing requested/effective timeframe, last-n-days, effective start/end, symbols, failure reason, requested profile, enabled/disabled optional filters, and whether filter state was applied before failure.

### Export/schema changes
Successful summary CSV rows now include additive requested/effective timeframe/window/symbol fields. Coverage errors include returned and required candle counts.

### Tests added/executed
Added regression tests for `_interval_ms("1d")`, `4h`, daily pagination, unsupported timeframe classification, dashboard failed metadata, and failed-dashboard diagnostic isolation.

### Risks and limitations
This patch does not tune strategy thresholds, weaken safety gates, or increase accepted trade count. Network-dependent live Binance validation remains environment-sensitive.

### Migration concerns
None for SQLite. Artifact consumers should tolerate additive metadata keys.

### Push recommendation
Safe to push after full pytest and requested smoke BACKTEST commands complete. LIVE remains NOT READY.


## 2026-06-30 - BACKTEST profile comparison runner

### Why the patch was needed
The existing comparison artifact could only describe the current run and marked other profiles as not run. That was sufficient for switch audit evidence but could not compare filter profiles over the same BACKTEST inputs.

### Root cause
The dashboard had a single BACKTEST subprocess path and no artifact-first coordinator for repeated profile executions.

### Files changed
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `docs/backtest_profile_comparison.md`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime behavior changes
Added an opt-in BACKTEST-only comparison runner that executes profile sub-runs under `profiles/<profile>/` with the same symbols, timeframe, balance, max-symbol cap, date window, and data source. Default single-profile dashboard behavior is unchanged when the checkbox is not selected.

### Lifecycle changes
No lifecycle state machine changes. Comparison artifacts consume existing per-profile lifecycle exports.

### Persistence changes
No SQLite schema migration. New JSON/CSV artifacts are written under the dashboard BACKTEST output directory.

### Export/schema changes
Added `backtest_filter_profile_comparison.json`, `backtest_profile_leaderboard.json`, and `backtest_profile_leaderboard.csv` for comparison mode, including objective-score components, warnings, artifact paths, and bucket diagnostics.

### Tests added/executed
Local validation included Python compilation and targeted pytest execution.


### Pre-merge safety audit correction
The initial comparison coordinator copied the single-profile command after UI filter disables had been appended, which could contaminate DEFAULT/STRICT/diagnostic profile sub-runs when the dashboard UI had custom disabled filters. It also relied on each subprocess computing `last_n_days` relative to its own clock. The patch now builds an immutable base BACKTEST command, appends profile-specific filter switches only per profile, and passes one fixed `--start`/`--end` window to every sub-run.

### Risks and limitations
The 30/90/180/365 multi-window matrix is scaffolded only; non-selected windows are marked NOT_RUN. Diagnostic guard profiles currently preserve default thresholds and export warnings/labels rather than changing global config. Drawdown is not fabricated when unavailable.

### Migration concerns
None for SQLite. Artifact consumers should tolerate new comparison keys.

### Push recommendation
Safe to push after targeted dashboard/backtest tests pass. LIVE remains NOT READY.

# AlphaForge Technical Surgery Report

## 2026-06-30 - Dashboard BACKTEST SHORT_BREAKDOWN_RESCUE switch

### Why this patch was needed
Operators had to manually set `ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED` outside the dashboard to compare baseline and rescue-enabled BACKTEST runs. That made dashboard evidence incomplete and increased the risk of stale shell state contaminating comparisons.

### Root cause
The rescue experiment existed in the backtest runner but the dashboard request model, form, subprocess environment, filter-state artifacts, and result rendering did not carry or show the selected experiment state.

### Files changed
- `.env.example`
- `backtest_order.py`
- `src/alphaforge/config_registry.py`
- `src/alphaforge/dashboard/__init__.py`
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_dashboard_rescue_switch.py`
- `tests/test_dashboard_app.py`
- `CHANGELOG.md`
- `VERSION.md`
- `REPORT.md`

### Runtime behavior changes
Dashboard BACKTEST requests now include a disabled-by-default `SHORT_BREAKDOWN_RESCUE experiment` checkbox. Launching a dashboard backtest passes a scoped subprocess environment override: `false` for baseline and `true` only when selected. The command also adds `--rescue-enabled` when selected. The supported dashboard runner remains `backtest_order.py`; no `python -m alphaforge.backtest.runner` module exists in this repository.

### Lifecycle changes
No lifecycle transition semantics changed. Rescue remains constrained by existing BACKTEST-only acceptance checks and does not activate in PAPER/LIVE.

### Persistence/export/schema changes
No SQLite schema migration. `backtest_filter_state.json` and `.csv` now include SHORT_BREAKDOWN_RESCUE experiment evidence, including BACKTEST-only scope and default-off status. Summary CSV metadata includes the selected rescue state and dashboard result rendering exposes baseline/rescue accepted counts, PnL, combined PnL, and accepted-reason breakdown.

### Tests added/executed
Added dashboard rescue-switch tests for disabled/enabled scoped env values, default-off behavior, BACKTEST-only settings scope, filter-state artifact evidence, dashboard result rendering, and existing rescue-disabled/rescue-enabled baseline behavior.

### Risks and limitations
Rescue-enabled BACKTEST results are experimental comparison evidence, not production readiness. Candidate quality gate CSV behavior remains reporting-only. Optional FastAPI/httpx absence still skips HTML rendering coverage in minimal environments.

### Migration concerns
None for SQLite. Existing artifact consumers should tolerate the added JSON fields and CSV columns.

### Push recommendation
Safe to push after the targeted tests pass. Do not promote rescue to PAPER/LIVE and do not infer LIVE readiness from rescue-enabled BACKTEST acceptance.

---

## 2026-06-30 - BACKTEST filter-state audit and filters-off damage diagnostics
## 2026-06-30 - BACKTEST SHORT Breakdown Rescue Reporting Experiment

## Why the patch was needed
The latest DEFAULT all-filters-on BACKTEST produced only 11 accepted trades from 1,436 candidates, while `candidate_quality_gates` showed a concentrated SHORT + BREAKDOWN_DOWN + BREAKOUT/NORMAL-stop cluster with positive rejected-shadow expectancy. A global filter loosen would be unsafe, so the patch adds a reporting-first, opt-in rescue path for that narrow hypothesis only.

## Root cause
The dashboard/export layer already exposed the SHORT breakdown quality gate as reporting-only evidence, but there was no controlled way to test reduced-size activation without disabling broad reject filters or changing baseline behavior.

## Files changed
- `.env.example`
- `backtest_order.py`
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_backtest_order_scanner.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

## Runtime behavior changes
DEFAULT BACKTEST behavior remains unchanged because `ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED=false`. When explicitly enabled, BACKTEST may rescue only SHORT `BREAKDOWN_DOWN` candidates in BREAKOUT/NORMAL-compatible conditions whose first reject reason/gate is allowed and whose execution context passes conservative checks.

## Lifecycle changes
Rescued trades use the normal simulation lifecycle and are marked with `accepted_reason=SHORT_BREAKDOWN_RESCUE`, `original_reject_reason`, `rescue_size_multiplier`, `rescue_effective_rr`, and JSON `rescue_decision_context`.

## Persistence changes
No SQLite migration is required. Existing lifecycle/export metadata fields carry rescue evidence. Summary exports include rescue diagnostics and baseline-vs-rescue PnL separation.

## Export/schema changes
`.env.example` adds the BACKTEST-only rescue variables. `backtest_filter_state` now includes a `backtest_only_experiments` section identifying the rescue switch as BACKTEST-only. The dashboard summary separates BASELINE accepted trades, RESCUE accepted trades, and reporting-only gates.

## Tests added
Regression tests prove disabled baseline rejection, enabled rescue rows, SHORT-only eligibility, LOW_SCORE LONG exclusion, metadata population, filter-state BACKTEST-only labeling, and PAPER/LIVE non-activation.

## Tests executed
- `pytest -q tests/test_backtest_order_scanner.py -k 'short_breakdown_rescue or rescue_enabled_only_backtest or rescue_disabled'`

## Risks
The rescue gate is still experimental and depends on rejected-shadow/backtest diagnostics. Spread/slippage may be estimated. Enabling the rescue can change accepted count and PnL in BACKTEST only.

## Remaining limitations
`ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_MIN_SHADOW_EXPECTANCY` is exported/configured for operator audit but live per-candidate activation currently gates on candidate execution context rather than recomputing a per-run shadow aggregate inside the decision loop.

## Migration concerns
No database migration required. Artifact consumers should tolerate additive JSON/CSV fields.

## Push recommendation
Safe to push as a BACKTEST-only reporting-first experiment. Do not enable for LIVE; LIVE remains not ready.
