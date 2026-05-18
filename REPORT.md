# AlphaForge Phase 2/3 Lifecycle Export + Contract Parity Patch Report

## Hotfix — `regime_ok` UnboundLocalError deterministic gate repair (2026-05-17)

### Why this change was needed
- Post-merge CI failures (`UnboundLocalError`) showed `regime_ok` was read by the gate collector before initialization in trade quality evaluation, crashing shared decision paths.

### Exact behavior changed
- `src/alphaforge/order.py`
  - Moved regime-compatibility derivation (`regime_ok`) before gate checks so all branches initialize deterministically before use.
  - Preserved regime semantics: setup/regime compatibility still drives `REGIME_MISMATCH` when required and incompatible.
- `tests/test_trade_quality.py`
  - Added regressions to prove missing candidate regime (`None`) does not crash.
  - Added regressions confirming missing candidate regime falls back to market regime and still rejects incompatible regimes with `REGIME_MISMATCH`.

### Expected BACKTEST / PAPER / LIVE impact
- Shared quality gate path no longer crashes on candidate regime-null cases.
- Reject determinism is preserved across modes because the same `evaluate_trade_quality(...)` gate order still applies.
- No live endpoint behavior was introduced in BACKTEST; mode routing remains unchanged by this fix.

### Compatibility risks
- Low risk; no schema/API field removals and no threshold value changes.
- Gate outcomes remain equivalent except crash cases now resolve to normal accept/reject decisions.

### Migration concerns
- None. No DB migration required.

### Contract / lifecycle / persistence impact
- Decision contract: unchanged required fields; fixed runtime safety (no unbound local crash).
- Lifecycle schema: unchanged.
- Persistence semantics: unchanged.

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

## Generation 6 — CSV Export Schema-Drift Hardening (2026-05-17)

### Why the change was needed
- `python3 backtest_order.py --top-n 50` failed during CSV export with `ValueError: dict contains fields not in fieldnames` because row dictionaries now carry richer lifecycle/market/decision fields (for example `event_flags`, `tp`, `entry`, `s1`, `liquidity_score`, `volatility_score`, `expected_slippage_pct`, `spread_pct`) that were not guaranteed to exist in the first row used to seed CSV headers.

### Exact behavior changed
- `backtest_order.py`
  - Added `resolve_csv_fieldnames(rows, preferred_fieldnames)` to build a deterministic union schema for CSV export.
  - Exporters that write row lists now compute fieldnames from:
    1) preferred/base order from existing first-row keys,
    2) plus all additional keys discovered across all rows, appended in alphabetical order.
  - Empty row list behavior remains safe (`""` written, no crash), and unknown fields are no longer dropped or ignored.
- `tests/test_backtest_order_scanner.py`
  - Added regression coverage for `resolve_csv_fieldnames` ordering and union behavior.

### Expected runtime/backtest impact
- Backtest CSV exports are resilient to schema drift introduced by new lifecycle/decision/execution fields.
- Backtest runs no longer fail when later rows include fields absent from the first row.
- Export outputs preserve lifecycle and reject observability without silently discarding data.

### Compatibility risks
- CSV files may include additional columns compared to prior runs when new row keys exist; downstream strict column-order consumers should tolerate appended columns.

### Migration concerns
- No DB migration required.
- CSV consumers that hardcode exact schemas may need to allow additive columns.

### Contract/lifecycle/persistence semantics
- Decision contract changed: **No** (field values/meaning unchanged; only export schema resolution changed).
- Lifecycle schema changed: **No** (no lifecycle state/field removal).
- Persistence semantics changed: **No** for DB writes; CSV export semantics strengthened to preserve complete row keys.

## Generation 6 — Exchange-Reconciled Live Control Plane (2026-05-17)

### Why the patch was needed
- Generation 5 gated LIVE startup but did not provide continuous runtime reconciliation supervision and deterministic incident/repair workflows after startup.

### Root cause
- Runtime had only localized timeout reconciliation events; there was no periodic reconciliation engine unifying intended orders, lifecycle state, and exchange/account snapshots with persistent incident tracking.

### Files changed
- `src/alphaforge/reconciliation.py`
- `src/alphaforge/runtime.py`
- `tests/test_reconciliation.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- Added a continuous reconciliation loop in `RuntimeOrchestrator` with configurable interval and timeout guard.
- Added fail-closed escalation when reconciliation timeout occurs or findings require fail-closed action.
- Added de-duplication guard to prevent repeated repair-trigger lifecycle spam for identical findings.

### Lifecycle changes
- Reconciliation findings emit deterministic `RECONCILIATION_REPAIR` lifecycle events including incident evidence payloads for auditability.

### Persistence changes
- Added additive `reconciliation_incidents` table creation path and indexes (timestamp/symbol/severity).
- Added deterministic incident serialization (incident type, severity, symbol, lifecycle ref, remediation status, operator ack flag, fail-closed flag, forensic payload).

### Reconciliation semantics
- Detects and reports: `ORPHAN_ORDER`, `ORPHAN_POSITION`, `STALE_ORDER`, and `LIFECYCLE_DIVERGENCE` with deterministic remediation recommendations.
- Supports PAPER/LIVE mode operation without live exchange dependency during tests via snapshot-source abstraction.

### Fail-closed behavior
- Runtime now triggers shutdown on reconciliation timeout and on fail-closed findings to preserve capital and execution integrity.

### Tests added
- `test_orphan_order_and_repair_generation`
- `test_lifecycle_divergence_detection`
- `test_reconciliation_persistence`
- `test_runtime_reconciliation_fail_closed_and_no_duplicate_repair`
- `test_snapshot_replay_consistency`

### Tests executed
- `pytest -q tests/test_reconciliation.py tests/test_runtime.py`

### Migration/compatibility notes
- Schema evolution is additive only (`reconciliation_incidents`), preserving backward compatibility for existing persistence paths.
- No runtime architecture rewrite; orchestration flow and lifecycle semantics are extended minimally.

### Remaining LIVE blockers / risks
- Exchange snapshot lineage still requires deeper adapter integration for production-grade venue truth (fills/cancel lineage and account drift evidence).
- Repair workflow is recommendation-first (dry-run/shadow flags) and still requires operator ack governance wiring for any live actioning.

### Generation 7 recommendations
- Wire reconciliation snapshot inputs to authoritative exchange/account endpoints with deterministic retry/backoff.
- Persist fill lineage and cancellation lineage tables with replay IDs and duplicate-fill protection constraints.
- Add operator acknowledgement workflow persistence for repair plans and escalation runbooks.

## Generation 7 — Runtime Bootstrap Entrypoint & RR Wiring Audit (2026-05-17)

### Why the patch was needed
- `src/alphaforge/runtime.py` contained orchestration internals but no executable bootstrap path, so `python -m alphaforge.runtime` exited immediately.

### Root cause
- Missing module entrypoint (`if __name__ == "__main__": asyncio.run(main())`) and missing async runtime bootstrap that constructs `RuntimeOrchestrator` from environment configuration.

### Exact missing runtime bootstrap components added
- Added environment-driven bootstrap helpers:
  - `execution_mode_from_env(...)` is now used by `_build_runtime_from_env()`.
  - `_build_runtime_from_env()` initializes DB/session/`AIBrain`, parses runtime intervals from env, and wires a safe no-op async market scanner.
- Added async `main()` that:
  - configures logging level from env,
  - logs deterministic startup/shutdown messages,
  - starts orchestrator and preserves graceful shutdown via existing signal handlers in `RuntimeOrchestrator.start()`.
- Added module launch hook: `if __name__ == "__main__": asyncio.run(main())`.

### Architecture impact
- Minimal and surgical: no new runtime package, no alternate orchestration flow, no lifecycle rewrite.
- Existing `RuntimeOrchestrator` task loops, lifecycle emission, reject persistence, and reconciliation flow remain unchanged.

### Runtime safety considerations
- Startup is safe in environments without feeds/adapters because default bootstrap scanner is a no-op that yields no symbols.
- BACKTEST/PAPER startup requires no exchange connectivity.
- LIVE still remains fail-closed by existing qualification and adapter requirements.

### RR fallback analysis
- Runtime signal build path still computes `rr = float(market_ctx.get("rr", 2.0) or 2.0)`.
- Adaptive/dynamic RR already exists upstream in selection/backtest pipelines where `market_ctx["rr"]` is supplied; runtime previously used that value when present and only fell back for missing/invalid input.
- No evidence that runtime forces 2.0 when dynamic RR exists; added regression test to ensure provided dynamic RR is preserved in runtime signal payload.
- This patch intentionally avoids aggressive RR derivation rewrites to preserve lifecycle/decision architecture; fallback remains a defensive null-safety path.

### Tests added
- Runtime bootstrap env parsing and initialization smoke test.
- Runtime start loop liveness test (verifies loop remains active until explicit shutdown).
- Dynamic RR propagation test (runtime signal risk_reward uses provided RR instead of fallback).

### Tests executed
- `pytest -q tests/test_runtime.py` -> `11 passed`
## Generation 7 — Production-grade `.env.example` and operational safety defaults (2026-05-17)

### Why the patch was needed
- Existing `.env.example` was incomplete relative to current runtime/backtest/live control-plane architecture and did not clearly separate safe mode defaults.

### Root cause
- Configuration coverage lagged behind runtime lifecycle/reconciliation/readiness evolution, and environment guidance did not fully reflect execution-risk controls.

### Files changed
- `.env.example`
- `README.md`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime behavior changes
- No runtime code path rewrite; this patch is configuration/documentation hardening.
- Added explicit environment template fields for runtime mode, risk limits, execution realism thresholds, readiness/reconciliation toggles, and notification/integration placeholders.

### Lifecycle/persistence/schema impact
- No schema change.
- No lifecycle state-machine change.
- No persistence write/read semantics change.

### Safety defaults introduced
- LIVE disabled by default.
- Paper mode enabled by default.
- Dry-run enabled by default.
- Conservative risk/leverage-style thresholds and strict execution-risk gates included in template.

### Compatibility and migration concerns
- Backward-compatible: existing deployments can keep previous vars.
- Risk: teams with strict env validators may need to allow additional optional keys.
- Some fields are roadmap-aligned placeholders for adapters/integrations not fully wired yet.

### Tests executed
- Manual static verification of variable references and architecture mapping with ripgrep audits.

### Remaining limitations
- Current repository has limited direct `os.getenv` wiring; central env loader integration can be expanded in future patch without changing runtime architecture.

## 2026-05-17 Patch: Backtest order lifecycle accounting correction

### Root cause
- Backtest lifecycle persistence classified every non-reject lifecycle event as `ACCEPTED`, including `SIGNAL_CREATED`, which created contradictory ACCEPTED+REJECTED rows for the same signal.
- `SYMBOL_REJECTED` lifecycle events were not mapped to rejected decisions in SQL persistence.
- Backtest summary used row-level counts (`len(lifecycle)`) for `total_orders`, inflating orders by counting non-order lifecycle events.

### Exact fix
- `backtest_order.py`
  - Updated lifecycle decision mapping in `_persist_lifecycle_rows(...)`:
    - `SIGNAL_CREATED` => `PENDING`
    - `SIGNAL_REJECTED`/`ORDER_REJECTED`/`SYMBOL_REJECTED` => `REJECTED`
    - all other progression states => `ACCEPTED`
  - Added `_derive_backtest_counts(...)` to derive candidate/reject counts by final per-signal terminal decision and avoid double counting.
  - Updated summary derivation to:
    - compute `total_candidates`, `accepted_count`, `total_rejected`, `rejection_rate` from per-signal final decisions
    - compute `total_orders` from `ORDER_PLACED` rows only
    - compute `triggered_orders` from `ENTRY_TRIGGERED`

### Behavior impact
- Lifecycle exports no longer emit `ACCEPTED` for `SIGNAL_CREATED` rows.
- Symbol-level rejections are now consistently marked `REJECTED` with preserved reject reason.
- Summary metrics align with actual order lifecycle semantics instead of raw event-row counts.

### Compatibility / migration risks
- Decision value `PENDING` appears for `SIGNAL_CREATED` in persisted backtest lifecycle events; downstream consumers that assumed only ACCEPTED/REJECTED should tolerate this explicit non-terminal state.
- No schema migration required.

### Tests added/updated
- Updated: `test_lifecycle_export_reads_persisted_sql_events` to assert `SIGNAL_CREATED` persists as `PENDING`.
- Added: `test_symbol_rejected_rows_are_persisted_as_rejected_decision`.
- Added: `test_derive_backtest_counts_uses_terminal_per_signal_and_order_placed_only`.
- Added: `test_signal_id_cannot_end_with_both_terminal_accepted_and_rejected`.

### Tests executed
- `pytest -q tests/test_backtest_order_scanner.py -k "lifecycle_export_reads_persisted_sql_events or symbol_rejected_rows_are_persisted_as_rejected_decision or derive_backtest_counts_uses_terminal_per_signal_and_order_placed_only or signal_id_cannot_end_with_both_terminal_accepted_and_rejected"`

## Generation 8 - Setup Quality Diagnostics & Gate Traceability (2026-05-17)

### Why this patch was needed
- Backtest showed 100% rejection with LOW_SCORE dominance, but exported candidate/rejection rows did not expose enough setup-level diagnostics to distinguish threshold strictness from weak setup construction.
- Quality summary lacked percentile-style distribution and cross-slices (reason by setup/regime) required for structural root-cause analysis.

### Root-cause findings from code audit
- Setup generation is currently heuristic and single-pattern biased in backtest (`_build_market_ctx`): fixed `setup_type=BREAKOUT_UP`, `side=LONG`, simple `entry/sl/tp` from current/previous candle, and synthetic `rr`/`score` formulas. This can create structurally weak candidates in chop/range periods.
- Symbol regime/chop filtering is upstream and can reject before order flow (`select_symbol`), yielding large `TOO_CHOPPY` counts that never become executable-quality setups.
- Runtime/PAPER and BACKTEST share the same order quality gate implementation (`evaluate_trade_quality`), so LOW_SCORE pressure can propagate across modes when setup quality is poor.
- BACKTEST uses estimated/derived context fields when unavailable, while runtime can have richer live microstructure fields; this context gap can alter effective quality and execution penalties.

### Exact code locations (trace map)
- Setup generation (candles -> market context): `backtest_order.py::_build_market_ctx`, `backtest_order.py::_build_symbol_market_data`.
- Candidate materialization: `src/alphaforge/order.py::build_order_candidate`.
- Score/reject gate: `src/alphaforge/order.py::evaluate_trade_quality`.
- Regime/chop prefilter: `src/alphaforge/symbol_selector.py::select_symbol`.
- Effective RR / execution penalties: `backtest_order.py::_execution_reject_flags`, `src/alphaforge/execution.py::build_execution_context`.
- Backtest lifecycle->candidate/rejected export path: `backtest_order.py::process_backtest_result` and CSV write section in `main()`.

### Behavior changed in this patch
- Rejected candidate export rows now include setup-quality diagnostics fields: `raw_rr`, `effective_rr`, `min_required_score`, `trend_strength`, `volatility_pct`, `range_position`, `slippage_pct`, `first_blocking_gate`, `all_failed_gates`.
- Accepted candidate export rows now include the same diagnostic schema columns (empty/default where not applicable) to keep CSV contracts aligned for analysis tooling.
- Trade-quality diagnostics now compute `all_failed_gates` in addition to the first blocking gate to support full gate-failure visibility.
- Backtest quality summary now includes:
  - score percentiles (p10/p25/p50/p75/p90)
  - raw RR percentiles
  - effective RR percentiles
  - rejection reason by setup type
  - rejection reason by regime
  - near-threshold low-score rejection count

### Runtime impact
- No threshold loosening was introduced.
- No architecture rewrite; patch is additive diagnostics and analysis-safety focused.
- Runtime/PAPER decision behavior is unchanged except richer diagnostics payload keys.

### Tests executed
- `pytest -q tests/test_backtest_order_scanner.py::test_process_backtest_result_writes_rejection_rows_and_skips_sim tests/test_backtest_order_scanner.py::test_backtest_quality_summary_includes_effective_rr_distribution tests/test_symbol_selector.py`

### Remaining risks
- Setup generation logic remains simplistic and breakout-biased; diagnostics now expose the issue but do not yet redesign setup construction.
- Backtest context still contains estimated fields when venue-native data is unavailable.

### Minimal safe next patch plan
1. Add bounded pre-candidate setup validation (trend/range edge + structure confidence) before scoring.
2. Add multi-setup generator variants (trend continuation / range mean reversion) with explicit regime binding.
3. Add regression tests for setup-type diversity and RR realism under choppy inputs.
4. Keep thresholds unchanged until post-diagnostic distributions confirm setup-quality uplift.

## Hotfix — Backtest lifecycle summary mismatch reconciliation (2026-05-18)

### Why this patch was needed
- Backtest quality and order summaries were mixing candidate-level and lifecycle-event-level denominators, producing contradictory counts.

### Root cause
- Candidate and rejection counts in quality summary were derived from all persisted lifecycle rows instead of per-signal candidates.
- Main summary `total_orders` semantics drifted from accepted order objects and lifecycle buckets could not reconcile against accepted counts.

### Files changed
- `backtest_order.py`
- `tests/test_backtest_order_scanner.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- `_derive_backtest_counts(...)` now computes:
  - `total_candidates` from signal-level identities (`SIGNAL_CREATED` + `SYMBOL_REJECTED`)
  - `rejected_count` from terminal reject lifecycle states
  - `accepted_count` as `total_candidates - rejected_count`
  - `total_orders` as accepted pending-order lifecycle objects (`WAITING_ENTRY_ZONE`)
  - lifecycle outcome buckets (`triggered`, `not_triggered`, `tp`, `sl`, `open_at_end`) from lifecycle terminal states
- `order_backtest_summary.csv` now includes explicit `rejected_count` and keeps `total_rejected` as compatibility alias.
- `build_backtest_quality_summary(...)` now uses signal-level candidates (`SIGNAL_CREATED`) as denominator and signal-scoped reject accounting.

### Lifecycle / persistence / export impact
- No schema change.
- Lifecycle semantics preserved; only summary aggregation logic changed to use a consistent source-of-truth.
- Export consistency improved: quality summary and order summary now reconcile to shared candidate/reject semantics.

### Tests added/updated
- Added regression: `test_backtest_quality_summary_uses_signal_created_as_candidate_denominator`.
- Extended derive-count assertions for TP/SL buckets.

### Risks / limitations
- `python backtest_order.py --top-n {5,50}` still depends on Binance network access; blocked in restricted environments unless offline fixture mode is used.

## Generation 9: Adaptive Learning Data Foundation
- Why needed: rejects are alpha only when auditable; prior system lacked structured review datasets across executed and rejected outcomes.
- Behavior changed: added deterministic review persistence and SQL aggregation/shadow-threshold computation; no live decision gates were activated by default.
- Runtime impact: passive write-path enrichment only; decision contract remains backward-compatible.
- Lifecycle/schema impact: persistence schema expanded with adaptive learning tables and richer closed-trade review columns.
- Migration/compatibility risks: legacy consumers of `closed_trade_reviews` should tolerate additive columns; no destructive schema rewrites were introduced.
- Shadow mode: recommendation-only threshold output (`STATIC` vs `SHADOW_ADAPTIVE`) with insufficient-sample fail-safe and clamp constraints.
- Remaining work: forward-outcome labeling for rejected signals, scoped aggregation jobs, export CSV writers, and guarded opt-in active threshold application.

## 2026-05-18 Hotfix — Backtest quality summary compatibility + execution_metrics persistence restoration

### Why this patch was needed
- Regression after adaptive-foundation changes caused backtest quality summary to ignore direct decision rows in tests and set candidate/reject counts to zero.
- Closed-trade persistence wrote a later `closed_trade_reviews` row with `execution_metrics = NULL`, breaking legacy execution-layer readers that load JSON from this column.

### Root cause
- `build_backtest_quality_summary(...)` only used `SIGNAL_CREATED` lifecycle rows as the candidate source and did not robustly normalize mixed row contracts.
- Adaptive path inserted detailed closed-trade rows without populating legacy `execution_metrics`, and latest-row queries returned that NULL payload.

### Files changed
- `backtest_order.py`
- `src/alphaforge/adaptive_learning.py`
- `src/alphaforge/persistence.py`
- `CHANGELOG.md`
- `VERSION.md`

### Runtime behavior changes
- Quality summary now supports mixed row inputs:
  - Uses `decision` first; falls back to `status/status_after/status_before/lifecycle_state`.
  - Counts candidates from `SIGNAL_CREATED` rows when present, otherwise counts direct candidate rows.
  - Preserves reject/accept distributions for both lifecycle and direct test-row inputs.

### Persistence changes
- Adaptive closed-trade inserts now populate `execution_metrics` JSON (non-null; `{}` fallback).
- SQLite migration/init now ensures `closed_trade_reviews.execution_metrics` exists if missing.
- `save_closed_trade_review` now uses SQLAlchemy `text(...)` for reliable parameterized insert execution.

### Backward compatibility
- No columns removed, no table drops, no adaptive columns removed.
- Legacy readers selecting `execution_metrics` remain functional.
- Existing lifecycle-summary semantics for signal-denominator mode remain intact.

### Tests executed
- Targeted failing tests: all pass.
- Full suite: `161 passed`.

### Remaining risks
- Quality summary metadata-row detection is conservative (`metric/value`, `row_type` markers). Future non-candidate row formats may need explicit markers if introduced.
- Existing duplicate insertion strategy in `after_position_close` remains unchanged by design (minimal patch scope).

## Generation N+1 → N+2 Safe Evolution Patch (2026-05-18)

### Why the patch was needed
- Existing reject analysis had deterministic counterfactual helpers, but lacked a unified forward-window labeling contract for accepted/rejected lifecycle analytics and lacked scope-rich adaptive aggregation hooks for next-generation learning.

### Root cause
- Forward analysis logic existed as fragmented shadow helpers and was not exposed as a reusable deterministic evaluator contract.
- Adaptive aggregation centered on broad scopes (`GLOBAL/SYMBOL/REGIME/SETUP`) and did not provide a normalized entrypoint for reject-learning scopes such as rejection reason and execution-quality buckets.

### Files changed
- `backtest_order.py`
- `src/alphaforge/adaptive_learning.py`
- `tests/test_backtest_order_scanner.py`
- `tests/test_adaptive_learning_foundation.py`
- `VERSION.md`
- `CHANGELOG.md`
- `REPORT.md`

### Runtime behavior changes
- Added deterministic `evaluate_forward_window(...)` post-decision evaluator producing replay-safe forward labels and reject-quality outcomes.
- Added deterministic execution-quality bucket classification for forward telemetry slicing.
- Added `update_adaptive_stats_by_scope(...)` as additive aggregation interface for next-generation adaptive engines.

### Lifecycle impact
- No lifecycle transition rewrites.
- Lifecycle rows now have a deterministic forward-evaluation interface for post-lifecycle analytics.

### Persistence / schema impact
- No destructive schema changes.
- No existing table/column removals.
- SQL migration risk: none in this patch (additive interface-only evolution).

### Determinism guarantees
- Forward evaluator uses bounded lookahead windows (`forward_window_minutes`), deterministic same-candle SL-priority, and no randomness.
- Evaluator is post-decision analytics only and does not mutate same-candle decisions.
- Added replay determinism regression asserting repeated evaluations are identical.

### Tests added
- `test_forward_window_evaluator_labels_reject_correctness_deterministically`
- `test_adaptive_stats_by_scope_rejection_reason`

### Tests executed
- `pytest -q tests/test_adaptive_learning_foundation.py tests/test_backtest_order_scanner.py -q`

### Risks introduced
- Scope filters for advanced adaptive scopes rely on `payload_json` keys being populated by upstream writers; missing keys reduce sample coverage.
- Forward evaluator labels are currently produced in-memory and require future persistence wiring for full SQL export parity.

### Next-generation hooks enabled
- Forward label contract ready for:
  - confidence calibration (`predicted_quality` vs realized forward labels)
  - reject quality scoring
  - regime-aware expectancy attribution
  - execution degradation attribution
- Scoped adaptive aggregation entrypoint ready for future threshold and weighting engines without architecture rewrite.

### Suggested Generation N+2 roadmap
1. Persist forward-window evaluations into additive SQL table (`forward_signal_evaluations`) keyed by `signal_id + window + evaluator_version`.
2. Add export surface (`forward_evaluations.csv`, adaptive scope snapshots) and integrity verifier hooks.
3. Wire evaluator invocation after terminal lifecycle state emission in both BACKTEST and PAPER replay paths.
4. Extend scoped aggregation to accepted-path expectancy and calibration error metrics.
5. Add restart/reload invariance tests for persisted rolling scope stats and immutable historical rows.

### Push recommendation
- Safe to merge as deterministic analytics foundation patch; follow with additive SQL persistence/export patch before enabling any adaptive threshold consumers.
