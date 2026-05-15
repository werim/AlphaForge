# AlphaForge Phase 1-3 Completion Patch Report

## 1) Why the change was needed
Persistence helpers for order decisions and lifecycle events were effectively no-ops, which prevented Phase 2/3 contract verification and made rejection/lifecycle auditability incomplete.

## 2) Exact behavior changed
- `src/alphaforge/persistence.py`
  - Expanded DB schema for `signals`, `order_decisions`, and `trade_lifecycle_events` with contract fields (`mode`, `decision`, `reject_reason`, `score`, `rr`, `effective_rr`, `expectancy_bucket`, execution context payload/missing flags, timestamps, IDs).
  - Implemented real SQL upsert writes for `save_signal`, `save_order_decision`, and `save_trade_lifecycle_event`.
  - Preserved defensive behavior for expectancy fetches and other existing helper semantics.
- Added Phase 1-3 regression tests in `tests/test_phase123_foundations.py` covering package shadowing, runtime package origin, decision reject persistence, lifecycle persistence, rejection lifecycle ordering, reject reason propagation, lifecycle pre-trade states, UNAVAILABLE_BACKTEST sentinels, and contract field consistency.

## 3) Expected runtime/backtest impact
- Rejected decisions and lifecycle states are now persisted in SQL instead of dropped.
- Backtest lifecycle and rejection contracts are now guarded with explicit regression tests.
- Sentinel values such as `UNAVAILABLE_BACKTEST` are preserved in persistence payloads (not coerced to 0.0).

## 4) Compatibility risks
- SQLite schema in `init_db` is broader; any consumers assuming prior minimal columns may need to tolerate additional columns.
- Upsert requires SQLite `ON CONFLICT` support (available in project baseline environments).

## 5) Migration concerns
- For in-memory/ephemeral DB usage: no migration action needed.
- For persistent DB usage with prior schema: apply migrations or recreate local dev DB so new columns/unique IDs are present.

## 6) Whether decision contract changed
YES.
- `order_decisions` persistence now stores/updates decision contract fields (`decision`, `reject_reason`, scoring/RR fields, mode, execution context metadata).

## 7) Whether lifecycle schema changed
YES.
- `trade_lifecycle_events` persistence now stores explicit lifecycle contract fields including state, decision, reject reason, execution context metadata, and event timestamps.

## 8) Whether persistence semantics changed
YES.
- `save_order_decision` and `save_trade_lifecycle_event` moved from no-op behavior to real idempotent upsert writes.

## 9) Tests added/updated
Added:
- `test_no_duplicate_alphaforge_package_shadowing`
- `test_runtime_imports_from_src_package`
- `test_save_order_decision_persists_reject`
- `test_save_trade_lifecycle_event_persists_state`
- `test_rejected_signal_lifecycle_precedes_trade_creation`
- `test_order_rejected_lifecycle_contains_reason`
- `test_backtest_lifecycle_does_not_start_directly_at_created`
- `test_unavailable_backtest_context_uses_sentinel_not_zero`
- `test_backtest_paper_decision_contract_fields_match`

## 10) pytest -q result
- `122 passed, 21 warnings in 2.14s`
