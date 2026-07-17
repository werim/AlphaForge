# AlphaForge Version

- Current version: Phase 9 persisted worker identity and continuation hardening
- Current phase: Phase 9 - production-like PAPER burn-in execution, evidence audit, and release decision hardening
- Runtime maturity: PAPER operational workflow with fail-closed preflight, persisted-identity worker attachment, verified worker attachment, watchdog incidents, fail-fast recovery drill, append-only/terminal source evidence audit, daily report, and final package; LIVE trading remains unavailable.
- BACKTEST/PAPER/LIVE alignment: Phase 9 requires campaign == active continuation == runtime identity parity for PAPER attachment; runtime limits remain mode-aware, while persisted or runtime config drift fails closed.
- Lifecycle coverage: unattached continuations are terminally FAILED with an end timestamp; RUNNING source runs are append-only; RECOVERY_REQUIRED/COMPLETED/FAILED/SUSPENDED source runs are immutable; pending rejects, PAPER positions, closures, qualification snapshots, incidents, and release decisions are persisted and audited.
- Execution realism coverage: Uses runtime execution-cost identity, Binance read-only market-data/time provenance, resolver/provider failure separation, and no synthetic trade generation.
- Known critical risks: Real multi-day PAPER evidence is still required; provider data/time outages fail closed; manual operator recovery is required; existing campaigns need one audit to seed source baselines. Zero-sample failed continuations are retained for audit but excluded from aggregate evidence.
- Last audit date: 2026-07-17
- Live readiness verdict: NOT LIVE READY; Phase 9 may only produce `PAPER_BURNIN_QUALIFIED_FOR_CANARY_REVIEW` when canonical `CANARY_QUALIFIED` evidence passes all operational gates.

- **2026-07-17 audit:** detached burn-in worker crash observability and dead-worker lifecycle cleanup are implemented; PAPER burn-in remains non-live-ready pending operational validation.
- **2026-07-17 follow-up:** post-attach worker failures and pause shutdown attribution are lifecycle-correct; worker identity guards remain fail-closed.
