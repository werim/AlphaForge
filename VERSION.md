# AlphaForge Version

## PAPER reject-label integrity gate (2026-08-16)

- Current version: Phase 9 SQL-first reject-label validation
- Current phase: pre-Issue #309 Phase C evidence gating; Phase C has not started
- Runtime maturity: read-only campaign and standalone validation exposes identity, resolver, finalized-evidence, and reject-quality integrity
- BACKTEST/PAPER/LIVE alignment: observability only; decisions, lifecycle, resolver semantics, execution, and graph authority are unchanged
- Lifecycle coverage: authoritative rejected reviews are checked through pending labels and canonical forward outcomes
- Execution realism coverage: accuracy excludes incomplete, ambiguous, and execution-invalidated evidence; unavailable metrics remain null
- Known critical risks: provider gaps and legacy null timeframe/horizon-bar evidence remain explicitly incomplete; a PASS requires mature eligible evidence
- Last audit date: 2026-08-16
- Live readiness verdict: NOT LIVE READY

## Existing SQLite reject-label compatibility hotfix (2026-08-16)

- Current version: Phase 9 reject-label SQLite compatibility hotfix
- Current phase: additive existing-PAPER-database repair
- Runtime maturity: canonical startup and schema doctor now require and idempotently add all resolver-consumed PR #317 pending-label columns
- BACKTEST/PAPER/LIVE alignment: persistence schema alignment only; decision and execution behavior are unchanged
- Lifecycle coverage: legacy pending reject evidence is preserved and remains resolvable after restart
- Execution realism coverage: legacy rows retain stored `horizon_seconds`; no timeframe or horizon is fabricated
- Known critical risks: legacy rows still lack timeframe-aware completeness checks unless original source evidence supplies those fields
- Last audit date: 2026-08-16
- Live readiness verdict: NOT LIVE READY

## PAPER reject forward-label feedback restoration (2026-08-13)

- Current version: Phase 9 reject feedback-loop restoration
- Current phase: execution-aware PAPER reject outcome observation
- Runtime maturity: eligible reviews and pending labels commit atomically; partial market windows remain retryable until one complete outcome is finalized
- BACKTEST/PAPER/LIVE alignment: PAPER gains observational labeling; thresholds and execution decisions are unchanged
- Lifecycle coverage: every authoritative final PAPER gate uses one reject-decision identity across review, pending label, and outcome without creating trades
- Execution realism coverage: only complete, contiguous, execution-valid windows may set `reject_correct`; raw incomplete observations remain auditable
- Known critical risks: legacy pending rows without timeframe use their stored seconds; persistent provider gaps intentionally leave evidence pending
- Last audit date: 2026-08-16
- Live readiness verdict: NOT LIVE READY

## Stale PAPER STARTING recovery (2026-08-11)

- Current version: Phase 9 stale-scanner recovery correction
- Current phase: transactional PAPER campaign lifecycle recovery
- Runtime maturity: an operational attached worker owns a three-row-consistent `STARTING -> RUNNING` before any `OPERATING` snapshot; a dead stale scanner may become `FAILED` only through authenticated zero-exposure terminalization
- BACKTEST/PAPER/LIVE alignment: PAPER campaign metadata corrected; LIVE recovery remains unchanged and fail-closed
- Lifecycle coverage: decisions remain preserved; zero executions and zero execution lifecycle states are mandatory for the fallback
- Execution realism coverage: fresh authenticated complete exchange positions/orders and available zero local/runtime exposure are mandatory
- Known critical risks: missing worker identity, evidence, lineage, partial status promotion, or any exposure blocks recovery
- Last audit date: 2026-08-12
- Live readiness verdict: NOT LIVE READY

## Issue #309 Phase B shadow evidence (2026-08-10)

- Current version: Phase B Market/Signal/Quality shadow adapters
- Current phase: observational parity burn-in; no cutover
- Runtime maturity: graph remains disabled by default and legacy-authoritative
- BACKTEST/PAPER/LIVE alignment: identical shadow adapters; no authoritative behavior change
- Lifecycle coverage: additive shadow SIGNAL_CREATED/SIGNAL_REJECTED evidence only
- Execution realism coverage: observed spread/slippage/liquidity/funding are nullable; raw RR is geometric and zero effective RR is preserved
- Known critical risks: incomplete legacy snapshots defer candidate/quality analysis; Phase C+ remains unimplemented
- Last audit date: 2026-08-10
- Live readiness verdict: NOT LIVE READY

## PR #314 provenance and recovery-scope correction (2026-08-09)

- Current version: Phase 9 authenticated terminalization evidence revision
- Current phase: explicit PAPER-only historical continuation terminalization
- Runtime maturity: shared recovery remains conservatively blocked for same-campaign unclean state; only the explicit terminalizer owns the evidence bridge
- BACKTEST/PAPER/LIVE alignment: normal PAPER and all LIVE recovery semantics unchanged
- Lifecycle coverage: existing transactional FAILED terminalization contract unchanged
- Execution realism coverage: bridge requires machine-verifiable authenticated exchange provenance plus complete zero exposure
- Known critical risks: missing or unauthenticated provenance fails closed without evidence persistence
- Last audit date: 2026-08-09
- Live readiness verdict: NOT LIVE READY

## Historical PAPER evidence bridge (2026-08-09)

- Current version: Phase 9 campaign-linked terminalization evidence revision
- Current phase: explicit dead-continuation recovery terminalization
- Runtime maturity: fresh authenticated reconciliation is appended and transaction-bound before terminal mutation
- BACKTEST/PAPER/LIVE alignment: PAPER-only operator recovery; BACKTEST and LIVE behavior unchanged
- Lifecycle coverage: RECOVERY_REQUIRED becomes FAILED only under the existing atomic terminalization contract
- Execution realism coverage: complete CLEAN zero-position/order/orphan exchange evidence remains mandatory and expires after 120 seconds
- Known critical risks: provider or worker-death ambiguity remains fail-closed; historical reduced snapshot schemas are additively completed
- Last audit date: 2026-08-09
- Live readiness verdict: NOT LIVE READY

## PAPER terminalization TOCTOU hardening (2026-08-06)

- Current version: Phase 9 transactional recovery evidence revision
- Current phase: final in-transaction validation and evidence-bound terminalization
- Runtime maturity: local recovery gates are re-read under `BEGIN IMMEDIATE`; all status mutations require exactly one row
- BACKTEST/PAPER/LIVE alignment: PAPER-only recovery; trading and qualification paths unchanged
- Lifecycle coverage: execution/lifecycle counts, continuation identity, source hashes, and audit evidence are transaction-bound
- Execution realism coverage: fresh CLEAN reconciliation is bound to an immutable runtime snapshot identity
- Known critical risks: PID start identity is not persisted on every legacy campaign; missing dead-worker identity fails closed
- Last audit date: 2026-08-06
- Live readiness verdict: NOT LIVE READY

## PAPER zero-exposure terminalization follow-up (2026-08-06)

- Current version: Phase 9 PAPER recovery completion hotfix
- Current phase: explicit zero-exposure operator terminalization and non-blocking contention waits
- Runtime maturity: resolver and maintenance SQLite waits run off the asyncio event loop; heartbeat and scanning remain schedulable
- BACKTEST/PAPER/LIVE alignment: PAPER-only recovery operation; decision and qualification behavior unchanged
- Lifecycle coverage: RECOVERY_REQUIRED continuations can become FAILED only after explicit, complete zero-exposure verification
- Execution realism coverage: CLEAN reconciliation and available zero runtime/campaign exposure are mandatory
- Known critical risks: terminalization is intentionally unavailable for any unknown/nonzero exposure or execution lifecycle evidence
- Last audit date: 2026-08-06
- Live readiness verdict: NOT LIVE READY

## PAPER burn-in SQLite contention recovery (2026-08-01)

- Current version: Phase 9 PAPER burn-in contention hotfix
- Current phase: operational burn-in resilience and stale-worker recovery
- Runtime maturity: resolver/qualification SQLite locks use bounded fresh-connection retries and skip exhausted cycles without stopping scanning or runtime heartbeat
- BACKTEST/PAPER/LIVE alignment: decision, reject, and qualification thresholds are unchanged; the patch affects PAPER operational scheduling only
- Lifecycle coverage: evidence and lifecycle rows remain deterministic; stale dead-worker continuations transition to `RECOVERY_REQUIRED`
- Execution realism coverage: unchanged; no fills, costs, or qualification gates are weakened
- Known critical risks: SQLite remains a single-writer database; sustained contention can defer qualification and requires operator review
- Last audit date: 2026-08-01
- Live readiness verdict: NOT LIVE READY

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

## PR #313 explicit-CORS and composite-freshness revision (2026-08-09)

- Current version: Phase 9 PAPER Control Center finishing safety pass
- Current phase: explicit browser trust and honest multi-source freshness
- Runtime maturity: no implicit CORS origins; composite responses preserve per-source evidence without aggregate timestamp fabrication
- BACKTEST/PAPER/LIVE alignment: unchanged; controls remain PAPER-only
- Lifecycle coverage: unchanged; canonical pause/resume postconditions remain enforced
- Execution realism coverage: process presence is distinct from canonical worker health
- Known critical risks: new GitHub Actions head and real Windows/browser acceptance remain required
- Last audit date: 2026-08-09
- Live readiness verdict: NOT LIVE READY

## PR #313 canonical Control Center revision (2026-08-08)

- Current version: Phase 9 PAPER Control Center canonical safety correction
- Current phase: Python 3.11 CI, persisted freshness, attachment identity, and recovery-boundary verification
- Runtime maturity: focused Control Center tests pass locally; full suite and current GitHub Actions remain release gates
- BACKTEST/PAPER/LIVE alignment: observer reads are shared canonical evidence; controls remain PAPER-only and recovery mutation stays in burn-in ops
- Lifecycle coverage: no new transitions; guarded CLI postconditions observe canonical campaign/run state
- Execution realism coverage: missing/future/stale timestamps and ambiguous process attachment are explicit, never fabricated healthy/fresh
- Known critical risks: real Windows PID/lease behavior and sustained PAPER operation remain unverified
- Last audit date: 2026-08-08
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
