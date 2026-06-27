# AlphaForge Version

- Current version: 0.1.0
- Current phase: BACKTEST accepted-diagnostics and score-calibration hardening
- Runtime maturity: research/runtime integration with artifact-first accepted-trade diagnostics and diagnostic-only score saturation reporting
- BACKTEST/PAPER/LIVE alignment: BACKTEST exports now preserve accepted geometry/PnL evidence from lifecycle execution context; PAPER/LIVE order paths are unchanged
- Lifecycle coverage: accepted rows retain side, entry, stop, target, close reason, exit, gross/net PnL, fee/cost evidence, and exported/not-exported status where lifecycle/order artifacts provide them
- Execution realism coverage: high effective RR is treated as insufficient without score-bucket calibration, same-day degradation context, and execution-cost evidence
- Known critical risks: score de-saturation and dynamic trade-limit logic are proposal/diagnostic-only and disabled by default; historical artifacts without lifecycle/order evidence still remain unavailable rather than synthetically filled
- Last audit date: 2026-06-27
- Live readiness verdict: NOT LIVE READY; accepted diagnostics and calibration guardrails improve BACKTEST auditability but do not validate live execution, reconciliation, or adapter readiness


## 2026-06-27 Config Registry Audit
- Current phase: mode-aware configuration hardening.
- Runtime maturity: PAPER-oriented; LIVE remains guarded/not ready without qualification.
- BACKTEST/PAPER/LIVE alignment: shared trade-quality evaluator with mode-aware runtime caps.
- Lifecycle coverage: unchanged; rejects remain explicit.
- Execution realism coverage: typed execution-cost filters; missing context must remain explicit.
- Known critical risks: active runtimes require restart for risk-critical changes; LIVE cannot be enabled from Settings.
- Last audit date: 2026-06-27.
- Live readiness verdict: NOT READY by default.
