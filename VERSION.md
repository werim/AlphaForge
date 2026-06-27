# AlphaForge Version

- Current version: 0.1.0
- Current phase: BACKTEST/PAPER/LIVE config-filter hardening
- Runtime maturity: research/runtime integration with canonical env filter path for shared score/RR/execution/symbol gates
- BACKTEST/PAPER/LIVE alignment: shared filters now flow through `RuntimeSettings` / `runtime_filter_config`; LIVE remains guarded by existing qualification and order-safety gates
- Lifecycle coverage: shared reject reasons are emitted before order placement for score, raw RR, effective RR, spread, slippage, funding, liquidity, stale data, cooldown, and concurrent-position gates
- Execution realism coverage: effective RR, spread, slippage, funding, stale market data, and liquidity settings are parsed from env and consumed by real selection/decision/runtime-risk paths
- Known critical risks: direct legacy calls to `evaluate_trade_quality(..., config={})` retain compatibility defaults; historical execution context can still be unavailable and is fail-closed/flagged by execution evidence checks rather than treated as measured zero
- Last audit date: 2026-06-27
- Live readiness verdict: NOT LIVE READY; this patch wires LIVE config paths but does not bypass live qualification, kill-switch, adapter, or reconciliation safety requirements
