# AlphaForge Version

- Current version: 0.1.0
- Current phase: BACKTEST/PAPER hardening
- Runtime maturity: research/runtime integration, defensive defaults preserved
- BACKTEST/PAPER/LIVE alignment: BACKTEST now has explicit experimental filter switches; PAPER and LIVE ignore those switches
- Lifecycle coverage: signal creation, rejection, accepted execution, rescue metadata, and disabled-filter bypass evidence are exported for BACKTEST review
- Execution realism coverage: spread/slippage/liquidity/effective-RR gates remain conservative by default
- Known critical risks: disabled BACKTEST filters can inflate accepted trades and worsen expectancy; legacy env cleanup remains a future audit area
- Last audit date: 2026-06-27
- Live readiness verdict: NOT LIVE READY; filter bypass experiments are research-only and are not qualification evidence
