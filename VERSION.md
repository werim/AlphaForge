# AlphaForge Version

- Current version: Phase 9.1 safe configuration remediation and Binance Demo REST coherence
- Current phase: Phase 9 - production-like PAPER burn-in execution, evidence audit, and release decision hardening
- Runtime maturity: PAPER operational workflow with fail-closed preflight, STARTING-to-RUNNING verified worker attachment, explicit startup-failure terminalization, verified recovery evidence, historical local-zero-exposure diagnostic fallback evidence, stale-continuation terminalization, watchdog incidents, append-only/terminal source evidence audit, daily report, and final package; LIVE trading remains unavailable.
- BACKTEST/PAPER/LIVE alignment: Phase 9 requires campaign == active continuation == runtime identity parity for PAPER attachment; runtime limits remain mode-aware, while persisted or runtime config drift fails closed.
- Lifecycle coverage: startup interruptions terminalize explicitly with worker ownership cleared; dead PID-less RUNNING continuations transition explicitly to terminal `RECOVERY_REQUIRED` in both run tables only after runtime-owned position/order/orphan/reconciliation checks are clean, or after the narrow unrelated-historical PAPER fallback proves zero local exposure, dead process, absent/dead PID, no kill switch, no pending labels, and records provider unavailability; pending reject labels are preserved as non-financial evidence; RUNNING source runs are append-only; RECOVERY_REQUIRED/COMPLETED/FAILED/SUSPENDED source runs are immutable.
- Execution realism coverage: Uses runtime execution-cost identity, Binance read-only market-data/time provenance, resolver/provider failure separation, and no synthetic trade generation.
- Known critical risks: Real multi-day PAPER evidence is still required; related/current runtime reconciliation still requires an enabled provider plus Binance API credentials and outages fail closed; Demo websocket parity is not claimed and websocket consumers require an explicit endpoint; nonzero/unknown exposure still requires manual recovery.
- Last audit date: 2026-07-20
- Live readiness verdict: NOT LIVE READY; Phase 9 may only produce `PAPER_BURNIN_QUALIFIED_FOR_CANARY_REVIEW` when canonical `CANARY_QUALIFIED` evidence passes all operational gates.

- LIVE authorization integration: runtime-owned authorization is derived from current qualification, reconciliation/recovery, operator, LIVE-enable, environment allow-order, and persisted kill-switch state; mutable state is refreshed at the final adapter boundary. Phase 6 still disables LIVE mutation.

- Configuration maturity: dry-run remediation classifies source-aware findings and apply mode is restricted to provably safe `.env` edits with backup, atomic replacement, bounded re-audit, and secret redaction. Production/testnet/Demo REST identity remains fail closed; no LIVE or websocket safety gate is weakened.

- **2026-07-17 audit:** detached burn-in worker crash observability and dead-worker lifecycle cleanup are implemented; PAPER burn-in remains non-live-ready pending operational validation.
- **2026-07-17 follow-up:** post-attach worker failures and pause shutdown attribution are lifecycle-correct; worker identity guards remain fail-closed.

- **2026-07-17 recovery audit:** PAPER recovery is now scope-aware and evaluates current SQL positions/orders, reconciliation evidence, and kill switch before inheriting history. Snapshot provenance is append-only with nullable campaign/run/release lineage columns. LIVE/LIVE_PRECHECK remain strict; LIVE remains NOT LIVE READY.

- **2026-07-18 startup-interruption audit:** Detached launch is now compensating-transition safe for `KeyboardInterrupt`, `SystemExit`, `_launch_worker()` `RuntimeError`, spawn failure, worker early exit, attachment timeout, and identity mismatch. Zero-exposure startup failures can be safely terminalized; exposure-bearing or unavailable evidence remains blocked.
