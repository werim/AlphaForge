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
