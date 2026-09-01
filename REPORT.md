# Burn-in evidence and continuation integrity — 2026-08-31

## Need and root cause

Campaign `camp_119394d8c198138d` persisted 519 canonical rejected decisions and 29 additional `incomplete_reject_geometry_*` audit observations. Qualification counted every observation row as a decision, producing 548 samples and 548 rejects. The root cause was the absence of a canonical-decision discriminator in denominator queries.

## Minimal correction

New observations persist `metrics_json.observation_kind`; normal runtime observations default to `CANONICAL_DECISION`, while incomplete-geometry audit rows explicitly use `DIAGNOSTIC`. A centralized SQL predicate consumes that discriminator and recognizes immutable pre-discriminator incomplete-geometry rows through a compatibility fallback. Run counter reconciliation, campaign aggregation, and Phase 7 qualification use the same predicate. Aggregate materialization still copies all 548 rows, preserving audit/export evidence.

Campaign elapsed time is now recomputed from `burnin_campaign_runs`: closed eligible continuations contribute only `started_at -> ended_at`, the current RUNNING continuation contributes only `started_at -> now`, gaps contribute nothing, and FAILED continuations contribute only if an operational attachment event proves legitimate runtime. Derived per-run and campaign duration fields are synchronized without modifying immutable timestamps. Aggregation, qualification, completion, status, and export paths consume that contract.

All process existence probes now route through one helper. POSIX retains `os.kill(pid, 0)` plus `/proc` command/creation identity where available; Windows exclusively uses `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`, `GetExitCodeProcess`, `GetProcessTimes`, and `CloseHandle`. No liveness path calls Windows `os.kill`. Worker ownership requires command identity on inspectable POSIX systems and creation-time correlation on Windows.

The detached CLI reads `release_id` from the persisted campaign, overrides any conflicting parent environment for the child, and holds campaign/run state at STARTING. Only the existing runtime attachment identity transition may promote the successor to RUNNING.

## Lifecycle, persistence, compatibility, and risks

Incomplete geometry remains fail-closed at SIGNAL_REJECTED, no side/stop/target is fabricated, and pending/resolved reject labels are unchanged. No table, Alembic, or CSV schema changes are required; historical evidence rows and timestamps are not rewritten. BACKTEST, PAPER decision logic, strategy thresholds, reject rates, order authorization, and LIVE behavior are unchanged. Windows cannot obtain command line through the limited query handle, so persisted launch time and process creation time form the non-destructive ownership check there.

## Validation and recommendation

Regression coverage retains the exact 519 canonical plus 29 diagnostic case, covers explicit diagnostic classification, reproduces 9,790 active seconds plus a 44,000-second pause, an unattached failed startup, and 60 resumed seconds as 9,850 seconds, verifies query-only Windows liveness/dead/recycled PID handling, and verifies detached resume of release `3108T03` without parent release state. Recommend review and a fresh PAPER qualification snapshot; do not infer LIVE readiness.

---

# PR #336 MTF heartbeat persistence follow-up — 2026-08-31

## Need and root cause

The PAPER runtime emitted all MTF counters to `save_runtime_heartbeat`, but `_safe_payload` applied a separate permitted-key allowlist that omitted them. Consequently, in-memory observability was correct while `runtime_heartbeats.payload_json` silently discarded the MTF counter family.

## Minimal correction and test

`src/alphaforge/runtime_heartbeat.py` now permits the nine existing MTF counter keys. `tests/test_runtime_heartbeat.py` persists representative campaign-shaped counts through the public heartbeat writer, reads the physical SQLite `payload_json`, parses it, and proves every counter survives while an unallowlisted credential remains excluded. No database schema, migration, export, MTF threshold, alignment reason, execution rule, trading gate, or LIVE behavior changes.

Neutral execution remains rejected as `MTF_EXECUTION_NOT_CONFIRMED` before AIBrain exactly as implemented in the core patch. The possible setup-layer distinction between unavailable evidence and available evidence without a valid setup remains a non-blocking follow-up risk; this patch does not alter or weaken setup rejection behavior.

## Validation and recommendation

Focused heartbeat and MTF coverage passed 23 tests. The normal literal `pytest -q` command could not collect because the declared Alembic distribution is absent from this container. The complete behavioral suite excluding only the three Alembic-dependent modules passed 1,316 tests with 6 skipped. Compileall, diff validation, and the CI offline backtest/output check passed. Update PR #336 for review against `dev`; do not merge automatically and do not infer LIVE readiness.

---

# PAPER MTF execution-evidence classification correction — 2026-08-30

## Need and root cause

Campaign `camp_9afc71c6a419749c` demonstrated that the execution builder made `evidence_status=COMPLETE` conditional on a directional trigger. Consequently, five valid closed 1m candles plus canonical spread, slippage, latency, and liquidity evidence were called unavailable whenever their 2/5 MA delta was neutral. The alignment evaluator then persisted `MTF_EXECUTION_UNAVAILABLE` alongside the accurate `MTF_EXECUTION_NOT_CONFIRMED`, making the first reason semantically misleading.

## Files and exact behavior change

`src/alphaforge/multi_timeframe.py` now computes execution completeness from required closed candles and finite, non-negative canonical market evidence independently of the trigger. A complete neutral observation remains triggerless and therefore fails alignment with `MTF_EXECUTION_NOT_CONFIRMED`; genuinely absent/invalid candles or market fields remain incomplete and fail with `MTF_EXECUTION_UNAVAILABLE`. Direction mismatches, stale checks, closed-candle filtering, and reason ordering are unchanged. The shared 0.0005 behavior is retained through explicit per-timeframe defaults rather than tuned to campaign outcomes, and each context records its observed absolute MA delta and applied threshold.

`src/alphaforge/runtime.py` adds `mtf_execution_not_confirmed` independently from `mtf_execution_missing` and exposes the complete MTF counter family in persisted heartbeat payloads. Rejected burn-in observations already persist the full MTF structure, so neutral strength/threshold and all alignment reasons remain auditable without a schema change. `tests/test_multi_timeframe.py` adds regression coverage for neutral complete evidence, each missing execution field, missing candles, and the real PAPER pre-AIBrain reject boundary; existing tests retain aligned LONG/SHORT, both mismatch classes, stale/future evidence, unsupported provenance, and side binding coverage.

## Lifecycle, persistence, compatibility, and safety

The gate remains fail-closed: neutral execution never aligns, never reaches AIBrain, and never increases accepted decisions. SIGNAL_CREATED -> SIGNAL_REJECTED behavior is unchanged. No SQLite table, Alembic revision, CSV export, runtime threshold, effective-RR, spread, slippage, liquidity, expectancy, portfolio-risk, order authorization, or LIVE behavior changed. Heartbeat JSON gains one backward-compatible counter and exposes existing counters; historical campaign rows, including T04 evidence, are not rewritten. No migration is required.

## Tests, remaining risks, and recommendation

Focused MTF/runtime validation passed 17 tests. The relevant full behavioral suite passed 1,315 tests with 6 skipped; its only two failures were environment import failures in lifecycle migration tests because the declared Alembic distribution is absent. The literal full suite also stopped during collection for the same missing dependency. `python -m compileall -q src tests backtest_order.py` and `git diff --check` passed. The MA classifier is intentionally still a simple, identical 0.0005 default across all three timeframes; the implementation can now vary thresholds by timeframe after evidence-backed calibration, but this patch supplies no such tuning. Public Binance availability, the missing local Alembic dependency, and fresh campaign qualification remain operational risks. Open for review against `dev`; do not merge automatically and do not infer LIVE readiness.

---

# PAPER multi-timeframe decision surgery — 2026-08-30

## PR #334 targeted correction
The first implementation passed raw scanner fields into the MTF execution layer even though the canonical execution builder had already normalized `market_data_latency_ms` and modelled expected slippage. It also aligned the three MTF layers without binding their direction to the enriched geometry side, and could call the Binance provider for another source. Runtime now passes the canonical execution context explicitly, rejects aligned-side/geometry-side disagreement as `MTF_DIRECTION_MISMATCH`, and emits explicit incomplete provenance for unsupported providers without fetching Binance candles. Regression coverage uses a Binance-shaped row with measured market-data latency and absent raw slippage, proves canonical normalization yields COMPLETE MTF execution evidence, covers both matching directions and both mismatch directions with persisted concrete reasons, and proves cross-provider substitution is impossible. No schema, identity, cache, lookahead, downstream gate, or LIVE behavior changed.

## Need and root cause
The canonical flow selected exchange candidates and sent one 1m-derived geometry record directly through AIBrain and the downstream risk gates. Campaign `intervals` were identity/reporting metadata; they did not fetch or analyze that timeframe. Thus a 1h campaign could persist 1m observations without any 1h structure participating.

## Architecture and behavior
After canonical symbol selection, PAPER now builds 1h regime, 15m structural setup, and 1m timing contexts from Binance closed klines as of one decision timestamp. The alignment evaluator rejects missing, incomplete, stale, future, contradictory, no-setup, and unconfirmed execution evidence. Alignment only continues into the existing runtime risk, AIBrain structural/expectancy, effective-RR, portfolio/correlation, and execution paths; it never accepts by itself. Provider failures produce incomplete contexts and rejection.

## Files, persistence, lifecycle, and schema
`multi_timeframe.py` owns closed-candle filtering, context builders, deterministic alignment, and provider cache. Runtime/config/campaign/operations files wire the gate, three explicit settings, identity drift, metrics, health, and JSON provenance. Canonical reject reasons were extended additively. Burn-in observation `metrics_json` and pending-label `source_provenance_json` carry MTF evidence alongside existing evidence. No table, ORM, Alembic, CSV, or SQL ownership changed. SIGNAL_CREATED -> SIGNAL_REJECTED ordering is retained.

## Lookahead, load, compatibility, and migration
Rows are usable only when provider `closeTime <= decision timestamp`; future/partial rows are filtered and the evaluator independently rejects future timestamps. Cache identity is `(symbol, timeframe, closed-boundary, provider URL)` and refreshes only on a new boundary; calls occur only for selected candidates. Historical campaigns/rows remain untouched. Stop the old worker, archive its campaign as historical, deploy, set the three variables, run preflight, create a new campaign, and launch it. The old alias maps only to execution timeframe.

## Tests, risks, and recommendation
Tests cover LONG/SHORT alignment, both mismatch classes, every missing layer, no setup, stale evidence, and partial/future candle exclusion, plus campaign/config/runtime regressions. Remaining risk is classifier calibration and public-provider availability; rejects during outage are intentional. Merge only with passing CI and collect a fresh PAPER qualification campaign. Do not recommend LIVE readiness.

## PR #328 CI wiring-contract follow-up

The remaining CI failure was metadata-only: the PAPER decision timeframe was behaviorally consumed but its `Learning` category fell back to generic `load_config_from_env` metadata. The registry now identifies `_scan_binance` as the specific production consumer and points to a behavioral test that changes the environment value, proves unsupported `5m` performs no provider request or silent `1m` fallback, proves supported `1m` reaches scanner candidates, and verifies reject-evaluation identity/hash changes. No runtime, lifecycle, persistence, fee, latency, geometry, resolver-health, schema, or LIVE mutation semantics changed.

## PR #328 follow-up: timeframe semantics and resolver health

A campaign interval such as `1h` is now explicitly a campaign reporting/universe interval, distinct from the canonical PAPER decision/setup timeframe and reject-forward evaluation timeframe. Identity persists all three semantics plus horizon bars, pending-label provenance repeats them, and the Binance scanner fails closed if configured away from its currently supported closed-`1m` geometry. Thus a `1h` campaign can intentionally evaluate `1m` decisions for 240 bars, but that four-hour horizon is no longer implicit and any semantic change alters identity. No persistence schema or export migration is required.

Direct operations tests now establish that immature queue growth remains healthy, overdue growth emits `RESOLVER_BACKLOG_GROWTH`, stale claims emit `STALE_RESOLVING_CLAIMS`, and resolver/provider failures remain unhealthy. The fee setting is explicitly total round-trip entry-plus-exit basis points and `_phase7_costs_from_execution_ctx` applies it once, avoiding undercount or double count. Existing strict qualification, latency, SHORT geometry, historical-campaign immutability, and disabled LIVE mutation behavior remain unchanged. Start a fresh PAPER campaign after merge.

# PAPER reject-forward evidence regression surgery — 2026-08-20

## Need and root cause

Historical campaign `camp_5004b6d9236213b6` remains untouched. Its 409 pending labels lacked fee and latency costs because execution contexts had no fee assumption and Binance explicitly persisted latency as unavailable. Its 492 incomplete geometry observations lacked stop and target because the raw scanner fabricated LONG and enrichment discarded valid canonical SHORT geometry. Health also treated normal immature queue growth as resolver degradation.

## Files and runtime behavior

`execution.py`, runtime/config loading, the registry, and campaign identity now carry a non-negative explicit PAPER fee with `CONFIGURED_PAPER_ASSUMPTION` provenance; missing/invalid fee remains null and attached PAPER burn-in fails closed. The Binance scanner measures the conservative book-ticker request RTT with `perf_counter`; clock/provider failure remains `None`/`UNAVAILABLE`. Raw Binance candidates no longer claim direction, while selected two-closed-candle geometry authoritatively supplies side, entry, SL, TP, RR, and setup type without changing symbol/source identity. Resolver health now alarms on growth of overdue labels, stale resolving claims, and resolver/provider failures rather than immature pending growth.

## Lifecycle, persistence, schema, compatibility, and migration

Reject lifecycle ordering and strict critical-cost qualification are unchanged. Complete LONG and SHORT rejects remain eligible for pending-label persistence; genuine geometry/provider failure remains explicit incomplete evidence and cannot execute. No table or CSV schema changed and no migration is required. Campaign execution-cost hashes now include fee basis points and percentage, so operators must create a fresh campaign; the historical campaign must not be resumed or rewritten. LIVE order authorization and mutation paths are unchanged and disabled.

## Tests, risks, limitations, and recommendation

Regressions cover configured/missing fees, cost identity drift, measured/unavailable latency, canonical SHORT ownership, provider-failure incompleteness, production reject persistence, and mature resolver health. Public RTT includes network and Binance response time by design. Clock/provider outages reduce evidence completeness rather than producing zero. Recommend merge only after the relevant suites pass, then start a new PAPER campaign; do not recommend LIVE readiness.

---


## Need and final root cause

PR #322 originally mislabeled Binance 24-hour extrema as execution geometry; PR #323 correctly replaced that with the accepted backtest path's two-candle setup calculation. The remaining P1 was architectural: geometry HTTP calls still ran inside the raw Binance scanner, before `RuntimeOrchestrator._scan_once` invoked canonical `select_symbols`. With up to 30 raw Binance candidates, the old placement permitted up to 30 sequential `/fapi/v1/klines` calls, wasted evidence work on unselected symbols, and could resemble a PR #321 runtime stall.

The accepted geometry source remains `backtest_order._build_market_ctx`, extracted as `build_breakout_geometry`: side follows current versus previous close, stop spans both setup candles, and target uses the existing bounded breakout/body-strength raw RR. No 24-hour price extreme is used as stop or target.

## Final architecture and files changed

`scan_exchange_markets` now returns the complete raw provider candidate universe without kline geometry. `RuntimeOrchestrator._scan_once` runs the unchanged `select_symbols(..., include_rejected=True)` and unchanged tradable/rank slice first. Only that already-bounded list is passed to `selected_candidate_enricher`; enriched inputs are then consumed by the existing `_process_symbol`, `_build_signal`, AIBrain, reject/accept, and persistence flow. Candidate count and `(symbol, source_exchange)` identity are guarded against enricher mutation.

`enrich_selected_market_geometry` schedules at most one explicit-timeout `to_thread` request for each unique selected Binance 1m symbol and awaits the bounded set with `gather`; selected non-Binance candidates cause no Binance request, duplicates share one result, and no asyncio task survives completion. Therefore the final bound is `geometry requests per scan <= unique selected Binance symbols <= selected Binance symbols <= max_symbols_per_scan`. The default maximum is five. Expected network/JSON/unavailable-data errors return absent geometry; programmer/contract errors propagate through the existing market-loop failure path. Scan progress time is recorded only after enrichment completes, preserving PR #321 stale-scan meaning.

Files changed by this follow-up are `src/alphaforge/exchange_market_scanner.py`, `src/alphaforge/runtime.py`, `tests/test_exchange_market_scanner.py`, `tests/test_issue322_reject_geometry.py`, `CHANGELOG.md`, `REPORT.md`, and `VERSION.md`. The shared `src/alphaforge/signal_geometry.py` and `backtest_order.py` reuse remain unchanged.

## Behavior, parity, persistence, and safety

The Binance raw scanner still emits LONG candidates. Geometry is applied only when its calculated side matches the selected candidate, so helper-level SHORT correctness remains covered without claiming production scanner SHORT support. Missing, incomplete, malformed, timed-out, or direction-mismatched setup candles do not fabricate entry, stop, target, or RR. Reject decisions remain rejected, exact `stop`/`target` missing evidence remains auditable as `INCOMPLETE_REJECT_GEOMETRY`, and ineligible rows do not enter `burnin_pending_reject_labels`.

For identical candles, backtest and PAPER still consume the same shared calculation. Phase-B regression proves legacy SL, TP, raw RR, SignalAgent `raw_rr`, and QualityAgent stop width are unchanged, `rr_difference` is zero, and legacy/shadow parity is `MATCH`. The real production-chain regression remains mocked Binance market payload -> raw scanner -> canonical selection -> bounded enrichment -> `_scan_once` -> AIBrain early reject -> `_persist_reject` -> eligible pending label, with no manual SL/TP injection.

No schema, migration, export, threshold, reject gate, reconciliation state, risk state, authorization, order, or position behavior changed. Expected provider failure is observational and fail-closed. Unexpected enrichment code failure propagates and does not publish false scan progress. Canonical PAPER database identity, worker supervision, campaign terminal behavior, and historical evidence identity remain unchanged.

## Tests, risks, and recommendation

Regressions cover 30 raw Binance candidates with limits five and two, full-universe canonical selection, exact selected-symbol requests, zero-selection/zero-request behavior, mixed providers, duplicate protection, timeout/incomplete evidence, programmer-error propagation, production-chain eligibility, idempotency, no execution mutation, helper SHORT geometry, and Phase-B parity. Focused scanner/geometry/Phase-B/runtime coverage passed 79 tests. The complete local suite reached 1,247 passed and 6 skipped; its only four failures were environment import failures because the declared Alembic distribution is absent (`alembic.config`/`alembic.command`). No behavioral test failed. `python -m compileall -q src tests backtest_order.py` and `git diff --check` passed.

The remaining operational limitation is one bounded public kline request per unique selected Binance symbol; concurrent blocking threads cannot be force-stopped after coroutine cancellation, but every HTTP call retains the explicit provider timeout and the awaited asyncio task set is bounded to processable symbols. Preserve `camp_8a577772ded0bdf2` as immutable pre-fix evidence and start a fresh PAPER campaign after merge. LIVE remains NOT READY. Recommend GitHub CI and re-review before merge.

---

# PAPER burn-in canonical persistence and supervision surgery — 2026-08-17

## Need and root cause

The runtime builder created an engine and session factory from the environment, then campaign code replaced only `runtime.persistence_engine`. AIBrain and lifecycle/reject closures therefore retained a different database. Separately, the market loop swallowed fatal exceptions and campaign maintenance heartbeats allowed a dead scanner to appear healthy.

## Files and behavior changed

`runtime.py` accepts an injected engine/session factory, uses them for every persistence consumer, and propagates market-loop failures with persisted failure state. `burnin_campaign.py` builds the production runtime with the campaign engine, compares canonical paths before attachment, and treats unexpected runtime exit as a campaign failure rather than leaving resolver/maintenance alive. Worker launchers propagate the canonical database path. `persistence.py` rolls back before its compatibility retry and raises target plus original/fallback SQL evidence. `burnin_ops.py` requires fresh runtime-owned heartbeat and scan evidence and exposes scanner/decision counters and timestamps. Regression tests cover canonical lifecycle/reject/AIBrain bindings and original SQL failure evidence.

## Lifecycle, persistence, export, and schema impact

Lifecycle ordering and idempotent upsert keys are unchanged. Failed SQL is no longer converted to `None`; callers receive the causal exception after a correct rollback. All attached PAPER evidence uses one existing campaign schema. There is no schema or CSV/export format change and no migration is required. Append-only campaign failure diagnostics include expected and observed canonical paths.

## Risks and limitations

Database path comparison is filesystem-canonical and deliberately fail-closed. Historical evidence already split across databases is not automatically merged because provenance cannot be safely inferred. Scan-stall health uses the configured heartbeat-age bound as startup/advance grace. Reconciliation, recovery, LIVE guards, decision thresholds, and execution modeling are unchanged.

## PR #321 CI follow-up

GitHub reported 1 failed, 1,231 passed, and 3 skipped rather than the previously claimed green 1,232-pass run. The failing completion test exposed a scheduling-sensitive production inefficiency: `_maintenance_tick` committed a valid `COMPLETED` transition and then synchronously entered `_qualify_if_due` through the same `asyncio.to_thread` call. That post-terminal qualification cannot change the loop exit decision, but it could outlive the caller timeout under CI load. Maintenance now skips qualification after completion or an already terminal campaign, while active campaigns retain periodic qualification.

`FIRST_COMPLETED` remains necessary for zombie-runtime detection, but every normally completed child is now classified against persisted campaign state. Runtime normal exit during `STARTING`/`RUNNING` fails the continuation; resolver or maintenance normal exit during those states also fails rather than cancelling a healthy runtime silently. Normal supervisor completion is accepted only after a valid terminal campaign transition. Runtime failure cancels and awaits both siblings. New deterministic tests cover terminal maintenance, active maintenance, resolver normal exit, runtime normal exit with sibling cancellation, and canonical database mismatch diagnostics.

## Runtime heartbeat lineage follow-up

Campaign health previously selected the newest global PAPER heartbeat, allowing a fresh unrelated runtime to mask a stale or absent attached runtime. Health now resolves the current `PHASE8_CAMPAIGN_ATTACHED` event for the campaign `active_run_id`, extracts its `runtime_instance_id`, and queries only PAPER heartbeats with that exact identity. Missing attachment identity is `RUNTIME_ATTACHMENT_IDENTITY_MISSING`; a known identity without matching heartbeat is `RUNTIME_HEARTBEAT_MISSING` and an active campaign also remains `RUNTIME_HEARTBEAT_STALE`. No global fallback exists, unrelated counters/timestamps remain excluded, and no schema migration is needed. Regression tests cover stale-target/fresh-unrelated and missing-attachment/fresh-global cases.

## Tests and push recommendation

The Phase 9 operations suite passed after adding lineage regressions. The complete behavioral suite excluding Alembic passed 1,231 tests with 6 skips, and the GitHub-equivalent offline backtest passed. The literal full command reached 1,232 passed and 6 skipped but its four Alembic checks could not import the declared Alembic dependency; installation was blocked by the environment proxy (HTTP 403). CI installs declared dependencies and remains the merge gate for those four migration checks. Do not infer LIVE readiness.

---

## Issue #322 — PAPER early-reject geometry surgery (2026-08-17)

### Need and confirmed root cause

The production path is `scan_exchange_markets` -> `select_symbols` -> `RuntimeOrchestrator._process_symbol` -> `_build_signal` -> `AIBrain.before_real_order` -> `score_signal` -> `choose_order_plan`. A score or post-cost expectancy failure returns `OrderPlan(decision="REJECTED", reason="Score below threshold or negative expectancy.")`; `_process_symbol` then enters its first post-score reject branch and calls `_persist_reject`, `_canonical_reject_payload`, `_persist_pending_reject`, and `persist_pending_reject_label`. The label status reader subsequently classifies the incomplete observation.

PR #317 already made `_build_signal` and that reject payload preserve `sl`/`tp`, and the accepted path reads the same fields. Binance's real scanner emitted entry, side, timeframe, and volatility context but no setup geometry, so all 590 rejects correctly failed closed as `INCOMPLETE_REJECT_GEOMETRY`. The original PR #322 analysis incorrectly treated unrelated ticker extrema as the missing setup geometry; PR #323 supersedes that conclusion.

### Minimal behavior change and canonical source

Superseded by PR #323: the original ticker-extrema mapping was removed. The scanner candidate remains the single input to both `_build_signal`/normal accepted execution and early-reject evidence, but geometry now comes only from the shared closed-timeframe setup builder. Missing setup evidence produces no geometry; `None` remains distinct from zero, and the resolver records exact missing fields and refuses label eligibility.

No score, expectancy, risk, sizing, reconciliation, authorization, lifecycle, or portfolio gate changed. The rejected plan remains rejected and returns before `_execute`; tests assert no order, PAPER position, execution metric, or LIVE adapter mutation. Later post-score rejects benefit from the same source payload, while deliberately pre-signal runtime-control/risk rejects were not broadened merely to increase the denominator.

### Files, persistence, compatibility, and tests

`src/alphaforge/exchange_market_scanner.py` owns the source-observation validation and candidate geometry. `tests/test_exchange_market_scanner.py` verifies shared closed-candle mapping and fail-closed incomplete setup evidence. `tests/test_issue322_reject_geometry.py` drives the real `_scan_once`/selection/build/score/reject/persistence sequence for LONG, SHORT, negative expectancy, repeated persistence, label eligibility, exact incomplete fields, and no execution mutation. `CHANGELOG.md`, `VERSION.md`, and `REPORT.md` record the operational change.

There is no schema, migration, CSV/export, identity, outcome, or historical-data rewrite. Atomic review plus pending persistence and their unique identities remain unchanged. Existing canonical outcomes remain immutable.

### Existing campaign and push recommendation

`camp_8a577772ded0bdf2` must be preserved as historical pre-fix evidence and restarted as a fresh post-fix PAPER campaign. Its 590 incomplete observations do not safely contain the absent source levels, and the idempotency/provenance contract does not authorize retroactively rewriting them. Phase C is not complete. Recommend this focused hotfix for review only after the focused and full repository suites pass. LIVE readiness is unchanged.

Focused scanner/runtime/status validation passed 85 tests. The full behavioral run completed with 1,239 passed and 6 skipped; its only four failures were the repository's Alembic checks because the declared Alembic package is absent. Installing it was attempted and blocked by the environment proxy with HTTP 403. `compileall` and diff checks passed. CI with declared dependencies remains the full-suite merge gate.

---

# PAPER reject forward-outcome feedback surgery — 2026-08-13

## Existing SQLite reject-label compatibility hotfix — 2026-08-16

PR #317 updated the Alembic revision and fresh Phase 8 table DDL, and added normal-bootstrap compatibility for `rejected_signal_reviews`, but `_apply_sqlite_migrations` omitted the four new columns consumed by the standalone resolver on an already-existing `burnin_pending_reject_labels` table. SQLite `CREATE TABLE IF NOT EXISTS` does not alter that table, so startup could complete while the resolver's first `GROUP BY symbol,timeframe` failed. The prior tests started from fresh databases or bootstrapped the campaign schema directly; they did not run normal `init_db` against a real pre-#317 pending-label shape.

The narrow repair adds nullable `timeframe`, `horizon_bars`, `claim_token`, and `claimed_at` columns idempotently in `_apply_sqlite_migrations`. The existing central schema doctor now conditionally requires the same columns whenever the pending-label table exists and performs that conservative additive repair during its pre-bootstrap validation, ensuring the schema is usable before startup reports success. Alembic 0006, fresh DDL, campaign bootstrap, normal SQLite bootstrap, and schema validation now agree on all four pending-label columns; the existing partial unique review-decision index and review columns remain symmetric in Alembic and normal bootstrap.

Regression coverage creates a physical legacy SQLite table and row, verifies doctor detection, initializes twice, checks exact column affinities and null preservation, and exercises the standalone runtime scheduling query with both a legacy null-timeframe row and a new 1m row across restart. Both pending identities and both first outcomes remain singular. Legacy `horizon_seconds` remains unchanged, and no timeframe, horizon bars, claim, label, or outcome is fabricated during migration.

Changed files are `src/alphaforge/persistence.py`, `src/alphaforge/schema_doctor.py`, `tests/test_sqlite_schema_bootstrap.py`, `tests/test_runtime.py`, and the three operational documents. There are no threshold, lifecycle, export, reject-decision, forward-outcome, or LIVE-readiness changes. The migration is safe for the existing PAPER database without deletion or reset: stop the process normally, retain a backup, and restart through canonical `init_db`; startup will add only missing nullable columns and preserve legacy pending rows.

Verification: runtime, resolver, and SQLite bootstrap tests passed (74 passed, 3 skipped); compileall and diff checks passed. The full suite completed with 1,199 passed and 6 skipped. Its only four failures were Alembic test imports because Alembic is not installed in the container. Installing declared dependencies was attempted, but the package index was inaccessible through the environment proxy (HTTP 403), so the Alembic upgrade checks remain a CI/connected-environment merge gate rather than a behavioral failure in this patch.

Push recommendation: ship as a narrow PAPER compatibility hotfix after the declared full suite passes; do not infer LIVE readiness.

---

## PR #317 transient-window and atomic-boundary correction — 2026-08-16

Two merge blockers remained. First, a partial or gapped candle response was written as an immutable incomplete outcome and the pending row became `FAILED`, so later complete market evidence could not repair it. The resolver now releases its claim back to `PENDING`, records `INCOMPLETE_MARKET_WINDOW` diagnostics, and creates no outcome until the explicit window-completeness check passes. Missing immutable execution-cost assumptions remain separately finalizable as execution-invalidated evidence. Second, runtime committed the operator review before opening a separate pending-label transaction. The authoritative PAPER boundary now upserts the review and idempotently enqueues the pending label through one `engine.begin()` transaction; any enqueue exception rolls both back, while retrying a pre-existing orphan review deterministically self-heals through the same reject identity.

`_sync_review` no longer treats `forward_window_bars` as a finalization marker. It synchronizes the exact reject decision while `evidence_complete` is not true, allowing interrupted legacy rows with a populated horizon but missing outcome fields to finish. Once complete, the row remains immutable. New regressions cover a gapped/partial first pass, complete second pass, single canonical TP result, exact review completion, conflicting post-final retry, and orphan-review restart recovery with one review and one pending label.

Changed files in this follow-up are `src/alphaforge/burnin_resolver.py`, `src/alphaforge/runtime.py`, `tests/test_phase8_reject_resolver.py`, `tests/test_runtime.py`, and the three operational documents. No schema, export, threshold, LIVE, identity, claim-token, MFE/MAE, or timeframe contract changed. Full verification results are recorded after execution below.

Verification on this follow-up: the required focused suite passed 63 tests and `python -m compileall src` passed. The complete suite reached 1,197 passed and 6 skipped; its only four failures were environment import failures in `tests/test_alembic_revision_graph.py` because the current container does not have the Alembic package installed (`alembic.config` and `alembic.command` unavailable). No behavioral test failed. This environment limitation remains a merge-gate requirement: run the full suite with declared dependencies installed before merging PR #317.

---

## PR #317 idempotency and evidence-integrity correction — 2026-08-16

The initial restoration incorrectly treated missing execution costs as a correct reject, used replace semantics for finalized outcomes, assumed one-minute bars, synchronized reviews by signal ID, and allowed incomplete candle windows to appear complete. This follow-up introduces a stable `reject_decision_id`, one operator review per final decision, insert-once outcomes, compare-and-set `RESOLVING` claims with stale-claim recovery, and canonical-outcome synchronization for retries. Missing costs, ambiguous outcomes, non-calculable net R, gaps, and incomplete windows retain raw TP/SL and excursion observations but keep `reject_correct` null and evidence incomplete.

Pending labels now persist nullable `timeframe`, `horizon_bars`, `claim_token`, and `claimed_at`. New rows derive `due_at` from configured horizon bars and the actual signal timeframe; standalone runtime fetches each symbol/timeframe group with the matching provider interval. Legacy rows retain their stored `horizon_seconds` and pre-timeframe evaluation fallback because fabricating an interval would corrupt evidence. Revision `0006_reject_label_identity_timeframe` and SQLite bootstrap add the same nullable columns, review validity fields, and partial unique decision index without rewriting historical rows or exports.

Geometry validation requires finite values, positive entry, nonzero risk, and directionally valid LONG/SHORT stop/target ordering. Candles are timestamp-sorted and deduplicated; MFE/MAE stop at the first terminal candle, while timeout uses the full horizon. Explicit bar-count, boundary, and gap checks prevent incomplete market windows from qualifying. Adaptive aggregation excludes invalidated, ambiguous, and incomplete rows. Trading thresholds, PAPER/LIVE_PRECHECK no-order guarantees, and LIVE execution authorization are unchanged; LIVE remains NOT READY.

Changed implementation files are `runtime.py`, `ai_brain.py`, `adaptive_learning.py`, `burnin.py`, `burnin_campaign.py`, `burnin_resolver.py`, `persistence.py`, `schema_doctor.py`, configuration registry/loading, `.env.example`, and Alembic revision 0006. Focused tests cover costs, adaptive exclusion, immutable retry, duplicate identity, overlapping claims, 1m/5m/1h maturity/provider selection, invalid geometry, legacy campaign-only operation, and runtime persistence. Full-suite results are recorded after final execution below.

Final verification: the focused reject-resolver/runtime/adaptive suite passed 61 tests; configuration, migration, and wider focused checks passed; `python -m compileall -q src` passed; and the complete repository suite passed 1,202 tests with 3 skips. The first full run exposed one timing-sensitive heartbeat assertion that passed immediately in isolation and two schema-doctor head-recognition failures; recognizing additive revision 0006 fixed the schema failures, and the final complete run was green. Push recommendation: update PR #317 for review, do not merge automatically, and do not infer LIVE readiness.

---

## Need and root cause

`AIBrain._persist_decision` created `rejected_signal_reviews` with intentionally null future fields, but the runtime's final reject boundary only wrote a burn-in observation. `persist_pending_reject_label` was called by campaign-specific flows, and `resolve_campaign_batch` was scheduled only by `BurnInCampaignRunner`; standalone PAPER runtime therefore had neither enqueueing nor evaluation. In addition, `_build_signal` discarded available stop, target, setup, and regime inputs, which explains both ineligible geometry and `unknown` metadata. The evaluator requires campaign identity, due market candles, complete geometry, and execution costs; ordinary PAPER supplied none of that linkage.

## Minimal runtime, lifecycle, and persistence change

The authoritative final PAPER reject boundary now queues one deterministic pending label per runtime signal after persisting `SIGNAL_REJECTED`. Attached runs use their campaign ID; standalone runs use a stable run-scoped namespace without creating a duplicate trading runtime. `INSERT OR IGNORE` and the unique reject-decision key prevent duplicate pending work. Pending rows remain durable across restart. A runtime background loop fetches only due windows with the canonical read-only candle provider and calls the existing campaign resolver; attached campaign workers remain compatible.

Resolution preserves first-touch TP/SL/timeout/ambiguous semantics, calculates percentage MFE and MAE, persists execution cost assumptions and market-data provenance in the auditable outcome payload, and fills only still-null adaptive review labels. A resolved pending row is excluded from later scans, so finalized labels are not overwritten. The outcome unique key prevents duplicate outcomes. No reject threshold was changed and no rejected signal becomes an order or trade.

## Execution awareness and metadata

Hypothetical net R continues to subtract critical spread, entry/exit slippage, fees, funding, and latency costs. Missing critical costs set `execution_invalidated` and prevent a theoretical TP from becoming a false-negative reject; complete non-positive net outcomes count as correct rejects. MFE/MAE remain market movement observations, not executable PnL. Liquidity, volatility, setup, and source context are retained in pending provenance/outcome payloads when observed. Setup/regime/volatility are propagated from scanner inputs rather than replaced with fabricated defaults; genuinely unavailable values remain null/unknown.

## Files, tests, compatibility, and remaining risks

`runtime.py` owns enqueueing, standalone scheduling, geometry/metadata propagation, and due-window candle acquisition. `burnin_resolver.py` owns MFE/MAE, execution-adjusted correctness, and immutable adaptive-review synchronization. Resolver and runtime regressions cover enqueue, TP, SL, timeout, horizon maturity, excursions, correctness, restart recovery, duplicate prevention, and metadata. No schema or CSV migration is required. Historical unlabeled reviews cannot be reconstructed unless their missing geometry/timestamps can be sourced; provider outages leave pending rows untouched for retry. Ambiguous same-bar TP/SL remains auditable and unlabeled for correctness. LIVE remains NOT READY.

## Push recommendation

Recommend review after focused and full-suite tests pass. This restores evidence collection only and must not be used to loosen `LOW_CONFIDENCE` or other gates until sufficient complete forward evidence accumulates.

---

# Stale PAPER STARTING recovery surgery — 2026-08-11

## Need and root cause

Detached startup demoted the campaign and both continuation rows to `STARTING`, but only the launcher called `_mark_attached_running`. If that launcher disappeared after the worker attached, the worker could continue scanning and persisting decisions indefinitely without owning the operational status transition. Recovery then classified only dead `RUNNING` continuations as stale; the zero-decision startup fallback deliberately rejected a 9,990-decision continuation, leaving this safe but inconsistent case blocked.

## Minimal behavior and state transition

`RuntimeOrchestrator.start` now persists all three linked rows as `RUNNING` immediately after runtime recovery, attachment, and reconciliation succeed, before the `OPERATING` snapshot. Conditional row counts and campaign/run lineage prevent partial or wrong-continuation promotion. For historical dead `STARTING` PAPER scanners, recovery dispatches to the existing explicit transactional zero-exposure terminalizer. The chosen terminal state is `FAILED`: the worker is gone, the continuation cannot truthfully resume, and `RECOVERY_REQUIRED` is the guarded intermediate/operator state rather than a completed outcome. Decisions remain evidence and are not treated as executions.

Follow-up lifecycle hardening moves promotion before setting or persisting runtime `OPERATING`. A promotion failure therefore leaves the runtime at `STARTING`, writes no authoritative `OPERATING` snapshot, and starts no scanner/heartbeat/reconciliation tasks. An already-`RUNNING` campaign is idempotent only after the exact active run and campaign-run mapping are re-read and both prove lineage-matched `RUNNING`; partial three-row state raises an explicit transition inconsistency.

The CI portability follow-up does not require a `STARTUP` snapshot because its persistence is conditional on runtime persistence configuration. The regression instead verifies the production contract directly: the promotion exception propagates, no `OPERATING` snapshot exists, runtime status is not `OPERATING`, and no worker tasks start.

The terminalizer accepts `STARTING` only with a persisted dead PID identity and retains its existing fresh external-evidence bridge, 120-second identity, `BEGIN IMMEDIATE`, final campaign/run/mapping/source/exposure/execution/lifecycle re-reads, exact one-row conditional updates, and rollback-on-drift contract. Any execution or execution lifecycle state, pending reject, local/runtime position/order/orphan, missing evidence, live worker, lineage mismatch, stale snapshot, source change, or unavailable query blocks mutation. LIVE paths are unchanged.

## Reconciliation, persistence, compatibility, and risk

An authenticated `COMPLETE` `AUTHENTICATED_EXCHANGE_SNAPSHOT` with empty positions/orders is now the effective `CLEAN` status of that evaluation; the older `EXCHANGE_STATE_UNKNOWN` event remains immutable history. Non-authoritative probes retain prior semantics and cannot create campaign-linked terminalization evidence. No schema, migration, CSV/export, decision, lifecycle, runtime snapshot, reconciliation, or audit row is deleted or rewritten. The only mutations are guarded campaign/run statuses plus append-only events/evidence. LIVE remains NOT READY.

## Files and tests

`burnin_campaign.py` owns operational promotion, `runtime.py` invokes it before the authoritative operational boundary, `burnin_ops.py` owns stale-scanner dispatch and atomic FAILED terminalization, and `runtime_state.py` resolves the effective clean probe status. Regressions cover 9,990 preserved decisions, all-three-row operational promotion, partial-`RUNNING` rejection, and absence of an `OPERATING` snapshot/task startup after promotion failure; the existing terminalization matrix continues to cover live worker, execution/lifecycle evidence, missing/unknown exposure, pending rejects, source/evidence drift, row-count mismatch, and rollback.

## Tests executed

- Focused stale-scanner, operational-transition, terminalization, runtime recovery, and reconciliation suites passed.
- Focused runtime/recovery suite passed: 199 passed.
- Full repository suite completed with 1,185 passed and 6 skipped; 4 Alembic graph tests could not run because this environment lacks the installed Alembic package.
- Compile and diff checks passed.

## Migration concerns and push recommendation

No migration is required. The patch is suitable for review with the full local suite green; pushed-head CI remains the merge gate. Do not infer LIVE readiness from this PAPER-only correction.

---

# PR #314 production-safety blocker correction — 2026-08-09

## Scope and root cause

The initial bridge incorrectly relaxed `evaluate_runtime_recovery` globally for a same-campaign unclean PAPER snapshot, and treated a complete empty probe as authoritative without cryptographic-request provenance expressed in the normalized contract. This correction restores the shared conservative predicate exactly and confines the exception to explicit `--terminalize-zero-exposure` handling.

## Provenance and terminalization behavior

The canonical credential-gated Binance read-only provider now emits `authenticated=true` and `input_source=AUTHENTICATED_EXCHANGE_SNAPSHOT`; normalization preserves these as typed values. Both the explicit terminalizer predicate and the persistence helper require the exact boolean/source contract. Provider class/name strings are retained only for diagnostics and never establish authority. False, absent, or fake-name-only provenance cannot append a snapshot or mutate a campaign.

Normal recovery, recovery-drill, startup, and LIVE continue treating same-campaign prior-unclean state as blocked even when a clean probe exists. The explicit terminalizer may consume that one known block only when the state is same-campaign/prior-unclean, the prior process is dead, canonical worker-death and local gates passed, the authenticated probe is complete, and every runtime exposure/source gate is available and zero. It then appends exact campaign/run/release evidence before the unchanged transaction.

## Safety and compatibility

The 120-second freshness policy, `BEGIN IMMEDIATE`, final re-reads, source/runtime hashes, execution/lifecycle counts, exact conditional row counts, rollback, FAILED status, audit event, idempotency, and append-only persistence are unchanged. `ACTIVE_CAMPAIGN_STATUSES`, Control Center, LIVE, dashboards, exports, and schemas are not changed by this correction.

## Tests executed

- `python -m compileall src`: passed.
- `pytest -q tests/test_phase9_burnin_ops.py`: 101 passed.
- `pytest -q tests/test_runtime.py`: 42 passed.
- `pytest -q tests/test_phase8_burnin_campaign.py`: 33 passed.
- `pytest -q tests/test_control_center_api.py`: 52 passed with dependency deprecation warnings.
- `pytest -q tests/test_binance_reconciliation_provider.py` (combined focused run): passed; the combined runtime/Phase 9/provider run completed 198 tests.
- `pytest -q`: 1,178 passed, 3 skipped, 282 warnings.
- `git diff --check`: passed.

The local full suite is green. GitHub Actions on the final pushed PR head remains the merge gate.

---

# Historical PAPER evidence-bridge surgery — 2026-08-09

## Need and root cause

The explicit terminalizer correctly rejected stale or unlinked runtime snapshots, but a dead historical worker could not create the fresh campaign/run-linked snapshot the terminalizer required. `recovery-drill` evaluated current state for continuation recovery and was intentionally not a terminalization evidence writer.

## Behavior, lifecycle, and persistence

`burnin_ops` now performs strict PAPER, RECOVERY_REQUIRED, continuation, local exposure, and canonical dead-worker checks before requesting authoritative recovery state. Only a complete authenticated probe with available local/runtime sources, no query errors or recovery block, a clean kill-switch state, and zero positions/orders/orphans is appended as a new `RECONCILED` runtime snapshot. The row has a database ID, concrete canonical timestamp, unique instance/startup IDs, and exact campaign, burn-in run, release, and PAPER linkage; its diagnostics retain the probe and prior snapshot ID. No historical row is rewritten.

Terminalization remains separate. The existing `BEGIN IMMEDIATE` phase re-reads campaign/run/mapping/worker/exposure/execution/lifecycle/source and exact latest runtime linkage, applies the unchanged 120-second freshness bound, requires three one-row conditional updates, writes the audit event, and commits atomically. Failure rolls back terminal mutation. Success remains FAILED with `MANUAL_ZERO_EXPOSURE_TERMINALIZED`; normal recovery and `recovery-drill` remain non-destructive.

## Files, compatibility, tests, and risks

`src/alphaforge/runtime_state.py` owns append-only canonical evidence persistence and additive completion of reduced historical snapshot schemas. `src/alphaforge/burnin_ops.py` owns the guarded bridge and unchanged terminal transaction authority. `tests/test_phase9_burnin_ops.py` covers zero/nonzero decision histories, exact audit identity, provider failure, no fake snapshot, and existing race/replay gates. `docs/KOMUTLAR.md` documents operator results. There are no export changes, Control Center writes, LIVE changes, freshness widening, or `ACTIVE_CAMPAIGN_STATUSES` changes. Migration is additive only for legacy reduced runtime snapshot tables. Provider ambiguity and any exposure remain fail-closed. LIVE remains NOT READY; merge recommendation depends on the full suite passing.

## Example results

Success returns `status: PASS`, `terminal_status: FAILED`, and the exact `runtime_evidence` snapshot ID/time/hash. Provider or reconciliation failure returns `status: FAIL_CLOSED` with existing-compatible evidence/runtime reasons, leaves RECOVERY_REQUIRED unchanged, and appends no fabricated snapshot.

## Tests executed

- `python -m compileall src`: passed.
- `pytest -q tests/test_phase9_burnin_ops.py`: 98 passed.
- `pytest -q tests/test_phase8_burnin_campaign.py`: 33 passed.
- `pytest -q tests/test_control_center_api.py`: 52 passed (39 dependency deprecation warnings).
- `pytest -q`: 1,167 passed, 6 skipped, and 4 failed solely because the environment does not have the declared Alembic dependency installed. An attempted `pip install 'alembic>=1.13,<2.0'` was blocked by the environment's package-index proxy (HTTP 403).
- `git diff --check`: passed.

Because the requested full suite is red in this environment, this report does **not** recommend merge. Run the full suite on the PR head in an environment with repository dependencies installed and require green results before merge.

---

# PAPER terminalization TOCTOU surgery — 2026-08-06

## Root cause and transaction ordering

The prior operator path validated campaign/runtime exposure, lineage, worker state, executions, and source hashes before acquiring `BEGIN IMMEDIATE`. A concurrent local writer could therefore invalidate the decision before status updates. The revised order is: capture expected continuation and versioned external runtime evidence; acquire `BEGIN IMMEDIATE`; re-read every final local gate through the transaction owner; compare identities/hashes/linkage; execute three exact conditional updates with `rowcount == 1`; insert the audit event; commit. Any validation, row-count, or event failure rolls back the entire transaction.

## External identity, lifecycle, and persistence

Authoritative evidence is bound to runtime snapshot ID, snapshot timestamp, canonical snapshot hash, instance/startup/campaign/run lineage, PAPER mode, reconciliation status, and a 120-second freshness policy. The locally stored snapshot linkage is re-hashed inside the write transaction. Dead-worker evidence requires an append-only recovery event containing the historical PID; PID absence alone is insufficient. Source rows are never rewritten. Strict replay requires the same campaign/run, FAILED statuses in all three records, matching terminalization audit identity, unchanged source hash, and unchanged runtime evidence hash.

## Files, tests, compatibility, and risks

`src/alphaforge/burnin_ops.py` owns the transactional snapshot helpers, evidence model, exact mutations, audit payload, and replay rules. `tests/test_phase9_burnin_ops.py` adds real SQLite mutations between precheck and `BEGIN IMMEDIATE` for campaign/run/status/exposure/reject/execution/lifecycle/source/worker/runtime linkage, trigger-driven zero-row updates, event rollback, identity recording, replay, and unrelated FAILED rejection. Existing resolver/maintenance offload and lock retry remain unchanged and covered by Phase 8/heartbeat tests. The focused Phase 8/Phase 9/heartbeat suite passed 133 tests. The full suite completed with 1,112 passed and 6 skipped; four Alembic graph tests could not run because the environment lacks the installed Alembic package. Compileall and diff validation passed. No schema/export migration or trading behavior change exists. Legacy evidence without immutable identities fails closed; LIVE remains NOT READY.

---

# PAPER recovery completion follow-up — 2026-08-06

## Why and root cause

The contention hotfix correctly moved a dead worker to RECOVERY_REQUIRED, but that status intentionally remained in duplicate-active preflight scope and there was no operator command capable of safely completing the state. A verified zero-exposure campaign could therefore remain blocked forever. Synchronous SQLite busy waits also still ran inside asyncio resolver and maintenance loops.

## Files and behavior

`src/alphaforge/burnin_ops.py` adds the explicit PAPER-only `recover-runtime --terminalize-zero-exposure` path. It requires a dead worker, RECOVERY_REQUIRED campaign/run lineage, complete campaign and runtime query availability, CLEAN unblocked reconciliation, zero positions/orders/orphans/pending rejects, and zero executions/lifecycle executions. It preserves decisions and all source evidence, atomically marks both run tables and the campaign FAILED, and appends `PHASE9_MANUAL_ZERO_EXPOSURE_TERMINALIZED`; event failure rolls the transaction back and replay is idempotent. Without the flag, existing recovery remains fail-closed.

`src/alphaforge/burnin_campaign.py` moves resolver and maintenance ticks to `asyncio.to_thread`; maintenance now uses fresh-connection lock retry. A SQLite busy timeout/retry may occupy its worker thread, but no longer blocks runtime heartbeat or scanner scheduling on the event loop.

## Lifecycle, persistence, export/schema, and compatibility

No schema, export, source hash, observation, decision, lifecycle evidence, trading threshold, qualification gate, or reconciliation gate changes. FAILED is the canonical terminal status for this explicitly abandoned zero-execution continuation. RECOVERY_REQUIRED remains an active blocker until the operator supplies the flag and every gate passes. Existing campaigns require no migration.

## Tests, risks, and recommendation

Tests cover explicit gating, complete/unavailable exposure, atomic rollback when event persistence fails, idempotence, evidence-hash preservation, duplicate-active release, fresh runtime heartbeat during exhausted resolver waits, and multiple locked maintenance cycles. Persistent contention can delay campaign maintenance in worker threads; SQLite remains single-writer. Run repository CI on the pushed PR head and require visible passing checks before merge. LIVE remains NOT READY. The focused burn-in/operations/heartbeat suite passed 120 tests; the full suite passed 1,106 tests with 3 skips; offline CI backtest and output checks, compileall, and diff validation passed. GitHub check visibility could not be queried from this environment because GitHub CLI authentication is unavailable, and flake8 installation was blocked by the environment network proxy; merge still requires the pushed-head Actions checks.

---

# PAPER burn-in SQLite contention surgery — 2026-08-01

## Why and exact root cause

The resolver committed its batch, then synchronously rebuilt the complete synthetic aggregate on every resolver tick. Aggregate delete/copy work shared SQLite with runtime heartbeat and decision writers. Although WAL and a busy timeout were present in the observed database, the aggregate transaction could still lose the single-writer race. Its `OperationalError` entered a broad resolver handler that opened another write transaction to persist `RESOLVER_BATCH_FAILED`; when that insert encountered the same lock, the secondary exception escaped `_resolver_loop`, and `run_foreground` treated it as a task failure.

## Files and runtime behavior

`src/alphaforge/burnin_campaign.py` adds lock-only bounded exponential retry. Every failed attempt rolls back, invalidates and closes the SQLAlchemy connection, then checks out a new DBAPI connection. Aggregate replacement, qualification evaluation, and snapshot/event linkage remain separate transactions. Lock exhaustion increments resolver failures, emits best-effort SQL evidence plus stderr fallback, skips the cycle, and does not set the worker stop event. Non-lock `OperationalError` continues to escape fail-closed. Qualification now runs for first evidence, campaign target proximity, or after both the minimum interval and 25 new observations, rather than on every resolver tick.

`src/alphaforge/persistence.py` and the burn-in runner apply WAL, 30-second busy timeout, `synchronous=NORMAL`, and `foreign_keys=ON` on every SQLAlchemy SQLite connection. `src/alphaforge/burnin_ops.py` makes official preflight/watch cleanup preserve evidence while moving a dead-PID `RUNNING` continuation and campaign to `RECOVERY_REQUIRED`; normal authenticated recovery gates still apply.

## Lifecycle, persistence, schema, export, and compatibility

No trading decision, reject, RR, lifecycle, execution-cost, reconciliation, evidence, or qualification threshold changed. Aggregate source ordering and canonical hashing are unchanged. There is no schema or CSV migration. Transient lock failure may delay the latest qualification snapshot, explicitly preferring stale-but-valid evidence over partial/fabricated evidence. A stale campaign changes state from `RUNNING` to `RECOVERY_REQUIRED`, with an audit event and preserved rows; it is not automatically resumed or qualified.

## Tests and risks

Tests cover first-lock success-on-retry with distinct connections, exhausted resolver locks, secondary event locks, non-lock schema errors, qualification gating, existing concurrent runtime/resolver behavior, runtime heartbeat persistence, and dead-worker continuation transitions. SQLite remains single-writer, so sustained contention can defer maintenance. A new campaign is not required: the failed campaign evidence can be recovered through the supported drill if integrity and reconciliation pass, but the failed worker incident must not itself be relabeled as successful. Starting a clean continuation after recovery is recommended for additional burn-in duration. LIVE remains NOT READY.

## Tests executed

- Focused burn-in, operations, and heartbeat suite: 115 passed.
- Full suite: 1,094 passed and 6 skipped; four Alembic revision tests could not run because the environment lacks the installed `alembic` package.
- Python compileall and Git whitespace validation passed.

## Migration and push recommendation

No migration is required. Push is recommended after the focused and full suites pass; operators should run official preflight/recovery for stale campaign `camp_d53aa4fe41a221c2` and audit campaign `camp_8b3c86cda7056d1d` before resuming.

---

# Phase A shadow agent graph surgery report — 2026-08-01

## PR #310 SQLite contention revision

Production PAPER evidence exposed `database is locked` while authoritative lifecycle and reconciliation writers overlapped. The original Phase A hook amplified risk by creating one task/thread and two write transactions per decision against the canonical database, including repeated DDL bootstrap.

The revision removes agent DDL from canonical `init_db`, makes repository construction read/write-free, and performs one controlled bootstrap only when the feature is enabled. Shadow traces default to `data/runtime/alphaforge_agent_shadow.db`, with WAL/busy timeout applied on its connections, short transactions, and bounded busy retry. One worker drains a bounded queue and serializes all trace writes; no per-decision task or thread is created. Full-queue overload deterministically drops the newest optional trace without affecting legacy behavior. Metrics expose queue depth, dropped/deferred traces, retry count, lock-wait duration, and worker count.

Concurrency tests overlap lifecycle, reject, reconciliation, and heartbeat writes on the canonical runtime database with 60 queued shadow decisions, verify no lock failures or authoritative failure, retain every admitted trace, and prove one worker. Migration is additive only in the separate shadow database; existing canonical databases need no agent schema migration. LIVE remains NOT READY.

Executed checks: the focused config/agent/runtime-heartbeat suite passed 51 tests; the full suite produced 1,090 passed and 6 skipped, with only the 4 Alembic revision-graph tests failing because the environment lacks the installed `alembic` package (`ModuleNotFoundError`/`ImportError`). `compileall` and `git diff --check` passed. This dependency limitation is unrelated to the shadow persistence change.

---

## Why and root cause

Issue #309 requires a shared deterministic boundary before future agents can be evaluated without coupling experimental logic to the authoritative runtime. Previously there was no immutable stage envelope, bounded fixed graph, or isolated SQL trace store.

## Files and behavior

`src/alphaforge/agents/` adds immutable contracts, the fixed shadow orchestrator, and additive SQL repository. The config registry/loader and environment examples add six typed controls. `runtime.py` schedules a copied legacy decision after rejection persistence or an accepted decision, never awaits the graph on the order path, and records only new shadow metrics. `persistence.py` bootstraps isolated trace tables. Agent tests cover determinism, validation, bounds, hard rejects, failure isolation, duplicate-safe SQL, null unavailable values, and disabled defaults. `docs/agent_graph.md` and `docs/KOMUTLAR.md` document design and operation.

## Lifecycle, persistence, schema, compatibility, and migration

Legacy lifecycle, reject gates, reconciliation, authorization, burn-in, order submission, and decision values are unchanged. `agent_runs` and `agent_stage_events` are additive SQLite-compatible tables; no existing table is repurposed or written by the graph. Bootstrap is idempotent and requires no destructive migration. The feature defaults off. Shadow flags intentionally remain outside Phase 8/9 campaign identity because they cannot affect execution decisions; this avoids campaign fragmentation.

## Tests, risks, limitations, and recommendation

Focused contract/orchestrator/persistence tests were added. The full suite is executed before push and its final result is recorded in the PR. Phase A has no business handlers, reflection retry requests, exchange access, or authority. Background scheduling avoids order-path latency, but an abrupt shutdown can lose an unstarted trace. Persistence failure is diagnostic only. LIVE remains NOT READY. Push is recommended only with passing focused and full suites; no auto-merge or LIVE rollout is recommended.

---

# Binance USD-M reconciliation symbol-validation surgery — 2026-07-25

## Unicode catalog follow-up (v8)

### Why needed and root cause
PR #306 limited authoritative `exchangeInfo` validation to delivery-pattern
candidates. Legitimate Demo Unicode symbols therefore still failed the ordinary
ASCII grammar despite exact catalog membership. The scanner also lacked explicit
catalog-status gating, which could not prove that reconciliation visibility and
new-trade eligibility remained separate.

### Files and behavior changed
- `src/alphaforge/binance_reconciliation_provider.py` rejects empty, surrounding
  whitespace, Unicode/ASCII control, and invisible-format input before catalog
  access. Every other grammar exception requires exact, case-preserving catalog
  membership. The verified spelling flows unchanged into signed `userTrades`;
  standard URL encoding percent-encodes its UTF-8 bytes. Catalog status is not a
  reconciliation filter, preserving existing exposure visibility.
- `src/alphaforge/exchange_market_scanner.py` fetches `exchangeInfo` and emits
  Binance candidates only for exact `TRADING` members. `PENDING_TRADING` remains
  ineligible even when ticker, book, and funding data exist.
- Provider and scanner tests cover the four observed symbols, absence, a Unicode
  lookalike, unsafe raw inputs, encoded URLs, pending status, and legacy ASCII and
  delivery behavior.

### Lifecycle, persistence, export/schema, and compatibility
No lifecycle transition, persistence row, CSV export, database schema, or
migration changes. Reconciliation selection expands only to exchange-verified
symbols representing existing exposure/orders/tracking. Scanner compatibility is
intentionally stricter: catalog absence, malformed payload, non-`TRADING` status,
or request failure yields no Binance new-trade candidate.

### Tests executed
- `pytest -q tests/test_binance_reconciliation_provider.py tests/test_exchange_market_scanner.py`
- `pytest -q` (1,065 passed and 6 skipped; 4 Alembic tests could not run because
  the environment does not have the Alembic package installed)
- `python -m compileall -q src tests`
- `git diff --check`

### Risks, limitations, migration, and push recommendation
Unicode comparison is deliberately exact: no normalization or lookalike folding is
performed. Catalog availability is required only when reconciliation encounters a
safe grammar exception, while scanning always requires fresh status evidence.
Credentialed Demo verification remains outstanding, so LIVE is NOT READY. No
migration is needed; push is recommended after the targeted and full test suites.

---

## Why the patch was needed and root cause
The provider accepted only `[A-Z0-9]{2,20}`. Binance USD-M delivery contracts use
an underscore plus a six-digit delivery date (for example `BTCUSDT_250627`), so a
legitimate nonzero position could abort position normalization before global
`openOrders` was requested. The retained failure evidence safely hashed invalid
raw symbols, which means the exact historical value cannot be recovered from the
reported `active_position_invalid_symbol` error alone. A credentialed rerun (or
the original sanitized raw capture) is required to identify that exact value.

## Files changed and runtime behavior
- `src/alphaforge/binance_reconciliation_provider.py` recognizes the narrow USD-M
  delivery candidate grammar only after exact membership validation through public
  `/fapi/v1/exchangeInfo`. Ordinary symbols retain the conservative grammar.
  Whitespace, controls, Unicode, overlong values, and unlisted delivery candidates
  remain fail-closed for nonzero exposure. Diagnostics expose only a reason and a
  stable SHA-256 identifier for invalid raw data.
- Endpoint statuses now distinguish `NOT_ATTEMPTED`, `REQUEST_FAILED`,
  `PAYLOAD_VALIDATION_FAILED`, and `PASS`. Consequently `openOrders` is explicitly
  not attempted when earlier position validation blocks the snapshot.
- `src/alphaforge/binance_reconciliation_check.py` passes through the provider's
  endpoint statuses instead of deriving false failures from coverage booleans.
- `tests/test_binance_reconciliation_provider.py` covers verified delivery symbols,
  catalog rejection, overlong/whitespace/control/Unicode inputs, active fail-closed
  behavior, zero-exposure warnings, endpoint ordering, and endpoint status truth.

## Lifecycle, persistence, export/schema, and compatibility impact
Lifecycle and trading decisions are unchanged. No persistence table, CSV export,
or schema changes, and no migration is required. Snapshot JSON gains
`endpoint_statuses` and invalid-symbol warnings gain a safe `reason`; existing
coverage and evidence fields remain. Account-wide `positionRisk` and `openOrders`
requests are preserved, and fill requests remain restricted to the union of
tracked, active-position, and open-order symbols.

## Tests executed
- `pytest -q tests/test_binance_reconciliation_provider.py tests/test_binance_reconciliation_check.py`
- `python -m compileall -q src tests`
- `git diff --check`

## Risks, limitations, migration concerns, and push recommendation
Public `exchangeInfo` availability becomes mandatory only when a nonzero delivery
candidate requires proof; an outage remains fail-closed. Exact-zero malformed rows
remain nonblocking warnings and never expand fill scope. The original raw incident
symbol is not present in this repository or prompt evidence, so asserting its exact
value would be unsafe; a diagnostic rerun will preserve a verified legitimate
symbol verbatim while malformed values remain hashed. No migration is needed.
Push is recommended after credentialed Demo acceptance. LIVE remains NOT READY.

---

# RECOVERY_REQUIRED failed-startup recovery surgery — 2026-07-25

## Why the patch was needed and root cause
PR #304 admitted FAILED and PAUSED campaigns to the narrow provider-only,
zero-activity PAPER startup fallback. Before that fix, however, the general drill
failure branch had already changed affected PAUSED campaigns to RECOVERY_REQUIRED
and stamped `RECOVERY_DRILL_PRECHECK_FAILED`. On retry, every exposure and startup
predicate could pass while the stale campaign-state allowlist alone rejected the
campaign, producing a self-created recovery deadlock.

## Files and exact behavior changed
- `src/alphaforge/burnin_ops.py` recognizes RECOVERY_REQUIRED only when paired
  exactly with `RECOVERY_DRILL_PRECHECK_FAILED`, proving this recovery mechanism
  produced the state. Diagnostics expose the observed status/error, that narrow
  state predicate, and the complete terminal fallback result.
- `tests/test_phase9_burnin_ops.py` executes the actual two-attempt transition and
  verifies terminal FAILED state, no resume/launch, append-only drill/reconciliation
  evidence, and cleared later recovery scope. Negative cases cover other/null
  provenance, decisions, executions, lifecycle execution, PID/liveness, campaign
  and runtime exposure, pending rejects, unavailable local evidence, mixed errors,
  and LIVE/LIVE_PRECHECK.

## Runtime, lifecycle, persistence, export, and schema impact
Successful repair changes only the campaign status to FAILED and clears worker
ownership. The active run stays FAILED; no successor or worker is created.
Recovery drills, incidents, runtime snapshots, reconciliation events, and the
terminalization event remain append-only. No lifecycle data is rewritten, no CSV
contract or schema changes, and no migration is required.

## Tests executed
- `pytest -q tests/test_phase9_burnin_ops.py`
- `pytest -q` (attempted; environment lacks the Alembic package and collection fails at `tests/test_alembic_revision_graph.py`)
- `python -m compileall -q src tests`
- `git diff --check`

## Risks, remaining limitations, migration concerns, and push recommendation
The exception deliberately cannot repair arbitrary RECOVERY_REQUIRED campaigns.
All prior safety gates remain conjunctive, including PAPER-only mode, complete
zero local activity/exposure, absent/dead process and worker, inactive kill switch,
and provider-unavailability as the only query failure. Ambiguity remains manual;
LIVE is NOT READY. No migration is needed. Push is recommended after targeted and
full-suite verification.

---

# PAUSED zero-exposure PAPER failed-startup recovery surgery — 2026-07-25

## Why the patch was needed and root cause
The provider-only terminal startup fallback in `recovery_drill` required
`campaign_status == FAILED`. The observed run row was correctly FAILED, dead, and
empty, but its campaign remained PAUSED. That single campaign-state predicate
prevented creation of append-only local recovery evidence; the later general
terminalization predicate also excludes PAUSED and requires an already-clear
runtime gate, so the drill returned manual recovery and preflight retained its
`runtime_recovery_scope` blocker.

## Files and exact behavior changed
- `src/alphaforge/burnin_ops.py` admits PAUSED alongside FAILED only to the narrow
  terminal zero-startup fallback. All existing predicates remain mandatory:
  FAILED PAPER run, dead and absent worker PID, available zero decision/execution/
  lifecycle counts, available zero campaign and runtime exposure, inactive kill
  switch, dead prior process, and provider unavailability as the entire error set.
- `tests/test_phase9_burnin_ops.py` exercises the observed PAUSED state through
  real append-only recovery persistence and asserts the terminal FAILED campaign,
  preserved FAILED run, and unblocked later PAPER scope. Additional regression
  cases cover campaign positions, pending rejects, an alive worker, unavailable
  local exposure, and a non-provider reconciliation error; existing tests cover
  decisions, executions, lifecycle executions, runtime orders/orphans, and LIVE.

## Runtime, lifecycle, persistence, export, and schema impact
The failed run is never resumed or rewritten. Recovery appends the decision,
provider-unavailable reconciliation event, local diagnostic runtime snapshot,
campaign terminalization event, and recovery drill, then changes only the campaign
from PAUSED to FAILED. Historical snapshots and evidence rows are retained. The
diagnostic explicitly preserves unknown exchange state and does not claim it is
zero. Lifecycle, CSV exports, and schemas are unchanged; no migration is needed.

## Tests executed
- `python -m pytest tests/test_phase9_burnin_ops.py -q`
- `python -m pytest -q`
- `python -m compileall -q src tests`
- `git diff --check`

## Risks, remaining limitations, migration concerns, and push recommendation
This exception is intentionally PAPER-only and proves the failed run had no local
activity before accepting unavailable remote evidence. Any missing SQL evidence or
possible exposure remains manual recovery. LIVE remains NOT LIVE READY and fully
fail-closed. No migration or compatibility action is required. Push is recommended
after targeted and full-suite verification.

---

# Runtime exposure startup schema-bypass surgery — 2026-07-24

## Why the patch was needed and root cause
PR #302 made schema diagnosis and recovery counting schema-family aware, but
`RuntimeOrchestrator._load_recovery_state` retained two physical-table SQL strings.
On an Alembic-head database, preflight correctly selected `runtime_positions` and
`runtime_orders`, then the detached worker queried domain `positions.qty/status`
and exited with `sqlite3.OperationalError`. The stale locations were
`src/alphaforge/runtime.py` lines 389 and 391 before this patch.

## Files and exact behavior changed
- `src/alphaforge/schema_doctor.py` now owns validated exposure resolution and
  normalized readers for active positions and pending orders. It detects schema
  family, checks required columns and migration checksums, validates every state,
  and emits `RUNTIME_EXPOSURE_SCHEMA_UNAVAILABLE`, `UNKNOWN_EXPOSURE_STATE`, or
  `MIGRATION_CHECKSUM_MISMATCH` rather than leaking a missing-column SQL error.
- `src/alphaforge/runtime.py` uses those readers during startup. A repository-wide
  scan found no other production Python/SQL direct reads or writes matching the
  requested safety-sensitive physical-table patterns; the removed two statements
  were runtime exposure/recovery uses. Domain persistence remains owned by its
  ORM/Alembic layer, lifecycle storage remains separate, reconciliation uses the
  central exposure count path, and dashboard/reporting has no direct matching SQL.
- `tests/test_schema_doctor.py` covers both schema families, normalized fields,
  active and terminal filtering, missing quantity, and unknown status behavior.
  `tests/test_runtime.py` prevents reintroduction of direct exposure SQL.

## Runtime, lifecycle, persistence, export, and schema impact
ALEMBIC_HEAD resolves to `runtime_positions/runtime_orders`; lightweight runtime
schemas resolve to `positions/orders`. Domain Alembic tables are neither altered
nor interpreted as runtime exposure. Runtime order adapters now explicitly carry
`created_at`, which is added by the v4 additive migration so pending-order timeout
recovery retains its exact prior contract. Lifecycle ordering and CSV exports are
unchanged. Unknown state, missing field/table, untrusted schema identity, or bad
migration checksum blocks startup before an exposure SELECT is attempted.

## Tests executed
- `pytest -q tests/test_runtime.py`
- `pytest -q tests/test_schema_doctor.py`
- `pytest -q tests/test_runtime_state.py`
- `pytest -q tests/test_phase9_burnin_ops.py`
- `pytest -q tests/test_sqlite_schema_bootstrap.py`
- Full `pytest -q` and detached Alembic-head smoke results are recorded before push.

## Risks, limitations, migration concerns, and push recommendation
Existing valid runtime schemas receive the additive `orders.created_at` column and
a v4 migration record; historical checksums remain recognized. PostgreSQL doctor
parity is not introduced by this SQLite-specific correction. Empty `created_at`
on a pending order intentionally triggers stale-order recovery rather than being
invented. LIVE remains NOT READY. Push is recommended only after the requested
full suite and detached-worker attachment smoke pass.

---

# Terminal PAPER startup recovery surgery — 2026-07-24

## Why the patch was needed and root cause
Recovery had two independently narrow escape paths that did not cover the observed
campaign. The provider-unavailable local fallback required a stale `RUNNING`
continuation in `UNRELATED_HISTORICAL_RUNTIME` scope, while startup terminalization
required an allow-listed launcher error and an already unblocked runtime recovery.
A terminal same-campaign snapshot failed both predicates: provider failure kept the
runtime gate blocked, and `EXCHANGE_RECONCILIATION_UNAVAILABLE` was not a launcher
error. No durable recovery marker could become the latest runtime state, so every
later PAPER preflight continued to inherit the stale blocker.

## Files and exact behavior changed
- `src/alphaforge/burnin_ops.py` adds a fail-closed SQL evidence collector for run
  mode/status, observations (decisions), trade/pending-position execution rows, and
  execution lifecycle states. Recovery permits the provider-unavailable append-only
  fallback for a terminal, dead, PID-less PAPER startup only when every count is
  available and zero, campaign/runtime exposure is available and zero, the kill
  switch is inactive, and provider unavailability is the complete query-error set.
- `tests/test_phase9_burnin_ops.py` covers the observed safe case, persisted audit
  evidence, unchanged historical rows, later unrelated runtime scope, decisions,
  executions, lifecycle executions, positions, orders, both orphan classes, SQL
  evidence failure, and LIVE mode.

## Runtime, lifecycle, persistence, export, and schema impact
The safe case appends `runtime_recovery_events`, `exchange_reconciliation_events`,
and a `LOCAL_DIAGNOSTIC_RECOVERY` runtime snapshot, then persists the existing
Phase 9 campaign recovery event/drill evidence. It does not delete or overwrite the
failed campaign, run, or prior runtime snapshot. The appended clean-for-local-PAPER
scope marker prevents the historical row from remaining the latest global blocker.
No schema or CSV contract changes. Any observed decision, trade/pending-position
execution, execution lifecycle, position/order/orphan, kill switch, SQL error, live
mode, alive worker, running state, or non-provider reconciliation error remains
fail-closed.

## Tests executed
- `python -m pytest tests/test_phase9_burnin_ops.py -q`
- `python -m pytest -q`
- `python -m compileall -q src tests`
- `git diff --check`

## Risks, limitations, migration concerns, and push recommendation
The local diagnostic marker explicitly does not claim authenticated remote exchange
state. Its safety rests on the stronger condition that SQL proves this PAPER run
never produced a decision or any execution/lifecycle evidence and all local runtime
exposure sources are available and empty. Historical databases missing any queried
table/column fail closed. No migration is required. LIVE remains NOT READY. Push is
appropriate after the targeted and full suites pass.

---

# Cross-platform Alembic bootstrap surgery — 2026-07-24

## Why the patch was needed and root cause
Revision `0001_phase1_init` retained one SQLAlchemy `Inspector` while interleaving
schema inspection and table DDL. Inspector reflection is cached, so a migration
decision could use a stale pre-DDL schema view. On SQLite/macOS this left
`config_snapshots` absent when SQLite validated the target of its append-only
trigger, correctly raising `no such table`. PostgreSQL's trigger path also used
unconditional function and trigger creation, which was not safe after a partial
initialization. The error is not suppressed.

GitHub Actions did not exercise the failing migration because `requirements.txt`
did not install Alembic while the revision-graph tests used `pytest.importorskip`;
the executable Alembic checks were therefore reported as skipped in that install
path.

## Files and exact behavior changed
- `alembic/versions/0001_phase1_init.py` now obtains a fresh inspector for every
  create-if-missing decision, completes all table DDL before trigger DDL, and
  verifies all three append-only tables exist before installing triggers.
- PostgreSQL functions use `CREATE OR REPLACE FUNCTION`, and trigger creation is
  guarded by a catalog check scoped to the target relation. SQLite retains
  `CREATE TRIGGER IF NOT EXISTS` only after table existence is freshly verified.
- `tests/test_alembic_revision_graph.py` now covers a partially initialized
  SQLite database using the complete revision-0001 `exchange_symbols` schema,
  preservation of a real row, a repeated upgrade, and fail-closed rejection of
  an incompatible existing table before the database is stamped.
- `requirements.txt` now installs Alembic so CI cannot silently skip executable
  migration checks. The tests now import Alembic directly rather than skipping
  when it is absent.

## Runtime, lifecycle, persistence, export, and schema impact
Runtime decision flow, lifecycle ordering, execution logic, and exports are
unchanged. The intended schema is unchanged. Empty databases create all tables
before triggers; schema-compatible partially initialized databases preserve
existing tables and rows and add missing objects. Existing tables missing any
supported schema family fail clearly and are not stamped as upgraded. The three
shared baseline tables with known divergence (`trade_lifecycle_events`,
`positions`, and `orders`) explicitly accept either the complete revision-0001
shape or the complete current normalized `init_db` shape; arbitrary partial
hybrids remain rejected.

## Tests executed
- `python -c "import alembic; print(alembic.__version__)"`
- `python -m pytest tests/test_alembic_revision_graph.py -q -rs`
- `python -m pytest -q`
- `python -m compileall -q src tests alembic`
- `git diff --check`

## Risks, limitations, migration concerns, and push recommendation
The migration intentionally does not attempt to reshape an incompatible existing
table; it fails closed and requires an explicit repair migration. Column presence
is validated, while deeper type/constraint equivalence remains outside this
revision's compatibility check. Credentialed PostgreSQL execution is
not available locally, so PostgreSQL behavior is covered by conservative native
DDL rather than an integration run. Push after both requested pytest commands
pass. LIVE remains NOT READY.

## PR #299 CI schema-family correction
GitHub Actions exposed that the first fail-closed implementation treated only
revision-0001 columns as valid. Current `init_db` intentionally owns normalized
`trade_lifecycle_events`, `positions`, and `orders` shapes, so the mixed bootstrap
path was supported rather than corrupt. Revision 0001 now recognizes both named,
complete schema families for those tables and still rejects tables matching
neither. No table is rewritten or silently exempted from validation. This restores
`init_db -> alembic head`, `alembic head -> init_db`, and repeated upgrade
compatibility without changing persistence data or runtime lifecycle semantics.

---

# PR #291 Dotenv Bootstrap Consistency Addendum

## Root cause and correction
The pure audit and reconciliation loaders intentionally accepted environment mappings but their CLI wrappers did not call the repository's canonical `bootstrap_environment()`. Operator commands could therefore miss `.env` credentials/settings unless PowerShell copied them into process scope. Both CLI entrypoints now bootstrap once before reading settings. Pure functions never bootstrap and resolve only their explicit mapping, keeping tests and library callers isolated. Process values remain authoritative because the existing bootstrap never overwrites present keys.

## Safety and compatibility
No parser was added: the existing dotenv bootstrap handles quoting and inline comments. Provenance distinguishes keys loaded from dotenv from pre-existing process values, and secrets still expose presence/source only. No trading, recovery, risk, reconciliation, persistence, or LIVE semantics changed; no migration is required.

---
# PR #291 Windows Configuration Diagnostic Report

## Root causes
PowerShell emits comma expressions and space-separated values as multiple argv tokens, while the CLI accepted only one token. The CLI loaded the complete runtime configuration, so an unrelated risk-setting error prevented the narrow exchange diagnostic, then replaced the real cause with a generic label. Repository tracing shows `ALPHAFORGE_MAX_DAILY_LOSS_PCT` is consistently stored and compared as a fraction (`0.02` is 2%); accepting `2.0` would disable the intended limit until 200% loss, so it remains fail-closed with explicit migration guidance. Binance configuration exposed multiple common spellings but validated only `USD_M` without normalization.

## Behavior, precedence, and safety
The CLI now accepts quoted comma lists, multiple PowerShell tokens, and mixed forms; it normalizes, validates, and deduplicates them. A focused loader resolves only reconciliation settings through the canonical registry, while a separate multi-error global audit remains visible. Safe failures include stage/reason/setting metadata and use distinct exit codes. Market aliases normalize to `USD_M`; spot/coin modes fail. Resolution precedence remains process > dashboard > `.env.local` > `.env` > defaults; canonical/alias conflicts fail and empty aliases do not override. Runtime loading remains globally fail-fast. No daily-loss, exchange-state, orphan, kill-switch, LIVE, identity, persistence, or qualification safety gate was weakened.

## Persistence, migration, risks
No schema migration. Operators using `ALPHAFORGE_MAX_DAILY_LOSS_PCT=2.0` must explicitly migrate to `0.02` for 2%. No dashboard-specific daily-loss formatter exists in the current tree; persisted/runtime values remain fractions. Credentialed Demo acceptance is **NOT RUN** and LIVE remains **NOT READY**.

---
# PR #291 Merge-Readiness Validation Addendum

## Need and scope
The implementation was complete, but review still required explicit compatibility evidence for `ALPHAFORGE_BINANCE_RECV_WINDOW_MS` and its legacy `BINANCE_RECV_WINDOW_MS` alias, plus refreshed regression and operational status. No runtime code, trading, recovery, risk, dashboard, lifecycle, or environment-contract behavior changed.

## Validation and compatibility
Tests now prove canonical-only resolution, alias-only resolution, equal non-empty coexistence with canonical precedence, fail-closed contract audit for conflicting non-empty values, and non-conflicting example templates. No schema or migration change is required. Credentialed Demo acceptance remains **NOT RUN**. GitHub fetch/rebase, workflow conclusions, and remote mergeability remain external blockers if network access continues to reject GitHub.

---
# PR #291 Final Scope and Request-Count Correction

## Why, root cause, and behavior
The diagnostic CLI did not supply Phase 9 campaign symbols, so an empty account could complete without exercising `userTrades(BTCUSDT)`. Terminal endpoint summaries also undercounted actual HTTP traffic by omitting failed attempts, retries, and `/fapi/v1/time`. The CLI now reuses `burnin_ops.parse_symbols`, validates/deduplicates tracked scope through the provider, and reports requested/tracked/selected scope plus whether campaign scope was validated. Every provider HTTP operation now passes through one accounting wrapper that increments immediately before transport invocation and appends ordered, sanitized attempt evidence. Counters reset per snapshot; endpoint results remain separate.

## Compatibility, persistence, lifecycle, and risk
No schema, persistence, export, lifecycle, recovery-scope, orphan, PAPER identity, LIVE mutation, or qualification behavior changed. No migration is required. No-symbol mode remains operational but cannot be represented as campaign-equivalent evidence. Credentialed Demo acceptance is **NOT RUN**. GitHub CI and remote mergeability require verification after network access and push; LIVE remains **NOT READY**.

---
# PR #291 Corrective Reconciliation Report

## Need and root cause
The keep-alive transport consumed Binance HTTP error bodies and constructed `HTTPError` with `fp=None`, so the signed-request layer could not observe a real `-1021`. Position normalization also validated symbols before exact `Decimal` quantity classification, making an invalid exact-zero venue row indistinguishable from financially relevant malformed exposure.

## Corrective behavior and files
`binance_reconciliation_provider.py` now preserves error bytes, records sanitized HTTP/Binance diagnostics, refreshes same-host server time once, and resigns. Quantity parsing precedes symbol policy. Invalid exact-zero symbols are preserved as one-way hashes and warnings without fill queries; invalid epsilon or active positions are preserved and fail closed. Partial positions, orders, completed fills, coverage, and unknown symbols survive later failures. Stateful tests exercise the real transport's body retention, close/reset, retry, snapshot isolation, and boundary close behavior. The diagnostic CLI reports invalid exposure classes and can write a safe local distribution.

Canonical configuration now uses `ALPHAFORGE_BINANCE_RECV_WINDOW_MS`; the old `BINANCE_RECV_WINDOW_MS` remains a deterministic lower-precedence alias. Runtime and burn-in continue consuming the same resolved config fields. No persistence schema, export, lifecycle, recovery scope, kill switch, PAPER identity, LIVE mutation, or qualification behavior changed; no migration is required beyond preferring the canonical environment name.

## Validation, risks, and recommendation
Synthetic/default-transport validation is not credentialed Demo evidence. Credentialed Demo acceptance is **NOT RUN**. GitHub fetch/rebase, CI visibility, and remote mergeability cannot be verified while the container receives CONNECT tunnel HTTP 403. LIVE remains **NOT READY**. Request re-review only after current `origin/dev` rebase, green CI, and operator Demo output with complete evidence.

---
# Binance Read-only Reconciliation Phase 9 Surgery Report

## Why and root cause
Latest `dev` parsed every `positionRisk.positionAmt` through `float`, then included every nonzero result plus all normalized open-order rows and tracked symbols without a hard cap. Its one-shot `urllib` calls had no reusable transport, bounded transient retry, or `-1021` clock recovery. A later error replaced already retrieved positions/orders/fills with empty lists and zero orphan counts. Against Demo's broad position universe this produced serial TLS fan-out, stale signatures, incomplete recovery evidence, and a correct fail-closed preflight.

## Files and behavior
The provider now classifies exact quantities with `Decimal`, includes only tracked symbols, genuinely open orders, and positions strictly above the conservative `0.00000001` epsilon, validates symbols, attributes every source, and rejects scopes above the default 10 before `userTrades`. Ten is twice the normal five-symbol PAPER scan ceiling, allowing bounded overlap without permitting a 53-symbol exchange-universe cascade. Successful evidence survives later failure; orphan counts become null while evidence is incomplete. Signed timestamps and signatures are regenerated per attempt; one `-1021` refresh uses `/fapi/v1/time` on the resolved host. Transient transport/429/5xx retries are bounded and discard connections; auth/deterministic 4xx do not retry. Snapshots close transport state.

Runtime and burn-in provider construction now share canonical resolved timeout, receive window, lookback, epsilon, cap, and Binance base URL. Phase 9 preflight supplies its actual campaign symbols. The safe diagnostic CLI uses the same provider/config and provides a local sanitizer; the committed 53-row fixture is explicitly synthetic.

## Lifecycle, persistence, schema, and compatibility
No lifecycle transition, persistence schema, CSV export, order mutation, recovery-scope, kill-switch, PAPER identity, or LIVE qualification semantics changed. Incomplete snapshots preserve evidence and expose null—not zero—unknown orphan counts. No migration is required. Existing deployments gain two optional settings with validated defaults; a cap breach requires operator review rather than truncation.

## Tests and remaining risk
Synthetic cases cover zero-universe fan-out, active/dust Decimal boundaries, tracked/open/closed scope, corrupt exposure, cap failure, evidence preservation, and redaction. Credentialed Binance Demo acceptance is **NOT RUN** because credentials are unavailable. Network access also prevented fetching/rebasing `origin/dev`; the supplied repository HEAD (merge PR #289) was used. Real Demo output and CI remain required. LIVE is **NOT READY**.

## Push recommendation
Push for review only after rebasing on current `origin/dev`, running credentialed Demo acceptance, and confirming CI. Fail closed on any incomplete evidence.

---
# AlphaForge Phase 9 Burn-In Startup Interruption Surgery Report

## Why the patch was needed
A detached PAPER burn-in launch could be interrupted while the parent was polling worker attachment. Because campaign/run rows had already been committed as `RUNNING`, a dead worker PID and stale heartbeat left a ghost active campaign that blocked subsequent launches with both duplicate-active and stale-worker preflight failures.

## Root cause
`launch_campaign()` created the campaign, called `start_or_resume_campaign()`, committed `burnin_campaigns.campaign_status='RUNNING'` plus `burnin_runs.status='RUNNING'`, then spawned the worker and waited in `verify_worker_attachment()`. `KeyboardInterrupt` and `SystemExit` inherit from `BaseException`, so they bypass ordinary `except Exception` cleanup. Repository search found no launch watchdog or subprocess path intentionally raising `KeyboardInterrupt`; the traceback location is where the parent received the interrupt, not proof that a user pressed Ctrl+C. On Windows the worker was also launched without an explicit new process group.

## Runtime behavior changes
Detached launch now compensates the existing start flow by marking the newly allocated run/campaign `STARTING` before spawn, then transitions back to `RUNNING` only after process liveness, attach event, runtime instance, heartbeat freshness, active run ID, and identity evidence pass. Attachment polling detects child exit codes immediately. Startup-failure payloads include stdout/stderr log paths and worker exit code when available. Windows launches request `CREATE_NEW_PROCESS_GROUP` and `CREATE_NO_WINDOW` where Python exposes them, while stdout/stderr log capture remains append-only.

## Lifecycle and persistence changes
Startup failures persist terminal campaign/run evidence and clear invalid worker ownership using explicit reasons: `WORKER_ATTACHMENT_TIMEOUT`, `WORKER_EXITED_BEFORE_ATTACHMENT`, `WORKER_ATTACHMENT_INTERRUPTED`, `WORKER_IDENTITY_MISMATCH`, `WORKER_SPAWN_FAILED`, `_launch_worker()` `RuntimeError` reasons such as `PHASE8_CAMPAIGN_ACTIVE_RUN_MAPPING_INVALID`, `WORKER_STARTUP_EXITED`, `SYSTEM_EXIT_DURING_ATTACHMENT`, or `LAUNCH_STARTUP_FAILED`. Historical rows are retained. `STARTING` is an additive text status, so no schema migration is required.

## Recovery behavior
`watch` can still mark dead workers failed. `recovery_drill()` now recognizes failed startup-only runs with zero burn-in observations, no campaign exposure, no pending reject labels, no runtime positions/orders/orphans, available kill-switch evidence, and no live/ambiguous worker as safe for terminalization. It records `PHASE9_ZERO_EXPOSURE_STARTUP_FAILURE_TERMINALIZED` and returns PASS without creating a continuation. Nonzero or unavailable exposure remains blocked.

## CLI behavior
`--symbols` and `--intervals` now accept comma-separated values and repeated/space-separated values; help text includes a PowerShell example.

## Tests added/executed
Added regression coverage for `KeyboardInterrupt`, `SystemExit`, `_launch_worker()` `RuntimeError`, worker early exit during attachment polling, zero-exposure failed startup terminalization, and symbol parser compatibility. Executed `pytest -q tests/test_phase9_burnin_ops.py` with 46 passing tests.

## Risks, migration, and push recommendation
No schema migration is required, but downstream dashboards that hard-code campaign/run status values should tolerate `STARTING`. The precise external Windows signal source remains unknown without OS/process telemetry from the affected host. Push is recommended after review because the patch prevents ghost active campaigns while preserving fail-closed exposure gates.

---

# AlphaForge Phase 9 Historical Recovery Deadlock Surgery Report

## Why the patch was needed
The observed campaign was stuck in `RECOVERY_REQUIRED` even though there was no live worker, no PID, no campaign open positions, no pending reject labels, no runtime positions/orders/orphans, and no kill switch. The only blocker was an unavailable Binance read-only reconciliation provider attached to an `UNRELATED_HISTORICAL_RUNTIME` snapshot.

## Root cause
`evaluate_runtime_recovery()` counted unresolved reconciliation as global execution risk even when the only unresolved item was provider-unavailable evidence for unrelated PAPER history. `recovery_drill()` then required the runtime recovery gate to be fully unblocked before terminalizing a dead PID-less continuation, so it failed with `UNRESOLVED_RUNTIME_EXPOSURE_OR_RECONCILIATION` and left the old run `RUNNING`.

## Runtime behavior changes
Provider construction remains read-only and credential-gated; no order submission path was enabled. Related/current PAPER runtime history, all LIVE/LIVE_PRECHECK paths, live processes, kill switch activation, nonzero SQL exposure, orphan evidence, pending runtime orders, open campaign positions, and pending reject labels still fail closed. For only the unrelated historical PAPER + dead process/PID + zero local exposure case, recovery drill appends explicit local diagnostic evidence that the provider was unavailable, then terminalizes the stale run as `RECOVERY_REQUIRED` and resumes through the normal monotonic continuation path.

## Lifecycle and persistence changes
The old run and campaign-run mapping no longer remain `RUNNING`; they transition to the existing canonical terminal `RECOVERY_REQUIRED` state. The campaign gets an auditable `PHASE9_STALE_CONTINUATION_RECOVERED` event and incident. Runtime recovery persistence gains an append-only `HISTORICAL_RUNTIME_RECOVERED_LOCAL_EVIDENCE` event, a `LOCAL_ONLY_DIAGNOSTIC` exchange reconciliation event with `exchange_read_only_status=UNAVAILABLE`, and a `LOCAL_DIAGNOSTIC_RECOVERY` PAPER snapshot whose diagnostics include the unavailable-provider reason and original blocked recovery decision. This local diagnostic state is intentionally not exchange-verified evidence. No historical row is rewritten.

## Tests added/executed
Added tests for unrelated historical provider-unavailable recovery, related provider-unavailable blocking, provider-plus-authoritative-query-failure blocking, post-fallback re-evaluation blocking, nonzero exposure blocking, live process blocking, persisted recovery evidence/event, idempotent recovery replay, and no worker launch/order submission when blocked. Executed `pytest -q tests/test_runtime.py tests/test_phase9_burnin_ops.py` with 80 passing tests.

## Risks, migration, and push recommendation
No schema migration is required. The local diagnostic fallback is not exchange-verified and must remain PAPER-only and unrelated-history-only; operators should prefer Binance read-only reconciliation when credentials are available. Push is recommended after review because the patch removes a fail-closed deadlock without weakening current/LIVE exposure safety.

# AlphaForge Phase 9 Burn-In Identity Parity and Worker Attachment Surgery Report

## Recovery deadlock repair

### Root causes
`recovery_drill` treated a PID and a live worker as mandatory preconditions. Thus a PID-less, dead-worker continuation whose two run rows still said `RUNNING` failed before any lifecycle transition, merely setting the campaign to `RECOVERY_REQUIRED`; `attach` and `resume` were null because neither was attempted. Separately, preflight selected the latest runtime snapshot globally. An unscoped, unclean historical PAPER snapshot with no current SQL exposure required a read-only reconciliation probe, but the probe was not persisted after success and provider construction returned `None` when the feature flag or both Binance API credentials were absent.

### Patch and state transitions
For a dead/PID-less `RUNNING` continuation, recovery drill now consults both campaign-owned pending PAPER-position outcomes and the runtime-owned authoritative recovery gate (SQL open positions/orders plus latest orphan order/position reconciliation findings and reconciliation completeness). Only zero exposure with a non-blocked runtime gate transitions both `burnin_runs.status` and `burnin_campaign_runs.status` from `RUNNING` to terminal `RECOVERY_REQUIRED`, records an incident and campaign event containing PID/liveness, heartbeat, exposure and transition evidence, then performs the normal monotonic resume and attachment path. Pending reject labels are preserved as non-financial forward-label evidence. Nonzero/unknown runtime exposure and unavailable reconciliation remain fail-closed with a persisted drill report containing an explicit failure reason.

For an unrelated historical PAPER runtime, a complete empty signed read-only Binance account snapshot now appends a `CLEAN` exchange-reconciliation event and a `RECONCILED` runtime snapshot with `recovery_action_required=0`. This is only done after verified empty orders and positions; missing providers, incomplete results, errors, orders, positions, and all LIVE/LIVE_PRECHECK cases remain blocked. Klines and server-time checks do not construct this provider: it requires `ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION=true` and both `BINANCE_API_KEY` and `BINANCE_API_SECRET`.

### Persistence, compatibility, and safety
No schema migration is required. The patch is append-only for runtime recovery data and does not edit historical snapshots. `RECOVERY_REQUIRED` is the existing compatible terminal state; a new enum/state is unnecessary. Historical rows with null campaign/run/release IDs came from unscoped runtime construction and are now safely superseded only by verified reconciliation, not SQL zero counts alone. No LIVE path or order-submission capability was enabled.

### Tests executed
- `pytest -q tests/test_phase9_burnin_ops.py tests/test_runtime.py` — 67 passed.

## Worker release-identity follow-up

### Why the patch was needed
Preflight constructed a runtime with a temporary `ALPHAFORGE_RELEASE_ID`, but detached worker startup did not pass that release identity. A worker could therefore attach using its inherited environment or the runtime-config fallback, despite the persisted campaign and initial continuation having the requested release.

### Root cause and behavior
`_actual_runtime_identity` temporarily exports the requested release for preflight, while `_launch_worker` previously exported only PAPER mode. Runtime attachment reads release identity from `ALPHAFORGE_RELEASE_ID` first and otherwise from `RuntimeConfig.phase7_burnin_release_id`. This made preflight PASS and worker attachment fail closed with a genuine runtime release mismatch. The mismatch guard remains intact.

Workers now derive `ALPHAFORGE_RELEASE_ID` from `burnin_campaigns.release_id`. Attachment-failure events contain the expected campaign/run release and hashes, observed runtime identity, mismatch map, and release source. The failed active continuation is marked terminally `FAILED` in `burnin_runs` and `burnin_campaign_runs`; it remains auditable but a zero-sample failure is excluded from aggregate qualification/finalization evidence. Resume still creates the next monotonic continuation and preserves all rows.

### Three-way persisted identity follow-up
The original repair loaded the active run for diagnostics but did not use all run identity fields as an attachment gate. Attachment now first compares the campaign and active run (`release_id`, configuration, strategy, universe, PAPER mode, and `git_commit`); disagreement fails with `PHASE8_CAMPAIGN_RUN_IDENTITY_MISMATCH`. It then compares the active run to runtime for runtime-resolvable fields and the campaign to runtime for the campaign-scoped execution-cost hash. A failed zero-sample run is excluded only if it has no observations, trade/reject outcomes, metrics, drawdown, qualification/suspension, or pending position/reject evidence.

### Files changed
- `src/alphaforge/burnin_ops.py`: worker startup passes persisted release identity and operational attachment failure terminalizes the active run.
- `src/alphaforge/runtime.py`: attachment records complete expected/observed identity provenance and terminalizes failed attachments.
- `src/alphaforge/burnin_campaign.py`: keeps run-table lifecycle states consistent and excludes only failed zero-sample continuations from aggregate evidence.
- `tests/test_phase9_burnin_ops.py`, `tests/test_phase8_burnin_campaign.py`: cover persisted worker environment identity, full mismatch evidence, terminal lifecycle, monotonic resume, and aggregate exclusion.

### Migration and operator action
No schema migration is required. For `camp_eaba9994e631d647`, retain `run_0000` as audit evidence, mark it `FAILED` with an end timestamp if it still reads RUNNING, then audit and resume/create the next continuation. Do not rewrite or delete existing evidence.

## Why the patch was needed
Phase 9 PAPER preflight could report a config-hash mismatch even when the intended candidate and runtime configuration were the same. The candidate used `RuntimeSettings`, while `_build_runtime_from_env` copied only a subset of identity-relevant decision-filter fields into `RuntimeConfig`.

## Root cause
The constructed runtime silently fell back to `RuntimeConfig` defaults for omitted stop, regime, and daily-limit fields. The canonical Phase 8 identity builder therefore hashed a different effective filter payload. `RUNTIME_LIMITS_ACTIVE` itself is consistently mode-derived (`True` for PAPER and `False` for BACKTEST); it was not the inconsistency.

## Files changed
- `src/alphaforge/runtime.py`: retains all identity-relevant decision-filter fields in `RuntimeConfig` and transfers their environment-resolved values into the constructed runtime.
- `src/alphaforge/burnin_ops.py`: keeps the canonical identity builder payload available during preflight and persists candidate/runtime payloads plus every differing key/value in the preflight evidence.
- `tests/test_phase9_burnin_ops.py`: covers PAPER parity with non-default fields, mode-aware identity differences, deterministic component hashes, passing preflight, and derived config drift fail-closed behavior.
- `CHANGELOG.md`, `REPORT.md`, `VERSION.md`: document the parity repair.

## Runtime behavior changes
- `build_phase8_campaign_identity` remains the single canonical Phase 8/9 hash builder for campaign candidates and runtime attachment.
- PAPER preflight now compares and exports both exact config payloads and an explicit per-key difference map, in addition to retaining strict critical hash checks.
- Config drift continues to produce `FAIL_CLOSED`; no identity check has been bypassed or weakened.

## Lifecycle changes
- No lifecycle transition behavior changed.

## Persistence changes
- No schema migration is required. Existing preflight JSON/CSV evidence gains candidate/runtime config payload comparison details.

## Export/schema changes
- No database schema changes. Preflight report structure is additive and backward-compatible for existing report consumers that use the existing check fields.

## Tests added
- PAPER candidate/runtime hashes and exact config payloads match with non-default identity fields.
- PAPER and BACKTEST identities differ when their mode-aware runtime fields differ.
- Strategy, universe, and execution-cost component hashes are deterministic.
- Fully healthy preflight passes; a derived runtime config change remains `FAIL_CLOSED` and identifies the differing field.

## Tests executed
- `PYTHONPATH=src pytest -q tests/test_phase9_burnin_ops.py tests/test_phase8_burnin_campaign.py`

## Risks and remaining limitations
- Preflight remains intentionally fail-closed for real payload/hash differences and unavailable critical checks.
- Existing campaigns generated with the prior mismatched payload may need recreation or deliberate operator review; the patch does not rewrite persisted campaign identities.
- LIVE remains unavailable and cannot be approved by Phase 9 decisions.

## Migration concerns
- No schema migration is needed. Operators should rerun preflight before launch so the exported payload comparison captures the effective configuration.

## Push recommendation
Push after full suite confirms no regressions. Do not merge if candidate/runtime payload parity or fail-closed drift detection regresses.

## 2026-07-17 Detached burn-in worker observability repair
- **Root cause:** detached workers discarded stdout/stderr and uncaught worker errors were only returned to the process, leaving attached runs RUNNING after death.
- **Runtime/lifecycle:** worker output is written per campaign; worker exceptions persist events and terminalize active run rows; watchdog dead-worker cleanup clears stale attachment metadata. Operator pauses terminalize the active continuation as PAUSED without altering `last_heartbeat_at` and record `last_operator_activity_at` instead.
- **Persistence/schema:** additive `last_operator_activity_at` column only; no export contract removal. Existing identity checks remain before worker spawn.
- **Risk:** abrupt OS termination can still require a subsequent watchdog/status pass to discover a dead PID; logs are local artifacts and require normal retention management.
- Follow-up: post-attachment uncaught exceptions now use the generic terminalization path and `WORKER_UNCAUGHT_EXCEPTION`; pause retains PID metadata until the worker exits, preventing an untracked-worker window and duplicate resume.

## Recovery poisoning surgery — 2026-07-17

### Why / root cause
`RuntimeOrchestrator._load_recovery_state()` selected the DB-global latest runtime snapshot and compared only `runtime_status` to `STOPPED/CLEAN_SHUTDOWN/STOPPING`. Thus snapshot 3's `RECOVERY_REQUIRED` / `EXCHANGE_RECONCILIATION_UNAVAILABLE` caused each later PAPER startup to persist `STARTUP` then `RECOVERY_REQUIRED` with `UNCLEAN_SHUTDOWN_RECOVERY_REQUIRED`; the newest derived snapshot became the next global blocker. Preflight did not use this runtime branch, so it could pass before startup failed.

### Repair
`evaluate_runtime_recovery()` now evaluates current authoritative SQL positions, orders, latest reconciliation event, and runtime-control kill switch alongside the predecessor snapshot. It classifies `SAME_RUNTIME_LINEAGE`, `SAME_CAMPAIGN`, `GLOBAL_EXECUTION_RISK`, or `UNRELATED_HISTORICAL_RUNTIME`. A fresh unrelated PAPER campaign is permitted only without current exposure, active kill switch, dirty current reconciliation, or living predecessor. Same-campaign and LIVE/LIVE_PRECHECK recovery remain fail closed. PAPER preflight calls exactly the same evaluator.

### Persistence / migration
`runtime_state_snapshots` receives nullable `campaign_id`, `burnin_run_id`, and `release_id` by an additive SQLite `ALTER TABLE` migration. No history is changed or deleted. Non-blocking inherited history writes an append-only recovery event containing blocking snapshot/instance/startup provenance, original reason, current exposure check, and `UNRELATED_HISTORY_NON_BLOCKING`; it never fabricates `CLEAN_SHUTDOWN`.

### Files and tests
- `src/alphaforge/runtime_state.py`: snapshot lineage, additive schema migration, and shared evaluator.
- `src/alphaforge/runtime.py`: scope-aware startup decision and provenance persistence.
- `src/alphaforge/burnin_ops.py`: preflight/startup parity.
- `tests/test_runtime.py`: production poisoning sequence, same-campaign/LIVE strictness, and active position/pending order blocks.

### Risks / operator guidance
This does not clear real exposure: active positions, pending orders, orphan counts in current reconciliation, kill switch, a living predecessor, same campaign recovery, and LIVE history block. Before retrying `camp_aa4d6344700fdb7d`, run preflight then launch with its normal Phase 9 command; retain `camp_74d6a6c0c0fea8a8` and `camp_aa4d6344700fdb7d` rows/events as terminal audit evidence and do not delete snapshots. Cleanup is limited to the existing dead-worker terminalization workflow; no fake clean snapshot is appropriate.

### Recovery follow-up
The evaluator now fails closed on every authoritative evidence-read error and records `query_errors` in recovery events. A read-only reconciliation provider snapshot may independently establish that an old provider outage is no longer current before PAPER startup; absent that current evidence, dirty historical reconciliation remains blocking. Pending orders associated with an unclean predecessor are global execution risk. Preflight bootstraps the normal runtime schema and uses only the shared evaluator, removing the prior DB-global campaign-status count.

### Probe parity follow-up
Preflight and runtime use `build_readonly_reconciliation_probe` with identical provider snapshot normalization. A historical dirty reconciliation event may be overridden only by a current COMPLETE read-only probe with no orders or positions; unavailable/incomplete evidence remains fail closed and its provenance is retained in the recovery decision.

### Probe validation and process identity follow-up
A COMPLETE probe is usable only with empty provider errors and validated list-shaped orders/positions. Missing, malformed, timeout, or incomplete probe evidence fails closed as `RECOVERY_EVIDENCE_UNAVAILABLE`. PID liveness now validates `/proc` command-line lineage when available; a reused unrelated PID with campaign lineage is not a predecessor.

---
# Canonical Configuration Remediation and Demo REST Reconciliation — 2026-07-23

## Why and root cause
Three remote proposals (#293, #294, and #295) were reported to duplicate configuration remediation and Demo REST work. Their refs and metadata could not be fetched in this container because GitHub access returned CONNECT tunnel HTTP 403, so a truthful commit-by-commit/file-by-file remote comparison and remote PR closure could not be completed here. The supplied clean `dev` merge commit was audited directly. It already had the correct bounded fill scope, Decimal epsilon, canonical receive-window alias contract, and canonical reconciliation loader, but runtime and burn-in still selected provider fields locally and Demo resolution unnecessarily required a websocket for REST-only reconciliation.

## Files changed and runtime behavior
`env_contract.py` now distinguishes a REST-only resolver call from strict runtime/streaming resolution. `config/__init__.py` is the sole definition of `load_reconciliation_settings` and opts into REST-only resolution. Runtime, burn-in, and the existing reconciliation CLI all use that loader for endpoint, credentials, receive window, timeout, lookback, Decimal epsilon, and fill cap. `config_fix.py` performs only the mechanically safe receive-window alias canonicalization; dry-run is the default and mutation requires `--apply`.

## Lifecycle, persistence, export, and schema impact
No trading decision, lifecycle transition, reject behavior, persistence row, export, database schema, strategy threshold, score, RR, or acceptance rule changed. No migration is required. LIVE remains disabled. Demo runtime startup still fails closed without a supported explicit websocket, while signed read-only REST reconciliation can operate without one.

## Remediation safety and compatibility
Application writes an atomic `.env.bak` before atomically replacing `.env`. Equal duplicate aliases are removed, conflicts and duplicate ambiguity block without mutation, and a second application is a no-op. Secret values, LIVE controls, and ambiguous risk values are never remediation targets or emitted. The legacy receive-window alias remains accepted when unambiguous; canonical/alias conflicts remain errors.

## Tests and validation
New tests prove one loader definition, all three consumers, Demo REST/runtime separation, conflict closure, deterministic mapping precedence, atomic remediation, secret/LIVE/risk preservation, backup integrity, idempotence, and redaction. The full suite, compileall, config audit, dry-run fixer, and whitespace checks are the required release checks.

## Risks, limitations, migration, and push recommendation
Credentialed Demo network acceptance remains unexecuted. GitHub refs, CI state, and remote closure of superseded PRs must be performed by an operator with GitHub connectivity; no claim is made that those remote actions occurred. There is no schema or configuration migration beyond optionally running `python -m alphaforge.config_fix --apply` after reviewing its dry-run. Push the replacement for review only after required checks pass; do not enable LIVE.
# Executable Environment Contract Surgery Report — 2026-07-19

## Why and root cause
The four dotenv examples contained duplicate, contradictory, and decorative keys. CLI configuration used a simplistic comment split and incorrect low-to-high precedence. `BINANCE_TESTNET` did not select the scanner/reconciliation endpoint, allowing test credentials to reach production public/account URLs.

## Files and runtime behavior
`env_contract.py` now owns portable dotenv parsing/bootstrap and the explicit Binance USD-M production/testnet constants and resolver. `config_registry.py` owns typed operational settings, deterministic aliases, and isolated reserved metadata. `config_audit.py` produces redacted JSON. The canonical config exposes environment, REST/websocket URLs, source, quote asset, market type, receive window, and timeout. Scanner, connectivity, runtime reconciliation, and burn-in reconciliation share that resolved object. No order-submission path was added.

## Lifecycle, persistence, export, and schema
No lifecycle transition, persistence table, CSV export, or database schema changed. Burn-in adds fail-closed preflight evidence before campaign creation. Preflight includes endpoint names/URLs and secret presence/placeholder booleans, never values.

## Tests and audit
Regression tests cover exact-one classification, duplicates, audit PASS, dotenv precedence/comments/quoted hashes, detached-environment inheritance semantics, endpoint precedence/backward compatibility/mismatch rejection, shared reconciliation URL, and secret redaction/placeholders. Existing safety and Phase 8/9 suites are executed as part of the full suite. The committed `docs/config_audit_report.json` is generated with an empty controlled process environment.

## Migration, compatibility, risks, and recommendation
Rename `ALPHAFORGE_BINANCE_RECV_WINDOW_MS` to `BINANCE_RECV_WINDOW_MS`; canonical names win. Replace `BINANCE_TESTNET` with `BINANCE_ENVIRONMENT=testnet`; the boolean remains deprecated. Remove values from RESERVED keys because they have no operational effect. Explicit URL overrides win and generate a warning. `demo` requires both explicit URLs rather than guessed endpoints. Existing `.env` process overrides continue to win. No migration is required. PAPER remains mutation-disabled and LIVE remains NOT LIVE READY. Push is recommended after all tests pass; real credentials must never be committed and matching-environment Phase 9 reconciliation still requires operator-owned read-only credentials and network availability.

---
# Environment Contract Consumer-Wiring Follow-up — 2026-07-19

## Why and root cause
The first contract revision correctly prevented silent omission but overused RESERVED as a parking lot. It mislabeled already-real runtime fields, BACKTEST rejection switches, strategy guardrails, Telegram evidence, and Hyperliquid scanning as unsupported because those settings had not yet been moved into the registry.

## Runtime and behavioral changes
The registry now owns runtime cadence/caps, daily-loss and notional limits, reconciliation operations, persistence/logging, eight BACKTEST rejection switches, strategy-profile guardrails, SHORT breakdown rescue controls, Telegram evidence configuration, and Hyperliquid scanning configuration. BACKTEST switches continue to reach `evaluate_trade_quality`/`select_symbol` only in BACKTEST; PAPER cannot use their bypass list. Hyperliquid enablement now observably suppresses its scanner. Telegram no longer reads process environment directly. Canonical max-concurrent-positions wins over its legacy alias.

## Audit, tests, and unsupported settings
Audit now fails for missing post-loader consumers/tests, invalid mode metadata, and conflicting non-empty alias/canonical values. Its inventory reports consumer, applicable modes, behavioral test, and exact unsupported reason. RESERVED fell from 93 to 47 entries. Remaining reasons are restricted to `NOT_IMPLEMENTED`, `DEPRECATED_NO_EFFECT`, `REMOVED`, `UNSAFE`, and `FUTURE_SUBSYSTEM`.

Executed `PYTHONPATH=src pytest -q` with 801 passed and 16 skipped, plus `python -m alphaforge.config_audit` with PASS (101 WIRED, 14 ALIAS, 47 RESERVED).

## Lifecycle, persistence, compatibility, and risks
No lifecycle, schema, or export format changed. Persistence enablement is now canonical but preserves its prior default. LIVE mutation remains disabled and the existing readiness/reconciliation/recovery gates remain authoritative. Existing `.env` files with conflicting canonical/alias values must delete the alias or make values equal. Unsupported values should be removed. No database migration is required. LIVE remains NOT LIVE READY.

---
# Environment Contract Safety and Behavioral-Evidence Closure — 2026-07-19

## Why and root cause
The consumer-wiring revision still treated the LIVE allow flag and two existing filter concepts as unsupported, accepted category-wide metadata without resolving symbols/tests, and described typed snapshot variation too strongly. A caller could also set `OrderExecutionContext.allow_live_orders` without proving the canonical environment gate or the rest of the authorization chain.

## Runtime, decision, and safety changes
`ALPHAFORGE_ALLOW_LIVE_ORDERS` is now re-resolved at the final LIVE adapter boundary and combined with LIVE enablement evidence, operator acknowledgement, qualification, reconciliation, and an inactive kill switch. False blocks before balance/order adapters; true alone remains insufficient. PAPER/BACKTEST never evaluate this LIVE-only mutation gate. `ENABLE_REGIME_FILTER` aliases the existing canonical regime-alignment decision gate. `ALPHAFORGE_ENABLE_ORDERBOOK_FILTER` and its deprecated alias now reject missing orderbook evidence under the existing unknown-context policy or extreme measured imbalance/spoof risk, without bypassing spread, slippage, liquidity, volatility, or portfolio controls. No exchange call is added to BACKTEST.

## Contract validation and unsupported inventory
Audit resolves each WIRED `consumed_by` dotted symbol against repository AST and each full pytest node ID against a real test function. Generic file paths no longer pass. Typed-resolution tests are retained as parser regressions only. Each unsupported row now includes its exact reason, key-specific explanation, removal recommendation, and intended subsystem. The contract contains 103 WIRED, 16 ALIAS, and 44 RESERVED entries. Full validation completed with 809 passed and 16 skipped; focused contract validation completed with 140 passed, and new safety/filter validation with 9 passed.

## Lifecycle, persistence, compatibility, and risk
No lifecycle, persistence schema, or export change is introduced. Canonical values retain precedence, while contradictory non-empty aliases fail audit. Existing `.env` files should replace `ENABLE_REGIME_FILTER` and `ENABLE_ORDERBOOK_FILTER` with canonical names. LIVE readiness, reconciliation, recovery scope, mutation disablement, and kill-switch behavior remain fail closed. LIVE remains NOT LIVE READY.

---
# Authoritative Runtime LIVE Authorization Integration — 2026-07-20

## Why and root cause
The prior final adapter guard was fail closed, but only tests populated its authorization dictionary. `RuntimeOrchestrator._execute()` still called the real adapter directly and therefore did not demonstrate that qualification, reconciliation, operator, environment, and control-store evidence reached the final order boundary.

## Runtime and safety changes
`RuntimeOrchestrator` now derives a five-field authorization snapshot from authoritative state: runtime LIVE enablement, configured operator acknowledgement, the persisted qualification report, current reconciliation/recovery/orphan state, and a fresh `RuntimeControlStore` kill-switch read. The runtime builds the LIVE `OrderExecutionContext` itself and provides a bound refresh callback. Both `_execute()` and `execute_order_candidate()` invoke the same final validator. The validator ignores a cached mapping as authority, re-runs the callback, and re-resolves `ALPHAFORGE_ALLOW_LIVE_ORDERS` immediately before mutation.

## Tests and stale-state coverage
The integration test drives the real `RuntimeOrchestrator._execute()` path and proves missing/failed qualification, failed reconciliation, active kill switch, disabled LIVE trading, and disabled allow-orders all block before adapter invocation; all gates permit exactly one call. It also creates a passing snapshot, changes the persisted-control source, and proves the final refresh rejects the stale snapshot. PAPER/BACKTEST remain unaffected.

Executed `PYTHONPATH=src pytest -q` with 886 passed and 8 skipped, the focused runtime authorization suite with 11 passed, and `PYTHONPATH=src python -m alphaforge.config_audit` with PASS.

## Lifecycle, persistence, compatibility, and risk
No lifecycle, schema, persistence, reconciliation, or export contract changed. The control store remains authoritative and existing early kill-switch checks remain in place as defense in depth. Static manually supplied authorization mappings now fail closed. Phase 6 runtime mutation disablement remains unchanged, and LIVE remains NOT LIVE READY.

---

---

# Phase 9 Operational Acceptance Diagnosis Report (2026-07-23)

## Need and root cause
Operators lacked one database-wide, non-mutating view of campaign/continuation lineage, worker freshness, attachment identity, pending PAPER evidence, runtime reconciliation state, and conservative cleanup classification. Existing campaign commands bootstrap schemas and are campaign-specific, making them unsuitable as a forensic first action on an uncertain database.

## Files and behavior
`burnin_ops diagnose-db` opens an existing SQLite file with `mode=ro` and `query_only`, performs no bootstrap, and reports every campaign, active and historical continuations, PID/liveness/heartbeat, release/config/strategy/universe/execution-cost identity, open PAPER positions, pending order count or explicit unknown, pending reject labels, latest reconciliation/recovery state, and stale/orphaned continuation rows. Its plan only recommends archival for terminal campaigns with verified zero runtime/local exposure and no pending labels; ambiguous state remains manual review. Evidence deletion, unknown-to-zero conversion, and automatic LIVE/reconciliation clearing are explicitly disabled. Tests hash the database before and after diagnosis and verify fail-closed unknown exposure. `docs/KOMUTLAR.md` now provides argparse-compatible PowerShell and Bash flows.

## Lifecycle, persistence, compatibility, and migration
No lifecycle transition or database row is written by diagnosis. No schema, CSV, or evidence-package format changed, and no migration is required. The command tolerates databases without runtime lineage by reporting pending orders/reconciliation as unknown rather than zero. Existing launch, recovery, runtime, strategy, score, RR, and acceptance logic is unchanged.

## Tests, risks, and recommendation
The repository suite, compileall, config audit, dry-run config remediation, and diff checks are recorded in the delivery summary. Credentialed Demo reconciliation and a multi-day elapsed campaign cannot be claimed without operator credentials/runtime; therefore readiness remains blocked configuration/reconciliation until that evidence is complete. LIVE remains disabled and **NOT LIVE READY**. Push is appropriate for PAPER operational review only.

---

# PR #297 Historical Database Safety Correction (2026-07-23)

## Why and root cause
The first diagnosis assumed current runtime and campaign schemas. It could select absent historical columns, treated missing local evidence tables as empty collections, and allowed zero counters without proving snapshot lineage, freshness, authenticated reconciliation, or known exchange state. Those assumptions were unsafe for partially migrated databases.

## Behavior, persistence, and compatibility
Diagnosis now inventories every table and column with `PRAGMA table_info`, builds SELECT lists only from verified columns, and returns unavailable sources as `available=false`, `value=null`, plus structured `missing_schema` or `query_error` evidence. Local positions, pending labels, runtime positions/orders, orphan evidence, and reconciliation evidence have independent availability. ARCHIVABLE/RECOVERABLE requires every source, exact zero collections/counters, no query errors, campaign/run/release lineage, a fresh snapshot, known exchange state, no recovery action, authenticated COMPLETE evidence, and non-local/available read-only reconciliation. Otherwise it is MANUAL_REVIEW. Windows drive-letter and POSIX paths are encoded by a tested URI builder. No lifecycle, trading, recovery, reject, score, RR, persistence, schema, or LIVE control changes were made.

## Tests, migration, and remaining risk
Compatibility tests cover absent/historical runtime snapshots, absent counter columns, malformed JSON, absent local evidence tables, unrelated/stale/local-only/unknown-exchange snapshots, injected query failure, missing DB CLI behavior, checksum/schema/row/WAL/SHM immutability, and Windows/POSIX paths. No migration is required. Diagnosis intentionally cannot certify exposure when old databases lack authenticated, campaign-scoped runtime evidence. LIVE remains disabled and NOT LIVE READY.
## 2026-07-24 — Repository schema compatibility surgery

### Why / root cause
Persistence bootstrap, Alembic revisions, burn-in operational bootstrap, and runtime recovery had evolved independently. The canonical bootstrap created `positions.status` and `orders.status`, while recovery queried those columns directly and burn-in preflight initialized only its operational tables. Consequently, legacy databases could reach business queries before core migration. Existing migration bookkeeping also lacked checksums and success/details fields, and exposure query errors could carry a numeric zero beside an unavailable flag.

### Schema inventory (important owners)
- `persistence.init_db`: signals, decisions, lifecycle/evidence/review tables, positions, orders, fills, paper/backtest events, runtime state/control/reconciliation, release evidence, TimesFM evidence, expectancy/calibration, and Phase 7 burn-in tables.
- `burnin_campaign.bootstrap_campaign_schema`: Phase 8 campaign, run, observation, outcome, qualification, and resolver tables.
- `burnin_ops.bootstrap_ops_schema`: preflight, incidents, health, recovery drills, integrity, release decisions, and evidence hashes.
- `models.schema` plus Alembic `0001`–`0005`: SQLAlchemy production metadata and revision history. The SQLite runtime bootstrap remains a compatibility surface and is not replaced.
- `schema_doctor.RUNTIME_SCHEMA`: the single safety-critical registry for runtime exposure columns; SQLite introspection additionally reports every discovered column's type, nullability, default, and primary-key flag.

### Mismatches found
TABLE: `positions`; EXPECTED BY: runtime recovery/startup; ACTUAL SCHEMA: legacy `state`, `closed_at`, or `exit_time`; MISSING/MISMATCHED: canonical `status`; AFFECTED QUERIES: active exposure counts and startup snapshot load; RISK: raw OperationalError or false-clean recovery evidence; FIX: additive `status` migration with explicit legacy adapter and index.

TABLE: `orders`; EXPECTED BY: runtime recovery/startup; ACTUAL SCHEMA: legacy `order_status`; MISSING/MISMATCHED: canonical `status`; AFFECTED QUERIES: pending order counts and startup snapshot load; RISK: hidden pending exposure; FIX: additive `status` migration/backfill and index.

TABLE: `schema_migrations`; EXPECTED BY: auditable centralized migration; ACTUAL SCHEMA: `version/applied_at/notes`; MISSING/MISMATCHED: name, checksum, success, details; RISK: unverifiable partial history; FIX: additive bookkeeping columns and a checksummed version row.

PATH: CLI `--db`, `ALPHAFORGE_DB_PATH`, and persistence URLs could resolve differently; FIX: doctor/preflight report the absolute SQLite target and preflight bootstraps that exact file.

### Runtime, lifecycle, persistence, export/schema impact
Runtime and burn-in now migrate then validate before safety queries. Known legacy states retain rows and gain a canonical status; missing fresh tables are created empty. Ambiguous shapes and type mismatches are never rewritten. Lifecycle and export schemas are unchanged, so CSV/JSON contracts and lifecycle ordering are unaffected. No table/column is dropped and no user row is deleted.

### Migrations
- `2026_07_24_runtime_exposure_v1`: creates missing exposure tables, adds identifier/status columns, maps only recognized legacy evidence, adds status indexes, and records checksum/success/details. It is transactional and idempotent.

### Tests added/executed
Added fresh/current/legacy shapes, active/closed exposure, missing and unsupported shapes, wrong type refusal, repeat application, injected rollback, preservation, CLI check/apply, path behavior, inventory metadata, and static raw-SQL registry tests. Targeted schema/bootstrap/burn-in suites passed (95 passed, 3 skipped). The full suite reached 924 passed and 14 skipped; four Alembic graph tests could not import the Alembic package in the current environment (the repository's local `alembic/` namespace was found, but the declared dependency was not installed).

### Risks / limitations / migration concerns
PostgreSQL and arbitrary ORM drift are reported by existing Alembic checks but are outside this SQLite doctor. Nullable legacy status rows are not guessed; they remain non-active but schema-valid, so operators should validate data semantics before LIVE consideration. Foreign-key reconstruction and type changes are destructive in SQLite and intentionally require a manual plan. Static SQL checking targets the safety-critical positions/orders class rather than attempting to parse all dynamic SQL.

### Push recommendation
Push for PAPER/BACKTEST validation after the full test suite passes. Do not authorize LIVE trading based on this patch alone.
## 2026-07-24 — PR #302 fail-open and migration-integrity closure

### Need and root cause
The first schema doctor revision validated column shapes but still used `COALESCE(status,'')` for exposure, created missing exposure tables in ordinary apply mode, trusted an existing migration version without validating checksum/success, and declared `id` required without an explicit legacy policy. Those gaps could turn unknown state or a wrong database path into apparent zero exposure.

### Files and behavior changed
- `schema_doctor.py` now owns recognized active/terminal state registries, affected-row diagnostics, non-mutating inspection, explicit `allow_fresh_bootstrap`, checksum/failed-history verification, identifier fail-closed policy, semantic post-backfill validation, row-count invariants, and validated exposure counting.
- `persistence.init_db` proves file freshness before broad bootstrap; existing files are validated/migrated before any missing exposure table could be created.
- `runtime_state.evaluate_runtime_recovery` consumes `exposure_count`; unknown state, invalid schema, checksum failure, or query failure leaves availability false and blocks recovery.
- Burn-in and SQLite bootstrap fixtures now initialize canonical exposure schema explicitly. New runtime-state tests exercise terminal, active, and unknown recovery evidence.

### Lifecycle, persistence, schema, and compatibility impact
No table, column, or row is removed. Recognized legacy `state`, `closed_at`, `exit_time`, and `order_status` values remain additively migratable. NULL/blank/unrecognized source or result values roll back, do not record migration success, and return `UNKNOWN_EXPOSURE_STATE` plus affected row IDs. Missing legacy identifiers return `IDENTIFIER_COLUMN_MISSING`; no surrogate identity is invented. Existing unrelated databases return `DATABASE_IDENTITY_UNVERIFIED`/`EXPOSURE_TABLES_MISSING`. Fully recognized terminal states are authoritative zero exposure; active states remain blocking exposure.

### Migration integrity
Existing `2026_07_24_runtime_exposure_v1` rows retain and are checked against their original checksum; this follow-up is recorded separately as `2026_07_24_runtime_exposure_v2`. Every known row must have the version-specific checksum and successful state. Mismatch returns `MIGRATION_CHECKSUM_MISMATCH`; false/missing success returns `MIGRATION_PREVIOUSLY_FAILED`. Successful v2 details record pre/post row counts, affected rows, and additive intent. Migration and semantic checks remain transactional and idempotent.

### Tests and risks
Added NULL/blank/unknown position and order states, terminal/active groups, unrelated/wrong paths, non-mutating check/apply, explicit fresh bootstrap, checksum mismatch, prior failure, missing identifiers, semantic rollback, and runtime recovery tests. Targeted schema doctor, Phase 9, runtime-state, runtime, and SQLite bootstrap suites pass. Full local suite still requires Alembic; package installation was attempted but blocked by the environment's 403 network policy. CI must pass the complete declared dependency suite before merge. PostgreSQL schema-doctor parity and ambiguous manual data mappings remain known limitations. LIVE remains NOT READY.

### Push recommendation
Update PR #302 for CI validation. Do not merge until the full CI suite, including Alembic revision tests, passes.

---
## 2026-07-24 — PR #302 Alembic-head compatibility correction

### Root cause and exact Alembic schema
Repository revision `0001_phase1_init` defines domain `positions(id BIGINT PK, symbol_id BIGINT FK NOT NULL, side enum/VARCHAR NOT NULL, size NUMERIC(20,10) NOT NULL)` and `orders(id BIGINT PK, order_intent_id BIGINT FK NOT NULL, external_order_id VARCHAR(128), status VARCHAR(24) NOT NULL)`. Revision `0005_core_identifier_normalization` additively supplies nullable lifecycle identifiers: positions receive `position_id`, `signal_id`, `symbol`, `timeframe`, `mode`, `created_at`, `updated_at`; orders receive `order_id`, `signal_id`, `position_id`, `symbol`, `timeframe`, `mode`, `created_at`, `updated_at`. These are order-intent/domain persistence models, not the lightweight runtime exposure contract (`qty` plus lifecycle `status`). Literal type comparison also incorrectly rejected SQLite-compatible `BIGINT`/`VARCHAR` declarations.

### Selected compatibility design
The preferred separate-surface design was selected. A known `alembic_version=0005_core_identifier_normalization` plus expected core tables and exact domain identifiers establishes trusted `ALEMBIC_HEAD` identity. Empty trusted databases add dedicated `runtime_positions` and `runtime_orders`; runtime recovery and `exposure_count` select those tables. The Alembic domain tables are not polluted with meaningless nullable `qty/status` columns. If either domain table already contains rows before adapter establishment, migration blocks with `ALEMBIC_DOMAIN_EXPOSURE_REQUIRES_RECONCILIATION` rather than claiming zero runtime exposure. Unknown revisions and foreign shapes remain blocked.

### Migration and persistence impact
`2026_07_24_runtime_exposure_v3` is a new checksummed migration; v1/v2 checksums are unchanged and still verified. Evidence records detected schema family, Alembic revision, chosen adapter, columns/tables added, pre/post row counts, affected rows, and semantic outcome. All changes remain additive and transactional. Both `init_db → Alembic head → init_db` and `Alembic head → init_db → Alembic head` preserve identifiers and data without drops or rewrites.

### Tests, remaining risk, and push recommendation
Added affinity tests for BIGINT/INT/SMALLINT, VARCHAR/CHAR/CLOB, FLOAT/DOUBLE/REAL and NUMERIC; compatible declared-schema validation; trusted/foreign Alembic identities; empty adapter initialization; domain-row fail-closed behavior; v3 evidence and idempotency; and preserved checksum enforcement. Local targeted results: schema doctor 41 passed; SQLite bootstrap 14 passed and 3 Alembic-dependent skips; runtime state 3 passed; Phase 9 ops 70 passed. This environment still lacks the Alembic distribution, so the two unchanged mixed-bootstrap tests cannot execute locally. GitHub Actions has not yet reported for this commit. Push to update PR #302, but do not merge until Actions reports the full suite with zero failures. LIVE remains NOT READY.

---

---
## 2026-07-28 — PR #307 merged dev HEAD verification

### Why, root cause, and files changed
The post-merge audit was required to bind the current dev merge SHA to dependency, import,
test, compile, configuration, whitespace, and Actions evidence. No product defect was
identified. The new `docs/audits/PR307_DEV_HEAD_AUDIT.md` and SHA-keyed evidence directory
preserve the exact outputs; VERSION, REPORT, and CHANGELOG receive documentation-only
updates.

### Runtime, lifecycle, persistence, export/schema, and compatibility impact
None. Strategy, sizing, thresholds, scoring, RR, lifecycle, database schema, exports, and
LIVE controls are untouched. There is no migration concern. Constant RR/score were not
reopened because the current tests produced no recurrence failure and no new empirical
distribution was sampled.

### Tests executed and risks
The preprovisioned Python 3.12.13 environment produced 1072 passed, 0 failed, and 3 skipped;
Alembic 1.18.5 imported, compileall and diff checks passed. The exact config command failed
because the package was not installed, while the diagnostic source-path invocation passed.
The exact Actions Python 3.11 dependency sequence was attempted, but both requirements and
fallback installs failed on proxy HTTP 403. The same network restriction prevented a
GitHub run-ID lookup. These limitations are recorded as failures/unverified evidence, not
hidden. Push is recommended only for the audit record; LIVE remains NOT LIVE READY.

## 2026-08-08 — PR #313 canonical Control Center correction

The collection failure was caused by a nested escaped f-string inside reject pagination SQL, which Python 3.11 rejects. The selected-column fragment is now built separately. The response envelope had also fabricated fresh evidence from response time, worker health omitted canonical attachment identity, status inferred contamination from a run name, historical ordering assumed optional timestamp columns, and resume acquired its lease after precondition reads. These were corrected without changing canonical recovery code: persisted source timestamps now drive explicit freshness states; worker health requires campaign/run/PID/start/heartbeat/attachment identity; contamination is unavailable; schema-selected ordering fails closed; and lease acquisition precedes fresh canonical preconditions and CLI execution.

Control Center does not terminalize or update recovery state and exposes no recovery mutation endpoint. PR #312's `alphaforge.burnin_ops` path remains authoritative. SQLite reads remain read-only; audit and lease artifacts are the only writes. Tests now cover Python 3.11 grammar, freshness absence/staleness/clock skew, contamination non-inference, FAILED/multiple campaigns, attachment ambiguity, PID absence/death, reject/history compatibility, pause/resume argv and postconditions, audit failure, lease ownership, CORS validation, CLI help, and runtime DB non-mutation. LIVE remains out of scope and NOT READY.

Final verification evidence: `pytest -q tests/test_control_center_api.py` passed 48/48; `pytest -q tests/test_dashboard_app.py tests/test_dashboard_settings.py` passed 50/50; `pytest -q tests/test_phase8_burnin_campaign.py tests/test_phase9_burnin_ops.py tests/test_runtime_heartbeat.py` passed 111/111; and `pytest -q` passed 1145 with 3 pre-existing skips and 0 failures. After the final future-attachment guard, the focused 48-test suite passed again. `python -m compileall -q src tests`, Python 3.11 `py_compile`, `PYTHONPATH=src python -m alphaforge.control_center --help`, and `git diff --check` passed. A full Python 3.11 import could not run locally because that interpreter has no FastAPI/Pydantic dependencies and network installation is blocked by proxy 403; Python 3.11 grammar compilation passed and CI with installed dependencies remains the authoritative import check. GitHub API/Actions status lookup was likewise blocked by HTTP 401/403, so no green CI claim or merge recommendation is made.

## 2026-08-09 — PR #313 targeted finishing pass

The remaining production-safety gaps were implicit localhost CORS trust and composite responses being labeled unavailable when they intentionally had no single evidence timestamp. CORS now accepts only explicitly configured exact origins, with unset/empty configuration producing an empty allowlist without breaking same-origin use. Composite health/runtime/campaign/control envelopes now use `MULTI_SOURCE` with null aggregate time fields while preserving component freshness. Confirmed process existence is labeled `PROCESS_PRESENT`, not `ACTIVE`; the stronger attachment-based `HEALTHY` contract is unchanged. No recovery, persistence, runtime mutation, trading, lifecycle, reject, lease, CLI, or audit authority changed.

Verification: `pytest -q tests/test_control_center_api.py` passed 52; dashboard/settings passed 50; Phase 8/9/heartbeat regressions passed 111; full `pytest -q` passed 1149 with 3 pre-existing skips and 0 failures. `python -m compileall -q src tests`, `PYTHONPATH=src python -m alphaforge.control_center --help`, and `git diff --check` passed. The exact module command without `PYTHONPATH` was executed but failed because this checkout uses an uninstalled src-layout package; the tested entry point itself passes with the source path, while an installed CI environment remains authoritative. No Git remote is configured in this container, so the new head could not be pushed and its GitHub Actions status could not be queried; merge remains blocked pending green required CI on the pushed head.
# Issue #309 Phase B technical surgery — 2026-08-10

## Need and root cause

Phase A intentionally registered no stage handlers, so every Market, Signal,
and Quality row was a generic skip. The runtime snapshot was persisted safely
but could not explain regime, score construction, geometric RR, quality gates,
or divergence from the legacy reject. Follow-up source investigation identified
the exact production placeholder in `RuntimeOrchestrator._build_signal`
(`float(market_ctx.get("rr", 2.0) or 2.0)`). Phase B does not consume that RR:
it requires entry/SL/TP geometry and returns explicit incomplete/no-candidate
evidence when geometry is unavailable. Fixed RR values also exist in tests,
rollback evidence, and deterministic qualification-probe samples. No production
Phase-B `score = 0.8` assignment exists.

## Files and behavior

`agents/phase_b.py` adds immutable-snapshot adapters. Market records justified
canonical regimes, freshness, observed execution context, and availability.
Signal consumes the canonical AIBrain score and component evidence from the
immutable snapshot (and defers when it is absent) and computes RR from
side-aware entry/SL/TP geometry. Quality reuses `evaluate_trade_quality`,
retains all failed gates plus the legacy hard reason, and classifies parity as
MATCH/PARTIAL_MATCH/MISMATCH/UNAVAILABLE. `runtime.py` registers these adapters
only in the existing opt-in shadow worker and publishes bounded counters.
`agents/persistence.py` adds duplicate-safe `agent_phase_b_evidence` normalized
columns with JSON only for component/availability/reason detail.

## Safety, lifecycle, compatibility, and migration

The graph remains disabled by default and shadow-only. It has no exchange,
order, position, risk-budget, threshold, pause/resume, recovery,
reconciliation, or campaign dependency. Exceptions and persistence failures
remain diagnostic. Legacy decisions and lifecycle rows are unchanged; the
isolated store alone gets an idempotently-created additive table. Phase-B
evidence carries SIGNAL_CREATED/SIGNAL_REJECTED semantics without writing the
canonical lifecycle. No CSV schema changes exist.

## PAPER explainability and remaining risk

Operators can now distinguish incomplete/no candidate, low component score,
invalid/low geometric RR, regime/quality rejection, missing execution context,
expectancy failure, and legacy/graph parity. Thus a 100% PAPER reject run is
query-explainable for new traces; incomplete historical snapshots remain
explicitly UNAVAILABLE rather than retrospectively reconstructed. Phase C+
Risk/Execution/Verification/Reflection/Portfolio behavior and every cutover
remain unimplemented. This is not LIVE readiness.

## Tests

Focused Phase-B tests cover determinism, freshness, null preservation, regime
mapping, canonical AIBrain score parity, RR geometry/variability, invalid/no
candidate, quality/legacy reject preservation, parity, and duplicate-safe SQL.
Requested regressions and the full suite are executed before push; merge is
recommended only if those results and CI are green.

Follow-up review removed the independent Phase-B weighting formula, preserves
an observed `effective_rr=0.0` instead of falling back to raw RR, labels which
quality checks executed, and prevents incomplete execution context from being
presented as validated. Repository inspection found volatility/trend feature
builders but no pure canonical classifier for the issue-309 regime vocabulary;
MarketAgent therefore explicitly reports observed normalization provenance and
does not claim to have classified the regime.

Follow-up verification passed 10 Phase-B tests, 14 contract/orchestrator/
persistence tests, 45 runtime/concurrency tests, and 134 Phase-8/Phase-9 tests.
The heartbeat selection is now clean after accounting for whole-second storage
quantization in the test only; runtime freshness policy is unchanged. The full
suite completed with 1,181 passed and 6 skipped; its only four failures are
Alembic imports. Installing the declared `alembic>=1.13,<2.0` dependency was
attempted but the environment package-index tunnel returned HTTP 403, so this
is separately recorded as environment-unavailable rather than a code pass.

Execution result: the requested Phase-B/contract/orchestrator/persistence/runtime/
Phase-8/Phase-9 selection completed with 197 passes and one timing-sensitive
heartbeat freshness failure (measured age 1.080632s against a 1s assertion);
the exact failing test passed immediately in isolation. The full suite completed
with 1,179 passed, 6 skipped, and four failures because the environment lacks
the declared Alembic package (`ModuleNotFoundError: alembic.config` / missing
`alembic.command`), an already documented repository environment limitation.
# PAPER reject forward-outcome integrity gate — 2026-08-16

## PR #319 merge-blocker recheck

The validator initially indexed reviews only by `reject_decision_id`, while the authoritative resolver permits a legacy review with `reject_decision_id IS NULL` to match the pending row's `signal_id`. That mismatch could falsely report `ORPHAN_PENDING_LABEL` and omit the synchronized legacy review from reject-quality aggregation. The validator now applies the resolver's deterministic precedence without mutation: exact decision matches first; only if none exists may null-decision reviews match by signal. Explicit conflicting decisions never fall back by signal. Multiple eligible legacy reviews, or multiple exact reviews in a damaged database, produce `AMBIGUOUS_REVIEW_LINKAGE` plus `DUPLICATE_REJECT_IDENTITY` and fail closed. Aggregation consumes this resolved pending-to-review mapping.

Regressions cover legacy null-decision linkage before and after resolver synchronization, explicit mismatch isolation, ambiguous duplicate legacy matches, quality aggregation through the legacy mapping, and unchanged PR #318 null timeframe/horizon-bar preservation. No resolver, schema, lifecycle, trading, threshold, execution, or graph behavior changed.

Focused recheck results: the hardened validator suite passed 17 tests; the required resolver/runtime/SQLite bootstrap selection passed 73 tests with 3 skips; compileall and diff checks passed. The complete local suite reached 1,215 passed and 6 skipped. Its four failures remain limited to importing the absent Alembic distribution in `tests/test_alembic_revision_graph.py`; installation could not reach the package index through this container's HTTP 403 proxy. This checkout has no Git remote, GitHub credentials, or accessible Actions API, so run #1509's exact hosted traceback and a post-push full CI result cannot be independently represented as verified here. GitHub Actions with its successfully imported Alembic dependency remains the merge gate.

## Need and root cause

PRs #317 and #318 established authoritative reject review/pending/outcome persistence and legacy SQLite compatibility, but operators had no single deterministic proof that those rows remained one-to-one, mature, internally consistent, and suitable for reject-accuracy analysis. Raw counts could incorrectly mix incomplete, ambiguous, or execution-invalidated evidence and could not distinguish normal early-campaign incompleteness from corruption.

## Narrow implementation

`reject_label_status.py` provides a SQL-first, read-only validator scoped to the persisted campaign identity or canonical `standalone:<burnin_run_id>` identity. `burnin_ops.py` exposes it without invoking mutating bootstrap. It reports identity cardinality/orphans, every resolver state, stale claims, overdue/oldest unresolved work, latest successful resolution, invalid correctness labels, missing excursions, canonical synchronization, geometry, and nullable per-reason quality metrics. Accuracy and correctness-derived counts use only complete, unambiguous, execution-valid evidence. Concrete reason codes classify retryable maturity/provider conditions as `INCOMPLETE` and structural/impossible evidence as `FAIL`.

## Persistence, lifecycle, export, and compatibility

The command opens SQLite with `mode=ro` plus `PRAGMA query_only`; repeated reports perform no initialization, schema migration, repair, deletion, or evidence update. Operators must run the existing schema doctor/bootstrap separately after deployment. The pre-#317 regression runs current bootstrap, proves PR #318 columns are present, preserves null timeframe/horizon bars and the original `horizon_seconds`, then validates the database. No schema or export changes exist. Lifecycle, reject decisions, resolver claims/outcome semantics, execution, thresholds, sizing, LIVE authorization, and agent-graph authority are unchanged.

## Tests, risks, migration, and recommendation

Focused tests cover a healthy correct reject, immature/no-outcome and incomplete-window states, duplicate/orphan identities, resolved-without-outcome, stale claims, exclusion of invalid/ambiguous/incomplete evidence, impossible correctness, legacy bootstrap, and byte-stable repeated validation. Remaining limitations are honest: review-only orphans are campaign-scoped using persisted review payload identity, old rows without that identity cannot safely be attributed; null legacy timeframe/horizon bars are never invented; provider outages remain operationally incomplete. Run schema doctor after deploying, preserve the database, then run this gate. Do not begin Issue #309 Phase C until the intended fresh PAPER identity returns `PASS` with a representative quantity and distribution of mature, complete, execution-valid outcomes—not merely one passing row. LIVE remains NOT READY.

Push recommendation: merge only after all required focused and full CI checks are green; this observability patch does not justify parameter tuning or Phase C cutover.

Verification in this environment is recorded in the merge-blocker recheck above. CI with declared dependencies must be green before merge.

---
# Phase C0 complete PAPER reject-denominator surgery — 2026-08-16

PR #319 began from pending labels, allowing one mature label to produce PASS while other persisted rejects had never become labelable. The root cause was a pending-first query boundary rather than reconciliation of the requested campaign lineage or standalone run.

`src/alphaforge/reject_label_status.py` now derives authoritative run membership, scopes PAPER rejected burn-in observations by that membership, and reconciles their stable reject-decision identities with reviews, pending labels, incomplete-geometry audit observations, and canonical outcomes. It reports all requested denominator, coverage, status, execution-invalidated, and accuracy-eligible counters. Missing eligible pending ownership and duplicate ownership are FAIL conditions. Incomplete geometry/cost evidence and FAILED, AMBIGUOUS, or execution-invalidated populations explicitly block PASS as INCOMPLETE; structural contradictions remain FAIL. Campaign continuation membership and standalone run membership are isolated, and unrelated history is not inferred from timestamps or signal coincidence.

The validator performs SELECT/PRAGMA operations only. There are no schema/export changes, migrations, backfills, threshold changes, resolver mutations, lifecycle writes, production/order-path changes, agent handlers, BACKTEST cutover, or LIVE authorization changes. Pre-#317 databases remain supported through the existing additive bootstrap; unavailable source evidence remains null rather than fabricated.

`tests/test_reject_label_status.py` adds production-shaped coverage for one good label beside incomplete rejects, eligible missing-label failure, explicit failed/ambiguous/execution-invalidated blocking, complete denominator counts, campaign/standalone isolation, unrelated-history exclusion, idempotent zero-write validation, and a fully mature PASS fixture. Required focused, compatibility, full-suite, compile, and diff checks are recorded in the delivery response.

Compatibility risk is limited to intentionally stricter gate results: evidence sets that previously passed with partial coverage can now be INCOMPLETE or FAIL. No data migration is required. Push recommendation: merge as the final Phase C0 evidence gate only after CI confirms the full suite; do not infer Phase C or LIVE readiness.

---
# Phase C0 production evidence and state-consistency correction — 2026-08-16

The first complete-denominator patch assumed production observations already contained `metrics_json.reject_decision_id`; the authoritative runtime persistence call did not supply it. Consequently, a valid normal PAPER reject could be counted as an additional unidentified row and forced to FAIL. The validator also lacked a general maturity blocker when future-due work coexisted with one mature result, and did not validate all pending/outcome status combinations.

`runtime.py` now adds canonical reject-decision ID, signal ID, and available campaign/standalone runtime identity to the existing PAPER burn-in observation metrics and provenance. The burn-in run remains authoritative in its existing column. This changes evidence shape only: decisions, thresholds, lifecycle authority, resolver calculations, orders, adapters, BACKTEST, and LIVE behavior are untouched.

`reject_label_status.py` deduplicates identified rejects strictly by canonical reject-decision ID. Legacy unattributed observations are reported separately, excluded from the exact identified total, and block Phase C as INCOMPLETE rather than becoming invented distinct rejects or structural FAILs. Every eligible PENDING, READY, or RESOLVING label now emits `IMMATURE_LABELS_PRESENT`; PASS requires mature coverage of 1.0. State validation fails on unknown statuses, unresolved labels with outcomes, invalid RESOLVED outcomes, AMBIGUOUS without matching ambiguity, FAILED with complete outcomes, and pending-row completeness contradictions.

Regression coverage exercises the real `RuntimeOrchestrator._persist_reject` path, legacy unattributed evidence, one mature result beside future-due labels, and terminal state contradictions. No schema/export migration or evidence backfill is required. Existing legacy evidence remains immutable and explicitly incomplete. Push recommendation: update PR #320 only after all local checks and GitHub Actions are green; do not merge or infer LIVE readiness before then.

---
# Alembic 0007 normalized runtime lifecycle schema repair — 2026-08-18

## Why the patch was needed and root cause

An SQLite database stamped at Alembic `0006_reject_label_identity_timeframe` could retain a mixed `trade_lifecycle_events` table and fail on the first normalized `SIGNAL_CREATED` insert. Investigation confirmed that `0005_core_identifier_normalization` was intentionally scoped to identifier columns on a list of other domain tables: it did not include `trade_lifecycle_events`, normalized lifecycle evidence, or runtime upsert indexes. Its successful application therefore truthfully reported completion of its narrow identifier task, not lifecycle readiness. The independent `schema_migrations` runtime-exposure v4 success check was also scoped to position/order exposure adapters. The actual defect was weak aggregate startup success criteria: schema doctor validated exposure and pending-label shapes but did not validate the lifecycle writer's complete SQL contract or conflict targets.

## Files and exact behavior changed

`alembic/versions/0007_repair_runtime_lifecycle_schema.py` follows 0006 and conservatively adds only missing nullable columns consumed by `save_trade_lifecycle_event()`. It preserves every existing row, legacy column, `event_payload`, and independent `payload`. Exact canonical values already present in `state` are the sole lifecycle-state backfill; unrecognized states remain NULL. No `event_ts`, `created_at`, decision, reject reason, score, RR, expectancy, or execution context is invented. No table rebuild is used.

Before either unique index is created, 0007 queries duplicate non-NULL identities and aborts with the offending identity/count diagnostics when uniqueness is unsafe. NULL `event_id` and nullable composite-key legacy rows remain legal under SQLite UNIQUE semantics. The migration creates UNIQUE `(event_id)` and UNIQUE `(signal_id,event_ts,lifecycle_state)` targets required by the writer's two `ON CONFLICT` statements.

`src/alphaforge/schema_doctor.py` now inspects all lifecycle persistence columns and their SQLite affinities, discovers ordered unique index targets through PRAGMA metadata, and returns `BLOCKED` for missing columns or conflict targets even when `alembic_version` is a recognized head. `burnin_ops.preflight` already treats any non-VALID doctor result as a critical failing `schema_current` check, so launch remains blocked without suppressing persistence errors.

## Persistence, lifecycle, export/schema, and compatibility impact

New runtime writes become possible after upgrade and retain existing idempotent upsert behavior. Historical rows and payloads are unchanged except the safe canonical-state copy described above. Lifecycle ordering logic, reject decisions, CSV exports, execution modeling, BACKTEST/PAPER decision parity, and persistence error propagation do not change. The additive nullable columns are backward compatible; the new uniqueness requirements can expose pre-existing contradictory evidence rather than rewriting it. No migration concern warrants a destructive rebuild.

## Tests executed and remaining limitations

Regression coverage constructs the reported exact mixed lifecycle shape at revision 0006, proves pre-upgrade doctor blocking, upgrades twice through normal Alembic execution, verifies every required column and both conflict targets, checks row/payload/null preservation and conservative state backfill, persists a real lifecycle event, and proves post-upgrade validation. A separate duplicate-identity regression proves fail-closed diagnostics and retained revision/row count. Revision-graph expectations now identify 0007 as head.

Legacy rows lacking trustworthy timestamp evidence intentionally retain NULL `event_ts` and `created_at`. Operators must investigate duplicate identities before retrying an aborted upgrade; automated deletion, identity rewriting, or evidence merging would be unsafe. LIVE readiness remains blocked pending the broader established readiness criteria.

## Migration and push recommendation

After backup and merge, stop writers and run exactly: `git pull`, `alembic upgrade head`, the canonical `preflight`, then `launch` only if preflight returns PASS. Recommend merge after the focused and full suites pass; this repair does not itself establish LIVE readiness.

---

### Verification result

`tests/test_env_wiring_contract.py` passed all 124 tests. The full local suite completed with 1,259 passed and 6 skipped; its six failures are limited to Alembic imports because the Alembic distribution is absent from this environment. The behavioral wiring change itself is green, so the existing review thread is ready to resolve in the PR host after push.
## Post-PR-#328 campaign universe regression surgery — 2026-08-20

### Need and root cause

Campaign symbols were hashed and persisted but never applied as an allow-list to the broad multi-provider scanner result. `max_symbols_per_scan=5` therefore selected up to five Binance USD-M symbols regardless of the two-symbol campaign identity. The qualification reader also did not compare persisted decision symbols with campaign identity. Geometry enrichment collapsed provider and deterministic failures to `{}`, so the 56 historical Binance incomplete rows cannot be retrospectively separated into timeout, malformed payload, insufficient rows, invalid OHLC, zero risk, or invalid target without mutating evidence.

### Files and behavior

`runtime.py` loads authoritative symbols and provider identity from the attached campaign, filters and symbol-deduplicates before canonical selection/enrichment, and rechecks scope before processing, reject persistence, and PAPER execution. Violations raise `CAMPAIGN_UNIVERSE_RUNTIME_MISMATCH` and append a campaign event containing campaign/run, declared scope, observed symbol/provider, and stage. `reject_label_status.py` remains read-only and SQL-first while reporting declared/observed symbols and providers, mismatch populations/counts, and structural FAIL reasons. `burnin_campaign.py` gives newly created PAPER campaigns explicit Binance read-only provider identity by default.

`signal_geometry.py` keeps `build_breakout_geometry` backward compatible and adds a diagnostic variant. `exchange_market_scanner.py` records `COMPLETE`, `UNAVAILABLE`, or `INVALID`, a stable reason, and `BINANCE_CLOSED_1M_KLINES`; it neither retries nor synthesizes geometry. Runtime burn-in observations retain these diagnostics and candidate source exchange. Tests cover bounded allow-list behavior, late defense, historical read-only detection, and fail-closed geometry classifications.

### Lifecycle, persistence, compatibility, migration, and risks

No table, CSV, threshold, score, RR, reconciliation, authorization, or LIVE behavior changed; no migration is required. Out-of-scope candidates create no decision/reject/pending-label/order rows. One diagnostic campaign event is intentionally written only for a late runtime invariant violation. Historical rows and the contaminated campaign are not repaired. Because old rows contain only missing fields, the exact cause of each of the 56 Binance failures is unrecoverable; future attempts are classifiable. Start a new release/preflight/campaign and verify observed symbols/providers before qualification. LIVE remains NOT READY.

### Push recommendation

Focused required regressions passed 280 tests. The full suite completed with 1,265 passed and 6 skipped; six Alembic tests failed because the Alembic Python distribution is absent, and one backtest compatibility test exposed an overly strict diagnostic check. That compatibility defect was corrected and its 151-test backtest/scanner regression passed afterward; the environment-only Alembic failures remain. Compileall and diff checks pass. Never resume `camp_e902c3018c2eb1fd` as qualification evidence.

---
## PR #329 provider identity binding follow-up — 2026-08-20

### Why and root cause

PR #329 correctly used campaign provenance as the executable provider allow-list, but provider scope was not hashed. Mutating `source_provenance_json` could therefore alter executable behavior while retaining the campaign ID. The correction normalizes only stable exchange identity—`paper_source_exchanges`, currently `["binance"]`—and places it in the existing Phase 8 `config_payload`, hence `config_hash` and `campaign_id`.

### Runtime, persistence, lifecycle, and compatibility

Campaign creation requires normalized provenance scope to equal the requested identity scope. Runtime independently builds its expected Binance PAPER scope, compares it with persisted provenance, and terminalizes attachment with `PHASE8_CAMPAIGN_PROVIDER_DRIFT` on disagreement. The existing pre-selection symbol/provider filter, symbol deduplication, post-selection geometry bound, and processing/persistence/execution defenses remain unchanged. No threshold, RR, score, lifecycle, reconciliation, LIVE authority, table, or export changed.

### Tests, migration, risks, and recommendation

Tests cover config-hash divergence between Binance and Hyperliquid, creation mismatch, mutated-provenance attachment drift, direct Hyperliquid-identity rejection by the independently Binance-scoped runtime, same-symbol provider filtering with zero database side effects, read-only provider contamination failure, and explicit `KLINE_TIMEOUT`. The focused set passed 136 tests. The full suite produced 1,272 passed and 6 skipped; six failures were the environment's missing Alembic distribution and one unrelated heartbeat timing test passed immediately when rerun alone. No migration is required, but identity semantics require a fresh release/preflight/campaign. Never resume the contaminated historical campaign. LIVE remains NOT READY.

---
# Database Doctor v1 surgery report — 2026-08-29

## PR #331 merge-blocker correction

Writer probes now use the shared SQLite online-backup snapshot primitive rather
than a filesystem copy, capturing committed WAL content without checkpointing
or mutating the source. The lifecycle migration inventories all deployed
columns, indexes, triggers, checks, uniques, and foreign keys before creating a
replacement; objects outside the explicitly understood canonical/legacy set
block before destructive DDL. Before dropping the old table, ordered old/new
values are compared exactly and independently hashed with deterministic SHA-256
evidence digests. Repair reaches `REPAIRED` only when post-migration structural
diagnosis and actual lifecycle, decision, heartbeat, and state writers pass on
the safe snapshot. The lifecycle probes now cover creation, idempotent upsert,
and a distinct valid rejection transition.

## Need and root cause

Alembic revision 0001 declared `trade_lifecycle_events.id` as `BIGINT PRIMARY KEY`.
SQLite grants implicit rowid allocation only to the exact `INTEGER PRIMARY KEY`
declaration, while the production writer intentionally supplies no surrogate
ID. The same legacy table also required `order_intent_id` and `event_payload`,
which the current writer does not populate.

## Patch and runtime behavior

- Added `alphaforge.db_doctor` commands for diagnose, plan, repair, and certify,
  including JSON output, canonical path/file identity, inventory, integrity,
  migration identity, actionable issue evidence, and real persistence probes.
- Added Alembic `0008_database_doctor_lifecycle_contract`. SQLite rebuilds the
  lifecycle table with an autoincrement rowid PK, nullable legacy evidence
  columns, and both canonical unique identities. PostgreSQL receives an
  explicit owned sequence/default without SQLite syntax.
- Repair uses SQLite's online backup API before migration and validates backup
  existence and integrity. Duplicate identities, corrupt databases, ambiguous
  identity, failed backup, and failed probes remain blocked.

## Lifecycle, persistence, export, and compatibility

Historical rows, supplied numeric IDs, legacy order-intent IDs, and legacy
event payloads are copied without synthesizing canonical event evidence. Row
counts and uniqueness are verified before replacement. No CSV/export schema,
trading threshold, rejection, execution, expectancy, risk, PAPER decision, or
LIVE authorization behavior changes. No migration is required outside Alembic
upgrade head; downgrade is intentionally non-destructive/no-op.

## Tests and risks

Focused tests cover current bootstrap, actual 0001 history, exact 0007 writer
failure, repaired persistence, row preservation, duplicate fail-closed behavior,
read-only missing-path diagnosis, validated backup, and real writer
certification. The focused suite passed. Full-suite results are recorded in the
delivery summary. A historical Alembic-only database can still expose unrelated
optional writer-table drift; certification deliberately reports that condition.

## Push recommendation

Push for PAPER database remediation after backup retention has been verified.
Do not interpret database certification as LIVE readiness.
# Repository-wide Database Doctor contract audit — 2026-08-29

Lifecycle-only diagnosis could not prove compatibility across independently owned Alembic, init, runtime, burn-in, campaign, ops, and ORM schemas. The doctor now maintains a non-DDL audit registry, inspects all SQLite tables and features read-only, resolves configured targets, classifies exposure and adaptive generations, and returns writer compatibility and conservative repair classes. No migration, runtime trading behavior, lifecycle semantics, evidence row, export, or LIVE authorization changes. Ambiguous exposure is never zero and manual-review evidence is never merged or deleted. Tests cover target conflicts, conflicting exposure, and read-only preservation. Remaining multiple ownership and ORM/Alembic alignment should be handled in a narrow follow-up PR; no migration is required.

The follow-up replaces synthetic `MULTIPLE` ownership with real owner lists and source locations, compares shared ORM metadata to deployed table columns, primary keys, nullability, comparable defaults, and unique constraints, and derives `autogenerate_safe` from findings. The audited `init_db` family reports `exchange_symbols` absent while ORM/Alembic define materially different exchange/venue shapes. SQLAlchemy URL parsing preserves PostgreSQL and malformed candidates; Windows and POSIX path identities normalize equivalent SQLite URLs. Runtime SQL files are classified from explicit SQLite constructs. SQLite JSON capability is distinct from the Doctor connection's observed PRAGMAs and the application connection contract (`foreign_keys=ON`, WAL, 30-second busy timeout).

Focused repository-auditor tests passed 7 tests and schema/persistence/runtime/burn-in coverage passed 220 tests. Full collection remains environment-blocked because the declared Alembic package cannot be installed through the HTTP 403 package tunnel; no behavioral failure was dismissed. Remaining risk is that independent DDL owners are only diagnosed, not consolidated, and PostgreSQL runtime writers remain uncertified. Recommend the next PR establish one canonical metadata source and portable writer adapters without changing LIVE authority.

CI #1541 exposed that architectural findings were incorrectly treated as universal blockers. Each finding now carries explicit operation gates. ORM drift and compatible multiple ownership block autogeneration/consolidation but not lifecycle repair or writer probes; target and integrity failures block repair and certification; exposure ambiguity blocks PAPER certification; duplicate or unsupported lifecycle evidence blocks repair/migration. Certification reports `runtime_certification` separately from `repository_audit`. Generic NOT NULL inference against intentionally partial optional-table contracts was removed, restoring canonical `init_db` runtime health without hiding repository findings.

---
## Fresh-database contract reconciliation — 2026-08-29

### Why and root cause

A repository-owned fresh SQLite database could be judged against obsolete or unrelated contracts. Database Doctor attributed SQL-first runtime tables to both `init_db`, ORM metadata, and sometimes Alembic; treated the obsolete ORM surrogate `id` as authoritative for natural-key expectancy tables; described reconciliation incidents with a nonexistent `incident_id`; and lacked writer-specific proof for interpreting NOT NULL columns. The fresh path also omitted the canonical reconciliation/runtime-control setup and the runtime-state time index until a later subsystem call.

### Files and exact behavior

`db_doctor/contracts.py` now declares `signals`, the wide `order_decisions`, `ai_decision_features`, lifecycle, and natural-key expectancy tables as SQL-first `init_db` contracts. `symbol`, `setup`, and `regime` remain their canonical primary keys; no surrogate IDs were introduced. The reconciliation contract now exactly mirrors `persist_findings`: incident type, severity, symbol, lifecycle reference, remediation status, acknowledgement/fail-closed flags, forensic payload, and creation time. Writer-guaranteed columns are explicit and scoped only to the writer that owns each table.

`diagnostics.py` reports a NOT NULL writer conflict only for a no-default, non-PK column that the corresponding proven writer cannot guarantee. Valid required reconciliation, heartbeat, runtime-state, and persistence values no longer become false positives; unknown writer contracts are left to executable isolated probes rather than guessed. `orm_audit.py` excludes declared SQL-first tables from Alembic comparison and exposes that exclusion, while continuing to report unrelated ORM/deployment drift and keeping global autogenerate unsafe.

`persistence.init_db` orchestrates canonical reconciliation, runtime-control, and runtime-state schema functions after its transaction. This adds the genuinely required runtime-state timestamp index through the normal fresh path. Heartbeat remains conditionally provisioned by PAPER/LIVE code so BACKTEST does not acquire heartbeat evidence. Writer probes now directly exercise signal and reconciliation persistence in addition to decisions, lifecycle, heartbeat, and runtime state. The CLI loads migration-only repair/certification modules lazily, so read-only `diagnose` remains usable when the local Alembic distribution is unavailable.

### Lifecycle, persistence, export, schema, and compatibility

Lifecycle transitions, uniqueness, reject persistence, and fail-closed checks are unchanged. The patch is fresh-bootstrap additive and idempotent; it neither rebuilds nor deletes existing tables or rows and introduces no user-database repair as its solution. No CSV/export shape changes. Existing databases may gain only the already-canonical runtime tables/indexes when `init_db` runs. Alembic autogenerate must remain disabled because unrelated ORM-only/deployed tables still differ; that repository warning does not block PAPER certification.

### NOT NULL audit disposition

Signal IDs/timestamps, order-decision generated IDs and serialized evidence, lifecycle identities/state/timestamps/serialized evidence, heartbeat identity/mode/state/evidence, runtime-state timestamp/instance, and all strict reconciliation incident fields are supplied by their real writers. Their NOT NULL constraints are valid. A required no-default column outside a writer's guaranteed set remains a CRITICAL `NOT_NULL_WRITER_CONFLICT`. Runtime control, burn-in, and adaptive learning are not statically guessed where a complete guarantee contract is not declared; real-writer probes and their own schema functions remain authoritative.

### Tests, risks, migration, and recommendation

Regression coverage creates an empty temporary SQLite path exclusively through `init_db`, provisions the conditional heartbeat surface, diagnoses it, asserts integrity and zero PAPER blockers/NOT NULL false positives/canonical owner conflicts, and runs isolated direct writer smoke probes. Focused reconciliation, runtime-control/state/heartbeat, and repository-audit suites were executed. The local environment cannot install the declared Alembic package because its package proxy returns HTTP 403, so migration-importing tests remain an external CI gate. No migration is required. Recommend merge after CI; do not infer LIVE readiness.

---

---
## Runtime bootstrap/default hardening — 2026-08-30

**Need/root cause.** Registry/env examples, burn-in fallbacks, Settings, and Alembic still named repository-root `alphaforge.db`, while runtime had a cwd-sensitive newer path. PAPER reconciliation defaulted off and preflight treated credentials as non-blocking even though startup fail-closed required a provider.

**Files/behavior.** `database_defaults.py` now owns the canonical repository-root-safe path, SQLAlchemy URL conversion, and URL > legacy `ALPHAFORGE_DB_PATH` > default precedence. Config, persistence, both burn-in CLIs, Settings, Alembic, and env profiles consume the contract. Alembic creates only the canonical parent directory. Burn-in `--db` remains highest precedence. Preflight requires enabled reconciliation, complete non-placeholder credentials, and a complete authenticated signed read-only snapshot. PAPER exchange reconciliation compares no simulated PAPER order as an intended real order, but real exchange orders/positions remain orphan exposure and fail closed.

**Lifecycle/persistence/export/schema.** No lifecycle transition, persisted column, CSV export, or schema revision changed. Fresh directory creation is idempotent. No database is copied, renamed, deleted, or silently selected over an explicit URL.

**Tests/execution.** Added clean canonical bootstrap, no-root-file, override precedence, burn-in parity, and Alembic declaration regression tests. Targeted and full pytest plus static legacy-literal audit are required before push; results are recorded in the final implementation response.

**Risks/limitations/migration.** PAPER now intentionally blocks earlier when signed Binance read-only evidence is unavailable. Existing custom URL users need no action. Existing root databases are retained but must be explicitly selected. No destructive migration exists. LIVE order authorization and mutation behavior were not enabled. Push recommendation: merge only with tests green; LIVE remains NOT READY.

---
## PR #335 merge-blocker follow-up — 2026-08-30

**Why/root cause.** `burnin_ops._db_path()` checked legacy `ALPHAFORGE_DB_PATH` before loading the canonical URL, unlike runtime and `burnin_cli`. The previous documentation patch also removed useful lifecycle operations and contained foreground launch, invalid DB-doctor, and raw-process credential checks.

**Files and behavior.** `burnin_ops.py` now preserves explicit `--db`, bootstraps the canonical dotenv contract, and delegates URL/legacy/default precedence to `database_defaults.resolve_runtime_database_url`. Regression tests set both environment forms and prove runtime plus both burn-in CLIs select the URL, while explicit CLI input remains highest. `docs/KOMUTLAR.md` restores the full guide and corrects detached launch, DB-doctor syntax, canonical-loader credential booleans, canonical DB use, lifecycle operations, SQL, and troubleshooting.

**Lifecycle/persistence/export/schema/compatibility.** No lifecycle, reconciliation, MTF, campaign identity, persistence, export, or schema behavior changed. No DB is created, moved, mutated, or deleted by this follow-up. Legacy `ALPHAFORGE_DB_PATH` remains supported below the canonical URL.

**Tests/risks/migration/push.** Targeted precedence, burn-in, and documentation assertions plus the full suite are required. There is no migration. The only compatibility correction is removal of the unintended legacy-path precedence. LIVE mutation authorization remains unchanged and LIVE is NOT READY.

---
## PR #335 Alembic dotenv merge-blocker — 2026-08-30

**Why/root cause.** Alembic inspected `os.environ` before AlphaForge's canonical dotenv bootstrap, so a DB override present only in `.env` could split migrations from runtime and burn-in.

**Files/behavior.** `database_defaults.py` now exposes an Alembic-specific selector that runs the existing `bootstrap_environment` against the effective environment, preserves a deliberate non-default Alembic URL, and otherwise applies canonical URL aliases, legacy path, then default precedence. `alembic/env.py` uses that selector before creating the SQLite parent. Tests cover URL-only dotenv, URL plus legacy path, canonical default/no root DB, and deliberate Alembic override.

**Lifecycle/persistence/export/schema/compatibility.** No lifecycle, reconciliation, LIVE, MTF, campaign identity, schema, migration revision, export, or existing DB behavior changed. No database is moved, deleted, or automatically migrated.

**Tests/risks/migration/push.** Targeted resolver tests pass. Alembic integration/full-suite execution remains contingent on the declared Alembic package being installed. No migration is required. LIVE remains NOT READY.
