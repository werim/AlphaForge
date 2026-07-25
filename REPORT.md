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
