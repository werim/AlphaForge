# AlphaForge Version

- Current version: Phase 9 Binance reconciliation operational-validation follow-up
- Current phase: Phase 9 - production-like PAPER burn-in execution, evidence audit, and release decision hardening
- Runtime maturity: PAPER operational workflow with fail-closed, format-validated, time-bounded Binance fill scope; failed pooled connections are discarded before retry or reuse. Local automated validation is complete, but credentialed Demo acceptance and GitHub CI/mergeability evidence are unavailable in this environment. LIVE trading remains unavailable.
- BACKTEST/PAPER/LIVE alignment: Phase 9 requires campaign == active continuation == runtime identity parity for PAPER attachment; runtime limits remain mode-aware, while persisted or runtime config drift fails closed.
- Lifecycle coverage: startup interruptions terminalize explicitly with worker ownership cleared; dead PID-less RUNNING continuations transition explicitly to terminal `RECOVERY_REQUIRED` in both run tables only after runtime-owned position/order/orphan/reconciliation checks are clean, or after the narrow unrelated-historical PAPER fallback proves zero local exposure, dead process, absent/dead PID, no kill switch, no pending labels, and records provider unavailability; pending reject labels are preserved as non-financial evidence; RUNNING source runs are append-only; RECOVERY_REQUIRED/COMPLETED/FAILED/SUSPENDED source runs are immutable.
- Execution realism coverage: Uses runtime execution-cost identity, Binance read-only market-data/time provenance, dust-aware position activity, fresh per-attempt signed timestamps, resolver/provider failure separation, and no synthetic trade generation.
- Known critical risks: Credentialed Demo acceptance has not been executed here because Binance credentials are absent; GitHub remote/dev/CI state is unavailable because this checkout has no remote and outbound GitHub access is blocked. Reconciliation transport/auth/payload/scope errors remain fail closed.
- Last audit date: 2026-07-20
- Live readiness verdict: NOT LIVE READY; Phase 9 may only produce `PAPER_BURNIN_QUALIFIED_FOR_CANARY_REVIEW` when canonical `CANARY_QUALIFIED` evidence passes all operational gates.

- **2026-07-17 audit:** detached burn-in worker crash observability and dead-worker lifecycle cleanup are implemented; PAPER burn-in remains non-live-ready pending operational validation.
- **2026-07-17 follow-up:** post-attach worker failures and pause shutdown attribution are lifecycle-correct; worker identity guards remain fail-closed.

- **2026-07-17 recovery audit:** PAPER recovery is now scope-aware and evaluates current SQL positions/orders, reconciliation evidence, and kill switch before inheriting history. Snapshot provenance is append-only with nullable campaign/run/release lineage columns. LIVE/LIVE_PRECHECK remain strict; LIVE remains NOT LIVE READY.

- **2026-07-18 startup-interruption audit:** Detached launch is now compensating-transition safe for `KeyboardInterrupt`, `SystemExit`, `_launch_worker()` `RuntimeError`, spawn failure, worker early exit, attachment timeout, and identity mismatch. Zero-exposure startup failures can be safely terminalized; exposure-bearing or unavailable evidence remains blocked.
