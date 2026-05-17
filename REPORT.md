# AlphaForge Phase 2/3 Lifecycle Export + Contract Parity Patch Report

## Why this patch was needed
- Backtest lifecycle export was list/dataclass-driven and not proven against persisted SQL lifecycle events.
- BACKTEST/PAPER contract parity coverage relied on a handcrafted dict key check.
- `execution_ctx_missing` was persisted as stringified bool values (`"True"`/`"False"`) causing mixed semantics risk.

## Exact behavior changed
- `backtest_order.py`
  - Added deterministic lifecycle event ID generation for backtest rows.
  - Added persistence of in-memory lifecycle rows into SQL `trade_lifecycle_events` before writing lifecycle CSV.
  - Changed `order_lifecycle.csv` export source to persisted SQL rows (sorted deterministically by `event_ts,event_id`).
  - Rejected lifecycle rows (`SIGNAL_REJECTED`, `ORDER_REJECTED`) are persisted and exported in this SQL-backed path.
- `src/alphaforge/persistence.py`
  - Normalized `execution_ctx_missing` schema columns to INTEGER in `init_db` table definitions.
  - Normalized write-path conversion to canonical 0/1 for both decisions and lifecycle events.
- `tests/test_backtest_order_scanner.py`
  - Added SQL-backed lifecycle export verification and duplicate-event-id checks.
- `tests/test_phase123_foundations.py`
  - Added idempotency tests for order decision/lifecycle upserts.
  - Added execution_ctx_missing round-trip consistency test (including compatibility with legacy `True`/`False` text rows).
  - Supplemented real output parity checks using real BACKTEST and PAPER order-cycle outputs plus runtime paper execution output fields.

## OPEN_AT_END representation note
- This patch does **not** introduce a fake `OPEN_AT_END` lifecycle state.
- Current architecture continues representing open-at-end via timeout/open rows (`close_reason == "TIMEOUT"` + `open_at_end.csv`).

## Compatibility and migration concerns
- Existing persistent SQLite DBs created with TEXT `execution_ctx_missing` may require migration/rebuild to adopt INTEGER canonical storage.
- Read paths that may encounter historical TEXT values should tolerate both legacy text and integer representations.

## Decision/lifecycle contract impact
- Decision contract persistence semantics changed at storage boundary: `execution_ctx_missing` now canonical 0/1.
- Lifecycle schema semantics changed for `execution_ctx_missing` storage type and export source-of-truth for backtest lifecycle CSV.

## Tests added/updated
- Added:
  - `test_lifecycle_export_reads_persisted_sql_events`
  - `test_lifecycle_export_has_no_duplicate_event_ids`
  - `test_order_decision_upsert_is_idempotent`
  - `test_trade_lifecycle_event_upsert_is_idempotent`
  - `test_backtest_and_paper_real_outputs_share_required_contract_fields`
  - `test_execution_ctx_missing_round_trip_type_is_consistent`
  - `test_runtime_paper_output_has_execution_fields`

## Actual verification run
- `python -m compileall src tests`
- `pytest -q` => `129 passed, 21 warnings in 1.13s`
- `PYTHONPATH=src python -c "import alphaforge.runtime as r; print(r.__file__)"` => `/workspace/AlphaForge/src/alphaforge/runtime.py`

## Remaining known gaps
- Full end-to-end parity assertion across every optional field (including all lifecycle/persistence timestamp typing nuances) can be extended further.
- Legacy DB migration automation is not included in this patch.

## Phase 3.5 Backtest quality semantics patch (effective_rr integrity)

### Scope
- Fixed a semantic integrity bug in backtest lifecycle persistence where `effective_rr` was incorrectly persisted as raw `rr`.
- Preserved existing lifecycle/export architecture and early-rejection flow.

### Exact change
- `backtest_order.py`
  - `LifecycleRow` now includes an explicit `effective_rr` field (optional) so execution-adjusted RR can be carried with lifecycle events.
  - SQL lifecycle persistence now writes `effective_rr` from `LifecycleRow.effective_rr` when present, and falls back to raw `rr` only when no effective value is provided.
  - Execution-penalty rejection path now stores computed `effective_rr` onto the emitted `ORDER_REJECTED` lifecycle row.

### Tests updated
- `tests/test_backtest_order_scanner.py`
  - Added `test_lifecycle_persistence_uses_effective_rr_when_available` to prove persisted `effective_rr` can differ from raw `rr` under execution-penalty conditions and remains exported via SQL-backed lifecycle rows.

### Remaining gaps (not addressed in this patch)
- Backtest still depends on live Binance endpoints for top-N universe unless fixture mode is used.
- Some execution context fields are estimated/offline-derived in backtest mode (by design); this patch does not introduce new live dependencies.

## Phase 3.5 Next Patch: Backtest Quality Distribution Report

- Added `build_backtest_quality_summary(...)` to aggregate lifecycle-backed quality distributions (accepted/rejected split, reject-rate, reject reasons, score/RR/effective-RR distributions, effective-vs-raw RR divergence count, expectancy buckets, execution-context missing distribution, and unavailable execution context field counts).
- Added `write_backtest_quality_summary(...)` and integrated it into `main()` to emit `data/backtest/backtest_quality_summary.csv` alongside existing backtest outputs.
- Added tests to verify quality summary explicitly includes `effective_rr` distribution and reject-reason distribution counts.
- Kept unavailable execution context as explicit sentinel/null semantics (no synthetic `0.0` coercion for missing values).


## Generation 1: Contract Lockdown & Lifecycle Determinism

### Why this change was needed
- Runtime lifecycle events were ad-hoc (`event`/epoch fields), and invalid transitions could pass without explicit error state semantics.
- Reject reason taxonomy and timestamp formatting were not normalized through one contract utility surface.

### Exact behavior changed
- Added `src/alphaforge/contracts.py` with canonical lifecycle event constants, reject reason normalization, UTC timestamp normalization, and legal transition map/validator.
- `RuntimeOrchestrator` now emits canonical `lifecycle_event_type`, `lifecycle_state`, `timestamp` (UTC ISO8601 `Z`), `previous_lifecycle_state`, and enforces transition guardrails (`ERROR` on invalid transition).
- `save_trade_lifecycle_event(...)` now validates transition intent (`previous_lifecycle_state` -> `lifecycle_state`), canonicalizes timestamps, and persists normalized reject reason values.

### Runtime/backtest impact
- BACKTEST/PAPER/LIVE runtime lifecycle event payloads are deterministic and contract-shaped.
- Invalid lifecycle transition attempts are explicitly surfaced as `ERROR`, improving auditability and preventing silent lifecycle drift.

### Compatibility risks / migration concerns
- Consumers reading runtime event callbacks must accept `lifecycle_event_type`/`timestamp` canonical fields (old `event`/`ts` are no longer emitted by runtime orchestrator callbacks).
- Persisted reject reasons remain backward-compatible strings; unknown/empty normalize to `UNKNOWN`.

### Decision/lifecycle/persistence contract deltas
- Decision contract: reject reason normalization utility added and used in runtime rejection path.
- Lifecycle schema: no new DB columns were added; semantics are tightened via validation and canonical event/timestamp handling.
- Persistence semantics: transition-invalid lifecycle writes are persisted as `ERROR` when prior state is provided and illegal.

## Generation 2: Persistence Integrity & Migration Safety

### Why this patch was needed
- Persistence invariants were partially enforced by convention only, leaving schema drift and legacy bool/text/integer ambiguity risks during reruns/backfills.
- Export integrity checks between persisted lifecycle rows and emitted CSV artifacts were not enforced as a hard gate.

### Exact behavior changed
- `src/alphaforge/persistence.py`
  - Added idempotent SQLite migration bootstrap (`schema_migrations`) with version note `2026_05_16_persistence_integrity_v1`.
  - Added non-destructive migration upgrades for `trade_lifecycle_events` (`lifecycle_seq`, `cancel_reason`, `lifecycle_id`) and unique index guard on `(signal_id,event_ts,lifecycle_state)`.
  - Added explicit legacy normalization for `execution_ctx_missing` values (`True/False/1/0/...`) to canonical integer 0/1 semantics.
  - Kept backward-compatible write behavior by deterministic fallback IDs for missing `signal_id/decision_id/event_id` while still enforcing canonical write fields when present.
  - Normalized rejected decision persistence to canonical reject reason taxonomy.
- `backtest_order.py`
  - Added `verify_export_integrity(...)` to validate lifecycle/rejected row-count parity and required rejected/lifecycle fields.
  - Main backtest flow now runs export verification after CSV writes and fails fast on mismatch.
- Tests
  - Added migration column presence test and generated-event-id fallback lifecycle persistence test.
  - Added export integrity verifier mismatch detection test.

### Expected runtime/backtest impact
- Rerun/backfill behavior is safer against duplicate lifecycle persistence and mixed legacy bool/text/int field semantics.
- Backtest export now explicitly fails when SQLite↔CSV lifecycle/rejected datasets diverge.

### Compatibility risks
- New lifecycle metadata columns are additive/non-destructive.
- Unique index on `(signal_id,event_ts,lifecycle_state)` may surface pre-existing duplicate legacy rows as write conflicts (expected hardening behavior).

### Migration concerns
- Migration is idempotent and non-destructive.
- Legacy rows remain readable; coercion normalizes boolean-like values for `execution_ctx_missing`.

### Contract impact
- Decision contract: reject-reason normalization tightened for rejected rows.
- Lifecycle schema: additive columns (`lifecycle_seq`, `cancel_reason`, `lifecycle_id`) plus uniqueness constraint for deterministic replay safety.
- Persistence semantics: stronger replay/backfill determinism and explicit export verification failure mode.


## Generation 3 — Execution Realism Engine Hardening (2026-05-16)
- Why needed: execution costs and missing context could be normalized toward optimistic defaults, weakening reject quality and effective RR traceability.
- Root cause: prior effective RR formula used a compressed penalty shortcut and reused neutral defaults when context was partial/unknown.
- Files changed: `src/alphaforge/execution.py`, `src/alphaforge/order.py`, `tests/test_execution_layer.py`, `CHANGELOG.md`, `VERSION.md`.
- Runtime behavior changes: deterministic execution-cost primitive now computes spread/slippage/latency/funding/liquidity penalties and context completeness (`complete|partial|unavailable`).
- Lifecycle changes: none to lifecycle schema/order; rejection flags are more explicit (`UNKNOWN_EXECUTION_CONTEXT`, `LOW_EFFECTIVE_RR`, etc.).
- Persistence changes: decision payload now carries execution-cost completeness metadata through `execution_metrics` and existing persistence path.
- Export/schema changes: none.
- Decision contract changed: no required field removals; effective RR and execution flags semantics tightened.
- Lifecycle schema changed: no.
- Persistence semantics changed: yes (unknown execution context explicitly penalized/rejected instead of looking like zero costs).
- Compatibility risks: tests/analytics expecting previous effective RR values may require baseline refresh.
- Migration concerns: no DB migration required; downstream consumers should handle new execution flags and completeness metadata.
- Tests added/updated: effective RR expectation update + unknown-context regression test in `tests/test_execution_layer.py`.
- Tests executed: targeted execution-layer and trade-quality suites.
- Remaining limitations: regime/volatility/liquidity band reject calibration is still rule-based and should be tuned with market-specific data.
- Push recommendation: safe to merge for research/backtest hardening; schedule follow-up for full band calibration framework across broader modules.

## Generation 4 — Runtime Safety Controls & Reconciliation Layer (2026-05-16)

### 1) Why changes were needed
- Runtime flow had limited fail-closed controls before order submission and weak explicit handling for timeout/error/ack-loss drift scenarios.

### 2) Exact runtime behaviors changed
- Added deterministic risk-gate checks before AI order acceptance progression (kill switch, stale data, spread/funding sanity, cooldown, duplicate position, concurrency guard).
- Accepted signal lifecycle progression now uses explicit entry submission/ack/fill semantics.

### 3) Reconciliation semantics added
- Execution timeout/error/missing-ack style outcomes emit `EXECUTION_ERROR` plus `RECONCILIATION_REPAIR` with state snapshot payload (`intended_state`, `exchange_state`, `persisted_state`).
- Reconciliation routine is idempotent at event-level via deterministic lifecycle callbacks and existing persistence upsert strategy.

### 4) Lifecycle changes introduced
- Added extended runtime lifecycle taxonomy for entry, failure, protective, and reconciliation states.
- Transition map updated to reject illegal transitions and keep invalid flow explicit as `ERROR`.

### 5) Persistence changes introduced
- Added additive migration columns on `trade_lifecycle_events`: `failure_reason`, `reconciliation_reason`, `incident_payload`.
- Lifecycle write path now accepts and persists these explicit failure/reconciliation fields.

### 6) Backward compatibility concerns
- New lifecycle states may require downstream parsers/analytics allowlist updates.
- Schema changes are additive; old readers should ignore unknown columns, but strict column selectors may need updates.

### 7) Runtime safety improvements
- Runtime now fails closed on key pre-trade hazard conditions.
- Uncertain execution outcomes are no longer silent: they are lifecycle-persisted and incident-counted.

### 8) Remaining operational risks
- Concentration/correlation exposure gates currently rely on limited runtime context and should be tied to richer portfolio state.
- Reconciliation currently journals and classifies but does not yet perform full venue-side corrective workflows.

### 9) PAPER vs LIVE parity impact
- PAPER and LIVE now share the same pre-trade risk gate path and failure-state lifecycle semantics in orchestrator.
- LIVE still requires deeper adapter-side failure/retry standardization for production safety.

### 10) Architectural risk assessment
- Patch is surgical and contained to contracts/runtime/persistence/test/docs without introducing duplicate packages or replacing core architecture.
- Risk is moderate-low for existing flow; main integration risk is consumer adaptation to extended lifecycle vocabulary.


## Generation 5 — Live Readiness Qualification & Controlled Enablement (2026-05-17)

### Why the patch was needed
- LIVE path existed but lacked deterministic, persisted pre-deployment qualification and explicit operator-controlled enablement gates.

### Root cause
- Prior runtime safety logic protected per-trade flow but did not provide a holistic deployment qualification barrier across lifecycle integrity, persistence integrity, reconciliation confidence, and operational rollback observability.

### Files changed
- `src/alphaforge/live_readiness.py`
- `src/alphaforge/runtime.py`
- `tests/test_live_readiness.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- Added deterministic LIVE readiness evaluation with fail-closed gating.
- LIVE startup now requires: qualification pass, shadow-mode enabled, canary enabled, and explicit operator acknowledgement.
- Qualification status is persisted (`live_readiness_reports`) and logged for deployment-state visibility.

### Lifecycle changes
- No lifecycle schema rewrite; added lifecycle integrity checks for orphan states, illegal transitions, missing reject reasons, and missing exit completion after entry trigger.

### Persistence changes
- Added compatibility-safe table `live_readiness_reports` for audit persistence of qualification reports.
- Added forensic snapshot export utility (`qualification_snapshot_<timestamp>.json`) for incident rollback diagnostics.

### Export/schema changes
- Additive schema only (`live_readiness_reports`), no destructive migration behavior.

### Reconciliation guarantees
- Qualification enforces zero orphan positions/orders and zero duplicate fills in provided reconciliation snapshot inputs before LIVE enablement.

### Shadow/canary controlled workflow
- LIVE remains blocked unless shadow-mode and canary flags are enabled, and operator acknowledgement is explicit.

### Tests added
- `test_live_readiness_pass_and_persistence`
- `test_live_readiness_detects_lifecycle_orphan`
- `test_runtime_live_mode_blocked_without_acknowledgement`
- `test_forensic_snapshot_written`

### Tests executed
- `pytest -q tests/test_live_readiness.py tests/test_runtime.py` -> 12 passed

### Compatibility / migration concerns
- New table is additive and lazily created by evaluator persistence call.
- Existing runtime entrypoints preserved; LIVE mode now requires explicit readiness inputs, which may require operator config updates.

### Remaining LIVE deployment risks
- Reconciliation snapshot inputs are currently static defaults inside orchestrator and should be wired to real exchange/account telemetry before any production usage.
- Alert coverage checks currently validate declared readiness flags, not external alert transport health.

### Rollback limitations
- Qualification snapshots aid forensics but do not automate execution rollback actions.

### Push recommendation
- Merge for safety hardening in research/staging environments; do not claim production LIVE readiness until reconciliation telemetry and remediation automation are integrated.
