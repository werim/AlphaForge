# AlphaForge 0.1.0 — #421 P2-D full-chain replay in progress — 2026-09-24

- Current phase: two P2-A golden cases replay through real PAPER scoring/decision, lifecycle and SQLite persistence, resolver, readiness and isolated audit ingestion. P2-C passed exact-head CI on `c3a9ffd`.
- Runtime maturity/alignment: attached PAPER campaigns now own reject labels, reject identities and decision observations without relying on an environment variable. Accepted decisions retain geometry status/reason in existing JSON evidence; durable accepted evidence prevents repeated PAPER execution, and an unrelated campaign's reject cannot suppress the same signal. BACKTEST and LIVE behavior, thresholds and costs are unchanged; LIVE stays disabled.
- Lifecycle/execution/persistence: frozen profitable LONG closes at TP; high spread rejects before submit and retains a pending forward label. Both replay twice with the same semantic result; repeated accepted scans skip the finalized signal. A real SIGKILL cold-start position also blocks replay. PAPER fills remain modeled. No schema/export migration or historical evidence rewrite.
- Validation: focused replay/crash tests and related regressions passed; exact-head CI for this patch remains required. Known risks: 21 other golden cases still lack production full-chain replay; P2-E load, P2-F documentation sweep and P1-E release qualification remain. Last audit: 2026-09-24. LIVE NOT READY.

# #421 P2-C seeded safety properties — 2026-09-24

- Current phase: P2-C deterministic property contracts for geometry, partial fills, cost monotonicity, time boundaries, and run/campaign/release identity. P2-B exact-head CI passed on b3800e8; this patch still needs its own exact-head CI.
- Runtime maturity/alignment: extreme finite fill ledgers now return finite weighted means; boolean fill price/quantity is rejected. Unrepresentable executable RR returns 0.0 and cannot pass the effective-RR gate. Ordinary finite geometry, costs, thresholds and mode policies are unchanged; LIVE remains disabled.
- Lifecycle/persistence: no state ordering, schema, export, migration, campaign or historical evidence change. The property suite uses isolated in-memory SQL and frozen timestamps.
- Validation: 9 property tests, 167 focused and 238 adjacent regressions passed; the existing mutation gate remained 25/25 KILLED, with no survivors/errors. Compileall/diff checks passed. Known risks: this is bounded seeded coverage, not exhaustive proof. P2-D replay, P2-E load and P1-E release qualification remain. LIVE NOT READY. Last audit: 2026-09-24.

# #421 P2-B targeted safety mutation gate — 2026-09-24

- Current phase: P2-B focused mutation qualification; 25 named source mutations and 14 baseline test nodes passed locally (25 KILLED, 0 SURVIVED, 0 ERROR). Another 252 related regressions, compileall and diff checks passed. Exact pushed CI for this change is pending.
- Runtime maturity/alignment: no production decision, threshold, cost, mode or authorization logic changed. The gate protects PAPER execution and portfolio paths, readiness scope, accepted resolver completeness, market time and final LIVE authorization reread without enabling LIVE.
- Lifecycle/persistence/execution realism: existing tests check rejects, isolated SQLite evidence, scope and position-window behavior. No schema/export change, migration, historical backfill or campaign mutation.
- Risks: focused mutations are not exhaustive; P2-C properties, P2-D replay, P2-E load and P1-E release qualification remain. LIVE NOT READY. Last audit: 2026-09-24.

# Finite configuration threshold validation — 2026-09-24

- Current phase: #421 audit / P2-B preparation; canonical configuration NaN bypass repaired.
- Runtime maturity/alignment: all registry-managed float settings reject NaN and infinities before range checks across environment/dashboard consumers. Defaults and thresholds are unchanged; LIVE remains disabled.
- Lifecycle/persistence/execution realism: no lifecycle, database, schema, export or cost-formula changes. Invalid dashboard updates preserve the existing override file; no campaign data was touched.
- Validation: focused registry/environment and adjacent dashboard/configuration regressions, compileall and diff checks; exact pushed CI still required. Preceding execution-number repair 1b2671f passed exact-head CI.
- Known risks: direct manually constructed runtime configs and other numeric consumers remain separate audit scope. P2-B/release qualification incomplete; LIVE NOT READY. Last audit: 2026-09-24.

# Invalid execution-number safety repair — 2026-09-24

- Current phase: #421 audit / P2-B preparation; reproduced numeric fail-open repaired before mutation work.
- Runtime maturity: canonical safety evaluation blocks NaN, infinities, malformed numeric strings and booleans; invalid effective RR remains unavailable and rejects.
- Alignment/lifecycle: PAPER and LIVE_PRECHECK share the repaired guard; invalid PAPER evidence reaches SIGNAL_REJECTED without execution. Other mode paths are unchanged; this is not full numeric hardening of BACKTEST/LIVE.
- Persistence/execution realism: existing unavailable-field and reject evidence used; no schema/export migration, historical rewrite or campaign mutation.
- Validation: 121 focused and 299 adjacent tests passed; compileall/diff checks passed. Preceding CI prerequisite b711ca7 passed exact-head CI; this patch still requires its own pushed CI.
- Known risks: other numeric consumers/normalizers and configuration thresholds need separate audit; P2-B and release qualification remain incomplete. LIVE NOT READY. Last audit: 2026-09-24.

# CHATGPT exact-commit CI prerequisite — 2026-09-24

- Current phase: #421 P2-B preparation; existing Tests workflow now includes CHATGPT pushes.
- Runtime maturity/alignment, lifecycle, execution realism, persistence and schema: unchanged.
- Validation: six focused CI/audit contract tests and compileall passed locally; pushed exact-head CI remains pending.
- Critical risks: mutation coverage and final release qualification remain incomplete. LIVE NOT READY; last audit 2026-09-24.

# Canonical execution-cost semantics — 2026-09-22

- Current version/phase: prospective `execution_cost_semantics_v1` contract on `fix/canonical-execution-cost-semantics`, based on merged PR #379 (`b833f27`).
- Runtime maturity: `strategy entry -> expected/modelled fill -> actual/realized fill` is now one side-normalized contract. Positive cost is adverse for LONG and SHORT; every percentage/bps metric uses strategy entry as denominator; fees remain separate.
- BACKTEST/PAPER/LIVE alignment: the authoritative effective-RR pipeline and its existing embedded-entry-slippage no-double-count rule are unchanged. PAPER simulated fills are `MODELLED`, never exchange `ACTUAL`. LIVE submission/authorization is unchanged and does not infer missing fill evidence.
- Lifecycle/persistence: decision-time JSON contains only entry, expected fill, expected cost, decision timestamp, and provenance. Fill-time PAPER provenance adds modelled actual fill, realized deviation, total realized cost, and fill timestamp. Missing actual fills remain NULL/None/UNAVAILABLE. No schema migration, historical backfill, campaign launch, or campaign mutation.
- Partial fills: the existing `fills.qty`/`fills.price` ledger now has one deterministic quantity-weighted average helper; it is not wired into the generic LIVE adapter until that adapter supplies authoritative persisted fill groups.
- Validation: 1,782 tests pass with 3 skips when the one unrelated exact-float assertion in `test_strategy_quality_guardrails.py` is deselected; all focused and relevant execution/runtime/PAPER/position/MTF/SHADOW/LIVE-safety suites pass. LIVE remains NOT READY.
- Known critical risks: generic LIVE fill persistence/aggregation is still adapter-defined, historical `actual_slippage_pct` rows are not reinterpreted/backfilled, and #374 must add rejected-trade alignment prospectively from decision-time evidence. Last audit date: 2026-09-22. Live readiness verdict: NOT LIVE READY.

# MTF structural RR geometry — 2026-09-21

- Current version/phase: prospective `mtf_setup_structure_v1` PAPER decision-geometry correction on `fix/structural-mtf-rr-geometry`.
- Runtime maturity: 1h remains regime selection; 15m closed setup candles now provide support/resistance-based structural stop and target; 1m can refine only an entry that lies inside the 15m entry zone. Candidate RR is the independently calculated reward/risk ratio and contains no `MIN_RR` target construction.
- BACKTEST/PAPER/LIVE alignment: execution-cost and effective-RR gates remain unchanged. The existing `MIN_RR` quality filter can reject low structural opportunity, and `LOW_EFFECTIVE_RR` still rejects otherwise valid geometry after fill/cost effects.
- Lifecycle/persistence: absent, invalid, or non-favorable structure fails closed with explicit geometry reasons. Existing JSON decision/MTF evidence now carries geometry source, entry/stop/target source, timeframes, structural levels, candidate RR, executable RR, and effective RR; no schema migration or historical campaign mutation is required.
- Validation: 156 focused structural, MTF, runtime, order-filter, legacy-geometry, scanner, and executable-RR tests passed; changed modules compiled, `git diff --check` passed, and the CI-style F821 lint selection reported zero findings.
- Known critical risks: setup-window extrema are deliberately conservative historical support/resistance evidence, not a forecast of future liquidity. A fresh PAPER campaign is required for prospective distribution evidence; LIVE remains NOT READY.
# MTF execution-confirmation SHADOW experiment — 2026-09-21

- Current version/phase: minimal prospective PAPER-only experiment on `experiment/mtf-execution-shadow`; no campaign was launched, resumed, migrated, or mutated.
- Runtime maturity: 1h regime, 15m setup, and `MTF_EXECUTION_COUNTER_REGIME` remain authoritative. `MTF_EXECUTION_NOT_CONFIRMED` is ENFORCE by default; in explicitly configured PAPER SHADOW mode it is persisted as counterfactual evidence and continues to the existing score, raw-RR, effective-RR, execution-cost, and risk gates.
- BACKTEST/PAPER/LIVE alignment: ENFORCE preserves the prior behavior and identity. SHADOW is rejected outside PAPER at config and direct-runtime construction; LIVE authorization, adapters, and Binance mutation behavior are unchanged.
- Lifecycle coverage: SHADOW candidates follow the existing final lifecycle for their actual downstream outcome. MTF counter-regime and all non-shadow MTF reasons retain the existing immediate `SIGNAL_REJECTED` progression.
- Persistence/identity: no schema migration or export-column change. Canonical observation JSON now records mode, shadow reason, ENFORCE counterfactual reason, and final authoritative reason. SHADOW is included in prospective campaign/config and strategy hashes; ENFORCE retains legacy hashes for compatibility.
- Validation: 193 focused MTF, runtime-env, and configuration-contract tests passed. `compileall` and `git diff --check` passed before documentation updates.
- Known critical risks: SHADOW evidence is experimental and must not be mixed with ENFORCE qualification evidence. A fresh, isolated PAPER campaign is required for outcome analysis; LIVE remains NOT READY. Last audit date: 2026-09-21.

# PAPER execution-candle replay idempotency — 2026-09-21

- Current version/phase: prospective PAPER replay-safety fix on `fix/paper-execution-candle-replay-idempotency`; no campaign was launched, resumed, paused, migrated, or mutated.
- Runtime maturity: Binance closed-1m execution-candle identities are normalized to integer epoch milliseconds and processed only when strictly newer than the per-market high-water mark. Equal, older/replayed, malformed, and already-finalized signal skips are observable separately.
- BACKTEST/PAPER/LIVE alignment: the durable canonical-final lookup is PAPER-only. BACKTEST logic, LIVE authorization, thresholds, selector/MTF/scoring semantics, and execution policy are unchanged.
- Lifecycle coverage: an already-finalized canonical PAPER `signal_id` is stopped before `SIGNAL_CREATED` and state-direction shadow preparation. Later lifecycle transitions for accepted orders/positions are not intercepted.
- Execution realism/persistence: canonical final-decision upserts are unchanged. No schema, migration, export, campaign identity, config hash, or historical evidence changed.
- Validation: 29 focused replay/shadow tests, 126 runtime/persistence/scanner tests, and 94 PAPER/MTF/alignment tests passed; changed Python files compiled and `git diff --check` passed before documentation update.
- Known critical risks: the in-memory high-water mark resets on process restart, so restart safety depends on the canonical final-decision lookup for completed signals; no new durable per-market candle watermark was introduced. The active `POST360S03` campaign and `camp_9f4a9001259415e4` were not accessed or modified. Last audit date: 2026-09-21. LIVE verdict: NOT LIVE READY.

# PAPER pre-MTF selector ordering correction — 2026-09-21

- Current version/phase: prospective PAPER-only selector-ordering fix on `fix/paper-selector-pre-mtf-gate`; no campaign was launched or resumed.
- Runtime maturity: liquid, tight-spread PAPER candidates in MTF-guided mode retain coarse Binance 24h `TOO_CHOPPY` and `WEAK_TREND_AND_NO_RANGE_EDGE` observations as advisories and can reach the canonical 1h/15m/1m pipeline.
- BACKTEST/PAPER/LIVE alignment: BACKTEST selector filters keep their existing hard-reject and experiment-switch semantics. LIVE behavior and authorization are unchanged. PAPER without authoritative MTF guidance is unchanged.
- Lifecycle coverage: selector safety rejects remain pre-signal; candidates passed for analysis can still become canonical `SIGNAL_REJECTED` decisions through MTF, AIBrain, quality, expectancy, RR, execution-cost, and portfolio gates.
- Execution realism: volume, spread, liquidity, funding, provider validity, and other evidenced execution-safety checks remain hard pre-MTF constraints. Coarse chop keeps its ranking penalty. No threshold was loosened.
- Persistence/observability: no schema, migration, export, or historical-row change. Runtime heartbeats add `top_selection_advisory_reasons`; hard rejects remain separate in `top_selection_reject_reasons`.
- Validation: 34 focused tests, 17 exchange-scanner tests, and 447 broader adjacent tests passed; changed Python files compiled; `git diff --check` passed.
- Known critical risks: a fresh PAPER campaign is required for prospective production evidence. The active `POST360S02` campaign and `data/campaign/POST360S02.db` were untouched and must not be resumed as post-change qualification evidence. Last audit date: 2026-09-21. LIVE verdict: NOT LIVE READY.

# Live Readiness Agent v1

- Added a read-only, evidence-only readiness gate engine. It cannot authorize LIVE.

# BACKTEST authoritative AIBrain scoring boundary — 2026-09-20

- Current increment: timestamp-bounded expectancy evidence. New resolved accepted/reject evidence is append-only, keyed to first-class decision identity and decision/resolution time. BACKTEST reads it only with `decision_time <= T AND resolved_at <= T`; absent evidence retains the existing conservative AIBrain prior. PAPER/LIVE scorer and execution behavior are unchanged. Historical execution data is not backfilled or fabricated. LIVE verdict: NOT LIVE READY.

- Current version/phase: prospective deterministic BACKTEST scoring-parity increment; no PAPER/LIVE campaign, order, schema, or historical-data mutation.
- Runtime maturity: PAPER and BACKTEST now share `scoring_context` normalization and the exact `AIBrain.score_signal` / order-plan semantics. The extraction preserves PAPER's selection-regime fallback. BACKTEST uses a stateless scorer and closed candles through the candidate timestamp only.
- Expectancy integrity: runtime SQL expectancy tables are not timestamp-bounded. BACKTEST therefore uses AIBrain's existing zero-history context instead of reading full-run aggregate stats, preventing future-outcome leakage. The resulting lower confidence is explicit.
- Execution realism: historical candle-derived setup/momentum/volatility, derived liquidity, and labelled spread/slippage inputs feed the shared scorer. Funding, orderbook, latency, and historical expectancy remain unavailable unless evidenced; no numeric zero is fabricated.
- Known critical risks: BACKTEST cannot reproduce PAPER's true multi-timeframe exchange snapshots, configured PAPER latency/fill assumptions, or timestamp-bounded expectancy cohorts from the current datasets. It has semantic scorer parity, not complete data parity. LIVE verdict: NOT LIVE READY.
- Validation: 241 requested P0 BACKTEST/runtime tests passed before review; 91 focused scorer/runtime tests passed after the fallback-regression correction; diff checks passed.

# BACKTEST decision-pipeline parity correction — 2026-09-20

- Current version/phase: prospective BACKTEST lifecycle-evidence correction on `feature/state-direction-shadow-evaluation`; no PAPER campaign or LIVE action was started.
- Runtime maturity: BACKTEST now sends the deterministic offline fixture through its established selector, shared pre-submit decision boundary, quality/reject gates, portfolio state, lifecycle persistence, and export path. It no longer injects a hand-authored accepted order and reject row.
- BACKTEST/PAPER/LIVE alignment: BACKTEST continues to use `evaluate_signal_decision` plus `run_order_cycle` for candidate construction, quality gates, effective-RR evidence, and fail-closed parity. PAPER/LIVE runtime code and safety gates were not changed. BACKTEST makes no LIVE exchange/order call; offline mode makes no network call.
- Lifecycle coverage: candidate decisions persist `SIGNAL_CREATED -> SIGNAL_REJECTED` when rejected; accepted candidates continue through `SIGNAL_ACCEPTED -> WAITING_ENTRY_ZONE -> ENTRY_TRIGGERED -> ORDER_PLACED -> POSITION_OPENED -> POSITION_CLOSED | OPEN_AT_END` via the existing simulator.
- Persistence/execution realism: manually fabricated offline `score`, `rr`, `expectancy`, execution fields, and `LOW_EFFECTIVE_RR` reject evidence were removed. Historical funding remains explicitly unavailable when no historical value exists; no zero was fabricated. No schema, migration, or historical campaign data changed.
- Validation: 151 focused BACKTEST/shared-decision parity tests passed; Python compilation and diff whitespace checks passed.
- Known critical risks: BACKTEST market context still derives score/expectancy from deterministic candle geometry rather than the production runtime's AIBrain scoring context; historical spread is an explicitly labelled estimate when no historical quote data exists. This patch does not establish full PAPER-runtime scorer parity. LIVE verdict: NOT LIVE READY.
- Last audit date: 2026-09-20. PAPER verdict: unchanged. LIVE verdict: NOT LIVE READY.

# State Direction Shadow Evaluation — 2026-09-19

- Current version/phase: `adaptive_shadow_v2`, observational PAPER-only evaluation on `feature/state-direction-shadow-evaluation`; production state-direction resolution remains default false.
- Runtime maturity: the existing separate adaptive SQLite store now records deterministic state-direction decisions and outcomes without entering canonical campaign evidence. Actual ACCEPT/REJECT, side, geometry, order flow, reject reason, execution count, qualification, and campaign identity remain authoritative and unchanged.
- BACKTEST/PAPER/LIVE alignment: the observational hook is enabled only for PAPER through `ALPHAFORGE_ENABLE_STATE_DIRECTION_SHADOW_EVALUATION` (default false). REGIME_GUIDED semantics remain authoritative. BACKTEST and LIVE behavior are unchanged.
- Lifecycle coverage: shadow rows are written only after the actual decision is finalized. They do not create or alter lifecycle transitions, pending campaign labels, trade outcomes, or qualification samples.
- Execution realism/persistence: raw RR is derived from shadow entry/SL/TP, then the canonical execution-cost breakdown supplies effective RR and cost drag. The canonical burn-in forward evaluator now supplies TP/SL/timeout/ambiguity/MFE/MAE mechanics to both campaign and shadow callers. Mirrored geometry is diagnostic and never classified as structure-valid.
- Storage: default observational path is `data/runtime/alphaforge_adaptive_shadow.db`; campaign tables are refused. Added `state_direction_shadow_decisions` and `state_direction_shadow_outcomes` with deterministic upserts. No campaign schema or historical campaign database is migrated.
- Validation: 91 focused adaptive/MTF/shadow/cost/resolver tests passed; one broader relevant PAPER/execution/position-resolver/qualification pass passed 37 tests; targeted Python compilation and `git diff --check` passed. Ruff is not installed in the repository environment.
- Known critical risks: forward outcomes require an explicit offline resolver invocation and trustworthy closed-candle input; structure-valid opposite-side geometry is supported only when an evidenced structure geometry is supplied. No automatic tuning or promotion exists. No PAPER or LIVE campaign was launched.
- Last audit date: 2026-09-19. PAPER verdict: observational feature is reviewable but must remain disabled until an operator deliberately supplies the separate shadow path. Live readiness verdict: NOT LIVE READY.

# Phase9 detached worker attachment snapshot correction — 2026-09-17

- Current version/phase: proposed clean release `1709MAC05`, prospective Phase9 detached PAPER startup correction on `fix/burnin-worker-attachment-race`.
- Runtime maturity: each attachment polling iteration now uses one coherent campaign-worker liveness result and one `Popen.poll()` result for checks and classification. A live child with incomplete or temporarily uncertain attachment evidence remains waiting until attachment or timeout; only an observed subprocess exit code produces `WORKER_EXITED_BEFORE_ATTACHMENT`.
- BACKTEST/PAPER/LIVE alignment: trading decisions, thresholds, sizing, execution costs, reconciliation, and position management are unchanged. The fix is limited to Phase9 detached worker attachment orchestration and recovery-drill subprocess evidence.
- Lifecycle coverage: genuine pre-attachment exits still fail immediately; positive active-run mismatch still fails as `WORKER_IDENTITY_MISMATCH`; missing attach, heartbeat, runtime-instance, run-match, or worker-identity evidence remains fail-closed as `WORKER_ATTACHMENT_TIMEOUT` when no exit is proven.
- Persistence/execution realism: no schema, export, migration, or historical-row change. Phase9 no longer persists checks showing an alive/not-exited worker and then classifies the same polling observation as exited because of a second probe.
- Validation: 9 focused attachment regressions passed; 189 requested Phase9/process-liveness/Phase8 tests passed; 368 broader burn-in/runtime/control-center tests passed with 40 dependency deprecation warnings; the full suite passed 1,712 tests with 3 skips and 120 dependency deprecation warnings.
- Known critical risks: OS identity probing remains intentionally fail-closed and can withhold attachment if start-time or command ownership cannot be established. The existing substring command match remains unchanged because `alphaforge.burnin` matches the canonical `alphaforge.burnin_cli` command and was not the demonstrated cause. No real PAPER or LIVE campaign was launched.
- Evidence preservation: release `1709MAC04` and its failed campaign artifacts remain immutable historical failure evidence; no local historical campaign database was modified.
- Last audit date: 2026-09-17. PAPER verdict: safe for review and a fresh post-merge detached PAPER validation under `1709MAC05`; do not resume or rewrite `1709MAC04`. Live readiness verdict: NOT LIVE READY.

# PAPER executable-fill RR geometry correction — 2026-09-17

- Current version/phase: `paper_executable_rr_v1`, prospective PAPER execution-realism correction.
- Runtime maturity: the final PAPER acceptance gate now derives `expected_fill` from the same adverse-slippage function used by the simulator, recomputes `executable_raw_rr` from fill/SL/TP geometry, and subtracts only costs not already embedded in the entry fill. A 1.20 candidate RR is no longer sufficient when a tight stop makes executable RR fall below `MIN_EFFECTIVE_RR`.
- BACKTEST/PAPER/LIVE alignment: PAPER and runtime pre-submit evidence use executable geometry. The legacy generic effective-RR helper and the separate `order.py` BACKTEST decision surface remain unchanged; full canonicalization across every non-runtime consumer is a follow-up risk, not hidden by threshold changes.
- Lifecycle coverage: accepted/rejected lifecycle ordering is unchanged. Low executable geometry is persisted as `LOW_EFFECTIVE_RR` before `WAITING_ENTRY_ZONE` or execution. TP/SL resolution continues to compute gross R from actual simulated fill and fixed exit geometry.
- Persistence/execution realism: no schema migration or historical rewrite. Decision metrics and pending-position provenance add candidate RR, expected fill, executable raw RR, residual penalty, and fill-slippage evidence. Entry slippage embedded in fill is not charged again as an additive realized cost; modelled exit slippage remains charged.
- Correlation exposure: BTC and ETH families now share a direction-aware `CRYPTO_MAJOR` bucket. Same-direction positions count toward configured correlation limits; opposite-direction exposure does not. No correlation threshold was tuned, and the default limit of two correlated positions still permits one BTC/ETH pair.
- Validation: 134 affected runtime, portfolio, resolver, parity, and live-readiness tests passed. An isolated full suite passed 1,566 tests with 3 skips and 120 dependency deprecation warnings. Dedicated regressions cover the observed ETH 0.1724R geometry, tight/normal stops, LONG/SHORT symmetry, TP gross R, and BTC/ETH same-side rejection when the configured limit is one.
- Known critical risks: a fresh PAPER campaign is required to validate outcome distributions prospectively. The environment path for `MAX_CORRELATED_POSITIONS` remains reserved/not wired, so changing the default correlation limit requires a separate explicit policy/config patch. LIVE remains NOT READY.
- Last audit date: 2026-09-17. PAPER verdict: merge and start a fresh campaign; do not mutate or resume historical evidence. Live readiness verdict: NOT LIVE READY.

# Reject persistence canonical parity — 2026-09-16

- Current version/phase: `reject_parity_v1`, pre-long-PAPER burn-in evidence hardening.
- Runtime maturity: `rejects_persisted` is now the database-derived count of unique canonical persisted rejects for the attached campaign, restored on attach/start/restart and refreshed before heartbeat publication. Standalone non-burn-in runtimes count unique successfully persisted canonical reject IDs within the process.
- BACKTEST/PAPER/LIVE alignment: reject decisions, reasons, thresholds, and execution behavior are unchanged. PAPER heartbeat and execution metrics no longer expose persistence-attempt counts as canonical evidence.
- Lifecycle/execution coverage: rejected lifecycle rows, reviews, burn-in observations, and pending forward labels retain their existing idempotent persistence contracts. Qualification sample logic remains DB-backed and does not use heartbeat counters.
- Persistence/schema: no schema migration or historical rewrite. The stopped POSTRSLVRFX source DB and sidecars were hash-verified unchanged after copy-only forensic inspection.
- Known critical risks: this fixes the demonstrated counter contract but does not infer which two historical call paths replayed POSTRSLVRFX rejects; callbacks and in-memory diagnostic logs may still observe retries. LIVE remains NOT READY.
- Last audit date: 2026-09-16. PAPER verdict: safe to start a fresh post-merge long campaign; do not resume or rewrite historical campaigns. Live readiness verdict: NOT LIVE READY.

# Burn-in evidence identity and qualification cohort correction (2026-09-16)
- Current version/phase: prospective `dev` burn-in operations evidence-integrity correction; historical `data/campaign/1609t01.db` remained read-only and `camp_a955d6d821c775a4` was not resumed.
- Runtime maturity: preflight, campaign creation, and continuation start now fail closed when a release token is a canonical campaign, run, or aggregate identity. Qualification explicitly separates operational closures from complete, cost-valid closed outcomes.
- BACKTEST/PAPER/LIVE alignment: decision logic, MTF, RR, scoring, costs, fills, and order authorization are unchanged. The release namespace guard applies to PAPER burn-in operations; qualification consumers use the complete evidence cohort.
- Lifecycle coverage: `AMBIGUOUS_INTRABAR` remains CLOSED operationally, persisted with `evidence_complete=0`, and excluded from qualification sample, expectancy, LCB, harmful-accept, and concentration calculations.
- Persistence/execution realism: no schema, migration, export, or aggregate-hash change. Existing rows are neither rewritten nor deleted. Operational and qualification counts are exposed separately.
- Validation: 145 focused tests passed; 250 broader campaign/audit/resolver/runtime/dashboard tests passed; full suite 1,550 passed and 3 skipped.
- Known critical risks: historical invalid-release campaigns remain immutable and non-resumable. A fresh PAPER campaign with a valid release token is required for new qualification evidence. LIVE remains NOT READY.
- Last audit date: 2026-09-16. Live readiness verdict: NOT LIVE READY.

# AlphaForge Version

## Autonomous qualification harness (2026-09-15)
- SOAK release-gate follow-up: 30-second heartbeat/reconciliation/resolver and safety/resource sampling, public scans every 300 seconds, explicit feed-gap/recovery events, actual wall-clock duration, and RSS high-water, database/artifact, queue/backlog, SQLite-lock, and latency trend evidence. The completed isolated public six-hour SOAK passed the PAPER release gate.
- Current version/phase: isolated PAPER-only end-to-end qualification for the POST363 transient-provider recovery baseline.
- Runtime maturity: FAST deterministically exercises 15 provider, persistence, heartbeat, resolver, restart, and replay faults; SOAK schedules the same matrix across a configurable 6–24 hour run while the production public market scanner, clean reconciliation, and heartbeat paths continue probing. Every scenario uses a fresh database, campaign/run, runtime identity, and artifact directory.
- BACKTEST/PAPER/LIVE alignment: qualification executes production PAPER safety paths with live submission disabled. BACKTEST and LIVE behavior are unchanged; the harness cannot authorize exchange mutation.
- Lifecycle/execution coverage: unknown exchange state blocks new and in-flight PAPER execution, transient recovery requires a committed CLEAN reconciliation, terminal state is checked across all campaign lineage tables, and every harness worker has a persisted exit reason.
- Persistence/execution realism: machine and human reports link each fault to exact isolated SQLite evidence; reconciliation lock recovery, replay identity, reject parity, qualification readability, and export checksums are verified. No schema or migration change.
- Validation: initial focused relevant suite 325 passed; updated SOAK sampling/feed-gap regressions 12 passed; updated full suite in an isolated test checkout 1,535 passed and 3 skipped; fresh FAST qualification PASS. The final public SOAK at `/private/tmp/alphaforge-autonomous-qualification/alphaforge-qualification-z54y1qgr` passed after 21,602.472 wall-clock seconds: 720 safe samples, 15 scheduled fault scenarios, one explicitly recovered public feed gap, no unexplained exit, persistence gap, lineage drift, or resource-growth flag.
- Known critical risks: FAST is accelerated; public SOAK depends on external exchange availability, while its optional synthetic mode cannot prove that availability. Signed-account reconciliation remains deterministic and isolated from production credentials. A persistent database writer outage delays durable evidence. LIVE remains NOT READY.
- Last audit date: 2026-09-15. Live readiness verdict: NOT LIVE READY.

## POST363 transient provider outage recovery (2026-09-14)
- Current version/phase: prospective PAPER burn-in recovery correction; POST363 evidence remains unchanged and no campaign was restarted.
- Runtime maturity: known transient read-only transport failures block execution immediately while reconciliation and resolver probing continue for the configured 300-second grace. Auth/protocol failures pause immediately; unknown failures retain a three-attempt escalation.
- BACKTEST/PAPER/LIVE alignment: BACKTEST is unchanged. PAPER uses the new recovery gate and final execution recheck; LIVE order authorization and fail-closed behavior remain unchanged.
- Lifecycle/execution coverage: an in-flight PAPER decision is cancelled if the final execution boundary sees an outage. Active exposure stays unresolved until authenticated CLEAN reconciliation commits.
- Persistence/execution realism: existing reconciliation, recovery, campaign event, and continuation tables carry failure attempts and explicit recovery/terminal evidence. No schema or export migration is required.
- Known critical risks: an indefinitely unavailable SQLite writer cannot durably record new attempts; resolver attempts are retained in memory for the next successful writer transaction. No production worker or exchange validation was performed. LIVE remains NOT READY.
- Last audit date: 2026-09-14. Live readiness verdict: NOT LIVE READY.

# Adaptive Decision Calibration Engine foundation (2026-09-14)
- Current version/phase: `adaptive_shadow_v1`, offline Phase 1 foundation; no runtime threshold mutation.
- Runtime maturity: retrospective, in-sample shadow proposals only. Canonical accepted and rejected PAPER campaign outcomes share one cost-adjusted net-R framework. Unresolved gates and orphan outcomes remain diagnostic.
- BACKTEST/PAPER/LIVE alignment: shared decision code and thresholds are untouched; BACKTEST score units are explicitly excluded from numeric score proposals. PAPER/LIVE have no calibration call site.
- Lifecycle/execution coverage: closed accepted trades and complete forward rejects are linked to canonical decisions; open/no-fill/cancel/incomplete evidence is recorded but cannot justify loosening. Costs and hold/MAE/MFE context are retained when present. Per-trade drawdown attribution remains unavailable.
- Known critical risks: historical evidence is in-sample and non-causal; normal-approximation confidence bounds cannot prove independence or regime stability. Health must be explicitly evidenced; the CLI defaults to no loosening. MTF/execution gates have no numeric shadow override. Historical campaign databases are not migrated or backfilled.
- Last audit date: 2026-09-14. Live readiness verdict: NOT LIVE READY.

## Runtime reject decision identity correction (2026-09-13)
- CI #1601 compatibility follow-up (2026-09-14): callback uses a supplied canonical reject ID and retains the artifact writer's `<signal_id>:REJECTED` fallback for direct callers without one. No schema, lifecycle, qualification, or execution change. Full pytest: 1,498 passed, 3 skipped, 9 unrelated backtest trade-quality/threshold/parity failures.
- Current version/phase: prospective runtime reject identity handoff; 303 focused and adjacent regression tests pass.
- Runtime maturity: the final `order_decisions.decision_id` now uses the run-scoped `reject_decision_id` already carried by the review, canonical burn-in observation, pending label, and resolved outcome. Accepted decisions and reject criteria are unchanged.
- BACKTEST/PAPER/LIVE alignment: the shared final reject artifact accepts the runtime-provided identity in configured modes; standalone callers retain the prior default. No decision or execution gate changes.
- Lifecycle/execution coverage: `SIGNAL_CREATED -> SIGNAL_REJECTED` and execution-cost assumptions are unchanged.
- Persistence coverage: no schema or export-format change. Existing campaign DBs retain old split IDs; repair requires an offline, audited backfill after the campaign stops.
- Known critical risks: the read-only campaign audit found all 82 current labels marked non-attributable legacy shadows; historical split identities also remain. This patch cannot make those labels qualify. LIVE remains NOT READY.
- Last audit date: 2026-09-13. Live readiness verdict: NOT LIVE READY.

## Reconciliation SQLite contention correction (2026-09-13)
- Current version/phase: narrow reconciliation-persistence hardening; focused and relevant regression validation complete.
- Runtime maturity: one observed reconciliation result commits its findings, exchange event, and corresponding state snapshot together. A recovered SQLite writer lock keeps the reconciliation task and campaign running; exhausted contention blocks trading and queues explicit failure evidence for the next successful commit.
- BACKTEST/PAPER/LIVE alignment: the same persistence boundary and fail-closed gate apply to every runtime mode; decision, strategy, threshold, and execution semantics are unchanged.
- Lifecycle/execution coverage: lifecycle transitions and order execution are unchanged. Reconciliation health now gates decisions until persistence succeeds.
- Persistence coverage: additive nullable `cycle_id` plus unique index on exchange reconciliation events; existing rows and exports remain compatible. Four attempts within a 750 ms deadline use a fresh `BEGIN IMMEDIATE` transaction, a 50 ms SQLite lock wait, and 25/50/100 ms backoffs only for `SQLITE_BUSY` or `database is locked`.
- Known critical risks: a persistent database lock prevents durable failure evidence until a later successful commit; the runtime logs and retains an in-memory failure meanwhile. Other SQLite writers remain outside this patch. LIVE remains NOT READY.
- Last audit date: 2026-09-13. Live readiness verdict: NOT LIVE READY.

## Issue #359 PAPER lifecycle signal-state correction (2026-09-10)
- Current version: prospective signal-scoped runtime lifecycle identity and audited `ERROR` persistence correction; no schema or threshold change.
- Current phase: implementation and bounded regression validation on `feature/359-paper-lifecycle-signal-state`; POST358C02 remains immutable historical evidence.
- Runtime maturity: one accepted PAPER decision now retains one `signal_id` through create, wait, trigger, simulated order, pending-position persistence, and position-open evidence. Later signals for the same symbol start independent lifecycle chains.
- BACKTEST/PAPER/LIVE alignment: shared lifecycle validation remains enabled and real persistence failures remain fail-closed. No LIVE adapter, authorization, threshold, scoring, or exchange-mutation behavior changed.
- Lifecycle/execution coverage: previous state is authoritative per signal; symbol state remains reconciliation diagnostics only. `ERROR` is canonical/persistable only with signal, failure, prior-state, and attempted-state evidence; it is an incident rather than clean lifecycle progress and blocks PAPER classification, burn-in qualification, and live readiness. Accepted PAPER burn-in observations are recorded as `POSITION_OPENED` only after simulated execution and pending-position persistence succeed.
- Persistence/execution realism coverage: campaign PAPER exposure remains sourced from `burnin_pending_position_outcomes`; generic `orders`/`positions` may legitimately remain empty. No migration or historical rewrite is performed.
- Known critical risks: historical split-ID and failed-worker rows are not repaired. Fresh isolated PAPER evidence is required before any readiness conclusion. LIVE remains NOT READY.
- Last audit date: 2026-09-10. Live readiness verdict: NOT LIVE READY.

## Issue #357 PAPER acceptance normalization (2026-09-09)
- Current version: prospective PAPER scoring-unit and portfolio-evidence correction; no schema or acceptance-threshold change.
- Current phase: implementation and regression validation on `feature/357-paper-acceptance-normalization`; a fresh isolated PAPER campaign is required after merge.
- Runtime maturity: guided MTF keeps raw MA deltas for diagnostics and supplies separate bounded setup, momentum, regime-alignment, and volatility-fit qualities to AIBrain. PAPER portfolio evaluation receives an explicit $1,000 local ledger and conservative $10 candidate notional when candidate/account evidence is absent.
- BACKTEST/PAPER/LIVE alignment: scoring feature names retain the shared AIBrain contract; account defaults are applied only in PAPER. LIVE and LIVE_PRECHECK account evidence remains fail-closed and no order-authorization path changed.
- Lifecycle/execution coverage: score, effective-RR, portfolio-risk, and paper-order gates remain in their existing order. Accepted PAPER execution emits `ORDER_PLACED` exactly once after its simulated result, then `POSITION_OPENED`; missing PAPER defaults still produce `UNKNOWN_PORTFOLIO_RISK` and all reject gates remain enabled.
- Known critical risks: historical campaigns retain pre-fix evidence and are not migrated. The local PAPER ledger is intentionally static for this immediate blocker; lifecycle PnL accounting remains future work. LIVE remains NOT READY.
- Last audit date: 2026-09-09. Live readiness verdict: NOT LIVE READY.

## M0 canonical reject-label identity correction (2026-09-08)
- Current version: prospective canonical reject/pending-label/outcome identity enforcement; no schema, strategy, scoring, or threshold change.
- Current phase: isolated M0 correctness validation on `dev` including PR #355.
- Runtime maturity: PAPER reject labels are created only after an explicit canonical `REJECTED` observation exists in the same campaign/run; canonical IDs are internally namespaced, repeated scoring/setup rejects remain idempotent across retries and restart, and retries retain one pending label and one explicitly ownership-linked immutable outcome identity.
- BACKTEST/PAPER/LIVE alignment: decision logic and execution authorization are unchanged. PAPER burn-in persistence is stricter; LIVE order authorization remains unchanged and fail-closed.
- Lifecycle coverage: canonical `SIGNAL_REJECTED` evidence precedes pending-label creation. Diagnostic/orphan observations remain auditable but cannot qualify.
- Execution realism coverage: existing geometry, horizon, candle, and cost semantics are unchanged; legitimately incomplete geometry remains label-ineligible.
- Known critical risks: historical campaign data is not repaired or migrated and must not be treated as corrected. Fresh isolated PAPER evidence is required before any readiness conclusion.
- Last audit date: 2026-09-08. Live readiness verdict: NOT LIVE READY.

## M0 scoring-context wiring correction (2026-09-08)
- Current version: isolated runtime scoring-context wiring and observability patch; no schema, strategy, or threshold change.
- Current phase: focused PAPER/LIVE parity, AIBrain, MTF, persistence, execution, and lifecycle regression validation complete.
- Runtime maturity: canonical numeric guided-MTF/market features now reach AIBrain; existing SQL expectancy statistics populate its consumed setup/regime/symbol context, using the least-supported scope for confidence and retaining zero samples when any required history scope is absent. New executed trades update expectancy once.
- BACKTEST/PAPER/LIVE alignment: one shared context builder is used by the real decision path without a PAPER-only optimistic path or LIVE authorization bypass.
- Lifecycle/persistence/execution impact: lifecycle order, table/CSV schemas, execution gates, and order submission behavior are unchanged; scoring diagnostics now distinguish incomplete inputs from genuine neutral values, and below-threshold scores expose `low_score`.
- Known critical risks: current MTF evidence has no universal numeric volatility-fit or regime-alignment representation, so those inputs remain explicitly incomplete when no canonical numeric value is present. Pre-patch expectancy rows may retain duplicate-writer distortion and are not repaired automatically. Fresh isolated PAPER evidence is required before any readiness conclusion.
- Last audit date: 2026-09-08. Live readiness verdict: NOT LIVE READY.

## PR #344 M0 blocker correction (2026-09-06)
- Current version: dev reject-label canonical attribution and bounded watchdog backlog escalation patch; no schema or strategy change.
- Current phase: focused PAPER operational-integrity tests pass; fresh PAPER runtime validation remains required.
- Runtime maturity: diagnostic shadow labels remain auditable but cannot enter canonical qualification identity; transient overdue-backlog growth is `DEGRADED`, while three consecutive growth samples fail closed.
- BACKTEST/PAPER/LIVE alignment: trading, MTF, score, RR, and execution decisions are unchanged; watchdog changes affect PAPER campaign supervision only.
- Lifecycle/persistence/execution impact: healthy attached RUNNING campaigns are not terminalized by backlog growth alone; genuine resolver/backlog degradation and all prior hard-safety conditions retain `RECOVERY_REQUIRED` protection. JSON health evidence is additive; schema/export shapes are unchanged.
- Known critical risks: historical false recovery transitions are not rewritten; fresh PAPER evidence is required. No additional confirmed #344 M0 code blocker remains in scope.
- Last audit date: 2026-09-06. Live readiness verdict: NOT LIVE READY.

## Canonical final-reject causality (2026-09-05)
- Current version: dev canonical rejection-causality persistence patch; no schema migration or threshold change.
- Current phase: focused PAPER evidence integrity validation complete.
- Runtime maturity: canonical rejected burn-in observations now carry `primary_reject_reason` and ordered `reject_reasons`; accepted observations carry neither.
- BACKTEST/PAPER/LIVE alignment: decision order, MTF confirmation, score/RR formulas, thresholds, and execution behavior are unchanged.
- Lifecycle/persistence/execution impact: lifecycle and table/CSV schemas are unchanged; only rejected `metrics_json` gains final gate causality while nested MTF diagnostics remain MTF-specific.
- Known critical risks: historical POST351 rows are immutable and remain unexplained; fresh observations are required for corrected evidence.
- Last audit date: 2026-09-05. Live readiness verdict: NOT LIVE READY.

## Burn-in qualification evidence accounting correction (2026-09-05)
- Current version: dev fail-closed canonical reject-attribution, pending/ambiguous accounting, aggregate-hash, dashboard-counter, and qualification-freshness correction; no schema migration or strategy tuning.
- Current phase: Phase 7/8 PAPER burn-in evidence integrity repair.
- Runtime maturity: diagnostic PAPER only; scan evidence remains auditable while qualification uses canonical decision identity.
- BACKTEST/PAPER/LIVE alignment: decision/reject thresholds and lifecycle gates are unchanged; PAPER reconciliation remains fail-closed.
- Lifecycle coverage: unchanged; rejected and infrastructure-blocked decisions remain persisted.
- Execution realism coverage: existing R-normalized cost model retained and dimensionally regression-tested.
- Known critical risks: existing campaign snapshots must be refreshed before their blockers are current; historical diagnostic labels remain non-qualifying; legacy fallback requires proven identity-less observations; a clean full-duration burn-in is still required.
- Last audit date: 2026-09-05. Live readiness verdict: NOT LIVE READY.

## Guided MTF setup identity diagnostic correction (2026-09-05)
- Current version: dev setup-direction sensitivity and setup-opportunity idempotency patch; no schema migration.
- Current phase: focused validation complete; a fresh PAPER campaign is required because the setup threshold changes strategy/config identity.
- Runtime maturity: the setup layer defaults to `0.0003`; regime and execution layers remain `0.0005`. One closed 15m setup identity may receive later 1m observations but may produce at most one accepted entry.
- BACKTEST/PAPER/LIVE alignment: PAPER guided-MTF semantics are corrected without changing scoring, effective-RR, or execution gates; no LIVE-readiness claim is made.
- Lifecycle/persistence/execution impact: `NO_SETUP`, `INVALID`, and `OVEREXTENDED` persist once per symbol/setup-timeframe/closed-candle opportunity. Canonical counters remain deduplicated; no table or export schema changes.
- Known critical risks: campaign `camp_53a9afa55070606a` remains historical evidence under the old threshold and must not be mixed with post-patch evidence.
- Last audit date: 2026-09-05. Live readiness verdict: NOT LIVE READY.

## PAPER preflight dotenv bootstrap correction (2026-09-05)
- Current version: dev burn-in executable dotenv-scope regression fix; no schema, strategy, endpoint, or campaign change.
- Current phase: operator reruns PAPER preflight after restoring a clean `dev` worktree; no campaign action is authorized by this patch.
- Runtime maturity: `burnin_ops` now gives config audit and signed read-only provider construction the same canonical repository `.env` view, with process values taking precedence and in-process environment restoration.
- BACKTEST/PAPER/LIVE alignment: PAPER remains mandatory for burn-in preflight; LIVE trading and LIVE orders remain disabled and the signed reconciliation gate remains fail-closed.
- Lifecycle/persistence/execution impact: none; no database rows, schema, lifecycle transitions, strategy thresholds, execution costs, or Binance endpoint resolution changed.
- Known critical risks: preflight still blocks a dirty `dev` worktree and still requires genuine COMPLETE authenticated exchange evidence.
- Last audit date: 2026-09-05. Live readiness verdict: NOT LIVE READY.

## 0409T02 audit semantics and test determinism (2026-09-05)
- Current version: dev burn-in attribution, recovery-duration, environment precedence, and lifecycle persistence correction; no schema migration or threshold tuning.
- Current phase: review only; no campaign start, commit, PR, or merge has been performed.
- Runtime maturity: executable dotenv bootstrap preserves process precedence, library loaders are deterministic and in-process helpers do not leak file-backed values; lifecycle export results are scoped to supplied events; legacy scanner shadow outcomes are diagnostic and non-attributable to MTF reject quality.
- BACKTEST/PAPER/LIVE alignment: PAPER/LIVE exchange-state gates remain fail-closed; successful complete reconciliation may clear only stale exchange-state errors. LIVE credential and authorization checks remain strict.
- Lifecycle/persistence/execution impact: existing rows and campaign databases are unchanged; healthy observed duration is capped by canonical operational/heartbeat intervals; execution-cost evidence is explicitly normalized to R.
- Known critical risks: historical 0409T02 qualification snapshots retain their original invalid attribution and must not be reused as corrected reject-quality evidence; the local `.env` intentionally differs from default 1m threshold identity.
- Last audit date: 2026-09-05. Live readiness verdict: NOT LIVE READY.

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
## Post-343 reject-evidence semantic isolation (2026-09-05)

- **Current version/phase:** dev post-343 evidence correction; no schema migration.
- **Runtime maturity:** PAPER guided-null rejects fail closed for canonical geometry while legacy scanner geometry remains diagnostic-only.
- **BACKTEST/PAPER/LIVE alignment:** strategy and thresholds unchanged; attribution correction applies at shared reject persistence/qualification boundaries.
- **Lifecycle coverage:** unchanged; canonical reject reason and lifecycle chain are preserved.
- **Execution realism:** real complete guided candidates remain attributable; absent guided candidates never inherit scanner RR/geometry.
- **Known critical risks:** affected historical/current campaign evidence is invalid for guided reject attribution and old snapshots are not rewritten.
- **Last audit:** 2026-09-05.
- **Live readiness:** **NOT READY** — a fresh release ID and campaign ID with new PAPER evidence are required.
