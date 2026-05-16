# Changelog

All notable documented repository-level changes are summarized from `REPORT.md`.

## [Unreleased] - 2026-05-16


### Added
- SQLite migration registry table (`schema_migrations`) and idempotent migration note `2026_05_16_persistence_integrity_v1`.
- Additive lifecycle schema hardening columns: `lifecycle_seq`, `cancel_reason`, `lifecycle_id`.
- Backtest export integrity verifier gating lifecycle/rejected CSV consistency.

### Changed
- Legacy `execution_ctx_missing` values now normalize to canonical integer 0/1 during DB init migration.
- Added unique lifecycle replay/index guard on `(signal_id,event_ts,lifecycle_state)` to reduce rerun duplicates.

- Deterministic lifecycle event ID generation for backtest lifecycle rows.
- SQL-backed lifecycle export verification tests, duplicate event ID checks, and idempotency tests for decision/lifecycle upserts.
- Contract parity tests using real BACKTEST/PAPER output fields and runtime paper execution field checks.
- Backtest quality distribution reporting (`backtest_quality_summary.csv`) with reject-rate/reason distributions and effective-vs-raw RR divergence visibility.
- Quality-summary tests validating effective RR and reject-reason distribution accounting.

### Changed
- Backtest lifecycle CSV export source shifted to persisted SQL lifecycle events with deterministic ordering (`event_ts,event_id`).
- Backtest lifecycle persistence path now persists in-memory lifecycle rows before CSV export.
- `execution_ctx_missing` persistence semantics normalized toward canonical 0/1-style behavior in schema/write path.
- Lifecycle persistence semantics updated so `effective_rr` is used when available, falling back to raw `rr` only when effective value is absent.

### Fixed
- Corrected semantic integrity issue where lifecycle persistence could incorrectly store raw `rr` instead of execution-adjusted `effective_rr`.
- Improved rejected lifecycle visibility by documenting persisted/exported SQL-backed rejected rows in backtest lifecycle output path.
- Reduced mixed-type persistence risk for `execution_ctx_missing` with explicit legacy compatibility handling.

### Known Issues
- LIVE mode remains not production-ready.
- Full optional-field and timestamp-typing parity across BACKTEST/PAPER/LIVE is still incomplete.
- Legacy SQLite stores may need migration/rebuild for canonical `execution_ctx_missing` persistence.
- Backtest top-N universe may depend on live Binance endpoints unless fixture mode is used.

- Added canonical contract utilities (`contracts.py`) for lifecycle transitions, reject reason normalization, and UTC timestamp normalization.
- Changed runtime lifecycle callbacks to emit deterministic contract fields (`lifecycle_event_type`, `lifecycle_state`, `timestamp`, `previous_lifecycle_state`).
- Fixed invalid lifecycle transition handling by explicitly emitting/persisting `ERROR` state semantics.
