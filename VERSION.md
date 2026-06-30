## 2026-06-30 Dashboard LOW_SCORE shadow evidence rendering fix

- Current version: unreleased dashboard BACKTEST LOW_SCORE evidence rendering increment
- Current phase: BACKTEST Evidence Consistency Phase UI completion
- Runtime maturity: BACKTEST dashboard rendering improved; PAPER/LIVE unchanged
- BACKTEST/PAPER/LIVE alignment: template-only display fix; no decision, artifact parser, persistence, runtime loop, or order-path behavior changed
- Lifecycle coverage: no lifecycle semantics changed; populated LOW_SCORE shadow diagnostics are now visible in rendered `/backtest/run` evidence
- Execution realism coverage: no execution model changes; LOW_SCORE shadow comparison remains diagnostic-only evidence
- Known critical risks: LIVE readiness remains fail-closed; rendered BACKTEST evidence does not prove positive expectancy
- Last audit date: 2026-06-30
- Live readiness verdict: NOT READY

## 2026-06-30 Dashboard top rejection reasons rendering fix

- Current version: unreleased dashboard BACKTEST rejection evidence rendering increment
- Current phase: BACKTEST Evidence Consistency Phase UI completion
- Runtime maturity: BACKTEST dashboard rendering improved; PAPER/LIVE unchanged
- BACKTEST/PAPER/LIVE alignment: template-only display fix; no decision, artifact parser, persistence, runtime loop, or order-path behavior changed
- Lifecycle coverage: no lifecycle semantics changed; populated rejection diagnostics are now visible in rendered `/backtest/run` evidence
- Execution realism coverage: no execution model changes; existing reject/cost diagnostics remain evidence-only
- Known critical risks: LIVE readiness remains fail-closed; rendered BACKTEST evidence does not prove positive expectancy
- Last audit date: 2026-06-30
- Live readiness verdict: NOT READY

## 2026-06-30 Dashboard accepted diagnostics rendering fix

- Current version: unreleased dashboard BACKTEST evidence rendering increment
- Current phase: BACKTEST Evidence Consistency Phase UI completion
- Runtime maturity: BACKTEST dashboard rendering improved; PAPER/LIVE unchanged
- BACKTEST/PAPER/LIVE alignment: template-only display fix; no decision, persistence, runtime loop, or order-path behavior changed
- Lifecycle coverage: populated accepted trade diagnostics are now visible in rendered `/backtest/run` evidence
- Execution realism coverage: no execution model changes; existing execution/cost diagnostics remain evidence-only
- Known critical risks: LIVE readiness remains fail-closed; rendered BACKTEST evidence does not prove positive expectancy
- Last audit date: 2026-06-30
- Live readiness verdict: NOT READY

## 2026-06-30 Dashboard selected profile artifact parsing and metric consistency

- Current version: unreleased dashboard BACKTEST profile artifact consistency increment
- Current phase: BACKTEST dashboard comparison-result parsing hardening
- Runtime maturity: BACKTEST dashboard artifact parsing improved; PAPER/LIVE unchanged
- BACKTEST/PAPER/LIVE alignment: patch is confined to BACKTEST/dashboard artifacts and quality summaries; no PAPER/LIVE runtime loops or order paths changed
- Lifecycle coverage: selected comparison profile now surfaces existing per-profile lifecycle/reject/calibration diagnostics; unique accepted-trade reason counting avoids lifecycle-event inflation
- Execution realism coverage: selected profile execution-cost summaries and effective-RR/score distributions are loaded from profile artifacts; unavailable values remain explicit
- Known critical risks: LIVE readiness remains fail-closed; comparison diagnostics do not prove positive expectancy or relax safety gates
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
