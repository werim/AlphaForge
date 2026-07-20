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
