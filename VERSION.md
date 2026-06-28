# AlphaForge Version

- Current version: 0.1.0
- Current phase: dashboard/backtest quality hardening
- Runtime maturity: research/PAPER validation; not LIVE-ready
- BACKTEST/PAPER/LIVE alignment: shared typed defaults now require `MIN_EFFECTIVE_RR=1.60`; `RR_TOO_LOW` uses execution-adjusted RR consistently, with BACKTEST-only switch experiments recorded.
- Lifecycle coverage: preserves SIGNAL_CREATED, SIGNAL_REJECTED, accepted lifecycle diagnostics, rejected distributions, near-miss diagnostics, execution-cost summaries, and config snapshot export.
- Execution realism coverage: conservative effective-RR default, configured LOW_EFFECTIVE_RR reject threshold, spread/slippage/liquidity/funding evidence remains explicit or unavailable.
- Known critical risks: score=10 saturation remains weakly calibrated; accepted-trade expectancy is not yet proven positive; dashboard filter switches can produce unsafe experiments if disabled.
- Last audit date: 2026-06-27
- Live readiness verdict: NOT LIVE READY. Capital preservation remains mandatory; use PAPER/backtest diagnostics only.
