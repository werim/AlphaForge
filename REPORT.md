## 2026-07-01 - BACKTEST reject reason attribution surgery report

### Why this patch was needed
Latest BACKTEST diagnostics showed 516/516 candidates rejected with every exported reject reason collapsed to `UNKNOWN`, obscuring whether rejects came from weak effective RR, expectancy, missing execution evidence, score, or regime/setup alignment.

### Root cause
The shared order gate already emitted diagnostics such as `effective_rr`, thresholds, failed gates, and expectancy status, but the BACKTEST handoff trusted `result.reason`/`result.reject_reason` first. When that field was `UNKNOWN`, exported `rejected_orders.csv` and dashboard summary distributions could remain unclassified instead of deriving the first concrete blocking cause from diagnostics and execution context.

### Files changed
- `backtest_order.py`: added concrete reject attribution from diagnostics/market context, preserves primary and secondary reject reasons, applies attribution before rejected lifecycle/CSV export, and adds threshold diagnostics to quality summaries.
- `src/alphaforge/order.py`: exports threshold settings in decision diagnostics so BACKTEST attribution can separate raw RR from effective RR failures at export time.
- `tests/test_backtest_order_scanner.py`: added reject attribution and rejected CSV preservation tests.
- `CHANGELOG.md`, `VERSION.md`, `REPORT.md`: documented behavior, compatibility, risks, and validation.

### Runtime behavior changes
Rejected BACKTEST candidates now preserve the first concrete cause when the runtime status reason is unknown. Filters are not loosened and no rejected candidate is forced into acceptance.

### Lifecycle changes
`SIGNAL_REJECTED` rows now carry a concrete reject reason when diagnostics identify one. Lifecycle ordering is unchanged.

### Persistence changes
No database migration. CSV/lifecycle artifacts gain better reject reason values and optional `secondary_reject_reasons` export data.

### Export/schema changes
`rejected_orders.csv` preserves `reject_reason` plus `secondary_reject_reasons`. BACKTEST quality summary diagnostics include `thresholds_used` with `min_score`, `min_raw_rr`, `min_effective_rr`, `reject_unknown_expectancy`, and `require_execution_context`.

### Tests added
Added focused regressions for low effective RR, negative expectancy, missing expectancy, missing execution context, non-UNKNOWN reject distributions, and rejected CSV preservation.

### Tests executed
- `pytest -q tests/test_backtest_order_scanner.py tests/test_phase123_foundations.py tests/test_backtest_paper_pre_submit_parity.py`

### Risks
Dashboard consumers may see `LOW_EFFECTIVE_RR` instead of older `RR_TOO_LOW` for execution-adjusted failures. This is intended attribution tightening, not strategy loosening.

### Remaining limitations
`UNKNOWN` is still possible when no concrete diagnostic, threshold, expectancy, score, regime, or execution-context failure can be determined.

### Migration concerns
No schema migration required. CSV consumers should tolerate the additional optional `secondary_reject_reasons` column.

### Push recommendation
Safe to push after full `pytest -q` passes. Do not claim LIVE readiness.

## 2026-07-01 - Dashboard guardrail section rendering regression surgery report

### Why this patch was needed
A regression test for `/backtest/run` expected the rendered HTML to include the exact `Strategy Quality Guardrails` heading when guardrail data exists, but the template only rendered guardrail rows inside the generic result table.

### Root cause
The guardrail data fields were present on `DashboardBacktestResult`, but the template no longer exposed a dedicated, searchable guardrail section heading.

### Files changed
- `src/alphaforge/dashboard/templates/overview.html`: restored a dedicated `Strategy Quality Guardrails` section gated by guardrail breakdown, top reasons, or representative examples.
- `CHANGELOG.md`, `REPORT.md`: documented the regression and validation.

### Runtime behavior changes
None. This is a dashboard/template rendering patch only.

### Lifecycle changes
None.

### Persistence changes
None.

### Export/schema changes
None. Existing result dataclass fields and artifact keys are unchanged.

### Tests added
No new test was needed; the existing regression test is preserved and targets this rendering behavior.

### Tests executed
- `pytest -q tests/test_backtest_profile_comparison.py::test_dashboard_renders_guardrail_breakdown_when_source_data_exists`
- `pytest -q`

### Risks
Low. The new section is hidden unless guardrail source data is present.

### Remaining limitations
Dashboard rendering depends on artifacts/result fields being populated upstream; this patch does not alter reporting metrics or strategy decisions.

### Migration concerns
None.

### Push recommendation
Safe to push after tests pass. Do not claim LIVE readiness.

## 2026-07-01 - Dashboard BACKTEST accepted-count and guardrail attribution surgery report

### Why this patch was needed
The latest profile-comparison artifact still showed DEFAULT_FILTERS/STRICT_FILTERS/CUSTOM_CURRENT_UI with 12,228 accepted trades and `OVERTRADE_RISK`, while the selected DEFAULT_FILTERS backtest summary reported zero accepted trades, zero outcomes, zero PnL, and unavailable accepted diagnostics.

### Root cause
Previous reporting fixes did not fully protect every dashboard comparison/fallback path from lifecycle/event rows and diagnostic rows. Guardrail/later-gate evidence was exported but dashboard attribution could remain empty, and the fallback gate funnel could show zero rejects despite canonical reject reasons being present.

### Files changed
- `src/alphaforge/dashboard/backtest_control.py`: added artifact-derived guardrail attribution fallback, canonical rejection gate-funnel fallback, and tests covering no-trade/overtrade warning behavior through comparison metrics.
- `backtest_order.py`: removed `SIGNAL_CREATED` accepted-count fallback from default gate-funnel construction and expanded gate-funnel CSV fields for exported diagnostic columns.
- `tests/test_backtest_profile_comparison.py`: added required zero-summary/12,228 lifecycle fixture, exact ALL_FILTERS_OFF fixture, guardrail attribution/funnel fixture, and dashboard rendering fixture.
- `CHANGELOG.md`, `VERSION.md`, `REPORT.md`: documented reporting-only behavior, risks, and validation.

### Runtime behavior changes
None to strategy logic, filter thresholds, scoring, PAPER, or LIVE behavior. The patch changes BACKTEST dashboard/reporting metrics only.

### Lifecycle changes
No lifecycle transitions changed. Lifecycle event rows remain visible as diagnostics but are not accepted trades.

### Persistence changes
No database migration. Existing CSV/JSON artifacts are read more conservatively; `default_gate_funnel.csv` export field coverage is expanded to include existing diagnostic columns without changing trade persistence semantics.

### Export/schema changes
Dashboard fallback guardrail sections can now populate `guardrail_reject_breakdown`, `top_guardrail_reject_reasons`, and representative examples from later-gate/rejection artifacts when `strategy_quality_guardrails.json` is absent or incomplete.

### Tests added
Added regressions proving summary `accepted_count=0` wins over 12,228 lifecycle rows, lifecycle events are not trades, average trades/day uses executed count only, no-trade profiles do not raise `OVERTRADE_RISK`, ALL_FILTERS_OFF exact executed values are preserved, guardrail breakdown populates from source data, and dashboard rendering does not show `Unavailable` when guardrail source data exists.

### Tests executed
- `pytest -q tests/test_backtest_profile_comparison.py -q`
- `pytest -q tests/test_strategy_quality_guardrails.py tests/test_backtest_profile_comparison.py -q`

### Risks
Downstream consumers may see lower accepted counts and lower avg trades/day where prior dashboards counted lifecycle events. This is intended and safer.

### Remaining limitations
Guardrail attribution uses exported evidence only; it does not synthesize fake fills or counterfactual PnL for rejected candidates.

### Migration concerns
No schema migration. Consumers should treat lifecycle row counts as diagnostics and accepted counts as summary/order/executed evidence only.

### Push recommendation
Safe to push after tests pass. Do not claim LIVE readiness.

## 2026-07-01 - Dashboard BACKTEST profile timeout handling surgery report

### Why this patch was needed
Dashboard `POST /backtest/run` could crash with an uncaught `subprocess.TimeoutExpired` during profile comparison, including invalid negative timeout values from exhausted time budgets.

### Root cause
Profile-comparison subprocess execution did not contain timeout failures per profile or persist timeout metadata before returning to the dashboard. A timed-out profile could therefore abort the whole request and hide completed profile artifacts.

### Files changed
- `src/alphaforge/dashboard/backtest_control.py`: added positive timeout validation, per-profile timeout handling, TIMEOUT profile metadata, PARTIAL comparison results, and leaderboard preservation.
- `src/alphaforge/dashboard/templates/overview.html`: renders PARTIAL results and profile statuses so completed profiles remain visible beside timed-out profiles.
- `tests/test_backtest_profile_comparison.py`: added timeout regression coverage for non-positive timeout rejection, partial results, TIMEOUT profile marking, artifact preservation, and dashboard rendering.
- `CHANGELOG.md`, `VERSION.md`, `REPORT.md`: documented behavior, risks, and validation.

### Runtime behavior changes
Profile comparison now uses a positive per-profile timeout and catches `TimeoutExpired` for the individual profile. The dashboard returns a controlled PARTIAL result with a user-readable warning instead of an uncaught ASGI traceback.

### Lifecycle changes
None. Completed profile lifecycle artifacts are preserved and selected DEFAULT_FILTERS evidence remains displayable if ALL_FILTERS_OFF times out.

### Persistence changes
No database migration. Timed-out profile artifact directories now receive `backtest_profile_metadata.json` with machine-readable BACKTEST mode, profile name, status `TIMEOUT`, failure reason, timeout seconds, and command.

### Export/schema changes
Profile comparison JSON now includes top-level `status` and per-profile `status`; leaderboard rows include `status`. Timed-out profile metrics are explicitly unavailable/null rather than fabricated.

### Tests added
Added regression tests for non-positive timeout rejection before `subprocess.run`, timeout-to-PARTIAL result conversion, profile-scoped TIMEOUT marking, completed output preservation, and DEFAULT_FILTERS rendering when ALL_FILTERS_OFF times out.

### Tests executed
- `pytest -q tests/test_backtest_profile_comparison.py`

### Risks
Downstream consumers of profile leaderboard CSV/JSON should tolerate the added `status` column/field and null metrics for timed-out profiles.

### Remaining limitations
A timed-out profile is not retried automatically; operators must rerun with a smaller universe/window or investigate the profile artifact directory.

### Migration concerns
No schema migration. Artifact readers should handle `PARTIAL` comparison status and per-profile `TIMEOUT`.

### Push recommendation
Safe to push after tests pass; do not claim LIVE readiness.

## 2026-06-30 - BACKTEST profile metric integrity surgery report

### Why this patch was needed
The latest dashboard artifact showed DEFAULT_FILTERS with `accepted_count=0` and zero orders in `order_backtest_summary.csv`, while root comparison/leaderboard outputs reported 12,221 accepted trades and `OVERTRADE_RISK`. That contradicted canonical order evidence and could rank a no-trade profile as strategy performance.

### Root cause
Profile comparison fell back to lifecycle-derived rows when summary values were zero or when lifecycle diagnostic exports contained many rows. That blurred lifecycle event count, rejected signal count, accepted trade count, and executed outcome count.

### Files changed
- `src/alphaforge/dashboard/backtest_control.py`: added canonical accepted-trade selection from summary/order evidence, no-trade handling, accepted distribution isolation, and leaderboard ranking safeguards.
- `backtest_order.py`: added guardrail reject reason breakdown/examples and gate-funnel comparability labels.
- `tests/test_backtest_profile_comparison.py`: added regression coverage for metric integrity and no-trade leaderboard behavior.
- `tests/test_strategy_quality_guardrails.py`: added guardrail explainability and gate-funnel labeling coverage.
- `CHANGELOG.md`, `VERSION.md`, `REPORT.md`: documented behavior, risks, and validation.

### Runtime behavior changes
BACKTEST dashboard/profile reporting now uses canonical executed trade evidence only. No trading gates were loosened and no PAPER/LIVE order path was changed.

### Lifecycle changes
Lifecycle exports remain intact. Reporting now explicitly separates lifecycle event count from accepted trade count and never counts `SIGNAL_CREATED`, `SIGNAL_REJECTED`, `SYMBOL_REJECTED`, or `ORDER_REJECTED` as accepted trades.

### Persistence changes
No database migration. CSV/JSON artifacts gain explanatory fields but existing core files remain in place.

### Export/schema changes
Profile comparison JSON includes `accepted_trades_source`, `lifecycle_event_count`, and `rejected_row_count`. Guardrail evidence includes `guardrail_reject_breakdown`, `top_guardrail_reject_reasons`, and `representative_guardrail_reject_examples`. Gate-funnel rows include scope/comparability notes.

### Tests added
Added tests for zero accepted count with 12k lifecycle rows, rejected lifecycle states not counted, accepted effective RR distribution count zero for no-trade profiles, no `OVERTRADE_RISK` from lifecycle rows, ALL_FILTERS_OFF executed count/outcomes/PnL, and default gate funnel comparability disclosure.

### Tests executed
- `pytest -q tests/test_backtest_profile_comparison.py -q`
- `pytest -q tests/test_strategy_quality_guardrails.py tests/test_backtest_profile_comparison.py -q`

### Risks
Existing downstream consumers may see accepted trade counts drop to zero where previous artifacts were inflated by lifecycle events. This is intended and safer.

### Remaining limitations
The patch does not replay guardrail-rejected candidates as trades, because that would create fake counterfactual PnL.

### Migration concerns
Consumers should read accepted counts from summary/order evidence and treat lifecycle row counts as diagnostics only.

### Push recommendation
Safe to push after tests pass; do not claim LIVE readiness.

## 2026-06-30 - Purpose-specific environment profiles surgery report

### Why this patch was needed
The single `.env.example` mixed BACKTEST diagnostics, PAPER evaluation, and LIVE preparation defaults, increasing the risk of mode confusion and threshold misuse.

### Root cause
Environment variables were meaningful but presented in one template without purpose-specific threshold tuning or copy guidance.

### Files changed
- `.env.example`: retained as safe medium PAPER-oriented default with profile pointers.
- `.env.test.example`: added loose BACKTEST/PAPER diagnostic profile marked NOT FOR LIVE.
- `.env.medium.example`: added balanced PAPER/default profile.
- `.env.live.example`: added hardened LIVE preparation profile with fail-closed real-order guards.
- `README.md`: documented profile purposes and copy commands for Windows PowerShell and macOS/Linux.
- `tests/test_env_example_profiles.py`: added profile validation coverage.
- `CHANGELOG.md`, `VERSION.md`, `REPORT.md`: operational documentation updates.

### Runtime behavior changes
None. This patch changes example configuration templates and validation tests only.

### Lifecycle changes
None. Lifecycle state transitions and persistence behavior are unchanged.

### Persistence changes
None. No database schema or CSV export contract changed.

### Export/schema changes
None.

### Tests added
Added tests for all four env examples existing, containing core variables, avoiding real-looking secrets, keeping LIVE stricter than TEST on critical thresholds, marking TEST as diagnostic/non-LIVE, and documenting all profiles in README.

### Tests executed
- `pytest tests/test_env_example_profiles.py -q`
- `pytest -q`

### Risks
Operators must still choose the right profile intentionally; LIVE examples remain templates and do not establish readiness evidence.

### Remaining limitations
The audit did not wire new variables because no cosmetic variables were added; existing reserved/unwired notes remain unchanged.

### Migration concerns
None for runtime code. Local users may copy the profile matching their workflow instead of the generic `.env.example`.

### Push recommendation
Push after tests pass; do not claim LIVE readiness.


## 2026-06-30 Strategy Quality Guardrails Surgery Report

### Why this patch was needed
Recent DEFAULT/DYNAMIC BACKTEST diagnostics showed excessive accepted trades, long loss streaks, score=10 saturation, STOP_TOO_WIDE softening leakage, and high-vol variance. Raw positive PnL alone was insufficient because score=10 no longer separated winners from losers and late daily-symbol caps hid weaker near-miss quality.

### Root cause
DEFAULT_FILTERS had evidence exports but lacked acceptance-time strategy-quality controls for same-day clusters, realized SL streaks, score saturation, and high-vol cost/variance.

### Files changed
- `backtest_order.py`: BACKTEST strategy-quality guardrail config, acceptance checks, explicit reject evidence, and profile-quality evidence exports.
- `.env.example`: documented guardrail and profile PASS/FAIL thresholds.
- `tests/test_strategy_quality_guardrails.py`: regression coverage.
- `CHANGELOG.md`, `VERSION.md`, `REPORT.md`: operational documentation.

### Runtime behavior changes
DEFAULT_FILTERS BACKTEST now rejects overactive daily/symbol/regime clusters, pauses after consecutive SLs, applies secondary checks to score>=9.8 candidates, and restricts high-vol acceptance. LIVE order placement is unchanged.

### Lifecycle changes
New guardrail rejections are persisted/exported as `SIGNAL_REJECTED` rows with `DAILY_TRADE_FREQUENCY_GUARD`, `LOSS_STREAK_PAUSE`, `SYMBOL_CLUSTER_GUARD`, `SCORE_SATURATION_GUARD`, `HIGH_VOL_GUARD`, `HIGH_VOL_OVERTRADE`, or `HIGH_VOL_EXECUTION_COST`.

### Persistence/export changes
Added `strategy_quality_guardrails.json/csv` plus summary fields for `profile_quality_status`, `profile_quality_reasons`, thresholds, and accepted before/after counts. No DB schema migration is required.

### Tests added/executed
Added unit tests for trade-frequency, loss-streak, score-saturation, high-vol, profile PASS/FAIL, diagnostic-only profile, and env coverage.

### Risks and limitations
Before-guardrail PnL/profit-factor/drawdown are exported as null because replaying rejected trades as accepted would create fake counterfactual performance. PAPER remains reject-heavy by design until calibrated evidence improves; this patch does not force PAPER trades.

### Migration concerns
Existing dashboards consuming `order_backtest_summary.csv` should tolerate appended columns. New guardrail reject reasons should be added to downstream reason allowlists if any exist.

### Push recommendation
Push after tests pass; do not claim LIVE readiness.

## 2026-06-30 - RejectedShadowEvaluation fixture alignment

### Why the patch was needed
CI reported `test_top_quality_improvement_note_explains_would_sl_dominance` failing because its direct `RejectedShadowEvaluation` constructor calls did not include newly required execution diagnostic fields.

### Root cause
The test fixture used named arguments for only the fields needed by the assertion and was not updated when `RejectedShadowEvaluation` required spread, liquidity, volatility, TP-hit, cost-penalty, and execution-ok diagnostics.

### Files changed
- `tests/test_dashboard_app.py`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime behavior changes
None. Production logic is unchanged.

### Lifecycle changes
None.

### Persistence changes
None.

### Export/schema changes
None.

### Tests added
No new test case; the existing dashboard fixture now uses a local helper with deterministic execution diagnostic values.

### Tests executed
- `pytest tests/test_dashboard_app.py::test_top_quality_improvement_note_explains_would_sl_dominance -q`
- `pytest tests/test_dashboard_app.py -q`
- `pytest -q`

### Risks
None beyond the dashboard module being import-skipped in environments missing optional dashboard dependencies.

### Remaining limitations
No production behavior was re-audited in this fixture-only change.

### Migration concerns
None.

### Push recommendation
Safe to push.

## 2026-06-30 - PR243/env DEFAULT_FILTERS overtrade audit diagnostics

### Why the patch was needed
After PR243/env changes, the comparable TOP 20 BACKTEST accepted count jumped from 11 to 354 and net expectancy turned negative. The dashboard did not make the gate-funnel failure, score=10 SL dominance, or missing drawdown obvious enough to prevent DEFAULT_FILTERS from looking strategy-quality.

### Root cause
Code audit found PR243 itself primarily changed dashboard dynamic-universe validation and selected artifact parsing, not the order gate. The acceptance jump is most consistent with existing environment/config behavior around STOP_TOO_WIDE softening and threshold calibration: score=10 saturation allowed many high-score wide/high-vol candidates to survive while STOP_TOO_WIDE disappeared from dominant reject reasons and DAILY_SYMBOL_TRADE_LIMIT became the later visible limiter. The patch records this as diagnostic evidence instead of changing strategy decisions.

### Files changed
- `backtest_order.py`
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_dashboard_app.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
No accepted/rejected decision behavior changed. BACKTEST now exports gate-funnel, equity-curve, and per-symbol/regime acceptance diagnostics and the dashboard shows blocking-level warnings for overtrade and score saturation risk.

### Lifecycle changes
No lifecycle states or transitions changed. Accepted terminal lifecycle rows are read in timestamp order for equity-curve and streak diagnostics only.

### Persistence changes
No SQLite schema or PAPER/LIVE persistence changes. BACKTEST CSV artifact set is extended with diagnostic-only files.

### Export/schema changes
Added optional BACKTEST exports: `equity_curve.csv`, `default_gate_funnel.csv`, and `symbol_regime_acceptance_diagnostics.csv`. `order_backtest_summary.csv` now includes drawdown/streak/profit-factor metrics and explicit return/net-PnL unit labels.

### Tests added
Added tests proving score saturation JSON renders in the dashboard table, overtrade and score saturation warnings fire, drawdown metrics are computed, STOP_TOO_WIDE and zero-reject gates are visible in the funnel, and WOULD_SL-dominated near misses do not become quality-improvement recommendations.

### Tests executed
- `pytest tests/test_dashboard_app.py -q` (skipped in this environment because optional dashboard deps were import-skipped)
- `pytest tests -q`
- `python -m py_compile backtest_order.py src/alphaforge/dashboard/backtest_control.py`

### Risks
This is diagnostic-only and does not fix the negative expectancy root calibration. If env leaves STOP_TOO_WIDE softening permissive or score=10 saturation uncalibrated, DEFAULT_FILTERS can still overtrade; the dashboard now flags that as not strategy-quality.

### Remaining limitations
The exact prior-run artifact was not present locally, so before/after attribution is based on git/env code audit and latest run metrics provided. Re-run the TOP 20 comparison to generate the new evidence artifacts.

### Migration concerns
No migration required. Consumers that parse summary CSVs should tolerate added columns.

### Push recommendation
Safe to push as diagnostic guardrails. Do not promote LIVE readiness.

## 2026-06-30 - BACKTEST dashboard dynamic top-volume universe validation

### Why the patch was needed
Leaving SYMBOLS blank with MAX SYMBOLS set failed dashboard validation even though the BACKTEST runner already supports selecting a top-volume universe when no fixed symbols are provided.

### Root cause
The dashboard form required at least one parsed symbol before considering MAX SYMBOLS, and command construction always emitted `--symbols` with the parsed symbol list. That made dynamic universe requests fail early or risk passing an invalid empty fixed-symbol argument.

### Files changed
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `backtest_order.py`
- `tests/test_dashboard_backtest_dynamic_universe.py`
- `tests/test_dashboard_app.py`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime behavior changes
The BACKTEST dashboard now accepts explicit symbols, or blank symbols with a positive MAX SYMBOLS for dynamic top-volume universe selection. Dynamic requests omit `--symbols` and pass `--max-symbols` to the BACKTEST runner. Explicit symbol requests still pass the fixed symbol list.

### Lifecycle changes
None. No lifecycle states, reject decisions, or order lifecycle transitions changed.

### Persistence changes
No SQLite schema, CSV export contract, PAPER persistence, or LIVE persistence changes. Dashboard run metadata now records dynamic-vs-explicit universe mode for BACKTEST runs.

### Export/schema changes
No required exporter schema changes. Dashboard reporting can replace the placeholder dynamic symbol display with actual exported selected symbols when the summary metadata includes them.

### Tests added
Added BACKTEST dashboard validation and command-boundary tests for dynamic universe acceptance, invalid blank/zero MAX SYMBOLS, explicit symbols with MAX SYMBOLS, omission of empty `--symbols`, `--max-symbols 20`, and BACKTEST-only mode preservation.

### Tests executed
- `pytest -q tests/test_dashboard_backtest_dynamic_universe.py`

### Risks
Dynamic universe selection depends on existing runner/exchange metadata behavior and historical data availability. The patch does not weaken filters, does not touch PAPER/LIVE runtime, and does not add Binance live order calls.

### Remaining limitations
If the exporter omits selected symbol metadata, the dashboard displays the dynamic MAX_SYMBOLS label rather than reconstructing symbols.

### Migration concerns
None. `backtest_order.py` keeps `--top-n` and adds `--max-symbols` as an alias for dashboard clarity.

### Push recommendation
Safe to push after targeted BACKTEST dashboard tests. LIVE remains NOT READY.

## 2026-06-30 - DEFAULT_FILTERS accepted-reason scope and STOP_TOO_WIDE recoverable diagnostics

### Why the patch was needed
The selected DEFAULT_FILTERS main panel showed accepted=10 and baseline/rescue accepted counts of 9/1, but accepted-reason breakdown could display aggregate comparison counts such as BASELINE=36 and SHORT_BREAKDOWN_RESCUE=4. That made the selected strategy panel internally inconsistent.

### Root cause
The dashboard trusted summary-level accepted-reason breakdown before deriving counts from selected `backtest_orders.csv`, allowing profile-comparison aggregate or wrong-scope summary values to leak into the selected main panel.

### Files changed
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_backtest_profile_comparison.py`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime behavior changes
The selected BACKTEST main panel now derives accepted-reason breakdown from the selected profile's `backtest_orders.csv` when available. It falls back to summary counts only when the parsed count total matches the selected accepted count. Profile-comparison aggregate data is not used for the selected main panel.

### Lifecycle changes
No lifecycle transitions changed. Rejected and accepted rows are still read from exported BACKTEST artifacts only.

### Persistence changes
No SQLite, CSV export schema, PAPER, or LIVE persistence changes.

### Export/schema changes
No required exporter schema changes. The dashboard consumes existing `backtest_orders.csv`, `order_backtest_summary.csv`, `rejected_shadow.csv`, and calibration summary artifacts.

### Tests added
Extended the DEFAULT_FILTERS profile fixture to prove selected accepted=10, baseline/rescue=9/1, accepted-reason breakdown BASELINE=9 and SHORT_BREAKDOWN_RESCUE=1, and aggregate ALL_FILTERS_OFF/summary counts do not leak into the selected main panel.

### Tests executed
- `pytest -q tests/test_backtest_profile_comparison.py`
- `pytest -q tests/test_dashboard_app.py`
- `pytest -q tests/test_backtest_order_scanner.py::test_stop_too_wide_split_metrics_are_exported`

### Risks
STOP_TOO_WIDE recoverable candidates are reporting-only. The patch does not loosen STOP_TOO_WIDE, increase accepted trades, change PAPER/LIVE runtime behavior, or treat ALL_FILTERS_OFF as strategy performance.

### Remaining limitations
The recoverable table depends on rejected-shadow artifacts. Missing shadow outcomes remain unknown rather than fabricated.

### Migration concerns
None. Existing artifacts continue to load, with stricter protection against wrong-scope summary reason counts.

### Push recommendation
Safe to push after targeted dashboard tests. LIVE remains NOT READY.

## 2026-06-30 - DEFAULT_FILTERS selected-profile artifact parser

### Why the patch was needed
The profile comparison leaderboard could read the uploaded run, but the main dashboard Backtest Result panel showed core DEFAULT_FILTERS metrics as unavailable because it did not resolve the selected profile directory or accepted diagnostics according to the real artifact schema from run `20260630T164308Z`.

### Root cause
The overview result model treated comparison output as leaderboard-only and did not populate the main panel from `profiles/DEFAULT_FILTERS`. Accepted diagnostics also relied on lifecycle-derived paths and did not explicitly fall back to `backtest_orders.csv` plus `lifecycle_calibration_summary.json` when `accepted_orders.csv` was absent.

### Files changed
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_backtest_profile_comparison.py`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime behavior changes
Profile-comparison dashboard results now default `selected_profile_name` to `DEFAULT_FILTERS` and resolve `selected_profile_dir` as `data/backtest/dashboard/<run_id>/profiles/DEFAULT_FILTERS` for real dashboard runs. The main panel reads summary metrics from `order_backtest_summary.csv`, accepted trades from `backtest_orders.csv`, accepted diagnostics/distributions from `lifecycle_calibration_summary.json` with `backtest_orders.csv` fallback, rejected diagnostics from `rejected_orders.csv`, optional rejected-shadow and signal-quality evidence from the profile directory, and filter state from `backtest_filter_state.json`.

### Lifecycle changes
No lifecycle state machine or runtime transition logic changed. The patch only reads exported lifecycle/calibration evidence for dashboard reporting.

### Persistence changes
No SQLite schema migration and no PAPER/LIVE persistence changes. Missing artifact reporting now names the exact expected path and fallback files checked.

### Export/schema changes
No exporter schema changes. The supported dashboard artifact schema is the run root with `backtest_profile_leaderboard.csv/json`, `backtest_run_metadata.json`, and selected profile artifacts under `profiles/DEFAULT_FILTERS/`, including `order_backtest_summary.csv`, `backtest_orders.csv`, `rejected_orders.csv`, `lifecycle_calibration_summary.json`, `backtest_filter_state.json`, `signal_quality_summary.json`, and optional shadow/quality CSVs.

### Tests added
Added a regression fixture matching the `20260630T164308Z` schema, asserting DEFAULT_FILTERS selection, profile-dir resolution, accepted/rejected counts, reject rate, win/loss/open, net PnL, baseline/rescue metrics, accepted diagnostics without `accepted_orders.csv`, rejected diagnostics from `rejected_orders.csv`, corrected leaderboard average trades/day, and stress-test-only ALL_FILTERS_OFF labeling.

### Tests executed
- `pytest -q tests/test_backtest_profile_comparison.py`

### Risks
This is BACKTEST/dashboard reporting only. It does not validate positive expectancy, weaken safety gates, change live order paths, or make ALL_FILTERS_OFF strategy performance.

### Remaining limitations
If optional shadow or signal-quality files are absent, the dashboard reports the absence rather than fabricating diagnostics. Existing historical artifacts with malformed warnings may still need tolerant parsing by consumers outside this dashboard path.

### Migration concerns
None for SQLite or live runtime. Artifact consumers should tolerate the new result fields `selected_profile_name`, `selected_profile_dir`, and `artifact_warnings`.

### Push recommendation
Safe to push after targeted dashboard tests and a full relevant test pass. LIVE remains NOT READY.

## 2026-06-30 - BACKTEST evidence rendering contract replacement

### Why the patch was needed
PR 239 was not merged because its table-by-table dashboard edits caused ping-pong regressions where completed BACKTEST evidence was hidden and failed BACKTEST runs could render misleading diagnostic evidence.

### Root cause
The selected BACKTEST template section did not have one explicit completed-vs-failed rendering contract. Individual diagnostic blocks were guarded independently, so later edits could hide completed-run headings or leak empty diagnostics into failed runs.

### Files changed
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_dashboard_app.py`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime behavior changes
The dashboard selected BACKTEST panel now has one top-level contract: completed runs render the full selected BACKTEST evidence chain, while failed/non-completed runs render only `SELECTED_BACKTEST_UNAVAILABLE_DUE_TO_FAILURE` plus failure details/warnings when available. PAPER SQL panels remain outside that selected BACKTEST evidence section.

### Lifecycle changes
No lifecycle state transition logic changed. Completed-run lifecycle-derived diagnostics remain visible; failed-run selected diagnostics are explicitly unavailable.

### Persistence changes
No SQLite schema or artifact persistence behavior changed.

### Export/schema changes
No artifact parsing semantics, CSV exports, or schema fields changed.

### Tests added/executed
Updated dashboard regression coverage to assert completed selected BACKTEST HTML includes accepted trade diagnostics, rejection reasons, shadow comparison, near-miss evidence, and required diagnostic headings. Updated failed selected BACKTEST coverage to assert diagnostic headings are absent when the selected run fails.

### Risks and limitations
This is a dashboard template/rendering patch only. It does not prove strategy expectancy, tune thresholds, weaken gates, or alter accepted trade counts.

### Migration concerns
None.

### Push recommendation
Safe to push as a clean replacement PR for the BACKTEST Evidence Rendering Contract Phase after dependency-complete dashboard test execution. LIVE remains NOT READY.


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
