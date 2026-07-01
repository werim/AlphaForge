## 2026-07-01 - Diagnostic profile execution-context strictness

### Added
- Added strict execution-context validation for `SHORT_LOW_SCORE_BREAKDOWN_DIAGNOSTIC`, blocking missing/non-numeric/unavailable `effective_rr`, `min_effective_rr`, `cost_penalty`, `liquidity_score`, `spread_pct`, and `expected_slippage_pct` as `EXECUTION_CONTEXT_UNAVAILABLE`.
- Added `.env` example documentation for `ALPHAFORGE_BACKTEST_SHORT_LOW_SCORE_BREAKDOWN_DIAGNOSTIC_SYMBOLS` as a BACKTEST-only reporting scope.
- Added regressions for unavailable spread, slippage, cost, liquidity, effective-RR, and min-effective-RR diagnostic evidence.

### Changed
- Diagnostic SHORT LOW_SCORE BREAKDOWN rows no longer treat missing spread/slippage/cost as zero or missing liquidity as perfect liquidity.

### Fixed
- Fixed execution-realism leakage where unavailable execution context could be interpreted as favorable diagnostic evidence.

### Removed
- Nothing.

### Breaking Changes
- None. This only tightens BACKTEST diagnostic inclusion and does not change DEFAULT_FILTERS, PAPER, or LIVE behavior.

### Known Issues
- Diagnostic sample counts may decrease when execution context is incomplete; this is intentional and preserves reject quality.

## 2026-07-01 - SHORT LOW_SCORE BREAKDOWN diagnostic profile

### Added
- Added BACKTEST-only `SHORT_LOW_SCORE_BREAKDOWN_DIAGNOSTIC` shadow-validation profile for SHORT `BREAKDOWN_DOWN` rows rejected by `LOW_SCORE` in `SHORT_LOW_SCORE_GOOD_UTC_HOURS`, scoped by default to BTCUSDT/ETHUSDT and configurable by `ALPHAFORGE_BACKTEST_SHORT_LOW_SCORE_BREAKDOWN_DIAGNOSTIC_SYMBOLS`.
- Added `diagnostic_short_low_score_breakdown_candidates.csv` and `diagnostic_short_low_score_breakdown_summary.json` with outcome counts, effective shadow R statistics, confidence lower bound, cost/spread/slippage/liquidity distributions, symbol/hour breakdowns, and explicit diagnostic-only rationale.
- Added dashboard rendering for the diagnostic profile as a separate `DIAGNOSTIC ONLY` row with “production thresholds unchanged.”

### Changed
- BACKTEST summaries now expose the diagnostic candidate count without changing accepted counts or default filters.

### Fixed
- Prevented SHORT LOW_SCORE diagnostic discovery from bypassing STOP_TOO_WIDE, HIGH_VOL_GUARD, invalid geometry, effective-RR, cost, spread, slippage, or liquidity sanity gates.

### Removed
- Nothing.

### Breaking Changes
- None. Additive BACKTEST artifacts/dashboard fields only.

### Known Issues
- The profile is not an acceptance path and is not PAPER/LIVE enabled. Latest multi-symbol artifacts still require regeneration to judge whether the bucket has durable positive expectancy after execution costs.

## 2026-07-01 - BACKTEST lifecycle/reject SQL persistence completion

### Added
- Added BACKTEST SQL order-decision persistence during lifecycle export persistence, including additive `sql_order_decision_count` and `sql_rejected_decision_count` diagnostics.
- Added explicit `REJECT_REASON_UNAVAILABLE` fallback for rejected rows only when concrete reject attribution is unavailable.

### Changed
- Missing BACKTEST expectancy now exports `BACKTEST_EXPECTANCY_UNAVAILABLE` to distinguish unavailable historical context from an ambiguous unknown bucket.

### Fixed
- Fixed rejected BACKTEST lifecycle rows being exportable without matching SQL `order_decisions` evidence in the persistence pass.

### Removed
- Nothing.

### Breaking Changes
- None. CSV columns are additive; no SQLite migration.

### Known Issues
- BACKTEST artifact SQL persistence remains run-local/in-memory unless a persistent DB is explicitly wired by callers. LIVE remains NOT READY.

## 2026-07-01 - Reject overlay diagnostics

### Added
- Added BACKTEST-only diagnostic reject overlay labels for LONG breakout session traps, SHORT LOW_SCORE breakdown candidates, near-threshold LOW_SCORE splits, guard no-rescue confirmations, and bucket verdict labels.
- Added `reject_overlay_diagnostics.csv`, `reject_overlay_summary.json`, `reject_bucket_expectancy.csv`, and `reject_bucket_expectancy.json` artifacts.
- Added regression coverage for overlay labels, near-threshold 5% gaps, guard non-bypass, bucket verdicts, and no accepted-count mutation.

### Changed
- Zero-accepted root-cause summaries now include strongest positive/negative diagnostic buckets and an explicit no-production-threshold-change recommendation.

### Fixed
- Prevented missing rejected-forward evidence from becoming an optimistic diagnostic candidate by requiring forward-evaluable first-touch evidence for SHORT breakdown candidate labels.

### Removed
- Nothing.

### Breaking Changes
- None. Additive BACKTEST artifacts only.

### Known Issues
- Diagnostic candidates remain labels only and must not be used to relax LOW_SCORE, HIGH_VOL_GUARD, or STOP_TOO_WIDE without separate production validation.

## 2026-07-01 - Score calibration diagnostics

### Added
- Added `score_calibration_summary.json` BACKTEST artifact with Pearson/Spearman score correlations, monotonicity checks, score source interpretation, and miscalibration flags.
- Expanded `score_calibration_diagnostics.csv` with score buckets and breakdowns by reject reason, regime, and setup type, including raw/effective outcome rates and execution metrics.
- Added BACKTEST-only `calibrated_score`, component, delta, and verdict diagnostics that penalize high volatility, wide stops, overextension, late breakouts, execution costs, spread, and slippage.
- Added regression tests for score calibration exports, reconciliation, high-score SL cluster flags, diagnostic calibrated-score penalties, and no PAPER/LIVE loosening.

### Changed
- Signal-quality diagnostics now include a score calibration summary while preserving existing accepted/rejected lifecycle evidence.

### Fixed
- Fixed the audit gap where score calibration artifacts lacked direct raw WOULD_TP/effective TP correlations and high-score failure cluster flags.

### Removed
- Nothing.

### Breaking Changes
- None. Additive BACKTEST diagnostics only.

### Known Issues
- The calibrated score is not used for acceptance. BTCUSDT 30d/1h artifacts must be regenerated to validate observed correlations and clusters end-to-end.

## 2026-07-01 PR256 diagnostic extraction correction

### Added
- LOW_SCORE diagnostic fields for score threshold source, detected score scale, detected threshold scale, mismatch detection, and correction flags.
- Symbol reject diagnostic fields for nested selector inputs/metrics, reject reasons, sub-scores, and metric source.
- Root-cause evidence-quality reasons.

### Changed
- LOW_SCORE diagnostics now derive thresholds from row-level evidence before falling back to BACKTEST config.
- Symbol reject diagnostics now parse nested `diagnostics.selector` payloads when top-level columns are empty.
- Zero-accepted root-cause evidence quality no longer claims COMPLETE when key diagnostic evidence is missing or invalid.

### Fixed
- Prevented 0-1 BACKTEST fallback threshold from masking 0-10 exported LOW_SCORE thresholds.
- Prevented FEATURE_MISSING verdicts when selector metrics are present inside diagnostics JSON.

### Removed
- Nothing.

### Breaking Changes
- None. Additive diagnostic/export fields only.

### Known Issues
- No production threshold tuning is included; manual artifact regeneration is still required to validate BTCUSDT 30d/1h outputs end-to-end.

## 2026-07-01 - PR255 HIGH_VOL_GUARD correction and zero-accepted bottleneck audit

### Added
- Added corrected HIGH_VOL_GUARD gap/trigger diagnostics, explicit pass/fail counterfactual booleans, and diagnostic-only warning fields.
- Added `low_score_diagnostics.csv`, `low_score_summary.json`, `symbol_reject_diagnostics.csv`, `symbol_reject_summary.json`, and zero-accepted root-cause summary JSON/CSV artifacts.
- Added dashboard parsing/rendering for summary artifacts without loading raw diagnostic rows into the UI.
- Added regression coverage for HIGH_VOL_GUARD gap correctness, LOW_SCORE summaries, symbol reject metrics, and zero-accepted bottleneck reporting.

### Changed
- HIGH_VOL_GUARD summaries now classify far-below-threshold effective RR as protective evidence and recommend continuing LOW_SCORE/symbol-level audits.
- Backtest quality summaries now include HIGH_VOL_GUARD, LOW_SCORE, and symbol-reject verdict/evidence metrics.

### Fixed
- Fixed misleading `counterfactual_volatility_penalty=0` when HIGH_VOL_GUARD rejected effective RR below the high-vol threshold.
- Fixed ambiguity where effective RR was exported as a volatility metric without guard metric name/value/threshold/gap semantics.

### Removed
- None.

### Breaking Changes
- None; artifacts and summary fields are additive.

### Known Issues
- BTCUSDT 30d/1h artifacts still require regeneration in an environment with historical-data access. LIVE readiness remains NOT READY.

## 2026-07-01 - HIGH_VOL_GUARD zero-accepted diagnostics

### Added
- Added `high_vol_guard_diagnostics.csv`, `high_vol_guard_summary.json`, `acceptance_funnel.csv`, and `acceptance_funnel.json` BACKTEST artifacts.
- Added HIGH_VOL_GUARD counterfactual fields showing whether a candidate would pass if the guard were disabled, without changing default acceptance.
- Added dashboard surfacing for the acceptance funnel and HIGH_VOL_GUARD summary/diagnostic artifact paths.
- Added regression coverage for HIGH_VOL_GUARD diagnostics, acceptance-funnel reconciliation, diagnostic profile labeling, and no-guard emission below threshold.

### Changed
- HIGH_VOL_GUARD rejected rows now carry guard source metadata and volatility/context fields needed for audit.
- BACKTEST quality summaries now include HIGH_VOL_GUARD verdict, evidence, recommendation, count, and would-accept-without-guard metrics.

### Fixed
- Fixed the zero-accepted audit gap where HIGH_VOL_GUARD impact could not be separated from other filters in exported artifacts.

### Removed
- None.

### Breaking Changes
- None; new artifacts and summary fields are additive.

### Known Issues
- The real BTCUSDT 30d/1h artifact still needs regeneration to populate the new diagnostics for the observed 20 HIGH_VOL_GUARD rejects. LIVE readiness remains NOT READY.

## 2026-07-01 - BACKTEST quality summary reject-count parity

### Added
- Added explicit `signal_rejected_count`, `symbol_rejected_count`, and `canonical_rejected_count` metrics to `backtest_quality_summary.csv`.
- Added regression coverage for canonical reject totals and dashboard overall reject-rate counting.

### Changed
- `backtest_quality_summary.csv.rejected_count` now follows the canonical rejected total from `rejected_orders.csv` / canonical reject distribution instead of signal-only lifecycle rejects.
- Dashboard overall BACKTEST reject rate now prefers canonical rejected artifact rows when available.

### Fixed
- Fixed an internal contradiction where quality-summary reject distributions included `SYMBOL_REJECTED` rows but `rejected_count` excluded them.

### Removed
- None.

### Breaking Changes
- None. The previous signal-only count remains available as `signal_rejected_count`.

### Known Issues
- Manual BTCUSDT 30d/1h validation still depends on Binance/network availability. LIVE readiness remains NOT READY.

## 2026-07-01 - BACKTEST post-PR251 artifact consistency

### Added
- Added `canonical_reject_reason_distribution` and `raw_gate_reject_reason_distribution` to BACKTEST quality summaries.
- Added RR/effective-RR/expectancy availability flags and source-stage evidence for pre-signal `SYMBOL_REJECTED` exports.
- Added run-local candle artifact pruning to prevent stale symbol candle JSON files from being mistaken as current run inputs.

### Changed
- `reject_reason_distribution` now follows canonical post-attribution rejected rows when the exporter has `rejected_orders.csv` evidence.
- Pre-signal symbol-selector rejects now export `NOT_APPLICABLE_SYMBOL_FILTER` rather than `UNKNOWN` expectancy.

### Fixed
- Fixed quality summary reject distributions diverging from `rejected_orders.csv` / `order_backtest_summary.csv` after canonical attribution.
- Fixed symbol-selector rejects exporting fake `0.0` RR/effective RR without availability semantics.
- Fixed BTCUSDT-only run directories retaining stale ETHUSDT candle artifacts from previous runs.

### Removed
- Removed zero-as-missing semantics for symbol-selector RR/effective-RR fields in new exports.

### Breaking Changes
- CSV consumers should treat blank RR/effective-RR plus `*_available=false` as not applicable for `SYMBOL_REJECTED` rows.

### Known Issues
- Manual BTCUSDT 30d/1h validation was attempted but blocked by proxy tunnel 403. LIVE readiness remains NOT READY.

## 2026-07-01 - BACKTEST symbol-list parsing hardening

### Added
- Added shared symbol-list normalization and regression coverage for CLI/dashboard symbol inputs.

### Changed
- BACKTEST CLI and PowerShell runner now preserve multi-symbol requests as separate Binance symbols.

### Fixed
- Fixed combined symbols such as `BTCUSDT ETHUSDT` or `BTCUSDT+ETHUSDT` reaching Binance as one invalid kline/funding request.

### Removed
- None.

### Breaking Changes
- Invalid symbol tokens now fail early with a clear validation error instead of relying on Binance rejection.

### Known Issues
- Dynamic top-volume selection still depends on Binance availability when not running offline. LIVE readiness remains NOT READY.

## 2026-07-01 - BACKTEST lifecycle realism evidence completion

### Added
- Added explicit `EXPECTANCY_UNAVAILABLE` bucket behavior for missing BACKTEST expectancy evidence.
- Added canonical `EXECUTION_CONTEXT_UNAVAILABLE` attribution for rejects caused by missing historical execution context.
- Added deterministic fixture artifact coverage for lifecycle counts, score/RR variability, concrete reject distribution, SQL/export parity, unavailable context fields, effective-RR rejection, and dashboard/profile parser consistency.
- Added `rejected_signals.csv` as an additive compatibility export mirroring canonical rejected decision rows.

### Changed
- BACKTEST reject attribution no longer emits the legacy missing-context label and does not collapse missing expectancy into silent `UNKNOWN`.

### Fixed
- Fixed a duplicate unreachable `LOW_EFFECTIVE_RR` return in BACKTEST reject attribution.

### Removed
- None.

### Breaking Changes
- CSV/report consumers may now see `EXPECTANCY_UNAVAILABLE` and `EXECUTION_CONTEXT_UNAVAILABLE` where older artifacts showed `UNKNOWN`/`MISSING_EXECUTION_CONTEXT`.

### Known Issues
- Actual historical bid/ask spread is still unavailable unless supplied by artifacts; estimated spread remains clearly labeled and LIVE readiness remains NOT READY.

## 2026-07-01 - BACKTEST reject reason attribution

### Added
- Added BACKTEST reject attribution fallback from diagnostics, thresholds, expectancy, and execution-context completeness.
- Added `secondary_reject_reasons` export support and threshold diagnostics for reject dashboards.
- Added regression coverage for concrete reject reasons and rejected CSV preservation.

### Changed
- BACKTEST export/summary attribution now reports `LOW_EFFECTIVE_RR` for execution-adjusted RR failures instead of leaving placeholder `UNKNOWN` reasons.
- BACKTEST quality summaries derive concrete distribution keys when rejected rows still contain placeholder reasons.

### Fixed
- Fixed all-rejected BACKTEST runs reporting `UNKNOWN` for every reject reason despite concrete failure evidence.

### Removed
- None.

### Breaking Changes
- No runtime schema break. CSV/report consumers may observe more specific `LOW_EFFECTIVE_RR` values where placeholder `UNKNOWN` appeared before.

### Known Issues
- `UNKNOWN` remains only for genuinely unclassified rejects with insufficient diagnostics; LIVE readiness remains NOT READY.

## 2026-07-01 - Dashboard guardrail section rendering regression

### Added
- None.

### Changed
- Restored the dashboard `Strategy Quality Guardrails` section as a data-gated section when guardrail breakdown, top reasons, or representative examples exist.

### Fixed
- Fixed `/backtest/run` HTML rendering so guardrail source data such as `STOP_TOO_WIDE` appears under the expected guardrail heading instead of only in the generic result table.

### Removed
- None.

### Breaking Changes
- None. Dashboard/template-only change.

### Known Issues
- The section remains hidden when no guardrail source data exists; LIVE readiness remains NOT READY.

## 2026-07-01 - Dashboard BACKTEST accepted-count and guardrail attribution fix

### Added
- Added regression fixtures proving `accepted_count=0` with 12,228 lifecycle rows remains zero accepted trades and zero average trades/day.
- Added exact ALL_FILTERS_OFF profile fixture for 9 accepted trades, 2/7/0 outcomes, and -5.977496714410623 net PnL.
- Added dashboard-side guardrail attribution fallback from later-gate and canonical rejection artifacts.

### Changed
- Dashboard fallback gate funnel now uses canonical `rejected_orders.csv` reason counts plus canonical executed-trade count when exported funnel coverage is missing.
- No-trade profile warnings now remain `NO_EXECUTED_TRADES` / `NO_ACCEPTED_TRADES` and do not inherit `OVERTRADE_RISK` from lifecycle/event rows.

### Fixed
- Fixed residual accepted-trade inflation from lifecycle/diagnostic rows in BACKTEST profile comparison fixtures.
- Fixed guardrail breakdown rendering as unavailable when later-gate/rejection source data exists.
- Fixed DEFAULT gate funnel fallback so visible per-gate counts match canonical reject reasons instead of all-zero rows.

### Removed
- Removed the BACKTEST gate-funnel fallback that counted `SIGNAL_CREATED` rows as accepted trades.

### Breaking Changes
- None for trading/runtime behavior. Dashboard reporting may show lower accepted counts where prior artifacts counted lifecycle events.

### Known Issues
- Guardrail attribution remains artifact-derived reporting; it does not replay rejected candidates or alter strategy decisions. LIVE readiness remains NOT READY.

## 2026-07-01 - Dashboard BACKTEST profile timeout handling

### Added
- Added positive subprocess timeout validation before `subprocess.run`.
- Added per-profile TIMEOUT metadata artifacts and PARTIAL profile-comparison results.
- Added regression tests for timeout containment, completed profile preservation, and dashboard rendering.

### Changed
- Profile comparison now handles each profile timeout independently so completed profiles remain listed and DEFAULT_FILTERS can still populate the dashboard when ALL_FILTERS_OFF times out.
- Dashboard rendering now shows PARTIAL results and per-profile statuses.

### Fixed
- Fixed uncaught dashboard crashes from `subprocess.TimeoutExpired` during BACKTEST profile comparison.
- Prevented non-positive timeout values from being passed to `subprocess.run`.

### Removed
- None.

### Breaking Changes
- None for trading/runtime behavior. Artifact consumers should handle added status fields and null metrics for timed-out profiles.

### Known Issues
- Timed-out profile metrics are unavailable until rerun; LIVE readiness remains NOT READY.

## 2026-06-30 - BACKTEST profile metric integrity

### Added
- Added canonical accepted-trade source tracking, lifecycle/rejected row counts, no-trade warnings, guardrail reject breakdowns, top guardrail reasons, and representative guardrail reject examples.
- Added regression tests for zero-accepted profiles with large lifecycle exports, rejected lifecycle states, accepted effective RR isolation, no-trade overtrade warnings, ALL_FILTERS_OFF executed summaries, and gate-funnel comparability labeling.

### Changed
- Profile comparison and leaderboard ranking now prefer canonical executed trade evidence over lifecycle row counts and rank no-trade profiles below profiles with executed trades.
- `default_gate_funnel.csv` rows now expose scope/comparability notes when gate rejects are absent or not directly comparable to summary rejection counts.

### Fixed
- Fixed profile comparison accepted trade inflation caused by treating lifecycle diagnostic rows as accepted trades.
- Fixed accepted effective RR distributions for no-trade profiles so rejected/lifecycle diagnostic rows do not populate accepted distributions.

### Removed
- None.

### Breaking Changes
- Dashboard comparison artifacts may show lower accepted trade counts where previous outputs incorrectly counted lifecycle events.

### Known Issues
- This is a reporting integrity patch only; it does not prove strategy expectancy or LIVE readiness.

## 2026-06-30 - Purpose-specific environment profiles

### Added
- Added `.env.test.example`, `.env.medium.example`, and `.env.live.example` profiles for diagnostic BACKTEST, balanced PAPER, and hardened LIVE preparation.
- Added regression tests for profile presence, required variables, placeholder secret safety, LIVE-vs-TEST strictness, diagnostic labeling, and README references.

### Changed
- Updated `.env.example` into the safe medium PAPER-oriented default with pointers to purpose-specific templates.
- Documented environment profile copy commands for Windows PowerShell and macOS/Linux.

### Fixed
- Reduced mode confusion by separating loose diagnostic thresholds from PAPER defaults and LIVE readiness preparation defaults.

### Removed
- None.

### Breaking Changes
- None. Example templates only; runtime trading logic is unchanged.

### Known Issues
- LIVE readiness remains NOT READY until local evidence, credentials, reconciliation, lifecycle, and operator guard checks pass consistently.

## 2026-06-30 - RejectedShadowEvaluation test fixture alignment

### Added
- None.

### Changed
- Updated the dashboard score-saturation regression fixture to include the required `RejectedShadowEvaluation` execution diagnostic fields.

### Fixed
- Fixed the direct dashboard test fixture after constructor expansion for spread, liquidity, volatility, TP-hit, cost-penalty, and execution-ok fields.

### Removed
- None.

### Breaking Changes
- None. Test-only fixture alignment.

### Known Issues
- Dashboard-specific tests remain import-skipped when optional dashboard dependencies are unavailable in the environment.

## 2026-06-30 - Strategy Quality Guardrails phase

### Added
- Added BACKTEST-only DEFAULT_FILTERS strategy-quality guardrails for trade frequency, loss-streak pauses, score saturation, and high-vol acceptance.
- Added `strategy_quality_guardrails.json/csv` evidence exports with accepted before/after, rejected-by-guardrails, PnL, profit factor, drawdown, loss-streak, score=10 TP/SL, and high-vol before/after fields.
- Added profile-quality PASS/FAIL fields and threshold exports to `order_backtest_summary.csv`.
- Added regression tests for overtrade reduction, loss-streak pause, score saturation, high-vol flooding, diagnostic-only high-vol profile labeling, and env coverage.

### Changed
- `DEFAULT_FILTERS` is now conservative and strategy-quality oriented; high-vol momentum exploration is explicitly diagnostic-only unless thresholds pass.

### Fixed
- Prevented score=10 candidates from being accepted solely because raw score is saturated when secondary execution/regime/RR quality is weak.

### Removed
- None.

### Breaking Changes
- BACKTEST DEFAULT_FILTERS accepted trade counts may decrease because weak clusters and high-variance saturated-score candidates are now rejected with auditable guardrail reasons.

### Known Issues
- Before-guardrail PnL/profit-factor/drawdown remain unavailable unless rejected candidates are separately replayed; the export preserves nulls instead of fake counterfactual PnL.

## 2026-06-30 - DEFAULT_FILTERS overtrade diagnostics and drawdown exports

### Added
- Added `default_gate_funnel.csv`, `equity_curve.csv`, and `symbol_regime_acceptance_diagnostics.csv` BACKTEST exports.
- Added blocking dashboard warnings for `OVERTRADE_RISK`, score=10 SL-dominance saturation, and DEFAULT profile not strategy-quality status.
- Added drawdown, drawdown percent, win/loss streak, and profit factor metrics from accepted terminal trades.
- Added regression coverage for score saturation table rendering, overtrade warnings, drawdown metrics, visible zero-reject gates, and WOULD_SL-dominated quality-candidate suppression.

### Changed
- Score Saturation Diagnostics can now render from `score_10_by_regime` and `score_10_by_reject_reason` JSON diagnostics.
- Top Quality-Improvement Candidates now reports when no positive-expectancy candidate qualifies because high-RR near misses are WOULD_SL dominated.
- Return and net PnL display now includes unit labels so risk-percent sums are not confused with USDT PnL.

### Fixed
- Fixed invisible score=10 diagnostics when JSON saturation splits existed but bucket-table rows were absent.
- Fixed unavailable max-drawdown ranking inputs when accepted terminal trades can construct an equity curve.

### Removed
- None. No filters were loosened and no rejected shadows are converted to accepted trades.

### Breaking Changes
- None. BACKTEST exporter/dashboard diagnostics only; PAPER/LIVE runtime and order placement are unchanged.

### Known Issues
- The patch surfaces likely causes of the 11 -> 354 accepted-trade jump but does not recalibrate STOP_TOO_WIDE or score thresholds. DEFAULT_FILTERS remains not LIVE-ready when overtrade and score saturation warnings fire.

## 2026-06-30 - BACKTEST dashboard dynamic top-volume universe validation

### Added
- Added dashboard validation coverage for blank SYMBOLS with positive MAX SYMBOLS dynamic universe selection.
- Added `--max-symbols` as a BACKTEST runner alias for the existing top-volume universe cap.

### Changed
- BACKTEST dashboard command construction now omits `--symbols` for dynamic universe runs and passes the MAX SYMBOLS cap explicitly.
- Backtest result rendering labels dynamic runs as selected by MAX_SYMBOLS until exported selected symbols are available.

### Fixed
- Fixed dashboard validation incorrectly rejecting blank SYMBOLS when MAX SYMBOLS is greater than 0.
- Fixed invalid blank/zero dynamic universe requests to return a clear operator-facing validation message.

### Removed
- None.

### Breaking Changes
- None. BACKTEST dashboard/form boundary only; PAPER/LIVE runtime and safety gates are unchanged.

### Known Issues
- Dynamic universe runs still require available top-volume metadata and sufficient historical candles from the existing BACKTEST runner path.

## 2026-06-30 - DEFAULT_FILTERS accepted-reason scope and STOP_TOO_WIDE recoverable diagnostics

### Added
- Added diagnostic-only STOP_TOO_WIDE recoverable candidate reporting grouped by symbol, side, regime, effective-RR bucket, and shadow outcome.
- Added selected-profile regression coverage proving accepted=10, baseline/rescue=9/1, and accepted reason breakdown BASELINE=9 / SHORT_BREAKDOWN_RESCUE=1.

### Changed
- Selected BACKTEST main panel now prefers selected-profile `backtest_orders.csv` for accepted-reason breakdown.

### Fixed
- Fixed accepted-reason breakdown leakage from aggregate/profile-comparison scope into the selected DEFAULT_FILTERS main panel.

### Removed
- None.

### Breaking Changes
- None. BACKTEST/dashboard reporting only; PAPER/LIVE runtime and safety gates are unchanged.

### Known Issues
- STOP_TOO_WIDE highlighted candidates are calibration diagnostics only and do not imply gate relaxation or LIVE readiness.

## 2026-06-30 - DEFAULT_FILTERS selected-profile artifact parser

### Added
- Added regression coverage for the `20260630T164308Z` profile-comparison artifact schema rooted at `profiles/DEFAULT_FILTERS`.
- Added selected profile metadata on dashboard results so the overview can audit `selected_profile_name` and `selected_profile_dir`.

### Changed
- Profile-comparison dashboard results now default the selected strategy profile to `DEFAULT_FILTERS` and populate the main Backtest Result panel from `profiles/DEFAULT_FILTERS/order_backtest_summary.csv`.
- Accepted diagnostics now load without `accepted_orders.csv` by checking `backtest_orders.csv` and `lifecycle_calibration_summary.json`.
- Leaderboard `avg_trades_per_day` is recomputed as accepted trades divided by the effective/requested window days.

### Fixed
- Fixed main Backtest Result metrics showing unavailable for accepted trades, rejected signals, reject rate, win/loss/open, net PnL, baseline/rescue PnL, accepted distributions, and rejected diagnostics when comparison artifacts use the real profile directory schema.
- Fixed `OVERTRADE_RISK` warnings caused by artifact `avg_trades_per_day` values that were equal to total accepted trades.
- Fixed overview template rescue/baseline field names to match the result model.

### Removed
- Removed the implicit requirement that `accepted_orders.csv` exist for accepted diagnostics.

### Breaking Changes
- None. BACKTEST/dashboard reporting only; PAPER/LIVE runtime and safety gates are unchanged.

### Known Issues
- LIVE remains NOT READY. Missing artifacts are now reported explicitly but cannot be reconstructed when exporters do not emit them.

## 2026-06-30 - BACKTEST evidence rendering contract replacement

### Added
- Added explicit completed-vs-failed selected BACKTEST rendering contract coverage in dashboard tests.

### Changed
- Reworked the selected BACKTEST dashboard result panel so completed runs render the full artifact evidence chain under one branch.
- Failed selected BACKTEST runs now render only the unavailable marker and failure details instead of diagnostic tables or empty-state evidence.

### Fixed
- Fixed regressions that could hide `Accepted Trade Diagnostics`, `Backtest Top Rejection Reasons`, `LOW_SCORE Shadow Comparison`, or `Top Near-Miss Rejected Signals` for completed runs.
- Fixed failed-run rendering so selected BACKTEST diagnostics are not shown or substituted from stale PAPER SQL evidence.

### Removed
- Removed duplicate/stale per-table failed-run branching from the selected BACKTEST diagnostics section.

### Breaking Changes
- None; dashboard rendering contract only. BACKTEST decision logic, artifact parsing, PAPER/LIVE runtime loops, and LIVE readiness are unchanged.

### Known Issues
- LIVE remains NOT READY. Local environment could not install FastAPI/httpx because package index access returned 403, so dashboard-specific tests are dependency-limited here.


## 2026-06-30 - BACKTEST daily timeframe support and truthful failures

### Added
- Added Binance historical candle support for BACKTEST `4h` and `1d` intervals.
- Added dashboard run metadata artifacts with requested/effective timeframe, requested window, symbols, failure reason, and filter-state evidence.
- Added regression coverage for daily pagination, unsupported timeframe classification, failed metadata, and failed-dashboard diagnostic isolation.

### Changed
- Dashboard-supported BACKTEST timeframe choices now derive from backend historical interval support for `1m`, `15m`, `1h`, `4h`, and `1d`.
- Historical coverage failures now include requested_start, requested_end, symbol, interval, returned_count, and required_min_count.

### Fixed
- Unsupported historical intervals now report `UNSUPPORTED_TIMEFRAME` with requested interval, supported intervals, and source function instead of being mapped to not-enough-data messaging.
- Failed BACKTEST rendering now marks selected diagnostics unavailable and does not substitute stale PAPER diagnostics.
- Failed-run filter metadata no longer falsely implies all optional filters were disabled when the default profile was requested.

### Removed
- Nothing.

### Breaking Changes
- None; artifact fields are additive. PAPER/LIVE behavior is unchanged.

### Known Issues
- LIVE readiness is unchanged and remains NOT READY.


## 2026-06-30 - BACKTEST profile comparison runner

### Added
- Added optional dashboard BACKTEST profile comparison for DEFAULT_FILTERS, ALL_FILTERS_OFF, STRICT_FILTERS, CUSTOM_CURRENT_UI, and diagnostic guard profiles.
- Added comparison and leaderboard artifacts with raw net PnL and risk-adjusted objective-score rankings plus bucket-level net-PnL diagnostics.
- Added documentation for BACKTEST-only comparison behavior and filters-off stress-test semantics.

### Changed
- Dashboard backtest form now has an opt-in Run profile comparison checkbox; default single-profile behavior remains unchanged.

### Fixed
- Replaced comparison-mode placeholder semantics with real per-profile metrics when comparison mode is selected.
- Fixed comparison runner isolation so DEFAULT/STRICT/diagnostic profiles no longer inherit current UI-disabled filters, and all sub-runs receive a fixed start/end window for input parity.
- Added regression coverage for no filter leakage, stable date-window parity, BACKTEST-only command mode, and dashboard warning rendering.

### Removed
- Nothing.

### Breaking Changes
- None.

### Known Issues
- Multi-window 30/90/180/365 support is scaffolded; non-selected windows are marked NOT_RUN. LIVE remains NOT READY.

## 2026-06-30 - BACKTEST SHORT Breakdown Rescue Reporting Experiment

### Added
- Added disabled-by-default BACKTEST-only `SHORT_BREAKDOWN_RESCUE` activation path with conservative 0.25x default sizing and export-visible rescue metadata.
- Added `.env.example` controls for SHORT breakdown rescue enablement, max-per-day, allowed reasons, minimum effective RR, minimum shadow expectancy, and size multiplier.
- Added dashboard separation for BASELINE accepted trades, RESCUE accepted trades, and reporting-only quality gates.
- Added regression coverage for disabled baseline parity, enabled rescue rows, SHORT-only eligibility, LOW_SCORE LONG exclusion, metadata population, BACKTEST-only filter-state marking, and PAPER/LIVE non-activation.

### Changed
- `backtest_filter_state` now records BACKTEST-only experiment switches separately from standard optional filters.

### Fixed
- None.

### Removed
- None.

### Breaking Changes
- None; artifacts receive additive fields only.

### Known Issues
- Rescue remains BACKTEST-only and experimental; rejected-shadow expectancy is diagnostic evidence, not LIVE readiness.

## 2026-06-30 - BACKTEST filter-state audit and filters-off damage diagnostics

### Added
- Added machine-readable `backtest_filter_state.json` / `.csv` artifacts identifying optional BACKTEST filters, enabled/disabled state, source, affected reject reasons, hard safety gates, and ALL_OFF warnings.
- Added `backtest_filter_profile_comparison.json` scaffold for DEFAULT / ALL_OFF / CUSTOM artifact-only comparison.
- Added accepted-trade loss diagnostics by score bucket, regime, side, symbol, effective-RR bucket, high effective-RR accepted outcomes, and score=10 accepted net PnL.
- Added `docs/backtest_filter_switch_audit.md` with switch mapping, hard safety gates, naming notes, and filters-off interpretation.

### Changed
- Dashboard backtest results now label filter profile (`ALL_OFF`, `DEFAULT`, `CUSTOM`), enabled/disabled optional filters, hard safety gates, and all-off diagnostic warning.
- Score saturation and filters-off evidence remain diagnostic-only; default thresholds are unchanged.

### Fixed
- Backtest artifacts now prove that accepted diagnostics remain exportable while filter-state evidence is attached to each run.

### Removed
- None.

### Breaking Changes
- None. BACKTEST-only diagnostics; PAPER/LIVE behavior is unchanged.

### Known Issues
- Profile comparison is artifact-first: operators must run DEFAULT, ALL_OFF, and CUSTOM profiles to fill all comparison cells across 30/90/180/365 days. LIVE remains NOT READY.

# Changelog

## 2026-06-30 - Dashboard BACKTEST SHORT_BREAKDOWN_RESCUE switch

### Added
- Added a dashboard BACKTEST-only `SHORT_BREAKDOWN_RESCUE experiment` toggle that defaults off and passes `ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED` as a scoped run environment override.
- Added rescue experiment state and baseline/rescue accepted-count, net-PnL, combined-PnL, and accepted-reason breakdown display in dashboard backtest results.
- Added `backtest_filter_state.json` / `.csv` experiment evidence marking SHORT_BREAKDOWN_RESCUE as BACKTEST-only, disabled by default, and PAPER/LIVE-neutral.
- Documented the supported dashboard runner path as `backtest_order.py`; no `python -m alphaforge.backtest.runner` package entrypoint exists in this repo.

### Changed
- The dashboard package now lazily imports the FastAPI app factory so non-web backtest-control helpers remain importable without optional FastAPI dependencies.

### Fixed
- Dashboard-launched baseline runs now explicitly scope the rescue env value to `false`, while rescue comparison runs scope it to `true` without mutating `.env`.

### Removed
- None.

### Breaking Changes
- None. BACKTEST-only experiment wiring; PAPER/LIVE behavior is unchanged.

### Known Issues
- Dashboard HTML rendering tests still skip when optional FastAPI/httpx dependencies are unavailable. LIVE remains NOT READY.

## Unreleased

### Added
- Added accepted-trade quality diagnostics for TP/SL rate and expectancy by score bucket, regime, effective-RR bucket, side, symbol, and hour/session.
- Added score calibration diagnostics comparing score buckets to TP/SL/TIMEOUT, net PnL, effective RR, and expectancy buckets.
- Added disabled-filter acceptance evidence to quality summaries, including accepted-because-disabled count and estimated PnL impact when exported.
- Added regression tests for effective-RR `RR_TOO_LOW` gating, default regime mismatch safety, accepted quality diagnostics, score=10 saturation diagnostics, and disabled-filter metadata.

### Changed
- Raised the typed default `MIN_EFFECTIVE_RR` from 1.10 to 1.60 so accepted BACKTEST/PAPER/LIVE decisions require stronger execution-adjusted reward before trade count can increase.
- Made `RR_TOO_LOW` explicitly evaluate execution-adjusted RR, not only raw RR, while preserving BACKTEST-only disabled-filter experiments.

### Fixed
- Fixed the backtest execution reject path so `LOW_EFFECTIVE_RR` uses the configured minimum effective RR instead of a hardcoded 1.10 threshold.

### Removed
- None.

### Breaking Changes
- Conservative default quality tightening may reduce accepted trades in BACKTEST/PAPER/LIVE when overrides are not set.

### Known Issues
- Score calibration remains diagnostic-only; no curve-fit score bucket filter was added from the 90d BTC/ETH run.
- LIVE readiness remains rejected until lifecycle, persistence, execution realism, and stable PAPER expectancy are proven.

### Added
- Added accepted-trade diagnostics aliases for stop loss, take profit, exit price, gross PnL, net PnL, and fee/cost evidence.
- Added score saturation diagnostics with score-bucket WOULD_TP/WOULD_SL/TIMEOUT splits, score=10 TP/SL rates, accepted bucket splits, and rejected shadow bucket splits.
- Added DAILY_GLOBAL_TRADE_LIMIT near-miss diagnostics with symbol, side, timestamp, effective RR, score, shadow outcome, net-outcome direction, and same-day accepted trade context.
- Added a conservative dynamic trade-limit proposal artifact that is disabled by default.
- Added regression coverage for accepted diagnostics population, score saturation exports, daily limit diagnostics, and no default opt-in trade-frequency increase.

### Changed
- BACKTEST lifecycle export now extracts accepted trade geometry and close/PnL evidence from lifecycle `execution_ctx` when available.
- Dashboard accepted diagnostics now merge entry and close execution contexts so close rows do not erase side/entry/SL/TP evidence.

### Fixed
- Fixed accepted diagnostics reporting synthetic-empty `None` values when lifecycle/order artifacts contained accepted trade geometry or closed-trade PnL evidence.

### Removed
- None.

### Breaking Changes
- None; CSV/JSON changes are additive.

### Known Issues
- LOW_SCORE is not relaxed because current shadow comparison shows weak TP/SL separation.
- High effective RR alone is not enough for acceptance because current near-miss evidence still skews WOULD_SL after costs.
- LIVE remains NOT READY.

## Unreleased

### Added
- Added `runtime_filter_config(...)` as the canonical shared filter map for BACKTEST/PAPER/LIVE runtime paths.
- Added regression coverage proving env/config score, effective-RR, spread, funding, liquidity, stale-data, slippage, and max-symbol filters change real decisions or selection behavior.

### Changed
- Runtime symbol selection and runtime-risk gates now consume canonical spread, slippage, funding, liquidity, stale-data, cooldown, concurrent-position, raw-RR, and effective-RR settings.
- LIVE config wiring now follows the same canonical fields while preserving existing live qualification and no-unsafe-order safety guards.

### Fixed
- Wired `MIN_EFFECTIVE_RR`, `MAX_SPREAD_BPS`, `MAX_SLIPPAGE_BPS`, `ALPHAFORGE_MAX_EXPECTED_SLIPPAGE_PCT`, and `MIN_LIQUIDITY_USD` into real runtime selection/decision/risk consumers instead of leaving them as partial conventions.

### Removed
- None.

### Breaking Changes
- None; direct legacy order-quality calls retain compatibility defaults, while runtime paths use stricter canonical filters.

### Known Issues
- Historical execution context can still be unavailable; missing evidence is flagged/fail-closed rather than backfilled with fake measured zeros.

## Unreleased

### Added
- Added real BACKTEST-only reject filter switches for LOW_SCORE, TOO_CHOPPY, WEAK_TREND_AND_NO_RANGE_EDGE, STOP_TOO_WIDE, RR_TOO_LOW, DAILY_SYMBOL_TRADE_LIMIT, REGIME_MISMATCH, and PANIC_CONDITIONS.
- Added disabled-filter bypass evidence to BACKTEST summaries and calibration artifacts.
- Added dashboard checkboxes that pass real BACKTEST decision switches into the backtest command.
- Added regression tests proving the switches change decision flow rather than only dashboard display.

### Changed
- Hardened `.env.example` so the new active BACKTEST filter variables are wired to config and decision logic.

### Fixed
- Prevented experimental BACKTEST filter switches from affecting PAPER/LIVE decisions.

### Removed
- None.

### Breaking Changes
- None; BACKTEST artifact CSVs receive additive metadata columns only.

### Known Issues
- Disabling filters is experimental and can worsen expectancy; it is not LIVE readiness evidence.

## 2026-06-26 - Dashboard rejected-shadow aggregate diagnostics split

### Added
- Added separate strict shadow matching semantics for per-row near-miss rejected-signal enrichment.
- Added aggregate shadow-row handling so reporting diagnostics include shadow-only rows from `rejected_shadow.csv`.

### Changed
- Later-gate diagnostics, LOW_SCORE shadow comparison, STOP_TOO_WIDE rescue diagnostics, and STOP_TOO_WIDE WOULD_TP/WOULD_SL reporting now use aggregate shadow rows instead of strict matched rejected rows.
- Strict near-miss enrichment now prioritizes explicit `signal_id` and falls back only to symbol + timestamp + side.

### Fixed
- Fixed missing STOP_TOO_WIDE later-gate diagnostics when STOP_TOO_WIDE exists only in `rejected_shadow.csv`.
- Fixed STOP_TOO_WIDE WOULD_SL aggregate counts being dropped when the shadow row has no matching rejected row.
- Fixed STOP_TOO_WIDE rescue candidate counts being underreported when shadow-only rejected-shadow rows are present.

### Removed
- Removed aggregate/reporting dependence on strict rejected-row shadow matches.

### Breaking Changes
- None. The patch changes BACKTEST dashboard diagnostics only; thresholds, accepted trade counts, and PAPER/LIVE behavior are unchanged.

### Known Issues
- Dashboard tests require FastAPI/httpx, which are unavailable in this container; module compilation passed but targeted tests were skipped by missing dependencies.

## 2026-06-26 - Dashboard/backtest diagnostics hardening after BTCUSDT 60d/15m

### Added
- Added `later_gate_breakdown.csv` dashboard artifact sourced from candidates that already passed score/RR/expectancy and were rejected by later BACKTEST gates.
- Added regression coverage for accepted diagnostics order-field enrichment, later-gate grouping, high effective-RR WOULD_SL exclusion, and default STOP_TOO_WIDE quality-gate exclusion.

### Changed
- Quality-gate rescue/comparison remains disabled by default and now requires a counterfactual `WOULD_TP` outcome when enabled.
- Default quality-gate allowed reasons exclude `STOP_TOO_WIDE`; it must be explicitly opted in only after positive calibration evidence exists.

### Fixed
- Accepted trade diagnostics now ignore placeholder `NOT_EXPORTED`/`UNAVAILABLE` values and backfill side, entry, SL, TP, exit, and net PnL from matched `backtest_orders.csv` / close execution context when exported.
- Later-gate diagnostics now group only the passed-before-later-gates population instead of all same-reason shadow rows.

### Removed
- Removed `STOP_TOO_WIDE` from default quality-gate rescue reasons.

### Breaking Changes
- None for baseline BACKTEST acceptance; defaults are stricter for optional quality-gate diagnostics and do not loosen global acceptance.

### Known Issues
- Dashboard diagnostics remain dependent on exported artifact completeness; missing order/close fields are still surfaced as unavailable rather than fabricated.

## 2026-06-26 - BACKTEST_ONLY SHORT Breakdown Breakout Normal Stop Quality Gate

### Added
- Added opt-in `SHORT_BREAKDOWN_BREAKOUT_NORMAL_STOP_GATE` BACKTEST-only comparison metrics for SHORT + BREAKDOWN_DOWN + BREAKOUT + NORMAL stop-distance rejected-shadow candidates.
- Added quality-gate summary/export fields for baseline metrics, candidate/accepted/rejected counts, WOULD_TP/WOULD_SL/UNKNOWN counts, TP rate, mean effective RR, expected effective expectancy, size multiplier, reason/symbol breakdowns, and daily trade-count distribution.
- Added CLI flags for enabling and constraining the comparison lane without changing global thresholds.
- Added regression coverage for disabled defaults, BACKTEST-only counting, LIVE/PAPER exclusion, eligibility constraints, and exclusion of WIDE stops, LONG rows, REGIME_MISMATCH, and PANIC/NEWS_DRIVEN regimes.

### Changed
- Backtest summary `accepted_reason_breakdown` now counts accepted lifecycle states instead of `SIGNAL_CREATED` rows, preventing pending/rejected created rows from inflating `BASELINE` counts.

### Fixed
- Fixed accepted-reason breakdown inflation where signal-created rows could report `{"BASELINE": 1043}` instead of unique accepted trade evidence such as `{"BASELINE": 4}`.

### Removed
- None.

### Breaking Changes
- None. The quality gate is disabled by default, BACKTEST-only when enabled, reporting/comparison-only, and does not loosen global thresholds or baseline accepted trades.

### Known Issues
- This is not LIVE-ready and does not authorize LIVE acceptance. Quality-gate expectancy depends on rejected-shadow labels and available execution-cost fields.

## 2026-06-26 - Regime/Side/Setup Quality Gate Diagnostics

### Added
- Added `signal_quality_combo_groups.csv` for side/regime/setup/stop-distance/effective-RR combined signal-quality groups.
- Added `candidate_quality_gates.csv` with reporting-only candidate gate evidence for SHORT breakdown breakout, strict LONG breakout, high-effective-RR SHORT, and recoverable STOP_TOO_WIDE hypotheses.
- Added `score_calibration_diagnostics.csv` for score decile splits by side/regime/setup type plus D10 reject-reason and stop-distance outcome splits.
- Added summary counts and candidate gate details into `signal_quality_summary.json`.

### Changed
- Signal-quality export writing now handles heterogeneous diagnostic rows safely.

### Fixed
- Fixed `accepted_reason_breakdown` in backtest quality summaries so lifecycle `SIGNAL_CREATED`/pending rows do not inflate accepted BASELINE counts.

### Removed
- None.

### Breaking Changes
- None. Thresholds, strategy logic, reject decisions, and accepted trade counts are unchanged.

### Known Issues
- Candidate gates are diagnostics-only. LIVE remains not ready. Accepted diagnostic geometry may still be unavailable in separate dashboard summaries when source artifacts omit it.

## 2026-06-26 - Signal Quality Diagnostics Export Patch

### Added
- Added `signal_quality_summary.json`, `signal_quality_by_group.csv`, and `high_effective_rr_missed_alpha.csv` BACKTEST exports for accepted and rejected-shadow signal quality analysis.
- Added score saturation, STOP_TOO_WIDE WOULD_TP/WOULD_SL split, high effective-RR missed-alpha, and top quality-improvement candidate diagnostics.
- Added dashboard rendering for Signal Quality Diagnostics.
- Added regression coverage for unchanged counts/decisions, score deciles, high effective-RR splits, STOP_TOO_WIDE exports, and unavailable optional fields.

### Changed
- Rejected-shadow rows now carry diagnostic-only setup type, expected slippage, stop-distance, and timeframe-compatible fields when available.

### Fixed
- Fixed missing grouped visibility into what separates rejected WOULD_TP from WOULD_SL cases before threshold review.

### Removed
- None.

### Breaking Changes
- None. Thresholds, rescue acceptance, and strategy logic are unchanged.

### Known Issues
- Diagnostics are only as complete as exported execution context and rejected-shadow labels; missing fields remain unavailable rather than fake-filled. LIVE remains not ready.

## 2026-06-26 - High Effective-RR Rescue Acceptance Lane (BACKTEST-only)

### Added
- Added opt-in `HIGH_EFFECTIVE_RR_RESCUE` acceptance lane for BACKTEST only, disabled by default.
- Added reduced-size rescue metadata: `accepted_reason`, `original_reject_reason`, `rescue_size_multiplier`, `rescue_effective_rr`, and `rescue_decision_context`.
- Added backtest summary/quality metrics comparing baseline accepted trades with rescue candidates, accepted/rejected rescue counts, rescue PnL, score/effective-RR averages, reject reasons, and accepted-reason breakdowns.
- Added regression coverage for disabled baseline parity, BACKTEST-only gating, LIVE exclusion, execution-quality gates, max-concurrent protection, reduced sizing, exports, metrics, and unchanged global thresholds.

### Changed
- Rescue-accepted BACKTEST orders now follow normal lifecycle simulation with default 0.25x risk and explicit rescue metadata.

### Fixed
- None. This does not fix accepted diagnostics geometry unless source lifecycle/order artifacts already contain the required fields.

### Removed
- None.

### Breaking Changes
- None. Rescue is disabled by default and does not loosen existing global reject thresholds.

### Known Issues
- Feature is BACKTEST_ONLY experimental and not LIVE-ready. Accepted diagnostics export geometry can still be incomplete when upstream artifacts omit geometry/PNL fields.

## 2026-06-26 - Accepted Diagnostics Synthetic-ID Export Hardening

### Added
- Added regression coverage proving accepted diagnostics fill side, entry, SL, and TP from `backtest_orders.csv` when lifecycle rows require the canonical symbol/timestamp signal ID fallback.

### Changed
- Accepted diagnostics now preserve the canonical `symbol:timestamp` signal ID for accepted lifecycle rows with missing `signal_id`, enabling order-artifact matching without changing accepted trade count.
- Accepted diagnostics now include explicit `exit_status` alongside `net_pnl_status` when exit/PnL source evidence is not exported.

### Fixed
- Fixed accepted diagnostics leaving side, entry, SL, and TP null when `backtest_orders.csv` contained matching order geometry but lifecycle rows omitted explicit `signal_id`.

### Removed
- None.

### Breaking Changes
- None. Thresholds, accepted counts, reject gates, lifecycle decisions, and strategy logic are unchanged.

### Known Issues
- Exit and Net PnL remain `NOT_EXPORTED` when source artifacts do not contain those fields.

## 2026-06-26 - Accepted Diagnostics Completeness and STOP_TOO_WIDE Rescue Analysis

### Added
- Added STOP_TOO_WIDE rescue-analysis diagnostics for reduced position size, volatility-normalized stops, and structurally valid tighter alternate stops without changing thresholds or accepted trade counts.
- Added accepted diagnostic regression coverage for source CSV geometry, POSITION_CLOSED close reason, explicit Net PnL export status, reporting-only rescue analysis, and unchanged lifecycle counts.

### Changed
- Accepted trade diagnostics now merge accepted lifecycle rows so early score/geometry fields and terminal execution context can both populate the same diagnostic row.
- Accepted diagnostics now include `expectancy_bucket` and `decision_cost_penalty` when those fields are exported by lifecycle/order artifacts.

### Fixed
- Fixed accepted diagnostics losing side, entry, SL, TP, exit, close reason, and Net PnL evidence when the best available values were split between `backtest_orders.csv` and `order_lifecycle.csv` execution context.

### Removed
- None.

### Breaking Changes
- None. Runtime thresholds, reject gates, lifecycle decisions, and trade counts are unchanged.

### Known Issues
- Rescue diagnostics are analysis-only and depend on exported artifact evidence; missing fields remain unavailable rather than being filled with fake assumptions.

## 2026-06-26 - Dashboard Artifact Evidence Integrity Patch

### Added
- Added symbol/timestamp/side rejected-shadow fallback matching for near-miss rows.
- Added accepted-trade diagnostic enrichment from `backtest_orders.csv` and lifecycle `execution_ctx`, including SL, TP, close reason, and `net_pnl_status`.
- Added regression coverage for STOP_TOO_WIDE shadow counts, accepted diagnostic enrichment, explicit cost-penalty naming, and full lifecycle state counts.

### Changed
- Split ambiguous dashboard `cost_penalty` summary into `decision_cost_penalty` and `shadow_cost_penalty` with an explicit cost-basis note.
- Accepted diagnostics table now displays SL, TP, close reason, and Net PnL export status.

### Fixed
- Fixed near-miss rows incorrectly showing `UNAVAILABLE` when matching `rejected_shadow.csv` rows contain `WOULD_TP`, `WOULD_SL`, `WOULD_TIMEOUT`, or `UNKNOWN`.
- Fixed accepted diagnostics dropping side, entry, SL, TP, regime, and close reason when those fields are present in order artifacts or lifecycle execution context.

### Removed
- Removed the single ambiguous execution-cost summary metric that mixed decision and shadow cost penalties.

### Breaking Changes
- None for runtime trading behavior. Dashboard JSON consumers should read `decision_cost_penalty` and/or `shadow_cost_penalty` instead of the old ambiguous `cost_penalty` summary key.

### Known Issues
- Historical artifacts without exported PnL still report `net_pnl_status: NOT_EXPORTED`; this patch does not synthesize fake PnL.

## 2026-06-25 - STOP_TOO_WIDE Soft Risk-Control Patch

### Added
- Added STOP_TOO_WIDE softening config defaults, risk-scale diagnostics, and backtest summary counters for softened/hard-rejected wide stops and STOP_TOO_WIDE shadow outcomes.
- Added regression coverage for high-score softening, low effective-RR hard rejection, extreme stop protection, hard-reject disabled behavior, and spread-gate preservation.

### Changed
- Qualifying high-score, adequate effective-RR, non-extreme wide-stop signals now continue through the accepted lifecycle with reduced risk scale instead of being rejected solely by `STOP_TOO_WIDE`.
- BACKTEST lifecycle rows now persist cost-penalty diagnostics for accepted and rejected decisions when available.

### Fixed
- Fixed incomplete STOP_TOO_WIDE learning visibility by preserving original reject diagnostics on softened candidates and retaining rejected-shadow labels for hard-rejected STOP_TOO_WIDE rows.
## 2026-06-25 - Dashboard Accepted-Trade Diagnostics and Backtest Reject-Rate Clarity

### Added
- Added selected-backtest accepted trade diagnostics, accepted score/effective-RR distributions, and near-miss score/effective-RR distributions to calibration summary output and dashboard rendering.

### Changed
- Labeled top-card rejection metrics as PAPER SQL state and added a selected-backtest reject-rate row using accepted plus rejected summary counts.

### Fixed
- Reduced dashboard ambiguity where PAPER runtime SQL reject rate could be mistaken for the selected BACKTEST artifact reject rate.

### Removed
- None.

### Breaking Changes
- None. Defaults preserve hard rejection for low effective-RR or extreme wide stops.

### Known Issues
- Cost/spread diagnostics remain estimates when historical bid/ask/order-book data is unavailable. LIVE remains blocked by existing readiness gates.
- None.

### Known Issues
- Existing artifacts without accepted lifecycle score/effective-RR fields remain partially unavailable until regenerated. Local dashboard tests require optional FastAPI/httpx dependencies to run instead of being skipped.

## 2026-06-25 - Dashboard Calibration Rejected-Shadow Source Fix

### Added
- Added dashboard calibration loading of `rejected_shadow.csv` and stable signal/composite lookup enrichment for shadow diagnostics.
- Added regression fixtures where LOW_SCORE and later-gate rejects receive counterfactual shadow outcomes and cost penalties only from `rejected_shadow.csv`.

### Changed
- `lifecycle_calibration_summary.json` now computes cost-penalty, LOW_SCORE shadow comparison, later-gate shadow rates, and near-miss shadow fields from rejected-shadow diagnostics when available.

### Fixed
- Fixed incomplete calibration summaries where cost penalties and WOULD_TP/WOULD_SL counts stayed zero/null despite populated `rejected_shadow.csv` artifacts.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- Calibration output is diagnostic only; it does not justify threshold changes or LIVE readiness. Local dashboard tests require optional FastAPI/httpx dependencies to run instead of being skipped.

## 2026-06-25 - Dashboard Calibration Test Import Fix

### Added
- Added the missing `os` import required by dashboard calibration artifact path assertions.

### Changed
- None.

### Fixed
- Fixed CI `NameError: name 'os' is not defined` in `test_dashboard_backtest_shows_top_rejection_reasons_and_diagnostics`.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- Local dashboard tests require optional FastAPI/httpx dependencies to run instead of being skipped by dependency guards.

## 2026-06-25 - Lifecycle Calibration Later-Gate CI Fix

### Added
- None.

### Changed
- Later-gate calibration diagnostics now build an explicit grouped dictionary before iterating reason/source-stage groups.

### Fixed
- Fixed `ValueError: too many values to unpack` when passed score/RR/expectancy rows exist in lifecycle calibration output.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- Local dashboard tests still require optional FastAPI/httpx dependencies to run instead of being skipped by dependency guards.

## 2026-06-25 - Lifecycle Calibration Dashboard Reports

### Added
- Added `lifecycle_calibration_report.csv` grouped by source stage, lifecycle state, reject reason, symbol, regime/volatility regime, and expectancy bucket.
- Added `lifecycle_calibration_summary.json` with rejection funnel, later-gate diagnostics, LOW_SCORE WOULD_TP vs WOULD_SL comparison, execution-cost summaries, and near-miss rejected signals.
- Added regression coverage for calibration artifact generation, selector/actionable split, LOW_SCORE shadow comparison, later-gate traceability, estimated backtest spread labeling, and cost-penalty single application.

### Changed
- Dashboard BACKTEST wording now separates symbol-selector rejects from actionable signal rejects and order/lifecycle rejects.
- Dashboard artifacts now make estimated candle-only spread/slippage diagnostics explicit without pretending historical bid/ask exists.

### Fixed
- Fixed misleading dashboard aggregation that could mix pre-signal selector filters with signal-engine rejection diagnostics.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- Calibration output is diagnostic only; it does not prove LOW_SCORE is protective or justify threshold loosening. LIVE readiness remains unchanged.


## 2026-06-25 - Rejected Shadow Lifecycle Export Integrity

### Added
- Persisted rejected shadow diagnostics into BACKTEST lifecycle SQL/export rows via execution context fields: `shadow_outcome`, `cost_penalty`, `liquidity_score`, `volatility_score`, `liquidity_ok`, and `volatility_ok`.
- Regression tests covering rejected shadow persistence, LOW_SCORE reason retention, liquidity score derivation from candle volume, passing liquidity gates, shadow outcome export survival, WOULD_TP remaining rejected, and accepted-count protection.

### Changed
- BACKTEST liquidity scoring now uses derived historical candle quote volume when symbol metadata lacks quote volume instead of blindly clamping to the minimum value.
- Estimated BACKTEST spread now varies with the corrected liquidity proxy, reducing constant fallback spread artifacts.

### Fixed
- Rejected shadow outcomes were previously written only to `rejected_shadow.csv`; lifecycle export rows now retain the counterfactual label without converting the signal into an accepted trade.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- BACKTEST spread remains an estimate when real historical bid/ask data is unavailable; the export marks it through existing spread source/context fields.

## 2026-06-24 Backtest SYMBOL_REJECTED lifecycle ordering fix

### Added
- Added regression coverage for pre-signal selector rejects, post-signal SYMBOL_REJECTED normalization, BTCUSDT/ETHUSDT 15m lifecycle ordering, export transition validity, and reject-reason completeness.

### Changed
- BACKTEST pre-signal selector rejects now persist under `SYMBOL_SELECTOR:<symbol>:<timestamp>` diagnostic identities so they cannot collide with signal lifecycle identities.
- Post-signal `SYMBOL_REJECTED` rows are normalized to `SIGNAL_REJECTED` with the original `reject_reason` preserved.

### Fixed
- Fixed dashboard backtests failing closed with `has SYMBOL_REJECTED after signal creation` when selector diagnostics shared a signal lifecycle identity.

### Removed
- None.

### Breaking Changes
- None for runtime trading paths. CSV consumers should not treat `SYMBOL_SELECTOR:*` diagnostic ids as orderable signal ids.

### Known Issues
- Selector diagnostics remain BACKTEST diagnostics and do not make missing exchange-context fields available when source data is unavailable.

## 2026-06-24 Backtest order lifecycle diagnostics hardening

### Added
- Added canonical `SYMBOL_REJECTED` lifecycle state for selector-level rejects.
- Added dashboard BACKTEST lifecycle state, lifecycle path, final reject reason, order reject reason, and symbol-selector reject diagnostics.
- Added regression tests for liquidity-score scale consistency and selector reject export truth.

### Changed
- Symbol selector result liquidity now reports the normalized 0..1 contract; the 0..10 liquidity contribution remains available as a diagnostic sub-score.
- BACKTEST rejected selector rows now export `source_stage=SYMBOL_SELECTOR` and `lifecycle_state=SYMBOL_REJECTED`.

### Fixed
- Fixed misleading selector rejects being normalized into `SIGNAL_REJECTED` lifecycle exports.
- Fixed liquidity-score result scale ambiguity between selector diagnostics and execution/order gates.

### Removed
- Nothing.

### Breaking Changes
- None for runtime execution. CSV consumers should treat `SYMBOL_REJECTED` as the selector-level reject state instead of assuming all selector rejects are `SIGNAL_REJECTED`.

### Known Issues
- BACKTEST can still legitimately produce no `ORDER_PLACED` rows when execution-aware gates fail; LIVE remains NOT READY by default.

## 2026-06-24 Dashboard rejection diagnostics and gate mapping audit

### Added
- Added dashboard BACKTEST rejection diagnostics from `rejected_orders.csv`: top reasons, signal-row count, symbol-selector reject count, score/RR/effective-RR distributions, and pre-later-gate pass count.
- Added regressions for dashboard rejection diagnostics, BREAKOUT_UP/BREAKOUT regime alignment, effective RR unit consistency, and liquidity score scale normalization.

### Changed
- BREAKOUT_UP/BREAKOUT_DOWN setup alignment now allows a `breakout` volatility label when the setup and regime are otherwise compatible.
- Symbol selector liquidity diagnostics now explicitly normalize 0..10 liquidity inputs to the 0..1 threshold scale when the configured threshold is fractional.

### Fixed
- Fixed dashboard visibility gap where completed BACKTEST runs with zero accepted trades did not show rejection reason concentration or distributions.
- Fixed a potential BREAKOUT regime gate mismatch for aligned BREAKOUT setups labeled with breakout volatility.

### Removed
- Nothing.

### Breaking Changes
- None.

### Known Issues
- Effective RR can still legitimately reject all candidates when real execution costs reduce raw RR below threshold; LIVE remains NOT READY by default.

## 2026-06-24 Dashboard BACKTEST historical kline pagination diagnostics

### Added
- Added expected candle count helpers and regression coverage proving 30 days of 1m klines requires paginated Binance requests.
- Added dashboard regressions that preserve symbol-specific historical failure detail for multi-symbol BACKTEST runs.

### Changed
- Historical kline coverage validation now uses timeframe-aligned candle boundaries and includes symbol, timeframe, requested start/end, expected count, actual count, and actual first/last timestamps in failures.
- Dashboard historical-data failures now keep the detailed backend artifact/log reason instead of returning only the generic insufficient-data message.

### Fixed
- Fixed false insufficient-data failures caused by comparing arbitrary request end timestamps directly to candle open timestamps.
- Fixed dashboard failure diagnostics that hid which symbol/timeframe/window failed during multi-symbol historical hydration.

### Removed
- Nothing.

### Breaking Changes
- None.

### Known Issues
- Binance API availability/rate limits and long synchronous 1m dashboard runs can still fail closed; LIVE remains NOT READY by default.

## 2026-06-24 LIVE readiness input provenance hardening

### Added
- Added explicit runtime protocols for exchange snapshots, observability probes, and rollback readiness probes.
- Added `readiness_inputs_json` persistence for `live_readiness_reports` so every readiness input records source, type, and timestamp.
- Added regression tests for missing LIVE providers, explicit non-synthetic provider pass wiring, and deterministic offline fixture preservation for PAPER/BACKTEST-style tests.

### Changed
- LIVE qualification now fails closed when exchange snapshot, observability, or rollback readiness providers are not configured.
- Replaced static pass-biased observability/reconciliation defaults with explicit missing-evidence blockers.

### Fixed
- Removed synthetic/default truth from LIVE readiness qualification bootstrap.

### Removed
- Removed hardcoded LIVE qualification snapshots that implied clean reconciliation or operational readiness without providers.

### Breaking Changes
- LIVE qualification callers that relied on only `live_reconciliation_provider` plus static observability/rollback defaults must now configure observability and rollback probes explicitly.

### Known Issues
- LIVE remains NOT READY by default until all existing final gates, persisted operational evidence, and operator acknowledgement are satisfied.

## 2026-06-23 Work 1.3 Core identifier normalization

### Added
- Added Alembic revision `0005_core_identifier_normalization` for nullable core identifier columns and safe join indexes.
- Added bootstrap/migration tests for fresh `init_db()`, fresh Alembic, mixed ordering, indexes, and legacy insert compatibility.

### Changed
- Extended `init_db()` baseline DDL and SQLite additive repair logic so core lifecycle tables share stable identifiers where relevant.

### Fixed
- Reduced schema drift that prevented reliable joins across signals, decisions, orders, positions, PAPER events, BACKTEST events, calibration labels, and optimizer runs.

### Removed
- Nothing.

### Breaking Changes
- None. Changes are additive and nullable; no destructive renames, drops, truncates, or fake backfills were introduced.

### Known Issues
- Legacy rows can still contain null identifiers where AlphaForge has no deterministic source; LIVE remains NOT READY by default.

## 2026-06-23 Work 1.2 Alembic/init_db baseline schema alignment

### Added
- Added Alembic revision `0004_align_init_db_baseline_tables` to create missing baseline runtime/research tables additively.
- Added tests for fresh `init_db()`, fresh Alembic upgrade, `init_db() -> Alembic`, and `Alembic -> init_db()` schema paths.

### Changed
- Made touched Alembic migrations idempotent for existing tables/triggers so direct SQLite bootstrap and Alembic can run in either order.
- Extended `init_db()` baseline DDL to include required runtime tables that Alembic now also covers.

### Fixed
- Fixed schema drift risk where TimesFM evidence/index and required baseline tables could differ between Alembic and direct bootstrap paths.

### Removed
- Nothing.

### Breaking Changes
- None.

### Known Issues
- LIVE remains NOT READY by default; this patch only aligns schema bootstrap and migration coverage.

## 2026-06-23 PR-01 Lifecycle Contract + SQL Truth Audit

### Added
- Added canonical lifecycle constants, legacy-state mapping, unknown-state rejection, and transition-test helpers.
- Added `docs/decision_lifecycle_contract.md` documenting canonical state definitions, required fields, accepted/rejected flow, BACKTEST/PAPER/LIVE distinction, and unavailable execution-cost handling.
- Added lifecycle contract regression tests for canonical acceptance, unknown rejection, legacy `CREATED` mapping, transition validation, and docs/code state parity.

### Changed
- Normalized backtest SQL lifecycle persistence/export rows through the canonical lifecycle contract so legacy/internal states are not emitted as new export truth.
- Updated lifecycle persistence to reject unknown lifecycle states instead of silently saving them.

### Fixed
- Prevented new SQL lifecycle rows from persisting legacy `CREATED` as the first exported lifecycle truth.

### Removed
- Nothing.

### Breaking Changes
- None intended; legacy/internal lifecycle labels are compatibility-mapped at persistence/export boundaries.

### Known Issues
- LIVE remains NOT READY. Score/RR placeholders, reject/cancel reason completeness, and deeper SQL/dashboard export audits remain follow-up work.

## 2026-06-23 Work 1.1 SQLite schema bootstrap stabilization

### Added
- Added regression coverage proving fresh `init_db()` creates `timesfm_forecast_evidence` and `ix_timesfm_evidence_symbol_timeframe_ts`, repeated bootstrap remains idempotent, and the conservative TimesFM evidence columns are present.
- Added conservative TimesFM evidence compatibility columns `forecast_timestamp`, `point_forecast`, and `quantiles_json` to fresh SQLite and Alembic bootstrap DDL.

### Changed
- Kept TimesFM DDL ordered as tables first and indexes after their target tables, with additive legacy-column repair for existing SQLite databases.

### Fixed
- Fixed Work 1.1 bootstrap completeness risk by ensuring the TimesFM evidence table shape exists before its lookup index is created on fresh SQLite databases.

### Removed
- Nothing.

### Breaking Changes
- None.

### Known Issues
- LIVE remains NOT READY by default; this patch only stabilizes SQL schema bootstrap behavior.

## 2026-06-23 SQLite/Alembic config snapshot trigger repair

### Added
- Added idempotent SQLite append-only trigger repair for `config_snapshots` in the Alembic runtime bootstrap migration after the table existence check.

### Changed
- Documentation now records the schema bootstrap trigger-ordering repair and successful regression test run.

### Fixed
- Fixed partial legacy Alembic upgrade paths where `config_snapshots` could be defensively recreated by the runtime bootstrap revision without restoring its SQLite no-update/no-delete triggers.

### Removed
- Nothing.

### Breaking Changes
- None.

### Known Issues
- LIVE remains NOT READY by default; this patch only repairs schema bootstrap metadata and does not alter trading behavior.

## 2026-06-23 Persistence/lifecycle contract regression coverage

### Added
- Added regression coverage for the legacy scalar `fetch_expectancy_stat(...)` contract, separate expectancy metadata detail helper, idempotent legacy runtime-column repair, and accepted backtest `WAITING_ENTRY_ZONE` ordering.

### Changed
- Documentation now records that the persistence/lifecycle contract fixes are guarded by executable tests.
- SQLite legacy lifecycle table repair now adds base lifecycle audit columns before creating the uniqueness index.

### Fixed
- Protected against regressions that would return structured fallback dictionaries from the scalar expectancy API, omit legacy compatibility columns during SQLite bootstrap repair, or skip `WAITING_ENTRY_ZONE` before accepted backtest entry triggers.
- Fixed scalar expectancy SQL execution under SQLAlchemy 2 by wrapping dynamic SELECT statements in executable `text(...)`.

### Removed
- Nothing.

### Breaking Changes
- None.

### Known Issues
- LIVE remains NOT READY by default; this patch adds regression coverage and does not change trading thresholds.

## 2026-06-23 SQLite/Alembic bootstrap regression hardening

### Added
- Added direct regression coverage for TimesFM DDL helper ordering so the evidence table remains before its dependent index.
- Added direct regression coverage that `_apply_sqlite_migrations()` creates `schema_migrations` before reading applied versions on partial SQLite databases.
- Added Alembic fresh-head regression coverage that `config_snapshots` append-only triggers exist after table creation.

### Changed
- Documentation now records the schema bootstrap control-flow protections explicitly.

### Fixed
- Fixed `_apply_sqlite_migrations()` so partial databases that have `schema_migrations` bootstrapped but do not yet have optional lifecycle/review tables do not execute dependent `ALTER TABLE` or lifecycle index DDL against absent tables.

### Removed
- Nothing.

### Breaking Changes
- None.

### Known Issues
- LIVE remains NOT READY by default; this patch only strengthens schema bootstrap tests and documentation.

## 2026-06-23 SQLite/Alembic schema bootstrap repair

### Added
- Added an Alembic runtime bootstrap revision for `timesfm_forecast_evidence`, `timesfm_forward_outcome_labels`, and defensive `config_snapshots` repair on partial legacy databases.
- Added regression coverage for TimesFM SQLite bootstrap order, idempotent row preservation, and Alembic head table/index creation.

### Changed
- Centralized TimesFM SQLite DDL so the evidence table is always emitted before its dependent index.

### Fixed
- Fixed fresh SQLite bootstrap risk where `CREATE INDEX ... ON timesfm_forecast_evidence(...)` could run before the evidence table existed.
- Fixed Alembic fresh-head coverage for runtime TimesFM evidence tables and partial legacy `config_snapshots` absence.

### Removed
- Nothing.

### Breaking Changes
- None.

### Known Issues
- LIVE remains NOT READY by default; this patch only repairs schema bootstrapping and does not alter trading readiness gates.

## 2026-06-23 BACKTEST/PAPER pre-submit parity adapter

### Added
- Added `evaluate_paper_style_pre_submit(...)` for BACKTEST/PAPER no-submit parity checks using shared candidate-quality and execution-cost gates.
- Added parity tests for LOW_SCORE, LOW_EFFECTIVE_RR, EXPECTANCY_MISSING, HIGH_SPREAD, accepted lifecycle audit sequence, and rejected lifecycle audit sequence.

### Changed
- BACKTEST can now run PAPER-style pre-submit effective-RR execution checks without Binance live-order calls when using the adapter.

### Fixed
- Documented and tested the remaining gap between `backtest_order.py` scan flow and `RuntimeOrchestrator._process_symbol`.

### Removed
- Nothing.

### Breaking Changes
- None.

### Known Issues
- The adapter does not enable LIVE and does not remove PAPER runtime-only gates such as kill switch, stale data, cooldown, funding sanity, or exposure limits.


## [Unreleased] - 2026-06-23 (LIVE readiness aggregator CI repair)

### Added
- None.

### Changed
- Kept final gate display on the readiness page while preserving the legacy 27-item readiness probe catalog/API contract.
- TimesFM futures tests now skip cleanly when optional NumPy is unavailable.

### Fixed
- Fixed dashboard readiness probe matrix counts inflated by duplicating final gates into the legacy probe catalog.

### Removed
- Removed final aggregate gates from the probe catalog; they remain in readiness report JSON and dashboard gate tables.

### Breaking Changes
- None.

### Known Issues
- LIVE remains NOT READY by default; final readiness still requires every local gate to pass.

## [Unreleased] - 2026-06-22 (LIVE readiness final gate aggregator)

### Added
- Added a sixteen-gate final LIVE readiness aggregator with explicit verdict levels and machine-readable blockers.
- Added persisted `verdict`, `gates`, and `blockers` fields inside readiness report JSON.
- Added dashboard final-gate table coverage and regression tests for missing gates, lower-gate precheck readiness, kill-switch blocking, and TimesFM non-ordering safety.

### Changed
- Runtime now requires `LIVE_REAL_ORDERS_READY` before allowing LIVE real-order mode; lower verdicts fail closed.
- Dashboard readiness probes include final aggregate gates in addition to underlying evidence checks.

### Fixed
- Closed the gap where partial readiness evidence could be read as a generic pass/fail without an explicit final verdict contract.

### Removed
- None.

### Breaking Changes
- None at schema level; readiness JSON consumers should handle additional fields.

### Known Issues
- LIVE remains NOT READY by default. Missing or stale local evidence for any final gate remains blocking, and PAPER or TimesFM success cannot promote LIVE.

## [Unreleased] - 2026-06-22 (PAPER burn-in report generator)

### Added
- Added `alphaforge.paper_burnin` SQL-first CLI/report generator that writes `paper_burnin_summary.csv`, `paper_burnin_report.md`, and `paper_burnin_blockers.json`.
- Added burn-in classifications for selectivity, insufficient samples, data integrity, lifecycle integrity, execution context, observability, reconciliation, and LIVE-blocked posture.
- Added regression tests for empty DBs, missing reject reasons, bad lifecycle ordering, missing execution context, fake zeros, healthy synthetic selectivity, and optional TimesFM absence.

### Changed
- Documentation now includes PAPER burn-in usage and explicitly keeps LIVE readiness blocked unless independent readiness evidence exists.

### Fixed
- Closed the reporting gap where PAPER evidence had to be inspected manually across multiple SQL tables without a deterministic blocker artifact.

### Removed
- None.

### Breaking Changes
- None; reporting is additive and does not alter runtime decisions, thresholds, schemas, or order paths.

### Known Issues
- LIVE remains NOT READY. Missing heartbeat, reconciliation, rollback, observability, operator, and execution evidence must remain blockers.

## [Unreleased] - 2026-06-22 (Execution realism evidence contract)

### Added
- Added execution evidence classifier statuses: `COMPLETE_MEASURED`, `PARTIAL_ESTIMATED`, `UNAVAILABLE_BLOCKING`, and `INVALID_FAKE_ZERO`.
- Added persisted effective-RR breakdown fields for raw RR and spread, slippage, latency, liquidity, funding, and volatility penalties.
- Added regression tests for missing execution evidence, fake-zero detection, BACKTEST estimated labeling, LOW_EFFECTIVE_RR, LIVE_PRECHECK readiness blocking, and persisted penalty evidence.

### Changed
- PAPER/LIVE-style order prechecks now require measured execution evidence and fail closed on missing or fake-zero required fields.
- BACKTEST estimates must be explicitly labeled as estimated evidence rather than implied zero-cost realism.

### Fixed
- Fixed unavailable execution costs being normalized toward neutral defaults in order evidence.
- Fixed effective-RR evidence gaps by persisting the full penalty breakdown.

### Removed
- None.

### Breaking Changes
- None for valid evidence; incomplete or fake-zero execution contexts now block readiness/precheck paths more explicitly.

### Known Issues
- LIVE remains NOT READY. Measured exchange evidence, reconciliation, rollback, observability, heartbeat, canary, shadow, and operator readiness remain required.

## [Unreleased] - 2026-06-22 (LIVE_PRECHECK no-submit parity evidence)

### Added
- Added `LIVE_PRECHECK` execution mode for evidence-only PAPER parity checks without exchange mutation.
- Added persisted LIVE_PRECHECK evidence fields for input snapshot hash, no-submit verification, parity result, and execution context.
- Added runtime/readiness tests for parity, no-submit behavior, persisted evidence, missing execution context, mismatch blocking, and LIVE_REAL_ORDERS lockout.

### Changed
- Live readiness mode parity now requires explicit no-submit verification and complete execution-context evidence.

### Fixed
- Closed the gap where parity evidence could be considered without persisted no-submit evidence or execution-context completeness.

### Removed
- None.

### Breaking Changes
- None for existing modes; `order_decisions` receives nullable additive columns.

### Known Issues
- LIVE remains NOT READY. LIVE_PRECHECK parity alone does not satisfy reconciliation, rollback, observability, heartbeat, canary, shadow, operator, or real adapter readiness gates.

## [Unreleased] - 2026-06-22 (Dashboard test import CI repair)

### Added
- None.

### Changed
- Imported SQLAlchemy `create_engine` in dashboard tests so audit-event queries execute in full CI.

### Fixed
- Fixed full-suite CI failures in dashboard kill-switch/audit tests caused by a missing test import.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- LIVE remains NOT READY; no runtime behavior changed.

## [Unreleased] - 2026-06-21 (Dashboard kill switch/PAPER-LIVE fail-closed audit)

### Added
- Added persisted `runtime_control_audit_events` records for mode-switch attempts and kill-switch ON/OFF actions.
- Added dashboard tests for kill-switch rendering, restart persistence, switch audit events, LIVE readiness lockout, and secret redaction in HTML responses.
- Added runtime regression coverage proving persisted kill-switch state prevents scanner work.

### Changed
- Dashboard LIVE mode selection now requires explicit operator acknowledgement and PASS readiness evidence before changing requested mode.
- Dashboard overview now displays NOT LIVE-READY messaging when readiness evidence is absent or failing.

### Fixed
- Fixed unsafe dashboard mode-switch ambiguity by auditing blocked LIVE attempts with explicit failure reasons instead of silently accepting LIVE as requested mode.

### Removed
- None.

### Breaking Changes
- Dashboard requests to set LIVE mode now fail closed unless readiness evidence is PASS and acknowledgement is supplied.

### Known Issues
- LIVE remains NOT READY. This patch does not add real order placement or make existing readiness blockers pass.

## [Unreleased] - 2026-06-21 (Rejected decision SQL/CSV integrity)

### Added
- Added canonical rejected-decision artifact persistence that writes signal, order decision, and lifecycle rows with one stable `signal_id`.
- Added regression coverage for LOW_SCORE, RR_TOO_LOW, EXPECTANCY_MISSING, REGIME_MISMATCH, SPREAD_TOO_HIGH, SLIPPAGE_TOO_HIGH, VOLATILITY_TOO_HIGH/LOW, rejected SQL/CSV parity, and unknown reject refusal.

### Changed
- BACKTEST rejected rows now include stable `signal_id`, lifecycle state, execution-context-missing status, expectancy bucket, and cost-adjusted `effective_rr`.
- PAPER runtime reject persistence now uses the canonical rejected-decision helper and carries reject metadata into lifecycle persistence.

### Fixed
- Fixed rejected artifacts that could be persisted with partial SQL evidence, missing stable IDs, or raw RR reused as effective RR despite execution penalties.

### Removed
- None.

### Breaking Changes
- None for valid rejected artifacts; attempts to persist rejected artifacts with empty/UNKNOWN reasons fail closed.

### Known Issues
- LIVE remains NOT READY. Exchange/order reject detail quality still depends on adapter evidence.

## [Unreleased] - 2026-06-21 (Backtest lifecycle truth audit hardening)

### Added
- Added export integrity checks for rejected lifecycle SQL versus `rejected_orders.csv` count consistency.
- Added regression tests for legacy `CREATED`, CREATED-only lifecycle exports, missing lifecycle state/status, fake zero execution context, and suspicious constant score/RR distributions.

### Changed
- BACKTEST export verification now treats persisted lifecycle rows as the audit source and fails closed on lifecycle/reject/export truth defects.

### Fixed
- Fixed audit coverage gaps where malformed lifecycle exports, missing rejected rows, and missing execution context represented as numeric zero could pass verification.

### Removed
- None.

### Breaking Changes
- None for valid exports; invalid BACKTEST artifacts now fail closed instead of being accepted.

### Known Issues
- LIVE remains NOT READY. BACKTEST execution context is still limited by available historical metadata and conservative estimates.

## [Unreleased] - 2026-06-21 (Dashboard runtime control safety hardening)

### Added
- Added persisted runtime control state for requested mode, running mode, kill-switch state/source/time, runtime status, and last error.
- Added dashboard runtime mode, start, stop, kill-switch, and control-status endpoints.
- Added runtime-control and dashboard regression tests for kill switch, PAPER start, LIVE fail-closed errors, duplicate start prevention, and stopped transitions.

### Changed
- Runtime now checks the persisted/global kill switch before startup, scan processing, signal-to-order transition, and PAPER/LIVE execution action.
- Dashboard overview now displays requested mode, actual running mode, runtime status, kill-switch state, last change metadata, and last error.

### Fixed
- Fixed cosmetic-only dashboard control risk by wiring controls to runtime control state and supervisor behavior.
- Fixed silent mode-drift risk by rejecting mismatches between dashboard requested mode and constructed runtime mode.

### Removed
- Removed the prior read-only-only dashboard route assumption for kill-switch/runtime controls; order submission routes remain absent.

### Breaking Changes
- None.

### Known Issues
- LIVE remains NOT READY unless existing runtime/live-readiness guards independently pass.
- Dashboard runtime supervision is intentionally small and fail-closed; external process management may still be preferred operationally.

## 2026-06-19 Dashboard historical refresh hotfix

### Added
- Added `--force-refresh` CLI support for backtest historical candle hydration.
- Added regression coverage for dashboard force-refresh commands, stale-cache refresh attempts, and clean insufficient-data failures.

### Changed
- Dashboard BACKTEST runs now always request fresh Binance historical candles for the selected symbols, timeframe, and period before simulation.

### Fixed
- Fixed stale candle caches causing immediate `HistoricalDataError` failures when cached coverage started after the requested start.
- Fixed dashboard historical data failures to return a clean FAILED result with an operator-facing insufficient-data message.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- Fresh Binance historical data remains dependent on Binance API availability and coverage.

## [Unreleased] - 2026-06-19 (Dashboard BACKTEST control panel)

### Added
- Added a dashboard "Run Backtest" form with last-days, comma-separated symbols, safe timeframe selection, initial balance, and max-symbol controls.
- Added a BACKTEST-only dashboard runner wrapper around the existing `backtest_order.py` pipeline and result artifact summarization.
- Added dashboard tests for form rendering, validation, BACKTEST-only runner invocation, safe failure rendering, and unavailable lifecycle/execution warnings.

### Changed
- Overview dashboard now includes a clearly labeled BACKTEST ONLY control section and result panel.

### Fixed
- Dashboard no longer requires operators to leave the web UI for simple bounded backtest launches while preserving the no-LIVE/no-PAPER safety boundary.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- Dashboard backtest execution is synchronous and may time out on large historical windows.
- Historical data, lifecycle accuracy, and execution context fidelity remain limited to what the existing backtest pipeline and artifacts can provide.

## [Unreleased] - 2026-06-19 (Alembic revision graph integrity repair)

### Added
- Added Alembic revision-graph regression coverage for dangling `down_revision` references, script directory head resolution, and temporary SQLite `upgrade head` execution when Alembic is installed.

### Changed
- Restored the Phase 1 base migration revision identifier to `0001_phase1_init` so the existing adaptive learning lifecycle migration's `down_revision` points to a present base revision.

### Fixed
- Fixed Alembic graph loading failure where `0002_adaptive_learning_lifecycle` referenced missing revision `0001_phase1_init` while the base migration file declared `revision = "0001_phase1"`.

### Removed
- None.

### Breaking Changes
- None for fresh databases. Existing databases stamped with the erroneous `0001_phase1` revision require explicit operator review rather than blind stamping.

### Known Issues
- This patch repairs migration metadata lineage only; it does not change LIVE readiness, execution realism, or runtime trading behavior.

## [Unreleased] - 2026-06-19 (SQLite schema migration bootstrap legacy regression hardening)

### Added
- Extended legacy SQLite bootstrap regression coverage to assert `schema_migrations` is created and records the persistence migration exactly once across repeated `init_db(...)` calls.

### Changed
- Documentation now records the schema migration bootstrap regression hardening follow-up.

### Fixed
- Guarded against regressions where partial legacy SQLite databases with existing runtime rows but no `schema_migrations` table could fail before migrations are applied.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- LIVE readiness remains unchanged and blocked.

## [Unreleased] - 2026-06-19 (SQLite rollback evidence bootstrap)

### Added
- Fresh SQLite bootstrap now creates the canonical `live_rollback_validation_evidence` rollback evidence table and index idempotently.
- Regression coverage verifies rollback evidence schema creation during `init_db(...)`.

### Changed
- SQLite migrations now record the rollback evidence bootstrap migration without changing trading thresholds, scoring, reject logic, lifecycle semantics, or runtime decision behavior.

### Fixed
- Dashboard/readiness rollback evidence queries no longer depend on a later write path to create the rollback evidence table in fresh SQLite databases.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- LIVE readiness remains blocked; this is persistence bootstrap hardening only.

## [Unreleased] - 2026-06-19 (SQLite schema migration bootstrap regression)

### Added
- Added regression coverage proving fresh SQLite initialization creates `schema_migrations` before migration version reads.

### Changed
- Isolated SQLite migration bookkeeping bootstrap in an explicit helper while preserving the same idempotent schema.

### Fixed
- Fixed the migration bootstrap contract so `schema_migrations` exists before selecting applied migration versions.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- LIVE readiness remains unchanged and blocked.

## [Unreleased] - 2026-06-19 (TimesFM unbatched quantile + integration smoke hardening)

### Added
- Added unbatched TimesFM ndarray tuple regression tests for `(horizon, 10)` mean-plus-decile and `(horizon, 9)` older quantile layouts.
- Added an optional real TimesFM integration smoke gated by `ALPHAFORGE_RUN_TIMESFM_INTEGRATION=1`.
- Added a LIVE-mode rejection regression for the TimesFM replay API.

### Changed
- NumPy-shaped TimesFM tests now import NumPy directly so declared dev dependency coverage is not silently skipped in normal development environments.
- TimesFM tuple quantile parsing now uses quantile-specific batch detection to support both batched and unbatched output matrices.

### Fixed
- Fixed unbatched quantile matrices being interpreted as if the first horizon row were the entire forecast series.

### Removed
- Removed NumPy `importorskip` gates from TimesFM ndarray regression tests.

### Breaking Changes
- None.

### Known Issues
- Optional real TimesFM smoke requires externally installed/configured TimesFM package and model weights.
- This module remains PAPER/BACKTEST only and does not add LIVE or order-placement capability.

## [Unreleased] - 2026-06-19 (TimesFM post-merge compatibility hardening)

### Added
- Added deterministic TimesFM wrapper tests for tuple-style point/quantile outputs, NumPy array-like outputs, mean-plus-q10...q90 quantile layout, older nine-quantile layout, legacy `freq` forecast calls, malformed output rejection, and replay `INVALID_FORECAST` logging.
- Added NumPy to development/test dependencies for real ndarray-shaped TimesFM output regression coverage.

### Changed
- Hardened the TimesFM wrapper to try compatible 1.x/2.x forecast call surfaces before failing closed.
- Extended TimesFM output parsing to accept sequence-like list, tuple, and ndarray outputs without requiring NumPy at runtime.

### Fixed
- Fixed quantile extraction for real-shaped TimesFM tuple output where column 0 is a mean and p10 starts at the q10 column, preventing mean values from being mislabeled as p10.
- Fixed malformed tuple output handling so invalid forecasts raise `TimesFMForecastError` and replay records `INVALID_FORECAST` instead of crashing.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- TimesFM inference still requires an externally installed/configured `timesfm` package/model.
- Forecast replay still does not model spread, slippage, funding, liquidity, latency, or live execution and must not be used for LIVE orders.

## [Unreleased] - 2026-06-19 (TimesFM BTCUSDT futures PAPER/BACKTEST forecasting)

### Added
- Added BTCUSDT Binance USD-M Futures OHLCV loader support for 15m and 1h TimesFM research inputs.
- Added a TimesFM forecaster wrapper that lazily uses the optional `timesfm` package and exposes quantile forecasts for horizons 8, 16, and 24.
- Added PAPER/BACKTEST-only historical replay that passes only candles visible at decision time to prevent lookahead bias.
- Added CSV decision logging fields for timestamp, symbol, timeframe, current price, forecast quantiles, side, entry, stop, take-profit, expected RR, and rejection reason.
- Added tests for no-lookahead replay, invalid forecast rejection, LONG, SHORT, and NO_TRADE decisions.

### Changed
- REPORT.md and VERSION.md now document TimesFM module behavior, limitations, and live-readiness impact.

### Fixed
- None.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- TimesFM inference requires an externally installed/configured `timesfm` package/model.
- Forecast replay does not model spread, slippage, funding, liquidity, latency, or live execution and must not be used for LIVE orders.

## [Unreleased] - 2026-05-22 (JOB19 V1 audit diagnostics only)

### Added
- Added `sql/diagnostics/job19_paper_reject_rate_decision_quality_audit.sql` with reusable PAPER runtime SQL checks for reject-rate, reject-reason completeness, missing critical fields, duplicate/inconsistent decisions, score/RR variability, execution-context availability, and lifecycle consistency.

### Changed
- Updated `REPORT.md` with JOB19 V1 audit scope, evidence limitations, and classification framework (`HEALTHY_SELECTIVITY`, `DATA_INTEGRITY_FAILURE`, `EXECUTION_CONTEXT_FAILURE`, `SCORING_OR_REGIME_PIPELINE_FAILURE`, `INSUFFICIENT_SAMPLE`).

### Fixed
- None.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- Real verdict remains blocked without repository-accessible PAPER runtime SQLite evidence artifact.

## [Unreleased] - 2026-05-22 (LIVE qualification startup persistence and forensic redaction precision follow-up)

### Changed
- LIVE qualification startup no longer persists reconciliation findings to `reconciliation_incidents`; fail-closed qualification logic still uses canonical reconciliation counters.
- Qualification observability snapshot now sets `incident_persistence_verified=false` during startup evidence evaluation.
- Mode parity numeric evidence parsing is now defensive and fail-closed on invalid/placeholder values without raising exceptions.

### Fixed
- Forensic runtime snapshot sanitation now preserves benign keys containing `signed` (for example `assigned_symbols`) while still redacting signed/auth/secret payload values and sensitive nested keys.

### Added
- Regression coverage for fail-closed qualification on canonical orphan/duplicate findings without incident writes.
- Regression coverage for invalid parity numeric evidence persistence and non-throwing fail-closed behavior.
- Regression coverage proving `assigned_symbols` survives sanitation while signed/auth/signature values are redacted.

## [Unreleased] - 2026-05-22 (Evidence-based LIVE readiness qualification hardening)

### Added
- Structured fail-closed readiness evidence contract checks for mode parity, observability, and rollback/emergency controls.
- Forensic snapshot sanitization that removes key/secret/signature/signed-header style fields from persisted runtime snapshot payloads.
- Readiness tests covering parity minimum-sample enforcement and forensic secret redaction.

### Changed
- `mode_parity` qualification now requires COMPLETE evidence with minimum samples, zero mismatches, zero missing fields, and no-order-submission verification.
- Observability/rollback readiness checks now require measured evidence fields instead of optimistic booleans.

### Fixed
- Closed gap where static `alerts_configured` / `rollback_ready` booleans could qualify LIVE without measured evidence.

### Known Issues
- LIVE remains ❌ NOT LIVE-READY; alert delivery evidence is still blocking.

## [Unreleased] - 2026-05-22 (LIVE canonical reconciliation evidence-chain hardening)

### Added
- Duplicate-fill detection in canonical reconciliation (`DUPLICATE_FILL`) using exchange `trade_id` with documented fallback compound key.
- Canonical reconciliation finding summary adapter for readiness counters (`orphan_orders`, `orphan_positions`, `duplicate_fills`, `lifecycle_divergences`, `fail_closed_findings`, `stale_orders`).
- LIVE qualification tests proving provider optimistic counters cannot bypass canonical orphan/position detection.

### Changed
- LIVE qualification now passes provider raw `orders`/`positions`/`fills` through `ReconciliationEngine.reconcile(...)` and ignores provider orphan/duplicate summary claims.
- LIVE runtime/readiness fails closed on incomplete reconciliation evidence and on any fail-closed canonical finding.

### Fixed
- Closed false-qualification gap where provider-supplied zero orphan/duplicate counters could be trusted without runtime-intent comparison.

### Known Issues
- LIVE remains ❌ NOT LIVE-READY; no real order submission/cancellation/modification is introduced.

## [Unreleased] - 2026-05-22 (Authenticated Binance read-only LIVE reconciliation evidence)

### Added
- `BinanceReadonlyReconciliationProvider` with signed USER_DATA GET support for `/fapi/v1/openOrders`, `/fapi/v3/positionRisk`, and symbol-scoped `/fapi/v1/userTrades`.
- Deterministic mocked unit tests for request signing, credential redaction, hedge-mode position normalization, and fail-closed behavior.
- Runtime env gate test for LIVE fail-closed when read-only reconciliation is enabled without credentials.

### Changed
- Runtime env bootstrap now supports explicit read-only reconciliation toggles and bounded recvWindow/lookback configuration.
- LIVE runtime wiring can attach read-only reconciliation provider only when explicitly enabled and full credentials are present.

### Fixed
- Closed gap where LIVE had no authenticated exchange reconciliation evidence provider implementation.

### Known Issues
- LIVE remains ❌ NOT LIVE-READY; no real order submission/execution adapter is implemented.

## [Unreleased] - 2026-05-22 (LIVE qualification evidence fail-closed + reconciliation provider requirement)

### Added
- Runtime tests for deterministic scanner provenance assignment and stricter LIVE provenance allowlist blocking.
- LIVE readiness tests asserting fail-closed qualification details are persisted with explicit missing-evidence reasons.
- Runtime reconciliation test asserting LIVE mode blocks when no reconciliation provider is configured.

### Changed
- LIVE startup scanner provenance gate now requires explicit allowlisted provenance (`EXCHANGE_PUBLIC_MARKET_DATA`) instead of blacklist-only checks.
- Runtime bootstrap now deterministically assigns scanner provenance (`SAFE_PLACEHOLDER` override vs exchange-backed source).
- LIVE qualification no longer injects optimistic hardcoded mode parity/reconciliation/observability evidence.

### Fixed
- `_build_runtime_from_env()` now always assigns `scanner_source` before `RuntimeOrchestrator` construction.
- LIVE reconciliation now fail-closes when no explicit reconciliation provider exists, preventing in-memory-only snapshots from being treated as exchange truth.

### Known Issues
- LIVE remains ❌ NOT LIVE-READY; this patch does not add real order placement or authenticated exchange reconciliation reads.

## [Unreleased] - 2026-05-22 (P0 LIVE startup safety + Binance Futures consistency)

### Added
- Runtime regression tests for LIVE scanner provenance blocking and early missing-real-adapter startup blocking.
- Connectivity regression tests asserting Binance Futures endpoint family usage and funding fail-closed behavior.
- Config regression test for default Binance Futures host when `BINANCE_BASE_URL` is unset.

### Changed
- Runtime orchestrator now uses explicit scanner provenance (`scanner_source`) for LIVE startup safety gating.
- Binance connectivity probe now validates Futures orderbook and funding endpoints used by runtime scanner.
- Binance config default/fallback base URL now defaults to `https://fapi.binance.com`.

### Fixed
- Closed LIVE startup bypass where safe scanner could be wrapped by `_runtime_market_scanner` and evade name-based detection.
- Closed delayed LIVE startup failure path by blocking early when `real_execution_adapter` is not configured.
- Removed Spot endpoint qualification path for Binance Futures runtime readiness.

### Known Issues
- LIVE remains ❌ NOT LIVE-READY; no real trading adapter/order placement was enabled.

## [Unreleased] - 2026-05-22 (Binance Futures bookTicker spread hardening follow-up)

### Changed
- Binance scanner now uses Futures-only public endpoint family: `/fapi/v1/ticker/24hr`, `/fapi/v1/ticker/bookTicker`, `/fapi/v1/premiumIndex`.
- Binance `entry` is now conservative (`min(last_price, mid)`), while `spread_pct`/`spread_bps` are derived from `bookTicker` bid/ask.

### Fixed
- Removed Spot `/api/v3` dependency from Binance scanner path.
- Added fail-closed symbol filtering when `bookTicker` spread inputs are unavailable/malformed, avoiding optimistic synthetic spread.

### Added
- Deterministic tests for Futures endpoint URL usage, spread mapping, and malformed payload behavior.

## [Unreleased] - 2026-05-21 (Runtime/env failing-test triage audit)

### Changed
- Added audit documentation for reported runtime/env failures after git pull; current branch reproduces all targeted tests as passing under isolated execution.

### Fixed
- No runtime code fix required on current branch; issue characterized as likely stale DB/env-state contamination outside deterministic test isolation.

### Known Issues
- Historical failure reference includes a now-missing test node name (`test_runtime_rejected_decisions_do_not_persist_incomplete_rows`), suggesting rename/removal drift across branches/CI runs.
## [Unreleased] - 2026-05-21 (Read-only exchange scanner bootstrap alignment)

### Added
- `src/alphaforge/exchange_market_scanner.py` with read-only public Binance/Hyperliquid market scanning (no private API keys, no order submission).
- Tests in `tests/test_exchange_market_scanner.py` for deterministic mocked scanner behavior and exchange-failure fallback.

### Changed
- Runtime bootstrap now uses shared exchange scanner for PAPER/LIVE and reserves `_safe_market_scanner` for BACKTEST/offline override (`ALPHAFORGE_RUNTIME_SAFE_SCANNER=1`).

### Fixed
- Removed placeholder single BTC runtime scanner from default PAPER/LIVE path so runtime rehearsal uses real market-data shape.

## [Unreleased] - 2026-05-21 (LIVE connectivity default fail-closed + startup consistency)

### Changed
- LIVE startup now requires exchange connectivity by default (`RuntimeConfig.require_exchange_connectivity_for_live=True`).
- Runtime env bootstrap now wires `ALPHAFORGE_REQUIRE_EXCHANGE_CONNECTIVITY_FOR_LIVE`, `ALPHAFORGE_REQUIRED_LIVE_EXCHANGES`, and `ALPHAFORGE_EXCHANGE_CONNECTIVITY_TIMEOUT_SEC`.

### Added
- Regression test proving LIVE startup fails closed on exchange connectivity by default.
- Regression test proving LIVE connectivity gate can only be skipped via explicit override.
- Regression assertion covering default non-impact for PAPER mode configuration path.

## [Unreleased] - 2026-05-21 (LIVE placeholder scanner fail-closed gate)

### Fixed
- LIVE startup now blocks when runtime is wired to the bootstrap placeholder scanner (`_safe_market_scanner`) to prevent synthetic local feed use in LIVE mode.

### Added
- Runtime regression test ensuring LIVE cannot start with placeholder/mock bootstrap scanner wiring.

## [Unreleased] - 2026-05-21 (Exchange connectivity safety + opt-in integration checks)

### Added
- New `ExchangeHealth` contract and `check_exchange_connectivity(exchange_name)` for Binance/Hyperliquid public connectivity checks.
- New offline deterministic test module `tests/test_exchange_connectivity.py` with mocked success/failure coverage for Binance and Hyperliquid.
- Optional live integration tests marked `integration`, gated behind `ALPHAFORGE_RUN_EXCHANGE_INTEGRATION=1`.

### Changed
- Runtime LIVE startup can now optionally enforce exchange connectivity via `RuntimeConfig.require_exchange_connectivity_for_live` and `required_live_exchanges`.
- Pytest marker configuration now includes `integration: tests requiring live external services`.

### Fixed
- Missing connectivity failure path coverage for LIVE runtime startup safety.

### Known Issues
- Connectivity checks are public endpoint-only and do not place/cancel orders.

## [Unreleased] - 2026-05-21 (Runtime order_decisions audit semantics hardening)

### Fixed
- Runtime AI/internal `:real:` persistence no longer writes `mode=BACKTEST` during PAPER runtime; mode now reflects the actual runtime mode.
- Canonical runtime rejected decision rows now persist `phase=final` plus score/RR fields so final reject audits are not sparse.

### Changed
- `AIBrain` internal audit rows are now explicitly marked as `phase=ai_internal_real|ai_internal_virtual` to distinguish them from canonical runtime final decisions.
- Live-readiness reject-rate and persistence parity checks now count only canonical final order decisions (`COALESCE(phase,'final')='final'`) to prevent internal-audit double-count inflation.

### Added
- Regression test for PAPER runtime rejected persistence path asserting: no runtime-created row uses `mode=BACKTEST`, canonical final rejected row is populated and counted once, and AI/internal row is explicitly marked non-final.

## [Unreleased] - 2026-05-21 (Runtime rejected decision row completeness)

### Fixed
- `order_decisions` AI persistence rows for runtime `phase=real` rejections now persist non-empty `symbol` and canonical `reject_reason` instead of sparse incomplete duplicates.

### Changed
- Decision persistence upsert payload now includes `symbol`, `mode`, `score`, and `rr` for improved rejected-decision audit completeness.

### Added
- Regression coverage asserting rejected runtime rows do not include incomplete `:real:` paired rows with empty `symbol`/`reject_reason`.

## [Unreleased] - 2026-05-21 (Runtime signal identity + diagnostics hardening)

### Changed
- Runtime now generates/propagates non-empty `signal_id` before reject/lifecycle persistence, including fallback deterministic identity when absent in candidate payload.
- AI decision persistence now derives decision ids with market timestamp entropy to prevent repeated runtime decisions from collapsing into a single upsert row.

### Fixed
- Runtime reject persistence now maps concrete reject reasons into `reject_reason` so known reasons are no longer downgraded to `UNKNOWN`.
- Runtime decision exceptions now emit diagnostic-rich `ERROR` lifecycle events with non-empty `failure_reason` and structured `incident_payload`.
- Runtime lifecycle persistence callback now forwards failure/incident fields explicitly into `trade_lifecycle_events` persistence.

### Added
- Regression tests covering runtime reject signal_id propagation, reject reason preservation, runtime exception lifecycle diagnostics, and decision/features persistence consistency across repeated runtime decisions.


## [Unreleased] - 2026-05-21 (Lifecycle persistence strict-boolean return fix)

### Fixed
- `save_trade_lifecycle_event(...)` now returns literal `True` after successful insert/update + commit instead of returning integer-like row identifiers/rowcount values.
- Preserved existing lifecycle persistence SQL/upsert behavior, event-id generation, and failure semantics.
## [Unreleased] - 2026-05-21 (Runtime SQLite thread-safety fix)

### Changed
- Runtime decision path no longer dispatches `AIBrain.before_real_order` through `asyncio.to_thread`, preventing cross-thread SQLite session use during persistence.
- `AIBrain` now supports `session_factory` for session-per-operation persistence while preserving injected `session` compatibility.

### Fixed
- Resolved `sqlite3.ProgrammingError` caused by reusing a SQLAlchemy `Session` across worker threads in decision persistence flow.

### Added
- Regression test validating `AIBrain` persistence succeeds when `before_real_order` is invoked via `asyncio.to_thread` concurrently using short-lived sessions.



## [Unreleased] - 2026-05-20 (Phase 6.1 audit-trail canonicalization)

### Changed
- Runtime PAPER/backtest-facing lifecycle emissions now prefer canonical lifecycle vocabulary (`SIGNAL_CREATED`, `WAITING_ENTRY_ZONE`, `ENTRY_TRIGGERED`, `ORDER_PLACED`, `SIGNAL_REJECTED`, `POSITION_OPENED`).
- Runtime reject path now guarantees `SIGNAL_REJECTED` emission after `SIGNAL_CREATED` before returning.

### Fixed
- Persistence helpers `save_order_decision` / `save_trade_lifecycle_event` now attempt real durable inserts and return explicit failure on SQL exceptions for fail-detectable behavior.
- Runtime lifecycle persistence callback now fail-detects unsuccessful lifecycle inserts instead of silently continuing.

### Added
- Runtime tests for canonical PAPER lifecycle ordering and reject ordering guarantees.
- Contract transition support for canonical `ENTRY_TIMEOUT` state used in timeout/reconciliation path.

# Changelog

## [Unreleased] - 2026-05-21 (Rejected-shadow SHORT TP/SL fix)

### Fixed
- Rejected-shadow counterfactual simulation now applies directional TP/SL touch rules by side (`LONG`: high>=tp/low<=sl, `SHORT`: low<=tp/high>=sl), fixing SHORT false-negative WOULD_TP outcomes.
- Same-candle TP+SL ambiguity handling is now explicitly documented and conserved as SL-priority for both LONG and SHORT to avoid optimistic bias when intrabar path is unknown.

### Added
- Unit coverage for rejected counterfactual LONG/SHORT TP-only, SL-only, and same-candle TP/SL ambiguity scenarios.
- Regression coverage asserting a valid SHORT rejected setup can produce `shadow_outcome=WOULD_TP` with `effective_tp_hit=True` when effective RR and cost/liquidity/volatility filters pass.

## [Unreleased] - 2026-05-20 (SQLite schema bootstrap compatibility)

### Added
- Idempotent SQLite schema bootstrap helper using `PRAGMA table_info(...)` + additive `ALTER TABLE ... ADD COLUMN` for legacy runtime/backtest DBs.
- Coverage tests for legacy `order_decisions` / `ai_decision_features` migration repair and repeated `init_db()` idempotency.

### Fixed
- Runtime/backtest persistence crashes on existing SQLite files missing additive columns such as `order_decisions.phase` and `ai_decision_features.decision_id`.
- Addeditive schema compatibility checks for write paths that persist lifecycle and closed-trade review payload fields.

## [Unreleased] - 2026-05-20 (Runtime bootstrap smoke scanner + PAPER default)

### Added
- Runtime bootstrap `_safe_market_scanner` now emits one deterministic local smoke-test market candidate with full selector/risk/AI-required fields to exercise scanner→selection→AI→lifecycle→persistence wiring.

### Changed
- Runtime execution mode defaults now resolve to `PAPER` when `EXECUTION_MODE` is absent (`execution_mode_from_env` and `RuntimeConfig.execution_mode`).

### Fixed
- Removed bootstrap behavior that silently defaulted to BACKTEST on missing `EXECUTION_MODE`.

### Known Issues
- Bootstrap scanner remains intentionally synthetic and must not be treated as a live market feed.

All notable documented repository-level changes are summarized from `REPORT.md`.

## [Unreleased] - 2026-05-19 (Spread-unit + calibration/lifecycle persistence fixes)

### Fixed
- Symbol selector spread gating now uses fractional spread semantics (`max_spread_pct=0.0025`) so `0.0035` (0.35%) correctly triggers `WIDE_SPREAD` without weakening other reject gates.
- Backtest symbol market-data builder now propagates `spread_unit_assumed` diagnostics for explicit spread normalization audits.
- Backtest calibration snapshot export insert path no longer assumes an optional `payload_json` column, preventing SQLite runtime failures on existing schemas.
- `ForwardWindowEvaluation` construction is now backward-compatible in tests/callers that omit setup/score/RR quality fields.

## [Unreleased] - 2026-05-16


### Added
- SQLite migration registry table (`schema_migrations`) and idempotent migration note `2026_05_16_persistence_integrity_v1`.
- Additive lifecycle schema hardening columns: `lifecycle_seq`, `cancel_reason`, `lifecycle_id`.
- Backtest export integrity verifier gating lifecycle/rejected CSV consistency.

### Changed
- Legacy `execution_ctx_missing` values now normalize to canonical integer 0/1 during DB init migration.
- Added unique lifecycle replay/index guard on `(signal_id,event_ts,lifecycle_state)` to reduce rerun duplicates.

- Deterministic lifecycle event ID generation for backtest lifecycle rows.
- SQL-backed lifecycle export verification tests, duplicate event ID checks, and idempotency tests for decision/lifecycle upserts.
- Contract parity tests using real BACKTEST/PAPER output fields and runtime paper execution field checks.
- Backtest quality distribution reporting (`backtest_quality_summary.csv`) with reject-rate/reason distributions and effective-vs-raw RR divergence visibility.
- Quality-summary tests validating effective RR and reject-reason distribution accounting.

### Changed
- Backtest lifecycle CSV export source shifted to persisted SQL lifecycle events with deterministic ordering (`event_ts,event_id`).
- Backtest lifecycle persistence path now persists in-memory lifecycle rows before CSV export.
- `execution_ctx_missing` persistence semantics normalized toward canonical 0/1-style behavior in schema/write path.
- Lifecycle persistence semantics updated so `effective_rr` is used when available, falling back to raw `rr` only when effective value is absent.

### Fixed
- Corrected semantic integrity issue where lifecycle persistence could incorrectly store raw `rr` instead of execution-adjusted `effective_rr`.
- Improved rejected lifecycle visibility by documenting persisted/exported SQL-backed rejected rows in backtest lifecycle output path.
- Reduced mixed-type persistence risk for `execution_ctx_missing` with explicit legacy compatibility handling.

### Known Issues
- LIVE mode remains not production-ready.
- Full optional-field and timestamp-typing parity across BACKTEST/PAPER/LIVE is still incomplete.
- Legacy SQLite stores may need migration/rebuild for canonical `execution_ctx_missing` persistence.
- Backtest top-N universe may depend on live Binance endpoints unless fixture mode is used.

- Added canonical contract utilities (`contracts.py`) for lifecycle transitions, reject reason normalization, and UTC timestamp normalization.
- Changed runtime lifecycle callbacks to emit deterministic contract fields (`lifecycle_event_type`, `lifecycle_state`, `timestamp`, `previous_lifecycle_state`).
- Fixed invalid lifecycle transition handling by explicitly emitting/persisting `ERROR` state semantics.


## Generation 3 - Execution Realism Engine Hardening (2026-05-16)
### Added
- Shared deterministic execution-cost model with explicit missing-context semantics and completeness grading.
### Changed
- Effective RR now uses additive execution penalties (spread, slippage, latency, funding, liquidity) instead of optimistic proportional shortcut.
- Real-order decision payload now reports execution-cost completeness and missing fields.
### Fixed
- Unknown execution context now generates explicit rejection flags and does not silently act like measured zero cost.
### Breaking Changes
- Effective RR numeric behavior changed due to new penalty formulation.
### Known Issues
- Regime/liquidity band calibration remains config-light and should be tuned per venue/instrument.

## Generation 4 - Runtime Safety Controls & Reconciliation Layer (2026-05-16)
### Added
- Pre-trade runtime risk gates: global kill switch, stale market data rejection, spread/funding sanity gates, symbol cooldown, duplicate position guard, and max concurrent position guardrails.
- New lifecycle states for deterministic runtime execution and failure semantics (`ENTRY_PENDING`, `ENTRY_SUBMITTED`, `ENTRY_ACKNOWLEDGED`, `ENTRY_PARTIAL`, `ENTRY_FILLED`, `STOP_SUBMITTED`, `TAKE_PROFIT_SUBMITTED`, `CANCEL_REQUESTED`, `RECONCILIATION_REPAIR`, `EXECUTION_ERROR`, `EXCHANGE_REJECT`, `RUNTIME_PROTECTIVE_EXIT`).
- Runtime incident counters and reconciliation repair journaling payloads.
- Lifecycle persistence migration columns: `failure_reason`, `reconciliation_reason`, `incident_payload`.
### Changed
- Runtime accepted-flow lifecycle moved from generic waiting/triggered placement to deterministic entry submission/ack/fill sequencing.
- Timeout/error/missing-ack execution outcomes now emit explicit failure lifecycle events and trigger reconciliation events.
### Fixed
- Reduced silent runtime/exchange drift by forcing uncertain execution outcomes into auditable failure + reconciliation lifecycle rows.
### Known Issues
- Exposure/concentration gate inputs are presently inference-light and depend on market context quality.
- Reconciliation currently journals snapshots but does not yet perform active exchange order amendment/cancel calls.


## Generation 5 - Live Readiness Qualification & Controlled Enablement (2026-05-17)
### Added
- `src/alphaforge/live_readiness.py` deterministic qualification engine with lifecycle/persistence/runtime/statistical/operational gates.
- Qualification report persistence table `live_readiness_reports` and forensic qualification snapshot export helper.
- Runtime LIVE gating config flags for shadow mode, canary mode, and explicit operator acknowledgement.
- Focused tests covering qualification pass/fail, lifecycle orphan detection, runtime live-block behavior, and forensic snapshot integrity.
### Changed
- `RuntimeOrchestrator.start()` now fail-closes LIVE startup when readiness qualification fails.
- LIVE startup now logs readiness report payload for deployment-state visibility and audits.
### Known Issues
- Reconciliation checks currently consume deterministic snapshots and do not yet issue active exchange remediation actions.

## Generation 6 - CSV Export Schema Drift Hardening (2026-05-17)
### Added
- `resolve_csv_fieldnames(rows, preferred_fieldnames)` helper in `backtest_order.py` to build deterministic union CSV schemas.
- Regression test for base-column preservation + alphabetically appended discovered columns.
### Changed
- Row-list CSV export path now derives headers from all rows (preferred base columns first, extra discovered keys appended alphabetically).
### Fixed
- Resolved backtest CSV export failure: `ValueError: dict contains fields not in fieldnames` when later rows include keys missing from the first row.
### Known Issues
- Downstream consumers with rigid CSV header expectations may need to tolerate additive columns.

## Generation 6 - Exchange-Reconciled Live Control Plane (2026-05-17)
### Added
- New reconciliation subsystem `src/alphaforge/reconciliation.py` with structured findings, repair recommendations, snapshot model, and incident persistence table/index creation.
- Continuous runtime reconciliation loop with bounded interval/timeout controls and deterministic fail-closed escalation on severe findings/timeouts.
- Reconciliation incident SQL persistence (`reconciliation_incidents`) and deterministic forensic payload serialization.
- New focused test module `tests/test_reconciliation.py` covering orphan/stale/divergence detection, persistence, no duplicate repair triggers, fail-closed behavior, and snapshot replay consistency.
### Changed
- Runtime orchestration now tracks pending orders and emits reconciliation lifecycle repair events from deterministic findings.
### Known Issues
- Exchange/account snapshots are currently runtime-fed abstractions and require deeper live adapter telemetry lineage for full venue-truth supervision.

## Generation 7 - Runtime Bootstrap Entrypoint & Safe Startup Loop (2026-05-17)
### Added
- Runtime module async bootstrap (`main`) with environment-driven orchestrator construction.
- Executable module entrypoint (`asyncio.run(main())`) for `python -m alphaforge.runtime`.
- Safe default no-op market scanner for bootstrap startup without feed wiring.
- Runtime tests for bootstrap env parsing, loop liveness-until-shutdown, and dynamic RR propagation.
### Changed
- Runtime startup now emits explicit startup/shutdown logs and uses env-configured execution mode/intervals.
### Fixed
- Resolved immediate process exit when invoking `python -m alphaforge.runtime` by adding executable bootstrap path.
### Known Issues
- Default bootstrap scanner is intentionally no-op; production feed/adapters must still be wired externally.
## Generation 7 - Production-grade Environment Template & Safety Configuration (2026-05-17)
### Added
- Rebuilt `.env.example` as a grouped, execution-aware template with conservative safety defaults and inline operational comments.
- Added explicit mode-separation variables for BACKTEST/PAPER/LIVE, plus live-readiness, reconciliation, reject-quality, and execution-risk controls.
- Added placeholders for Binance/Hyperliquid/API/notifications/redis/queue integrations expected by the runtime architecture roadmap.
### Changed
- README now documents safe `.env` bootstrap, mode switching, and live-trading risk warnings.
### Known Issues
- Some template variables are forward-compatible operational toggles and are not yet wired by direct `os.getenv` reads in current modules.

## [Unreleased] - 2026-05-17 (Backtest lifecycle accounting fix)

### Fixed
- Backtest lifecycle persistence now marks `SIGNAL_CREATED` as `PENDING` (not `ACCEPTED`) and treats `SYMBOL_REJECTED` as `REJECTED` to prevent contradictory terminal decisions.
- Backtest summary counters now derive candidates/rejections by final per-signal terminal decision and count `total_orders` from `ORDER_PLACED` rows only.

### Added
- Regression tests for symbol-level reject decision integrity and per-signal terminal decision deduplication in backtest summary accounting.

## [Unreleased] - 2026-05-17 (Setup quality diagnostics)

### Added
- Rejected/accepted candidate export diagnostics: `raw_rr`, `effective_rr`, `min_required_score`, `trend_strength`, `volatility_pct`, `range_position`, `slippage_pct`, `first_blocking_gate`, `all_failed_gates`.
- Quality summary percentiles for score/raw RR/effective RR and slice distributions by setup type/regime reject reason.
- Near-threshold LOW_SCORE rejection counter for calibration analysis.

### Changed
- Trade-quality diagnostics now record `all_failed_gates` in addition to first blocking gate.

### Known Issues
- Setup generation heuristics remain simplistic and may still overproduce weak breakout-style candidates in choppy regimes.


## Generation 9 - Adaptive Learning Data Foundation (2026-05-17)
### Added
- Deterministic SQL-first adaptive learning module `src/alphaforge/adaptive_learning.py` with closed/rejected review persistence, adaptive stats aggregation, reject-accuracy computation, expectancy bucket classification, and shadow-threshold recommendation logic.
- New persistence tables: `rejected_signal_reviews`, `adaptive_stats`, `adaptive_threshold_snapshots`; expanded `closed_trade_reviews` schema for execution-aware review fields.
- Config safety flags for adaptive foundation (disabled learning by default, shadow mode default on, clamp and sample controls).
- Adaptive learning foundation tests in `tests/test_adaptive_learning_foundation.py`.
### Changed
- `AIBrain` now records adaptive review rows for closed trades and rejected decisions without changing acceptance/execution behavior.
### Known Issues
- Forward-labeling for rejected-signal outcome quality remains null until Generation 2 outcome-label jobs are added.
## [Unreleased] - 2026-05-17 (Regime gate initialization hotfix)

### Fixed
- Resolved `UnboundLocalError` in trade-quality evaluation by initializing `regime_ok` before gate checks.
- Preserved deterministic regime reject behavior (`REGIME_MISMATCH`) while preventing crashes for candidates with missing regime values.

### Added
- Regression tests covering missing-candidate-regime non-crash behavior and incompatible market-regime rejection behavior.

## [Unreleased] - 2026-05-18 (Backtest lifecycle summary reconciliation)

### Fixed
- Reconciled backtest summary counting semantics so `total_candidates = accepted_count + rejected_count` using signal-level lifecycle identities.
- Corrected `total_orders` meaning to accepted order objects (`WAITING_ENTRY_ZONE`) rather than candidate-level/event-level drift.
- Added explicit `rejected_count` in main order summary while preserving `total_rejected` alias for compatibility.
- Aligned quality summary denominator/reject accounting to signal-level candidates (`SIGNAL_CREATED`) to prevent candidate-vs-event mismatch.

### Added
- Regression test proving quality summary candidate denominator uses signal-level rows and matches reject distribution semantics.

## [Unreleased] - 2026-05-18 (Adaptive persistence compatibility hotfix)

### Fixed
- `build_backtest_quality_summary(...)` now supports both plain decision-row inputs and lifecycle-row inputs, with candidate counting based on `SIGNAL_CREATED` when present and direct-row counting fallback when absent.
- Restored legacy `closed_trade_reviews.execution_metrics` population in adaptive closed-trade persistence path to prevent NULL JSON in execution-layer review queries.
- Ensured SQLite migration/bootstrapping adds `execution_metrics` column when absent and uses SQLAlchemy `text(...)` for robust inserts in `save_closed_trade_review`.

### Added
- No architectural changes; patch is compatibility-focused and regression-safe.

## [Unreleased] - 2026-05-18 (Generation N+2 Forward Reject Telemetry Foundation)

### Added
- Deterministic forward-window evaluator (`evaluate_forward_window`) for lifecycle/reject rows with labels: `would_have_hit_tp`, `would_have_hit_sl`, `mfe_pct`, `mae_pct`, `max_forward_return`, `max_adverse_return`, `reject_correct`, `reject_missed_winner`, `reject_saved_from_loss`, `forward_window_minutes`, `forward_window_regime`, `execution_quality_bucket`.
- Adaptive stats scope entrypoint `update_adaptive_stats_by_scope(...)` supporting additive reject-learning scopes (`REJECTION_REASON`, execution/volatility/spread/liquidity/trend/session/timeframe buckets).
- Determinism regressions for forward-window replay stability and scoped reject-accuracy aggregation.

### Changed
- Reject quality telemetry can now be aggregated by richer scopes without enabling autonomous threshold mutation.

### Known Issues
- Forward-window outputs are generated deterministically but are not yet persisted into dedicated SQL tables in this generation.

## [Unreleased] - 2026-05-18 (Generation N+2 evaluator wiring + calibration snapshots)

### Added
- Post-terminal forward evaluator wiring in backtest export flow via `build_forward_evaluation_rows(...)` with terminal-only trigger semantics.
- Immutable/idempotent `calibration_snapshots` persistence table (unique by `signal_id`, `forward_window_minutes`, `realized_outcome`).
- New exports: `forward_evaluations.csv` and `calibration_snapshots.csv`.
- Expanded adaptive scope-key test coverage for regime/setup/timeframe/session/volatility/spread/liquidity/trend/rejection/execution-quality keys.

### Changed
- Forward evaluator remains post-decision analytics-only and now runs only for terminal outcomes (`TP_HIT`, `SL_HIT`, `EXPIRED`, `CANCELED`, `OPEN_AT_END`, `REJECTED`).
## [Unreleased] - 2026-05-18 (Generation N+2 Wiring: terminal forward-eval + calibration persistence)

### Added
- Terminal-state forward evaluator trigger wiring in backtest flow (post-lifecycle only) with CSV exports: `forward_evaluations.csv`, `adaptive_scope_stats.csv`, `calibration_snapshots.csv`.
- Immutable/idempotent `calibration_snapshots` persistence table (`UNIQUE(signal_id, forward_window_minutes)`, insert-do-nothing).
- Adaptive scope export payload covering regime/setup/timeframe/session/volatility/spread/liquidity/trend/rejection_reason/execution_quality dimensions.

### Fixed
- Forward evaluator remains isolated from same-signal decision path and now executes only on terminal closed lifecycle outcomes.


## [Unreleased] - 2026-05-19
### Added
- Probabilistic score payload in `AIBrain` scoring output and persisted execution feature metadata (`probabilistic_score`).
- Conservative prior warning (`CONSERVATIVE_PRIOR_NO_HISTORY`) when sample history is absent.
### Changed
- Score acceptance now requires probabilistic constraints (minimum `p_win`, execution success, confidence, positive expectancy-after-costs, fakeout cap) in addition to aggregate score.
- Rejected-signal review reason assignment now maps from probabilistic failure flags before falling back to `LOW_SCORE`.
### Fixed
- Reduced static scalar-score dependence by blending weighted legacy score with calibrated probabilistic score.


## [Unreleased] - 2026-05-19 (Forensic lifecycle audit documentation)

### Added
- Production-grade forensic lifecycle audit report in `REPORT.md` covering signal generation, scoring, regime gating, execution-penalty formulas, lifecycle path analysis, and root-cause matrix.

### Changed
- Updated `VERSION.md` with forensic audit status notes and explicit architecture-level findings summary.

### Known Issues
- Backtest candidate generation remains long-only in current builder path.
- Effective-RR formulation divergence exists between backtest-local helper and shared runtime cost model.
- Score-to-expectancy calibration remains weak for executable post-cost edge.

## [Unreleased] - 2026-05-19 (Backtest lifecycle calibration parity hardening)

### Added
- Backtest candidate generation now supports mirrored SHORT breakdown candidates (`BREAKDOWN_DOWN` / `CLOSE_BELOW_PREV_LOW`) alongside LONG breakout candidates.
- Execution diagnostics now include decomposed penalty fields (`cost_penalty_total`, `spread_penalty`, `slippage_penalty`, `latency_penalty`, `liquidity_penalty`, `funding_penalty`).
- Unit-assumption visibility fields (`spread_unit_assumed`, `slippage_unit_assumed`) now flow from execution-context construction.

### Changed
- Backtest effective-RR rejection helper now uses additive shared execution-cost-model semantics to align with runtime/PAPER/LIVE evaluation behavior.
- Spread/slippage inputs now normalize percent-point inputs into fractional-rate contract values before penalty modeling.

### Fixed
- Removed backtest-only multiplicative effective-RR divergence for order-level reject diagnostics.
- Reduced silent unit ambiguity risk where values like `0.1` could be interpreted inconsistently across callers.

## [Unreleased] - 2026-05-19 (PAPER SQLite bootstrap + diagnostics hardening)

### Added
- Runtime bootstrap diagnostics for SQLite path resolution, schema init confirmation, and discovered table names.
- Heartbeat diagnostics for persistence enabled state and selection/decision gate blockers.
- Tests covering PAPER schema bootstrap with empty cycles and absolute DB path logging.

### Changed
- Runtime scanner now captures top symbol selection reject reasons even when no symbols are selected.
- Runtime env bootstrap now wires reject/lifecycle callbacks to SQL persistence in PAPER/BACKTEST/LIVE runtime path (when enabled).

### Fixed
- PAPER runtime persistence observability gaps that obscured whether SQL schema initialization and persistence callbacks were active.

## [Unreleased] - 2026-05-19 (Rejected shadow gate diagnostics + STOP_TOO_WIDE rescue simulation)

### Added
- Rejected shadow export fields for gate score provenance (`gate_score`, `low_score_gate_score`) and STOP_TOO_WIDE rescue simulation outputs.
- Reject-reason diagnostics aggregation including row count, TP opportunity rates, effective TP rates, mean score/RR/cost-penalty, and top symbols/regimes.

### Changed
- Rejected shadow evaluation now normalizes `spread_pct` units before execution-penalty evaluation.
- Symbol market-data builder now normalizes `actual_spread_pct` to the same unit contract used by gate thresholds.

### Fixed
- Low-score gate/CSV observability gap by exporting the actual gate score used by rejection logic.

## [Unreleased] - 2026-05-20 (Lifecycle ordering audit hotfix)

### Fixed
- Restored backtest lifecycle ordering so `WAITING_ENTRY_ZONE` is emitted instead of being overwritten to `SIGNAL_CREATED` in `simulate_candidate(...)`.

### Changed
- Added explicit dev-branch design compliance audit section to `REPORT.md` with truthful status and remaining architecture gaps.

## [Unreleased] - 2026-05-20 (Backtest lifecycle/persistence/reporting defects)

### Fixed
- Lifecycle persistence upsert now supports composite lifecycle uniqueness `(signal_id,event_ts,lifecycle_state)` with compatibility fallback to `event_id` conflict handling.
- Backtest summary `total_orders` now counts unique `ORDER_PLACED` lifecycle keys (no longer reports zero when placed rows exist).
- Backtest summary `triggered_orders` now counts unique `ENTRY_TRIGGERED` keys; `not_triggered_orders` now uses accepted WAITING paths that never triggered/placed.
- Lifecycle SQL export ordering now uses deterministic lifecycle-aware sort keys (`event_ts,symbol,signal_id,lifecycle_seq,lifecycle_state,event_id`).

### Added
- Regression tests for lifecycle composite-key idempotency, not-triggered counting semantics, lifecycle sequence monotonicity, and LOW_SCORE rescue/watch diagnostic-only field semantics.

### Changed
- LOW_SCORE rescue/watch outputs are explicitly diagnostics-only and remain excluded from accepted/order/win-rate/realized-PnL aggregates.

## [Unreleased] - 2026-05-21 (Phase 6.1 audit-trail canonicalization merge conflict resolution)

### Changed
- PAPER accepted lifecycle path is canonicalized to emit `SIGNAL_CREATED -> WAITING_ENTRY_ZONE -> ENTRY_TRIGGERED -> ORDER_PLACED` before execution simulation.
- Runtime lifecycle persistence callback is now fail-closed and raises when lifecycle SQL persistence reports failure.

### Fixed
- `save_order_decision(...)` now returns an explicit failure indicator (`None`) on SQL exceptions instead of silently pretending success.
- `save_trade_lifecycle_event(...)` now returns explicit `False` when both lifecycle upsert strategies or commit fail.
- Added regression coverage for canonical PAPER ordering and lifecycle persistence failure detectability.


## [Unreleased] - 2026-05-21 (Persistence API compatibility + lifecycle sequencing)

### Added
- New `fetch_expectancy_stat_detail(...)` helper for metadata consumers while preserving legacy scalar expectancy API behavior.
- Legacy compatibility columns auto-repair in SQLite bootstrap: `order_decisions.payload`, `trade_lifecycle_events.trade_id`, `trade_lifecycle_events.state`, `trade_lifecycle_events.payload`.

### Changed
- `fetch_expectancy_stat(...)` contract restored to return `float | None` for backward compatibility.
- `save_trade_lifecycle_event(...)` now returns inserted/upserted row id and backfills legacy `trade_id/state/payload` fields.

### Fixed
- `save_order_decision(...)` now persists legacy `payload` JSON consistently, including rejected decision context.
- Backtest accepted lifecycle now includes `WAITING_ENTRY_ZONE` before `ENTRY_TRIGGERED`.

## 2026-05-21 config centralization
- Added centralized env config loading and runtime/exchange/backtest wiring updates.

## 2026-05-22
- Added deterministic historical Binance Futures replay provider with paginated kline fetching, gap checks, and funding joins.
- Backtest runtime now treats synthetic scanner as smoke-test only and labels market_data_source=SYNTHETIC_SMOKE_TEST when enabled.

## 2026-05-22 PR #148 follow-up (LIVE qualification non-mutating parity fix)

### Changed
- LIVE qualification mode parity evidence now runs through side-effect-free scoring/planning/explanation calls and no longer calls persistence-capable `before_real_order(...)`.
- Qualification parity samples now use stable fixture sample IDs and stable fixture market timestamps to keep parity inputs deterministic across repeated runs.

### Fixed
- Removed synthetic LIVE qualification probe mutation of `signals`, `order_decisions`, `ai_decision_features`, `trade_lifecycle_events`, and rejected-review tables during parity evaluation.
- Added regression coverage for non-mutating parity evidence and deterministic replay parity output (excluding `generated_at`).

## [JOB-22A] - 2026-05-24
### Changed
- Canonical execution evidence now preserves measured/modeled/unavailable provenance through scanner->runtime->persistence paths.
### Fixed
- Removed optimistic zero-default persistence for spread/slippage/latency when evidence is unavailable.
### Known Issues
- effective_rr currently remains equal to rr in persisted decisions and is still unresolved.

## [Unreleased] - 2026-06-21 (P0-3 TimesFM canonical evidence integration)

### Added
- Added canonical `timesfm_forecast_evidence` SQL persistence for TimesFM research decisions.
- Added stable TimesFM `forecast_id` generation and no-lookahead input end timestamp tracking.
- Added optional `timesfm_forward_outcome_labels` schema for future calibrated outcome labeling.
- Added tests for SQL TimesFM evidence persistence, stable IDs/idempotency, CSV evidence fields, and invalid forecast persistence.

### Changed
- TimesFM CSV decision logs now include canonical evidence fields and model/provider metadata.

### Fixed
- TimesFM forecast evidence is no longer CSV-only when a persistence session is supplied.

### Removed
- Nothing.

### Breaking Changes
- None. Schema changes are additive and TimesFM remains PAPER/BACKTEST only.

### Known Issues
- Forward outcome calibration is not implemented yet; TimesFM should remain isolated research evidence until calibrated.
- LIVE readiness remains NOT READY and TimesFM has no order authority.

## 2026-06-27 - Mode-aware config registry and Dashboard Settings

### Added
- Added typed managed config registry, Dashboard Settings page, local runtime override persistence, and config snapshot export support.

### Changed
- Moved order decision thresholds out of local fallback dictionaries and made PAPER/LIVE runtime daily caps inactive for BACKTEST by default.

### Fixed
- `ALPHAFORGE_MAX_TRADES_GLOBAL_PER_DAY` no longer changes BACKTEST trade-quality decisions by default.

### Removed
- Hidden production threshold fallback dictionary in `order.py`.

### Breaking Changes
- BACKTEST daily caps must use explicit `ALPHAFORGE_BACKTEST_*` settings rather than PAPER/LIVE runtime caps.

### Known Issues
- Existing long-running PAPER/LIVE processes still require restart for risk-critical setting changes.

## 2026-07-01 - Rejected forward outcome evidence artifacts

### Added
- Added canonical `rejected_forward_outcomes.csv/json` diagnostics with first-touch TP/SL/timeout/ambiguous/unavailable classifications.
- Added LOW_SCORE and symbol-level forward summary artifacts.
- Added HIGH_VOL_GUARD and STOP_TOO_WIDE forward confirmation fields to the zero-accepted root-cause summary.
- Added dashboard surfacing for compact forward summaries and artifact paths.
- Added regression tests for rejected forward outcome safety, geometry handling, cost penalties, and summary splits.

### Changed
- Zero-accepted root-cause summaries now include rejected-forward evidence completeness and conservative next-action guidance.
- Legacy rejected-shadow indexing now excludes the reject timestamp candle from forward simulation.

### Fixed
- Missing geometry is now exported as explicit unavailable evidence instead of being silently skipped by actionable-only shadow filtering.

### Removed
- None.

### Breaking Changes
- None. BACKTEST, PAPER, and LIVE acceptance behavior is unchanged.

### Known Issues
- Symbol-level rejects may remain forward-unevaluable until safe pre-reject candidate geometry is captured.
- PowerShell manual validation requires `pwsh`; Linux environments without PowerShell must run the equivalent Python command.

## 2026-07-01 - PR259 rejected-forward summary enrichment fix

### Added
- Added PR257-compatible LOW_SCORE gap source tracking, above-threshold/unknown counts, and 5% near-threshold definition to rejected forward summaries.
- Added LOW_SCORE threshold metadata and symbol selector metric enrichment to rejected forward rows.
- Added missing LOW_SCORE gap and missing symbol metric evidence-quality reasons.
- Added regression coverage for near/far classification, counterfactual-disabled subset expectancy, and selector metric preservation.

### Changed
- LOW_SCORE near/far classification now uses `0 <= gap <= min_score_threshold * 0.05` for near-threshold rows.
- `would_accept_if_low_score_disabled_mean_shadow_r` now uses only forward-evaluable LOW_SCORE rows that would pass if LOW_SCORE were disabled.

### Fixed
- Symbol reject forward summary means now use carried selector diagnostics instead of falling back to zero when diagnostics JSON contains metrics.

### Removed
- None.

### Breaking Changes
- None. Diagnostic artifacts are additive and no acceptance logic changed.

### Known Issues
- Missing persisted scores or selector diagnostics still limit evidence quality and are intentionally surfaced as unavailable.
