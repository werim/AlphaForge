# AlphaForge Technical Status Report

## Why the change was needed
Phase 1-3 foundations were incomplete: duplicate-package protection was untested, persistence entry points for order decisions/lifecycle events were effectively no-ops, and lifecycle contract regressions were not guarded.

## Exact behavior changed
- `init_db` now creates operational `order_decisions` and `trade_lifecycle_events` schemas with contract fields and idempotency constraints.
- `save_order_decision` now performs SQL inserts/upserts and persists rejection fields and contract context/sentinels.
- `save_trade_lifecycle_event` now performs SQL inserts with idempotent conflict handling.
- Added Phase 1-3 regression tests for package shadowing, runtime import path, rejection persistence, lifecycle state persistence/ordering, sentinel usage, and decision contract-field parity.

## Expected runtime/backtest impact
- Rejected and accepted order decisions are now durably persisted.
- Lifecycle events are now durably persisted and deduplicated for repeated writes.
- Backtest lifecycle sentinel behavior is explicitly enforced by tests.

## Compatibility risks
- `order_decisions` and `trade_lifecycle_events` table definitions in `init_db` are expanded; environments assuming the previous minimal schema may need fresh sqlite DB initialization.

## Migration concerns
- For existing sqlite files created with the previous minimal `init_db`, schema migration is not automatic in this patch. Recreate test/dev DBs when necessary.

## Whether decision contract changed
YES — persistence now stores the decision contract fields (`mode`, `decision`, `reject_reason`, `score`, `rr`, `effective_rr`, `expectancy_bucket`) explicitly.

## Whether lifecycle schema changed
YES — lifecycle persistence schema now includes `signal_id`, `mode`, `state`, `reject_reason`, timestamps, and idempotency uniqueness.

## Whether persistence semantics changed
YES — `save_order_decision` and `save_trade_lifecycle_event` changed from no-op to real SQL writes with commit/idempotency behavior.

## Tests added/updated
- Added `tests/test_phase123_foundations.py` with required Phase 1-3 regression coverage.

## pytest -q result
PASS


## Follow-up adjustments
- Added legacy SQLite schema compatibility in `init_db` by non-destructive `ALTER TABLE ADD COLUMN` migration for old minimal `order_decisions` and `trade_lifecycle_events` tables.
- Added regression test `test_init_db_migrates_legacy_schema_without_drop`.
- Runtime module invocation note: `python -m alphaforge.runtime` still requires src path/package install in this environment; validated via test import path and suite execution.
