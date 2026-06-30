## 2026-06-30 RejectedShadowEvaluation test fixture alignment

- Current version: unreleased test fixture alignment increment
- Current phase: CI regression repair for expanded rejected-shadow diagnostics constructor
- Runtime maturity: no runtime behavior changed; test fixture only
- BACKTEST/PAPER/LIVE alignment: unchanged
- Lifecycle coverage: unchanged
- Execution realism coverage: dashboard fixture now supplies deterministic execution diagnostic values instead of omitting required fields
- Known critical risks: none introduced; LIVE readiness remains NOT READY
- Last audit date: 2026-06-30
- Live readiness verdict: NOT READY

## 2026-06-30 DEFAULT_FILTERS overtrade diagnostics and drawdown exports

- Current version: unreleased BACKTEST diagnostic guardrail increment
- Current phase: PR243/env overtrade audit instrumentation for DEFAULT_FILTERS
- Runtime maturity: BACKTEST diagnostics/export visibility improved; PAPER/LIVE order placement and decision paths unchanged
- BACKTEST/PAPER/LIVE alignment: diagnostic-only changes do not loosen filters, rescue rejected shadows, or alter accepted trade decisions
- Lifecycle coverage: accepted terminal lifecycle rows now feed equity-curve risk metrics without changing lifecycle states
- Execution realism coverage: DEFAULT gate funnel, score saturation, overtrade, symbol/regime damage, and drawdown diagnostics are artifact-derived; missing gates are visible as zero-reject warnings rather than hidden
- Known critical risks: PR243 audit points to STOP_TOO_WIDE softening/env threshold calibration and score saturation as likely overtrade drivers; LIVE readiness remains blocked
- Last audit date: 2026-06-30
- Live readiness verdict: NOT READY

## 2026-06-30 BACKTEST dashboard dynamic top-volume universe validation

- Current version: unreleased BACKTEST dashboard dynamic-universe validation increment
- Current phase: BACKTEST form validation and command-boundary hardening
- Runtime maturity: BACKTEST dashboard request handling improved; PAPER/LIVE runtime and order paths unchanged
- BACKTEST/PAPER/LIVE alignment: dashboard BACKTEST form now accepts either explicit symbols or MAX_SYMBOLS-driven dynamic top-volume selection while preserving the existing BACKTEST runner universe logic
- Lifecycle coverage: no lifecycle transition or persistence lifecycle semantics changed
- Execution realism coverage: dynamic selection delegates to existing top-volume eligible universe selection; no fake symbols, filters, scores, RR, or fills introduced
- Known critical risks: dynamic universe runs still depend on available exchange metadata and historical data; LIVE readiness remains blocked
- Last audit date: 2026-06-30
- Live readiness verdict: NOT READY

## 2026-06-30 DEFAULT_FILTERS accepted-reason scope and STOP_TOO_WIDE diagnostics

- Current version: unreleased BACKTEST dashboard selected-profile consistency increment
- Current phase: selected DEFAULT_FILTERS dashboard artifact-scope hardening and recoverable STOP_TOO_WIDE reporting
- Runtime maturity: BACKTEST dashboard/reporting improved; PAPER/LIVE runtime and order paths unchanged
- BACKTEST/PAPER/LIVE alignment: selected main panel accepted-reason breakdown is sourced only from the selected profile artifacts and never from profile-comparison aggregates; no decision logic changed
- Lifecycle coverage: accepted/rejected lifecycle exports are read without modifying lifecycle state transitions
- Execution realism coverage: STOP_TOO_WIDE recoverable analysis is diagnostic-only and grouped by symbol, side, regime, effective-RR bucket, and shadow outcome with no stop-gate loosening
- Known critical risks: highlighted STOP_TOO_WIDE candidates are calibration leads only, not acceptance approvals; LIVE readiness remains blocked
- Last audit date: 2026-06-30
- Live readiness verdict: NOT READY

## 2026-06-30 DEFAULT_FILTERS profile artifact parser fix

- Current version: unreleased BACKTEST dashboard selected-profile artifact parser increment
- Current phase: dashboard overview artifact-schema compatibility for run `20260630T164308Z`
- Runtime maturity: BACKTEST reporting improved; PAPER/LIVE runtime and order paths unchanged
- BACKTEST/PAPER/LIVE alignment: profile-comparison overview now reads selected DEFAULT_FILTERS evidence from `profiles/DEFAULT_FILTERS` without treating ALL_FILTERS_OFF as strategy performance
- Lifecycle coverage: selected-profile lifecycle/calibration diagnostics are read from `lifecycle_calibration_summary.json`, `backtest_orders.csv`, `rejected_orders.csv`, and optional shadow artifacts; lifecycle transition logic is unchanged
- Execution realism coverage: accepted score/effective-RR distributions and rejected diagnostics remain artifact-derived; missing artifacts are reported with expected paths and fallback files checked
- Known critical risks: strategy expectancy remains unproven; dashboard parsing does not imply LIVE readiness; ALL_FILTERS_OFF remains a diagnostic stress test only
- Last audit date: 2026-06-30
- Live readiness verdict: NOT READY

## 2026-06-30 BACKTEST evidence rendering contract replacement

- Current version: unreleased BACKTEST dashboard evidence rendering contract increment
- Current phase: selected BACKTEST completed-vs-failed dashboard rendering hardening
- Runtime maturity: BACKTEST evidence visibility improved; PAPER/LIVE unchanged
- BACKTEST/PAPER/LIVE alignment: completed selected BACKTEST runs render only selected BACKTEST artifact evidence; failed selected BACKTEST runs fail closed without substituting PAPER SQL panels
- Lifecycle coverage: lifecycle exports and states are unchanged; dashboard rendering now preserves completed-run evidence and hides failed-run diagnostic empty states
- Execution realism coverage: execution cost summaries remain artifact-derived for completed runs only; unavailable failed-run evidence is explicitly marked unavailable
- Known critical risks: strategy expectancy and calibration remain unproven; LIVE readiness remains blocked pending full lifecycle/reject/persistence validation
- Last audit date: 2026-06-30
- Live readiness verdict: NOT READY

## 2026-06-30 BACKTEST daily timeframe support and truthful interval errors

- Current version: unreleased BACKTEST interval/reporting integrity increment
- Current phase: BACKTEST historical data plumbing and dashboard failure-reporting hardening
- Runtime maturity: BACKTEST `1d`/`4h` historical interval support improved; PAPER/LIVE unchanged
- BACKTEST/PAPER/LIVE alignment: no PAPER/LIVE decision, threshold, or order-path behavior changed
- Lifecycle coverage: successful runs preserve existing lifecycle exports; failed pre-run BACKTEST diagnostics are explicitly marked unavailable rather than filled from stale runtime state
- Execution realism coverage: Binance candles use real interval pagination; missing coverage remains a hard failure with explicit returned/required counts
- Known critical risks: strategy expectancy and score calibration remain unproven; dashboard failures still require operator review of artifacts
- Last audit date: 2026-06-30
- Live readiness verdict: NOT READY


## 2026-06-30 BACKTEST profile comparison runner

- Current version: unreleased dashboard BACKTEST profile comparison increment
- Current phase: BACKTEST artifact-first profile comparison and leaderboard diagnostics
- Runtime maturity: BACKTEST comparison improved; PAPER/LIVE unchanged
- Pre-merge safety audit correction: comparison sub-runs now use an isolated base command with a fixed start/end window; UI-disabled filters apply only to CUSTOM_CURRENT_UI or normal single-profile runs.
- BACKTEST/PAPER/LIVE alignment: comparison runner is BACKTEST-only and does not add PAPER/LIVE order paths
- Lifecycle coverage: comparison consumes existing lifecycle exports; no lifecycle state changes
- Execution realism coverage: objective score penalizes overtrading, loss streaks, execution costs, low samples, and unavailable drawdown is marked rather than fabricated
- Known critical risks: multi-window execution is scaffolded with NOT_RUN windows; diagnostic guard profiles do not prove positive expectancy; score saturation remains a calibration risk
- Last audit date: 2026-06-30
- Live readiness verdict: NOT READY

# AlphaForge Version

- Current version: 0.1.0
- Current phase: dashboard BACKTEST rescue experiment controls
- Runtime maturity: research/PAPER validation; not LIVE-ready
- BACKTEST/PAPER/LIVE alignment: shared typed defaults now require `MIN_EFFECTIVE_RR=1.60`; `RR_TOO_LOW` uses execution-adjusted RR consistently, with BACKTEST-only filter and SHORT_BREAKDOWN_RESCUE experiments recorded; PAPER/LIVE controls remain separate.
- Lifecycle coverage: preserves SIGNAL_CREATED, SIGNAL_REJECTED, accepted lifecycle diagnostics, rejected distributions, near-miss diagnostics, execution-cost summaries, and config snapshot export.
- Execution realism coverage: conservative effective-RR default, configured LOW_EFFECTIVE_RR reject threshold, spread/slippage/liquidity/funding evidence remains explicit or unavailable.
- Known critical risks: score=10 saturation remains weakly calibrated; accepted-trade expectancy is not yet proven positive; dashboard BACKTEST filter/rescue switches can produce unsafe experiments if misread as strategy quality.
- Last audit date: 2026-06-30
- Live readiness verdict: NOT LIVE READY. Filters-off damage diagnostics are BACKTEST-only evidence; capital preservation remains mandatory and PAPER/LIVE behavior is unchanged.
