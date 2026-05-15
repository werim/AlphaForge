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
