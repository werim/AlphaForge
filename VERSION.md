# AlphaForge Version

- Current version: Phase 9 cross-platform PAPER operational acceptance and read-only database diagnosis
- Current phase: Phase 9 - production-like PAPER burn-in execution, evidence audit, and release decision hardening
- Runtime maturity: PAPER operational workflow with fail-closed preflight, read-only database-wide diagnosis/cleanup planning, STARTING-to-RUNNING verified worker attachment, explicit startup-failure terminalization, verified recovery evidence, watchdog incidents, append-only/terminal source evidence audit, daily report, and final package; LIVE trading remains unavailable.
- BACKTEST/PAPER/LIVE alignment: Phase 9 requires campaign == active continuation == runtime identity parity for PAPER attachment; runtime limits remain mode-aware, while persisted or runtime config drift fails closed.
- Lifecycle coverage: startup interruptions terminalize explicitly with worker ownership cleared; dead PID-less RUNNING continuations transition explicitly to terminal `RECOVERY_REQUIRED` in both run tables only after runtime-owned position/order/orphan/reconciliation checks are clean, or after the narrow unrelated-historical PAPER fallback proves zero local exposure, dead process, absent/dead PID, no kill switch, no pending labels, and records provider unavailability; pending reject labels are preserved as non-financial evidence; RUNNING source runs are append-only; RECOVERY_REQUIRED/COMPLETED/FAILED/SUSPENDED source runs are immutable.
- Execution realism coverage: Uses runtime execution-cost identity, Binance read-only market-data/time provenance, resolver/provider failure separation, and no synthetic trade generation.
- Known critical risks: Real multi-day PAPER evidence and credentialed Demo REST acceptance are still required; runtime/streaming Demo startup requires an explicitly supported websocket; related/current runtime reconciliation still requires an enabled provider plus Binance API credentials and outages fail closed; nonzero/unknown exposure still requires manual recovery.
- Last audit date: 2026-07-23
- Live readiness verdict: NOT LIVE READY; Phase 9 may only produce `PAPER_BURNIN_QUALIFIED_FOR_CANARY_REVIEW` when canonical `CANARY_QUALIFIED` evidence passes all operational gates.

- LIVE authorization integration: runtime-owned authorization is derived from current qualification, reconciliation/recovery, operator, LIVE-enable, environment allow-order, and persisted kill-switch state; mutable state is refreshed at the final adapter boundary. Phase 6 still disables LIVE mutation.

- Configuration maturity: all four environment templates are generated from one WIRED/ALIAS/RESERVED registry; 103 settings have resolvable concrete consumers and full behavioral-test node IDs, 16 are deterministic aliases, and 44 unsupported entries carry key-specific explanations/removal guidance. LIVE allow-orders is an additional final deny gate; regime/orderbook aliases and behavioral orderbook filtering are tested. Dotenv precedence, typed validation, duplicate/conflict detection, secret redaction, mode metadata, and Binance resolution remain tested. LIVE remains fail closed.

- **2026-07-17 audit:** detached burn-in worker crash observability and dead-worker lifecycle cleanup are implemented; PAPER burn-in remains non-live-ready pending operational validation.
- **2026-07-17 follow-up:** post-attach worker failures and pause shutdown attribution are lifecycle-correct; worker identity guards remain fail-closed.

- **2026-07-17 recovery audit:** PAPER recovery is now scope-aware and evaluates current SQL positions/orders, reconciliation evidence, and kill switch before inheriting history. Snapshot provenance is append-only with nullable campaign/run/release lineage columns. LIVE/LIVE_PRECHECK remain strict; LIVE remains NOT LIVE READY.

- **2026-07-18 startup-interruption audit:** Detached launch is now compensating-transition safe for `KeyboardInterrupt`, `SystemExit`, `_launch_worker()` `RuntimeError`, spawn failure, worker early exit, attachment timeout, and identity mismatch. Zero-exposure startup failures can be safely terminalized; exposure-bearing or unavailable evidence remains blocked.

- **2026-07-23 reconciliation/configuration state:** one canonical reconciliation loader supplies runtime, burn-in, and diagnostic provider settings. Demo reconciliation is REST-only, while runtime/streaming websocket requirements remain strict. `config_fix` is dry-run-first and only canonicalizes the unambiguous receive-window alias; it never repairs secrets, LIVE controls, or ambiguous risk values. Operational Demo acceptance and remote CI remain outstanding validation, not pending merged code.

- **2026-07-20 Windows diagnostics:** reconciliation uses a narrow canonical config loader; global invalid settings remain separately visible; daily loss remains a fraction and LIVE remains NOT READY.

- **2026-07-20 dotenv correction:** operator diagnostics bootstrap canonical `.env` once; explicit mapping APIs remain isolated; LIVE remains NOT READY.

- **2026-07-23 PR #297 correction:** `diagnose-db` is schema-adaptive and read-only across historical databases; safe classifications require fresh, lineage-matched, authenticated COMPLETE zero-exposure evidence. Missing evidence remains manual review. LIVE remains disabled and NOT LIVE READY.
