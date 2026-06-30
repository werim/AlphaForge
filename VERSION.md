# AlphaForge Version

- Current version: 2026.06.30-short-breakdown-rescue-experiment
- Current phase: BACKTEST-only SHORT breakdown rescue reporting/activation experiment
- Runtime maturity: research/backtest diagnostics; defensive runtime preservation remains priority
- BACKTEST/PAPER/LIVE alignment: DEFAULT behavior unchanged; SHORT_BREAKDOWN_RESCUE is explicitly BACKTEST-only and disabled by default; PAPER/LIVE decision paths are not activated by the switch.
- Lifecycle coverage: rescued BACKTEST trades follow normal lifecycle simulation and carry `accepted_reason`, `original_reject_reason`, sizing, effective-RR, and decision-context metadata.
- Execution realism coverage: rescue eligibility requires execution-adjusted RR, acceptable liquidity, acceptable or explicitly unavailable BACKTEST volatility, spread/slippage caps, and conservative 0.25x default sizing.
- Known critical risks: rescue quality is based on BACKTEST diagnostics and rejected-shadow evidence, not LIVE execution proof; score calibration remains imperfect; historical spread/slippage are still estimates when real data is unavailable.
- Last audit date: 2026-06-30
- Live readiness verdict: NOT LIVE READY. The rescue lane is an opt-in BACKTEST experiment and must not be treated as PAPER/LIVE readiness.
