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


## Generation 3 - Execution Realism Engine Hardening (2026-05-16)
### Added
- Shared deterministic execution-cost model with explicit missing-context semantics and completeness grading.
### Changed
- Effective RR now uses additive execution penalties (spread, slippage, latency, funding, liquidity) instead of optimistic proportional shortcut.
- Real-order decision payload now reports execution-cost completeness and missing fields.
### Fixed
- Unknown execution context now generates explicit rejection flags and does not silently act like measured zero cost.
### Breaking Changes
- Effective RR numeric behavior changed due to new penalty formulation.
### Known Issues
- Regime/liquidity band calibration remains config-light and should be tuned per venue/instrument.

## Generation 4 - Runtime Safety Controls & Reconciliation Layer (2026-05-16)
### Added
- Pre-trade runtime risk gates: global kill switch, stale market data rejection, spread/funding sanity gates, symbol cooldown, duplicate position guard, and max concurrent position guardrails.
- New lifecycle states for deterministic runtime execution and failure semantics (`ENTRY_PENDING`, `ENTRY_SUBMITTED`, `ENTRY_ACKNOWLEDGED`, `ENTRY_PARTIAL`, `ENTRY_FILLED`, `STOP_SUBMITTED`, `TAKE_PROFIT_SUBMITTED`, `CANCEL_REQUESTED`, `RECONCILIATION_REPAIR`, `EXECUTION_ERROR`, `EXCHANGE_REJECT`, `RUNTIME_PROTECTIVE_EXIT`).
- Runtime incident counters and reconciliation repair journaling payloads.
- Lifecycle persistence migration columns: `failure_reason`, `reconciliation_reason`, `incident_payload`.
### Changed
- Runtime accepted-flow lifecycle moved from generic waiting/triggered placement to deterministic entry submission/ack/fill sequencing.
- Timeout/error/missing-ack execution outcomes now emit explicit failure lifecycle events and trigger reconciliation events.
### Fixed
- Reduced silent runtime/exchange drift by forcing uncertain execution outcomes into auditable failure + reconciliation lifecycle rows.
### Known Issues
- Exposure/concentration gate inputs are presently inference-light and depend on market context quality.
- Reconciliation currently journals snapshots but does not yet perform active exchange order amendment/cancel calls.


## Generation 5 - Live Readiness Qualification & Controlled Enablement (2026-05-17)
### Added
- `src/alphaforge/live_readiness.py` deterministic qualification engine with lifecycle/persistence/runtime/statistical/operational gates.
- Qualification report persistence table `live_readiness_reports` and forensic qualification snapshot export helper.
- Runtime LIVE gating config flags for shadow mode, canary mode, and explicit operator acknowledgement.
- Focused tests covering qualification pass/fail, lifecycle orphan detection, runtime live-block behavior, and forensic snapshot integrity.
### Changed
- `RuntimeOrchestrator.start()` now fail-closes LIVE startup when readiness qualification fails.
- LIVE startup now logs readiness report payload for deployment-state visibility and audits.
### Known Issues
- Reconciliation checks currently consume deterministic snapshots and do not yet issue active exchange remediation actions.

## Generation 6 - CSV Export Schema Drift Hardening (2026-05-17)
### Added
- `resolve_csv_fieldnames(rows, preferred_fieldnames)` helper in `backtest_order.py` to build deterministic union CSV schemas.
- Regression test for base-column preservation + alphabetically appended discovered columns.
### Changed
- Row-list CSV export path now derives headers from all rows (preferred base columns first, extra discovered keys appended alphabetically).
### Fixed
- Resolved backtest CSV export failure: `ValueError: dict contains fields not in fieldnames` when later rows include keys missing from the first row.
### Known Issues
- Downstream consumers with rigid CSV header expectations may need to tolerate additive columns.

## Generation 6 - Exchange-Reconciled Live Control Plane (2026-05-17)
### Added
- New reconciliation subsystem `src/alphaforge/reconciliation.py` with structured findings, repair recommendations, snapshot model, and incident persistence table/index creation.
- Continuous runtime reconciliation loop with bounded interval/timeout controls and deterministic fail-closed escalation on severe findings/timeouts.
- Reconciliation incident SQL persistence (`reconciliation_incidents`) and deterministic forensic payload serialization.
- New focused test module `tests/test_reconciliation.py` covering orphan/stale/divergence detection, persistence, no duplicate repair triggers, fail-closed behavior, and snapshot replay consistency.
### Changed
- Runtime orchestration now tracks pending orders and emits reconciliation lifecycle repair events from deterministic findings.
### Known Issues
- Exchange/account snapshots are currently runtime-fed abstractions and require deeper live adapter telemetry lineage for full venue-truth supervision.

## Generation 7 - Runtime Bootstrap Entrypoint & Safe Startup Loop (2026-05-17)
### Added
- Runtime module async bootstrap (`main`) with environment-driven orchestrator construction.
- Executable module entrypoint (`asyncio.run(main())`) for `python -m alphaforge.runtime`.
- Safe default no-op market scanner for bootstrap startup without feed wiring.
- Runtime tests for bootstrap env parsing, loop liveness-until-shutdown, and dynamic RR propagation.
### Changed
- Runtime startup now emits explicit startup/shutdown logs and uses env-configured execution mode/intervals.
### Fixed
- Resolved immediate process exit when invoking `python -m alphaforge.runtime` by adding executable bootstrap path.
### Known Issues
- Default bootstrap scanner is intentionally no-op; production feed/adapters must still be wired externally.
## Generation 7 - Production-grade Environment Template & Safety Configuration (2026-05-17)
### Added
- Rebuilt `.env.example` as a grouped, execution-aware template with conservative safety defaults and inline operational comments.
- Added explicit mode-separation variables for BACKTEST/PAPER/LIVE, plus live-readiness, reconciliation, reject-quality, and execution-risk controls.
- Added placeholders for Binance/Hyperliquid/API/notifications/redis/queue integrations expected by the runtime architecture roadmap.
### Changed
- README now documents safe `.env` bootstrap, mode switching, and live-trading risk warnings.
### Known Issues
- Some template variables are forward-compatible operational toggles and are not yet wired by direct `os.getenv` reads in current modules.
