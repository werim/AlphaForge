## 2026-06-23 Work 1.1 SQLite schema bootstrap stabilization

### Added
- Added regression coverage proving fresh `init_db()` creates `timesfm_forecast_evidence` and `ix_timesfm_evidence_symbol_timeframe_ts`, repeated bootstrap remains idempotent, and the conservative TimesFM evidence columns are present.
- Added conservative TimesFM evidence compatibility columns `forecast_timestamp`, `point_forecast`, and `quantiles_json` to fresh SQLite and Alembic bootstrap DDL.

### Changed
- Kept TimesFM DDL ordered as tables first and indexes after their target tables, with additive legacy-column repair for existing SQLite databases.

### Fixed
- Fixed Work 1.1 bootstrap completeness risk by ensuring the TimesFM evidence table shape exists before its lookup index is created on fresh SQLite databases.

### Removed
- Nothing.

### Breaking Changes
- None.

### Known Issues
- LIVE remains NOT READY by default; this patch only stabilizes SQL schema bootstrap behavior.

## 2026-06-23 SQLite/Alembic config snapshot trigger repair

### Added
- Added idempotent SQLite append-only trigger repair for `config_snapshots` in the Alembic runtime bootstrap migration after the table existence check.

### Changed
- Documentation now records the schema bootstrap trigger-ordering repair and successful regression test run.

### Fixed
- Fixed partial legacy Alembic upgrade paths where `config_snapshots` could be defensively recreated by the runtime bootstrap revision without restoring its SQLite no-update/no-delete triggers.

### Removed
- Nothing.

### Breaking Changes
- None.

### Known Issues
- LIVE remains NOT READY by default; this patch only repairs schema bootstrap metadata and does not alter trading behavior.

## 2026-06-23 Persistence/lifecycle contract regression coverage

### Added
- Added regression coverage for the legacy scalar `fetch_expectancy_stat(...)` contract, separate expectancy metadata detail helper, idempotent legacy runtime-column repair, and accepted backtest `WAITING_ENTRY_ZONE` ordering.

### Changed
- Documentation now records that the persistence/lifecycle contract fixes are guarded by executable tests.
- SQLite legacy lifecycle table repair now adds base lifecycle audit columns before creating the uniqueness index.

### Fixed
- Protected against regressions that would return structured fallback dictionaries from the scalar expectancy API, omit legacy compatibility columns during SQLite bootstrap repair, or skip `WAITING_ENTRY_ZONE` before accepted backtest entry triggers.
- Fixed scalar expectancy SQL execution under SQLAlchemy 2 by wrapping dynamic SELECT statements in executable `text(...)`.

### Removed
- Nothing.

### Breaking Changes
- None.

### Known Issues
- LIVE remains NOT READY by default; this patch adds regression coverage and does not change trading thresholds.

## 2026-06-23 SQLite/Alembic bootstrap regression hardening

### Added
- Added direct regression coverage for TimesFM DDL helper ordering so the evidence table remains before its dependent index.
- Added direct regression coverage that `_apply_sqlite_migrations()` creates `schema_migrations` before reading applied versions on partial SQLite databases.
- Added Alembic fresh-head regression coverage that `config_snapshots` append-only triggers exist after table creation.

### Changed
- Documentation now records the schema bootstrap control-flow protections explicitly.

### Fixed
- Fixed `_apply_sqlite_migrations()` so partial databases that have `schema_migrations` bootstrapped but do not yet have optional lifecycle/review tables do not execute dependent `ALTER TABLE` or lifecycle index DDL against absent tables.

### Removed
- Nothing.

### Breaking Changes
- None.

### Known Issues
- LIVE remains NOT READY by default; this patch only strengthens schema bootstrap tests and documentation.

## 2026-06-23 SQLite/Alembic schema bootstrap repair

### Added
- Added an Alembic runtime bootstrap revision for `timesfm_forecast_evidence`, `timesfm_forward_outcome_labels`, and defensive `config_snapshots` repair on partial legacy databases.
- Added regression coverage for TimesFM SQLite bootstrap order, idempotent row preservation, and Alembic head table/index creation.

### Changed
- Centralized TimesFM SQLite DDL so the evidence table is always emitted before its dependent index.

### Fixed
- Fixed fresh SQLite bootstrap risk where `CREATE INDEX ... ON timesfm_forecast_evidence(...)` could run before the evidence table existed.
- Fixed Alembic fresh-head coverage for runtime TimesFM evidence tables and partial legacy `config_snapshots` absence.

### Removed
- Nothing.

### Breaking Changes
- None.

### Known Issues
- LIVE remains NOT READY by default; this patch only repairs schema bootstrapping and does not alter trading readiness gates.

## 2026-06-23 BACKTEST/PAPER pre-submit parity adapter

### Added
- Added `evaluate_paper_style_pre_submit(...)` for BACKTEST/PAPER no-submit parity checks using shared candidate-quality and execution-cost gates.
- Added parity tests for LOW_SCORE, LOW_EFFECTIVE_RR, EXPECTANCY_MISSING, HIGH_SPREAD, accepted lifecycle audit sequence, and rejected lifecycle audit sequence.

### Changed
- BACKTEST can now run PAPER-style pre-submit effective-RR execution checks without Binance live-order calls when using the adapter.

### Fixed
- Documented and tested the remaining gap between `backtest_order.py` scan flow and `RuntimeOrchestrator._process_symbol`.

### Removed
- Nothing.

### Breaking Changes
- None.

### Known Issues
- The adapter does not enable LIVE and does not remove PAPER runtime-only gates such as kill switch, stale data, cooldown, funding sanity, or exposure limits.


## [Unreleased] - 2026-06-23 (LIVE readiness aggregator CI repair)

### Added
- None.

### Changed
- Kept final gate display on the readiness page while preserving the legacy 27-item readiness probe catalog/API contract.
- TimesFM futures tests now skip cleanly when optional NumPy is unavailable.

### Fixed
- Fixed dashboard readiness probe matrix counts inflated by duplicating final gates into the legacy probe catalog.

### Removed
- Removed final aggregate gates from the probe catalog; they remain in readiness report JSON and dashboard gate tables.

### Breaking Changes
- None.

### Known Issues
- LIVE remains NOT READY by default; final readiness still requires every local gate to pass.

## [Unreleased] - 2026-06-22 (LIVE readiness final gate aggregator)

### Added
- Added a sixteen-gate final LIVE readiness aggregator with explicit verdict levels and machine-readable blockers.
- Added persisted `verdict`, `gates`, and `blockers` fields inside readiness report JSON.
- Added dashboard final-gate table coverage and regression tests for missing gates, lower-gate precheck readiness, kill-switch blocking, and TimesFM non-ordering safety.

### Changed
- Runtime now requires `LIVE_REAL_ORDERS_READY` before allowing LIVE real-order mode; lower verdicts fail closed.
- Dashboard readiness probes include final aggregate gates in addition to underlying evidence checks.

### Fixed
- Closed the gap where partial readiness evidence could be read as a generic pass/fail without an explicit final verdict contract.

### Removed
- None.

### Breaking Changes
- None at schema level; readiness JSON consumers should handle additional fields.

### Known Issues
- LIVE remains NOT READY by default. Missing or stale local evidence for any final gate remains blocking, and PAPER or TimesFM success cannot promote LIVE.

## [Unreleased] - 2026-06-22 (PAPER burn-in report generator)

### Added
- Added `alphaforge.paper_burnin` SQL-first CLI/report generator that writes `paper_burnin_summary.csv`, `paper_burnin_report.md`, and `paper_burnin_blockers.json`.
- Added burn-in classifications for selectivity, insufficient samples, data integrity, lifecycle integrity, execution context, observability, reconciliation, and LIVE-blocked posture.
- Added regression tests for empty DBs, missing reject reasons, bad lifecycle ordering, missing execution context, fake zeros, healthy synthetic selectivity, and optional TimesFM absence.

### Changed
- Documentation now includes PAPER burn-in usage and explicitly keeps LIVE readiness blocked unless independent readiness evidence exists.

### Fixed
- Closed the reporting gap where PAPER evidence had to be inspected manually across multiple SQL tables without a deterministic blocker artifact.

### Removed
- None.

### Breaking Changes
- None; reporting is additive and does not alter runtime decisions, thresholds, schemas, or order paths.

### Known Issues
- LIVE remains NOT READY. Missing heartbeat, reconciliation, rollback, observability, operator, and execution evidence must remain blockers.

## [Unreleased] - 2026-06-22 (Execution realism evidence contract)

### Added
- Added execution evidence classifier statuses: `COMPLETE_MEASURED`, `PARTIAL_ESTIMATED`, `UNAVAILABLE_BLOCKING`, and `INVALID_FAKE_ZERO`.
- Added persisted effective-RR breakdown fields for raw RR and spread, slippage, latency, liquidity, funding, and volatility penalties.
- Added regression tests for missing execution evidence, fake-zero detection, BACKTEST estimated labeling, LOW_EFFECTIVE_RR, LIVE_PRECHECK readiness blocking, and persisted penalty evidence.

### Changed
- PAPER/LIVE-style order prechecks now require measured execution evidence and fail closed on missing or fake-zero required fields.
- BACKTEST estimates must be explicitly labeled as estimated evidence rather than implied zero-cost realism.

### Fixed
- Fixed unavailable execution costs being normalized toward neutral defaults in order evidence.
- Fixed effective-RR evidence gaps by persisting the full penalty breakdown.

### Removed
- None.

### Breaking Changes
- None for valid evidence; incomplete or fake-zero execution contexts now block readiness/precheck paths more explicitly.

### Known Issues
- LIVE remains NOT READY. Measured exchange evidence, reconciliation, rollback, observability, heartbeat, canary, shadow, and operator readiness remain required.

## [Unreleased] - 2026-06-22 (LIVE_PRECHECK no-submit parity evidence)

### Added
- Added `LIVE_PRECHECK` execution mode for evidence-only PAPER parity checks without exchange mutation.
- Added persisted LIVE_PRECHECK evidence fields for input snapshot hash, no-submit verification, parity result, and execution context.
- Added runtime/readiness tests for parity, no-submit behavior, persisted evidence, missing execution context, mismatch blocking, and LIVE_REAL_ORDERS lockout.

### Changed
- Live readiness mode parity now requires explicit no-submit verification and complete execution-context evidence.

### Fixed
- Closed the gap where parity evidence could be considered without persisted no-submit evidence or execution-context completeness.

### Removed
- None.

### Breaking Changes
- None for existing modes; `order_decisions` receives nullable additive columns.

### Known Issues
- LIVE remains NOT READY. LIVE_PRECHECK parity alone does not satisfy reconciliation, rollback, observability, heartbeat, canary, shadow, operator, or real adapter readiness gates.

## [Unreleased] - 2026-06-22 (Dashboard test import CI repair)

### Added
- None.

### Changed
- Imported SQLAlchemy `create_engine` in dashboard tests so audit-event queries execute in full CI.

### Fixed
- Fixed full-suite CI failures in dashboard kill-switch/audit tests caused by a missing test import.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- LIVE remains NOT READY; no runtime behavior changed.

## [Unreleased] - 2026-06-21 (Dashboard kill switch/PAPER-LIVE fail-closed audit)

### Added
- Added persisted `runtime_control_audit_events` records for mode-switch attempts and kill-switch ON/OFF actions.
- Added dashboard tests for kill-switch rendering, restart persistence, switch audit events, LIVE readiness lockout, and secret redaction in HTML responses.
- Added runtime regression coverage proving persisted kill-switch state prevents scanner work.

### Changed
- Dashboard LIVE mode selection now requires explicit operator acknowledgement and PASS readiness evidence before changing requested mode.
- Dashboard overview now displays NOT LIVE-READY messaging when readiness evidence is absent or failing.

### Fixed
- Fixed unsafe dashboard mode-switch ambiguity by auditing blocked LIVE attempts with explicit failure reasons instead of silently accepting LIVE as requested mode.

### Removed
- None.

### Breaking Changes
- Dashboard requests to set LIVE mode now fail closed unless readiness evidence is PASS and acknowledgement is supplied.

### Known Issues
- LIVE remains NOT READY. This patch does not add real order placement or make existing readiness blockers pass.

## [Unreleased] - 2026-06-21 (Rejected decision SQL/CSV integrity)

### Added
- Added canonical rejected-decision artifact persistence that writes signal, order decision, and lifecycle rows with one stable `signal_id`.
- Added regression coverage for LOW_SCORE, RR_TOO_LOW, EXPECTANCY_MISSING, REGIME_MISMATCH, SPREAD_TOO_HIGH, SLIPPAGE_TOO_HIGH, VOLATILITY_TOO_HIGH/LOW, rejected SQL/CSV parity, and unknown reject refusal.

### Changed
- BACKTEST rejected rows now include stable `signal_id`, lifecycle state, execution-context-missing status, expectancy bucket, and cost-adjusted `effective_rr`.
- PAPER runtime reject persistence now uses the canonical rejected-decision helper and carries reject metadata into lifecycle persistence.

### Fixed
- Fixed rejected artifacts that could be persisted with partial SQL evidence, missing stable IDs, or raw RR reused as effective RR despite execution penalties.

### Removed
- None.

### Breaking Changes
- None for valid rejected artifacts; attempts to persist rejected artifacts with empty/UNKNOWN reasons fail closed.

### Known Issues
- LIVE remains NOT READY. Exchange/order reject detail quality still depends on adapter evidence.

## [Unreleased] - 2026-06-21 (Backtest lifecycle truth audit hardening)

### Added
- Added export integrity checks for rejected lifecycle SQL versus `rejected_orders.csv` count consistency.
- Added regression tests for legacy `CREATED`, CREATED-only lifecycle exports, missing lifecycle state/status, fake zero execution context, and suspicious constant score/RR distributions.

### Changed
- BACKTEST export verification now treats persisted lifecycle rows as the audit source and fails closed on lifecycle/reject/export truth defects.

### Fixed
- Fixed audit coverage gaps where malformed lifecycle exports, missing rejected rows, and missing execution context represented as numeric zero could pass verification.

### Removed
- None.

### Breaking Changes
- None for valid exports; invalid BACKTEST artifacts now fail closed instead of being accepted.

### Known Issues
- LIVE remains NOT READY. BACKTEST execution context is still limited by available historical metadata and conservative estimates.

## [Unreleased] - 2026-06-21 (Dashboard runtime control safety hardening)

### Added
- Added persisted runtime control state for requested mode, running mode, kill-switch state/source/time, runtime status, and last error.
- Added dashboard runtime mode, start, stop, kill-switch, and control-status endpoints.
- Added runtime-control and dashboard regression tests for kill switch, PAPER start, LIVE fail-closed errors, duplicate start prevention, and stopped transitions.

### Changed
- Runtime now checks the persisted/global kill switch before startup, scan processing, signal-to-order transition, and PAPER/LIVE execution action.
- Dashboard overview now displays requested mode, actual running mode, runtime status, kill-switch state, last change metadata, and last error.

### Fixed
- Fixed cosmetic-only dashboard control risk by wiring controls to runtime control state and supervisor behavior.
- Fixed silent mode-drift risk by rejecting mismatches between dashboard requested mode and constructed runtime mode.

### Removed
- Removed the prior read-only-only dashboard route assumption for kill-switch/runtime controls; order submission routes remain absent.

### Breaking Changes
- None.

### Known Issues
- LIVE remains NOT READY unless existing runtime/live-readiness guards independently pass.
- Dashboard runtime supervision is intentionally small and fail-closed; external process management may still be preferred operationally.

## 2026-06-19 Dashboard historical refresh hotfix

### Added
- Added `--force-refresh` CLI support for backtest historical candle hydration.
- Added regression coverage for dashboard force-refresh commands, stale-cache refresh attempts, and clean insufficient-data failures.

### Changed
- Dashboard BACKTEST runs now always request fresh Binance historical candles for the selected symbols, timeframe, and period before simulation.

### Fixed
- Fixed stale candle caches causing immediate `HistoricalDataError` failures when cached coverage started after the requested start.
- Fixed dashboard historical data failures to return a clean FAILED result with an operator-facing insufficient-data message.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- Fresh Binance historical data remains dependent on Binance API availability and coverage.

## [Unreleased] - 2026-06-19 (Dashboard BACKTEST control panel)

### Added
- Added a dashboard "Run Backtest" form with last-days, comma-separated symbols, safe timeframe selection, initial balance, and max-symbol controls.
- Added a BACKTEST-only dashboard runner wrapper around the existing `backtest_order.py` pipeline and result artifact summarization.
- Added dashboard tests for form rendering, validation, BACKTEST-only runner invocation, safe failure rendering, and unavailable lifecycle/execution warnings.

### Changed
- Overview dashboard now includes a clearly labeled BACKTEST ONLY control section and result panel.

### Fixed
- Dashboard no longer requires operators to leave the web UI for simple bounded backtest launches while preserving the no-LIVE/no-PAPER safety boundary.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- Dashboard backtest execution is synchronous and may time out on large historical windows.
- Historical data, lifecycle accuracy, and execution context fidelity remain limited to what the existing backtest pipeline and artifacts can provide.

## [Unreleased] - 2026-06-19 (Alembic revision graph integrity repair)

### Added
- Added Alembic revision-graph regression coverage for dangling `down_revision` references, script directory head resolution, and temporary SQLite `upgrade head` execution when Alembic is installed.

### Changed
- Restored the Phase 1 base migration revision identifier to `0001_phase1_init` so the existing adaptive learning lifecycle migration's `down_revision` points to a present base revision.

### Fixed
- Fixed Alembic graph loading failure where `0002_adaptive_learning_lifecycle` referenced missing revision `0001_phase1_init` while the base migration file declared `revision = "0001_phase1"`.

### Removed
- None.

### Breaking Changes
- None for fresh databases. Existing databases stamped with the erroneous `0001_phase1` revision require explicit operator review rather than blind stamping.

### Known Issues
- This patch repairs migration metadata lineage only; it does not change LIVE readiness, execution realism, or runtime trading behavior.

## [Unreleased] - 2026-06-19 (SQLite schema migration bootstrap legacy regression hardening)

### Added
- Extended legacy SQLite bootstrap regression coverage to assert `schema_migrations` is created and records the persistence migration exactly once across repeated `init_db(...)` calls.

### Changed
- Documentation now records the schema migration bootstrap regression hardening follow-up.

### Fixed
- Guarded against regressions where partial legacy SQLite databases with existing runtime rows but no `schema_migrations` table could fail before migrations are applied.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- LIVE readiness remains unchanged and blocked.

## [Unreleased] - 2026-06-19 (SQLite rollback evidence bootstrap)

### Added
- Fresh SQLite bootstrap now creates the canonical `live_rollback_validation_evidence` rollback evidence table and index idempotently.
- Regression coverage verifies rollback evidence schema creation during `init_db(...)`.

### Changed
- SQLite migrations now record the rollback evidence bootstrap migration without changing trading thresholds, scoring, reject logic, lifecycle semantics, or runtime decision behavior.

### Fixed
- Dashboard/readiness rollback evidence queries no longer depend on a later write path to create the rollback evidence table in fresh SQLite databases.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- LIVE readiness remains blocked; this is persistence bootstrap hardening only.

## [Unreleased] - 2026-06-19 (SQLite schema migration bootstrap regression)

### Added
- Added regression coverage proving fresh SQLite initialization creates `schema_migrations` before migration version reads.

### Changed
- Isolated SQLite migration bookkeeping bootstrap in an explicit helper while preserving the same idempotent schema.

### Fixed
- Fixed the migration bootstrap contract so `schema_migrations` exists before selecting applied migration versions.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- LIVE readiness remains unchanged and blocked.

## [Unreleased] - 2026-06-19 (TimesFM unbatched quantile + integration smoke hardening)

### Added
- Added unbatched TimesFM ndarray tuple regression tests for `(horizon, 10)` mean-plus-decile and `(horizon, 9)` older quantile layouts.
- Added an optional real TimesFM integration smoke gated by `ALPHAFORGE_RUN_TIMESFM_INTEGRATION=1`.
- Added a LIVE-mode rejection regression for the TimesFM replay API.

### Changed
- NumPy-shaped TimesFM tests now import NumPy directly so declared dev dependency coverage is not silently skipped in normal development environments.
- TimesFM tuple quantile parsing now uses quantile-specific batch detection to support both batched and unbatched output matrices.

### Fixed
- Fixed unbatched quantile matrices being interpreted as if the first horizon row were the entire forecast series.

### Removed
- Removed NumPy `importorskip` gates from TimesFM ndarray regression tests.

### Breaking Changes
- None.

### Known Issues
- Optional real TimesFM smoke requires externally installed/configured TimesFM package and model weights.
- This module remains PAPER/BACKTEST only and does not add LIVE or order-placement capability.

## [Unreleased] - 2026-06-19 (TimesFM post-merge compatibility hardening)

### Added
- Added deterministic TimesFM wrapper tests for tuple-style point/quantile outputs, NumPy array-like outputs, mean-plus-q10...q90 quantile layout, older nine-quantile layout, legacy `freq` forecast calls, malformed output rejection, and replay `INVALID_FORECAST` logging.
- Added NumPy to development/test dependencies for real ndarray-shaped TimesFM output regression coverage.

### Changed
- Hardened the TimesFM wrapper to try compatible 1.x/2.x forecast call surfaces before failing closed.
- Extended TimesFM output parsing to accept sequence-like list, tuple, and ndarray outputs without requiring NumPy at runtime.

### Fixed
- Fixed quantile extraction for real-shaped TimesFM tuple output where column 0 is a mean and p10 starts at the q10 column, preventing mean values from being mislabeled as p10.
- Fixed malformed tuple output handling so invalid forecasts raise `TimesFMForecastError` and replay records `INVALID_FORECAST` instead of crashing.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- TimesFM inference still requires an externally installed/configured `timesfm` package/model.
- Forecast replay still does not model spread, slippage, funding, liquidity, latency, or live execution and must not be used for LIVE orders.

## [Unreleased] - 2026-06-19 (TimesFM BTCUSDT futures PAPER/BACKTEST forecasting)

### Added
- Added BTCUSDT Binance USD-M Futures OHLCV loader support for 15m and 1h TimesFM research inputs.
- Added a TimesFM forecaster wrapper that lazily uses the optional `timesfm` package and exposes quantile forecasts for horizons 8, 16, and 24.
- Added PAPER/BACKTEST-only historical replay that passes only candles visible at decision time to prevent lookahead bias.
- Added CSV decision logging fields for timestamp, symbol, timeframe, current price, forecast quantiles, side, entry, stop, take-profit, expected RR, and rejection reason.
- Added tests for no-lookahead replay, invalid forecast rejection, LONG, SHORT, and NO_TRADE decisions.

### Changed
- REPORT.md and VERSION.md now document TimesFM module behavior, limitations, and live-readiness impact.

### Fixed
- None.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- TimesFM inference requires an externally installed/configured `timesfm` package/model.
- Forecast replay does not model spread, slippage, funding, liquidity, latency, or live execution and must not be used for LIVE orders.

## [Unreleased] - 2026-05-22 (JOB19 V1 audit diagnostics only)

### Added
- Added `sql/diagnostics/job19_paper_reject_rate_decision_quality_audit.sql` with reusable PAPER runtime SQL checks for reject-rate, reject-reason completeness, missing critical fields, duplicate/inconsistent decisions, score/RR variability, execution-context availability, and lifecycle consistency.

### Changed
- Updated `REPORT.md` with JOB19 V1 audit scope, evidence limitations, and classification framework (`HEALTHY_SELECTIVITY`, `DATA_INTEGRITY_FAILURE`, `EXECUTION_CONTEXT_FAILURE`, `SCORING_OR_REGIME_PIPELINE_FAILURE`, `INSUFFICIENT_SAMPLE`).

### Fixed
- None.

### Removed
- None.

### Breaking Changes
- None.

### Known Issues
- Real verdict remains blocked without repository-accessible PAPER runtime SQLite evidence artifact.

## [Unreleased] - 2026-05-22 (LIVE qualification startup persistence and forensic redaction precision follow-up)

### Changed
- LIVE qualification startup no longer persists reconciliation findings to `reconciliation_incidents`; fail-closed qualification logic still uses canonical reconciliation counters.
- Qualification observability snapshot now sets `incident_persistence_verified=false` during startup evidence evaluation.
- Mode parity numeric evidence parsing is now defensive and fail-closed on invalid/placeholder values without raising exceptions.

### Fixed
- Forensic runtime snapshot sanitation now preserves benign keys containing `signed` (for example `assigned_symbols`) while still redacting signed/auth/secret payload values and sensitive nested keys.

### Added
- Regression coverage for fail-closed qualification on canonical orphan/duplicate findings without incident writes.
- Regression coverage for invalid parity numeric evidence persistence and non-throwing fail-closed behavior.
- Regression coverage proving `assigned_symbols` survives sanitation while signed/auth/signature values are redacted.

## [Unreleased] - 2026-05-22 (Evidence-based LIVE readiness qualification hardening)

### Added
- Structured fail-closed readiness evidence contract checks for mode parity, observability, and rollback/emergency controls.
- Forensic snapshot sanitization that removes key/secret/signature/signed-header style fields from persisted runtime snapshot payloads.
- Readiness tests covering parity minimum-sample enforcement and forensic secret redaction.

### Changed
- `mode_parity` qualification now requires COMPLETE evidence with minimum samples, zero mismatches, zero missing fields, and no-order-submission verification.
- Observability/rollback readiness checks now require measured evidence fields instead of optimistic booleans.

### Fixed
- Closed gap where static `alerts_configured` / `rollback_ready` booleans could qualify LIVE without measured evidence.

### Known Issues
- LIVE remains ❌ NOT LIVE-READY; alert delivery evidence is still blocking.

## [Unreleased] - 2026-05-22 (LIVE canonical reconciliation evidence-chain hardening)

### Added
- Duplicate-fill detection in canonical reconciliation (`DUPLICATE_FILL`) using exchange `trade_id` with documented fallback compound key.
- Canonical reconciliation finding summary adapter for readiness counters (`orphan_orders`, `orphan_positions`, `duplicate_fills`, `lifecycle_divergences`, `fail_closed_findings`, `stale_orders`).
- LIVE qualification tests proving provider optimistic counters cannot bypass canonical orphan/position detection.

### Changed
- LIVE qualification now passes provider raw `orders`/`positions`/`fills` through `ReconciliationEngine.reconcile(...)` and ignores provider orphan/duplicate summary claims.
- LIVE runtime/readiness fails closed on incomplete reconciliation evidence and on any fail-closed canonical finding.

### Fixed
- Closed false-qualification gap where provider-supplied zero orphan/duplicate counters could be trusted without runtime-intent comparison.

### Known Issues
- LIVE remains ❌ NOT LIVE-READY; no real order submission/cancellation/modification is introduced.

## [Unreleased] - 2026-05-22 (Authenticated Binance read-only LIVE reconciliation evidence)

### Added
- `BinanceReadonlyReconciliationProvider` with signed USER_DATA GET support for `/fapi/v1/openOrders`, `/fapi/v3/positionRisk`, and symbol-scoped `/fapi/v1/userTrades`.
- Deterministic mocked unit tests for request signing, credential redaction, hedge-mode position normalization, and fail-closed behavior.
- Runtime env gate test for LIVE fail-closed when read-only reconciliation is enabled without credentials.

### Changed
- Runtime env bootstrap now supports explicit read-only reconciliation toggles and bounded recvWindow/lookback configuration.
- LIVE runtime wiring can attach read-only reconciliation provider only when explicitly enabled and full credentials are present.

### Fixed
- Closed gap where LIVE had no authenticated exchange reconciliation evidence provider implementation.

### Known Issues
- LIVE remains ❌ NOT LIVE-READY; no real order submission/execution adapter is implemented.

## [Unreleased] - 2026-05-22 (LIVE qualification evidence fail-closed + reconciliation provider requirement)

### Added
- Runtime tests for deterministic scanner provenance assignment and stricter LIVE provenance allowlist blocking.
- LIVE readiness tests asserting fail-closed qualification details are persisted with explicit missing-evidence reasons.
- Runtime reconciliation test asserting LIVE mode blocks when no reconciliation provider is configured.

### Changed
- LIVE startup scanner provenance gate now requires explicit allowlisted provenance (`EXCHANGE_PUBLIC_MARKET_DATA`) instead of blacklist-only checks.
- Runtime bootstrap now deterministically assigns scanner provenance (`SAFE_PLACEHOLDER` override vs exchange-backed source).
- LIVE qualification no longer injects optimistic hardcoded mode parity/reconciliation/observability evidence.

### Fixed
- `_build_runtime_from_env()` now always assigns `scanner_source` before `RuntimeOrchestrator` construction.
- LIVE reconciliation now fail-closes when no explicit reconciliation provider exists, preventing in-memory-only snapshots from being treated as exchange truth.

### Known Issues
- LIVE remains ❌ NOT LIVE-READY; this patch does not add real order placement or authenticated exchange reconciliation reads.

## [Unreleased] - 2026-05-22 (P0 LIVE startup safety + Binance Futures consistency)

### Added
- Runtime regression tests for LIVE scanner provenance blocking and early missing-real-adapter startup blocking.
- Connectivity regression tests asserting Binance Futures endpoint family usage and funding fail-closed behavior.
- Config regression test for default Binance Futures host when `BINANCE_BASE_URL` is unset.

### Changed
- Runtime orchestrator now uses explicit scanner provenance (`scanner_source`) for LIVE startup safety gating.
- Binance connectivity probe now validates Futures orderbook and funding endpoints used by runtime scanner.
- Binance config default/fallback base URL now defaults to `https://fapi.binance.com`.

### Fixed
- Closed LIVE startup bypass where safe scanner could be wrapped by `_runtime_market_scanner` and evade name-based detection.
- Closed delayed LIVE startup failure path by blocking early when `real_execution_adapter` is not configured.
- Removed Spot endpoint qualification path for Binance Futures runtime readiness.

### Known Issues
- LIVE remains ❌ NOT LIVE-READY; no real trading adapter/order placement was enabled.

## [Unreleased] - 2026-05-22 (Binance Futures bookTicker spread hardening follow-up)

### Changed
- Binance scanner now uses Futures-only public endpoint family: `/fapi/v1/ticker/24hr`, `/fapi/v1/ticker/bookTicker`, `/fapi/v1/premiumIndex`.
- Binance `entry` is now conservative (`min(last_price, mid)`), while `spread_pct`/`spread_bps` are derived from `bookTicker` bid/ask.

### Fixed
- Removed Spot `/api/v3` dependency from Binance scanner path.
- Added fail-closed symbol filtering when `bookTicker` spread inputs are unavailable/malformed, avoiding optimistic synthetic spread.

### Added
- Deterministic tests for Futures endpoint URL usage, spread mapping, and malformed payload behavior.

## [Unreleased] - 2026-05-21 (Runtime/env failing-test triage audit)

### Changed
- Added audit documentation for reported runtime/env failures after git pull; current branch reproduces all targeted tests as passing under isolated execution.

### Fixed
- No runtime code fix required on current branch; issue characterized as likely stale DB/env-state contamination outside deterministic test isolation.

### Known Issues
- Historical failure reference includes a now-missing test node name (`test_runtime_rejected_decisions_do_not_persist_incomplete_rows`), suggesting rename/removal drift across branches/CI runs.
## [Unreleased] - 2026-05-21 (Read-only exchange scanner bootstrap alignment)

### Added
- `src/alphaforge/exchange_market_scanner.py` with read-only public Binance/Hyperliquid market scanning (no private API keys, no order submission).
- Tests in `tests/test_exchange_market_scanner.py` for deterministic mocked scanner behavior and exchange-failure fallback.

### Changed
- Runtime bootstrap now uses shared exchange scanner for PAPER/LIVE and reserves `_safe_market_scanner` for BACKTEST/offline override (`ALPHAFORGE_RUNTIME_SAFE_SCANNER=1`).

### Fixed
- Removed placeholder single BTC runtime scanner from default PAPER/LIVE path so runtime rehearsal uses real market-data shape.

## [Unreleased] - 2026-05-21 (LIVE connectivity default fail-closed + startup consistency)

### Changed
- LIVE startup now requires exchange connectivity by default (`RuntimeConfig.require_exchange_connectivity_for_live=True`).
- Runtime env bootstrap now wires `ALPHAFORGE_REQUIRE_EXCHANGE_CONNECTIVITY_FOR_LIVE`, `ALPHAFORGE_REQUIRED_LIVE_EXCHANGES`, and `ALPHAFORGE_EXCHANGE_CONNECTIVITY_TIMEOUT_SEC`.

### Added
- Regression test proving LIVE startup fails closed on exchange connectivity by default.
- Regression test proving LIVE connectivity gate can only be skipped via explicit override.
- Regression assertion covering default non-impact for PAPER mode configuration path.

## [Unreleased] - 2026-05-21 (LIVE placeholder scanner fail-closed gate)

### Fixed
- LIVE startup now blocks when runtime is wired to the bootstrap placeholder scanner (`_safe_market_scanner`) to prevent synthetic local feed use in LIVE mode.

### Added
- Runtime regression test ensuring LIVE cannot start with placeholder/mock bootstrap scanner wiring.

## [Unreleased] - 2026-05-21 (Exchange connectivity safety + opt-in integration checks)

### Added
- New `ExchangeHealth` contract and `check_exchange_connectivity(exchange_name)` for Binance/Hyperliquid public connectivity checks.
- New offline deterministic test module `tests/test_exchange_connectivity.py` with mocked success/failure coverage for Binance and Hyperliquid.
- Optional live integration tests marked `integration`, gated behind `ALPHAFORGE_RUN_EXCHANGE_INTEGRATION=1`.

### Changed
- Runtime LIVE startup can now optionally enforce exchange connectivity via `RuntimeConfig.require_exchange_connectivity_for_live` and `required_live_exchanges`.
- Pytest marker configuration now includes `integration: tests requiring live external services`.

### Fixed
- Missing connectivity failure path coverage for LIVE runtime startup safety.

### Known Issues
- Connectivity checks are public endpoint-only and do not place/cancel orders.

## [Unreleased] - 2026-05-21 (Runtime order_decisions audit semantics hardening)

### Fixed
- Runtime AI/internal `:real:` persistence no longer writes `mode=BACKTEST` during PAPER runtime; mode now reflects the actual runtime mode.
- Canonical runtime rejected decision rows now persist `phase=final` plus score/RR fields so final reject audits are not sparse.

### Changed
- `AIBrain` internal audit rows are now explicitly marked as `phase=ai_internal_real|ai_internal_virtual` to distinguish them from canonical runtime final decisions.
- Live-readiness reject-rate and persistence parity checks now count only canonical final order decisions (`COALESCE(phase,'final')='final'`) to prevent internal-audit double-count inflation.

### Added
- Regression test for PAPER runtime rejected persistence path asserting: no runtime-created row uses `mode=BACKTEST`, canonical final rejected row is populated and counted once, and AI/internal row is explicitly marked non-final.

## [Unreleased] - 2026-05-21 (Runtime rejected decision row completeness)

### Fixed
- `order_decisions` AI persistence rows for runtime `phase=real` rejections now persist non-empty `symbol` and canonical `reject_reason` instead of sparse incomplete duplicates.

### Changed
- Decision persistence upsert payload now includes `symbol`, `mode`, `score`, and `rr` for improved rejected-decision audit completeness.

### Added
- Regression coverage asserting rejected runtime rows do not include incomplete `:real:` paired rows with empty `symbol`/`reject_reason`.

## [Unreleased] - 2026-05-21 (Runtime signal identity + diagnostics hardening)

### Changed
- Runtime now generates/propagates non-empty `signal_id` before reject/lifecycle persistence, including fallback deterministic identity when absent in candidate payload.
- AI decision persistence now derives decision ids with market timestamp entropy to prevent repeated runtime decisions from collapsing into a single upsert row.

### Fixed
- Runtime reject persistence now maps concrete reject reasons into `reject_reason` so known reasons are no longer downgraded to `UNKNOWN`.
- Runtime decision exceptions now emit diagnostic-rich `ERROR` lifecycle events with non-empty `failure_reason` and structured `incident_payload`.
- Runtime lifecycle persistence callback now forwards failure/incident fields explicitly into `trade_lifecycle_events` persistence.

### Added
- Regression tests covering runtime reject signal_id propagation, reject reason preservation, runtime exception lifecycle diagnostics, and decision/features persistence consistency across repeated runtime decisions.


## [Unreleased] - 2026-05-21 (Lifecycle persistence strict-boolean return fix)

### Fixed
- `save_trade_lifecycle_event(...)` now returns literal `True` after successful insert/update + commit instead of returning integer-like row identifiers/rowcount values.
- Preserved existing lifecycle persistence SQL/upsert behavior, event-id generation, and failure semantics.
## [Unreleased] - 2026-05-21 (Runtime SQLite thread-safety fix)

### Changed
- Runtime decision path no longer dispatches `AIBrain.before_real_order` through `asyncio.to_thread`, preventing cross-thread SQLite session use during persistence.
- `AIBrain` now supports `session_factory` for session-per-operation persistence while preserving injected `session` compatibility.

### Fixed
- Resolved `sqlite3.ProgrammingError` caused by reusing a SQLAlchemy `Session` across worker threads in decision persistence flow.

### Added
- Regression test validating `AIBrain` persistence succeeds when `before_real_order` is invoked via `asyncio.to_thread` concurrently using short-lived sessions.



## [Unreleased] - 2026-05-20 (Phase 6.1 audit-trail canonicalization)

### Changed
- Runtime PAPER/backtest-facing lifecycle emissions now prefer canonical lifecycle vocabulary (`SIGNAL_CREATED`, `WAITING_ENTRY_ZONE`, `ENTRY_TRIGGERED`, `ORDER_PLACED`, `SIGNAL_REJECTED`, `POSITION_OPENED`).
- Runtime reject path now guarantees `SIGNAL_REJECTED` emission after `SIGNAL_CREATED` before returning.

### Fixed
- Persistence helpers `save_order_decision` / `save_trade_lifecycle_event` now attempt real durable inserts and return explicit failure on SQL exceptions for fail-detectable behavior.
- Runtime lifecycle persistence callback now fail-detects unsuccessful lifecycle inserts instead of silently continuing.

### Added
- Runtime tests for canonical PAPER lifecycle ordering and reject ordering guarantees.
- Contract transition support for canonical `ENTRY_TIMEOUT` state used in timeout/reconciliation path.

# Changelog

## [Unreleased] - 2026-05-21 (Rejected-shadow SHORT TP/SL fix)

### Fixed
- Rejected-shadow counterfactual simulation now applies directional TP/SL touch rules by side (`LONG`: high>=tp/low<=sl, `SHORT`: low<=tp/high>=sl), fixing SHORT false-negative WOULD_TP outcomes.
- Same-candle TP+SL ambiguity handling is now explicitly documented and conserved as SL-priority for both LONG and SHORT to avoid optimistic bias when intrabar path is unknown.

### Added
- Unit coverage for rejected counterfactual LONG/SHORT TP-only, SL-only, and same-candle TP/SL ambiguity scenarios.
- Regression coverage asserting a valid SHORT rejected setup can produce `shadow_outcome=WOULD_TP` with `effective_tp_hit=True` when effective RR and cost/liquidity/volatility filters pass.

## [Unreleased] - 2026-05-20 (SQLite schema bootstrap compatibility)

### Added
- Idempotent SQLite schema bootstrap helper using `PRAGMA table_info(...)` + additive `ALTER TABLE ... ADD COLUMN` for legacy runtime/backtest DBs.
- Coverage tests for legacy `order_decisions` / `ai_decision_features` migration repair and repeated `init_db()` idempotency.

### Fixed
- Runtime/backtest persistence crashes on existing SQLite files missing additive columns such as `order_decisions.phase` and `ai_decision_features.decision_id`.
- Addeditive schema compatibility checks for write paths that persist lifecycle and closed-trade review payload fields.

## [Unreleased] - 2026-05-20 (Runtime bootstrap smoke scanner + PAPER default)

### Added
- Runtime bootstrap `_safe_market_scanner` now emits one deterministic local smoke-test market candidate with full selector/risk/AI-required fields to exercise scanner→selection→AI→lifecycle→persistence wiring.

### Changed
- Runtime execution mode defaults now resolve to `PAPER` when `EXECUTION_MODE` is absent (`execution_mode_from_env` and `RuntimeConfig.execution_mode`).

### Fixed
- Removed bootstrap behavior that silently defaulted to BACKTEST on missing `EXECUTION_MODE`.

### Known Issues
- Bootstrap scanner remains intentionally synthetic and must not be treated as a live market feed.

All notable documented repository-level changes are summarized from `REPORT.md`.

## [Unreleased] - 2026-05-19 (Spread-unit + calibration/lifecycle persistence fixes)

### Fixed
- Symbol selector spread gating now uses fractional spread semantics (`max_spread_pct=0.0025`) so `0.0035` (0.35%) correctly triggers `WIDE_SPREAD` without weakening other reject gates.
- Backtest symbol market-data builder now propagates `spread_unit_assumed` diagnostics for explicit spread normalization audits.
- Backtest calibration snapshot export insert path no longer assumes an optional `payload_json` column, preventing SQLite runtime failures on existing schemas.
- `ForwardWindowEvaluation` construction is now backward-compatible in tests/callers that omit setup/score/RR quality fields.

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

## [Unreleased] - 2026-05-17 (Backtest lifecycle accounting fix)

### Fixed
- Backtest lifecycle persistence now marks `SIGNAL_CREATED` as `PENDING` (not `ACCEPTED`) and treats `SYMBOL_REJECTED` as `REJECTED` to prevent contradictory terminal decisions.
- Backtest summary counters now derive candidates/rejections by final per-signal terminal decision and count `total_orders` from `ORDER_PLACED` rows only.

### Added
- Regression tests for symbol-level reject decision integrity and per-signal terminal decision deduplication in backtest summary accounting.

## [Unreleased] - 2026-05-17 (Setup quality diagnostics)

### Added
- Rejected/accepted candidate export diagnostics: `raw_rr`, `effective_rr`, `min_required_score`, `trend_strength`, `volatility_pct`, `range_position`, `slippage_pct`, `first_blocking_gate`, `all_failed_gates`.
- Quality summary percentiles for score/raw RR/effective RR and slice distributions by setup type/regime reject reason.
- Near-threshold LOW_SCORE rejection counter for calibration analysis.

### Changed
- Trade-quality diagnostics now record `all_failed_gates` in addition to first blocking gate.

### Known Issues
- Setup generation heuristics remain simplistic and may still overproduce weak breakout-style candidates in choppy regimes.


## Generation 9 - Adaptive Learning Data Foundation (2026-05-17)
### Added
- Deterministic SQL-first adaptive learning module `src/alphaforge/adaptive_learning.py` with closed/rejected review persistence, adaptive stats aggregation, reject-accuracy computation, expectancy bucket classification, and shadow-threshold recommendation logic.
- New persistence tables: `rejected_signal_reviews`, `adaptive_stats`, `adaptive_threshold_snapshots`; expanded `closed_trade_reviews` schema for execution-aware review fields.
- Config safety flags for adaptive foundation (disabled learning by default, shadow mode default on, clamp and sample controls).
- Adaptive learning foundation tests in `tests/test_adaptive_learning_foundation.py`.
### Changed
- `AIBrain` now records adaptive review rows for closed trades and rejected decisions without changing acceptance/execution behavior.
### Known Issues
- Forward-labeling for rejected-signal outcome quality remains null until Generation 2 outcome-label jobs are added.
## [Unreleased] - 2026-05-17 (Regime gate initialization hotfix)

### Fixed
- Resolved `UnboundLocalError` in trade-quality evaluation by initializing `regime_ok` before gate checks.
- Preserved deterministic regime reject behavior (`REGIME_MISMATCH`) while preventing crashes for candidates with missing regime values.

### Added
- Regression tests covering missing-candidate-regime non-crash behavior and incompatible market-regime rejection behavior.

## [Unreleased] - 2026-05-18 (Backtest lifecycle summary reconciliation)

### Fixed
- Reconciled backtest summary counting semantics so `total_candidates = accepted_count + rejected_count` using signal-level lifecycle identities.
- Corrected `total_orders` meaning to accepted order objects (`WAITING_ENTRY_ZONE`) rather than candidate-level/event-level drift.
- Added explicit `rejected_count` in main order summary while preserving `total_rejected` alias for compatibility.
- Aligned quality summary denominator/reject accounting to signal-level candidates (`SIGNAL_CREATED`) to prevent candidate-vs-event mismatch.

### Added
- Regression test proving quality summary candidate denominator uses signal-level rows and matches reject distribution semantics.

## [Unreleased] - 2026-05-18 (Adaptive persistence compatibility hotfix)

### Fixed
- `build_backtest_quality_summary(...)` now supports both plain decision-row inputs and lifecycle-row inputs, with candidate counting based on `SIGNAL_CREATED` when present and direct-row counting fallback when absent.
- Restored legacy `closed_trade_reviews.execution_metrics` population in adaptive closed-trade persistence path to prevent NULL JSON in execution-layer review queries.
- Ensured SQLite migration/bootstrapping adds `execution_metrics` column when absent and uses SQLAlchemy `text(...)` for robust inserts in `save_closed_trade_review`.

### Added
- No architectural changes; patch is compatibility-focused and regression-safe.

## [Unreleased] - 2026-05-18 (Generation N+2 Forward Reject Telemetry Foundation)

### Added
- Deterministic forward-window evaluator (`evaluate_forward_window`) for lifecycle/reject rows with labels: `would_have_hit_tp`, `would_have_hit_sl`, `mfe_pct`, `mae_pct`, `max_forward_return`, `max_adverse_return`, `reject_correct`, `reject_missed_winner`, `reject_saved_from_loss`, `forward_window_minutes`, `forward_window_regime`, `execution_quality_bucket`.
- Adaptive stats scope entrypoint `update_adaptive_stats_by_scope(...)` supporting additive reject-learning scopes (`REJECTION_REASON`, execution/volatility/spread/liquidity/trend/session/timeframe buckets).
- Determinism regressions for forward-window replay stability and scoped reject-accuracy aggregation.

### Changed
- Reject quality telemetry can now be aggregated by richer scopes without enabling autonomous threshold mutation.

### Known Issues
- Forward-window outputs are generated deterministically but are not yet persisted into dedicated SQL tables in this generation.

## [Unreleased] - 2026-05-18 (Generation N+2 evaluator wiring + calibration snapshots)

### Added
- Post-terminal forward evaluator wiring in backtest export flow via `build_forward_evaluation_rows(...)` with terminal-only trigger semantics.
- Immutable/idempotent `calibration_snapshots` persistence table (unique by `signal_id`, `forward_window_minutes`, `realized_outcome`).
- New exports: `forward_evaluations.csv` and `calibration_snapshots.csv`.
- Expanded adaptive scope-key test coverage for regime/setup/timeframe/session/volatility/spread/liquidity/trend/rejection/execution-quality keys.

### Changed
- Forward evaluator remains post-decision analytics-only and now runs only for terminal outcomes (`TP_HIT`, `SL_HIT`, `EXPIRED`, `CANCELED`, `OPEN_AT_END`, `REJECTED`).
## [Unreleased] - 2026-05-18 (Generation N+2 Wiring: terminal forward-eval + calibration persistence)

### Added
- Terminal-state forward evaluator trigger wiring in backtest flow (post-lifecycle only) with CSV exports: `forward_evaluations.csv`, `adaptive_scope_stats.csv`, `calibration_snapshots.csv`.
- Immutable/idempotent `calibration_snapshots` persistence table (`UNIQUE(signal_id, forward_window_minutes)`, insert-do-nothing).
- Adaptive scope export payload covering regime/setup/timeframe/session/volatility/spread/liquidity/trend/rejection_reason/execution_quality dimensions.

### Fixed
- Forward evaluator remains isolated from same-signal decision path and now executes only on terminal closed lifecycle outcomes.


## [Unreleased] - 2026-05-19
### Added
- Probabilistic score payload in `AIBrain` scoring output and persisted execution feature metadata (`probabilistic_score`).
- Conservative prior warning (`CONSERVATIVE_PRIOR_NO_HISTORY`) when sample history is absent.
### Changed
- Score acceptance now requires probabilistic constraints (minimum `p_win`, execution success, confidence, positive expectancy-after-costs, fakeout cap) in addition to aggregate score.
- Rejected-signal review reason assignment now maps from probabilistic failure flags before falling back to `LOW_SCORE`.
### Fixed
- Reduced static scalar-score dependence by blending weighted legacy score with calibrated probabilistic score.


## [Unreleased] - 2026-05-19 (Forensic lifecycle audit documentation)

### Added
- Production-grade forensic lifecycle audit report in `REPORT.md` covering signal generation, scoring, regime gating, execution-penalty formulas, lifecycle path analysis, and root-cause matrix.

### Changed
- Updated `VERSION.md` with forensic audit status notes and explicit architecture-level findings summary.

### Known Issues
- Backtest candidate generation remains long-only in current builder path.
- Effective-RR formulation divergence exists between backtest-local helper and shared runtime cost model.
- Score-to-expectancy calibration remains weak for executable post-cost edge.

## [Unreleased] - 2026-05-19 (Backtest lifecycle calibration parity hardening)

### Added
- Backtest candidate generation now supports mirrored SHORT breakdown candidates (`BREAKDOWN_DOWN` / `CLOSE_BELOW_PREV_LOW`) alongside LONG breakout candidates.
- Execution diagnostics now include decomposed penalty fields (`cost_penalty_total`, `spread_penalty`, `slippage_penalty`, `latency_penalty`, `liquidity_penalty`, `funding_penalty`).
- Unit-assumption visibility fields (`spread_unit_assumed`, `slippage_unit_assumed`) now flow from execution-context construction.

### Changed
- Backtest effective-RR rejection helper now uses additive shared execution-cost-model semantics to align with runtime/PAPER/LIVE evaluation behavior.
- Spread/slippage inputs now normalize percent-point inputs into fractional-rate contract values before penalty modeling.

### Fixed
- Removed backtest-only multiplicative effective-RR divergence for order-level reject diagnostics.
- Reduced silent unit ambiguity risk where values like `0.1` could be interpreted inconsistently across callers.

## [Unreleased] - 2026-05-19 (PAPER SQLite bootstrap + diagnostics hardening)

### Added
- Runtime bootstrap diagnostics for SQLite path resolution, schema init confirmation, and discovered table names.
- Heartbeat diagnostics for persistence enabled state and selection/decision gate blockers.
- Tests covering PAPER schema bootstrap with empty cycles and absolute DB path logging.

### Changed
- Runtime scanner now captures top symbol selection reject reasons even when no symbols are selected.
- Runtime env bootstrap now wires reject/lifecycle callbacks to SQL persistence in PAPER/BACKTEST/LIVE runtime path (when enabled).

### Fixed
- PAPER runtime persistence observability gaps that obscured whether SQL schema initialization and persistence callbacks were active.

## [Unreleased] - 2026-05-19 (Rejected shadow gate diagnostics + STOP_TOO_WIDE rescue simulation)

### Added
- Rejected shadow export fields for gate score provenance (`gate_score`, `low_score_gate_score`) and STOP_TOO_WIDE rescue simulation outputs.
- Reject-reason diagnostics aggregation including row count, TP opportunity rates, effective TP rates, mean score/RR/cost-penalty, and top symbols/regimes.

### Changed
- Rejected shadow evaluation now normalizes `spread_pct` units before execution-penalty evaluation.
- Symbol market-data builder now normalizes `actual_spread_pct` to the same unit contract used by gate thresholds.

### Fixed
- Low-score gate/CSV observability gap by exporting the actual gate score used by rejection logic.

## [Unreleased] - 2026-05-20 (Lifecycle ordering audit hotfix)

### Fixed
- Restored backtest lifecycle ordering so `WAITING_ENTRY_ZONE` is emitted instead of being overwritten to `SIGNAL_CREATED` in `simulate_candidate(...)`.

### Changed
- Added explicit dev-branch design compliance audit section to `REPORT.md` with truthful status and remaining architecture gaps.

## [Unreleased] - 2026-05-20 (Backtest lifecycle/persistence/reporting defects)

### Fixed
- Lifecycle persistence upsert now supports composite lifecycle uniqueness `(signal_id,event_ts,lifecycle_state)` with compatibility fallback to `event_id` conflict handling.
- Backtest summary `total_orders` now counts unique `ORDER_PLACED` lifecycle keys (no longer reports zero when placed rows exist).
- Backtest summary `triggered_orders` now counts unique `ENTRY_TRIGGERED` keys; `not_triggered_orders` now uses accepted WAITING paths that never triggered/placed.
- Lifecycle SQL export ordering now uses deterministic lifecycle-aware sort keys (`event_ts,symbol,signal_id,lifecycle_seq,lifecycle_state,event_id`).

### Added
- Regression tests for lifecycle composite-key idempotency, not-triggered counting semantics, lifecycle sequence monotonicity, and LOW_SCORE rescue/watch diagnostic-only field semantics.

### Changed
- LOW_SCORE rescue/watch outputs are explicitly diagnostics-only and remain excluded from accepted/order/win-rate/realized-PnL aggregates.

## [Unreleased] - 2026-05-21 (Phase 6.1 audit-trail canonicalization merge conflict resolution)

### Changed
- PAPER accepted lifecycle path is canonicalized to emit `SIGNAL_CREATED -> WAITING_ENTRY_ZONE -> ENTRY_TRIGGERED -> ORDER_PLACED` before execution simulation.
- Runtime lifecycle persistence callback is now fail-closed and raises when lifecycle SQL persistence reports failure.

### Fixed
- `save_order_decision(...)` now returns an explicit failure indicator (`None`) on SQL exceptions instead of silently pretending success.
- `save_trade_lifecycle_event(...)` now returns explicit `False` when both lifecycle upsert strategies or commit fail.
- Added regression coverage for canonical PAPER ordering and lifecycle persistence failure detectability.


## [Unreleased] - 2026-05-21 (Persistence API compatibility + lifecycle sequencing)

### Added
- New `fetch_expectancy_stat_detail(...)` helper for metadata consumers while preserving legacy scalar expectancy API behavior.
- Legacy compatibility columns auto-repair in SQLite bootstrap: `order_decisions.payload`, `trade_lifecycle_events.trade_id`, `trade_lifecycle_events.state`, `trade_lifecycle_events.payload`.

### Changed
- `fetch_expectancy_stat(...)` contract restored to return `float | None` for backward compatibility.
- `save_trade_lifecycle_event(...)` now returns inserted/upserted row id and backfills legacy `trade_id/state/payload` fields.

### Fixed
- `save_order_decision(...)` now persists legacy `payload` JSON consistently, including rejected decision context.
- Backtest accepted lifecycle now includes `WAITING_ENTRY_ZONE` before `ENTRY_TRIGGERED`.

## 2026-05-21 config centralization
- Added centralized env config loading and runtime/exchange/backtest wiring updates.

## 2026-05-22
- Added deterministic historical Binance Futures replay provider with paginated kline fetching, gap checks, and funding joins.
- Backtest runtime now treats synthetic scanner as smoke-test only and labels market_data_source=SYNTHETIC_SMOKE_TEST when enabled.

## 2026-05-22 PR #148 follow-up (LIVE qualification non-mutating parity fix)

### Changed
- LIVE qualification mode parity evidence now runs through side-effect-free scoring/planning/explanation calls and no longer calls persistence-capable `before_real_order(...)`.
- Qualification parity samples now use stable fixture sample IDs and stable fixture market timestamps to keep parity inputs deterministic across repeated runs.

### Fixed
- Removed synthetic LIVE qualification probe mutation of `signals`, `order_decisions`, `ai_decision_features`, `trade_lifecycle_events`, and rejected-review tables during parity evaluation.
- Added regression coverage for non-mutating parity evidence and deterministic replay parity output (excluding `generated_at`).

## [JOB-22A] - 2026-05-24
### Changed
- Canonical execution evidence now preserves measured/modeled/unavailable provenance through scanner->runtime->persistence paths.
### Fixed
- Removed optimistic zero-default persistence for spread/slippage/latency when evidence is unavailable.
### Known Issues
- effective_rr currently remains equal to rr in persisted decisions and is still unresolved.

## [Unreleased] - 2026-06-21 (P0-3 TimesFM canonical evidence integration)

### Added
- Added canonical `timesfm_forecast_evidence` SQL persistence for TimesFM research decisions.
- Added stable TimesFM `forecast_id` generation and no-lookahead input end timestamp tracking.
- Added optional `timesfm_forward_outcome_labels` schema for future calibrated outcome labeling.
- Added tests for SQL TimesFM evidence persistence, stable IDs/idempotency, CSV evidence fields, and invalid forecast persistence.

### Changed
- TimesFM CSV decision logs now include canonical evidence fields and model/provider metadata.

### Fixed
- TimesFM forecast evidence is no longer CSV-only when a persistence session is supplied.

### Removed
- Nothing.

### Breaking Changes
- None. Schema changes are additive and TimesFM remains PAPER/BACKTEST only.

### Known Issues
- Forward outcome calibration is not implemented yet; TimesFM should remain isolated research evidence until calibrated.
- LIVE readiness remains NOT READY and TimesFM has no order authority.
