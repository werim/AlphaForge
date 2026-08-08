# AlphaForge Version

## PR #310 SQLite contention revision (2026-08-01)

- Current version: Phase A isolated shadow writer revision
- Current phase: immutable contracts plus bounded, single-worker shadow orchestration
- Runtime maturity: graph disabled by default; enabled traces use a separate SQLite database and cannot contend with authoritative runtime writers
- BACKTEST/PAPER/LIVE alignment: authoritative decisions and lifecycle behavior remain unchanged
- Lifecycle coverage: unchanged; 50+ decision concurrency stress covers simultaneous canonical lifecycle/reject/reconciliation/heartbeat persistence
- Execution realism coverage: unchanged; no orders or fills are submitted or simulated
- Known critical risks: overload intentionally drops newest shadow trace and records the count; abrupt termination can still lose in-flight optional evidence
- Last audit date: 2026-08-01
- Live readiness verdict: NOT LIVE READY

## PR #311 Control Center correctness revision (2026-08-01)

- Current version: Phase 9 PAPER Control Center pre-limit reject and worker-postcondition revision
- Current phase: merge-blocker correctness and process-safe operation ownership
- Runtime maturity: campaign-wide canonical reject semantics and bounded pause worker verification are fixture-tested; Windows acceptance remains pending
- BACKTEST/PAPER/LIVE alignment: PAPER-only control remains enforced; trading paths are unchanged
- Lifecycle coverage: PAUSED campaign and stopped/detached worker are independently verified; ambiguous evidence is partial failure
- Execution realism coverage: no synthetic evidence; missing identity/reason/process evidence remains explicit
- Known critical risks: PID command-line/start identity is not canonical and live Windows lease/polling behavior is unverified
- Last audit date: 2026-08-01
- Live readiness verdict: NOT LIVE READY

## Phase A shadow agent graph (2026-08-01)

- Current version: Phase A agent graph foundation
- Current phase: immutable contracts and deterministic shadow orchestration; no agent business logic
- Runtime maturity: legacy runtime remains authoritative; graph disabled by default and isolated from order mutation
- BACKTEST/PAPER/LIVE alignment: decision and lifecycle behavior unchanged; the optional trace hook observes legacy snapshots only
- Lifecycle coverage: unchanged; agent traces use separate additive tables
- Execution realism coverage: unchanged; missing context remains null and no fills/orders are simulated
- Known critical risks: full business-agent parity and sustained shadow evidence are not implemented; background traces may be absent on abrupt process termination
- Last audit date: 2026-08-01
- Live readiness verdict: NOT LIVE READY

## PR #307 merged-dev audit (2026-07-28)

- Current version: unchanged Phase 9 Binance USD-M Unicode catalog validation v8
- Current phase: post-merge verification; no runtime behavior change
- Runtime maturity: local source suite passed 1072 tests; clean Python 3.11 install and GitHub Actions identity remain unverified because network access failed
- BACKTEST/PAPER/LIVE alignment: unchanged by this documentation-only audit
- Lifecycle coverage: unchanged; no lifecycle transition or persistence contract changed
- Execution realism coverage: unchanged; no new runtime distribution evidence was collected
- Known critical risks: 3 optional external tests skipped; GitHub Actions run ID unavailable; exact config command requires installation or `PYTHONPATH=src`
- Last audit date: 2026-07-28
- Live readiness verdict: NOT LIVE READY

- Current version: Phase 9 Binance USD-M Unicode catalog validation v8
- Current phase: Phase 9 - fail-closed exchange exposure evidence hardening
- Runtime maturity: Account-wide Binance position and order reconciliation accepts grammar-exception symbols only by exact public `exchangeInfo` membership; unsafe raw input remains blocking before catalog lookup.
- BACKTEST/PAPER/LIVE alignment: PAPER and LIVE reconciliation share exact catalog validation, while the public strategy scanner independently admits only `TRADING` catalog members.
- Lifecycle coverage: Unchanged; no trade lifecycle transition or persistence contract changed.
- Execution realism coverage: Global exposure remains visible for any safe catalog-listed status, including `PENDING_TRADING`; only `TRADING` symbols enter the new-trade universe.
- Known critical risks: Authenticated Demo acceptance and sustained PAPER validation remain outstanding; catalog outages fail closed for grammar-exception exposure.
- Last audit date: 2026-07-25
- Live readiness verdict: NOT LIVE READY; reconciliation hardening does not authorize LIVE execution.

- Current version: Phase 9 PAPER failed-startup recovery v6
- Current phase: Phase 9 - fail-closed schema, recovery, and persistence hardening
- Runtime maturity: PAPER recovery may terminalize a PAUSED/FAILED campaign, or a RECOVERY_REQUIRED campaign stamped specifically by a prior recovery-drill precheck failure, only when its FAILED run has complete zero-activity and zero-local-exposure SQL evidence and read-only reconciliation is the only unavailable evidence; every broader case remains blocked.
- BACKTEST/PAPER/LIVE alignment: canonical exposure validation is shared by runtime recovery and operational preflight; LIVE/LIVE_PRECHECK remain fully fail-closed.
- Lifecycle coverage: Startup recovery audits lifecycle execution states and recognized position/order terminal or active states while preserving rows append-only.
- Execution realism coverage: Provider unavailability is recorded explicitly and never substitutes for zero SQL execution/exposure evidence.
- Known critical risks: Authenticated reconciliation is still required for any ambiguous or nonzero exposure, any RECOVERY_REQUIRED state without recovery-drill provenance, and all LIVE operation; sustained PAPER burn-in, PostgreSQL doctor parity, and Demo acceptance remain outstanding.
- Last audit date: 2026-07-25
- Live readiness verdict: NOT LIVE READY; migration correctness does not authorize LIVE execution.

## Prior operational baseline

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
## 0.1.1 — Schema compatibility hardening (2026-07-24)

- **Phase:** persistence/runtime safety audit
- **Runtime maturity:** PAPER/BACKTEST persistence operational; LIVE remains blocked
- **Mode alignment:** canonical SQLite bootstrap and exposure schema validation now shared by runtime and burn-in preflight
- **Lifecycle coverage:** unchanged; lifecycle persistence remains additive and auditable
- **Execution realism:** unchanged
- **Critical risks:** non-SQLite schema doctor coverage and ambiguous legacy exposure shapes require manual migration
- **Last audit:** 2026-07-24
- **Live readiness:** **NOT READY** — full suite and production database validation remain operator gates

## PAPER Control Center backend (2026-08-01)

- Current version: Phase 9 PAPER Control Center backend adapter
- Current phase: canonical burn-in observation and guarded operator control
- Runtime maturity: read-only, schema-aware APIs plus verified canonical CLI pause/resume; real Windows burn-in acceptance remains pending
- BACKTEST/PAPER/LIVE alignment: PAPER-only guard; trading decision paths are unchanged and LIVE control is rejected
- Lifecycle coverage: canonical campaign/run pause and continuation transitions are reused and postcondition checked; no STOPPED state is fabricated
- Execution realism coverage: canonical persisted decisions, positions, rejects, heartbeat and worker process evidence only; unavailable values remain null
- Known critical risks: no live Windows worker validation; PID command-line identity is not canonical; sustained burn-in evidence is still required
- Last audit date: 2026-08-01
- Live readiness verdict: NOT LIVE READY

## PR #311 deployment compatibility revision (2026-08-06)

- Current version: Phase 9 PAPER Control Center deployment adapter
- Current phase: route/CORS/entry-point and health-contract compatibility
- Runtime maturity: backend aliases, diagnostics, exact-origin CORS, and local executable are testable; external SPA acceptance remains pending
- BACKTEST/PAPER/LIVE alignment: reads report typed configured mode; control actions remain PAPER-only
- Lifecycle coverage: unchanged; canonical pause/resume postconditions and audit remain enforced
- Execution realism coverage: database, runtime, campaign, and worker health are reported separately without fabricated evidence
- Known critical risks: hosted-browser local-network policy and external frontend contract remain deployment acceptance gates
- Last audit date: 2026-08-06
- Live readiness verdict: NOT LIVE READY
