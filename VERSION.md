## 2026-07-01 Diagnostic profile execution-context strictness

- Current version: unreleased diagnostic execution-context strictness follow-up
- Current phase: BACKTEST-only diagnostic evidence hardening
- Runtime maturity: SHORT_LOW_SCORE_BREAKDOWN_DIAGNOSTIC now fails closed on unavailable execution context; DEFAULT_FILTERS, PAPER, and LIVE decisions remain unchanged
- BACKTEST/PAPER/LIVE alignment: no production threshold, acceptance, PAPER, or LIVE configuration changes
- Lifecycle coverage: rejected rows remain rejected; no lifecycle transition changes
- Execution realism coverage: missing/non-numeric/unavailable effective RR, min effective RR, cost penalty, liquidity, spread, or slippage blocks diagnostic inclusion as EXECUTION_CONTEXT_UNAVAILABLE
- Known critical risks: diagnostic sample sizes may shrink when evidence is incomplete; LIVE readiness remains blocked
- Last audit date: 2026-07-01
- Live readiness verdict: NOT LIVE READY. No production threshold relaxation is recommended.

## 2026-07-01 SHORT LOW_SCORE BREAKDOWN diagnostic profile

- Current version: unreleased SHORT LOW_SCORE BREAKDOWN diagnostic profile increment
- Current phase: BACKTEST-only rejected LOW_SCORE shadow validation
- Runtime maturity: BACKTEST exports a narrowly scoped diagnostic profile for SHORT BREAKDOWN_DOWN LOW_SCORE candidates; DEFAULT_FILTERS, PAPER, and LIVE decisions are unchanged
- BACKTEST/PAPER/LIVE alignment: diagnostic artifacts do not lower LOW_SCORE thresholds, do not accept trades, and do not affect PAPER/LIVE configuration
- Lifecycle coverage: rejected rows remain rejected; no lifecycle transition semantics changed
- Execution realism coverage: HIGH_VOL_GUARD, STOP_TOO_WIDE, effective-RR, cost, spread, slippage, liquidity, and geometry sanity gates remain active for diagnostic inclusion
- Known critical risks: positive shadow buckets are exploratory and may not survive larger samples or execution-cost drift; LIVE readiness remains blocked
- Last audit date: 2026-07-01
- Live readiness verdict: NOT LIVE READY. No production threshold relaxation is recommended.

## 2026-07-01 BACKTEST lifecycle/reject SQL persistence completion

- Current version: unreleased BACKTEST lifecycle/reject SQL persistence increment
- Current phase: BACKTEST order-decision lifecycle simulator persistence hardening
- Runtime maturity: BACKTEST lifecycle export persistence now writes signal, final order-decision, and lifecycle SQL evidence for accepted and rejected candidates; PAPER/LIVE behavior unchanged
- BACKTEST/PAPER/LIVE alignment: shared reject outputs remain canonical while BACKTEST uses offline historical/safe execution context only; no live Binance orderbook/order calls added
- Lifecycle coverage: SIGNAL_CREATED, SIGNAL_REJECTED, WAITING_ENTRY_ZONE, ENTRY_TRIGGERED, ORDER_PLACED, ORDER_REJECTED, POSITION_OPENED, POSITION_CLOSED, ENTRY_TIMEOUT, and SYMBOL_REJECTED remain represented in artifacts
- Execution realism coverage: unavailable execution/expectancy evidence is explicit (`UNAVAILABLE_BACKTEST`, `BACKTEST_EXPECTANCY_UNAVAILABLE`, `REJECT_REASON_UNAVAILABLE`) and is not treated as zero cost
- Known critical risks: durable BACKTEST SQL storage is still caller-dependent; historical funding/spread reconstruction remains limited by available inputs; LIVE readiness remains blocked
- Last audit date: 2026-07-01
- Live readiness verdict: NOT LIVE READY

## 2026-07-01 reject overlay diagnostics

- Current version: unreleased reject-overlay diagnostic discovery increment
- Current phase: BACKTEST-only rejected-forward bucket discovery
- Runtime maturity: BACKTEST exports diagnostic overlay/bucket artifacts; PAPER/LIVE behavior unchanged
- BACKTEST/PAPER/LIVE alignment: default accept/reject decisions and thresholds are unchanged; overlays are labels only
- Lifecycle coverage: rejected rows remain rejected; no lifecycle transitions changed
- Execution realism coverage: bucket verdicts use first-touch rejected-forward outcomes, effective shadow R after costs, MFE/MAE, adverse excursion, and conservative sample thresholds
- Known critical risks: overlay candidates are not production recommendations; micro-buckets remain exploratory only
- Last audit date: 2026-07-01
- Live readiness verdict: NOT LIVE READY. No production threshold relaxation is recommended.

## 2026-07-01 score calibration diagnostics

- Current version: unreleased score calibration diagnostics increment
- Current phase: post-lifecycle/reporting score calibration audit
- Runtime maturity: BACKTEST exports richer score calibration evidence and BACKTEST-only calibrated-score diagnostics; PAPER/LIVE behavior unchanged
- BACKTEST/PAPER/LIVE alignment: score source audit documented; no acceptance threshold or PAPER/LIVE default score formula changed
- Lifecycle coverage: rejected shadow rows remain lifecycle-linked rejected evidence; no lifecycle transitions changed
- Execution realism coverage: diagnostics now expose score buckets, outcome rates, execution costs, stop distance, volatility, spread/slippage, and high-score SL-prone clusters
- Known critical risks: raw WOULD_TP calibration remains unproven on latest BTCUSDT 30d/1h evidence; calibrated_score is diagnostic-only until validated across runs
- Last audit date: 2026-07-01
- Live readiness verdict: NOT LIVE READY. No production threshold relaxation is recommended.

## 2026-07-01 PR256 diagnostic extraction correction

- Current version: unreleased PR256 diagnostic extraction follow-up
- Current phase: zero-accepted artifact diagnostic integrity
- Runtime maturity: BACKTEST diagnostics improved; PAPER/LIVE behavior unchanged
- BACKTEST/PAPER/LIVE alignment: no production threshold or decision-path changes
- Lifecycle coverage: rejected LOW_SCORE and symbol-level decisions remain persisted/exported; no lifecycle transitions changed
- Execution realism coverage: diagnostic evidence now preserves row-level score thresholds and selector market-structure metrics instead of using misleading fallback/missing classifications
- Known critical risks: accepted-trade expectancy remains unproven; threshold tuning remains blocked pending complete shadow/outcome evidence
- Last audit date: 2026-07-01
- Live readiness verdict: NOT LIVE READY

## 2026-07-01 PR255 HIGH_VOL_GUARD correction and zero-accepted audit

- Current version: unreleased PR255 follow-up diagnostics increment
- Current phase: HIGH_VOL_GUARD diagnostic correctness and LOW_SCORE/symbol reject bottleneck audit
- Runtime maturity: BACKTEST artifact diagnostics improved; PAPER/LIVE order paths unchanged
- BACKTEST/PAPER/LIVE alignment: default BACKTEST remains conservative and diagnostic counterfactuals do not alter PAPER/LIVE behavior
- Lifecycle coverage: rejected HIGH_VOL_GUARD, LOW_SCORE, TOO_CHOPPY, and WEAK_TREND_AND_NO_RANGE_EDGE decisions remain persisted/exported rejects; no lifecycle transitions changed
- Execution realism coverage: effective RR gaps, execution cost penalties, spread/slippage/liquidity pass flags, and unavailable volatility metrics are explicit
- Known critical risks: accepted-trade expectancy remains unproven; LOW_SCORE calibration and symbol-level market-structure filters need further evidence before any tuning proposal
- Last audit date: 2026-07-01
- Live readiness verdict: NOT LIVE READY. No production threshold relaxation is recommended.

## 2026-07-01 HIGH_VOL_GUARD zero-accepted diagnostics

- Current version: unreleased BACKTEST HIGH_VOL_GUARD diagnostics increment
- Current phase: zero-accepted root-cause audit and guardrail impact measurement
- Runtime maturity: BACKTEST now exports acceptance-funnel and HIGH_VOL_GUARD diagnostics without loosening default filters; PAPER/LIVE decision paths unchanged
- BACKTEST/PAPER/LIVE alignment: HIGH_VOL_GUARD remains a BACKTEST strategy-quality guardrail in `backtest_order._guardrail_rejection_reason`; diagnostic-off profiles are BACKTEST-only and labeled non-production
- Lifecycle coverage: no lifecycle transition semantics changed; rejected HIGH_VOL_GUARD candidates remain SIGNAL_REJECTED and export counterfactual evidence separately
- Execution realism coverage: diagnostics expose effective-RR/cost thresholds, execution context, stop distance, and counterfactual fields rather than force-accepting high-volatility candidates
- Known critical risks: latest BTCUSDT 30d/1h artifact must be regenerated in an environment with historical data access to classify the real 20 HIGH_VOL_GUARD rows; LIVE readiness remains blocked
- Last audit date: 2026-07-01
- Live readiness verdict: NOT READY

## 2026-07-01 BACKTEST quality summary reject-count parity

- Current version: unreleased BACKTEST artifact count-consistency increment
- Current phase: quality-summary canonical reject accounting hardening
- Runtime maturity: BACKTEST reporting now exposes canonical, signal-only, and symbol-selector reject counts separately; PAPER/LIVE decision paths unchanged
- BACKTEST/PAPER/LIVE alignment: no filters, thresholds, acceptance logic, lifecycle transitions, or LIVE readiness gates changed
- Lifecycle coverage: SIGNAL_REJECTED and SYMBOL_REJECTED remain distinct lifecycle evidence while quality-summary overall rejects use canonical rejected rows
- Execution realism coverage: no execution-cost or fill assumptions changed; this is artifact accounting only
- Known critical risks: manual BTCUSDT 30d/1h validation depends on historical data/network availability; LIVE readiness remains blocked
- Last audit date: 2026-07-01
- Live readiness verdict: NOT READY

## 2026-07-01 BACKTEST post-PR251 artifact consistency

- Current version: unreleased BACKTEST artifact consistency increment
- Current phase: reject attribution/export consistency and run-artifact hygiene
- Runtime maturity: BACKTEST quality summaries now separate canonical post-attribution reject distribution from raw gate diagnostics; PAPER/LIVE decision paths unchanged
- BACKTEST/PAPER/LIVE alignment: no acceptance thresholds or order runtime logic changed; this is artifact/persistence/reporting alignment only
- Lifecycle coverage: SYMBOL_REJECTED rows remain pre-signal symbol-selector rejects, with not-applicable expectancy/RR availability flags instead of fake zero geometry
- Execution realism coverage: zero RR/effective RR no longer represents unavailable symbol-filter geometry; stale candle JSON files are pruned from run-local artifacts before fetch
- Known critical risks: historical spread remains estimated when orderbook data is unavailable; manual BTCUSDT 30d/1h validation was attempted but Binance fetch was blocked by proxy tunnel 403; LIVE readiness remains blocked
- Last audit date: 2026-07-01
- Live readiness verdict: NOT READY

## 2026-07-01 BACKTEST symbol-list parsing hardening

- Current version: unreleased BACKTEST symbol input safety increment
- Current phase: CLI/dashboard historical fetch input validation
- Runtime maturity: BACKTEST symbol parsing is hardened before Binance fetches; strategy decisions and PAPER/LIVE paths unchanged
- BACKTEST/PAPER/LIVE alignment: no decision logic changed; this is BACKTEST runner/dashboard input normalization only
- Lifecycle coverage: no lifecycle transitions changed
- Execution realism coverage: malformed combined symbols now fail before producing unrealistic or invalid historical fetch attempts
- Known critical risks: manual PowerShell execution was not available in the Linux validation container; LIVE readiness remains blocked
- Last audit date: 2026-07-01
- Live readiness verdict: NOT READY

## 2026-07-01 BACKTEST lifecycle realism evidence completion

- Current version: unreleased BACKTEST lifecycle realism evidence increment
- Current phase: historical decision lifecycle, reject evidence, and artifact consistency hardening
- Runtime maturity: BACKTEST now exports and tests canonical pre-trade lifecycle, rejected decision artifacts, SQL/export parity, and concrete reject attribution; PAPER/LIVE gates remain conservative and unchanged
- BACKTEST/PAPER/LIVE alignment: BACKTEST reuses the shared order cycle for signal quality while applying offline-only historical execution context; no live Binance/orderbook calls are required for decision simulation
- Lifecycle coverage: SIGNAL_CREATED, SIGNAL_REJECTED, WAITING_ENTRY_ZONE, ENTRY_TRIGGERED, ORDER_PLACED, POSITION_OPENED, POSITION_CLOSED, ORDER_REJECTED, and ENTRY_TIMEOUT are canonical export states; deterministic fixture artifacts assert accepted and rejected paths before terminal results
- Execution realism coverage: score/RR are historical adapter outputs, effective RR includes execution penalties, missing expectancy/context is labeled unavailable rather than zero-filled
- Known critical risks: historical spread remains estimated when actual orderbook spread is absent; strategy expectancy remains unproven; LIVE readiness remains blocked
- Last audit date: 2026-07-01
- Live readiness verdict: NOT READY

## 2026-07-01 BACKTEST reject reason attribution

- Current version: unreleased BACKTEST reject attribution integrity increment
- Current phase: rejected candidate auditability and dashboard explanation repair
- Runtime maturity: BACKTEST reject diagnostics improved; filters remain conservative and no acceptance thresholds were loosened
- BACKTEST/PAPER/LIVE alignment: shared order diagnostics now expose raw/effective RR thresholds for BACKTEST attribution; PAPER/LIVE decision paths retain concrete reasons
- Lifecycle coverage: SIGNAL_REJECTED rows preserve concrete reject reasons when diagnostics support attribution; lifecycle ordering unchanged
- Execution realism coverage: LOW_EFFECTIVE_RR, NEGATIVE_EXPECTANCY, EXPECTANCY_MISSING, MISSING_EXECUTION_CONTEXT, LOW_SCORE, and REGIME_MISMATCH are surfaced instead of masked as UNKNOWN
- Known critical risks: strategy expectancy remains unproven; UNKNOWN can still occur for genuinely unclassified rejects; LIVE readiness remains blocked
- Last audit date: 2026-07-01
- Live readiness verdict: NOT READY

## 2026-07-01 Dashboard BACKTEST accepted-count and guardrail attribution fix

- Current version: unreleased dashboard BACKTEST reporting integrity increment
- Current phase: profile comparison accepted-count correction and guardrail attribution wiring
- Runtime maturity: BACKTEST dashboard reporting improved; strategy logic, thresholds, PAPER, and LIVE paths unchanged
- BACKTEST/PAPER/LIVE alignment: reporting now uses canonical executed-trade evidence from summary/order artifacts and does not treat lifecycle/reject/diagnostic rows as trades
- Lifecycle coverage: lifecycle event counts remain diagnostics only; no lifecycle transitions changed
- Execution realism coverage: no-trade profiles receive no-trade warnings instead of overtrade risk, and guardrail/later-gate rejects are attributed to concrete exported reasons
- Known critical risks: no-trade DEFAULT_FILTERS remains not strategy-quality evidence; LIVE readiness remains blocked
- Last audit date: 2026-07-01
- Live readiness verdict: NOT READY


## 2026-07-01 Dashboard BACKTEST profile timeout handling

- Current version: unreleased dashboard BACKTEST subprocess timeout hardening increment
- Current phase: profile-comparison timeout containment and partial artifact preservation
- Runtime maturity: BACKTEST dashboard runner stability improved; trading decision logic and PAPER/LIVE order paths unchanged
- BACKTEST/PAPER/LIVE alignment: dashboard action remains BACKTEST-only and no runtime loop or live order path is invoked
- Lifecycle coverage: no lifecycle transitions changed; completed profile lifecycle artifacts remain readable when another profile times out
- Execution realism coverage: no score, filter, expectancy, RR, fill, or execution-cost logic changed
- Known critical risks: timed-out profiles have unavailable metrics and require operator review; LIVE readiness remains blocked
- Last audit date: 2026-07-01
- Live readiness verdict: NOT READY


## 2026-06-30 BACKTEST profile metric integrity

- Current version: unreleased BACKTEST profile comparison integrity increment
- Current phase: dashboard/profile artifact accounting correction
- Runtime maturity: reporting improved; trading decision logic and live order paths unchanged
- BACKTEST/PAPER/LIVE alignment: BACKTEST comparison now separates lifecycle event rows, rejected diagnostics, accepted trade counts, and executed outcomes without loosening filters
- Lifecycle coverage: lifecycle rows remain exported, but SIGNAL_CREATED/SIGNAL_REJECTED/SYMBOL_REJECTED/ORDER_REJECTED are not counted as accepted trades
- Execution realism coverage: accepted effective RR distributions now require accepted/executed trade evidence; guardrail rejects expose reason breakdowns and examples
- Known critical risks: no-trade profiles remain NOT strategy-quality evidence; LIVE readiness remains blocked
- Last audit date: 2026-06-30
- Live readiness verdict: NOT READY


## 2026-06-30 Purpose-specific environment profiles

- Current version: unreleased environment profile separation increment
- Current phase: configuration hygiene for BACKTEST diagnostics, PAPER defaults, and LIVE preparation
- Runtime maturity: no trading logic changed; example configuration profiles now make mode intent explicit
- BACKTEST/PAPER/LIVE alignment: profiles preserve shared variable names while tuning thresholds by purpose; LIVE remains fail-closed by default
- Lifecycle coverage: no lifecycle transitions changed; profile tests protect reject/readiness-related core variables
- Execution realism coverage: PAPER/LIVE examples retain execution-cost, spread, slippage, funding, stale-data, liquidity, and risk controls with stricter LIVE values
- Known critical risks: templates do not prove strategy expectancy or LIVE readiness; local evidence and full validation remain mandatory
- Last audit date: 2026-06-30
- Live readiness verdict: NOT READY


## 2026-06-30 Strategy Quality Guardrails

- Current version: Strategy Quality Guardrails phase
- Current phase: conservative DEFAULT_FILTERS BACKTEST guardrails for overtrade, score saturation, and high-vol acceptance
- Runtime maturity: BACKTEST improved; PAPER reject reasons remain shared gate outputs; LIVE order placement unchanged
- BACKTEST/PAPER/LIVE alignment: guardrails are BACKTEST acceptance controls first and do not loosen PAPER/LIVE; PAPER retains granular LOW_SCORE/NEGATIVE_EXPECTANCY/EXPECTANCY_MISSING/execution rejects from shared decision gates
- Lifecycle coverage: new guardrail rejects are persisted as SIGNAL_REJECTED evidence rows with explicit reasons
- Execution realism coverage: saturated-score and high-vol candidates require stronger effective RR and bounded execution cost
- Known critical risks: before-guardrail PnL is exported as unavailable rather than simulated; high-vol diagnostic profile is not strategy-quality by default
- Last audit date: 2026-06-30
- Live readiness verdict: NOT LIVE READY

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

## 2026-07-01 Rejected forward outcome evidence phase
- Current version: unreleased rejected forward evidence increment
- Current phase: rejected LOW_SCORE/symbol-level forward outcome diagnostics and zero-accepted evidence completion
- Runtime maturity: BACKTEST exports diagnostic rejected-forward evidence; PAPER/LIVE order paths unchanged
- BACKTEST/PAPER/LIVE alignment: no acceptance thresholds or runtime decision logic changed; diagnostics are BACKTEST artifact-only
- Lifecycle coverage: rejected lifecycle states remain persisted/exported; forward outcomes annotate rejects without creating trades
- Execution realism coverage: effective shadow R records execution cost penalty after spread/slippage/liquidity cost modeling
- Known critical risks: symbol-level rejects can lack candidate geometry; accepted-trade expectancy and LIVE readiness remain unproven
- Last audit date: 2026-07-01
- Live readiness verdict: NOT LIVE READY; diagnostic evidence only and no production threshold relaxation recommended

## 2026-07-01 PR259 rejected-forward enrichment correction
- Current version: unreleased rejected forward evidence correction
- Current phase: PR259 review fixes for LOW_SCORE near/far semantics and selector context preservation
- Runtime maturity: BACKTEST diagnostic artifact correctness improved; PAPER/LIVE order paths unchanged
- BACKTEST/PAPER/LIVE alignment: no decision logic, thresholds, or canonical rejected decisions changed
- Lifecycle coverage: no lifecycle state changes; enriched rejected-forward evidence only
- Execution realism coverage: counterfactual-disabled means now use the correct forward-evaluable subset after costs
- Known critical risks: missing historical score gaps or selector metrics still constrain evidence quality
- Last audit date: 2026-07-01
- Live readiness verdict: NOT LIVE READY; diagnostic evidence only and no production threshold relaxation recommended
