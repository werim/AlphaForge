## 2026-06-25 - Dashboard Calibration Test Import CI Fix

### Why the patch was needed
CI exposed a `NameError: name 'os' is not defined` in the dashboard calibration regression test after calibration artifact path assertions were added.

### Root cause
`os` was imported only inside the mocked `fake_run()` helper, so the outer test assertion scope could not call `os.path.exists(...)`.

### Files changed
- `tests/test_dashboard_app.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
None. Production dashboard calibration logic, thresholds, lifecycle semantics, and accepted/rejected counts are unchanged.

### Lifecycle changes
None.

### Persistence changes
None.

### Export/schema changes
None.

### Tests executed
- `pytest -q tests/test_dashboard_app.py::test_dashboard_backtest_shows_top_rejection_reasons_and_diagnostics -q` (local optional dashboard dependencies unavailable)
- `pytest -q`

### Risks and remaining limitations
This is a test-only CI fix. Local targeted dashboard test execution still depends on optional FastAPI/httpx packages being installed.

### Push recommendation
Safe to push as a minimal test import fix.

## 2026-06-25 - Lifecycle Calibration Later-Gate CI Fix

### Why the patch was needed
CI exposed a `ValueError: too many values to unpack` in the lifecycle calibration later-gate diagnostic loop when passed score/RR/expectancy rows existed.

### Root cause
The later-gate builder iterated over a set of `(reason, source_stage)` tuples but attempted to unpack each item as `((reason, stage), rows)`, even though no grouped row list existed.

### Files changed
- `src/alphaforge/dashboard/backtest_control.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
No trading thresholds, accepted trade counting, lifecycle states, or rejected-shadow semantics changed. Later-gate diagnostics now use an explicit grouped dictionary before computing counts and rates.

### Lifecycle changes
None. `WOULD_TP` rejected rows remain rejected counterfactual diagnostics.

### Persistence changes
None.

### Export/schema changes
No artifact names or schemas were removed; later-gate summary rows now include grouped count fields without crashing.

### Tests executed
- `pytest -q tests/test_dashboard_app.py::test_dashboard_backtest_shows_top_rejection_reasons_and_diagnostics -q` (environment skipped collection because FastAPI/httpx are not installed locally)
- `python -m py_compile src/alphaforge/dashboard/backtest_control.py`
- `pytest -q`

### Risks and remaining limitations
Dashboard-specific targeted tests require optional dashboard dependencies in this local container; full local pytest still passes with dashboard tests skipped by dependency guards. CI with dashboard dependencies should exercise the fixed test directly.

### Push recommendation
Safe to push as a minimal CI fix; no threshold or lifecycle acceptance behavior changed.

## 2026-06-25 - Lifecycle Calibration Dashboard Report Patch

### Why the patch was needed
Rejected shadow persistence made `order_lifecycle.csv` more truthful, but dashboard analytics still mixed pre-signal `SYMBOL_SELECTOR` rejects with actionable `SIGNAL_ENGINE` rejects and did not expose later-gate diagnostics for candidates that already passed score/RR/expectancy.

### Root cause
Dashboard BACKTEST summarization read `rejected_orders.csv` as a single rejection pool. That hid whether reasons such as `TOO_CHOPPY` and `WEAK_TREND_AND_NO_RANGE_EDGE` were pre-signal selector filters or final actionable signal rejects, and it provided no calibration artifact for LOW_SCORE shadows, execution-cost summaries, or near-miss later-gate rejects.

### Files changed
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_dashboard_app.py`
- `tests/test_execution_layer.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
No threshold, scoring, reject, acceptance, or order execution logic was loosened. Dashboard BACKTEST completion now writes `lifecycle_calibration_report.csv` and `lifecycle_calibration_summary.json` next to existing artifacts.

### Lifecycle changes
Lifecycle state semantics are unchanged. `WOULD_TP`, `WOULD_SL`, and `WOULD_TIMEOUT` remain counterfactual rejected-shadow labels only and never convert a rejected row into an accepted trade.

### Persistence changes
No database migration or persistence schema change was added. The patch consumes existing CSV/export fields defensively and tolerates older rows with missing execution context fields.

### Export/schema changes
Added dashboard-generated calibration artifacts grouping by source stage, lifecycle state, reject reason, symbol, regime/volatility regime, and expectancy bucket. Dashboard wording now separates symbol-selector rejects, actionable signal rejects, and order/lifecycle rejects. Candle-only spread diagnostics are explicitly labeled as estimated when historical bid/ask is unavailable.

### Tests added
Added dashboard regression coverage for separated selector/actionable counts, LOW_SCORE WOULD_TP vs WOULD_SL comparison, later-gate traceability, calibration artifact generation, estimated spread labeling, and a cost-penalty/effective-RR single-application invariant.

### Tests executed
- `python -m py_compile src/alphaforge/dashboard/backtest_control.py`
- `pytest tests/test_execution_layer.py::test_effective_rr_cost_penalty_is_applied_once -q`
- `pytest -q`

### Risks and remaining limitations
The calibration report is diagnostic only; it does not decide whether LOW_SCORE is protective. Historical bid/ask is still unavailable in candle-only backtests, so spread/slippage remain estimates.

### Migration concerns
No migration required. CSV consumers can ignore the new dashboard-generated calibration artifacts.

### Push recommendation
Safe to push as a dashboard/reporting calibration patch; full local pytest passed. Not a LIVE readiness endorsement.


## 2026-06-25 - Rejected Shadow Backtest Export Patch

### Why the patch was needed
Rejected shadow CSV exports showed 4,322 rejected candidates with constant `liquidity_score` around 0.1, universally false `liquidity_ok`, near-constant estimated spread, and shadow outcomes that were not represented in lifecycle SQL/export rows. These rows are counterfactual rejected signal/order candidates, not executed orders.

### Root cause
`backtest_order.py::_build_market_ctx` correctly derived `volume_24h_usdt` from historical candle volume when ticker metadata was unavailable, but the liquidity score passed into execution context still read directly from missing `symbol_meta.quoteVolume` and clamped to a minimum. That made explicit-symbol historical runs default to thin liquidity, which also made the estimated spread nearly constant. Rejected shadow labels were written to `rejected_shadow.csv` only and were not attached to persisted lifecycle rows.

### Files changed
- `backtest_order.py`
- `tests/test_backtest_order_scanner.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
BACKTEST now derives liquidity score from available historical candle volume when exchange ticker quote volume is unavailable. Rejected shadow diagnostics are attached to matching rejected lifecycle decisions before SQL persistence.

### Lifecycle changes
Rejected rows remain `SIGNAL_REJECTED`/`ORDER_REJECTED`; `WOULD_TP` shadow outcomes are preserved only as counterfactual labels and do not approve execution or inflate accepted counts.

### Persistence changes
Lifecycle persistence execution context now includes rejected shadow outcome, cost penalty, liquidity/volatility scores, and liquidity/volatility gate booleans. The SQL lifecycle export projects these fields as CSV columns.

### Export/schema changes
No table migration was added. The in-memory SQL export query now exposes execution-context JSON fields as lifecycle CSV columns for rejected shadow analysis.

### Tests added
Added regression tests for rejected shadow lifecycle persistence, LOW_SCORE reject-reason retention, derived liquidity scoring, true liquidity gates, shadow outcome export preservation, WOULD_TP remaining rejected, and accepted-count invariance.

### Tests executed
- `pytest -q tests/test_backtest_order_scanner.py -q`
- `python backtest_order.py --offline --output-dir /tmp/af_out`

### Risks and remaining limitations
Historical bid/ask spread is still unavailable in offline candle-only backtests, so spread remains an execution-aware proxy rather than a measured historical spread. Liquidity is still normalized from quote-volume proxy and should be audited against exchange-specific depth data before LIVE readiness claims.

### Migration concerns
No database migration required. CSV consumers should tolerate additional lifecycle export columns.

### Push recommendation
Safe to push as a minimal SQL/export and backtest-proxy correction. Not a LIVE readiness endorsement.

## 2026-06-24 Backtest SYMBOL_REJECTED lifecycle ordering fix

### Why the patch was needed
Dashboard BACKTEST runs for BTCUSDT/ETHUSDT 15m could fail closed because `SYMBOL_REJECTED` selector diagnostics were exported under the same `<symbol>:<timestamp>` identity used by signal lifecycle rows.

### Root cause
`SYMBOL_REJECTED` is a pre-signal symbol selector rejection. The persistence/export path reused the default signal id format for selector diagnostics, so lifecycle integrity checks could interpret a selector diagnostic as a post-`SIGNAL_CREATED` state for the same signal.

### Files changed
- `backtest_order.py`
- `tests/test_backtest_order_scanner.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
BACKTEST selector rejects keep SQL-first persistence but use a `SYMBOL_SELECTOR:<symbol>:<timestamp>` diagnostic identity. If a `SYMBOL_REJECTED` row is produced after a signal exists, it is exported as `SIGNAL_REJECTED` with `reject_reason` preserved rather than as an invalid post-signal symbol state.

### Lifecycle changes
Valid pre-signal selector diagnostics remain visible as `SYMBOL_REJECTED`. Signal-level rejections after `SIGNAL_CREATED` export as `SIGNAL_REJECTED`. The validator remains fail-closed.

### Persistence changes
SQLite lifecycle persistence is preserved. No schema migration is required.

### Export/schema changes
`order_lifecycle.csv` and `rejected_orders.csv` may contain selector diagnostic ids prefixed with `SYMBOL_SELECTOR:`. No columns were added or removed.

### Tests added
Added tests for selector diagnostic ids, post-signal normalization, dashboard-symbol lifecycle validity, export transition validity, and reject-reason completeness.

### Tests executed
- `python -m pytest tests/test_backtest_order_scanner.py -q`
- `python -m pytest -q`

### Risks
Downstream CSV consumers that assumed every lifecycle `signal_id` is orderable must ignore or separately bucket `SYMBOL_SELECTOR:*` diagnostics.

### Remaining limitations
The patch does not add live exchange calls or synthesize unavailable execution fields.

### Migration concerns
No database schema migration is needed. Historical exports with colliding selector ids may need regeneration for dashboard validation.

### Push recommendation
Safe to push after full test pass; this is a minimal producer/exporter fix and does not loosen lifecycle validation.

## 2026-06-24 Backtest order lifecycle diagnostics hardening

### Why the patch was needed
New `order_lifecycle.csv` diagnostics showed selector-level `LOW_LIQUIDITY` rejects being exported as `SIGNAL_REJECTED`, while accepted-looking candidates stopped at `ORDER_REJECTED` and dashboard diagnostics could not separate selector rejects, signal rejects, and order rejects clearly.

### Root cause
`SYMBOL_REJECTED` was compatibility-mapped to `SIGNAL_REJECTED` in the canonical lifecycle contract, so selector rejects looked like signal-engine rejects after persistence. Separately, `SymbolSelectionResult.liquidity_score` returned a 0..10 sub-score while gates and execution contexts used a 0..1 threshold scale, creating a misleading scale surface even though the selector threshold itself normalized raw inputs before gating.

### Files changed
- `src/alphaforge/lifecycle_contract.py`
- `src/alphaforge/symbol_selector.py`
- `src/alphaforge/dashboard/backtest_control.py`
- `backtest_order.py`
- `tests/test_symbol_selector.py`
- `tests/test_backtest_order_scanner.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
BACKTEST selector rejects now persist/export as `SYMBOL_REJECTED` with selector source evidence instead of being normalized into `SIGNAL_REJECTED`. The selector result liquidity field now reports the normalized 0..1 liquidity contract, while the 0..10 display contribution remains in diagnostics under `sub_scores.liquidity_score`.

### Lifecycle changes
Added explicit canonical `SYMBOL_REJECTED` lifecycle state. This prevents selector-level rejects from being mislabeled as signal-level rejects and keeps the canonical order lifecycle states available for later accepted candidates without bypassing any gates.

### Persistence changes
No destructive migration. In-memory BACKTEST lifecycle persistence now writes `SYMBOL_REJECTED` directly and includes `source_stage=SYMBOL_SELECTOR` in event payload/execution context for selector rejects. Existing legacy rows are not rewritten.

### Export/schema changes
`rejected_orders.csv` selector rows now include `signal_id`, `lifecycle_state=SYMBOL_REJECTED`, `source_stage=SYMBOL_SELECTOR`, execution fields, and RR parity fields when available. Dashboard result objects now include lifecycle state counts, lifecycle path counts, final reject reason counts, order reject reason counts, and symbol-selector reject counts.

### Tests added
Added regressions for 0..1 liquidity-score result consistency, valid-liquidity high RR candidates not becoming `LOW_LIQUIDITY`, selector rejects persisting as `SYMBOL_REJECTED`, and export integrity detecting misleading selector-as-signal rejects.

### Tests executed
- `pytest -q tests/test_symbol_selector.py tests/test_backtest_order_scanner.py -q` — passed.

### Risks
This does not guarantee accepted trades; strict score/RR/execution gates can still reject all candidates. The patch intentionally does not loosen strategy thresholds or assume missing spread/slippage/funding is zero.

### Remaining limitations
Manual dashboard BTCUSDT/ETHUSDT last-10-days 15m verification depends on Binance/network access and must be rerun in an environment with successful data hydration.

### Migration concerns
No migration required. Downstream consumers that previously expected selector rejects to appear as `SIGNAL_REJECTED` should read `SYMBOL_REJECTED` or `source_stage=SYMBOL_SELECTOR` instead.

### Push recommendation
Safe to push after the full requested test matrix is run. Keep LIVE guarded / NOT READY.

## 2026-06-24 Dashboard rejection diagnostics and gate mapping audit

### Why the patch was needed
A completed dashboard BACKTEST for BTCUSDT over the last 10 days at 15m produced 958 candidates, 958 rejects, and zero accepted trades. Operators could see only aggregate counts, making it hard to separate low-score rejects from later gate failures or suspicious mapping/unit issues.

### Root cause
Dashboard result rendering did not inspect `rejected_orders.csv` beyond linking the artifact. Trade-quality regime alignment also treated BREAKOUT setups as compatible only with normal/high volatility labels, so a provider label of `breakout` could incorrectly surface as `REGIME_MISMATCH` even when setup and regime were aligned. Liquidity inputs accepted both 0..1 and 0..10-like values in different paths without an explicit normalization marker.

### Files changed
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `src/alphaforge/order.py`
- `src/alphaforge/symbol_selector.py`
- `tests/test_dashboard_app.py`
- `tests/test_trade_quality.py`
- `tests/test_execution_layer.py`
- `tests/test_symbol_selector.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
Dashboard BACKTEST results now derive rejection diagnostics from `rejected_orders.csv`: top reasons, signal lifecycle reject count, symbol-selector reject count, score distribution, raw RR distribution, effective RR distribution, and rows that pass score/RR/expectancy before later gates. BREAKOUT_UP/BREAKOUT candidates are no longer rejected solely because volatility was labeled `breakout` rather than normal/high.

### Lifecycle changes
No lifecycle states are collapsed or removed. The dashboard explicitly counts `SIGNAL_REJECTED` and `SYMBOL_SELECTOR_REJECT` rows separately while retaining total rejected rows.

### Persistence changes
No database schema changes and no DB data is dropped, recreated, or truncated. Diagnostics are read from generated CSV artifacts only.

### Export/schema changes
No CSV schema changes. The dashboard consumes existing fields when present and reports unavailable numeric distributions as unavailable/null summaries rather than fake zeros.

### Tests added
Added regressions for dashboard rejection diagnostics, BREAKOUT_UP + BREAKOUT regime alignment, effective RR percent/fraction unit consistency, and liquidity-score 0..10 input normalization.

### Tests executed
- `python -m pytest -q tests/test_trade_quality.py tests/test_execution_layer.py tests/test_symbol_selector.py tests/test_dashboard_app.py` — 46 passed, 1 skipped.
- `python -m pytest -q tests/test_dashboard_app.py` — skipped because `fastapi` is unavailable in this environment.
- `python -m pytest -q -k backtest` — 94 passed, 3 skipped, 278 deselected.
- `python -m pytest -q` — 365 passed, 10 skipped.
- `python backtest_order.py --mode BACKTEST --last-n-days 10 --symbols BTCUSDT --top-n 1 --interval 15m --balance 10000 --output-dir data/backtest/manual_dashboard_verification_20260624 --force-refresh` — failed with Binance proxy tunnel 403 before data hydration.

### Risks
The dashboard pass-count diagnostic uses existing CSV fields and defaults the minimum score to 7.5 when a row does not export `min_required_score`; this is diagnostic-only and does not affect trading decisions. BREAKOUT mapping is intentionally minimal and does not loosen score, RR, expectancy, spread, slippage, stop-width, cooldown, or effective-RR gates.

### Remaining limitations
The observed zero-trade run may still be valid if effective RR never reaches the required threshold after execution costs. The manual BTCUSDT last-10-days 15m BACKTEST command was attempted, but Binance access failed in this environment with a proxy tunnel 403 before data hydration.

### Migration concerns
None. No schema migration or artifact migration is required.

### Push recommendation
Safe to push after full tests pass. Keep LIVE guarded / NOT READY.

## 2026-06-24 Dashboard BACKTEST historical kline pagination diagnostics

### Why the patch was needed
Dashboard BACKTEST runs for BTCUSDT/ETHUSDT over the last 30 days at 1m could fail closed with a generic insufficient Binance historical data message even when the actionable problem was missing paginated coverage detail for a specific symbol/timeframe/request window.

### Root cause
The kline loader already used the Binance Futures public kline endpoint with `limit=1500` pagination, but coverage validation compared raw, non-timeframe-aligned request end timestamps against candle open timestamps. The dashboard then collapsed historical failures into a generic message, losing symbol, timeframe, expected count, and actual count context.

### Files changed
- `src/alphaforge/historical_market_data.py`
- `src/alphaforge/dashboard/backtest_control.py`
- `tests/test_historical_market_data.py`
- `tests/test_dashboard_app.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
BACKTEST historical hydration still uses public Binance USD-M Futures klines only and does not call live order/execution APIs. The loader now validates coverage against the first/last candle open expected within the requested period/timeframe and includes detailed expected/actual candle counts in `HistoricalDataError` messages.

### Lifecycle changes
No lifecycle transition semantics changed. Rejected/accepted signal flow and strategy logic are untouched.

### Persistence changes
No schema or database persistence behavior changed. Candle cache writing remains compatible; no fake candles or placeholder execution values are persisted.

### Export/schema changes
No CSV schema changes. Dashboard failed-result messaging now preserves detailed artifact/log reasons instead of hiding them behind an unavailable metric state.

### Tests added
Added focused historical ingestion/dashboard regressions for 30-day 1m pagination, clear insufficient-kline failures, multi-symbol failed-symbol diagnostics, and dashboard failure HTML preserving detailed historical-data reasons while metrics remain unavailable rather than silently zeroed.

### Tests executed
- `pytest tests/test_historical_market_data.py tests/test_dashboard_app.py -q` — 6 passed, 1 skipped.

### Risks
Low to medium. Validation is stricter and more explicit; genuine Binance gaps/rate-limit truncation still fail closed rather than fabricating missing candles.

### Remaining limitations
Funding-rate fetching remains a separate single call and was not expanded in this focused patch. Large synchronous dashboard 1m backtests can still be slow or fail if Binance is unavailable.

### Migration concerns
None. No schema, cache format, or strategy compatibility migration is required.

### Push recommendation
Safe to push after focused tests pass. Do not treat this as LIVE readiness; it only improves BACKTEST historical ingestion diagnostics and reliability.

## 2026-06-24 LIVE readiness input provenance hardening

### Why the patch was needed
LIVE readiness qualification still constructed some static reconciliation/observability truth inside `RuntimeOrchestrator._run_live_qualification_gate()`. That risked treating missing operational evidence as a configured readiness input.

### Root cause
The qualification bootstrap mixed real provider evidence with default dictionaries for reconciliation, observability, and rollback checks. Missing providers were not represented as first-class missing inputs with persisted provenance.

### Files changed
- `src/alphaforge/runtime.py`
- `src/alphaforge/live_readiness.py`
- `tests/test_live_readiness_security_regression.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
LIVE qualification now requires explicit exchange snapshot, observability, and rollback readiness providers. Missing providers produce fail-closed incomplete evidence rather than static passing values. Synthetic fixture-tagged inputs are rejected for LIVE readiness.

### Lifecycle changes
No lifecycle transition semantics changed. Existing lifecycle and reject-persistence gates remain part of readiness qualification.

### Persistence changes
`live_readiness_reports` is widened additively with nullable `readiness_inputs_json`. Each persisted report includes readiness input source/type/timestamp metadata in both the JSON payload and the new column.

### Export/schema changes
No CSV export behavior changed. SQLite schema change is additive and legacy report tables are repaired with `ALTER TABLE ... ADD COLUMN` when needed.

### Tests added
Added security regressions proving LIVE blocks when exchange snapshot, observability, or rollback probes are missing; LIVE pass wiring requires explicit non-synthetic providers and operator acknowledgement; and deterministic fixture inputs remain usable for offline PAPER/BACKTEST-style tests outside LIVE qualification.

### Tests executed
- `pytest -q tests/test_live_readiness_security_regression.py -q` — 10 passed.
- `pytest -q tests/test_live_readiness.py tests/test_runtime.py tests/test_runtime_control.py tests/test_sqlite_schema_bootstrap.py` — 77 passed, 3 skipped, 54 warnings.

### Risks
Medium. External LIVE bootstrap code must now pass explicit observability and rollback probe objects. This is intentional fail-closed behavior and may surface missing operational integrations earlier.

### Remaining limitations
This patch does not make LIVE trading ready. Existing dashboard/RBAC, heartbeat, burn-in, full-test, exchange, lifecycle, persistence, and operator acknowledgement gates remain required.

### Migration concerns
Additive SQLite-only report column is nullable and backward-compatible. Existing readiness rows remain readable, but older rows will have null `readiness_inputs_json`.

### Push recommendation
Safe to push after focused tests pass. Do not enable LIVE unless all final readiness gates are independently satisfied with fresh non-synthetic evidence.

## 2026-06-23 Work 1.3 Core identifier normalization

### Why the patch was needed
Work 1.1 and Work 1.2 stabilized SQLite bootstrap and Alembic alignment, but core lifecycle tables still had inconsistent identifier surfaces, limiting reliable reconstruction of signals, decisions, orders, positions, PAPER events, BACKTEST events, calibration labels, and optimizer runs.

### Root cause
The SQL bootstrap historically grew table-by-table. Some tables had only local IDs or legacy timestamps, while related tables lacked `signal_id`, `position_id`, `run_id`, `timeframe`, `mode`, or `updated_at` fields needed for conservative joins.

### Files changed
- `src/alphaforge/persistence.py`
- `alembic/versions/0005_core_identifier_normalization.py`
- `tests/test_sqlite_schema_bootstrap.py`
- `tests/test_alembic_revision_graph.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
No trading behavior changed. The patch only widens SQL tables additively and creates idempotent indexes for common joins.

### Lifecycle changes
No lifecycle transitions or reject semantics changed. Lifecycle reconstruction is improved by making expected identifiers available across persistence tables.

### Persistence changes
Added nullable identifier columns where missing for `signals`, `order_decisions`, `signal_id_state`, `orders`, `positions`, `fills`, `paper_events`, `backtest_runs`, `backtest_events`, `symbol_snapshots`, `timesfm_forecast_evidence`, `calibration_labels`, and `optimizer_runs`. Existing rows are preserved and no fake identifier backfill is performed.

### Export/schema changes
No CSV export behavior changed. Schema compatibility is additive and SQLite-safe.

### Tests added
Added core identifier schema assertions for fresh `init_db()`, fresh Alembic upgrade, `init_db() -> Alembic`, `Alembic -> init_db()`, important join indexes, and legacy insert-path compatibility.

### Tests executed
- `python -m pytest -q tests/test_sqlite_schema_bootstrap.py` — 14 passed, 3 skipped.
- `python -m pytest -q tests/test_alembic_revision_graph.py` — 1 passed, 2 skipped.
- `python -m pytest -q tests/test_runtime.py` — 35 passed, 54 warnings.
- `python -m pytest -q tests/test_dashboard_app.py` — 1 skipped; environment does not have dashboard optional dependencies active for this focused file.
- `python -m pytest -q` — 356 passed, 10 skipped, 165 warnings.

### Risks
Low-to-medium. Additive nullable columns and indexes are conservative, but legacy rows without deterministic identifiers remain null and require downstream consumers to tolerate unavailable IDs.

### Remaining limitations
This patch does not normalize historical data, modify runtime trading logic, introduce optimizer behavior, or make LIVE trading ready.

### Migration concerns
Alembic revision `0005_core_identifier_normalization` is additive only. Downgrade is intentionally non-destructive.

### Push recommendation
Safe to push if targeted and full pytest suites pass. LIVE remains NOT_READY.

## 2026-06-23 Work 1.2 Alembic/init_db baseline schema alignment

### Why the patch was needed
Work 1.1 stabilized fresh SQLite `init_db()` ordering, but Alembic and direct bootstrap could still drift. A database created by one path needed to be safely accepted by the other path without dropping or rewriting runtime evidence.

### Root cause
The Alembic base migrations used unconditional table/trigger creation, while `init_db()` owned several runtime baseline tables outside Alembic. That made `init_db() -> alembic upgrade head` vulnerable to duplicate-object failures and made Alembic-only fresh databases miss runtime baseline tables expected by AlphaForge tests and operators.

### Files changed
- `src/alphaforge/persistence.py`
- `alembic/versions/0001_phase1_init.py`
- `alembic/versions/0002_adaptive_learning_lifecycle.py`
- `alembic/versions/0003_sqlite_bootstrap_runtime_tables.py`
- `alembic/versions/0004_align_init_db_baseline_tables.py`
- `tests/test_sqlite_schema_bootstrap.py`
- `tests/test_alembic_revision_graph.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
None beyond schema availability. The direct SQLite bootstrap now creates the required baseline persistence tables additively with `CREATE TABLE IF NOT EXISTS`.

### Lifecycle changes
No lifecycle state machine or transition logic changed.

### Persistence changes
Added additive baseline coverage for `signals`, `order_decisions`, `signal_id_state`, `positions`, `orders`, `fills`, `paper_events`, `backtest_runs`, `backtest_events`, `symbol_snapshots`, `timesfm_forecast_evidence`, `runtime_control_state`, `calibration_labels`, and `optimizer_runs`. TimesFM index `ix_timesfm_evidence_symbol_timeframe_ts` is asserted in both paths.

### Export/schema changes
No CSV export logic changed. Schema migration behavior is now conservative/idempotent and avoids destructive downgrade operations in the touched migrations.

### Tests added
Added bootstrap-path coverage for fresh `init_db()`, fresh Alembic upgrade, `init_db() -> Alembic`, and `Alembic -> init_db()`, with required table/index assertions and repeated initialization safety.

### Tests executed
- `python -m pytest -q tests/test_sqlite_schema_bootstrap.py tests/test_alembic_revision_graph.py` — 13 passed, 3 skipped.
- `python -m pytest -q` — 354 passed, 8 skipped, 165 warnings.
- `python -m alembic heads` — environment warning: this checkout has a local `alembic/` package directory but the Alembic package console/module entry point is not installed, so Python reported no `alembic.__main__`.
- `alembic heads` / `alembic history` — environment warning: console script unavailable.

### Risks
Low-to-medium. The migration is additive and avoids data deletion, but legacy databases with divergent column types are intentionally handled conservatively by checking for required objects rather than byte-for-byte schema equality.

### Remaining limitations
Optional Alembic package execution is skipped in this container because the package entry point is unavailable; tests that require it skip cleanly. LIVE readiness remains out of scope.

### Migration concerns
No destructive migration. The new Alembic revision only creates missing baseline tables/indexes and leaves existing data intact.

### Push recommendation
Safe to push after the successful full test run. LIVE remains NOT_READY.

## 2026-06-23 PR-01 Lifecycle Contract + SQL Truth Audit

### Why the patch was needed
AlphaForge needed one explicit lifecycle vocabulary for SQL-first signal, decision, order, reject, entry, exit, and export evidence. Existing code already had partial lifecycle constants, but legacy/internal states such as `CREATED`, `SIGNAL_ACCEPTED`, `SYMBOL_REJECTED`, and terminal close reasons were not documented as compatibility mappings in one canonical contract.

### Root cause
Lifecycle semantics were distributed across persistence, runtime, order execution, and backtest export code. That made it possible for exports or audit paths to treat legacy/internal labels as final truth instead of deriving canonical lifecycle evidence from SQL.

### Files/functions inspected
- `src/alphaforge/persistence.py`: `init_db`, `save_signal`, `save_order_decision`, `save_rejected_decision_artifact`, `save_trade_lifecycle_event`.
- `src/alphaforge/runtime.py`: `RuntimeOrchestrator._process_symbol`, `_emit_lifecycle_event`, `_persist_reject`, `_persist_lifecycle`.
- `src/alphaforge/order.py`: `LifecycleState`, `_audit`, pre-submit decision/reject execution flow.
- `backtest_order.py`: `LifecycleRow`, `simulate_candidate`, `scan`, `_persist_lifecycle_rows`, `verify_export_integrity`, CSV export writing.
- `src/alphaforge/contracts.py`: shared reject/lifecycle compatibility constants and `validate_transition`.

### Files changed
- `src/alphaforge/lifecycle_contract.py`
- `src/alphaforge/contracts.py`
- `src/alphaforge/persistence.py`
- `src/alphaforge/__init__.py`
- `backtest_order.py`
- `tests/test_lifecycle_contract.py`
- `docs/decision_lifecycle_contract.md`
- `VERSION.md`
- `CHANGELOG.md`
- `REPORT.md`

### Contract added
Added canonical lifecycle states: `SIGNAL_CREATED`, `SIGNAL_REJECTED`, `WAITING_ENTRY_ZONE`, `ENTRY_TRIGGERED`, `ORDER_PLACED`, `ORDER_REJECTED`, `POSITION_OPENED`, `POSITION_CLOSED`, `ENTRY_TIMEOUT`, and `CANCELLED`. Added explicit compatibility mappings for legacy/internal labels including `CREATED -> SIGNAL_CREATED`, `SIGNAL_ACCEPTED -> WAITING_ENTRY_ZONE`, `SYMBOL_REJECTED -> SIGNAL_REJECTED`, and terminal close reasons into `POSITION_CLOSED`.

### Runtime behavior changes
No LIVE enablement, risk-threshold loosening, or trade-frequency increase. Runtime compatibility lifecycle constants remain available for existing PAPER/LIVE readiness and reconciliation code.

### Lifecycle changes
New lifecycle persistence rejects unknown states and normalizes known legacy/internal states before storing new SQL truth. Invalid canonical transitions are now directly testable with `is_valid_lifecycle_transition`.

### Persistence changes
`save_trade_lifecycle_event` now normalizes lifecycle state through the canonical contract before writing SQL and returns `None` for unknown lifecycle states. Existing schema is unchanged.

### Export/schema changes
`backtest_order._persist_lifecycle_rows` normalizes lifecycle states before SQL persistence and CSV export reads the persisted SQL rows, preventing new exports from emitting legacy `CREATED` as canonical first state. No schema migration was added.

### Tests added
Added `tests/test_lifecycle_contract.py` covering canonical states, unknown-state rejection, `CREATED` compatibility mapping without treating it as canonical, invalid transition checks, and docs/code state parity.

### Tests executed
- `python -m pytest tests/test_lifecycle_contract.py -q` — 5 passed.
- `pytest -q` — 354 passed, 7 skipped, 165 warnings.

### Risks
Low-to-medium. Unknown lifecycle states are now rejected instead of being silently persisted, which improves audit truth but can expose any remaining caller that invents non-contract states. Compatibility mappings reduce migration risk for known legacy/internal labels.

### Remaining limitations / known gaps
- This PR does not fix possible fixed score (`0.8`) or RR (`2.0`) placeholders except by documenting them as follow-up audit risks.
- This PR does not fully harden reject_reason/cancel_reason completeness.
- This PR does not prove every dashboard/export query is SQL-derived, only the current backtest lifecycle persistence/export path.
- This PR does not make lifecycle-accurate backtest complete.
- LIVE remains NOT_READY.

### Migration concerns
No schema migration is required. Legacy rows already stored as `CREATED` are not rewritten; new persistence/export rows normalize known legacy/internal states.

### Push recommendation
Safe to push after full tests pass. Continue with later PRs for score/RR variability, reject/cancel persistence completeness, SQL-derived dashboard audit, and lifecycle-accurate backtest terminal semantics.

## 2026-06-23 Work 1.1 SQLite schema bootstrap stabilization

### Why the patch was needed
`init_db()` is the SQL-first persistence bootstrap path and must initialize a fresh SQLite database without attempting to create indexes for absent tables. Recent failures centered on `ix_timesfm_evidence_symbol_timeframe_ts`, which depends on `timesfm_forecast_evidence`.

### Root cause
SQLite validates the target table during `CREATE INDEX IF NOT EXISTS`. If the TimesFM evidence table is absent or incomplete in the bootstrap sequence, index creation can fail even though the index DDL itself is idempotent.

### Files changed
- `src/alphaforge/persistence.py`
- `alembic/versions/0003_sqlite_bootstrap_runtime_tables.py`
- `tests/test_sqlite_schema_bootstrap.py`
- `VERSION.md`
- `CHANGELOG.md`
- `REPORT.md`

### Runtime behavior changes
Fresh SQLite bootstrap now includes conservative TimesFM evidence compatibility columns (`forecast_timestamp`, `point_forecast`, `quantiles_json`) while preserving the existing canonical forecast fields used by runtime TimesFM code. Existing SQLite databases receive these columns additively through the runtime schema repair path.

### Lifecycle changes
None. Lifecycle ordering, lifecycle state vocabulary, reject persistence, and order-decision behavior were not changed.

### Persistence changes
The TimesFM evidence table remains created before `ix_timesfm_evidence_symbol_timeframe_ts`. The change is additive only: no tables are dropped, truncated, recreated, or deleted, and no broad schema-error masking was added.

### Export/schema changes
No CSV export behavior changed. The TimesFM SQL schema is widened additively for compatibility with conservative evidence fields.

### Tests added
Added focused SQLite bootstrap tests proving fresh TimesFM table/index creation, repeated `init_db()` idempotency, and conservative TimesFM evidence columns.

### Tests executed
- `python -m pytest -q tests/test_sqlite_schema_bootstrap.py` — 12 passed.
- `python -m pytest -q tests/test_alembic_revision_graph.py` — 1 passed, 2 skipped.
- `python -m pytest -q` — 349 passed, 7 skipped.

### Risks
Low. The patch only adds nullable columns and tests bootstrap ordering; it does not alter trading decisions, execution modeling, lifecycle semantics, or exports.

### Remaining limitations
This does not validate all production database dialects beyond the existing Alembic coverage, and it does not make LIVE trading ready.

### Migration concerns
Existing SQLite databases are repaired additively via `ALTER TABLE ... ADD COLUMN` when columns are missing. No destructive migration is required.

### Push recommendation
Safe to merge after the requested pytest suite passes.

## 2026-06-23 Patch Addendum — SQLite/Alembic config snapshot trigger repair

### Why this patch was needed
SQLite and Alembic bootstrap paths must be ordered so tables exist before dependent indexes, triggers, or other operations reference them. TimesFM evidence ordering was already guarded in `init_db()`, and the Alembic runtime repair path also needed to preserve the append-only contract when it defensively creates a missing `config_snapshots` table.

### Root cause
A partially-applied legacy database could reach the runtime bootstrap revision without `config_snapshots`. The revision created the table before later operations, but did not also recreate the SQLite no-update/no-delete triggers in that repair path.

### Files changed
- `alembic/versions/0003_sqlite_bootstrap_runtime_tables.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
None. This is an Alembic schema bootstrap repair only.

### Lifecycle changes
None.

### Persistence changes
The Alembic runtime bootstrap revision now idempotently creates SQLite `config_snapshots` append-only triggers after ensuring the table exists. Existing rows are preserved; no tables are dropped or recreated.

### Export/schema changes
No export changes. Schema metadata repair is additive/idempotent for SQLite trigger presence.

### Tests added
No new tests were required; the existing SQLite bootstrap and Alembic revision graph tests cover the intended table/index/trigger contracts when optional Alembic dependencies are present.

### Tests executed
- `python -m pytest tests/test_sqlite_schema_bootstrap.py -q`
- `python -m pytest tests/test_alembic_revision_graph.py -q`
- `python -m pytest -q`

### Risks
Low. The patch only adds `CREATE TRIGGER IF NOT EXISTS` statements after the target table exists on SQLite.

### Remaining limitations
This does not validate forecast quality, execution realism, or LIVE readiness.

### Migration concerns
None for fresh databases. Partial legacy SQLite databases that have `config_snapshots` but are missing append-only triggers are repaired idempotently during Alembic upgrade.

### Push recommendation
Safe to push after targeted and full tests pass. LIVE remains blocked by readiness gates.

## 2026-06-23 Patch Addendum — Persistence/lifecycle contract regression coverage

### Why this patch was needed
The reported macOS failures targeted contracts that must remain stable: `fetch_expectancy_stat(...)` must return `float | None`, SQLite bootstrap must repair compatibility columns additively, and accepted backtest lifecycles must not jump from acceptance directly to entry trigger.

### Root cause
The implementation already preserves these contracts in this checkout, but the exact failure surfaces needed explicit regression coverage so future persistence metadata helpers or lifecycle edits cannot silently weaken audit quality.

### Files changed
- `src/alphaforge/persistence.py`
- `tests/test_persistence_lifecycle_contracts.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
SQLite legacy runtime schema repair now ensures base lifecycle compatibility columns needed by the lifecycle uniqueness index are present before index creation. The scalar expectancy lookup now uses SQLAlchemy executable SQL text while preserving the existing `float | None` return contract.

### Lifecycle changes
No runtime lifecycle behavior changed. Tests now assert `WAITING_ENTRY_ZONE` appears before `ENTRY_TRIGGERED` for an accepted limit backtest candidate.

### Persistence changes
No destructive schema change. Legacy `trade_lifecycle_events` tables are additively repaired with base audit columns before index creation. Tests now verify repeated `init_db()` calls preserve legacy rows while adding `order_decisions.payload` and `trade_lifecycle_events.trade_id/state/payload`.

### Export/schema changes
None.

### Tests added
- Legacy scalar `fetch_expectancy_stat(...)` contract test.
- Separate `fetch_expectancy_stat_detail(...)` metadata test.
- Idempotent legacy runtime-column repair and row-preservation test.
- Accepted backtest `WAITING_ENTRY_ZONE` ordering test.

### Tests executed
- `alembic heads` (environment warning: console script unavailable in local container)
- `alembic history` (environment warning: console script unavailable in local container)
- `alembic upgrade head` (environment warning: console script unavailable in local container)
- `python -m pytest tests/test_persistence_lifecycle_contracts.py tests/test_alembic_revision_graph.py tests/test_phase123_foundations.py::test_backtest_lifecycle_does_not_start_directly_at_created tests/test_sqlite_schema_bootstrap.py::test_init_db_migrates_legacy_order_decisions_schema -q`
- `python -m pytest -q`

### Risks
Low. The code change is additive/idempotent SQLite repair and executable SQL compatibility only; it does not alter trading thresholds, reject gates, exports, or lifecycle decisions.

### Remaining limitations
The local container lacks the Alembic console script and network package installation was blocked, so `alembic heads/history/upgrade head` could not be executed as shell commands here; the Alembic revision graph tests still load the script directory when the optional Alembic package is available.

### Migration concerns
None.

### Push recommendation
Safe to push after targeted and full tests pass. LIVE remains blocked by readiness gates.

## 2026-06-23 Patch Addendum — SQLite/Alembic bootstrap regression hardening

### Why this patch was needed
Local evidence showed failures could still be caused by bootstrap control flow rather than missing text: a table/index DDL string may exist in source while the executed sequence still reaches a dependent index or migration read too early.

### Root cause
The repaired code already contains the required helpers, but regression coverage needed to assert executable ordering directly: `schema_migrations` must be created before `_apply_sqlite_migrations()` reads versions, `timesfm_forecast_evidence` must precede `ix_timesfm_evidence_symbol_timeframe_ts` in the actual helper list, and SQLite Alembic head must leave `config_snapshots` present before append-only triggers are created. The new direct partial-database regression also exposed a second control-flow defect: after bootstrapping `schema_migrations`, `_apply_sqlite_migrations()` could continue into lifecycle `ALTER TABLE` and index statements even when `trade_lifecycle_events` or `closed_trade_reviews` did not exist.

### Files changed
- `src/alphaforge/persistence.py`
- `tests/test_sqlite_schema_bootstrap.py`
- `tests/test_alembic_revision_graph.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
`_apply_sqlite_migrations()` still creates `schema_migrations` before reading versions, but now guards lifecycle/review table ALTER and lifecycle index DDL behind actual table existence. This keeps partial legacy migrations additive/idempotent without hiding missing-table errors behind try/except and without dropping data.

### Lifecycle changes
None.

### Persistence changes
No schema shape changed. Regression tests now prove partial SQLite migrations create `schema_migrations` before selecting from it, skip dependent ALTER/INDEX DDL for absent optional tables, and preserve the migration bookkeeping path.

### Export/schema changes
None. Alembic fresh-head coverage now also verifies `config_snapshots` append-only triggers exist.

### Tests added
- Direct TimesFM DDL helper order assertion.
- Direct partial-database `_apply_sqlite_migrations()` schema_migrations bootstrap assertion, including safe handling when lifecycle/review tables are absent.
- Fresh SQLite Alembic head assertion for `config_snapshots` no-update/no-delete triggers.

### Tests executed
- `python -m pytest -q tests/test_sqlite_schema_bootstrap.py tests/test_alembic_revision_graph.py`
- `python -m pytest -q`

### Risks
Low. The code change only prevents dependent ALTER/INDEX DDL from running when the target table is absent; no trading path, thresholds, table drops, or data rewrites were introduced.

### Remaining limitations
These tests protect bootstrap ordering but do not validate forecast quality, execution realism, or LIVE readiness.

### Migration concerns
None for this addendum.

### Push recommendation
Safe to push after targeted and full tests pass. LIVE remains blocked by readiness gates.

## 2026-06-23 Patch Addendum — SQLite/Alembic schema bootstrap repair

### Why this patch was needed
Fresh and partial legacy SQLite bootstraps must create runtime research evidence tables before any dependent indexes. A failure in this path cascades into TimesFM persistence and many downstream tests because the canonical `timesfm_forecast_evidence` table is absent.

### Root cause
SQLite validates the target table when creating an index, even when the index DDL uses `IF NOT EXISTS`. The TimesFM evidence index therefore cannot be allowed to appear in a bootstrap sequence unless the `timesfm_forecast_evidence` table has already been created. Alembic also did not have a dedicated runtime repair revision for the TimesFM evidence tables, and partial legacy databases could reach later revisions without `config_snapshots` present.

### Files changed
- `src/alphaforge/persistence.py`
- `alembic/versions/0003_sqlite_bootstrap_runtime_tables.py`
- `tests/test_sqlite_schema_bootstrap.py`
- `tests/test_alembic_revision_graph.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
`init_db()` now obtains TimesFM evidence DDL from a dedicated helper that returns the table definitions before the dependent index. This is an additive bootstrap repair only; no order decision logic, reject thresholds, lifecycle vocabulary, or LIVE behavior changed.

### Lifecycle changes
None. Existing lifecycle persistence remains additive and unchanged.

### Persistence changes
Fresh SQLite databases and partial legacy databases now idempotently create `timesfm_forecast_evidence`, `timesfm_forward_outcome_labels`, and the TimesFM lookup index without dropping data. Repeated `init_db()` calls preserve existing TimesFM evidence rows.

### Export/schema changes
Added Alembic revision `0003_sqlite_bootstrap_runtime_tables` so `alembic upgrade head` creates the runtime TimesFM evidence tables and defensively repairs missing `config_snapshots` on partial legacy databases. No existing columns were removed or relaxed.

### Tests added
- `init_db()` creates TimesFM evidence columns before the TimesFM index on fresh SQLite.
- Repeated `init_db()` calls preserve existing TimesFM evidence rows.
- Alembic head upgrade asserts `config_snapshots`, `timesfm_forecast_evidence`, `timesfm_forward_outcome_labels`, and the TimesFM index exist.

### Tests executed
- `pytest -q tests/test_sqlite_schema_bootstrap.py tests/test_alembic_revision_graph.py`
- `pytest -q`

### Risks
Low. The patch is additive and idempotent. It does not drop data, does not rewrite rows, and does not alter trading decisions.

### Remaining limitations
This repair only guarantees schema bootstrap availability. It does not validate TimesFM forecast quality, execution cost realism, or LIVE readiness.

### Migration concerns
Alembic head advances to `0003_sqlite_bootstrap_runtime_tables`. Operators should apply the new migration before relying on TimesFM persistence in Alembic-managed SQLite databases.

### Push recommendation
Safe to push after full tests pass. LIVE remains blocked by readiness gates.

## 2026-06-23 Patch Addendum — BACKTEST/PAPER pre-submit parity adapter

### Why this patch was needed
The audit showed BACKTEST uses `order.run_order_cycle(...)` in `backtest_order.py`, while PAPER runtime uses `RuntimeOrchestrator._process_symbol(...)` and `AIBrain.before_real_order(...)`. A minimal no-submit adapter was needed to prove shared pre-submit reject behavior without enabling LIVE or Binance order calls.

### Root cause
The shared candidate-quality gate already lived in `alphaforge.order.run_order_cycle(...)`, but PAPER-style execution-cost pre-submit flags were not exposed as a safe BACKTEST/PAPER parity adapter. `backtest_order.py` also has local post-cycle execution rejects, while `RuntimeOrchestrator` has runtime-only risk gates.

### Files changed
- `src/alphaforge/order.py`
- `tests/test_backtest_paper_pre_submit_parity.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
Added `evaluate_paper_style_pre_submit(...)`, a no-submit adapter that calls `run_order_cycle(...)` and then applies the shared effective-RR execution flag calculation in PAPER mode. Existing runtime flows are unchanged unless callers opt into the adapter.

### Lifecycle changes
No lifecycle vocabulary changed. Adapter audit storage records accepted candidates as `ORDER_PLACED` and rejected pre-submit candidates as `SIGNAL_REJECTED` for parity assertions.

### Persistence changes
None. The adapter is side-effect-light and does not write SQL by itself. Existing persistence helpers remain unchanged.

### Export/schema changes
None.

### Tests added
- BACKTEST/PAPER parity for LOW_SCORE.
- BACKTEST/PAPER parity for LOW_EFFECTIVE_RR.
- BACKTEST/PAPER parity for EXPECTANCY_MISSING.
- BACKTEST/PAPER parity for HIGH_SPREAD.
- Accepted candidate audit lifecycle parity.
- Rejected candidate audit lifecycle parity.

### Tests executed
- `pytest -q tests/test_backtest_paper_pre_submit_parity.py`

### Risks
Low. The adapter does not enable LIVE, does not loosen thresholds, and does not alter existing backtest or PAPER runtime entrypoints by default.

### Remaining limitations
`RuntimeOrchestrator._process_symbol(...)` still has additional PAPER runtime gates (kill switch, stale market data, cooldown, exposure, funding sanity) that are not part of the backtest scanner. Full orchestrator/backtest unification remains separate work.

### Migration concerns
None.

### Push recommendation
Safe to push as a parity-test adapter. Do not enable LIVE.


## 2026-06-23 Patch Addendum — LIVE readiness aggregator CI repair

### Why this patch was needed
CI showed the dashboard readiness probe matrix expected the existing 27 probe catalog entries, but the previous patch duplicated the 16 final gates into that legacy probe catalog and inflated API counts.

### Root cause
Final gates belong in readiness report JSON and the dashboard final-gate table, not in the legacy readiness probe catalog used by existing dashboard API tests and consumers.

### Files changed
- `src/alphaforge/dashboard/queries.py`
- `tests/test_timesfm_futures.py`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
None. Runtime LIVE refusal behavior and final readiness aggregation are unchanged.

### Lifecycle changes
None.

### Persistence changes
None.

### Export/schema changes
The readiness probe API contract remains at the legacy 27 probes; final gates remain exported through `live_readiness_reports.report_payload` and the readiness page.

### Tests added
No new assertions; repaired optional dependency handling for the TimesFM futures test module.

### Tests executed
- `pytest -q`
- `python -m compileall -q src tests`

### Risks
Low. This is an API compatibility repair for dashboard probes; the final LIVE gate contract remains persisted and visible.

### Remaining limitations
LIVE remains blocked without complete measured evidence for every final gate.

### Migration concerns
None.

### Push recommendation
Safe to push as CI repair.

## 2026-06-22 Patch Addendum — LIVE readiness final gate aggregator

### Why this patch was needed
P2-2 required a single fail-closed readiness contract that combines lifecycle, persistence, parity, execution realism, exchange, reconciliation, operational, dashboard, TimesFM, PAPER burn-in, test, and operator evidence into one explicit verdict.

### Root cause
Readiness evidence existed as individual checks, reports, dashboard probes, and burn-in diagnostics, but there was no final aggregation layer with explicit verdict levels and blockers that could prevent partial evidence from being interpreted as LIVE-ready.

### Files changed
- `src/alphaforge/live_readiness.py`
- `src/alphaforge/runtime.py`
- `src/alphaforge/dashboard/queries.py`
- `src/alphaforge/dashboard/templates/readiness.html`
- `tests/test_live_readiness.py`
- `README.md`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
Runtime persists the final readiness verdict and blocks LIVE real-order startup unless the verdict is exactly `LIVE_REAL_ORDERS_READY`. The default posture remains fail-closed.

### Lifecycle changes
No lifecycle vocabulary changed. Lifecycle integrity is now elevated into a final aggregate gate.

### Persistence changes
No schema migration. Existing `live_readiness_reports.report_payload` now includes `verdict`, `gates`, and `blockers` JSON fields for machine-readable consumption.

### Export/schema changes
Dashboard readiness JSON/probe matrix now includes final aggregate gates and blockers. The readiness page renders the final gate contract separately from underlying checks.

### Tests added
- Missing final gates block real orders.
- Lower gates can produce only `LIVE_PRECHECK_READY`, not real orders.
- Kill switch active blocks readiness.
- TimesFM evidence cannot satisfy execution/order readiness.

### Tests executed
- `pytest -q tests/test_live_readiness*.py tests/test_runtime*.py tests/test_dashboard_app.py`
- `pytest -q tests/test_live_readiness.py`
- `python -m compileall -q src tests`

### Risks
The aggregator is intentionally conservative and may block LIVE until operators wire measured local evidence for dashboard/RBAC, burn-in, full tests, authenticated reconciliation, and heartbeat evidence. This is expected.

### Remaining limitations
Runtime currently supplies only the evidence it can measure directly; missing external operator/test/dashboard artifacts remain blockers. No live order placement was added.

### Migration concerns
No database migration is required; consumers of readiness JSON should tolerate the added `verdict`, `gates`, and `blockers` fields.

### Push recommendation
Safe to push as P2-2 fail-closed readiness aggregation. Do not enable LIVE trading until every local gate has fresh measured passing evidence.

## 2026-06-22 Patch Addendum — PAPER burn-in report generator

### Why this patch was needed
P2-1 required a deterministic PAPER burn-in report so operators can inspect whether persisted PAPER runtime evidence is safe and complete before considering any later LIVE_DRY_RUN or LIVE_REAL_ORDERS discussion.

### Root cause
PAPER runtime evidence existed across decisions, lifecycle rows, heartbeat evidence, execution context, dashboard/runtime-control tables, readiness reports, and TimesFM tables, but there was no single fail-closed report contract that summarized selectivity, integrity, observability, reconciliation, and execution-realism blockers.

### Files changed
- `src/alphaforge/paper_burnin.py`
- `tests/test_paper_burnin.py`
- `README.md`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
None. This is reporting-only and does not change thresholds, order placement, scanner behavior, runtime controls, or live-readiness gates.

### Lifecycle changes
No lifecycle vocabulary changed. The report validates persisted PAPER lifecycle ordering with the existing lifecycle transition contract and surfaces invalid ordering as `LIFECYCLE_INTEGRITY_FAILURE`.

### Persistence changes
No schema migration. The CLI reads existing SQLite tables and writes external report artifacts: `paper_burnin_summary.csv`, `paper_burnin_report.md`, and `paper_burnin_blockers.json`.

### Export/schema changes
Added a deterministic burn-in report artifact contract. Missing tables or incomplete evidence are represented as blockers instead of fabricated metrics.

### Tests added
- Empty DB classifies as `INSUFFICIENT_SAMPLE`.
- Missing reject reasons classify as `DATA_INTEGRITY_FAILURE`.
- Bad lifecycle ordering classifies as `LIFECYCLE_INTEGRITY_FAILURE`.
- Missing execution context and fake-zero execution fields classify as `EXECUTION_CONTEXT_FAILURE`.
- Healthy synthetic PAPER evidence can classify as `HEALTHY_SELECTIVITY` while still remaining `NOT_LIVE_READY`.
- TimesFM absence is noted as optional/non-fatal.

### Tests executed
- `pytest -q tests/test_paper_burnin.py`

### Usage
```bash
python -m alphaforge.paper_burnin --db path/to/paper_runtime.db --out reports/paper_burnin
```

### Risks
The report is intentionally conservative: incomplete heartbeat, reconciliation, readiness, or execution evidence remains blocking even when selectivity looks healthy. Fake-zero detection is field-level and should be reviewed if an exchange supplies explicit zero-cost proof in the future.

### Remaining limitations
The report does not prove LIVE readiness, does not configure TimesFM requirements, and does not create reconciliation evidence. It summarizes persisted evidence only.

### Migration concerns
None; no database schema changes.

### Push recommendation
Safe to push as P2-1 PAPER diagnostics. Do not enable LIVE trading from this report alone.

## 2026-06-22 Patch Addendum — Execution realism evidence contract

### Why this patch was needed
P1-2 required spread, slippage, latency, liquidity, funding, orderbook, volatility, and effective-RR evidence to be measurable, explicit, and fail-closed across BACKTEST, PAPER, and LIVE_PRECHECK.

### Root cause
Execution context normalization still allowed some unavailable fields to become neutral numeric defaults, and effective-RR persistence did not expose a complete penalty breakdown with a readiness-grade evidence classifier.

### Files changed
- `src/alphaforge/execution.py`
- `src/alphaforge/effective_rr.py`
- `src/alphaforge/order.py`
- `src/alphaforge/live_readiness.py`
- `tests/test_execution_layer.py`
- `tests/test_live_readiness.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- Added execution evidence statuses: `COMPLETE_MEASURED`, `PARTIAL_ESTIMATED`, `UNAVAILABLE_BLOCKING`, and `INVALID_FAKE_ZERO`.
- PAPER/LIVE-style prechecks require measured evidence and flag missing or fake-zero fields.
- BACKTEST can use estimated execution fields only when explicitly labeled as estimates such as `ESTIMATED_BACKTEST`.
- Effective RR now persists a full cost breakdown instead of only a final adjusted value.

### Lifecycle changes
No lifecycle vocabulary changed. Decision evidence attached to lifecycle/order artifacts now carries execution-evidence status and penalty breakdown for auditability.

### Persistence changes
No schema migration. Existing JSON payload fields now include `effective_rr_breakdown` and expanded `execution_metrics` with raw RR and per-cost penalties.

### Export/schema changes
No CSV/schema shape was changed in this patch. JSON evidence is additive.

### Tests added
- Missing spread/slippage/funding remain null and block instead of becoming zero.
- Fake measured zero context is classified `INVALID_FAKE_ZERO`.
- Costs reduce effective RR and can trigger `LOW_EFFECTIVE_RR`.
- BACKTEST estimates classify as `PARTIAL_ESTIMATED`.
- LIVE_PRECHECK invalid execution evidence blocks readiness.
- Order decision persistence includes the effective-RR penalty breakdown.

### Tests executed
- `pytest -q tests/test_execution_layer.py tests/test_live_readiness.py tests/test_runtime_heartbeat.py tests/test_exchange_connectivity.py tests/test_backtest*`

### Risks
The fake-zero detector is intentionally conservative for measured zero cost fields; legitimate zero measurements must include explicit zero-verification evidence before they should be considered complete.

### Remaining limitations
Upstream exchange/scanner modules still determine whether evidence is measured or estimated. LIVE remains blocked until measured execution evidence, reconciliation, heartbeat, rollback, observability, canary/shadow, and operator gates all pass.

### Migration concerns
None; persistence changes are additive JSON payload content only.

### Push recommendation
Safe to push as P1-2 execution-realism hardening. Do not enable LIVE trading.

## 2026-06-22 Patch Addendum — LIVE_PRECHECK no-submit parity evidence

### Why this patch was needed
P1-1 required a safe LIVE-like precheck path that can prove PAPER/LIVE decision parity without placing, modifying, or canceling exchange orders.

### Root cause
Existing mode-parity evidence was in-memory qualification data and did not persist a full no-submit evidence contract for runtime LIVE_PRECHECK decisions, including input snapshot hash, no-submit verification, parity result, and execution context completeness.

### Files changed
- `src/alphaforge/runtime.py`
- `src/alphaforge/live_readiness.py`
- `src/alphaforge/persistence.py`
- `tests/test_runtime.py`
- `tests/test_live_readiness.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- Added `LIVE_PRECHECK` execution mode.
- LIVE_PRECHECK requires verified exchange public market-data scanner provenance when started, but does not require a real execution adapter.
- LIVE_PRECHECK runs the same pre-submit scoring/order-plan helpers used for PAPER parity comparison on normalized input.
- Accepted LIVE_PRECHECK candidates persist parity evidence and return before real execution.
- Direct `_execute` calls in LIVE_PRECHECK produce a local `no_submit_verified` result and never invoke adapter submit.

### Lifecycle changes
LIVE_PRECHECK follows PAPER-style pre-entry lifecycle evidence through `SIGNAL_CREATED`, `WAITING_ENTRY_ZONE`, `ENTRY_TRIGGERED`, and local `ORDER_PLACED` evidence, then stops before exchange mutation. This is evidence-only and not a real exchange order.

### Persistence changes
- Added additive `order_decisions` columns: `input_snapshot_hash`, `no_submit_verified`, and `parity_result`.
- LIVE_PRECHECK evidence persists mode, symbol, decision, reject reason, score, raw RR, effective RR, execution context, snapshot hash, no-submit flag, and PAPER-vs-LIVE_PRECHECK comparison payload.

### Export/schema changes
SQLite schema is additively extended only. Existing order decision writes remain backward-compatible through nullable new columns. No CSV export format changed in this patch.

### Tests added
- LIVE_PRECHECK PAPER parity/no-submit persistence regression.
- Direct LIVE_PRECHECK execution no-submit regression.
- Readiness block on LIVE_PRECHECK parity mismatch.
- Readiness block on missing LIVE_PRECHECK execution context.
- Successful LIVE_PRECHECK parity alone does not unlock LIVE real orders.

### Tests executed
- `pytest -q tests/test_runtime*.py tests/test_live_readiness*.py tests/test_order*.py` (blocked because this checkout has no `tests/test_order*.py` path)
- `pytest -q tests/test_runtime*.py tests/test_live_readiness*.py`
- `pytest -q` (blocked by missing optional `numpy` for TimesFM futures tests)
- `python -m compileall -q src tests`

### Risks
LIVE_PRECHECK evidence depends on the supplied market scanner/execution context fidelity. Missing context fails readiness; partial context remains visible in persisted JSON rather than being fabricated.

### Remaining limitations
LIVE_DRY_RUN still needs complete reconciliation evidence, observability evidence, rollback evidence, fresh LIVE heartbeat, canary/shadow gates, operator acknowledgement, and adapter-specific non-mutating endpoint proof.

### Migration concerns
The schema change is additive and nullable. Existing SQLite databases require `init_db()`/schema bootstrap to add the new columns before querying them.

### Push recommendation
Safe to push as P1-1 no-submit parity hardening. Do not enable LIVE_REAL_ORDERS from this evidence alone.

## 2026-06-22 Patch Addendum — Dashboard test import CI repair

### Why this patch was needed
CI full-suite execution exposed that the new dashboard audit tests referenced `create_engine` without importing it in environments where dashboard dependencies are installed and the tests execute instead of skipping.

### Root cause
Local optional dependency availability caused `tests/test_dashboard_app.py` to skip during the earlier targeted run, so the missing SQLAlchemy import was not exercised locally.

### Files changed
- `tests/test_dashboard_app.py`
- `REPORT.md`
- `CHANGELOG.md`
- `VERSION.md`

### Runtime behavior changes
None. This is a test/import repair only.

### Lifecycle changes
None.

### Persistence changes
None.

### Export/schema changes
None.

### Tests added
No new assertions; existing dashboard audit tests can now execute in CI.

### Tests executed
- `pytest -q tests/test_dashboard_app.py::test_dashboard_kill_switch_survives_restart_and_audits tests/test_dashboard_app.py::test_dashboard_paper_switch_accepted_and_live_blocked_without_readiness` (skipped locally because optional dashboard test dependencies are unavailable)
- `pytest -q` (blocked locally during collection by missing optional `numpy` for TimesFM futures tests)
- `python -m compileall -q src tests`

### Risks
Low; import-only test repair.

### Remaining limitations
LIVE remains NOT READY; this repair does not change runtime controls or readiness posture.

### Push recommendation
Safe to push as CI repair for the P0-4 dashboard-control test coverage.

## 2026-06-21 Patch Addendum — Dashboard kill switch/PAPER-LIVE fail-closed audit

### Why this patch was needed
Dashboard runtime controls existed, but the P0-4 audit required explicit proof that operator actions are persisted, auditable, fail-closed for LIVE, restart-visible, and do not expose credentials or create a real order path.

### Root cause
The prior persisted control state covered requested mode and kill switch, but switch attempts were not written to a dedicated audit log and the dashboard could accept LIVE as the requested mode before proving PASS readiness evidence plus explicit operator acknowledgement.

### Files changed
- `src/alphaforge/runtime_control.py`
- `src/alphaforge/dashboard/app.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_dashboard_app.py`
- `tests/test_runtime.py`
- `tests/test_runtime_control.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- Persisted kill switch remains a runtime gate and now has audit records for ON/OFF transitions.
- Persisted kill switch blocks scanner invocation before new work is selected.
- PAPER mode remains safely selectable while stopped.
- LIVE requested mode is refused unless latest persisted readiness evidence is PASS and the operator acknowledgement field is present.

### Lifecycle changes
No lifecycle vocabulary or transition sequence changed. Runtime kill-switch blocks for in-flight signals continue to use explicit `KILL_SWITCH_ACTIVE` reject semantics.

### Persistence changes
- Added idempotent `runtime_control_audit_events` table for operator-control audit evidence.
- The existing single-row `runtime_control_state` table remains backward compatible.

### Export/schema changes
No CSV export format changed. SQLite schema gains one additive audit table only.

### Tests added
- Dashboard render verifies kill-switch visibility, NOT LIVE-READY display, and secret non-disclosure.
- Dashboard kill-switch POST persists across app recreation and writes an audit event.
- Dashboard PAPER switch succeeds; LIVE switch with incomplete evidence is blocked with an explicit message and audit event.
- Runtime scan refuses scanner work when persisted kill switch is ON.

### Tests executed
- `pytest -q tests/test_dashboard_app.py` (skipped in this environment because optional dashboard test dependencies are unavailable)
- `pytest -q tests/test_runtime*.py tests/test_live_readiness*.py`
- `pytest -q` (blocked during collection by missing optional `numpy` for TimesFM futures tests)
- `python -m compileall -q src tests`

### Risks
- The audit table is created idempotently outside Alembic in the same style as current runtime-control bootstrap; formal migration alignment may be needed if this repository later requires Alembic-only schema management for operator-control tables.

### Remaining limitations
- LIVE remains blocked by readiness, connectivity, adapter, reconciliation, observability, and operational evidence requirements.
- Dashboard supervisor remains minimal and is not a production process manager.

### Migration concerns
Existing SQLite databases receive the new audit table on runtime-control store initialization. Existing control state rows are preserved.

### Push recommendation
Safe to push as a narrow fail-closed operator-control hardening patch. Do not interpret this as LIVE readiness approval.

## 2026-06-21 Patch Addendum — Rejected decision SQL/CSV integrity

### Why this patch was needed
Rejected signals/orders must be auditable alpha artifacts, not side effects. Some BACKTEST rejected CSV rows lacked stable `signal_id`/lifecycle metadata, PAPER reject persistence wrote order decisions separately from lifecycle evidence, and raw RR could be reused where execution penalties should reduce effective RR.

### Root cause
Reject persistence was split across callers. BACKTEST constructed CSV rows and lifecycle rows independently, while runtime PAPER persistence used separate reject and lifecycle callbacks. This made it easy for reject paths to miss shared fields or persist incomplete SQL evidence.

### Files changed
- `src/alphaforge/persistence.py`
- `src/alphaforge/runtime.py`
- `backtest_order.py`
- `tests/test_phase123_foundations.py`
- `tests/test_backtest_order_scanner.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
PAPER rejected decisions now persist through `save_rejected_decision_artifact(...)`, which writes the signal, rejected order decision, and rejected lifecycle artifact with one stable signal ID and non-empty canonical reason. Runtime risk/AI rejects now use execution-cost-adjusted effective RR.

### Lifecycle changes
Rejected runtime lifecycle persistence now receives reject reason, score, RR, effective RR, expectancy bucket when present, execution context, and missing-context status. BACKTEST rejected lifecycle and CSV rows share the same stable signal ID.

### Persistence changes
No duplicate schema was introduced. Existing `signals`, `order_decisions`, and `trade_lifecycle_events` tables are reused. Empty/UNKNOWN rejected artifact reasons fail closed instead of writing unauditable rows.

### Export/schema changes
No schema migration. `rejected_orders.csv` rows now include stable `signal_id`, `lifecycle_state`, `execution_ctx_missing`, `expectancy_bucket`, `raw_rr`, and cost-adjusted `effective_rr` parity with rejected lifecycle SQL rows.

### Tests added
- Canonical rejected artifact SQL persistence across signal/order/lifecycle rows.
- Unknown/empty reject reason refusal.
- Major reject reason class coverage.
- BACKTEST rejected SQL/CSV signal ID, reject reason, effective RR, and count parity.

### Tests executed
- `pytest -q tests/test_phase123_foundations.py tests/test_backtest_order_scanner.py tests/test_runtime.py` — passed (130 passed, 54 warnings).
- `shopt -s nullglob; files=(tests/test_order*.py tests/test_runtime*.py tests/test_persistence*.py tests/test_*lifecycle*.py); pytest -q "${files[@]}"` — passed (49 passed, 54 warnings).
- `python -m compileall -q src tests backtest_order.py` — passed.
- `pytest -q` — blocked by missing optional dependency `numpy` during `tests/test_timesfm_futures.py` collection.

### Risks
The helper is intentionally additive and strict; any caller trying to write a rejected artifact without a real reason now receives `None` and should treat that as a persistence failure.

### Remaining limitations
Symbol-selection rejects are summarized by runtime metrics but are not individually persisted as order decisions unless they become processed signal candidates. Exchange adapter reject payload richness remains adapter-dependent.

### Migration concerns
None. Existing SQLite tables and CSV exports are extended by populated fields, not schema replacement.

### Push recommendation
Safe to push after the required targeted and full test commands complete in CI. LIVE remains NOT READY.

## 2026-06-21 Patch Addendum — Backtest lifecycle truth audit hardening

### Why this patch was needed
Earlier BACKTEST lifecycle artifacts showed red flags: constant score/RR, `CREATED`-style shortcut rows, empty reject reasons, missing rejected rows, and execution context represented as zero. The current pipeline already used improved lifecycle rows in normal paths, but export verification did not fully prove that persisted lifecycle truth matched CSV artifacts.

### Root cause
`verify_export_integrity(...)` only checked lifecycle/CSV row counts, rejected-record/CSV row counts, rejected lifecycle reasons, and empty expectancy buckets. It did not fail closed on legacy `CREATED`, CREATED-only signal exports, SQL rejected lifecycle count drift versus `rejected_orders.csv`, missing lifecycle state/status, fake zero execution fields when context was missing, or suspiciously constant score/RR distributions.

### Files changed
- `backtest_order.py`
- `tests/test_backtest_order_scanner.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
BACKTEST generation remains on the existing scanner/order-cycle path. The patch only hardens post-generation export integrity verification and fails closed when lifecycle artifacts are not audit-truthful. PAPER and LIVE paths are unchanged.

### Lifecycle changes
No new lifecycle states were introduced. Export integrity now rejects legacy `CREATED`, empty lifecycle state/status, and signal IDs that export only `SIGNAL_CREATED` without a terminal or progression state. Rejected lifecycle states must carry `reject_reason`.

### Persistence / export / schema changes
No schema changes. `order_lifecycle.csv` continues to be written from persisted in-memory SQLite lifecycle rows. Verification now compares rejected CSV rows to rejected lifecycle SQL rows and rejects fake zero execution context when `execution_ctx_missing` is true.

### Tests added
- Rejected SQL lifecycle count versus `rejected_orders.csv` mismatch detection.
- Missing lifecycle state/status and legacy `CREATED` detection.
- CREATED-only lifecycle export detection.
- Fake zero missing execution context detection.
- Suspicious constant score/RR distribution detection.

### Tests executed
- `python -m compileall -q src tests backtest_order.py` — passed.
- `pytest -q tests/test_backtest_order_scanner.py` — passed.
- `pytest -q tests/test_backtest_order_scanner.py tests/test_phase123_foundations.py tests/test_schema.py tests/test_sqlite_schema_bootstrap.py tests/test_execution_layer.py tests/test_runtime.py` — passed.
- `python backtest_order.py --offline --start 2026-01-01T00:00:00Z --end 2026-01-01T01:00:00Z --output-dir <tmp>` — passed.
- `pytest -q` — blocked by missing optional dependency `numpy` during `tests/test_timesfm_futures.py` collection.

### Risks
The suspicious constant score/RR check is intentionally conservative and only triggers at three or more signal-created candidates. Very small deterministic fixtures may not prove variability.

### Remaining limitations
BACKTEST context is still bounded by historical metadata availability and conservative estimates. This patch does not prove full real execution fidelity, protective order behavior, or LIVE readiness.

### Migration concerns
None. No database schema or CSV column migration was introduced.

### Push recommendation
Safe to push after review. LIVE remains NOT READY.

## 2026-06-21 Dashboard runtime control safety hardening

### Why this patch was needed
- Dashboard Kill Switch and PAPER/LIVE controls needed to be real runtime controls rather than cosmetic UI state.
- Operators needed explicit visibility into requested mode, actual running mode, runtime status, kill-switch metadata, and last error.

### Root cause
- The dashboard previously consumed runtime heartbeat/readiness evidence but did not own a persisted runtime-control contract.
- Runtime kill-switch behavior came only from environment config and was not re-read from dashboard state before signal execution/order placement.

### Files changed
- `src/alphaforge/runtime_control.py`
- `src/alphaforge/runtime.py`
- `src/alphaforge/dashboard/app.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `src/alphaforge/dashboard/templates/partials/status_bar.html`
- `tests/test_runtime_control.py`
- `tests/test_runtime.py`
- `tests/test_dashboard_app.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- Added a persisted `runtime_control_state` table with requested mode, running mode, kill-switch state/source/time, status, last error, and update timestamp.
- Runtime checks kill switch before startup, scans, in-flight signal acceptance, and execution.
- Kill-switch blocks persist explicit final rejects with reason `KILL_SWITCH_ACTIVE` where a signal exists.
- Dashboard start uses requested mode only and fails closed if constructed runtime mode differs.
- Repeated dashboard starts reuse the existing running task instead of creating duplicate loops.

### Lifecycle changes
- In-flight kill-switch blocks emit `SIGNAL_REJECTED` after `SIGNAL_CREATED` with reason `KILL_SWITCH_ACTIVE`.
- No lifecycle states were removed or collapsed.

### Persistence changes
- Added additive SQLite table `runtime_control_state`; existing runtime/order/lifecycle tables are unchanged.
- Existing `order_decisions` reject persistence is reused for `KILL_SWITCH_ACTIVE` when practical.

### Export/schema changes
- No CSV export schema changes.
- SQLite schema addition is additive and idempotent.

### Tests added
- Runtime-control store persistence and kill-switch read tests.
- Runtime supervisor PAPER start, duplicate start prevention, stop transition, and LIVE guard fail-closed tests.
- Dashboard control API tests for kill switch and requested mode.

### Tests executed
- `pytest -q tests/test_runtime_control.py tests/test_runtime.py::test_live_start_blocks_placeholder_bootstrap_scanner tests/test_runtime.py::test_paper_accept_path_uses_canonical_lifecycle_sequence tests/test_dashboard_app.py::test_dashboard_runtime_control_api_and_kill_switch tests/test_dashboard_app.py::test_dashboard_requested_mode_updates_only_when_stopped` passed.

### Risks / remaining limitations
- Dashboard runtime supervision is intentionally minimal and in-process; production deployments may still prefer a dedicated process supervisor using the same persisted control state.
- LIVE remains blocked unless all existing independent readiness, scanner, exchange connectivity, qualification, reconciliation, and adapter guards pass.

### Migration concerns
- The `runtime_control_state` table is additive and created lazily/idempotently.

### Push recommendation
- Merge after full CI confirms dependency-complete test suite health. Do not treat this as LIVE readiness approval.

## 2026-06-19 Dashboard BACKTEST Binance historical refresh hotfix

### Why this patch was needed
- Dashboard-triggered 30-day BTCUSDT/ETHUSDT 15m backtests could fail immediately when an existing candle cache started after the requested start timestamp.
- The failure surfaced as a raw `HistoricalDataError` instead of a clean dashboard FAILED result.

### Root cause
- Historical candle cache coverage was treated as a hard precondition in the backtest wrapper path rather than an optimization.
- Dashboard backtest commands did not force a fresh Binance candle download for the operator-selected range.

### Files changed
- `src/alphaforge/historical_market_data.py`
- `backtest_order.py`
- `src/alphaforge/dashboard/backtest_control.py`
- `tests/test_dashboard_app.py`
- `tests/test_historical_market_data.py`
- `REPORT.md`
- `VERSION.md`
- `CHANGELOG.md`

### Runtime behavior changes
- `load_or_fetch_candles(...)` now accepts `force_refresh=False`.
- When `force_refresh=True`, Binance klines are fetched for the full requested range regardless of existing cache.
- When `force_refresh=False`, stale/incomplete cache coverage triggers a full-range Binance fetch attempt before any historical coverage error can be raised.
- Dashboard backtests now always invoke `backtest_order.py` with `--force-refresh`.

### Lifecycle changes
- No lifecycle transition semantics changed. The patch only affects pre-simulation historical data hydration and dashboard failure reporting.

### Persistence / cache / export changes
- Successful fresh fetches preserve the existing candle cache write format and metadata contract.
- No SQLite schema or CSV export schema changes were introduced.

### Tests added / executed
- Added dashboard command regression coverage for `--force-refresh`.
- Added stale-cache regression coverage proving a fetch is attempted before `HistoricalDataError`.
- Added clean dashboard FAILED-result coverage for insufficient Binance historical data.

### Risks / remaining limitations
- Binance availability/rate-limit behavior remains an external dependency for non-offline dashboard backtests.
- If Binance genuinely returns insufficient range coverage, the simulation correctly fails closed with an operator-facing message rather than fabricating missing candles.

### Push recommendation
- Merge recommended. This is a narrow fail-closed data-refresh hotfix with no LIVE readiness claim.

## 2026-06-19 Patch Addendum — Dashboard BACKTEST control panel

### Why the patch was needed
- Operators had dashboard observability for runtime status, rejects, lifecycle, and readiness, but no guarded UI path to run a bounded BACKTEST from the web dashboard.
- A dashboard control was needed without weakening the existing runtime safety posture or duplicating trading/strategy logic in UI code.

### Root cause
- The dashboard app factory is `create_app(...)` in `src/alphaforge/dashboard/app.py` and previously exposed read-oriented overview/reject/lifecycle/readiness pages only.
- The existing backtest entrypoint is the repository-level `backtest_order.py` script, whose CLI already accepts `--mode`, `--last-n-days`, `--symbols`, `--top-n`, `--interval`, `--balance`, and `--output-dir`.
- There was no dashboard adapter to validate operator inputs, force BACKTEST mode, run that existing pipeline, and summarize generated artifacts.

### Files changed
- `src/alphaforge/dashboard/app.py`
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `src/alphaforge/dashboard/static/dashboard.css`
- `tests/test_dashboard_app.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- Added `POST /backtest/run` to validate dashboard form inputs and invoke the existing backtest pipeline synchronously.
- The command constructed by the dashboard always includes `--mode BACKTEST`; the user form has no mode field and cannot select PAPER or LIVE.
- The dashboard wrapper summarizes `order_backtest_summary.csv`, `order_lifecycle.csv`, and `rejected_orders.csv` when the existing pipeline produces them.

### Lifecycle changes
- No lifecycle transition semantics were changed.
- Dashboard displays lifecycle/reject counts only if generated artifacts expose them; missing values are rendered as unavailable with an explicit lifecycle/reject warning.

### Persistence changes
- No runtime persistence schema changes.
- Backtest artifacts are written under the configured backtest output directory in a dashboard timestamp subdirectory.

### Export/schema changes
- No CSV schema change was made to the backtest writer.
- Dashboard reads existing artifact fields opportunistically and does not invent absent metrics such as max drawdown.

### Tests added
- Dashboard form rendering coverage.
- Server-side validation coverage for invalid `last_days` and empty symbols.
- Runner invocation coverage proving the dashboard passes a mode-less validated request and the runner command forces BACKTEST.
- Safe failure rendering coverage.
- Unavailable lifecycle/execution metric warning coverage.

### Tests executed
- `pytest -q` failed during collection before changes because NumPy is not installed for `tests/test_timesfm_futures.py`.
- `pytest -q tests/test_dashboard_app.py` skipped in this container because FastAPI/httpx/Jinja2 are not installed.
- `python -m pip install fastapi httpx python-multipart jinja2` failed because package-index access returned 403 Forbidden.
- `PYTHONPATH=src python -m py_compile src/alphaforge/dashboard/backtest_control.py src/alphaforge/dashboard/app.py` passed.
- `PYTHONPATH=src python - <<'PY' ...` direct validation/forced-BACKTEST smoke passed using a mocked subprocess result.

### Risks
- Synchronous runs may exceed the 600-second subprocess timeout for large symbol/window combinations.
- The backtest runner may use network-backed historical data unless local cache is available; unavailable history fails closed through the result error message.
- Max drawdown remains unavailable because the current summary artifact does not expose it.

### Remaining limitations
- This is not a LIVE readiness improvement and does not add order placement.
- Lifecycle/reject and execution-context accuracy remain bounded by the current backtest pipeline artifacts.
- Unknown spread/slippage/funding is displayed as unavailable/incomplete rather than converted to zero.

### Migration concerns
- None; no database migration or schema change.

### Push recommendation
- Safe to push after dependency-complete CI runs the dashboard test suite; preserve NOT LIVE-READY posture.

## 2026-06-19 Patch Addendum — Alembic revision graph integrity repair

### Why the patch was needed
- `alembic upgrade head` failed because the migration graph contained a dangling `down_revision`: `0002_adaptive_learning_lifecycle` pointed to `0001_phase1_init`, but no loaded migration declared that revision identifier.
- The base migration file existed as `alembic/versions/0001_phase1_init.py`, but its internal `revision` metadata was `0001_phase1`, creating a filename/header/lineage mismatch.

### Root cause
- The initial Phase 1 migration was not deleted or replaced by SQLite bootstrap logic; it was present under the expected filename and contains the base schema DDL.
- The revision identifier inside that base migration was shortened/renamed to `0001_phase1` while the next migration retained the intended `down_revision = "0001_phase1_init"` lineage.

### Files changed
- `alembic/versions/0001_phase1_init.py`
- `tests/test_alembic_revision_graph.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Migration graph before
- Present revision declared by base file: `0001_phase1` with `down_revision = None`.
- Adaptive lifecycle migration: `0002_adaptive_learning_lifecycle` with `down_revision = "0001_phase1_init"`.
- Result: dangling reference to missing `0001_phase1_init`; Alembic script loading can warn and then fail with `KeyError` when resolving heads/upgrades.

### Migration graph after
- Base migration: `0001_phase1_init` with `down_revision = None`.
- Adaptive lifecycle migration: `0002_adaptive_learning_lifecycle` with `down_revision = "0001_phase1_init"`.
- Result: single linear graph `0001_phase1_init -> 0002_adaptive_learning_lifecycle` with head `0002_adaptive_learning_lifecycle`.

### Runtime behavior changes
- None. No trading thresholds, scoring, reject logic, lifecycle semantics, scanner behavior, order behavior, or execution-cost logic changed.
- This is an Alembic metadata-lineage repair only.

### Lifecycle changes
- None. Lifecycle transition emission and persistence semantics are unchanged.

### Persistence changes
- No table/column DDL was changed in the base migration body.
- Fresh Alembic databases now have a resolvable migration chain and can apply the existing base and adaptive lifecycle migrations in order.
- Existing databases whose `alembic_version` table contains the erroneous `0001_phase1` value need explicit operator review/remediation; this patch intentionally does not use `alembic stamp` or fake migration success.

### Export/schema changes
- None. CSV exports and runtime schema definitions are unchanged by this metadata repair.

### Tests added
- Added a static regression test that fails on any Alembic migration referencing a missing `down_revision`.
- Added an Alembic `ScriptDirectory` regression test that verifies script loading and head resolution when the Alembic package is installed.
- Added a temporary SQLite `alembic upgrade head` regression test when the Alembic package is installed.

### Tests executed
- `pytest -q tests/test_alembic_revision_graph.py` passed for the static graph test, with Alembic-dependent tests skipped in this container because the Alembic package is not installed.

### Risks
- Low for fresh databases and repositories that had not stamped the incorrect `0001_phase1` revision.
- Compatibility risk exists for any database already stamped with `0001_phase1`; those databases should be handled through explicit migration-version remediation reviewed against actual schema state, not blind stamping.

### Remaining limitations
- This patch does not reconcile SQLAlchemy metadata-vs-Alembic DDL completeness beyond the reported graph integrity issue.
- LIVE readiness remains unchanged and not approved.

### Migration concerns
- No manual action is needed for fresh databases.
- Operators should inspect `alembic_version` on existing databases before upgrading; if it contains `0001_phase1`, verify the Phase 1 schema is present before applying a deliberate version-table correction.

### Push recommendation
- Safe to push as a minimal Alembic lineage repair after validating Alembic-dependent tests in a normal development environment with project dependencies installed.

## 2026-06-19 Patch Addendum — SQLite schema migration bootstrap legacy regression hardening

### Why the patch was needed
- The reported failure path was `init_db(...) -> _apply_sqlite_migrations(conn) -> SELECT version FROM schema_migrations`, which crashes on fresh or partial legacy SQLite databases if migration bookkeeping has not been created first.
- The implementation already creates `schema_migrations` before the version query; this patch strengthens regression coverage for repeated legacy initialization so the bootstrap ordering cannot regress silently.

### Root cause
- A migration bootstrap path that queries applied versions before creating `schema_migrations` is invalid for brand new SQLite files and legacy files that predate migration bookkeeping.
- Partial legacy databases can contain runtime tables and user rows while still lacking `schema_migrations`.

### Files changed
- `src/alphaforge/persistence.py`
- `tests/test_sqlite_schema_bootstrap.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- None. The production bootstrap remains `_ensure_sqlite_schema_migrations_table(conn)` followed by `SELECT version FROM schema_migrations` inside `_apply_sqlite_migrations(conn)`.
- No trading thresholds, scoring, reject logic, lifecycle semantics, scanner behavior, order behavior, or execution-cost logic changed.

### Lifecycle changes
- None. Lifecycle transition emission and persistence semantics are unchanged.

### Persistence changes
- No new schema change beyond the existing idempotent `CREATE TABLE IF NOT EXISTS schema_migrations` bootstrap contract.
- Regression coverage now verifies a legacy `order_decisions` table without `schema_migrations` preserves existing rows, creates migration bookkeeping, and records the persistence migration once across repeated `init_db(...)` calls.

### Export/schema changes
- None. CSV exports and runtime data-table schemas are unchanged by this test-only hardening patch.

### Tests added
- Extended `test_init_db_migrations_are_idempotent_and_preserve_data` to assert `schema_migrations` exists and contains exactly one `2026_05_16_persistence_integrity_v1` row after repeated initialization of a partial legacy SQLite database.

### Tests executed
- `pytest -q tests/test_sqlite_schema_bootstrap.py` passed.
- `pytest -q tests/test_runtime.py tests/test_runtime_heartbeat.py tests/test_dashboard_app.py tests/test_job22_dashboard_rollback_evidence.py` passed.
- `pytest -q` failed during collection because this container cannot import NumPy for `tests/test_timesfm_futures.py`.
- `python -m pip install numpy` failed because package-index access returned 403 Forbidden for `/simple/numpy/`.

### Risks
- Low; this patch changes regression assertions only and leaves production persistence code unchanged.

### Remaining limitations
- LIVE readiness remains unchanged and not approved.
- Migration safety still depends on continued fresh and legacy SQLite test coverage.

### Migration concerns
- No manual migration required; existing bootstrap DDL is `CREATE TABLE IF NOT EXISTS` and the regression confirms user rows are preserved.

### Push recommendation
- Safe to push as persistence/bootstrap regression hardening; do not change LIVE readiness posture.

## 2026-06-19 SQLite rollback evidence bootstrap hardening

### Why the patch was needed
- Fresh SQLite bootstrap could leave dashboard/readiness rollback evidence reads dependent on a later rollback evidence write path to create `live_rollback_validation_evidence`.
- The previous Alembic chain repair concern also required verifying migration bookkeeping exists before any applied-version reads.

### Root cause
- Rollback evidence schema creation lived in `alphaforge.rollback_evidence.ensure_rollback_evidence_schema(...)`, which is called by the persistence write path but not by generic `init_db(...)` bootstrap.
- Fresh dashboard/readiness database paths can query rollback evidence status before any evidence has been persisted.

### Files changed
- `src/alphaforge/persistence.py`
- `src/alphaforge/persistence.py`
- `tests/test_sqlite_schema_bootstrap.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- `init_db(...)` now creates the canonical `live_rollback_validation_evidence` table and its read index idempotently during SQLite bootstrap.
- `schema_migrations` is still created before selecting applied versions.
- No trading thresholds, scoring, reject logic, lifecycle semantics, scanner behavior, order behavior, or execution-cost logic changed.

### Lifecycle changes
- None. Lifecycle transition emission and persistence semantics are unchanged.

### Persistence changes
- Additive SQLite-only bootstrap DDL for canonical rollback validation evidence storage.
- Existing rollback evidence rows and runtime tables are preserved by `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`; no drop/recreate behavior was added.
- A new migration marker `2026_06_19_rollback_evidence_bootstrap` records the additive bootstrap repair.

### Export/schema changes
- No CSV/export behavior changed.
- SQLite schema now includes rollback evidence storage immediately after fresh `init_db(...)`.

### Tests added
- Regression coverage verifies fresh `init_db(...)` creates `live_rollback_validation_evidence` with the canonical columns and index.

### Tests executed
- `pytest tests/test_sqlite_schema_bootstrap.py -q`
- `pytest tests/test_runtime_heartbeat.py -q`
- `pytest tests/test_job22_dashboard_rollback_evidence.py -q`
- `pytest -q`

### Risks
- The rollback evidence DDL is duplicated between generic persistence bootstrap and rollback evidence writer bootstrap; future schema changes must keep both idempotent definitions aligned.

### Remaining limitations
- LIVE readiness remains blocked; this patch only ensures audit/readiness evidence storage exists.

### Migration concerns
- Additive and idempotent for SQLite. Existing databases keep their rows; missing tables/indexes are created on next bootstrap.

### Push recommendation
- Merge recommended after full test pass; low blast radius persistence bootstrap repair.

## 2026-06-19 Patch Addendum — SQLite schema migration bootstrap regression

### Why the patch was needed
- Fresh and partially legacy SQLite databases must initialize without failing before migration bookkeeping exists.
- The persistence migration path needed explicit regression coverage that `schema_migrations` is present before version reads occur.

### Root cause
- `_apply_sqlite_migrations` depends on `schema_migrations` for idempotency bookkeeping; the safe behavior is to bootstrap that table before selecting existing versions.
- The bootstrap contract needed to be isolated into an explicit helper so `CREATE TABLE IF NOT EXISTS schema_migrations` remains visibly ahead of the version read and direct fresh-database regression coverage.

### Files changed
- `src/alphaforge/persistence.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- SQLite migration bookkeeping is explicitly created by `_ensure_sqlite_schema_migrations_table()` before `SELECT version FROM schema_migrations` runs.
- Migration execution remains idempotent through the existing version table and does not alter trading thresholds, scoring, reject logic, or order lifecycle behavior.

### Lifecycle changes
- None. No lifecycle transitions or reject semantics changed.

### Persistence changes
- Fresh SQLite databases now have a guaranteed `schema_migrations` table during migration bootstrap.
- Existing legacy table data remains preserved; migrations still add only missing runtime columns and normalize existing persistence fields.

### Export/schema changes
- Adds no export fields and no trading-data schema change beyond ensuring migration bookkeeping exists.

### Tests added
- No new test file changes in this patch; existing SQLite bootstrap regression coverage validates `schema_migrations` creation and migration idempotency.

### Tests executed
- `pytest tests/test_sqlite_schema_bootstrap.py -q` passed.
- `pytest tests/test_runtime_heartbeat.py -q` passed.
- `pytest -q` failed during collection because this container cannot import NumPy for `tests/test_timesfm_futures.py`.

### Risks
- Low; the patch is limited to SQLite migration bootstrap ordering/coverage and does not modify execution decisions.

### Remaining limitations
- LIVE readiness remains unchanged and not approved.
- Migration safety still depends on tests continuing to cover fresh and legacy SQLite database shapes.

### Migration concerns
- No manual migration required; the bootstrap uses `CREATE TABLE IF NOT EXISTS` and remains backward-compatible with databases that already have `schema_migrations`.

### Push recommendation
- Safe to push as a minimal persistence/bootstrap fix after validating full-suite dependencies in a normal dev environment; do not change LIVE readiness posture.

## 2026-06-19 Patch Addendum — TimesFM unbatched quantile + optional integration smoke hardening

### Why the patch was needed
- Real TimesFM tuple outputs can be returned without an explicit batch dimension, so quantile matrices shaped `(horizon, 10)` and `(horizon, 9)` needed deterministic regression coverage.
- NumPy-backed tests previously used `pytest.importorskip`, which could silently hide coverage in a correctly provisioned development environment.
- The real TimesFM package/model smoke needed to remain explicit and opt-in through `ALPHAFORGE_RUN_TIMESFM_INTEGRATION=1`.

### Root cause
- Quantile tuple parsing distinguished batch-vs-series shape through the generic point-series helper, which was safe for batched tuples but ambiguous for unbatched quantile matrices.
- Test coverage validated batched ndarray layouts only and skipped NumPy tests when NumPy was absent instead of requiring the declared dev dependency.

### Files changed
- `src/alphaforge/models/timesfm_forecaster.py`
- `tests/test_timesfm_futures.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- TimesFM tuple parsing now selects the quantile horizon row with quantile-specific shape detection, supporting both batched `(1, horizon, width)` and unbatched `(horizon, width)` arrays.
- No trade threshold, signal scoring, order lifecycle, persistence write path, exchange adapter, or LIVE execution behavior changed.

### Lifecycle changes
- None. TimesFM remains a forecast-decision logger only; invalid forecasts still produce `NO_TRADE` / `INVALID_FORECAST`.

### Persistence changes
- None. No schema, SQLite, or CSV field change was introduced.

### Export/schema changes
- None. Decision log columns are unchanged.

### Tests added
- Added unbatched NumPy tuple regression coverage for `(horizon, 10)` mean-plus-decile output.
- Added unbatched NumPy tuple regression coverage for `(horizon, 9)` older quantile output.
- Added an opt-in real TimesFM integration smoke gated by `ALPHAFORGE_RUN_TIMESFM_INTEGRATION=1`.
- Added a LIVE-mode rejection regression to confirm the TimesFM replay API remains PAPER/BACKTEST-only and introduces no order path.

### Tests executed
- `pytest -q tests/test_timesfm_futures.py` failed in this container because NumPy could not be imported.
- `python -m pip install -e '.[dev]'` failed because package-index access for build dependencies returned 403 Forbidden.

### Risks
- Real TimesFM default factory construction may require upstream-specific model arguments; the optional smoke intentionally fails when enabled in an incorrectly configured environment.
- Current container could not validate NumPy-backed tests because dependency installation was blocked by package-index access.

### Remaining limitations
- TimesFM model weights/package setup remains external.
- Execution costs such as spread, slippage, funding, liquidity, and latency remain unavailable in this research module and are not faked.

### Migration concerns
- None expected; parser behavior is backward-compatible for existing batched tuple and dict outputs.

### Push recommendation
- Safe to push after validating `pytest -q tests/test_timesfm_futures.py` in a normal dev environment with NumPy installed; do not enable LIVE based on this patch.

## 2026-06-19 Patch Addendum — TimesFM post-merge API/output compatibility hardening

### Why the patch was needed
- PR #177 left unresolved compatibility risk around real TimesFM forecast call signatures and returned tuple/NumPy output shapes.
- Quantile extraction could misread a mean column as p10 for TimesFM layouts that return mean followed by q10...q90 columns.

### Root cause
- The wrapper assumed a single `forecast([series], horizon_len=horizon)` API surface and list/tuple-only output handling.
- Tuple parsing treated the first and last quantile columns as p10/p90, which is wrong for mean-plus-decile layouts where column 0 is the mean and p10 begins at column 1.

### Files changed
- `src/alphaforge/models/timesfm_forecaster.py`
- `tests/test_timesfm_futures.py`
- `pyproject.toml`
- `requirements.txt`
- `VERSION.md`
- `CHANGELOG.md`
- `REPORT.md`

### Runtime behavior changes
- `TimesFMForecaster` now tries compatible TimesFM call surfaces using `inputs` plus `horizon`, `horizon_len`, positional variants, and legacy `freq` variants before failing closed.
- The parser now accepts generic sequence-like outputs, including NumPy arrays, while keeping malformed output fail-closed as `TimesFMForecastError`.
- Tuple quantile parsing now supports mean-plus-q10...q90, q10...q90, and p10/p50/p90 compact layouts.

### Lifecycle changes
- No order lifecycle states are emitted or advanced.
- Bad model output continues to become `NO_TRADE` with `INVALID_FORECAST` during replay instead of creating trades.

### Persistence/export/schema changes
- No database or CSV schema changes.
- Rejected forecast rows remain exportable with null forecast/order fields where quality is unavailable.

### Tests added
- `test_timesfm_tuple_numpy_mean_plus_deciles_extracts_true_p10_p50_p90`
- `test_timesfm_tuple_numpy_older_nine_quantile_layout_is_supported`
- `test_timesfm_forecaster_tries_legacy_freq_signature`
- `test_timesfm_malformed_numpy_output_raises_forecast_error`
- `test_replay_logs_invalid_forecast_for_malformed_real_shaped_model_output`

### Tests executed
- `pytest -q tests/test_timesfm_futures.py`
- `pytest -q`
- `python -m compileall -q src/alphaforge tests`

### Risks / remaining limitations
- Real TimesFM installation/model weights remain external and were not exercised against a live package in this environment.
- Local package-index access denied NumPy installation, so NumPy-specific tests skip unless NumPy is available from the test environment.
- This patch does not add spread, slippage, funding, liquidity, latency, fill, or exchange-rejection modeling.

### Migration concerns
- No schema migration required.
- Test environments should install development dependencies, including NumPy, to execute ndarray-specific regression tests.

### Push recommendation
- Safe to merge as a contained PAPER/BACKTEST compatibility hardening patch. Do not enable LIVE trading.

## 2026-06-19 Patch — TimesFM BTCUSDT futures PAPER/BACKTEST forecasting module

### Why the patch was needed
- The repository needed an execution-safe research module for TimesFM-based BTCUSDT futures forecasts without introducing any LIVE order path.
- Forecast decisions needed auditable quantile, RR, and rejection fields for PAPER/BACKTEST replay.

### Root cause
- No existing module combined Binance USD-M Futures candle loading, TimesFM quantile forecasts, no-lookahead historical replay, and decision logging for BTCUSDT forecast research.

### Files changed
- `src/alphaforge/historical_market_data.py` (existing Binance Futures kline pagination reused)
- `src/alphaforge/models/timesfm_forecaster.py`
- `src/alphaforge/timesfm_futures.py`
- `tests/test_timesfm_futures.py`
- `VERSION.md`
- `CHANGELOG.md`
- `REPORT.md`

### Runtime behavior changes
- Added a PAPER/BACKTEST-only TimesFM futures replay path for BTCUSDT 15m/1h candles.
- Added quantile-to-decision conversion for horizons 8, 16, and 24 with `LONG`, `SHORT`, or `NO_TRADE` output.
- LIVE mode is explicitly rejected by the replay API; the module has no order-placement function or execution adapter integration.

### Lifecycle changes
- No production order lifecycle transitions are emitted.
- Forecast outputs are decision/audit records only; rejected forecasts remain visible as `NO_TRADE` with `rejection_reason`.

### Persistence/export/schema changes
- No database schema changes.
- Added CSV export support for every required decision field: timestamp, symbol, timeframe, current price, p10/p50/p90, side, entry, stop, take profit, expected RR, and rejection reason.
- Unavailable forecast/order fields are written as null/empty CSV values rather than fake defaults.

### Backtest metrics
- Unit replay fixture: 70 historical candles with `min_history=64` produced 7 replay decisions, matching the number of decision points available without future candles.
- Decision conversion tests cover one accepted LONG, one accepted SHORT, one low-confidence NO_TRADE, and invalid forecast rejection.
- No PnL, win-rate, drawdown, spread, slippage, funding, or latency metrics are claimed because this patch does not simulate fills or real execution costs.

### Tests added
- `test_loader_uses_binance_futures_btcusdt_15m_and_1h`
- `test_backtest_replay_prevents_lookahead_bias`
- `test_invalid_forecast_handling_logs_no_trade_rejection`
- `test_long_decision_from_quantile_forecast`
- `test_short_decision_from_quantile_forecast`
- `test_no_trade_decision_from_low_confidence_forecast`

### Tests executed
- `pytest -q tests/test_timesfm_futures.py`

### Risks / remaining limitations
- Actual TimesFM inference depends on installing/configuring the optional external `timesfm` package and model weights.
- The wrapper supports common TimesFM output shapes but may require adaptation for a specific upstream release.
- The module does not model spread, slippage, funding, liquidity, latency, partial fills, or exchange rejection.
- The module is not LIVE-ready and must remain research/logging only until execution realism and lifecycle integration are separately verified.

### Migration concerns
- None for database users; no schema migration is required.
- Consumers should treat CSV logs as a new research artifact, not canonical live execution evidence.

### Push recommendation
- Merge as a contained PAPER/BACKTEST research capability. Do not enable for LIVE trading.

## 2026-05-22 Patch Addendum — Minimal follow-up: startup incident persistence rollback + defensive evidence parsing

### Why the patch was needed
- LIVE qualification startup was persisting reconciliation findings into `reconciliation_incidents`, creating false operational history during preflight gating.
- Mode parity numeric parsing could raise on malformed evidence payloads and risk aborting readiness flow instead of persisting fail-closed reports.
- Forensic sanitation over-redacted benign keys containing `signed`, including legitimate metadata.

### Root cause
- `_run_live_qualification_gate()` persisted canonical findings unconditionally when provider evidence was COMPLETE.
- `_check_runtime()` used direct `int(...)` casts for evidence counters.
- `_sanitize_runtime_snapshot()` blocked keys using substring `signed` rather than sensitive key semantics/value redaction.

### Files changed
- `src/alphaforge/runtime.py`
- `src/alphaforge/live_readiness.py`
- `tests/test_live_readiness.py`
- `tests/test_live_readiness_security_regression.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- LIVE qualification startup still fails closed on canonical reconciliation findings (orphan/duplicate/fail-closed) but no longer writes startup findings into `reconciliation_incidents`.
- Qualification startup now explicitly reports `incident_persistence_verified=false`.
- Invalid parity numeric evidence values (`None`, `''`, `N/A`, malformed strings) now fail closed without exceptions; readiness report persistence continues.

### Lifecycle/persistence/schema impact
- No lifecycle transition changes.
- No schema changes.
- `live_readiness_reports` persistence remains intact even for invalid parity evidence payloads.

### Tests added/updated
- Added fail-closed parity parsing regression with persisted readiness report.
- Added LIVE qualification regression using canonical orphan/duplicate snapshot and asserting no incident rows written at startup.
- Strengthened forensic redaction regression to assert `assigned_symbols` retention and signed/auth/signature redaction.

### Risks / remaining limitations
- LIVE still not ready; observability evidence remains intentionally blocking without complete measured proof.
- This patch does not alter scoring, RR, thresholds, trade frequency, adapter behavior, or order submission paths.

### Push recommendation
- Merge as minimal follow-up patch restoring startup persistence semantics while preserving fail-closed LIVE qualification.

## 2026-05-22 Patch Addendum — Evidence-based parity/operational readiness checks

### Why the patch was needed
- LIVE readiness still accepted placeholder booleans for parity/observability/rollback without persisted, measurable operational evidence.

### Root cause
- `LiveReadinessEvaluator` runtime/operational checks were boolean shortcuts (`all(mode_parity.values())`, `alerts_configured`, `rollback_ready`) with no structured evidence sufficiency contract.

### Files changed
- `src/alphaforge/live_readiness.py`
- `src/alphaforge/runtime.py`
- `tests/test_live_readiness.py`
- `VERSION.md`
- `CHANGELOG.md`
- `REPORT.md`

### Runtime behavior changes
- Mode parity now fail-closes unless evidence is COMPLETE and satisfies minimum sample, zero mismatch, zero missing-field, and no-submit verification constraints.
- Observability and rollback checks now require explicit measured evidence fields rather than static booleans.
- Forensic snapshot export now sanitizes runtime snapshot keys that look like credentials/signatures/auth headers.

### Lifecycle/persistence/schema impact
- No schema changes.
- Existing `live_readiness_reports.report_payload` persists structured evidence details safely.

### Security/execution safety
- No real order submission/cancel/modify/close path introduced.
- No exchange mutation path added.
- Reconciliation remediation posture remains dry-run/non-mutating.

### Remaining limitations / blockers
- Alert delivery verification is not implemented and remains an explicit readiness blocker.
- Real execution readiness remains unavailable.
- LIVE remains NOT LIVE-READY.

### Push recommendation
- Merge as minimal fail-closed evidence hardening increment.

## 2026-05-22 Patch Addendum — LIVE canonical reconciliation evidence-chain hardening

### Why the patch was needed
- LIVE qualification consumed provider snapshot fields directly and could trust optimistic orphan/duplicate counters without canonical runtime-intent comparison.

### Root cause
- Canonical reconciliation ownership was split: provider returned summary counters while readiness gate relied on those counters instead of reconciliation findings produced by AlphaForge runtime logic.

### Files changed
- `src/alphaforge/runtime.py`
- `src/alphaforge/reconciliation.py`
- `src/alphaforge/live_readiness.py`
- `tests/test_reconciliation.py`
- `tests/test_live_readiness.py`
- `tests/test_runtime.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- LIVE qualification now treats authenticated provider as raw read-only exchange evidence source only.
- LIVE qualification converts provider snapshot to canonical reconciliation snapshot and runs `ReconciliationEngine.reconcile(...)` against runtime intended orders/lifecycle state.
- Provider-supplied `orphan_orders` / `orphan_positions` / `duplicate_fills` values are ignored for qualification decisions.
- LIVE readiness now fails closed when provider evidence is incomplete and when canonical fail-closed findings are present.

### Lifecycle/persistence/schema impact
- No schema changes.
- Reconciliation findings continue to persist through existing `reconciliation_incidents` persistence layer, including duplicate-fill incidents.

### Security/redaction impact
- No API keys/secrets/signatures added to incident payloads; persisted payloads contain only normalized safe reconciliation evidence.

### Remaining limitations / blockers
- Remediation suggestions remain dry-run/operator-review only.
- No order create/cancel/modify/close behavior introduced.
- LIVE remains blocked by broader readiness requirements and missing production execution/operational evidence.

### Push recommendation
- Merge as minimal fail-closed P0/P1 patch.

## 2026-05-22 Patch Addendum — Authenticated Binance READ-ONLY reconciliation provider

### Why the patch was needed
- LIVE qualification/readiness required authenticated reconciliation provider evidence, but no provider existed.

### Root cause
- The runtime had a reconciliation provider contract and fail-closed requirement, but no authenticated Binance USER_DATA implementation.

### Files changed
- `src/alphaforge/binance_reconciliation_provider.py`
- `src/alphaforge/runtime.py`
- `src/alphaforge/config.py`
- `src/alphaforge/config/__init__.py`
- `tests/test_binance_reconciliation_provider.py`
- `tests/test_runtime_env_config.py`
- `.env.example`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- Added read-only Binance reconciliation snapshot support with signed GET-only USER_DATA calls.
- Runtime wires provider only in LIVE when `ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION=true` and complete credentials are configured.
- LIVE fails closed with explicit missing/partial credential errors when reconciliation is enabled but credentials are incomplete.

### Security/redaction behavior
- API secret and signature are never persisted in evidence snapshots.
- Failure payloads are sanitized to class-level redacted errors.

### Orphan coverage strategy
- Uses global `/fapi/v3/positionRisk` and global `/fapi/v1/openOrders` to preserve orphan discovery capability.
- Uses bounded symbol-scoped `/fapi/v1/userTrades` only for tracked/open-position symbols.

### Lifecycle/persistence/schema impact
- No schema changes.
- No execution-path changes.

### Tests executed
- `pytest -q tests/test_binance_reconciliation_provider.py tests/test_runtime_env_config.py::test_live_reconciliation_enabled_requires_credentials`

### Remaining limitations / blockers
- No real order submission adapter exists.
- Mode parity/observability/rollback readiness evidence remains unverified.
- LIVE remains blocked/not ready by design.

### Push recommendation
- Merge as minimal authenticated read-only reconciliation evidence increment.

## 2026-05-22 Patch Addendum — LIVE qualification evidence fail-closed + scanner/reconciliation provenance hardening

### Why the patch was needed
- LIVE qualification still used optimistic hardcoded evidence payloads that could pass checks without measured runtime proof.
- LIVE reconciliation logic used in-memory runtime state snapshots only, which is insufficient as exchange-state evidence.
- Runtime bootstrap referenced `scanner_source` at construction time without deterministic assignment on all paths.

### Root cause
- `_run_live_qualification_gate()` supplied static pass-biased snapshots for mode parity, reconciliation, and observability.
- `_reconcile_runtime_state()` always built snapshots from `_pending_orders`/`_active_positions` regardless of mode.
- `_build_runtime_from_env()` passed `scanner_source` without guaranteed initialization.
- LIVE startup scanner checks were blacklist-based; UNKNOWN/unverified provenance could remain ambiguous.

### Files changed
- `src/alphaforge/runtime.py`
- `src/alphaforge/live_readiness.py`
- `tests/test_runtime.py`
- `tests/test_live_readiness.py`
- `tests/test_exchange_connectivity.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- LIVE startup now requires explicit allowlisted scanner provenance and blocks unverified/unknown sources fail-closed.
- Runtime bootstrap now assigns deterministic scanner provenance (`SAFE_PLACEHOLDER` for safe override, otherwise `EXCHANGE_PUBLIC_MARKET_DATA`).
- LIVE qualification now uses fail-closed evidence defaults and records explicit missing evidence reasons:
  - `MODE_PARITY_UNVERIFIED`
  - `LIVE_RECONCILIATION_PROVIDER_MISSING`
  - `OBSERVABILITY_EVIDENCE_UNVERIFIED`
  - `ROLLBACK_EVIDENCE_UNVERIFIED`
- LIVE reconciliation now requires an explicit reconciliation provider and blocks when absent.

### Lifecycle/persistence/schema impact
- No lifecycle schema changes.
- No persistence schema rewrite; readiness report payload now carries explicit missing-evidence details in existing `live_readiness_reports` table.

### Tests added/updated
- Added/updated scanner provenance and bootstrap determinism tests.
- Added fail-closed readiness evidence tests including persisted report detail checks.
- Updated LIVE connectivity runtime tests to set explicit allowlisted scanner provenance when testing connectivity gate behavior.

### Risks / limitations
- No authenticated exchange snapshot provider was introduced in this patch.
- LIVE remains intentionally blocked until real reconciliation provider evidence is available.
- No order placement capability was added.

### Push recommendation
- Merge as minimal P0 fail-closed hardening before any further LIVE enablement work.

## 2026-05-22 Patch Addendum — P0 LIVE startup scanner/adapter guards + Binance Futures gate consistency

### Why the patch was needed
- LIVE startup safety checks could be bypassed by runtime scanner wrapper indirection and did not fail early when no real execution adapter existed.
- Binance runtime scanner used Futures endpoints while config default/connectivity checks could still validate Spot assumptions.

### Root cause
- LIVE scanner guard relied on function `__name__` rather than resolved scanner provenance.
- LIVE adapter guard existed only inside execution path, after loops started.
- Binance default host and connectivity probe endpoint family were inconsistent with Futures runtime scanner endpoints.

### Files changed
- `src/alphaforge/runtime.py`
- `src/alphaforge/config/__init__.py`
- `src/alphaforge/exchange_connectivity.py`
- `tests/test_runtime.py`
- `tests/test_config_layer.py`
- `tests/test_exchange_connectivity.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- LIVE startup now blocks when resolved scanner source is safe/placeholder/mock/offline/synthetic and raises:
  - `LIVE mode blocked: safe/placeholder market scanner is not allowed`
- LIVE startup now blocks pre-loop when `real_execution_adapter` is missing and raises:
  - `LIVE mode blocked: real execution adapter is not configured`
- Binance connectivity now validates Futures endpoints (`/fapi/v1/ticker/bookTicker`, `/fapi/v1/premiumIndex`, optional `/fapi/v1/time`) and only marks connected when Futures orderbook+funding checks pass.
- Binance default base URL now resolves to `https://fapi.binance.com` when `BINANCE_BASE_URL` is unset.

### Lifecycle/persistence/schema impact
- No lifecycle schema changes.
- No persistence schema changes.

### Tests added/updated
- Added/updated regression tests for LIVE scanner-wrapper block, LIVE missing adapter startup block, Binance Futures default host, Futures-only connectivity endpoint checks, funding fail-closed behavior, and Spot-only non-qualification.
- Updated stale runtime expectation tests to validate effective behavior rather than wrapper function-name assumptions.

### Tests executed
- `pytest -q tests/test_runtime.py`
- `pytest -q tests/test_config_layer.py`
- `pytest -q tests/test_exchange_connectivity.py`
- `pytest -q tests/test_exchange_market_scanner.py`
- `pytest -q`

### Risks / limitations
- This patch does not introduce real order submission and does not change acceptance thresholds.
- LIVE readiness remains blocked by additional unresolved requirements outside this P0 patch.

### Push recommendation
- Merge as a minimal fail-closed safety patch before further LIVE transition work.

## 2026-05-22 Patch Addendum — Binance Futures bookTicker spread derivation hardening

### Why the patch was needed
- Binance scanner still used Spot `/api/v3/ticker/24hr` and relied on ticker bid/ask fields directly, leaving Futures consistency and spread provenance weaker than intended.

### Root cause
- Scanner endpoint mix was split between Spot and Futures families and did not explicitly require Futures `bookTicker` for spread derivation.

### Files changed
- `src/alphaforge/exchange_market_scanner.py`
- `tests/test_exchange_market_scanner.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- Binance scan now uses Futures endpoints consistently:
  - `/fapi/v1/ticker/24hr`
  - `/fapi/v1/ticker/bookTicker`
  - `/fapi/v1/premiumIndex`
- `entry` now uses conservative price selection `min(last_price, mid)` where `mid=(bid+ask)/2`.
- `spread_pct` and `spread_bps` are now derived from `bookTicker` bid/ask only.
- If `bookTicker` data is unavailable or malformed for a symbol, that symbol is skipped (fail-closed; no optimistic spread synthesis).
- PAPER/LIVE runtime wiring remains unchanged from v2.

### Lifecycle/persistence/schema impact
- No lifecycle changes.
- No persistence schema changes.

### Tests added/updated
- Updated scanner tests to cover Futures endpoint family, spread mapping from `bookTicker`, malformed payload fail-closed behavior, and deterministic URL assertions.

### Tests executed
- `pytest -q tests/test_exchange_market_scanner.py tests/test_runtime.py::test_build_runtime_uses_exchange_scanner_for_paper tests/test_runtime.py::test_build_runtime_keeps_safe_scanner_for_backtest`

### Risks / limitations
- Binance symbol coverage may decrease temporarily when `bookTicker` is incomplete for some symbols; this is intentional fail-closed behavior.
- Hyperliquid support remains mids-only as in v2.

### Push recommendation
- Recommended to merge as a small safe follow-up focused on spread realism and endpoint consistency.

## 2026-05-21 Patch Addendum — PAPER/LIVE read-only exchange scanner alignment

### Why the patch was needed
- Runtime PAPER/LIVE bootstrap scanner used deterministic placeholder BTC input, preventing real exchange market-data rehearsal.

### Root cause
- `_build_runtime_from_env()` always wired `_safe_market_scanner` regardless of execution mode.

### Files changed
- `src/alphaforge/runtime.py`
- `src/alphaforge/exchange_market_scanner.py`
- `tests/test_runtime.py`
- `tests/test_exchange_market_scanner.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- PAPER and LIVE now share `scan_exchange_markets(config)` read-only scanner path using public endpoints.
- BACKTEST continues to use `_safe_market_scanner` by default to avoid live dependency.
- Offline smoke override available via `ALPHAFORGE_RUNTIME_SAFE_SCANNER=1`.
- LIVE fail-closed protections remain: placeholder scanner block, exchange-connectivity gate, qualification gate, and required real execution adapter.

### Lifecycle/persistence/schema impact
- No lifecycle schema changes.
- No persistence schema changes.

### Tests added/updated
- Added `tests/test_exchange_market_scanner.py`.
- Added runtime bootstrap scanner wiring tests for PAPER and BACKTEST.

### Tests executed
- `pytest -q tests/test_exchange_market_scanner.py tests/test_runtime.py::test_build_runtime_uses_exchange_scanner_for_paper tests/test_runtime.py::test_build_runtime_keeps_safe_scanner_for_backtest`

### Risks / limitations
- Hyperliquid public scan currently provides mids-only (limited spread/volume detail), so selection may naturally reject more symbols; this is fail-safe.
- Public endpoint shape changes upstream could reduce candidate availability, which intentionally fail-closes to fewer/no trades.

### Push recommendation
- Recommended to merge as a minimal execution-rehearsal alignment patch without threshold loosening.

## 2026-05-21 Patch Addendum — LIVE connectivity default fail-closed + startup contradiction resolution

### Why the patch was needed
- LIVE startup safety messaging and behavior were inconsistent across recent summaries.
- LIVE connectivity gating existed but was optional-by-default, which is not fail-closed for production startup.

### Root cause
- `RuntimeConfig.require_exchange_connectivity_for_live` defaulted to `False`.
- `_build_runtime_from_env()` did not wire exchange connectivity env config into `RuntimeConfig`.

### Files changed
- `src/alphaforge/runtime.py`
- `tests/test_exchange_connectivity.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- Confirmed existing LIVE fail-closed guard remains in `RuntimeOrchestrator.start()` for `_safe_market_scanner`.
- LIVE exchange connectivity gate now defaults to required (`require_exchange_connectivity_for_live=True`).
- LIVE connectivity gate can still be explicitly bypassed for tests/overrides via config/env.
- PAPER and BACKTEST behavior remain unchanged.

### Lifecycle/persistence/schema impact
- No lifecycle changes.
- No persistence schema changes.

### Tests added/updated
- Added `test_live_startup_requires_exchange_connectivity_by_default`.
- Added `test_paper_start_does_not_require_exchange_connectivity_by_default`.
- Added `test_live_can_only_skip_connectivity_when_explicitly_configured_for_test_or_override`.
- Existing `test_live_start_blocks_placeholder_bootstrap_scanner` remains as guard proof.

### Risks / limitations
- Connectivity gate quality depends on upstream exchange health probe coverage/quality.
- Explicit override can still disable gate; this is intentional for deterministic tests.

### Push recommendation
- Recommended to merge as minimal fail-closed LIVE startup safety patch.

## 2026-05-21 Patch Addendum — LIVE placeholder scanner fail-closed gate

### Why the patch was needed
- LIVE bootstrap could be started with `_safe_market_scanner`, a deterministic local placeholder feed intended only for offline wiring checks.

### Root cause
- Runtime LIVE startup gates validated readiness/connectivity (when enabled) but did not explicitly forbid placeholder/mock scanner wiring.

### Files changed
- `src/alphaforge/runtime.py`
- `tests/test_runtime.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- `RuntimeOrchestrator.start()` now blocks LIVE startup with: `LIVE mode blocked: placeholder/mock scanner is not allowed` when scanner function resolves to `_safe_market_scanner`.

### Lifecycle/persistence/schema impact
- No lifecycle schema changes.
- No persistence schema changes.

### Tests added
- `test_live_start_blocks_placeholder_bootstrap_scanner` in `tests/test_runtime.py`.

### Tests executed
- `pytest tests/test_runtime.py -q`

### Risks / limitations
- Name-based guard targets known placeholder bootstrap scanner and does not yet classify all possible custom mock scanners.

### Push recommendation
- Recommended to merge as a minimal fail-closed LIVE safety patch.

## 2026-05-21 Patch Addendum — Exchange connectivity safety + offline deterministic tests

### Why the patch was needed
- Exchange adapter wiring checks were missing from deterministic tests, leaving LIVE startup safety under-validated.

### Root cause
- No shared exchange connectivity contract existed for Binance/Hyperliquid health checks, and no opt-in integration marker boundary was defined.

### Files changed
- `src/alphaforge/exchange_connectivity.py`
- `src/alphaforge/runtime.py`
- `tests/test_exchange_connectivity.py`
- `pyproject.toml`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- Added `check_exchange_connectivity(exchange_name)` returning explicit `ExchangeHealth` contract fields.
- Added optional LIVE connectivity gate (`require_exchange_connectivity_for_live`) that fail-closes runtime startup when required exchange checks fail.
- Exchange failures are explicit and never replaced with fake zeros.

### Persistence/schema impact
- No schema migration required.

### Tests added
- Offline mocked Binance success/failure connectivity tests.
- Offline mocked Hyperliquid success/failure connectivity tests.
- Runtime LIVE block regression when exchange connectivity is unhealthy.
- Secret-leak guard assertion for exchange health payloads.
- Opt-in integration tests (`@pytest.mark.integration`) for live public endpoint checks.

### Tests executed
- `pytest tests/test_exchange_connectivity.py -q`
- `pytest tests/test_runtime.py -q`
- `pytest tests/test_sqlite_schema_bootstrap.py -q`
- `pytest -q`

### Risks / limitations
- LIVE connectivity gate is config-controlled (`False` by default) to preserve existing deterministic startup behavior.
- Integration checks remain network-dependent and are skipped unless explicitly enabled.

### Push recommendation
- Recommended to merge; adds deterministic coverage and optional live safety checks without loosening trade logic.


## 2026-05-21 Patch Addendum — Runtime order_decisions audit semantics + mode correction

### Why the patch was needed
- Runtime rejected signals were being persisted twice into `order_decisions` without explicit semantic separation, and the AI/internal `:real:` row used `mode=BACKTEST` even during PAPER runtime.
- This made rejected-decision reporting ambiguous and vulnerable to double-counting.

### Root cause
- `AIBrain._persist_decision(...)` hardcoded mode to `BACKTEST` and used `phase=real` for internal AI audit writes, which looked like canonical final runtime rows.
- Runtime final reject persistence did not explicitly mark canonical finality and often omitted score/RR enrichment fields.
- Reporting checks counted all rejected rows in `order_decisions`, including internal AI audit rows.

### Files changed
- `src/alphaforge/ai_brain.py`
- `src/alphaforge/runtime.py`
- `src/alphaforge/live_readiness.py`
- `tests/test_runtime.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- AI/internal decision persistence now uses runtime-resolved mode from signal/market context and marks internal rows as `phase=ai_internal_real`/`phase=ai_internal_virtual`.
- Runtime canonical rejected persistence is explicitly marked `phase=final` and enriched with score/RR/effective_RR when available.
- Runtime signal payload now propagates runtime mode into AI decision persistence context.

### Persistence/schema impact
- No schema migration required.
- Contract clarified inside existing `order_decisions` structure:
  - canonical runtime final decision rows: `phase=final` (or null legacy)
  - AI/internal audit rows: `phase` prefixed with `ai_internal_`

### Reporting/counting impact
- Live-readiness persistence and reject-rate checks now count only canonical final decision rows (`COALESCE(phase,'final')='final'`), preventing internal AI audit rows from inflating rejected totals.

### Tests added/updated
- Added PAPER runtime regression test validating:
  - runtime-created rejected rows are never persisted with `mode=BACKTEST`
  - canonical final PAPER rejected row has populated key fields (`signal_id`, `symbol`, `reject_reason`, `score`, `rr` where available)
  - final rejected count remains exactly one per runtime signal despite AI/internal audit row persistence
  - AI/internal row remains present but explicitly non-final via `phase=ai_internal_*`

### Risks
- Low-to-moderate: behaviorally safe and backward-compatible, but downstream queries that assumed all `phase=real` rows are final should migrate to canonical-final filtering.

### Remaining limitations
- Historical rows created before this patch may still carry ambiguous `phase` semantics.

### Migration concerns
- Consumers/reports that aggregate `order_decisions` should prefer canonical-final filter (`COALESCE(phase,'final')='final'`) to avoid legacy internal-row double counts.

### Push recommendation
- Recommended to merge; this patch hardens audit semantics without dropping internal AI audit information.


## 2026-05-21 Patch Addendum — runtime duplicate rejected-row completeness fix

### Why the patch was needed
- Runtime rejected candidates were producing a second `order_decisions` row (`decision_id` containing `:real:`) with missing `symbol` and missing `reject_reason`, creating inconsistent duplicate audit rows.

### Root cause
- `AIBrain._persist_decision(...)` inserted into `order_decisions` without populating key rejected-row fields (`symbol`, `reject_reason`, plus score/RR audit context), while runtime reject persistence already wrote a fully-populated reject row.

### Files changed
- `src/alphaforge/ai_brain.py`
- `tests/test_runtime.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- AI decision persistence now writes `symbol`, `mode`, `score`, `rr`, and canonical `reject_reason` into `order_decisions` rows, including `phase=real` rejected rows.
- Existing runtime `signal_id` propagation remains preserved.
- Thresholds/scoring/reject logic are unchanged.

### Persistence/schema impact
- No schema migration required.
- Existing `:real:` rows remain valid decision records, now complete for audit usage rather than sparse duplicates.

### Tests added/updated
- Added regression test ensuring rejected runtime decision rows never persist empty `symbol`/`reject_reason`, and specifically guarding against incomplete `:real:` paired rows.

### Risks
- Low: localized persistence payload enrichment only.

### Push recommendation
- Safe to merge as runtime audit-integrity hardening.
## 2026-05-21 Patch Addendum — Runtime identity propagation + diagnostic lifecycle hardening

### Why the patch was needed
- Runtime persistence showed repeated `REJECTED/UNKNOWN` decisions with missing `signal_id`, and repeated `ERROR` lifecycle rows with empty diagnostics, making incident auditing unreliable.

### Root cause
- Runtime reject callback persisted `reason` without mapping it to `reject_reason`, so canonical reject reason collapsed to `UNKNOWN`.
- Runtime candidate identity (`signal_id`) was not guaranteed before reject/lifecycle persistence callbacks.
- Runtime decision pipeline exceptions were not converted into diagnostic-rich lifecycle error payloads.
- AI decision persistence used a low-entropy decision id (`{signal_id}:{phase}`), causing row upserts to collapse repeated runtime decisions.

### Files changed
- `src/alphaforge/runtime.py`
- `src/alphaforge/ai_brain.py`
- `tests/test_runtime.py`
- `tests/test_ai_feature_dedupe.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- Runtime now resolves a stable non-empty `signal_id` before persistence/lifecycle emission for each candidate and propagates it through reject and lifecycle callbacks.
- Runtime reject persistence now writes explicit `reject_reason` from concrete gate/decision reason instead of dropping to `UNKNOWN`.
- Runtime decision exceptions now emit `ERROR` lifecycle events with `failure_reason` and structured `incident_payload` (exception type/message, symbol, signal_id, phase).

### Persistence/schema impact
- No schema changes.
- Decision-id generation now uses a stable hash over `(signal_id, phase, market_ts|timestamp)` so repeated runtime decisions persist as distinct rows when market timestamp changes.

### Tests added/updated
- Added runtime regression checks for non-empty reject `signal_id` and preserved reject reason semantics.
- Added runtime regression check for exception-to-ERROR lifecycle diagnostics persistence fields.
- Updated AI dedupe test to use fixed `market_ts` for deterministic same-decision upsert.
- Added AI regression check verifying repeated runtime decisions persist consistently in both `order_decisions` and `ai_decision_features`.

### Risks
- Moderate, localized persistence-identity behavior change: decision row cardinality increases for distinct runtime timestamps by design (improves auditability).

### Push recommendation
- Safe to merge as an auditability and persistence-integrity hardening patch.


## 2026-05-21 Patch Addendum — lifecycle persistence strict bool success contract

### Why the patch was needed
- Two Phase 1/2/3 foundation tests asserted identity (`is True`) on `save_trade_lifecycle_event(...)` success, but the helper returned integer-like row IDs/rowcount values (e.g., `1`).

### Root cause
- `save_trade_lifecycle_event(...)` exposed database row identity/rowcount semantics instead of a strict public success/failure boolean contract.

### Files changed
- `src/alphaforge/persistence.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- On successful lifecycle upsert + commit, `save_trade_lifecycle_event(...)` now returns literal `True`.
- Existing SQL statements, `ON CONFLICT` behavior, event_id auto-generation, and commit flow are unchanged.

### Lifecycle/persistence/schema impact
- No schema changes.
- No lifecycle state vocabulary changes.
- Persisted lifecycle rows remain queryable as before (including `lifecycle_state` and `reject_reason`).

### Tests executed
- `pytest tests/test_phase123_foundations.py::test_save_trade_lifecycle_event_persists_state -q`
- `pytest tests/test_phase123_foundations.py::test_trade_lifecycle_generates_event_id_when_missing -q`
- `pytest tests/test_phase123_foundations.py -q`

### Risks / limitations
- Minimal and localized: only success return type was normalized from integer-like to strict bool.

### Push recommendation
- Safe to merge as a contract-correctness patch.

## 2026-05-21 Patch Addendum — Rejected-shadow directional TP/SL hardening

### Why the patch was needed
- Rejected-shadow analytics showed asymmetric behavior: LONG rejected rows produced normal WOULD_TP/WOULD_SL distribution while SHORT rows were near-zero WOULD_TP despite accepted SHORT trades reaching `TP_HIT`.

### Root cause
- `simulate_rejected_counterfactual(...)` used LONG-style TP/SL checks for all sides (`high>=tp`, `low<=sl`) and did not branch on `candidate.side`.

### Files changed
- `backtest_order.py`
- `tests/test_backtest_order_scanner.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Behavior changes
- Rejected-shadow TP/SL touch logic is now side-aware:
  - LONG: TP on `high>=tp`, SL on `low<=sl`.
  - SHORT: TP on `low<=tp`, SL on `high>=sl`.
- Conservative same-candle ambiguity convention is now explicit in-code and identical across both sides: if both TP and SL are touched within a candle, classify as SL to avoid optimistic bias.

### Lifecycle/persistence/schema impact
- No lifecycle state/schema changes.
- No CSV schema changes.
- No score threshold, RR gate, or accepted-order generation logic changes.

### Tests added/updated
- Added rejected-counterfactual tests for LONG/SHORT TP/SL directionality and same-candle ambiguity.
- Added SHORT regression test for `evaluate_rejected_shadow(...)` validating `WOULD_TP` + `effective_tp_hit=True` under passing filters.
- `tests/test_backtest_order_scanner.py` passes fully.

### Risks / limitations
- Intrabar order is still unavailable from OHLC alone; conservative SL-priority tie-break remains a designed approximation.

### Push recommendation
- Safe and recommended: minimal, focused correctness patch for rejected-shadow SHORT outcome evaluation without gate loosening.



## 2026-05-21 Patch Addendum — Runtime/AIBrain SQLite thread-safety

### Why the patch was needed
Runtime dispatched AI decisioning via `asyncio.to_thread`, but decision persistence used a shared SQLAlchemy `Session`, triggering SQLite thread-affinity failures.

### Root cause
AIBrain `_persist_decision` wrote using `self.session` regardless of calling thread, violating SQLite constraint that connection-bound objects stay on creating thread.

### Files changed
- `src/alphaforge/runtime.py`
- `src/alphaforge/ai_brain.py`
- `src/alphaforge/persistence.py`
- `tests/test_runtime.py`
- `VERSION.md`
- `CHANGELOG.md`
- `REPORT.md`

### Runtime behavior changes
- Removed `asyncio.to_thread` wrapping around runtime decision call (`before_real_order`).
- Added session-per-operation persistence path in AIBrain when `session_factory` is supplied.

### Persistence impact
- `_persist_decision` now opens a short-lived session, commits/rolls back, and closes it when using `session_factory`.
- Backward compatibility preserved for existing injected-session usage.

### Tests added
- `test_ai_brain_persistence_uses_short_lived_sessions_across_to_thread`

### Tests executed
- `pytest -q`

### Risks / limitations
- No threshold, scoring, or reject-gate logic changes.
- LIVE readiness unchanged; this is a thread-safety and persistence-correctness patch.



## 2026-05-20 Phase 6.1 Audit-trail canonicalization

### Why changes were needed
Runtime, persistence, and export paths still emitted mixed lifecycle vocabularies (`ENTRY_PENDING`/`ENTRY_SUBMITTED` etc.) and had partially silent persistence failure behavior. This undermined a single audit-truth contract across PAPER/BACKTEST/persistence rows.

### Lifecycle behavior before / after
- **Before:** accepted PAPER runtime emitted extended runtime states (`ENTRY_PENDING`, `ENTRY_SUBMITTED`, `ENTRY_ACKNOWLEDGED`, ...), while backtest/export paths centered on canonical order lifecycle names.
- **After:** accepted PAPER runtime now emits canonical progression: `SIGNAL_CREATED -> WAITING_ENTRY_ZONE -> ENTRY_TRIGGERED -> ORDER_PLACED` then `POSITION_OPENED` on fills. Rejected PAPER/runtime risk gates emit `SIGNAL_CREATED -> SIGNAL_REJECTED` deterministically.

### Persistence behavior before / after
- **Before:** helper writes could throw/short-circuit depending on schema differences and could be effectively placeholder-like in edge schemas.
- **After:** `save_order_decision` and `save_trade_lifecycle_event` perform durable insert attempts and fail closed (`None`/`False`) on SQL errors, enabling runtime detection. Runtime lifecycle persistence callback now raises when lifecycle persistence fails (detectable fail-closed preparation for LIVE hardening).

### Runtime impact
- Canonical lifecycle ordering is now explicit in PAPER accept/reject paths and tests.
- Reconciliation flow remains intact; timeout-like execution now uses canonical `ENTRY_TIMEOUT` before reconciliation escalation.

### Compatibility / migration / schema implications
- SQLite compatibility preserved; no destructive migration added.
- Existing extended lifecycle event support in `contracts.py` is retained for compatibility while canonical states are now preferred for core audit flow.
- Persistence helpers continue to tolerate optional columns/tables by returning failure state instead of crashing entire run path.

### Tests added/updated
- Added PAPER lifecycle sequence tests for accepted canonical flow and reject ordering.
- Updated runtime tests to assert `ORDER_PLACED` emission and `SIGNAL_CREATED` first semantics.
- Full suite passing (`177 passed`).

### Remaining blockers
- Full LIVE fail-closed exchange execution wiring remains out of scope (still blocked).
- Some non-core extended lifecycle states remain in reconciliation/ops channels for incident observability and must be converged in future phases if full canonical-only contract is required.
## 2026-05-20 Patch Addendum — SQLite additive schema bootstrap hardening

### Why the patch was needed
- Runtime/backtest persistence on existing SQLite files failed because table schemas lagged behind current write paths.
- `CREATE TABLE IF NOT EXISTS` did not modify existing tables, so additive columns (`order_decisions.phase`, `ai_decision_features.decision_id`, etc.) remained missing.

### Root cause
- Schema evolution introduced new columns without an idempotent additive migration pass for pre-existing SQLite DB files.

### Affected tables
- `order_decisions`
- `ai_decision_features`
- `trade_lifecycle_events`
- `closed_trade_reviews`
- `schema_migrations`

### Files changed
- `src/alphaforge/persistence.py`
- `src/alphaforge/persistence.py`
- `tests/test_sqlite_schema_bootstrap.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Added migrations/bootstrap behavior
- Added SQLite helpers for table-existence checks, column introspection, and additive per-column migration.
- `init_db()` now runs idempotent SQLite runtime schema repair after base table creation.
- Migration logs emitted when columns are added.

### Why create_all()/CREATE TABLE IF NOT EXISTS was insufficient
- SQLite `CREATE TABLE IF NOT EXISTS` only creates missing tables; it does not reconcile missing columns on existing tables.

### Test coverage
- Legacy `order_decisions` schema repaired and write-path verified.
- Legacy `ai_decision_features` schema repaired and write-path verified.
- Double `init_db()` idempotency and data preservation verified.

### Threshold/regression confirmation
- No changes to score thresholds, RR gates, spread/slippage limits, reject logic, or AI decision semantics.

### Push recommendation
- Safe to merge as additive, SQL-first backward-compatibility hardening for persistence stability.

## 2026-05-20 Patch Addendum — Runtime bootstrap smoke scanner + execution mode default

### Why the patch was needed
- Runtime bootstrap scanner returned `[]`, so startup wiring could not exercise symbol selection, AI decisioning, lifecycle emission, or persistence callbacks.
- Runtime startup defaulted to BACKTEST when `EXECUTION_MODE` was absent, which is unsafe for expected operator posture.

### Root cause
- `_safe_market_scanner` was implemented as an empty no-op list.
- `execution_mode_from_env(None)` and `RuntimeConfig.execution_mode` defaulted to `BACKTEST`.

### Files changed
- `src/alphaforge/runtime.py`
- `tests/test_runtime.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- Bootstrap scanner now returns one deterministic local-only smoke-test candidate with required selector/risk/AI fields.
- Runtime mode resolution now uses `EXECUTION_MODE` with default PAPER semantics.
- No exchange connectivity added; no real order submission path added.

### Lifecycle/persistence impact
- Startup smoke flow now can generate lifecycle/reject persistence artifacts via existing callbacks.
- Lifecycle contract and transition logic unchanged.

### Export/schema impact
- None.

### Tests added
- None.

### Tests executed
- `python -m compileall src/alphaforge/runtime.py`
- `python -m pytest tests -q`

### Risks
- Minimal: deterministic smoke candidate could be unexpectedly accepted/rejected depending on environment thresholds, but remains local-only and non-exchange.

### Remaining limitations
- Scanner is explicitly bootstrap smoke-only, not a live market scanner.

### Migration concerns
- None.

### Push recommendation
- Safe to merge as runtime bootstrap safety/alignment fix.

# AlphaForge Forensic Audit Report — Backtest Lifecycle Behavior (2026-05-19)

## 2026-05-19 Patch Addendum — Remaining pytest failures (targeted hotfix)

### Why the patch was needed
- Remaining backtest scanner failures showed spread-unit inconsistency in symbol gating and calibration snapshot insert schema mismatch (`payload_json` absent on current SQLite table).
- A constructor compatibility regression required optional defaults for `ForwardWindowEvaluation` in idempotency tests.

### Root cause
- `select_symbol(...)` treated spread thresholds with stale percent-point configuration (`0.12`) and scoring shape that let `0.0035` pass as strong spread.
- Backtest summary calibration insert expected `payload_json` column although in-memory initialized schema did not guarantee it.
- `ForwardWindowEvaluation` required fields not always supplied by test fixtures intended to validate persistence/idempotency semantics.

### Files changed
- `src/alphaforge/symbol_selector.py`
- `backtest_order.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- No production risk filter loosening: spread gate is stricter and unit-correct (`max_spread_pct=0.0025` as fraction).
- Calibration snapshot export insert is schema-compatible across current table variants (no hard dependency on `payload_json`).

### Lifecycle/persistence impact
- Lifecycle persistence remains SQL-backed and deterministic; event ID uniqueness behavior is unchanged.
- Effective RR precedence in lifecycle persistence remains `row.effective_rr` fallback to `row.rr`.

### Tests executed
- `python -m pytest tests/test_backtest_order_scanner.py -q`
- `python -m pytest -q`

### Risks / limitations
- This is a localized fix; no architectural rewrite.
- LIVE readiness remains unchanged and not recommended.

### Push recommendation
- Safe to merge as a defensive consistency fix with preserved reject rigor.

## Executive Summary

AlphaForge is **not failing because it generates zero signals**; it is failing because the current backtest signal stream is mostly low-quality, heavily long-biased, and then aggressively filtered by intentionally strict quality/execution gates. The observed lifecycle pattern (`SYMBOL_REJECTED` + `SIGNAL_REJECTED` + a very small `ORDER_REJECTED`, zero placed trades) is consistent with code behavior.

Primary root causes:
1. **Candidate generation is structurally long-only in backtest path** (`side="LONG"`, `BREAKOUT_UP` defaults).
2. **Score thresholding is intentionally high** (base `min_score=7.5`) versus observed score distribution centered around ~3–4.
3. **Symbol regime filters are strict and front-load rejections** (`TOO_CHOPPY`, `WEAK_TREND_AND_NO_RANGE_EDGE`).
4. **Execution penalty model can materially compress effective RR**, and a separate backtest-only execution penalty path exists with different formula than runtime real-order path.
5. **Order geometry (SL width) is fragile for late-breakout candles**, causing `STOP_TOO_WIDE` in survivors.

Reject engine appears **mostly directionally correct** (high rejected-loss share aligns with defensive objective), but calibration and unit consistency likely require tightening.

---

## System Flow

### Actual Decision Pipeline (Backtest)
1. Universe/symbol data loaded and scored by symbol selector.
2. Symbol-level rejects can emit `SYMBOL_REJECTED`.
3. For scannable bars, `_build_market_ctx(...)` creates candidate fields (entry/sl/tp/rr/score/regime/side).
4. `run_order_cycle(...)` in shared order runtime does:
   - `build_order_candidate(...)`
   - `evaluate_trade_quality(...)`
   - if accepted, `execute_order_candidate(...)`
5. Backtest script maps decisions into lifecycle rows and persists/exports.

### Lifecycle transitions currently seen in your extraction
Your extracted states match the early-gate flow where most candidates die before execution:
- `SYMBOL_REJECTED`
- `SIGNAL_CREATED`
- `SIGNAL_REJECTED`
- `ORDER_REJECTED` (for execution/effective-RR rejections after initial signal quality acceptance)

This is coherent with the gating architecture and with very low pass-through.

---

## Signal Generation Audit

### Where candidates are generated
- `backtest_order.py`
  - `_build_market_ctx(...)`
  - `scan_symbol_backtest(...)`

### Why many candidates are low quality
- Backtest score formula is heuristic and momentum-candle biased:
  - `score = clamp(3.0 + breakout_strength*500 + range_pct, 0..10)`
- Many bars will cluster in mid/low scores unless breakout extension is significant.
- Quality gate baseline is high (`MIN_SCORE_BASE=7.5`) in shared order engine.

### Why SHORT candidates are absent
- `_build_market_ctx(...)` hardcodes:
  - `setup_type="BREAKOUT_UP"`
  - `setup_reason="CLOSE_ABOVE_PREV_HIGH"`
  - `side="LONG"`
- No mirrored bearish builder is invoked in this path.

Conclusion: absence of SHORTs is architectural in the current backtest candidate builder, not merely a logging artifact.

---

## Score System Audit

### Where score is calculated
- Backtest candidate score: `backtest_order.py::_build_market_ctx(...)`
- Trade gate thresholding: `src/alphaforge/order.py::evaluate_trade_quality(...)`
- Adaptive threshold source: `src/alphaforge/order.py::compute_adaptive_thresholds(...)`

### Dominant scoring features
- Breakout extension ratio (`close > prev_high`) and candle range.
- This rewards **impulse intensity**, not necessarily executable expectancy after cost.

### Why score may not correlate strongly with shadow TP outcomes
- Score is not calibrated to forward realized outcomes in the same function.
- Execution/geometry frictions are evaluated later by separate gates.
- A strong momentum candle can score high while simultaneously implying bad SL geometry or poor effective RR after costs.

### Is ~7.5 threshold intentional?
Yes. `MIN_SCORE_BASE = 7.5` and adaptive logic shifts around that baseline.

---

## Regime Engine Audit

### Where TOO_CHOPPY / WEAK_TREND_AND_NO_RANGE_EDGE are enforced
- `src/alphaforge/symbol_selector.py::select_symbol(...)`
  - `TOO_CHOPPY` when `chop_score > max_chop_score`
  - `WEAK_TREND_AND_NO_RANGE_EDGE` when neither clean trend nor range-edge condition holds

### Strictness assessment
Given your distribution, filters are probably functioning as designed (defensive posture), but may be over-conservative combined with long-only breakout sourcing.

### Why REGIME_MISMATCH can show high TP yet still be blocked
- Regime compatibility checks in `evaluate_trade_quality(...)` are categorical and structural.
- Some mismatched setups can still hit TP in raw terms, but policy blocks them to avoid unstable regime-transfer behavior.
- If effective expectancy after costs remains negative, rejection remains philosophically consistent.

---

## Execution & Effective RR Audit

## Formulas located

### Backtest-local rejection helper
`backtest_order.py::_execution_reject_flags(rr, market_ctx)`:
- `execution_penalty = (slippage + spread) * 50`
- `effective_rr = max(rr * (1 - execution_penalty), 0)`

### Shared runtime cost model
`src/alphaforge/execution.py::build_execution_cost_model(...)`:
- `spread_penalty = spread_pct * 25`
- `slippage_penalty = expected_slippage_pct * 30`
- `latency_penalty = (latency_ms/1000) * 0.2`
- `funding_penalty = abs(funding_rate_pct) * 2.5`
- `liquidity_penalty = (1 - liquidity_score) * 0.6`
- `total_penalty = sum(above)`

`src/alphaforge/order.py::_effective_rr(...)`:
- `effective_rr = max(raw_rr - total_penalty, 0)`

### Key architectural finding
There are **two different effective-RR formulations** in the codebase (multiplicative backtest helper vs additive runtime model). This can create calibration mismatch in diagnostics and rejection interpretation.

### Unit consistency concern (spread_pct meaning)
- `_spread_pct_from_prices` returns fraction: `(ask-bid)/mid`.
- Backtest estimator `_estimate_backtest_spread_pct` returns values like `0.015` baseline.
- In these formulas, `0.015` behaves like **1.5% (fraction)**, not 0.015%.

If your external interpretation assumed percent points (0.015%), penalties will look unexpectedly harsh. Current code treats spread/slippage as fractional rates.

### Is effective_rr collapse legitimate or buggy?
Likely **combination**:
- Partly legitimate under conservative penalties and long-breakout timing.
- Partly suspicious if any pipeline provides spread/slippage in percent units while formulas expect fractional units.
- Existence of dual formulas increases risk of inconsistent collapse behavior.

---

## Order Geometry Audit

### Where stop distance and RR are built
- `backtest_order.py::_build_market_ctx(...)`
  - `sl = min(now.low, prev.low)`
  - `risk = entry - sl`
  - `rr` is heuristic, then `tp = entry + rr*risk`
- Stop-width gate in `evaluate_trade_quality(...)`:
  - `sl_pct = abs(entry-sl)/entry*100`
  - reject if `sl_pct > MAX_SL_PCT` (default 1.5)

### Why high-score candidates fail STOP_TOO_WIDE
Late breakout bars can widen structural SL distance quickly. Score can be high from impulse strength while SL% breaches cap.

### Could retry shaping help?
Yes, minimally:
- add bounded order-shaping retries (e.g., entry pullback bands, capped SL relocation, adaptive TP rebalance) **before** terminal reject.
- keep fail-closed final gate unchanged.

---

## Backtest Architecture Audit (vs PAPER/LIVE)

- Backtest uses shared `run_order_cycle(...)` quality gate path (good alignment).
- But backtest script still has extra local mechanics and evaluation helpers not identical to live order path.
- Effective RR math divergence (noted above) is a material alignment risk.
- Lifecycle persistence exists and is richer than earlier versions, but current observed run indicates early-stage-only transitions due to no accepted orders.

Assessment: architecture is partially aligned, but **not fully unified** in execution-penalty semantics and candidate construction realism.

---

## Lifecycle Architecture Trace

### Declared lifecycle model
Shared lifecycle enum/contract includes:
- `SIGNAL_CREATED` → `WAITING_ENTRY_ZONE` → `ENTRY_TRIGGERED` → `ORDER_PLACED` → close states
- plus reject/cancel/error states.

### Observed-only subset explanation
Because all candidates fail before order survival, only early reject states appear. Missing downstream states are a consequence of gate outcomes, not necessarily missing enum definitions.

### Potential bypass/missing practical states in this run
- No `WAITING_ENTRY_ZONE`/`ORDER_PLACED` terminal trades observed due to zero acceptances.
- Backtest realism for partial fills/advanced execution remains simplified relative to live complexity.

---

## Expectancy System Audit

### Where expectancy bucket is calculated
- `backtest_order.py::_bucket_expectancy(...)`
- Candidate expectancy in `_build_market_ctx(...)`: `((score/10)-0.5)*(rr-1.0)`

### Connection quality
- Bucket is persisted/propagated through lifecycle rows.
- But score formula and expectancy formula are tightly coupled heuristics, not empirically calibrated to realized forward bins in this module.

Conclusion: wiring exists; calibration linkage to realized expectancy is weak.

---

## Root Cause Matrix

| Symptom | Root cause | Severity | Confidence | Impacted files/functions |
|---|---|---:|---:|---|
| No SHORT candidates | Backtest builder hardcodes LONG/bullish setup | High | High | `backtest_order.py::_build_market_ctx` |
| Massive LOW_SCORE rejects | High min score (7.5+) vs low-mid heuristic score distribution | High | High | `src/alphaforge/order.py::compute_adaptive_thresholds`, `evaluate_trade_quality`; `backtest_order.py::_build_market_ctx` |
| Survivors fail STOP_TOO_WIDE | Breakout entries + structural SL from bar lows exceed 1.5% cap | Medium-High | High | `backtest_order.py::_build_market_ctx`; `src/alphaforge/order.py::evaluate_trade_quality` |
| Effective RR compressed heavily | Conservative penalties + possible unit interpretation mismatch + dual formulas | High | Medium-High | `src/alphaforge/execution.py::build_execution_cost_model`; `src/alphaforge/order.py::_effective_rr`; `backtest_order.py::_execution_reject_flags` |
| REGIME_MISMATCH still has TP winners | Categorical regime gate blocks structurally even when some raw winners occur | Medium | Medium | `src/alphaforge/order.py::evaluate_trade_quality`; `src/alphaforge/symbol_selector.py` |
| Backtest/PAPER/LIVE not perfectly aligned | Shared gate exists, but auxiliary backtest logic diverges in places | Medium | Medium | `backtest_order.py`; `src/alphaforge/order.py` |

---

## Recommended Minimal Fixes (No Architecture Rewrite)

1. **Add mirrored SHORT candidate builder** in backtest path with bearish breakout/pullback logic parity.
2. **Unify effective-RR formula usage** (single source of truth from `build_execution_cost_model`).
3. **Add explicit unit contract checks** for spread/slippage inputs (fraction vs percent).
4. **Introduce bounded order-shaping retry** before `STOP_TOO_WIDE` final reject.
5. **Instrument score-to-outcome calibration reports** without lowering defensive rejects blindly.
6. **Keep reject protections on**, but expose gate attribution and distributions by regime/setup/side.

---

## Recommended Diagnostics

Add metrics/logging/persistence fields:
- `score_rank_pct`, `score_decile`, `raw_rr`, `effective_rr`, `cost_penalty_total`, and decomposed penalties.
- `spread_unit_assumed` (`fraction`/`percent_points`) and raw source fields.
- `first_blocking_gate`, `all_failed_gates`, `regime_ok`, `sl_pct`.
- Side coverage metrics: long/short candidate counts pre/post each gate.
- Calibration outputs: TP/SL/timeout rates by score decile, regime, setup, side.

---

## Tests To Add

1. **SHORT generation tests**
   - assert both LONG and SHORT candidate creation under mirrored market patterns.
2. **Score variability + calibration tests**
   - verify score distribution is non-degenerate and monotonicity vs outcome isn’t inverted.
3. **Effective RR unit sanity tests**
   - explicit cases for spread/slippage in fraction vs percent-point inputs.
4. **Formula alignment tests**
   - ensure backtest effective-RR diagnostics match shared runtime model.
5. **Lifecycle transition tests**
   - validate complete path coverage and state legality under accepted/rejected branches.
6. **Rejected shadow export tests**
   - verify reject reason + shadow outcome + penalty decomposition persistence.
7. **Expectancy calibration tests**
   - bucket assignment consistency and realized expectancy drift alerts.

---

## Final Assessment

AlphaForge’s current backtest underperformance is a **combination problem**:
- It is **finding many weak candidates** (and mostly only LONG-type candidates).
- It is **correctly rejecting most of them** under defensive policy.
- A small set of stronger candidates then often fail **geometry + effective-RR** gates.
- Execution penalties may be **partly too harsh or inconsistently interpreted** due to unit/formula ambiguity.
- Score is **not sufficiently calibrated** to executable post-cost expectancy.

So the dominant failure mode is not a single bug; it is: **long-only candidate construction + scoring/calibration mismatch + strict execution/geometry gating, with potential penalty unit inconsistency amplifying final rejection.**


## Patch Update — 2026-05-19

- Implemented minimal mirrored SHORT candidate construction in backtest market-context builder.
- Aligned backtest execution reject effective-RR calculation to shared additive execution-cost model.
- Added spread/slippage unit normalization and explicit unit-assumption fields for diagnostics/export.
- Added regression tests for SHORT candidate emission and percent-point spread normalization behavior.
# PAPER Runtime Persistence and SQLite Bootstrap Investigation (2026-05-19)

## Root Cause Summary
- Tables were not visible primarily because PAPER runtime can point at an unexpected SQLite target (`:memory:` default or non-resolved relative path) while SQLTools inspected a different file.
- Runtime bootstrap already called `init_db(...)` (which issues `CREATE TABLE IF NOT EXISTS`), but startup lacked fail-fast logging to prove path/schema/table state.
- Runtime heartbeat counters stayed at zero because the default runtime scanner returns an empty candidate list, so symbol selection and decision generation never progressed.
- Prior runtime bootstrap did not wire reject/lifecycle callbacks to persistence, so runtime-generated reject/lifecycle data was not persisted by default even if events occurred.

## Exact Files / Functions Investigated
- `src/alphaforge/runtime.py`
  - `_build_runtime_from_env()`
  - `_scan_once()`
  - `_heartbeat_loop()`
- `src/alphaforge/persistence.py`
  - `init_db()`
  - `_apply_sqlite_migrations()`
  - `save_order_decision()`
  - `save_trade_lifecycle_event()`
- `src/alphaforge/symbol_selector.py`
  - `select_symbols()` / `select_symbol()`

## Why No Tables Appeared
- Schema creation function exists and is mode-agnostic: `init_db(...)` always executes DDL list with `CREATE TABLE IF NOT EXISTS`.
- It is invoked during runtime bootstrap (`_build_runtime_from_env`) before orchestrator starts.
- Empty decision flow does NOT skip schema init.
- Practical failure mode was observability/path mismatch: no explicit absolute DB path and no post-init table logging, making SQLTools likely pointed at a different DB file.

## Why PAPER Decisions Were Not Generated
- Runtime env bootstrap scanner currently returns `[]` in `_safe_market_scanner`; therefore:
  - `symbols_selected=0`
  - `decisions_generated=0`
  - `rejects_persisted=0`
  - `lifecycle_events=0`
- This is fail-closed and expected with no market candidates; not a permissiveness bug.

## Determination (a–e)
- (a) **Yes**: PAPER was not selecting symbols (`symbols_selected=0`) due to empty candidate list.
- (b) N/A in observed env bootstrap path (no symbols selected).
- (c) Previously possible in runtime path because callbacks were not wired by default; now fixed in bootstrap wiring.
- (d) SQL persistence was partially skipped for runtime events pre-patch (no default reject/lifecycle callbacks); now enabled when `ALPHAFORGE_PERSISTENCE_ENABLED=true`.
- (e) **Likely contributing factor**: SQLite path mismatch (relative/in-memory vs SQLTools target) due to missing absolute-path diagnostics; now fixed with explicit resolved path logging.

## BACKTEST vs PAPER/LIVE Persistence Comparison
- Table creation: all modes via shared `init_db(...)` when bootstrapped through runtime env path.
- Lifecycle writes: available via runtime `on_lifecycle_event` callback; now wired in bootstrap for runtime modes.
- Reject writes: available via runtime `on_reject_persist` callback; now wired in bootstrap for runtime modes.
- Orders/executions: execution counters/lifecycle emit in runtime; order decision persistence is callback-dependent and now wired in bootstrap path.
- Rejected decision consistency: improved by default callback wiring; remains dependent on runtime producing rejections.

## Patch Plan Executed
1. Add deterministic startup diagnostics for DB URL/path/schema/tables and persistence flag.
2. Ensure runtime bootstrap wires lifecycle/reject callbacks to SQLite persistence functions.
3. Add zero-selection diagnostics and gate blockers in scan + heartbeat.
4. Add tests for bootstrap schema/path/zero-selection diagnostics.

## Code Changes Made
- Implemented runtime bootstrap logging: configured DB URL, resolved absolute DB URL, schema init success, discovered table names.
- Added `persistence_enabled` metric and heartbeat surfacing.
- Added scan-time reject reason aggregation and explicit gate blockers for `NO_MARKET_CANDIDATES` and `NO_TRADABLE_SYMBOLS_AFTER_SELECTION`.
- Wired runtime bootstrap callbacks to `save_trade_lifecycle_event(...)` and `save_order_decision(...)`, guarded by `ALPHAFORGE_PERSISTENCE_ENABLED`.

## Tests Added
- PAPER bootstrap creates key schema tables even with empty decision cycle.
- Runtime bootstrap logs absolute SQLite DB path.
- Zero-selected-symbol scan records explicit gate blocker + rejection summary.

## Remaining Risks
- Default env scanner still returns no candidates; runtime remains inert unless real scanner/universe feed is wired.
- Persistence callbacks now wired, but successful rows still depend on runtime generating lifecycle/reject events.
- SQLTools/operator must verify they open the same resolved DB path logged by runtime.

## 2026-05-19 Rejected Shadow + Reject Gate Audit Patch

### Why this patch was needed
- Rejected-shadow analysis surfaced potential LOW_SCORE score-scale confusion and limited visibility into reason-level missed-opportunity structure.
- STOP_TOO_WIDE rejects showed non-trivial hypothetical TP opportunities that required bounded rescue diagnostics rather than global gate loosening.

### Root cause
- Exported reject rows lacked explicit gate-score provenance fields.
- Rejected-shadow summary was aggregate-only and not grouped with per-reason profitability/cost structure.
- Spread unit normalization was not consistently enforced for all market-data ingestion paths.

### Files changed
- `backtest_order.py`
- `tests/test_backtest_order_scanner.py`
- `CHANGELOG.md`
- `VERSION.md`
- `REPORT.md`

### Runtime behavior changes
- Added gate-score observability fields to rejected exports and shadow exports.
- Added STOP_TOO_WIDE rescue simulation diagnostics with bounded size reduction and post-cost effective-RR recomputation.
- Added grouped reject-reason shadow diagnostics.

### Persistence / export changes
- `rejected_orders.csv`: adds `gate_score`.
- `rejected_shadow.csv`: adds `low_score_gate_score` and rescue telemetry fields.
- `rejected_shadow_summary.csv`: adds `reject_reason_diagnostics` JSON payload.

### Risks / limitations
- Rescue path is diagnostic-only and intentionally conservative; it does not auto-accept trades.
- Top symbol/regime outputs are frequency-based and do not imply production allocation guidance.

## Dev Branch Design Compliance Audit (2026-05-20)

### Current status
- **Overall:** PARTIAL compliance. Core execution-aware components and persistence exist, but full shared signal-to-order contract parity across BACKTEST/PAPER/LIVE is incomplete.

### What works
- BACKTEST uses shared `run_order_cycle(...)` for candidate quality gating before simulation/execution lifecycle expansion.
- Execution-cost model computes additive penalties (spread, slippage, latency, funding, liquidity) and effective RR, with explicit missing-field handling.
- Rejected decisions/lifecycle events persist with reject reasons and execution-context flags/sentinels.
- Runtime has explicit pre-trade risk gates and lifecycle persistence paths.

### What failed / gaps found
- Runtime path still primarily uses `ai_brain.before_real_order(...)` and does not exclusively use the same `run_order_cycle(...)` decision path used in backtest.
- Naming/contract mismatch versus target contract (`SignalCandidate`, `ProbabilityDecision`, `evaluate_signal_to_order(...)`) remains partially semantic rather than exact API parity.
- Regime vocabulary support is partial; not all requested regime labels are first-class states in decision gates.

### Exact files/functions inspected
- `backtest_order.py`: `scan_symbol_backtest`, `simulate_candidate`, `process_backtest_result`, `_execution_reject_flags`.
- `src/alphaforge/order.py`: `run_order_cycle`, `build_order_candidate`, `evaluate_trade_quality`, `_effective_rr`.
- `src/alphaforge/runtime.py`: `_scan_once`, `_process_symbol`, `_execute`.
- `src/alphaforge/execution.py`: `build_execution_context`, `build_execution_cost_model`, `normalize_pct_input`.
- `src/alphaforge/persistence.py`: order/lifecycle persistence helpers and schema fields used by tests.

### Patches applied
- Fixed backtest lifecycle progression regression in `simulate_candidate(...)` that removed `WAITING_ENTRY_ZONE` from emitted state sequence.
  - Removed accidental overwrite forcing first lifecycle row from `SIGNAL_CREATED -> WAITING_ENTRY_ZONE` back to `SIGNAL_CREATED -> SIGNAL_CREATED`.

### Remaining risks
- Shared decision API parity is still architectural-partial across runtime vs backtest.
- Probabilistic fields exist in AI decision flow, but order-runtime gate remains primarily heuristic-threshold based.
- Regime mapping breadth is limited relative to requested taxonomy.

### Tests run
- `pytest -q`

### Test results
- **Before patch:** 1 failing test (`test_backtest_lifecycle_does_not_start_directly_at_created`).
- **After patch:** full suite passing.

### Known limitations
- This patch intentionally avoids large architecture rewrites to preserve safety and existing runtime behavior.
- No live-exchange dependency was added to backtest paths.

### Next recommended generation
1. Introduce explicit shared contract types (`SignalCandidate`, `ProbabilityDecision`) and a canonical `evaluate_signal_to_order(...)` API in `src/alphaforge/order.py`.
2. Route runtime `_process_symbol` through that shared evaluator pre-AI execution planning, preserving execution-mode-specific adapters.
3. Add parity tests proving BACKTEST and PAPER/LIVE use the same evaluator and reject-reason taxonomy.

## 2026-05-20 Patch Addendum — Backtest lifecycle/persistence/reporting defect fix

### Why the patch was needed
- Backtest lifecycle persistence could violate a deployed unique key `(signal_id,event_ts,lifecycle_state)`.
- Summary counters under-reported orders despite `ORDER_PLACED` lifecycle rows.
- Lifecycle CSV ordering could be nondeterministic under timestamp ties.

### Root cause
- Upsert conflict target was tied to `event_id` only.
- Summary counters used WAITING/timeout counts rather than unique triggered/placed lifecycle keys.
- Export query sorted only by timestamp/event id.

### Files changed
- `src/alphaforge/persistence.py`
- `backtest_order.py`
- `tests/test_phase123_foundations.py`
- `tests/test_backtest_order_scanner.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- `save_trade_lifecycle_event(...)` now prefers upsert by `(signal_id,event_ts,lifecycle_state)` and falls back to `event_id` compatibility path.
- Backtest summary now computes:
  - `total_orders` from unique `ORDER_PLACED` keys
  - `triggered_orders` from unique `ENTRY_TRIGGERED` keys
  - `not_triggered_orders` from WAITING keys that never trigger/place
- Lifecycle export ordering is stable by `event_ts, symbol, signal_id, lifecycle_seq, lifecycle_state, event_id`.
- LOW_SCORE rescue/watch fields are exported as diagnostics-only and do not alter accepted/order/PnL metrics.

### Lifecycle / persistence / schema impact
- No schema loosening and no constraint removal.
- Idempotent lifecycle replay now supports both uniqueness layouts (`event_id` and composite lifecycle key).

### Tests executed
- `pytest -q` (pass).
- Offline backtest smoke + CSV assertions for duplicate IDs, ordering semantics, WAITING-before-trigger, and summary count reconciliation.

### Threshold stance
- Global score threshold and scoring model were **not loosened or changed**.

---

## 2026-05-21 Patch Addendum — PR #114 merge conflict resolution (Phase 6.1 canonicalization)

### Why the patch was needed
- PR #114 required conflict-focused reconciliation with current dev behavior while preserving the Phase 6.1 lifecycle/persistence contract.

### Files changed
- `src/alphaforge/runtime.py`
- `src/alphaforge/persistence.py`
- `tests/test_runtime.py`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime/lifecycle changes
- PAPER accepted flow now emits canonical pre-execution states: `SIGNAL_CREATED -> WAITING_ENTRY_ZONE -> ENTRY_TRIGGERED -> ORDER_PLACED`.
- Rejected path remains `SIGNAL_CREATED -> SIGNAL_REJECTED`.
- Runtime lifecycle persistence callback now fails closed if lifecycle SQL persistence returns failure.

### Persistence changes
- `save_order_decision(...)` now catches SQL/commit failures and returns explicit failure (`None`).
- `save_trade_lifecycle_event(...)` now returns explicit `False` if both upsert strategies fail or commit fails.

### Tests added/executed
- Added runtime tests for PAPER canonical lifecycle sequence and lifecycle persistence failure detectability.
- Executed:
  - `python -m py_compile src/alphaforge/runtime.py src/alphaforge/order.py src/alphaforge/ai_brain.py src/alphaforge/persistence.py backtest_order.py`
  - `pytest -q`


## 2026-05-21 Patch Addendum — pytest compatibility fixes (persistence + lifecycle)

### Why the patch was needed
- Current persistence helper/API behavior diverged from legacy tests/contracts (`fetch_expectancy_stat` shape and legacy compatibility columns).
- Backtest accepted lifecycle progression could transition from `SIGNAL_ACCEPTED` directly to `ENTRY_TRIGGERED`.

### Root cause
- `fetch_expectancy_stat` had been broadened to metadata dict output rather than preserving scalar legacy return contract.
- SQLite bootstrap did not consistently guarantee all legacy compatibility columns across existing DBs.
- `simulate_candidate(...)` emitted `ENTRY_TRIGGERED` with `status_before='SIGNAL_ACCEPTED'` instead of waiting-state continuity.

### Files changed
- `src/alphaforge/persistence.py`
- `backtest_order.py`
- `VERSION.md`
- `CHANGELOG.md`
- `REPORT.md`

### Behavior changes
- Restored `fetch_expectancy_stat(...) -> float | None` semantics and added `fetch_expectancy_stat_detail(...)` for detailed exports/metadata callers.
- Added idempotent schema repair coverage for legacy compatibility columns in `order_decisions` and `trade_lifecycle_events`.
- `save_order_decision(...)` now mirrors serialized payload into compatibility `payload` column and preserves rejected payload details.
- `save_trade_lifecycle_event(...)` now populates compatibility `trade_id/state/payload` and returns inserted/upserted row id.
- Backtest accepted lifecycle now emits `WAITING_ENTRY_ZONE` before `ENTRY_TRIGGERED` in market/limit trigger paths.

### Threshold/regression confirmation
- No score thresholds changed.
- No reject/accept logic changed.
- No scoring logic changes.

### Tests executed
- `pytest -q tests/test_persistence_fetch_expectancy.py`
- `pytest -q tests/test_persistence_patch1.py`
- `pytest -q tests/test_phase123_foundations.py::test_backtest_lifecycle_does_not_start_directly_at_created`
- `pytest -q`

## 2026-05-21 Patch
Root cause: runtime/exchange/backtest parsed env independently with hardcoded defaults.
Changes: introduced centralized config loading and rewired runtime/exchange/backtest defaults.
Tests: pytest -q tests/test_config_layer.py tests/test_runtime_env_config.py tests/test_exchange_connectivity.py


## 2026-05-21 Patch Addendum — Runtime/env failing-test triage (stability verification only)

### Why the patch was needed
- Reported post-pull failures targeted runtime env aliasing, runtime DB path/bootstrap behavior, PAPER rejected-row persistence semantics, and adaptive-learning stats counts.

### Root cause
- No deterministic code defect reproduced on current branch.
- The previously observed `assert 941 == 60` symptom in adaptive stats is consistent with non-isolated/stale DB data contamination rather than scoring/threshold logic drift.
- Current runtime env alias + DB resolution tests pass and indicate canonical/alias precedence and absolute path logging behavior are intact.

### Files changed
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- None (verification-only documentation update).

### Lifecycle/persistence/schema impact
- None.

### Tests executed
- `pytest tests/test_adaptive_learning_foundation.py::test_adaptive_stats_and_shadow_thresholds -vv --tb=long`
- `pytest tests/test_runtime.py::test_runtime_module_bootstrap_builds_from_env -vv --tb=long`
- `pytest tests/test_runtime.py::test_runtime_logs_absolute_db_path -vv --tb=long`
- `pytest tests/test_runtime.py::test_paper_runtime_rejected_rows_use_paper_mode_and_single_final_count -vv --tb=long`
- `pytest tests/test_runtime_env_config.py::test_runtime_env_aliases_for_threshold_and_positions -vv --tb=long`
- `pytest tests/test_runtime_env_config.py -q`
- `pytest tests/test_adaptive_learning_foundation.py -q`
- `pytest tests/test_runtime.py -q`
- `pytest -q`

### Risks / limitations
- The specific missing test node (`test_runtime_rejected_decisions_do_not_persist_incomplete_rows`) no longer exists under that name, implying test rename/removal drift between failure report and current branch.
- Intermittent failures can still recur if external env vars or persistent sqlite files leak across test runs in non-isolated environments.

### Push recommendation
- Safe to merge as audit/traceability documentation update; no behavioral/runtime code change included.

## Patch 2026-05-22
- Runtime/backtest path now uses deterministic historical Binance Futures replay data with explicit source labeling.
- Added cache metadata coverage validation and loud failures for incomplete historical coverage.
- Added unit tests for pagination, dedupe, incomplete coverage failures, cache coverage checks, and funding anti-leak joins.

## 2026-05-22 PR #148 follow-up — LIVE qualification mutation bug fix

### Why the patch was needed
- LIVE qualification parity evaluation path was invoking `AIBrain.before_real_order(...)`, which internally persists decision artifacts. This violated the non-mutating LIVE qualification guarantee.

### Root cause
- Qualification probing reused a persistence-capable hook instead of a read-only decision path.
- Qualification sample inputs were partially runtime-derived, preventing strict deterministic replay comparison.

### Files changed
- `src/alphaforge/runtime.py`
- `tests/test_live_readiness_security_regression.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- Added side-effect-free pre-submit evaluator that calls:
  - `score_signal(...)`
  - `choose_order_plan(...)`
  - `explain_decision(...)`
- Added deterministic parity evidence builder with stable qualification sample IDs/timestamps and explicit comparison fields.

### Lifecycle / persistence impact
- LIVE qualification parity checks no longer mutate trading/audit persistence tables.
- No new order submit/amend/cancel/close behavior added.

### Safety posture
- `incident_persistence_verified` remains `False`.
- LIVE remains fail-closed / not live-ready pending alerting, rollback proof, real execution readiness, and protective-order lifecycle proof.

### Tests added
- Non-mutating parity evidence regression (pre/post table row-count invariance, including optional tables when present).
- Deterministic replay assertion for two parity evidence builds (equal after excluding `generated_at`, including identical sample IDs).

### Tests executed
- `pytest -q tests/test_live_readiness_security_regression.py tests/test_live_readiness.py tests/test_runtime.py`

### Risks / limitations
- Parity evidence now depends on AIBrain public scoring/planning/explanation interfaces being available and behaviorally stable.
- Deterministic fixture set is intentionally narrow (qualification evidence, not market-replay realism).

### Push recommendation
- Merge recommended. This closes a P1 qualification mutation bug while preserving deterministic PAPER vs LIVE_PRECHECK parity evidence semantics.

## 2026-05-22 JOB19 V1 — PAPER runtime reject-rate and decision-quality audit diagnostics (audit-only)

### Why this patch was needed
- JOB19 V1 required a lowest-risk audit instrument to inspect PAPER runtime selectivity and persistence quality without changing runtime behavior, thresholds, schema, or lifecycle emission logic.

### Root cause addressed
- There was no single reusable SQL diagnostics bundle in-repo to consistently evaluate PAPER decision/reject quality and lifecycle integrity from a runtime SQLite artifact.

### Files changed
- `sql/diagnostics/job19_paper_reject_rate_decision_quality_audit.sql`
- `REPORT.md`

### Runtime behavior changes
- None. No runtime Python execution path, reject logic, schema DDL, thresholds, score calculation, RR calculation, scanner wiring, or lifecycle emission code was modified.

### What the diagnostic queries prove
Given a real PAPER runtime SQLite DB containing `order_decisions` and `trade_lifecycle_events`, this query pack can prove:
- Total PAPER decisions, rejected/accepted counts, and computed rejection rate.
- Whether rejected rows are missing `reject_reason`.
- Missingness of audit-critical fields (`signal_id`, `symbol`, `decision`, timestamps, score/RR/effective RR, execution context fields).
- Duplicate or inconsistent per-signal decisions.
- Whether score/raw RR/effective RR show variation versus constant-like behavior.
- Execution context presence and `execution_ctx_missing` flag coverage.
- Lifecycle row completeness, unexpected state labels, and per-signal sequence/time ordering anomalies.

### What the diagnostic queries cannot prove (without runtime artifact evidence)
- They cannot issue a real reject-quality verdict in absence of a committed/repository-accessible PAPER runtime SQLite artifact with representative production-like sample size.
- They cannot prove economic expectancy or execution-adjusted profitability; they only measure persistence and decision/lifecycle data characteristics.
- They cannot validate external market realism inputs (spread/slippage/latency/liquidity fidelity) beyond what was persisted.

### Classification framework for JOB19
Use this framework only after executing diagnostics against a real PAPER runtime DB sample:
- `HEALTHY_SELECTIVITY`
  - Rejection rate and reason distribution are plausible, required fields are substantially complete, lifecycle ordering is coherent, and score/RR signals are non-constant.
- `DATA_INTEGRITY_FAILURE`
  - Critical missing fields, duplicated/inconsistent decisions, or lifecycle-state/timestamp integrity failures materially undermine auditability.
- `EXECUTION_CONTEXT_FAILURE`
  - Execution context availability is broadly absent or consistently flagged missing, preventing execution-aware reject-quality interpretation.
- `SCORING_OR_REGIME_PIPELINE_FAILURE`
  - Score/raw RR/effective RR variability collapses (constant/near-constant signatures) or reject reasoning appears structurally disconnected from expected signal diversity.
- `INSUFFICIENT_SAMPLE`
  - Sample too small or too narrow in time/symbol/regime coverage for confident selectivity conclusions.

### Risks / limitations
- Results remain fully artifact-dependent; query outputs should be interpreted with minimum sample-size and regime-diversity checks.
- SQLite dialect assumptions (e.g., `GROUP_CONCAT`) are intentionally used because runtime persistence target is SQLite by default.

### Push recommendation
- Recommend merge as audit-only instrumentation with minimal blast radius.

## JOB-22A (2026-05-24)
- Root cause: final persistence paths defaulted missing execution evidence to optimistic zeros and dropped canonical provenance.
- Fix: propagate one canonical execution_ctx across runtime/AI/final reject persistence and persist NULL for unavailable spread/slippage/latency.
- Remaining blocker: effective_rr is still not execution-cost-adjusted gate (tracked for follow-up).

## 2026-06-21 Patch Addendum — P0-3 TimesFM canonical evidence integration

### Why the patch was needed
- TimesFM decisions were CSV-exportable research outputs, but they lacked a canonical SQL evidence surface for audit, idempotency checks, and later calibration.
- P0-3 required TimesFM to become auditable forecast evidence without bypassing AIBrain, order, reject, or LIVE safety gates.

### Root cause
- `replay_timesfm_backtest` returned decisions and `write_decision_log` exported CSV, but no TimesFM-specific SQL table existed.
- Forward calibration status was not represented in schema, which made calibration gaps less explicit.

### Files changed
- `src/alphaforge/timesfm_futures.py`
- `src/alphaforge/persistence.py`
- `tests/test_timesfm_futures.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- TimesFM replay still accepts only PAPER/BACKTEST modes and still raises/fails closed for LIVE.
- Replay decisions now include a stable `forecast_id`, `mode`, `horizon`, model/provider metadata, and `no_lookahead_input_end_ts`.
- Optional SQL persistence writes TimesFM forecast evidence rows when a persistence session is supplied.
- No AIBrain, order, or execution adapter integration was added.

### Lifecycle changes
- None to production order lifecycle. TimesFM remains research evidence only.
- Invalid forecasts still produce `NO_TRADE` / `INVALID_FORECAST` and do not create orders or lifecycle fills.

### Persistence changes
- Added additive SQLite table `timesfm_forecast_evidence` with stable `forecast_id` uniqueness and audit fields: timestamp, symbol, timeframe, horizon, current price, p10/p50/p90, side, expected RR, rejection reason, mode, model metadata, and no-lookahead input end timestamp.
- Added additive `timesfm_forward_outcome_labels` table for future calibrated outcomes (`TP_BEFORE_SL`, `SL_BEFORE_TP`, `TIMEOUT`, `AMBIGUOUS`) and MFE/MAE / expected-vs-realized R storage.
- Added schema migration marker `2026_06_21_timesfm_canonical_evidence`.

### Export/schema changes
- CSV TimesFM decision logs now include canonical evidence fields, including `forecast_id`, `horizon`, `mode`, provider/model metadata, and `no_lookahead_input_end_ts`.
- Schema changes are additive only.

### Tests added
- Decision CSV contains canonical TimesFM evidence fields.
- SQL persistence contains TimesFM evidence rows and does not create order decision rows.
- Re-running the same candles produces stable forecast IDs and idempotent SQL rows.
- Invalid forecasts persist `NO_TRADE` / `INVALID_FORECAST`.

### Tests executed
- `pytest -q tests/test_timesfm_futures.py` failed during collection because this container cannot import NumPy.
- `PYTHONPATH=src python -m compileall -q src tests` passed.
- `PYTHONPATH=src python - <<'PY' ...` SQL smoke passed: two TimesFM evidence rows persisted and zero order decisions were created.
- `rg -n "submit|place_order|order_decision|save_order_decision" src/alphaforge/timesfm_futures.py src/alphaforge/models/timesfm_forecaster.py` returned no TimesFM order-submit path.

### Risks
- Full forward outcome labeling/calibration is not implemented; the table is present to support a future truthful calibration job.
- NumPy is missing in this container, so TimesFM ndarray regression tests could not execute here.
- Real TimesFM model weights/package setup remains external.

### Remaining limitations
- TimesFM should stay isolated as a forecast evidence provider until calibrated against forward outcomes.
- TimesFM should not become an AIBrain feature until calibration proves incremental value and execution-cost impact is measured.

### Migration concerns
- Additive SQLite migration only; no existing tables are dropped or rewritten.
- Existing CSV consumers may see additional columns in TimesFM decision logs.

### Push recommendation
- Safe to push after validating the full TimesFM test file in an environment with NumPy installed. Do not treat this patch as LIVE readiness.
