# AlphaForge Version

## Execution-cost evidence attribution (2026-09-03)
- Current version: dev execution-cost diagnostic and closed-1m volatility-evidence correction; no schema migration or threshold change.
- Current phase: review and a fresh PAPER campaign after merge; existing campaign evidence remains immutable.
- Runtime maturity: fee and latency penalties are attributed to their canonical fields; PAPER volatility uses up to 20 closed Binance 1m candles and remains unavailable/fail-closed when evidence is absent.
- BACKTEST/PAPER/LIVE alignment: the shared cost model is corrected; PAPER evidence propagation is improved; LIVE authorization and order semantics are unchanged.
- Lifecycle/persistence/execution impact: lifecycle and persistence schemas are unchanged; total penalty is unchanged by the attribution fix, while real volatility evidence can legitimately change effective RR versus the former missing-evidence penalty.
- Known critical risks: public HTTP RTT is market-data transport latency, not order submit/ack latency; a new campaign is required for homogeneous post-fix evidence.
- Last audit date: 2026-09-03. Live readiness verdict: NOT LIVE READY.

## Canonical PAPER env-template contract (2026-09-02)
- Current version: dev PAPER env-template safety correction; no runtime-code or schema migration.
- Current phase: operator supplies matching read-only Binance credentials, then runs PAPER preflight.
- Runtime maturity: `.env.example` is the canonical PAPER/burn-in profile; BACKTEST diagnostics are isolated in `.env.test.example`.
- BACKTEST/PAPER/LIVE alignment: PAPER mode derives active runtime limits and stable campaign/runtime config identity from the same canonical setting.
- Lifecycle/persistence/execution impact: unchanged; signed read-only reconciliation remains mandatory and real-order authorization remains disabled.
- Known critical risks: placeholder or missing credentials intentionally block env contract and authenticated reconciliation; external Binance availability remains required.
- Last audit date: 2026-09-02. Live readiness verdict: NOT LIVE READY.

## PAPER closed-candle geometry integrity (2026-09-01)
- Current version: dev post-PR338 invalid-geometry contamination correction; no schema migration.
- Current phase: review, CI validation, then a fresh PAPER campaign.
- Runtime maturity: enriched Binance 1m candidates fail closed before MTF/scoring when geometry is not COMPLETE; decisions use stable symbol/source/timeframe/closed-candle identity.
- BACKTEST/PAPER/LIVE alignment: the production PAPER enrichment path is corrected; legacy unenriched test/backtest paths remain compatible and thresholds are unchanged.
- Lifecycle coverage: invalid geometry records SIGNAL_CREATED -> SIGNAL_REJECTED with its provider reason and cannot enqueue a forward label.
- Execution realism coverage: missing geometry leaves side/stop/target/RR null; no synthetic 2.0 RR remains in signal construction.
- Known critical risks: historical contaminated evidence remains immutable and must not qualify the new behavior; provider candle availability remains external; local full pytest lacks the declared Alembic package and CI must validate those migration tests.
- Last audit date: 2026-09-01. Live readiness verdict: NOT LIVE READY.
- Merge-blocker follow-up: identity-less provider/data failures are idempotent `DIAGNOSTIC` observations, not canonical market rejects; identified invalid candles retain one canonical reject per candle, including across runtime restarts. In-memory candle suppression is bounded to one latest timestamp per market key.

## PR #338 canonical reject follow-up (2026-09-01)
- Current version: dev canonical decision/reject calibration correction; no destructive schema migration.
- Current phase: review and CI validation before a fresh PAPER campaign.
- Runtime maturity: only earlier canonical rows deduplicate canonical KPIs; diagnostic-first runtime ordering remains auditable and cannot suppress the decision.
- BACKTEST/PAPER/LIVE alignment: MTF remains fail-closed; all per-layer threshold defaults remain 0.0005 and calibration never tunes them.
- Lifecycle coverage: pending-label eligibility is based on recorded contract validation failures, with ineligible reason and integrity metrics.
- Execution realism coverage: completed execution-strength evidence includes an overflow bucket and cost-adjusted outcomes.
- Known critical risks: historical labels are not fabricated; local full-suite collection requires the declared Alembic dependency, unavailable from this network-restricted container.
- Last audit date: 2026-09-01. Live readiness verdict: NOT LIVE READY.

## Canonical PAPER reject calibration (2026-09-01)
- Current version: dev canonical decision/reject calibration patch; no destructive schema migration.
- Current phase: fresh PAPER burn-in required to collect outcome-complete MTF execution evidence.
- Runtime maturity: decision KPIs deduplicate by reject-decision/signal identity while diagnostic evidence remains physical and exportable; pending labels are idempotent.
- BACKTEST/PAPER/LIVE alignment: MTF remains a fail-closed PAPER gate; defaults remain 0.0005 and no threshold is auto-relaxed.
- Lifecycle coverage: canonical rejects remain SIGNAL_REJECTED; ineligible geometry remains diagnostic rather than becoming a fabricated label.
- Execution realism coverage: completed, cost-adjusted forward outcomes are bucketed by execution MA strength; ambiguous outcomes do not enter reject-correct denominators.
- Known critical risks: historical campaigns are not rewritten and threshold choice still needs fresh uncensored/cost-complete evidence.
- Last audit date: 2026-09-01. Live readiness verdict: NOT LIVE READY.

## Burn-in evidence and continuation integrity (2026-09-01)
- Current version: dev burn-in evidence/lifecycle patch; no table or CSV schema migration.
- Current phase: fresh PAPER campaign verification with historical timestamps and diagnostic rows preserved.
- Runtime maturity: qualification and its scheduling cadence count canonical decisions only; duration begins at operational attachment and accumulates eligible continuation intervals only; detached successors inherit persisted release identity; process probes are non-mutating across supported platforms.
- BACKTEST/PAPER/LIVE alignment: decision and reject behavior is unchanged; corrections are limited to PAPER evidence and worker lifecycle control.
- Lifecycle coverage: incomplete reject geometry remains fail-closed; detached successors remain STARTING until runtime attachment verifies identity; failed unattached startups add no active duration.
- Execution realism coverage: unchanged; missing geometry remains missing and is never fabricated.
- Known critical risks: Windows command-line ownership is unavailable through the query-only handle, so worker ownership additionally depends on persisted launch time matching process creation time; macOS without inspectable identity conservatively treats an existing PID as alive to prevent duplicate workers.
- Last audit date: 2026-09-01. Live readiness verdict: NOT LIVE READY.

## PAPER MTF heartbeat persistence follow-up (2026-08-31)
- Current version: dev PR #336 observability follow-up; no schema, export, threshold, or execution change.
- Current phase: heartbeat persistence verification before fresh post-fix PAPER campaign validation.
- Runtime maturity: the complete MTF counter family emitted by runtime now survives the heartbeat safety allowlist.
- BACKTEST/PAPER/LIVE alignment: trading behavior is unchanged; the heartbeat JSON extension is backward-compatible.
- Lifecycle coverage: unchanged; neutral execution remains fail-closed before AIBrain.
- Execution realism coverage: unchanged; no evidence or default value is fabricated.
- Known critical risks: setup evidence availability versus no-valid-setup remains a separate semantic review; setup rejection is unchanged and fail-closed.
- Last audit date: 2026-08-31. Live readiness verdict: NOT LIVE READY.

## PAPER MTF execution-evidence classification correction (2026-08-30)
- Current version: dev PAPER MTF observability patch; no database migration or export change.
- Current phase: fresh post-fix PAPER campaign validation while `camp_9afc71c6a419749c` remains immutable historical regression evidence.
- Runtime maturity: complete neutral 1m evidence is distinguished from missing/invalid evidence and still rejects before AIBrain.
- BACKTEST/PAPER/LIVE alignment: only PAPER MTF evidence classification changed; shared downstream gates, BACKTEST behavior, and LIVE authorization are unchanged.
- Lifecycle coverage: both unavailable evidence and unconfirmed triggers retain SIGNAL_CREATED -> SIGNAL_REJECTED ordering with full MTF diagnostics.
- Execution realism coverage: required candle, spread, slippage, latency, and liquidity values must be present, finite, and non-negative; no defaults are fabricated.
- Known critical risks: the unchanged 0.0005 per-timeframe defaults remain uncalibrated for production qualification; public Binance availability remains external.
- Last audit date: 2026-08-30. Live readiness verdict: NOT LIVE READY.

## Fail-closed PAPER multi-timeframe runtime (2026-08-30)
- PR #334 follow-up: MTF execution timing now consumes the canonical normalized execution context; aligned direction is bound to geometry side and non-Binance sources fail closed without cross-exchange substitution.
- Current version: dev PAPER MTF decision architecture; no database migration.
- Current phase: fresh 1h regime / 15m setup / 1m execution campaign qualification required.
- Runtime maturity: selected Binance candidates receive cached, closed-candle contexts and deterministic alignment before existing AIBrain, expectancy, portfolio, and execution gates.
- BACKTEST/PAPER/LIVE alignment: PAPER alone enables the new prerequisite while shared downstream gates remain authoritative; offline BACKTEST and LIVE authorization are unchanged.
- Lifecycle coverage: MTF failures persist explicit SIGNAL_REJECTED evidence; historical rows are immutable.
- Execution realism coverage: missing provider, spread, slippage, latency, liquidity, or candle evidence fails closed rather than becoming zero/neutral.
- Known critical risks: the MA-based structural classifier requires fresh campaign calibration and public Binance availability; no LIVE qualification has been established.
- Last audit date: 2026-08-30. Live readiness verdict: NOT LIVE READY.


## Fresh SQLite runtime-contract reconciliation (2026-08-29)
- Current version: dev runtime-contract patch; no destructive migration and Alembic remains at `0008_database_doctor_lifecycle_contract`.
- Current phase: canonical fresh-bootstrap and Database Doctor alignment for PAPER.
- Runtime maturity: `init_db` provisions runtime control, runtime state, and reconciliation through their canonical runtime schema functions; heartbeat remains PAPER/LIVE-provisioned rather than a BACKTEST side effect.
- BACKTEST/PAPER/LIVE alignment: shared persistence writers pass isolated fresh-database smoke probes; execution and decision behavior are unchanged.
- Lifecycle coverage: canonical SQL-first lifecycle schema and both conflict identities remain verified.
- Execution realism coverage: unchanged; unavailable execution values remain nullable and are not replaced with zero.
- Known critical risks: unrelated legacy ORM-only tables still make global Alembic autogenerate unsafe; this is separated from PAPER runtime certification.
- Last audit date: 2026-08-29. Live readiness verdict: NOT LIVE READY.

## Database Doctor repository contract auditor (2026-08-29)
- Current version: Alembic `0008_database_doctor_lifecycle_contract`; no new migration.
- Current phase: repository-wide read-only SQLite contract diagnosis.
- Runtime maturity: ownership, writer, target, feature, exposure, and adaptive audits fail closed.
- Gating maturity: runtime certification, lifecycle repair, migration, schema consolidation, and Alembic autogeneration consume explicit per-finding blockers rather than treating every finding as a runtime failure.
- ORM maturity: deployed metadata drift makes Alembic autogenerate explicitly unsafe; `exchange_symbols` is currently absent from the `init_db` family and differs from historical Alembic naming.
- BACKTEST/PAPER/LIVE alignment: PAPER certification is SQLite-only; LIVE authority is unchanged.
- Lifecycle coverage: v1 checks remain intact. Execution evidence is never invented.
- Known critical risks: multiple schema owners and ORM/Alembic drift require follow-up.
- Dialect coverage: runtime SQL surfaces containing SQLite DDL/functions are classified `SQLITE_ONLY`; PostgreSQL PAPER certification is blocked without removing future migration support.
- Last audit date: 2026-08-29. Live readiness verdict: NOT LIVE READY.

## Database Doctor v1 (2026-08-29)

- Current version: Alembic `0008_database_doctor_lifecycle_contract`.
- Current phase: evidence-preserving SQLite lifecycle schema remediation and operator certification.
- Runtime maturity: Database Doctor identifies, diagnoses, backs up, repairs, and probes the actual persistence writers; unknown or duplicate evidence fails closed.
- Certification maturity: private probes use SQLite online backup snapshots with committed WAL content; repair success requires structural and executable writer verification.
- BACKTEST/PAPER/LIVE alignment: shared persistence contract repaired; PAPER behavior is preserved and LIVE authority is unchanged.
- Lifecycle coverage: `trade_lifecycle_events.id` is SQLite rowid/autoincrement compatible; canonical identities and legacy payload/order-intent evidence survive rebuild.
- Execution realism coverage: unchanged; no execution, score, RR, timestamp, or historical identity evidence is invented.
- Known critical risks: non-lifecycle tables created solely by historical Alembic may not satisfy every newer optional runtime writer; certification reports this rather than masking it.
- Last audit date: 2026-08-29.
- Live readiness verdict: NOT LIVE READY.

## PR #329 provider identity binding follow-up (2026-08-20)

- Current version: dev PR #329 provider-scope identity correction.
- Current phase: fresh PAPER campaign required; historical contaminated evidence remains immutable.
- Runtime maturity: canonical `paper_source_exchanges` is hashed into campaign config identity and independently checked at attachment, including direct cross-provider identity rejection.
- BACKTEST/PAPER/LIVE alignment: PAPER provider identity is Binance read-only; trading logic and LIVE authority are unchanged.
- Lifecycle and execution realism: pre-selection filtering and explicit kline diagnostics remain fail closed.
- Known critical risks: historical campaigns created without this identity field cannot be resumed as post-fix evidence.
- Last audit date: 2026-08-20.
- Live readiness verdict: NOT LIVE READY.

## PAPER campaign executable-scope correction (2026-08-20)

- Current version: dev post-PR-#328 campaign-scope correctness repair.
- Current phase: fresh PAPER campaign required; `camp_e902c3018c2eb1fd` remains immutable contaminated evidence.
- Runtime maturity: attached campaign symbols and Binance provider identity now bound selection, decisions, persistence, and PAPER execution.
- BACKTEST/PAPER/LIVE alignment: shared geometry calculation remains compatible; PAPER adds identity enforcement and LIVE authority is unchanged.
- Lifecycle coverage: out-of-scope candidates cannot create lifecycle decisions; late invariant violations fail closed with durable diagnostics.
- Execution realism coverage: closed-1m Binance geometry failures expose status, reason, and source without synthetic values.
- Known critical risks: the historical 56 incomplete rows predate diagnostic reason capture and cannot be exactly classified without rewriting evidence.
- Last audit date: 2026-08-20.
- Live readiness verdict: NOT LIVE READY.

## PR #328 timeframe and health follow-up (2026-08-20)

- PR #328 CI follow-up: PAPER decision-timeframe registry metadata now names the production Binance scanner consumer and a behavioral node proves unsupported values fail closed without a 1m fallback.
- Campaign reporting intervals, PAPER decision/setup timeframe, reject evaluation timeframe, and horizon bars are explicit identity fields.
- Resolver health regressions directly cover immature, overdue, stale-claim, resolver-failure, and provider-failure states.
- `ALPHAFORGE_PAPER_FEE_BPS` is the total round-trip entry-plus-exit fee cost and is applied once.
- No schema migration; fresh PAPER campaign required; LIVE remains NOT READY.

## PAPER reject-forward evidence repair (2026-08-20)

- Current version: dev PAPER reject-forward evidence regression repair
- Current phase: fresh-campaign PAPER validation required; historical `camp_5004b6d9236213b6` is immutable regression evidence
- Runtime maturity: PAPER fee provenance, measured Binance book-ticker RTT, canonical bidirectional geometry, and mature resolver health are wired into the production chain
- BACKTEST/PAPER/LIVE alignment: shared decision/lifecycle logic is unchanged; the explicit fee assumption is PAPER-only and LIVE mutation remains disabled
- Lifecycle coverage: LONG and SHORT rejects can create pending labels; provider failures remain auditable incomplete geometry
- Execution realism coverage: configured non-negative PAPER fees and measured public HTTP RTT are explicit; unavailable values remain null
- Known critical risks: public-provider outages leave latency/geometry incomplete; new evidence must be collected in a new campaign
- Last audit date: 2026-08-20
- Live readiness verdict: NOT LIVE READY

## Runtime lifecycle schema repair (2026-08-18)

- Current version: Alembic `0007_repair_runtime_lifecycle_schema`
- Current phase: additive SQLite lifecycle-contract repair before PAPER relaunch
- Runtime maturity: launch preflight now verifies every lifecycle persistence column and both SQLite upsert conflict targets
- BACKTEST/PAPER/LIVE alignment: all modes retain the shared lifecycle writer; incompatible databases block before launch
- Lifecycle coverage: canonical legacy `state` evidence may populate `lifecycle_state`; ambiguous states and absent timestamps/decision metrics remain NULL
- Execution realism coverage: unchanged; no execution context, score, RR, decision, or timestamp evidence is fabricated
- Known critical risks: duplicate non-NULL lifecycle identities require operator reconciliation and intentionally abort migration; nullable legacy identities remain auditable
- Last audit date: 2026-08-18
- Live readiness verdict: NOT LIVE READY

## PR #323 post-selection geometry bound (2026-08-18)

- Current version: PR #323 bounded selected-candidate enrichment correction
- Current phase: GitHub CI/re-review before fresh PAPER evidence collection; Phase C has not started
- Runtime maturity: canonical full-universe selection precedes geometry; requests per scan are bounded by unique selected Binance symbols and `max_symbols_per_scan`
- BACKTEST/PAPER/LIVE alignment: BACKTEST and PAPER share one two-closed-candle builder; production scanner remains LONG-only and SHORT is helper-tested
- Lifecycle coverage: rejects remain `SIGNAL_REJECTED`; missing geometry remains ineligible and auditable without order/position creation
- Execution realism coverage: selected 1m Binance candidates use the last two completed 1m setup candles; provider failures leave geometry absent
- Known critical risks: one explicitly timed public request remains per unique selected Binance symbol; fresh PAPER evidence is required and historical campaign `camp_8a577772ded0bdf2` remains immutable
- Last audit date: 2026-08-18
- Live readiness verdict: NOT LIVE READY

## PR #323 geometry parity correction (2026-08-17)

- Current version: PR #323 shared timeframe-geometry correction
- Current phase: post-fix PAPER evidence validation; Phase C is not complete
- Runtime maturity: accepted and rejected candidates share the extracted two-candle breakout geometry used by the accepted backtest path
- BACKTEST/PAPER/LIVE alignment: superseded by the 2026-08-18 architecture above; the raw Binance scanner remains LONG-only and SHORT is helper-tested
- Lifecycle coverage: early rejects remain `SIGNAL_REJECTED`; pending labels are idempotent and no order/position lifecycle is created
- Execution realism coverage: stop spans the current/previous closed 1m candles and target uses the existing setup-strength RR calculation; missing candle evidence fails closed
- Known critical risks: superseded by post-selection enrichment; only selected Binance candidates request geometry, and LIVE remains blocked
- Last audit date: 2026-08-17
- Live readiness verdict: NOT LIVE READY

## PAPER early-reject canonical geometry (2026-08-17)

- Current version: issue #322 PAPER reject forward-label geometry hotfix
- Current phase: post-fix PAPER evidence collection; Phase C is not complete
- Runtime maturity: superseded by PR #323; the early-reject persistence boundary remains, but its original ticker-extrema geometry was removed
- BACKTEST/PAPER/LIVE alignment: normal and rejected signals consume the same scanner geometry; decision and authorization semantics are unchanged
- Lifecycle coverage: rejects remain `SIGNAL_REJECTED`; no order or position lifecycle is created by labelling
- Execution realism coverage: superseded; execution geometry now uses closed timeframe candles as documented above
- Known critical risks: campaign `camp_8a577772ded0bdf2` contains 590 immutable incomplete observations and must not be promoted as post-fix evidence; start a fresh campaign
- Last audit date: 2026-08-17
- Live readiness verdict: NOT LIVE READY

## PAPER canonical persistence and zombie supervision (2026-08-17)

- Current version: PAPER burn-in split-brain persistence hotfix
- Current phase: fail-closed PAPER operational burn-in validation
- Runtime maturity: campaign workers inject one canonical database into runtime, AIBrain, lifecycle, reject, heartbeat, reconciliation, and campaign persistence; terminal maintenance exits promptly, active supervisor exits fail closed, and health evidence is scoped to the attached runtime instance
- BACKTEST/PAPER/LIVE alignment: shared runtime decision and lifecycle paths are unchanged; attached PAPER adds database-identity enforcement
- Lifecycle coverage: SQL failures retain original exception/target evidence and market-loop failures become terminal
- Execution realism coverage: unchanged; reconciliation and execution-cost gates remain enforced
- Known critical risks: historical split-brain evidence requires operator audit and must not be merged as one campaign
- Last audit date: 2026-08-17
- Live readiness verdict: NOT LIVE READY

## Phase C0 reject-gate production evidence correction (2026-08-16)

- Current version: Phase C0 complete-denominator gate, production evidence revision
- Current phase: final pre-Phase C evidence validation; no Phase C agents or cutover
- Runtime maturity: authoritative PAPER reject observations now carry canonical decision, signal, and available campaign/runtime identity
- BACKTEST/PAPER/LIVE alignment: observational persistence and validation only; decisions, thresholds, resolver math, orders, and LIVE authority are unchanged
- Lifecycle coverage: all immature labels block PASS and pending/outcome state contradictions fail closed
- Execution realism coverage: mature coverage must be 1.0; failed, ambiguous, invalidated, geometry, and cost gaps remain explicit
- Known critical risks: legacy unattributed observations prevent exact historical coverage and block Phase C as INCOMPLETE
- Last audit date: 2026-08-16
- Live readiness verdict: NOT LIVE READY

## Complete PAPER reject-coverage gate (2026-08-16)

- Current version: Phase C0 complete reject-denominator validation
- Current phase: final pre-Phase C evidence gate; Phase C agents and cutover are not implemented
- Runtime maturity: read-only campaign/standalone reconciliation covers PAPER reject observations, reviews, pending labels, and canonical outcomes
- BACKTEST/PAPER/LIVE alignment: observability only; runtime decisions, agent graph, orders, and LIVE authority are unchanged
- Lifecycle coverage: eligible rejects require exactly one pending identity and resolved labels require exactly one canonical outcome
- Execution realism coverage: incomplete geometry, unavailable execution costs, failed/ambiguous labels, and execution-invalidated evidence are explicit non-PASS populations
- Known critical risks: historical evidence without stable run/decision identity remains unusable; provider and maturity gaps remain fail-closed as INCOMPLETE
- Last audit date: 2026-08-16
- Live readiness verdict: NOT LIVE READY

## PAPER reject-label integrity gate (2026-08-16)

- Current version: Phase 9 SQL-first reject-label validation
- Current phase: pre-Issue #309 Phase C evidence gating; Phase C has not started
- Runtime maturity: read-only campaign and standalone validation exposes identity, resolver, finalized-evidence, and reject-quality integrity; legacy review linkage now mirrors resolver precedence and fails closed on ambiguity
- BACKTEST/PAPER/LIVE alignment: observability only; decisions, lifecycle, resolver semantics, execution, and graph authority are unchanged
- Lifecycle coverage: authoritative rejected reviews are checked through pending labels and canonical forward outcomes
- Execution realism coverage: accuracy excludes incomplete, ambiguous, and execution-invalidated evidence; unavailable metrics remain null
- Known critical risks: provider gaps and legacy null timeframe/horizon-bar evidence remain explicitly incomplete; ambiguous legacy signal-only review identities fail closed; a PASS requires mature eligible evidence
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

## Runtime bootstrap/default hardening (2026-08-30)
- Current version/phase: dev canonical PAPER bootstrap; no schema migration.
- Runtime maturity: runtime, burn-in, and Alembic share `data/runtime/alphaforge_runtime.db`; PAPER preflight proves signed read-only reconciliation before PASS.
- BACKTEST/PAPER/LIVE alignment: BACKTEST remains credential-free; PAPER ignores simulated orders when auditing real exchange absence; LIVE authorization/mutation gates are unchanged.
- Lifecycle/persistence/execution realism: lifecycle/schema/export shapes are unchanged; missing/invalid authenticated exchange evidence fails closed and remains unavailable rather than fabricated.
- Known critical risks: valid Binance credentials/network access are operational dependencies for PAPER burn-in; existing custom/legacy DBs require deliberate operator selection.
- Last audit date: 2026-08-30. Live readiness verdict: NOT LIVE READY.

## PR #335 merge-blocker follow-up (2026-08-30)
- Current version/phase: dev bootstrap contract review follow-up; no migration.
- Runtime maturity/alignment: runtime, burn-in operations, and burn-in CLI now share URL > legacy path > canonical default precedence; explicit CLI remains highest.
- Lifecycle/execution realism: unchanged; signed PAPER reconciliation remains fail closed and unavailable evidence is not fabricated.
- Known critical risks: PAPER still depends on valid signed read-only Binance access; explicit legacy/custom DB selection remains operator responsibility.
- Last audit date: 2026-08-30. Live readiness verdict: NOT LIVE READY.

## PR #335 Alembic dotenv merge-blocker (2026-08-30)
- Current version/phase: dev bootstrap contract finalization; no migration.
- Runtime maturity/alignment: Alembic, runtime, and burn-in now bootstrap the same dotenv DB contract while deliberate Alembic config overrides remain authoritative.
- Lifecycle/execution realism: unchanged; reconciliation and LIVE safety remain fail closed.
- Known critical risks: migration execution requires the declared Alembic dependency; custom DB ownership remains operator-managed.
- Last audit date: 2026-08-30. Live readiness verdict: NOT LIVE READY.
