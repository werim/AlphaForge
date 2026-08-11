# Stale PAPER STARTING recovery surgery — 2026-08-11

## Need and root cause

Detached startup demoted the campaign and both continuation rows to `STARTING`, but only the launcher called `_mark_attached_running`. If that launcher disappeared after the worker attached, the worker could continue scanning and persisting decisions indefinitely without owning the operational status transition. Recovery then classified only dead `RUNNING` continuations as stale; the zero-decision startup fallback deliberately rejected a 9,990-decision continuation, leaving this safe but inconsistent case blocked.

## Minimal behavior and state transition

`RuntimeOrchestrator.start` now persists all three linked rows as `RUNNING` immediately after runtime recovery, attachment, and reconciliation succeed, before the `OPERATING` snapshot. Conditional row counts and campaign/run lineage prevent partial or wrong-continuation promotion. For historical dead `STARTING` PAPER scanners, recovery dispatches to the existing explicit transactional zero-exposure terminalizer. The chosen terminal state is `FAILED`: the worker is gone, the continuation cannot truthfully resume, and `RECOVERY_REQUIRED` is the guarded intermediate/operator state rather than a completed outcome. Decisions remain evidence and are not treated as executions.

Follow-up lifecycle hardening moves promotion before setting or persisting runtime `OPERATING`. A promotion failure therefore leaves the runtime at `STARTING`, writes no authoritative `OPERATING` snapshot, and starts no scanner/heartbeat/reconciliation tasks. An already-`RUNNING` campaign is idempotent only after the exact active run and campaign-run mapping are re-read and both prove lineage-matched `RUNNING`; partial three-row state raises an explicit transition inconsistency.

The terminalizer accepts `STARTING` only with a persisted dead PID identity and retains its existing fresh external-evidence bridge, 120-second identity, `BEGIN IMMEDIATE`, final campaign/run/mapping/source/exposure/execution/lifecycle re-reads, exact one-row conditional updates, and rollback-on-drift contract. Any execution or execution lifecycle state, pending reject, local/runtime position/order/orphan, missing evidence, live worker, lineage mismatch, stale snapshot, source change, or unavailable query blocks mutation. LIVE paths are unchanged.

## Reconciliation, persistence, compatibility, and risk

An authenticated `COMPLETE` `AUTHENTICATED_EXCHANGE_SNAPSHOT` with empty positions/orders is now the effective `CLEAN` status of that evaluation; the older `EXCHANGE_STATE_UNKNOWN` event remains immutable history. Non-authoritative probes retain prior semantics and cannot create campaign-linked terminalization evidence. No schema, migration, CSV/export, decision, lifecycle, runtime snapshot, reconciliation, or audit row is deleted or rewritten. The only mutations are guarded campaign/run statuses plus append-only events/evidence. LIVE remains NOT READY.

## Files and tests

`burnin_campaign.py` owns operational promotion, `runtime.py` invokes it before the authoritative operational boundary, `burnin_ops.py` owns stale-scanner dispatch and atomic FAILED terminalization, and `runtime_state.py` resolves the effective clean probe status. Regressions cover 9,990 preserved decisions, all-three-row operational promotion, partial-`RUNNING` rejection, and absence of an `OPERATING` snapshot/task startup after promotion failure; the existing terminalization matrix continues to cover live worker, execution/lifecycle evidence, missing/unknown exposure, pending rejects, source/evidence drift, row-count mismatch, and rollback.

## Tests executed

- Focused stale-scanner, operational-transition, terminalization, runtime recovery, and reconciliation suites passed.
- Full repository suite passed after lifecycle hardening: 1,192 passed, 3 skipped, 282 warnings.
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
