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
