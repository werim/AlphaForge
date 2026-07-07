## 2026-07-07 PR266 Pre-merge Fix Report

### Why this patch was needed
CI exposed that full BACKTEST lifecycle simulation still expected `cost_penalty_total` compatibility evidence from `_execution_reject_flags`. The previous Phase 3 patch also surfaced new checks without aggregating them into a lower fail-closed readiness gate, and `total_cost_pct` could be read as full RR penalty even though latency/liquidity/volatility are RR penalties rather than explicit percentage costs.

### Root cause
The canonical breakdown renamed the full penalty to `cost_penalty_rr` but some BACKTEST simulation paths still consumed the legacy `cost_penalty_total` key. Readiness checks were appended as standalone checks but omitted from lower readiness gate aggregation. Cost diagnostics did not clearly separate explicit percentage costs from full RR penalties.

### Files changed
- `src/alphaforge/execution.py`: preserves `cost_penalty_total`, adds `total_explicit_cost_pct`, `total_rr_penalty`, and conservative default thresholds; preserves reject priority for slippage/spread before low effective RR.
- `backtest_order.py`: carries `total_explicit_cost_pct` through lifecycle and SQL evidence updates, and populates Phase 3 source/unavailable fields from the canonical breakdown during simulation.
- `src/alphaforge/persistence.py`: additively adds `total_explicit_cost_pct` to `decision_evidence`.
- `src/alphaforge/live_readiness.py`: adds `phase3_execution_realism_complete` and includes it in lower readiness gates.
- `tests/test_live_readiness.py`: adds per-check fail-closed Phase 3 readiness regressions.
- `tests/test_execution_cost_breakdown.py`: asserts explicit-cost and RR-penalty diagnostics are not ambiguous.

### Runtime behavior changes
No strategy scoring, portfolio risk, live order submission, or live enablement changed. Execution rejection remains conservative: high slippage/spread can be the first blocking execution reason, while low effective RR remains in reject flags when the cost-adjusted RR is below threshold.

### Persistence/export changes
`decision_evidence` now includes additive `total_explicit_cost_pct`. Existing `total_cost_pct` is preserved as a compatibility alias for explicit percentage costs. The full RR penalty is `cost_penalty_rr` / `cost_penalty_total` in diagnostics and `cost_penalty` in SQL rows.

### Tests added/executed
Added Phase 3 gate regressions and explicit-cost semantic assertions. Required targeted and full test commands were executed.

### Risks / remaining limitations
Historical execution evidence remains estimated or unavailable unless supplied by source data. PAPER still needs sustained measured spread/latency/liquidity evidence. LIVE remains NOT READY and disabled.

### Push recommendation
Safe to push after full test suite passes; do not claim LIVE readiness.

## 2026-07-07 Phase 3 Execution Realism and Cost Model Hardening

### Root Cause
Phase 1/2 established shared pre-submit decisions and SQL-backed evidence, but execution costs still had gaps: the canonical penalty model did not expose a full cost-breakdown object, fee penalty was absent from effective RR diagnostics, missing liquidity could become perfect liquidity in `build_execution_context`, and readiness gates did not explicitly fail on accepted trades with missing critical execution context.

### Files Changed
- `src/alphaforge/execution.py`: added canonical `ExecutionCostBreakdown`, source classification, explicit fee penalty, reject flags, unavailable field propagation, and deterministic diagnostics JSON.
- `src/alphaforge/effective_rr.py`: includes `fee_penalty` in effective RR result diagnostics.
- `backtest_order.py`: BACKTEST execution reject flags now use canonical breakdown and lifecycle evidence carries cost/source/unavailable fields.
- `src/alphaforge/persistence.py`: additively extends `decision_evidence` with Phase 3 execution fields.
- `src/alphaforge/config_registry.py`: adds Phase 3 execution threshold env vars.
- `src/alphaforge/live_readiness.py`: adds fail-closed Phase 3 readiness gates.
- `tests/test_execution_cost_breakdown.py`: adds regression coverage for source tags, missing funding, fee penalty, low liquidity, and unavailable context flags.

### Execution Cost Model Before
The model penalized spread, slippage, latency, funding, liquidity, and volatility, but fee cost was not represented and callers could inspect only partial diagnostics. Some missing context paths relied on numeric fallbacks such as liquidity=1.0. Readiness only had Phase 2 fake-zero evidence checks.

### Execution Cost Model After
Canonical formula:

`effective_rr = raw_rr - spread_penalty - slippage_penalty - fee_penalty - funding_penalty - latency_penalty - liquidity_penalty - volatility_penalty`

`ExecutionCostBreakdown` exposes spread/slippage/fee/funding/latency/liquidity/volatility values, source tags (`MEASURED`, `ESTIMATED_BACKTEST`, `MODELLED`, `UNAVAILABLE`), `total_cost_pct`, `cost_penalty_rr`, `reject_flags`, `unavailable_fields`, and JSON diagnostics. Missing numeric values remain `NULL`/unavailable rather than fake zero.

### SQL / Export Evidence
`decision_evidence` adds: `total_cost_pct`, `spread_source`, `slippage_source`, `fee_pct`, `fee_source`, `funding_source`, `latency_ms`, `latency_source`, `liquidity_status`, `volatility_penalty_pct`, `volatility_source`, `reject_flags`, and `unavailable_fields`. The export remains SQL-backed through existing `decision_evidence.csv` export.

### Rejection / Readiness Impact
Canonical reject flags include `LOW_EFFECTIVE_RR`, `HIGH_SPREAD`, `HIGH_SLIPPAGE`, `HIGH_TOTAL_COST`, `LOW_LIQUIDITY`, `HIGH_LATENCY`, `EXECUTION_CONTEXT_UNAVAILABLE`, `EXCESSIVE_VOLATILITY_PENALTY`, `FUNDING_UNAVAILABLE`, and `FUNDING_TOO_HIGH`. LIVE remains fail-closed and NOT READY.

### Tests Added / Executed
- Added focused `tests/test_execution_cost_breakdown.py`.
- Executed targeted backtest/persistence/readiness regressions and the new cost breakdown regression.

### Remaining Phase 4 Blockers
- Sustained PAPER evidence with measured exchange spread/latency/liquidity.
- Authenticated read-only reconciliation evidence.
- Demonstrated no accepted trades below configured effective RR over representative runs.
- Full run artifact regeneration to populate new additive Phase 3 columns.
- LIVE order submission remains disabled until all historical gates and operational gates pass.

### Push Recommendation
Safe to push after targeted/full validation. Do not claim LIVE readiness.

## 2026-07-06 Phase 2 SQL-backed Evidence Consistency Report

### Why this patch was needed
BACKTEST exports and dashboard panels could still treat in-memory CSV rows or dashboard-only fallbacks as truth. Phase 2 makes persisted lifecycle evidence the source that CSV artifacts and dashboard metrics reconcile against.

### Root cause
The lifecycle exporter already wrote SQL rows, but there was no normalized decision-evidence surface containing the full lifecycle/decision/execution context contract, and dashboard/profile comparison parsing had multiple fallback paths that could disagree for the same selected profile.

### Files changed
- `src/alphaforge/persistence.py`: added additive `decision_evidence` schema and migration bookkeeping.
- `backtest_order.py`: persists normalized decision evidence beside lifecycle rows into the configured durable AlphaForge DB (or an explicit test DB URL), preserves unavailable numeric execution evidence as NULL, and exports SQL-backed lifecycle/evidence CSV aliases directly from SQL.
- `src/alphaforge/dashboard/backtest_control.py`: selected-profile parsing now falls back to `order_backtest_lifecycle.csv`, derives metrics from the same selected summary/lifecycle/rejected evidence, and surfaces specific missing-evidence warnings.
- `src/alphaforge/live_readiness.py`: added Phase 2 evidence gates for lifecycle/reject/accept persistence, fake-zero execution blockers, and decision parity mismatch blockers.
- `VERSION.md`, `CHANGELOG.md`, `REPORT.md`: documented Phase 2 evidence authority and remaining blockers.

### Schema/table changes
- Added `decision_evidence` with run/profile/mode/timestamp, lifecycle before/after, ACCEPT/REJECT/WAIT decisions, score/RR/expectancy, reject/cancel/close reasons, execution context metrics, diagnostics JSON, and signal/order/position/lifecycle identifiers.
- Existing tables remain backward compatible; no existing CSV artifact was removed.

### Before/after evidence flow
- Before: lifecycle rows were persisted, but dashboard/export consumers could rely on memory-built CSVs and inconsistent fallback calculations.
- After: lifecycle rows are persisted first, normalized decision evidence is written beside them, and CSV/dashboard values consume SQL-backed lifecycle/export evidence for the selected profile.

### Reconciliation formulas
- `final_decisions = accepted + rejected + cancelled`.
- `reject_rate = rejected / max(1, accepted + rejected)`.
- `SIGNAL_REJECTED/ORDER_REJECTED/SYMBOL_REJECTED lifecycle count == rejected_orders.csv row count`.
- `net_pnl = sum(net_pnl_usdt for POSITION_CLOSED rows)`, with summary values preferred only when the selected profile summary exists.
- Accepted-but-never-triggered rows remain lifecycle evidence and are not counted as filled trades.

### Tests added/executed
- Added durable decision-evidence regressions in `tests/test_backtest_order_scanner.py` for cross-session SQL reads, `decision_evidence.csv` row-count reconciliation, and NULL unavailable execution evidence.
- Added readiness regressions in `tests/test_live_readiness.py` proving SQL-empty `decision_evidence` fails even when CSV artifacts exist and `DECISION_PARITY_MISMATCH` in SQL blocks readiness.

### Risks and remaining limitations
- Durable SQL-backed BACKTEST export now writes to `ALPHAFORGE_DATABASE_URL` / `ALPHAFORGE_DB_URL` or an output-directory SQLite DB; Phase 3 should add retention/cleanup policy for accumulated run/profile evidence.
- Virtual BACKTEST fills remain simulation evidence, not live execution readiness.
- LIVE remains NOT READY and is not enabled by this patch.

### Migration concerns
Additive SQLite schema only. Consumers that ignore `decision_evidence.csv` and `order_backtest_lifecycle.csv` can continue using legacy artifacts.

### Push recommendation
Safe to push for Phase 2 review after targeted validation; do not promote LIVE readiness.


## 2026-07-06 Decision-Boundary Authority Follow-up

### Why this patch was needed
Review found that `evaluate_signal_decision(...)` was being used as a BACKTEST side-channel diagnostic rather than the authoritative boundary. It also called `evaluate_paper_style_pre_submit(...)`, which executes accepted candidates and emits an ORDER_PLACED audit, violating the intended pre-submit-only contract.

### Root cause
The first Phase 1 patch wrapped existing PAPER pre-submit behavior instead of separating candidate/quality/effective-RR evaluation from order execution. BACKTEST then called the wrapper but still trusted `run_order_cycle(...)` for the real accept/reject result.

### Files changed
- `src/alphaforge/order.py`: refactored `evaluate_signal_decision(...)` to directly perform candidate construction, quality gates, and effective-RR checks without executing orders or emitting audit events.
- `backtest_order.py`: BACKTEST scan now treats `DecisionResult` as authoritative and fails closed on parity mismatch with the legacy runtime cycle.
- `tests/test_backtest_paper_pre_submit_parity.py`: added no-audit, market-shape score variability, and fail-closed ignored-boundary tests.
- `tests/test_backtest_order_scanner.py`: updated scanner tests to account for fail-closed decision authority and realistic execution context.
- `CHANGELOG.md`, `REPORT.md`: documented the follow-up.

### Runtime behavior changes
- Accepted `evaluate_signal_decision(...)` calls no longer execute virtual/paper/live orders and no longer append ORDER_PLACED audit rows.
- BACKTEST scanner rejects with `DECISION_PARITY_MISMATCH` if legacy runtime status disagrees with the shared boundary.

### Lifecycle changes
- The boundary still returns lifecycle intent, but actual lifecycle persistence remains in the existing BACKTEST export path.
- Rejected BACKTEST decisions continue flowing into lifecycle/rejected exports via `process_backtest_result(...)`.

### Persistence/export/schema changes
- No schema migration.
- Existing SQL-first lifecycle and rejected export writers remain intact.

### Tests added/executed
- Added tests for no ORDER_PLACED audit side effect, candle-derived score variability, and fail-closed BACKTEST parity mismatch.
- Executed `python -m py_compile src/alphaforge/order.py backtest_order.py`.
- Executed `pytest -q tests/test_backtest_paper_pre_submit_parity.py tests/test_trading_modes.py`.
- Executed `pytest -q`.

### Risks and remaining limitations
- `run_order_cycle(...)` still exists for legacy parity checking and other call sites; Phase 2 should consolidate remaining direct runtime uses where practical.
- Full durable `DecisionResult` persistence by run/profile remains a Phase 2 blocker.

### Migration concerns
None.

### Push recommendation
Safe to push for review; LIVE remains NOT READY.


## 2026-07-06 Phase 1 Decision-Parity Surgery Report

### Why this patch was needed
BACKTEST needed an explicit shared pre-submit decision boundary with PAPER/LIVE semantics before virtual result simulation, because prior evidence suggested lifecycle exports could look like direct TP/SL result simulation rather than decision-pipeline evidence.

### Root cause
BACKTEST had partial reuse of `run_order_cycle(...)`, but no first-class `DecisionResult` object carrying the required mode-agnostic decision fields. Missing BACKTEST funding also fell back to fake zero in the offline market context.

### Files changed
- `src/alphaforge/order.py`: added `DecisionResult` and `evaluate_signal_decision(...)`.
- `backtest_order.py`: BACKTEST scanner invokes the shared boundary and no longer fake-zeroes missing funding.
- `tests/test_backtest_paper_pre_submit_parity.py`: added shared boundary parity/evidence tests.
- `CHANGELOG.md`, `VERSION.md`, `REPORT.md`: documented Phase 1 behavior and risks.

### Decision-flow map before
- BACKTEST generation: `backtest_order.py::_build_market_ctx(...)` and `scan_symbol_backtest(...)`.
- PAPER/LIVE generation: `src/alphaforge/runtime.py::_build_signal_payload(...)` and `_process_selection(...)`.
- Scoring/rejects: `evaluate_trade_quality(...)`, `AIBrain.score_signal(...)`, and runtime `before_real_order(...)` existed but were not exposed as a single shared boundary object for BACKTEST/PAPER/LIVE evidence.
- Persistence/export: BACKTEST used `LifecycleRow`, `_persist_lifecycle_rows(...)`, `save_order_decision(...)`, `save_trade_lifecycle_event(...)`, and CSV export.

### Decision-flow map after
- BACKTEST now calls `evaluate_signal_decision(...)` before virtual fills.
- `evaluate_signal_decision(...)` wraps the existing PAPER-style pre-submit path and returns decision, score, raw/effective RR, reject/cancel reason, expectancy bucket, regime, execution fields, liquidity status, and lifecycle intent.
- BACKTEST remains offline-safe and does not call Binance order APIs.

### Runtime behavior changes
- BACKTEST diagnostics tag the shared decision boundary.
- Missing offline funding is unavailable/null, not fake zero.

### Lifecycle changes
- Shared accepted lifecycle intent: SIGNAL_CREATED → WAITING_ENTRY_ZONE → ENTRY_TRIGGERED → ORDER_PLACED.
- Shared rejected lifecycle intent: SIGNAL_CREATED → SIGNAL_REJECTED.
- Existing BACKTEST SQL-first lifecycle persistence/export remains in place.

### Persistence/export/schema changes
- No schema migration.
- SQL-first persistence functions are unchanged; dashboard/export behavior continues to read persisted lifecycle evidence.

### Tests added/executed
- Added tests for shared decision boundary parity, BACKTEST low-score rejection, score variability, and unavailable funding evidence.
- Executed `pytest -q tests/test_backtest_paper_pre_submit_parity.py tests/test_trading_modes.py`.

### Risks and remaining limitations
- BACKTEST fill simulation after acceptance remains virtual.
- Historical spread/funding/orderbook/latency can be incomplete.
- Phase 2 should persist complete `DecisionResult` payloads for every long-running profile/run_id and further reconcile runtime direct `AIBrain.before_real_order(...)` calls into the boundary.

### Migration concerns
None.

### Push recommendation
Safe for Phase 1 review; do not claim LIVE readiness.

## 2026-07-02 BACKTEST SCORE10 SL dominance diagnostic guard

### Why the patch was needed
Latest BACKTEST artifacts showed score=10 was not reliably predictive: score=10 rows had more WOULD_SL than WOULD_TP evidence, including a STOP_TOO_WIDE high-score SL cluster. Operators needed an auditable diagnostic artifact without changing production thresholds.

### Root cause
Existing score saturation summaries exposed aggregate score=10 weakness but did not export a dedicated bucketed guard artifact with sample-size confirmation, exploratory marking, effective shadow R statistics, and STOP_TOO_WIDE cluster flags.

### Files changed
- `src/alphaforge/dashboard/backtest_control.py`: added BACKTEST-only SCORE10_SL_DOMINANCE_GUARD builders and JSON/CSV export wiring from existing accepted/rejected shadow diagnostic rows.
- `src/alphaforge/dashboard/templates/overview.html`: added a clearly labeled dashboard summary stating BACKTEST ONLY, diagnostic only, production thresholds unchanged, and PAPER/LIVE unchanged.
- `tests/test_backtest_order_scanner.py`: added regression coverage for SL-dominant flagging, TP-dominant non-flagging, exploratory low-sample buckets, disabled-env no-export behavior, and no accepted-count mutation path.
- `tests/test_dashboard_app.py`: added dashboard-side coverage for environments where optional dashboard dependencies are installed.
- `VERSION.md`, `CHANGELOG.md`, `REPORT.md`: documented diagnostic-only scope and risks.

### Runtime behavior changes
When `ALPHAFORGE_BACKTEST_SCORE10_SL_DOMINANCE_GUARD=true`, BACKTEST dashboard artifact processing writes `score10_sl_dominance_guard.json` and `score10_sl_dominance_guard.csv`. Production acceptance thresholds, legacy strategy guardrails, DEFAULT_FILTERS trade counts, PAPER, and LIVE behavior are unchanged.

### Lifecycle changes
None. The patch does not modify `_guardrail_rejection_reason`, does not call `_append_guardrail_reject`, and does not reject live orders.

### Persistence changes
No SQLite migration or persistence-contract change. The new artifacts are additive BACKTEST diagnostic exports derived from existing diagnostic rows.

### Export/schema changes
Added `score10_sl_dominance_guard.json` and `score10_sl_dominance_guard.csv` with bucket counts, forward-evaluable counts, WOULD_TP/WOULD_SL/ambiguous/timeout counts, rates, effective shadow R mean/median/confidence lower bound, exploratory markers, and flags.

### Tests added/executed
Added targeted regressions for SCORE10 SL dominance and disabled-env export behavior.

### Risks and remaining limitations
This is calibration evidence, not an acceptance or rejection rule. Bucket evidence can be sparse, and missing forward outcomes remain non-confirming. Broader multi-window validation is required before any future production threshold proposal.

### Migration concerns
None. Artifact consumers should tolerate the new optional BACKTEST files.

### Push recommendation
Push only after targeted/full validation. LIVE remains NOT READY.

## 2026-07-01 Diagnostic profile execution-context strictness

### Why the patch was needed
PR262 diagnostic gating could treat missing execution fields as favorable defaults: zero spread/slippage/cost or perfect liquidity. That violated AlphaForge execution-realism rules and the explicit-unavailable-marker standard.

### Root cause
`_diagnostic_short_low_score_breakdown_row_allowed` used `_safe_float(..., default)` fallbacks after geometry checks. Missing `cost_penalty`, `spread_pct`, and `expected_slippage_pct` became `0.0`, while missing `liquidity_score` became `1.0`.

### Files changed
- `backtest_order.py`: added strict numeric validation and blocks unavailable execution context as `EXECUTION_CONTEXT_UNAVAILABLE` before applying existing safety thresholds.
- `.env.example`, `.env.medium.example`, `.env.live.example`, `.env.test.example`: documented `ALPHAFORGE_BACKTEST_SHORT_LOW_SCORE_BREAKDOWN_DIAGNOSTIC_SYMBOLS` as BACKTEST-only diagnostic scope.
- `tests/test_backtest_order_scanner.py`: added missing/unavailable execution-context regression tests.
- `VERSION.md`, `CHANGELOG.md`, `REPORT.md`: documented the fail-closed diagnostic behavior.

### Runtime behavior changes
Only BACKTEST diagnostic inclusion changed. DEFAULT_FILTERS accepted counts, PAPER, LIVE, and production thresholds remain unchanged.

### Lifecycle changes
None. Diagnostic candidates remain rejected evidence.

### Persistence changes
None. Artifact rows may be fewer when execution context is unavailable, and summary blocked reasons now count `EXECUTION_CONTEXT_UNAVAILABLE`.

### Export/schema changes
No schema change. Existing diagnostic summary `blocked_reason_distribution` can now include `EXECUTION_CONTEXT_UNAVAILABLE`.

### Tests added/executed
Added regressions for missing spread, slippage, cost, liquidity, effective RR, and min effective RR.

### Risks and remaining limitations
The stricter diagnostic profile can reduce candidate counts if historical execution context is incomplete. This is intentional and safer than fake favorable defaults.

### Migration concerns
None.

### Push recommendation
Push after targeted/full validation. LIVE remains NOT READY.

## 2026-07-01 SHORT LOW_SCORE BREAKDOWN diagnostic profile

### Why the patch was needed
Latest BACKTEST/dashboard evidence indicated `DEFAULT_FILTERS` still accepted zero trades while `ALL_FILTERS_OFF` produced losing trades. The root-cause summary identified `LOW_SCORE` as the main bottleneck, but broad threshold relaxation was unsafe. The only defensible next step was a diagnostic-only, narrowly scoped shadow profile for the strongest observed SHORT LOW_SCORE BREAKDOWN bucket.

### Root cause
LOW_SCORE rejects were already forward-evaluable, but there was no dedicated artifact that isolated SHORT `BREAKDOWN_DOWN` LOW_SCORE rows in the validated good UTC-hour group while keeping execution-cost and hard safety gates active. This made it difficult to compare the bucket without risking production-filter drift.

### Files changed
- `backtest_order.py`: added `SHORT_LOW_SCORE_BREAKDOWN_DIAGNOSTIC`, configurable symbol scope, candidate filtering, safety gating, summary aggregation, and artifact exports.
- `src/alphaforge/dashboard/backtest_control.py`: reads the diagnostic summary/candidate artifact paths into the dashboard result model.
- `src/alphaforge/dashboard/templates/overview.html`: renders a separate diagnostic-only row/card and states production thresholds are unchanged.
- `tests/test_backtest_order_scanner.py`: added regressions for scope, STOP_TOO_WIDE/HIGH_VOL_GUARD preservation, candidate counts, and DEFAULT_FILTERS accepted-count immutability.
- `tests/test_dashboard_app.py`: extended dashboard artifact parsing/rendering coverage where dashboard dependencies are available.
- `VERSION.md`, `CHANGELOG.md`, `REPORT.md`: documented behavior, risks, and no-live-readiness stance.

### Runtime behavior changes
No production behavior changed. DEFAULT_FILTERS accepted counts are not mutated. PAPER/LIVE configuration and thresholds are unchanged.

### Lifecycle changes
No lifecycle states or transitions changed. Candidate rows remain rejected evidence and are exported only as diagnostic shadow candidates.

### Persistence changes
No SQLite migration and no order-decision persistence change. New CSV/JSON artifacts are BACKTEST-only and additive.

### Export/schema changes
Added `diagnostic_short_low_score_breakdown_candidates.csv` and `diagnostic_short_low_score_breakdown_summary.json`. `order_backtest_summary.csv` receives additive diagnostic count/profile/note fields.

### Tests added/executed
Added targeted regressions for diagnostic profile scope and safety gates. Dashboard rendering coverage is present but may be skipped in environments without dashboard optional dependencies.

### Risks and remaining limitations
The diagnostic sample can still be too small, execution-cost estimates may drift, and forward labels are shadow evidence rather than trade approvals. This patch must not be used to justify LOW_SCORE threshold relaxation or LIVE readiness.

### Migration concerns
None. Consumers should tolerate additive BACKTEST artifact files and summary fields.

### Push recommendation
Push after targeted/full tests and one multi-symbol comparison backtest. LIVE remains NOT READY.

## 2026-07-01 BACKTEST lifecycle/reject SQL persistence completion

### Why the patch was needed
BACKTEST lifecycle artifacts already emitted canonical pre-trade states, but rejected decisions were not mirrored into `order_decisions` during the SQL-first lifecycle persistence pass. This made SQL-backed reject diagnostics depend on lifecycle rows alone and left ambiguous fallback values (`UNKNOWN`) for last-resort reject and unavailable expectancy evidence.

### Root cause
`backtest_order._persist_lifecycle_rows` saved `trade_lifecycle_events` from lifecycle rows but did not also persist the corresponding signal and final order-decision rows. Missing reject attribution fell through to `UNKNOWN`, and missing BACKTEST expectancy used a generic unavailable bucket.

### Files changed
- `backtest_order.py`: persists `signals` and final `order_decisions` alongside lifecycle events; rejected SQL rows now receive non-empty canonical reject reasons with `REJECT_REASON_UNAVAILABLE` as the explicit defensive fallback; unavailable expectancy now exports `BACKTEST_EXPECTANCY_UNAVAILABLE`; persisted lifecycle export rows include additive SQL order-decision count diagnostics.
- `tests/test_backtest_order_scanner.py`: updated lifecycle/reject persistence regressions to assert rejected lifecycle rows have matching SQL rejected-decision diagnostics and the explicit BACKTEST expectancy-unavailable bucket.
- `VERSION.md`, `CHANGELOG.md`, `REPORT.md`: documented lifecycle/reject persistence behavior, risks, and validation.

### Runtime behavior changes
BACKTEST export persistence now writes SQL signal and final decision evidence in the same pass as lifecycle events. PAPER/LIVE behavior and guards are unchanged.

### Lifecycle changes
Canonical lifecycle states are preserved. Rejected signal/order states remain distinct as `SIGNAL_REJECTED`, `ORDER_REJECTED`, and `SYMBOL_REJECTED`; accepted paths still retain pre-terminal states before `POSITION_CLOSED`.

### Persistence changes
No migration. In-memory BACKTEST SQLite persistence now contains `signals`, `order_decisions`, and `trade_lifecycle_events` for exported lifecycle candidates. Rejected rows can be reconciled through additive `sql_order_decision_count` and `sql_rejected_decision_count` diagnostics.

### Export/schema changes
`order_lifecycle.csv` receives additive SQL-count columns through the existing dynamic fieldname resolver. Existing columns remain backward-compatible. Missing BACKTEST expectancy is now labeled `BACKTEST_EXPECTANCY_UNAVAILABLE` instead of generic `EXPECTANCY_UNAVAILABLE`/`UNKNOWN`.

### Tests added/executed
Updated regressions for SQL-backed rejected-decision counts and explicit unavailable expectancy buckets.

### Risks and remaining limitations
The BACKTEST SQL persistence used by artifact generation is still run-local/in-memory; it validates export parity and SQL contracts but is not a durable research database unless callers configure a persistent DB path. Historical funding/spread fields remain limited to available historical/safe estimates and explicit unavailable sentinels; BACKTEST still must not call live orderbook/order APIs.

### Migration concerns
No SQLite schema migration. CSV consumers should tolerate additive SQL diagnostic columns and the `BACKTEST_EXPECTANCY_UNAVAILABLE` value.

### Push recommendation
Push after targeted/full pytest validation. LIVE remains NOT READY.

## 2026-07-01 reject overlay diagnostics

### Why the patch was needed
PR259 rejected-forward evidence showed TP/SL separation in specific rejected buckets, especially SHORT LOW_SCORE BREAKDOWN candidates in selected UTC hours, while LONG BREAKOUT_UP bad-hour and guard rejects remained protective. AlphaForge needed a discovery layer to export those patterns without changing production thresholds or accepted trades.

### Root cause
Rejected-forward outcomes existed, but there was no conservative BACKTEST-only overlay that grouped rows by symbol, side, setup, regime, hour group, reject reason, and LOW_SCORE gap band, nor a summary that could distinguish positive shadow candidates from negative confirmations and insufficient samples.

### Files changed
- `backtest_order.py`: added reject-overlay label generation, bucket expectancy aggregation, conservative verdicts, CSV/JSON exports, and zero-accepted summary fields.
- `tests/test_reject_overlay.py`: added regressions for required overlay labels, near-threshold semantics, guard no-rescue behavior, verdict classification, missing evidence, and accepted-count immutability.
- `VERSION.md`, `CHANGELOG.md`, `REPORT.md`: updated operational documentation.

### Runtime behavior changes
No default BACKTEST accept/reject decision changed. PAPER/LIVE behavior is untouched. Diagnostic candidates remain labels only and are marked `production_decision_changed=false`.

### Lifecycle changes
No lifecycle states or transitions changed. Rejected rows remain rejected and are only annotated in diagnostic artifacts.

### Persistence changes
No SQLite migration. New evidence is written to BACKTEST CSV/JSON artifacts only.

### Export/schema changes
Added `reject_overlay_diagnostics.csv`, `reject_overlay_summary.json`, `reject_bucket_expectancy.csv`, and `reject_bucket_expectancy.json`. `zero_accepted_root_cause_summary` now includes strongest positive/negative diagnostic buckets, `production_threshold_change_recommended=false`, and the requested conservative next action.

### Tests added
Added targeted tests for LONG bad-hour traps, SHORT good-hour diagnostic candidates, 5% LOW_SCORE near-threshold splits, HIGH_VOL_GUARD LONG no-rescue labels, positive/negative/insufficient bucket verdicts, and no accepted-count changes.

### Tests executed
- `python -m pytest tests/test_reject_overlay.py -q`

### Risks and remaining limitations
Bucket verdicts require enough forward-evaluable evidence. Micro-buckets are exploratory only and must never drive production threshold changes. Symbol-level reject geometry may still be unavailable until safe pre-reject candidate geometry capture is added.

### Migration concerns
None for SQLite. CSV/JSON consumers should tolerate additive BACKTEST artifacts and summary fields.

### Push recommendation
Push after targeted/full pytest validation. Do not claim LIVE readiness or relax LOW_SCORE/HIGH_VOL_GUARD/STOP_TOO_WIDE.

## 2026-07-01 score calibration diagnostics

### Why the patch was needed
The latest BTCUSDT 30d/1h `rejected_shadow.csv` evidence showed score variability but weak raw-outcome calibration: WOULD_TP=187, WOULD_SL=328, UNKNOWN=3, mean score for WOULD_TP around 4.44, mean score for WOULD_SL around 4.41, and corr(score, WOULD_TP) around 0.01. Effective TP separation was better, indicating the current score is closer to mixed quality/execution evidence than raw TP-before-SL probability.

### Root cause / exact score source files and functions
- BACKTEST score originates in `backtest_order._build_market_ctx`, where score is a 0-10 blend of breakout strength and candle range, with expectancy derived from score and RR. It is routed through `scan_symbol_backtest`, the shared order runtime, guardrails, rejected exports, and `evaluate_rejected_shadow`.
- PAPER/LIVE score originates in `src/alphaforge/ai_brain.py::AIBrain.score_signal`, where `total_score` is a 0-1 deterministic probabilistic score mixing setup quality, regime alignment, expectancy edge, momentum, liquidity, volatility fit, RR quality, execution success probability, confidence, and penalties. Runtime persistence uses that score through `src/alphaforge/runtime.py` and `src/alphaforge/order.py`.
- BACKTEST and PAPER/LIVE score scales and formulas are therefore not identical: BACKTEST score mostly describes setup/breakout strength plus execution-derived gates, while PAPER/LIVE `AIBrain` score is intended as conservative probability/quality after costs. Neither should be treated as a pure raw WOULD_TP probability without calibration evidence.

### Files changed
- `backtest_order.py`: added score calibration artifact builders, Pearson/Spearman helpers, score bucket breakdowns, miscalibration flags, BACKTEST-only calibrated-score diagnostics, and `score_calibration_summary.json` export.
- `tests/test_backtest_order_scanner.py`: added regression tests for diagnostics exports, count reconciliation, correlations, high-score SL cluster flags, calibrated-score penalties, and diagnostic-only/no-threshold-change guarantees.
- `VERSION.md`, `CHANGELOG.md`, `REPORT.md`: updated operational documentation.

### Runtime behavior changes
No acceptance threshold changed. No default BACKTEST/PAPER/LIVE decision formula was loosened. The calibrated score is exported as diagnostic evidence only and is labeled `BACKTEST_DIAGNOSTIC_ONLY`.

### Lifecycle changes
No lifecycle state changed. Rejected signals remain rejected and continue to produce rejected-shadow evidence.

### Persistence changes
No SQLite migration. New evidence is emitted as BACKTEST CSV/JSON artifacts only.

### Export/schema changes
- `score_calibration_diagnostics.csv` now includes score-bucket/reason/regime/setup breakdown rows with counts, WOULD_TP/WOULD_SL/effective-TP rates, average raw/effective RR, cost penalty, stop distance, volatility, spread/slippage, and calibrated-score averages.
- `score_calibration_summary.json` includes Pearson/Spearman correlations for score vs raw WOULD_TP and effective TP, monotonicity status, miscalibration flags, source interpretation, and diagnostic-only threshold flags.

### Calibration evidence and high-score failure clusters
The patch encodes the observed failure modes as flags: `HIGH_SCORE_LOW_TP_RATE`, `SCORE_INVERSION`, `OVEREXTENSION_NOT_PENALIZED`, `HIGH_VOL_HIGH_SCORE_SL_CLUSTER`, `STOP_TOO_WIDE_HIGH_SCORE_SL_CLUSTER`, `SCORE_NOT_MONOTONIC`, and `SCORE_PREDICTS_EFFECTIVE_TP_BETTER_THAN_RAW_TP`.

### Tests added/executed
Added tests proving exported score-bucket counts reconcile to rejected-shadow records, correlation metrics are present, high-score low-TP clusters are flagged, calibrated_score penalizes high-volatility SL-prone candidates, diagnostic calibrated_score declares no forward outcome field usage, and default acceptance logic remains unchanged.

### Risks and remaining limitations
The diagnostic calibrated score is a conservative hypothesis, not production calibration. It uses only row-local pre-decision features, but it must be validated across regenerated BTCUSDT 30d/1h and additional symbols/timeframes before any default score or threshold change.

### Migration concerns
None for SQLite. CSV/JSON consumers should tolerate additive fields and the new `score_calibration_summary.json` artifact.

### Recommended next action
Regenerate BTCUSDT 30d/1h artifacts and compare raw score vs calibrated_score monotonicity and correlations. Do not loosen acceptance thresholds unless calibrated evidence improves raw and effective outcome separation without increasing execution-risk clusters.

### Push recommendation
Push after targeted and full pytest validation pass. Do not claim LIVE readiness.

## 2026-07-01 PR256 diagnostic extraction correction

### Why the patch was needed
Manual BTCUSDT 30d/1h artifact validation after PR256 found diagnostic extraction defects, not production threshold defects: LOW_SCORE diagnostics used a 0-1 fallback threshold while exported rows carried 7.5, and symbol-level rejects had selector metrics nested inside diagnostics JSON.

### Root cause
`build_low_score_diagnostics` trusted BACKTEST config fallback before row-level exported evidence. `build_symbol_reject_diagnostics` only inspected top-level columns and did not parse `diagnostics.selector.inputs` / `diagnostics.selector.metrics`. The zero-accepted summary therefore could overclaim COMPLETE evidence quality.

### Files changed
- `backtest_order.py`: LOW_SCORE threshold source/scale detection, nested symbol selector extraction, and root-cause evidence-quality reasons.
- `tests/test_strategy_quality_guardrails.py`: regression coverage for LOW_SCORE threshold scale, near/far counts, nested selector metrics, FEATURE_MISSING classification, and partial evidence quality.
- `REPORT.md`, `CHANGELOG.md`, `VERSION.md`: updated operational documentation.

### Runtime behavior changes
No BACKTEST/PAPER/LIVE acceptance threshold changed. The patch only changes exported diagnostics and summaries. LOW_SCORE diagnostics now prefer row evidence (`min_required_score`, diagnostics thresholds) over fallback config and report threshold source/scale metadata. Symbol reject diagnostics now populate selector metrics from diagnostics JSON when top-level columns are empty.

### Lifecycle changes
No lifecycle state transition changed. Rejected rows remain rejected and symbol-level rejects remain symbol-level rejects.

### Persistence changes
No database migration is required. BACKTEST diagnostic CSV/JSON artifacts gain additive fields for threshold sources, scale mismatch/correction flags, selector metrics, selector reject reasons, selector sub-scores, and evidence-quality reasons.

### Export/schema changes
Additive CSV fields may appear in `low_score_diagnostics.csv`, `symbol_reject_diagnostics.csv`, and `zero_accepted_root_cause_summary.csv/json`. Existing threshold values are not rewritten in source rejected rows.

### Tests added
Added tests proving score 6.37 vs threshold 7.5 produces a 1.13 gap, row threshold 7.5 overrides fallback scale, near/far counts use real thresholds, nested selector metrics populate symbol diagnostics, FEATURE_MISSING is reserved for genuinely absent metrics, and zero-accepted evidence quality is PARTIAL when diagnostic evidence is invalid/missing.

### Tests executed
- `python -m pytest tests/test_strategy_quality_guardrails.py -q`

### Risks and remaining limitations
This patch does not tune LOW_SCORE or symbol selector thresholds. Shadow-outcome completeness still depends on available rejected-shadow evidence in generated artifacts. Manual BTCUSDT 30d/1h validation should be rerun in an environment with market data access.

### Migration concerns
None for SQLite. CSV consumers should tolerate additive columns.

### Push recommendation
Push after full targeted and complete pytest runs pass.

## 2026-07-01 PR255 follow-up: HIGH_VOL_GUARD correction and zero-accepted root-cause audit

### Why the patch was needed
PR255 added HIGH_VOL_GUARD diagnostics, but effective-RR breaches could still show a misleading zero counterfactual penalty. The latest BTCUSDT 30d/1h manual artifact showed 718 candidates, 0 accepted, 718 rejected, 203 symbol-level rejects, and 515 signal-level rejects, with LOW_SCORE=480, TOO_CHOPPY=170, WEAK_TREND_AND_NO_RANGE_EDGE=33, HIGH_VOL_GUARD=20, and STOP_TOO_WIDE=15.

### Root cause
The HIGH_VOL_GUARD diagnostic used generic volatility metric naming and computed the counterfactual penalty as an above-threshold excess. That is correct for maximum-style metrics but wrong for minimum effective-RR breaches, where the meaningful gap is `max(0, high_vol_min_effective_rr - effective_rr)`.

### Files changed
- `backtest_order.py`: corrected HIGH_VOL_GUARD diagnostics, added LOW_SCORE diagnostics, symbol-level reject diagnostics, and zero-accepted root-cause summaries.
- `src/alphaforge/dashboard/backtest_control.py`: reads new summary artifacts and exposes artifact paths safely when present.
- `src/alphaforge/dashboard/templates/overview.html`: surfaces compact summary dictionaries and artifact paths, not raw CSV rows.
- `tests/test_strategy_quality_guardrails.py`: adds regressions for corrected gaps/triggers, protective verdicts, LOW_SCORE/symbol diagnostics, and bottleneck reporting.

### Runtime behavior changes
Default BACKTEST acceptance remains conservative. No thresholds were loosened, no candidates are force accepted, and PAPER/LIVE behavior is unchanged. The new counterfactual fields are diagnostic-only evidence.

### Exact HIGH_VOL_GUARD corrected diagnostic logic
For effective-RR guard breaches, `effective_rr_gap_to_threshold = max(0, high_vol_min_effective_rr - effective_rr)` and `counterfactual_effective_rr_gap` uses the same value. `counterfactual_volatility_penalty` now mirrors the correct guard gap instead of reporting zero for below-minimum effective RR. Rows also export `guard_metric_name`, `guard_metric_value`, `guard_threshold`, `guard_gap_to_threshold`, `guard_breach_direction`, high-vol context source, trigger classification, pass/fail booleans, cost penalty fields, and explicit diagnostic-only warning text.

### Latest BTCUSDT 30d/1h observed summary
The observed artifact had `total_candidates=718`, `accepted_count=0`, `rejected_count=718`, `symbol_rejected_count=203`, and `signal_rejected_count=515`. Canonical rejects were LOW_SCORE=480, TOO_CHOPPY=170, WEAK_TREND_AND_NO_RANGE_EDGE=33, HIGH_VOL_GUARD=20, and STOP_TOO_WIDE=15. HIGH_VOL_GUARD rows had effective RR around 1.37-1.65 versus a 2.30 threshold, 12 baseline counterfactual passes, 8 secondary failures, and zero rows within ±5% of threshold.

### Why HIGH_VOL_GUARD is not the primary zero-accepted bottleneck
HIGH_VOL_GUARD accounts for only 20 of 718 rejects. Its observed effective-RR breaches are not marginal, and relaxing the guard would not address the dominant LOW_SCORE and symbol-level market-structure rejects. HIGH_VOL_GUARD is currently classified as VALID_PROTECTIVE_GUARD for BTCUSDT 30d/1h because effective RR breaches are not marginal. The primary zero-accepted bottleneck is LOW_SCORE, followed by choppy/weak-trend symbol-level rejects. No production threshold relaxation is recommended without stronger counterfactual evidence.

### LOW_SCORE and symbol-level reject audit plan/results
The exporter now writes `low_score_diagnostics.csv` and `low_score_summary.json` with score gaps, threshold evidence, pass/fail execution-quality booleans, and counterfactual reject reasons. It also writes `symbol_reject_diagnostics.csv` and `symbol_reject_summary.json` for TOO_CHOPPY and WEAK_TREND_AND_NO_RANGE_EDGE rows, including threshold/gap fields, source function, interval sensitivity, and future-leakage risk explanation. `zero_accepted_root_cause_summary.json/csv` unifies counts, reject distribution, verdicts, bottlenecks, and recommendation.

### Lifecycle changes
None. Rejected decisions remain rejected; lifecycle state semantics are unchanged.

### Persistence changes
No SQLite migration. New artifacts are additive CSV/JSON exports.

### Export/schema changes
BACKTEST output gains additive HIGH_VOL_GUARD fields, LOW_SCORE artifacts, symbol-reject artifacts, and zero-accepted summary artifacts. Existing CSV readers should tolerate additional metrics.

### Tests added/executed
Added tests for corrected high-vol effective-RR gaps, stop-too-wide trigger labeling, protective verdicts, diagnostic-only behavior, LOW_SCORE rows/summaries, symbol reject diagnostics, missing metric verdicts, and zero-accepted root-cause reporting.

### Risk assessment
The diagnostics depend on fields available in rejected artifacts; unavailable ATR/realized-volatility/candle-range values are exported as `UNAVAILABLE_BACKTEST` rather than fabricated. Symbol feature source safety is reported as unknown where the artifact cannot independently prove lookback safety.

### Remaining limitations
Manual BTCUSDT 30d/1h regeneration was not completed in this patch cycle. Shadow outcome/drawdown evidence is still needed before any threshold audit could become a tuning proposal.

### Migration concerns
No migration required. Downstream dashboard consumers should read summary JSON where available and treat raw diagnostics as audit artifacts.

### Recommendation
Keep HIGH_VOL_GUARD enabled. Do not relax thresholds based on current evidence. Continue audit on LOW_SCORE and symbol-level market-structure rejects.

### Push recommendation
Safe to push after full requested pytest validation passes. Do not claim LIVE readiness.

## 2026-07-01 HIGH_VOL_GUARD / zero-accepted root-cause diagnostics

### Why the patch was needed
A BTCUSDT 30d/1h BACKTEST run was internally consistent after PR254 but still produced `accepted_count=0`. The remaining audit gap was whether the 20 `HIGH_VOL_GUARD` rows were legitimate execution-protective rejects or an over-strict/mis-scaled guardrail.

### Root cause / source audit
`HIGH_VOL_GUARD` is emitted by `backtest_order._guardrail_rejection_reason`. It is a BACKTEST strategy-quality guardrail, not PAPER/LIVE order-path logic. The guard is active when `StrategyQualityGuardrailConfig.enabled` and `high_vol_acceptance_guard` are true and `_is_high_vol_context(...)` detects `HIGH`, `VOL`, or `BREAKOUT` in the regime/volatility context. It rejects high-vol candidates when either effective RR is below `high_vol_min_effective_rr` or a previously softened wide stop is present. A separate high-vol cost breach emits `HIGH_VOL_EXECUTION_COST`, and high-vol daily saturation emits `HIGH_VOL_OVERTRADE`.

### Exact config/env variables used
- `ALPHAFORGE_BACKTEST_STRATEGY_GUARDRAILS_ENABLED` controls the strategy-quality guardrail family.
- `ALPHAFORGE_BACKTEST_HIGH_VOL_ACCEPTANCE_GUARD` controls the HIGH_VOL acceptance guard.
- The default `high_vol_min_effective_rr` threshold is `2.30` effective RR.
- The default `high_vol_max_cost_penalty` threshold is `0.18` total cost penalty.
- The default `high_vol_max_trades_per_day` threshold is `2` accepted high-vol trades/day.

### Runtime behavior changes
Default BACKTEST remains conservative. No production threshold was loosened and no rejected candidate is force-accepted. Diagnostic profiles `HIGH_VOL_GUARD_OFF_DIAGNOSTIC` and `VOL_GUARD_RELAXED_DIAGNOSTIC` bypass this BACKTEST-only strategy guardrail for measurement and are labeled diagnostic-only. The warning text is: `HIGH_VOL_GUARD_OFF_DIAGNOSTIC is not a production strategy profile. It measures guardrail impact only.`

### Lifecycle changes
No lifecycle transition semantics changed. HIGH_VOL_GUARD rows remain `SIGNAL_REJECTED`; counterfactual acceptance is exported as diagnostics only.

### Persistence / export / schema changes
No database migration is required. New additive artifacts are exported per BACKTEST run: `high_vol_guard_diagnostics.csv`, `high_vol_guard_summary.json`, `acceptance_funnel.csv`, and `acceptance_funnel.json`. `backtest_quality_summary.csv` gains additive HIGH_VOL_GUARD count/verdict/evidence/recommendation fields.

### Latest diagnostic artifact summary
The code now exports per-row HIGH_VOL_GUARD evidence including score, raw/effective RR, expectancy bucket, metric name/value/threshold/ratio, ATR/realized-volatility/candle-range fields when available, spread/slippage/funding/liquidity, pass/fail filter lists, would-accept-without-guard, counterfactual reason/effective RR/penalties, stop distance, and source function. The real BTCUSDT 30d/1h artifact must be regenerated to fill these fields for the observed 20 rows.

### Acceptance funnel summary
The new canonical funnel shows total candidates, symbol-level rejects, signal-created candidates, signal-level rejects, per-reason reject counts, HIGH_VOL_GUARD counterfactual would-accept count, accepted-before-guardrails, accepted-after-guardrails, and position-open/closed proxies.

### Counterfactual summary
Counterfactual fields are BACKTEST-only diagnostics. They quantify guardrail impact but do not mutate default accepted trades. PAPER/LIVE behavior is not weakened.

### Risk assessment
The HIGH_VOL_GUARD verdict defaults to protective unless diagnostics show candidates pass non-volatility filters and the threshold breach is marginal. Missing volatility fields are exported as unavailable rather than silently fabricating ATR or realized-volatility values.

### Recommendation
Regenerate BTCUSDT 30d/1h artifacts and inspect `high_vol_guard_verdict`, `high_vol_guard_evidence`, `acceptance_funnel.csv`, and `high_vol_guard_diagnostics.csv`. Do not relax HIGH_VOL_GUARD unless counterfactual rows pass score/RR/effective-RR/expectancy/spread/slippage/liquidity checks and drawdown/adverse-excursion evidence remains controlled.

### Tests added/executed
Added tests for HIGH_VOL_GUARD diagnostic fields, no emission below threshold, diagnostic profile labeling/no default mutation, and acceptance-funnel reconciliation.

### Migration concerns
None; artifacts/summary fields are additive CSV/JSON outputs.

### Push recommendation
Push after full test suite and manual backtest regeneration complete successfully.

## 2026-07-01 - BACKTEST quality summary reject-count parity surgery report

### Why this patch was needed
A BTCUSDT 30d/1h BACKTEST artifact showed `backtest_quality_summary.csv.rejected_count=518` while the same file's canonical reject distributions, `order_backtest_summary.csv`, and `rejected_orders.csv` all represented 718 rejected rows.

### Root cause
`build_backtest_quality_summary` kept using signal-created lifecycle rows for the top-level `rejected_count` while its canonical distributions were correctly sourced from `rejected_orders.csv`, which also includes pre-signal `SYMBOL_REJECTED` rows.

### Files changed
- `backtest_order.py`: makes quality-summary `rejected_count` and `canonical_rejected_count` use canonical rejected totals, while exporting explicit signal-only and symbol-selector counts.
- `src/alphaforge/dashboard/backtest_control.py`: makes overall dashboard BACKTEST rejection rate prefer canonical rejected artifact rows when present.
- `tests/test_backtest_order_scanner.py`: adds canonical total and split-count regressions.
- `tests/test_dashboard_app.py`: adds dashboard rejection-rate regression for canonical rejected rows.
- `VERSION.md`, `REPORT.md`, `CHANGELOG.md`: document behavior, compatibility, tests, and risks.

### Runtime behavior changes
No trading filters, acceptance thresholds, lifecycle progression, or PAPER/LIVE runtime behavior changed. Only BACKTEST artifact accounting and dashboard display accounting changed.

### Lifecycle changes
None. `SIGNAL_REJECTED` and `SYMBOL_REJECTED` remain separate lifecycle states; the quality summary now exports both split counts explicitly.

### Persistence changes
No SQLite schema migration. CSV quality-summary metrics are additive except that `rejected_count` is corrected to canonical overall rejected rows.

### Export/schema changes
`backtest_quality_summary.csv` now includes `signal_rejected_count`, `symbol_rejected_count`, and `canonical_rejected_count`; `rejected_count` equals the canonical total and the sum of `canonical_reject_reason_distribution`.

### Tests added
Added regressions for quality-summary canonical rejected total, signal/symbol split metrics, distribution-total parity, and dashboard overall reject-rate canonical counting.

### Tests executed
- `python -m pytest tests -k "backtest or quality or reject or dashboard" -q`
- `python -m pytest -q`
- `python backtest_order.py --interval 1h --last-n-days 30 --symbols BTCUSDT --output-dir data/backtests/manual_btcusdt_30d_1h_pr251_followup --force-refresh` (blocked by proxy tunnel 403 before artifacts were generated)

### Risks
Downstream consumers that interpreted `backtest_quality_summary.csv.rejected_count` as signal-only should switch to `signal_rejected_count`. The corrected `rejected_count` now matches canonical rejected artifacts.

### Remaining limitations
Manual BTCUSDT 30d/1h validation was attempted but Binance historical fetch was blocked by proxy tunnel 403 before artifacts were generated.

### Migration concerns
No database migration required. CSV readers should tolerate the new additive metrics.

### Push recommendation
Safe to push after requested pytest validation. Do not claim LIVE readiness.

## 2026-07-01 - BACKTEST post-PR251 artifact consistency surgery report

### Why this patch was needed
A real BTCUSDT 30d/1h BACKTEST artifact showed `backtest_quality_summary.csv` reporting a different reject truth than `rejected_orders.csv` and `order_backtest_summary.csv`, while pre-signal `SYMBOL_REJECTED` rows used `UNKNOWN` expectancy and fake numeric zero RR/effective-RR values. The same run package also contained a stale ETHUSDT candle JSON despite a BTCUSDT-only symbol universe.

### Root cause
The quality summary rebuilt reject reasons from lifecycle/gate diagnostics after attribution had already produced canonical `rejected_orders.csv` reasons. Symbol-selector rejects reused order-geometry fields even though order geometry is not applicable before the selector passes. Candle files were stored directly in the run artifact directory without pruning stale symbol files before a narrowed symbol run.

### Files changed
- `backtest_order.py`: separates raw and canonical reject distributions, writes canonical quality summary distributions from rejected-order rows, marks `SYMBOL_REJECTED` RR/effective-RR/expectancy as unavailable/not applicable, persists availability flags, and prunes stale candle JSON artifacts.
- `tests/test_backtest_order_scanner.py`: adds quality-summary canonical distribution, symbol-selector availability, and stale candle pruning regressions.
- `tests/test_backtest_profile_comparison.py`: adds dashboard regression proving top rejection reasons come from canonical rejected-order artifacts, not raw quality diagnostics.
- `VERSION.md`, `REPORT.md`, `CHANGELOG.md`: document behavior, compatibility, tests, and remaining risks.

### Runtime behavior changes
No trade acceptance thresholds, score logic, lifecycle transitions, or PAPER/LIVE runtime decisions changed. BACKTEST artifact generation now treats `rejected_orders.csv` as the canonical post-attribution reject source when building quality summaries.

### Lifecycle changes
`SYMBOL_REJECTED` remains a pre-signal symbol-selector lifecycle state. Exported context now identifies `source_stage=SYMBOL_SELECTOR`, `expectancy_bucket=NOT_APPLICABLE_SYMBOL_FILTER`, and false availability flags for RR/effective-RR/expectancy.

### Persistence changes
Lifecycle persistence now carries source-stage and availability diagnostics through the execution context projection. No SQLite schema migration is required because the fields are stored in existing JSON context and selected into CSV artifacts.

### Export/schema changes
`backtest_quality_summary.csv` includes explicit `canonical_reject_reason_distribution` and `raw_gate_reject_reason_distribution`; legacy `reject_reason_distribution` now mirrors canonical post-attribution reasons when canonical rejected rows are supplied. `SYMBOL_REJECTED` rows may now have blank RR/effective-RR values with `rr_available=false` and `effective_rr_available=false`.

### Tests added
Added regressions for canonical reject distribution parity, symbol-selector not-applicable expectancy/RR semantics, stale candle artifact pruning, and dashboard canonical reject reason use.

### Tests executed
- `python -m pytest tests -k "backtest or lifecycle or reject or dashboard or quality" -q`

### Risks
Downstream CSV readers that coerce blank RR/effective-RR to zero need to honor the new availability fields. This is intentionally more honest than fake zeros.

### Remaining limitations
Manual BTCUSDT 30d/1h validation was attempted, but Binance historical fetch failed with proxy tunnel 403 before artifacts could be generated. Historical spread remains estimated unless supplied by the data source.

### Migration concerns
No database migration required. Artifact consumers should prefer `canonical_reject_reason_distribution`; use `raw_gate_reject_reason_distribution` only for pre-attribution diagnostics.

### Push recommendation
Safe to push after full pytest validation. Do not claim LIVE readiness.

## 2026-07-01 - BACKTEST symbol-list parsing hardening surgery report

### Why this patch was needed
PowerShell multi-symbol BACKTEST runs could collapse `BTCUSDT,ETHUSDT` into a single space-separated string that was URL-encoded as `BTCUSDT+ETHUSDT`, causing Binance kline/funding endpoints to reject the request as an invalid symbol.

### Root cause
`scripts/run_backtest.ps1` accepted `-Symbols` as a scalar string, while the Python CLI only split symbols on commas. When PowerShell collapsed a comma expression into whitespace, Python treated the combined value as one symbol and the historical fetch path did not defensively validate impossible symbol tokens before building Binance URLs.

### Files changed
- `scripts/run_backtest.ps1`: accepts `Symbols` as a string array and forwards a comma-joined value to Python.
- `src/alphaforge/symbols.py`: adds shared symbol normalization and single-symbol validation.
- `backtest_order.py`: uses shared normalization for CLI fixed-symbol state and historical universe selection.
- `src/alphaforge/historical_market_data.py`: rejects invalid single-symbol fetches before kline/funding requests.
- `src/alphaforge/dashboard/backtest_control.py`: uses shared normalization for dashboard form symbols.
- `tests/test_historical_market_data.py` and `tests/test_dashboard_backtest_dynamic_universe.py`: add symbol parser/fetch/dashboard regressions.

### Runtime behavior changes
BACKTEST now accepts comma-separated, quoted comma-separated, whitespace-separated, PowerShell array, and dashboard comma-separated symbol inputs, normalizes them to uppercase unique symbols, and fails early on invalid tokens such as `BTCUSDT+ETHUSDT`. Strategy logic, lifecycle logic, filters, and thresholds were not changed.

### Lifecycle changes
None. This patch only prevents malformed symbol inputs from reaching historical data fetches.

### Persistence changes
None. No schema or CSV persistence changes.

### Export/schema changes
None.

### Tests added
Added coverage for comma-separated symbols, quoted/list symbol values, whitespace-separated accidental input, plus-sign rejection before fetch, dashboard parsing, and single-symbol preservation through existing parser/fetch tests.

### Tests executed
- `python -m pytest tests -k "symbol or backtest or dashboard" -q`
- `python -m pytest -q`

### Risks
The validator intentionally rejects symbols containing punctuation outside uppercase alphanumeric Binance-style tokens. If Binance lists a future futures symbol requiring other characters, the validator will need an explicit compatibility update before use.

### Remaining limitations
Manual PowerShell validation cannot be executed in this Linux container; regression coverage verifies the Python normalization and fetch guard.

### Migration concerns
No migration required. Invalid existing automation inputs must be corrected to comma/whitespace-separated symbols.

### Push recommendation
Safe to push after full validation. Do not claim LIVE readiness.

## 2026-07-01 - BACKTEST lifecycle realism evidence completion surgery report

### Why this patch was needed
BACKTEST readiness evidence must prioritize decision quality before trade-result PnL. The previous patch only tightened labels and documentation; it did not produce deterministic artifact evidence proving canonical lifecycle rows, variable score/RR, rejected CSV parity, SQL-before-export persistence, or dashboard/profile artifact consistency.

### Root-cause audit answers
1. `score` was historically fixed at `0.8` by placeholder fixtures/older diagnostic paths; the current scanner builds score in `backtest_order.py:_build_market_ctx` from breakout strength and candle range, then passes it through `src/alphaforge/order.py` via `run_order_cycle`. The new deterministic artifact test proves generated `SIGNAL_CREATED` rows have more than one score value.
2. `rr` was historically fixed at `2.0` by placeholder fixtures/older diagnostics; the current adapter computes RR from entry/stop/target geometry in `backtest_order.py:_build_market_ctx` unless a test fixture explicitly supplies fixed RR. The new artifact test proves generated `SIGNAL_CREATED` rows have more than one RR value.
3. Rejected signals/orders were missing when callers only exported simulated trade results. `backtest_order.py:process_backtest_result`, `_persist_lifecycle_rows`, and the CLI export now preserve rejected decisions; the follow-up patch also writes `rejected_signals.csv` as a compatibility alias beside `rejected_orders.csv`.
4. Lifecycle previously appeared to start at `CREATED` because dashboard/summary compatibility fields collapsed candidate rows. Canonical BACKTEST rows now start with `SIGNAL_CREATED`; the artifact test asserts every signal's first lifecycle row is `SIGNAL_CREATED` and that no lifecycle state equals `CREATED`.
5. `expectancy_bucket` was `UNKNOWN` when expectancy was absent because `_bucket_expectancy(None)` returned `UNKNOWN`. Missing expectancy now exports `EXPECTANCY_UNAVAILABLE`; the artifact test asserts the missing-expectancy rejected signal is not `UNKNOWN`.
6. Execution/context fields were zero-looking when old code or fixtures defaulted missing values. Current lifecycle rows default to `UNAVAILABLE_BACKTEST`, and the artifact test asserts missing spread/slippage/funding/volume export as `UNAVAILABLE_BACKTEST`, not `0.0`.
7. BACKTEST is not intended to use live calls for decision validation. It uses the shared order-cycle semantics for quality gates and an offline historical adapter for market/execution context.
8. Older artifact paths could behave like trade-result simulators only. The current path emits decision lifecycle states before terminal PnL and persists rejected decisions as evidence; the fixture contains accepted paths plus `SIGNAL_REJECTED` and `ORDER_REJECTED`.
9. Safe PAPER/LIVE reuse without live exchange calls: order quality evaluation, reject attribution, lifecycle contract normalization, persistence schema, effective-RR cost model, and dashboard artifact parsing. Unsafe to reuse directly: live orderbook fetches, live placement, account/balance reconciliation, and exchange heartbeat/order status polling.

### Code locations changed
- `backtest_order.py:_primary_reject_reason_from_context`: canonicalizes missing execution-context rejects to `EXECUTION_CONTEXT_UNAVAILABLE`.
- `backtest_order.py:_bucket_expectancy`: maps missing/unknown/unavailable expectancy evidence to `EXPECTANCY_UNAVAILABLE`.
- `backtest_order.py` CLI artifact writer: exports `rejected_signals.csv` with the same canonical rejected rows as `rejected_orders.csv`.
- `tests/test_backtest_order_scanner.py`: adds a deterministic fixture artifact test for lifecycle counts, score/RR variability, reject distribution, unavailable execution context, SQL/export parity, and effective-RR rejection.
- `tests/test_backtest_profile_comparison.py`: adds dashboard main-panel vs profile-comparison canonical artifact consistency coverage.

### Deterministic fixture artifact evidence
Generated inside `test_fixture_backtest_artifacts_prove_lifecycle_rejects_variability_and_sql_export`:

| Lifecycle state | Count | Evidence meaning |
|---|---:|---|
| SIGNAL_CREATED | 4 | every candidate starts canonically before decision/result |
| WAITING_ENTRY_ZONE | 2 | accepted candidates enter pre-fill lifecycle |
| ENTRY_TRIGGERED | 2 | historical price reaches entry |
| ORDER_PLACED | 2 | simulated order placement is represented |
| POSITION_OPENED | 2 | historical fill/open state is represented |
| POSITION_CLOSED | 2 | terminal TP/SL close happens after pre-trade states |
| SIGNAL_REJECTED | 1 | low-score rejected candidate is first-class lifecycle evidence |
| ORDER_REJECTED | 1 | missing execution-context order reject is first-class lifecycle evidence |

Additional fixture distributions:
- Score distribution evidence: `SIGNAL_CREATED` rows contain scores `{8.4, 6.9, 2.1, 8.0}`, so `nunique > 1`.
- RR distribution evidence: `SIGNAL_CREATED` rows contain RR `{2.2, 1.4, 1.1, 1.7}`, so `nunique > 1`.
- Reject reason distribution evidence: rejected artifacts contain `LOW_SCORE=1` and `EXECUTION_CONTEXT_UNAVAILABLE=1`; no global `UNKNOWN`.
- Expectancy evidence: missing expectancy exports `EXPECTANCY_UNAVAILABLE`, not `UNKNOWN`.
- Execution context availability evidence: missing spread/slippage/funding/volume exports `UNAVAILABLE_BACKTEST`, not fake `0.0`.
- SQL-before-export evidence: the fixture writes `order_lifecycle.csv` from `_persist_lifecycle_rows(...)` output, then `verify_export_integrity(...)` validates persisted lifecycle rows against exported rejected rows.

### Dashboard/parser evidence
`test_dashboard_main_and_profile_comparison_use_same_canonical_profile_artifacts` builds one selected `profiles/DEFAULT_FILTERS` artifact directory and asserts both `_apply_backtest_artifact_model(...)` and `_comparison_metrics(...)` read the same `order_backtest_summary.csv`, `order_lifecycle.csv`, `backtest_orders.csv`, and `rejected_orders.csv` values for accepted count, rejected count, rejection rate, win/loss, net PnL, and top reject reasons.

### Runtime behavior changes
BACKTEST attribution is stricter and more auditable: missing expectancy/context is visible evidence, not hidden as generic unknown/zero values. Rejected signals are exported through both `rejected_orders.csv` and `rejected_signals.csv`. No filters were loosened and no rejected candidate is force-accepted.

### Lifecycle changes
Canonical decision lifecycle evidence is tested in generated artifacts: accepted paths include pre-trade states before `POSITION_CLOSED`, and rejected paths include `SIGNAL_REJECTED`/`ORDER_REJECTED`.

### Persistence changes
No schema migration. The fixture proves rejected lifecycle decisions are persisted into SQLite via `_persist_lifecycle_rows(...)` before CSV export parity is checked.

### Export/schema changes
Added `rejected_signals.csv` as an additive compatibility export containing the same rejected decision rows as `rejected_orders.csv`. Existing `expectancy_bucket` and `reject_reason` values may be more specific.

### Tests proving acceptance criteria
- Lifecycle/export/SQL/score/RR/reject/context: `test_fixture_backtest_artifacts_prove_lifecycle_rejects_variability_and_sql_export`.
- Effective-RR penalties and rejection: `test_effective_rr_penalties_can_reject_below_threshold`.
- Missing execution-context reason: `test_backtest_unknown_reject_reason_attributed_missing_execution_context`.
- Missing expectancy reject attribution: `test_backtest_unknown_reject_reason_attributed_missing_expectancy_when_required`.
- Dashboard/profile canonical artifact consistency: `test_dashboard_main_and_profile_comparison_use_same_canonical_profile_artifacts`.

### Tests executed
- `python -m pytest tests/test_backtest_order_scanner.py -q`
- `python -m pytest tests/test_dashboard_app.py -q`
- `python -m pytest tests/test_backtest_profile_comparison.py -q`
- `python -m pytest tests -k "backtest or lifecycle or dashboard or reject" -q`
- `python -m pytest -q`

### Risks
Older consumers that hardcode `UNKNOWN` or `MISSING_EXECUTION_CONTEXT` may need to accept the explicit new labels. Historical spread is still an estimate unless actual spread is supplied. `rejected_signals.csv` is additive and intentionally duplicates canonical rejected rows for consumers expecting signal-named diagnostics.

### Remaining limitations
BACKTEST remains historical simulation, not LIVE execution. Actual historical orderbook depth/spread is unavailable unless supplied by artifacts, and missing fields must remain unavailable rather than zero-filled. Rejected trades are first-class evidence; a high reject rate can be healthy when filters are selective.

### Migration concerns
No SQL migration. Regenerate CSV/JSON artifacts to see `EXPECTANCY_UNAVAILABLE`, `EXECUTION_CONTEXT_UNAVAILABLE`, and the additive `rejected_signals.csv` export.

### Push recommendation
Safe to push after full validation. Do not claim LIVE readiness.

## 2026-07-01 - BACKTEST reject reason attribution surgery report

### Why this patch was needed
Latest BACKTEST diagnostics showed 516/516 candidates rejected with every exported reject reason collapsed to `UNKNOWN`, obscuring whether rejects came from weak effective RR, expectancy, missing execution evidence, score, or regime/setup alignment.

### Root cause
The shared order gate already emitted diagnostics such as `effective_rr`, thresholds, failed gates, and expectancy status, but the BACKTEST handoff trusted `result.reason`/`result.reject_reason` first. When that field was `UNKNOWN`, exported `rejected_orders.csv` and dashboard summary distributions could remain unclassified instead of deriving the first concrete blocking cause from diagnostics and execution context.

### Files changed
- `backtest_order.py`: added concrete reject attribution from diagnostics/market context, preserves primary and secondary reject reasons, applies attribution before rejected lifecycle/CSV export, and adds threshold diagnostics to quality summaries.
- `src/alphaforge/order.py`: exports threshold settings in decision diagnostics so BACKTEST attribution can separate raw RR from effective RR failures at export time.
- `tests/test_backtest_order_scanner.py`: added reject attribution and rejected CSV preservation tests.
- `CHANGELOG.md`, `VERSION.md`, `REPORT.md`: documented behavior, compatibility, risks, and validation.

### Runtime behavior changes
Rejected BACKTEST candidates now preserve the first concrete cause when the runtime status reason is unknown. Filters are not loosened and no rejected candidate is forced into acceptance.

### Lifecycle changes
`SIGNAL_REJECTED` rows now carry a concrete reject reason when diagnostics identify one. Lifecycle ordering is unchanged.

### Persistence changes
No database migration. CSV/lifecycle artifacts gain better reject reason values and optional `secondary_reject_reasons` export data.

### Export/schema changes
`rejected_orders.csv` preserves `reject_reason` plus `secondary_reject_reasons`. BACKTEST quality summary diagnostics include `thresholds_used` with `min_score`, `min_raw_rr`, `min_effective_rr`, `reject_unknown_expectancy`, and `require_execution_context`.

### Tests added
Added focused regressions for low effective RR, negative expectancy, missing expectancy, missing execution context, non-UNKNOWN reject distributions, and rejected CSV preservation.

### Tests executed
- `pytest -q tests/test_backtest_order_scanner.py tests/test_phase123_foundations.py tests/test_backtest_paper_pre_submit_parity.py`

### Risks
Dashboard consumers may see `LOW_EFFECTIVE_RR` instead of older `RR_TOO_LOW` for execution-adjusted failures. This is intended attribution tightening, not strategy loosening.

### Remaining limitations
`UNKNOWN` is still possible when no concrete diagnostic, threshold, expectancy, score, regime, or execution-context failure can be determined.

### Migration concerns
No schema migration required. CSV consumers should tolerate the additional optional `secondary_reject_reasons` column.

### Push recommendation
Safe to push after full `pytest -q` passes. Do not claim LIVE readiness.

## 2026-07-01 - Dashboard guardrail section rendering regression surgery report

### Why this patch was needed
A regression test for `/backtest/run` expected the rendered HTML to include the exact `Strategy Quality Guardrails` heading when guardrail data exists, but the template only rendered guardrail rows inside the generic result table.

### Root cause
The guardrail data fields were present on `DashboardBacktestResult`, but the template no longer exposed a dedicated, searchable guardrail section heading.

### Files changed
- `src/alphaforge/dashboard/templates/overview.html`: restored a dedicated `Strategy Quality Guardrails` section gated by guardrail breakdown, top reasons, or representative examples.
- `CHANGELOG.md`, `REPORT.md`: documented the regression and validation.

### Runtime behavior changes
None. This is a dashboard/template rendering patch only.

### Lifecycle changes
None.

### Persistence changes
None.

### Export/schema changes
None. Existing result dataclass fields and artifact keys are unchanged.

### Tests added
No new test was needed; the existing regression test is preserved and targets this rendering behavior.

### Tests executed
- `pytest -q tests/test_backtest_profile_comparison.py::test_dashboard_renders_guardrail_breakdown_when_source_data_exists`
- `pytest -q`

### Risks
Low. The new section is hidden unless guardrail source data is present.

### Remaining limitations
Dashboard rendering depends on artifacts/result fields being populated upstream; this patch does not alter reporting metrics or strategy decisions.

### Migration concerns
None.

### Push recommendation
Safe to push after tests pass. Do not claim LIVE readiness.

## 2026-07-01 - Dashboard BACKTEST accepted-count and guardrail attribution surgery report

### Why this patch was needed
The latest profile-comparison artifact still showed DEFAULT_FILTERS/STRICT_FILTERS/CUSTOM_CURRENT_UI with 12,228 accepted trades and `OVERTRADE_RISK`, while the selected DEFAULT_FILTERS backtest summary reported zero accepted trades, zero outcomes, zero PnL, and unavailable accepted diagnostics.

### Root cause
Previous reporting fixes did not fully protect every dashboard comparison/fallback path from lifecycle/event rows and diagnostic rows. Guardrail/later-gate evidence was exported but dashboard attribution could remain empty, and the fallback gate funnel could show zero rejects despite canonical reject reasons being present.

### Files changed
- `src/alphaforge/dashboard/backtest_control.py`: added artifact-derived guardrail attribution fallback, canonical rejection gate-funnel fallback, and tests covering no-trade/overtrade warning behavior through comparison metrics.
- `backtest_order.py`: removed `SIGNAL_CREATED` accepted-count fallback from default gate-funnel construction and expanded gate-funnel CSV fields for exported diagnostic columns.
- `tests/test_backtest_profile_comparison.py`: added required zero-summary/12,228 lifecycle fixture, exact ALL_FILTERS_OFF fixture, guardrail attribution/funnel fixture, and dashboard rendering fixture.
- `CHANGELOG.md`, `VERSION.md`, `REPORT.md`: documented reporting-only behavior, risks, and validation.

### Runtime behavior changes
None to strategy logic, filter thresholds, scoring, PAPER, or LIVE behavior. The patch changes BACKTEST dashboard/reporting metrics only.

### Lifecycle changes
No lifecycle transitions changed. Lifecycle event rows remain visible as diagnostics but are not accepted trades.

### Persistence changes
No database migration. Existing CSV/JSON artifacts are read more conservatively; `default_gate_funnel.csv` export field coverage is expanded to include existing diagnostic columns without changing trade persistence semantics.

### Export/schema changes
Dashboard fallback guardrail sections can now populate `guardrail_reject_breakdown`, `top_guardrail_reject_reasons`, and representative examples from later-gate/rejection artifacts when `strategy_quality_guardrails.json` is absent or incomplete.

### Tests added
Added regressions proving summary `accepted_count=0` wins over 12,228 lifecycle rows, lifecycle events are not trades, average trades/day uses executed count only, no-trade profiles do not raise `OVERTRADE_RISK`, ALL_FILTERS_OFF exact executed values are preserved, guardrail breakdown populates from source data, and dashboard rendering does not show `Unavailable` when guardrail source data exists.

### Tests executed
- `pytest -q tests/test_backtest_profile_comparison.py -q`
- `pytest -q tests/test_strategy_quality_guardrails.py tests/test_backtest_profile_comparison.py -q`

### Risks
Downstream consumers may see lower accepted counts and lower avg trades/day where prior dashboards counted lifecycle events. This is intended and safer.

### Remaining limitations
Guardrail attribution uses exported evidence only; it does not synthesize fake fills or counterfactual PnL for rejected candidates.

### Migration concerns
No schema migration. Consumers should treat lifecycle row counts as diagnostics and accepted counts as summary/order/executed evidence only.

### Push recommendation
Safe to push after tests pass. Do not claim LIVE readiness.

## 2026-07-01 - Dashboard BACKTEST profile timeout handling surgery report

### Why this patch was needed
Dashboard `POST /backtest/run` could crash with an uncaught `subprocess.TimeoutExpired` during profile comparison, including invalid negative timeout values from exhausted time budgets.

### Root cause
Profile-comparison subprocess execution did not contain timeout failures per profile or persist timeout metadata before returning to the dashboard. A timed-out profile could therefore abort the whole request and hide completed profile artifacts.

### Files changed
- `src/alphaforge/dashboard/backtest_control.py`: added positive timeout validation, per-profile timeout handling, TIMEOUT profile metadata, PARTIAL comparison results, and leaderboard preservation.
- `src/alphaforge/dashboard/templates/overview.html`: renders PARTIAL results and profile statuses so completed profiles remain visible beside timed-out profiles.
- `tests/test_backtest_profile_comparison.py`: added timeout regression coverage for non-positive timeout rejection, partial results, TIMEOUT profile marking, artifact preservation, and dashboard rendering.
- `CHANGELOG.md`, `VERSION.md`, `REPORT.md`: documented behavior, risks, and validation.

### Runtime behavior changes
Profile comparison now uses a positive per-profile timeout and catches `TimeoutExpired` for the individual profile. The dashboard returns a controlled PARTIAL result with a user-readable warning instead of an uncaught ASGI traceback.

### Lifecycle changes
None. Completed profile lifecycle artifacts are preserved and selected DEFAULT_FILTERS evidence remains displayable if ALL_FILTERS_OFF times out.

### Persistence changes
No database migration. Timed-out profile artifact directories now receive `backtest_profile_metadata.json` with machine-readable BACKTEST mode, profile name, status `TIMEOUT`, failure reason, timeout seconds, and command.

### Export/schema changes
Profile comparison JSON now includes top-level `status` and per-profile `status`; leaderboard rows include `status`. Timed-out profile metrics are explicitly unavailable/null rather than fabricated.

### Tests added
Added regression tests for non-positive timeout rejection before `subprocess.run`, timeout-to-PARTIAL result conversion, profile-scoped TIMEOUT marking, completed output preservation, and DEFAULT_FILTERS rendering when ALL_FILTERS_OFF times out.

### Tests executed
- `pytest -q tests/test_backtest_profile_comparison.py`

### Risks
Downstream consumers of profile leaderboard CSV/JSON should tolerate the added `status` column/field and null metrics for timed-out profiles.

### Remaining limitations
A timed-out profile is not retried automatically; operators must rerun with a smaller universe/window or investigate the profile artifact directory.

### Migration concerns
No schema migration. Artifact readers should handle `PARTIAL` comparison status and per-profile `TIMEOUT`.

### Push recommendation
Safe to push after tests pass; do not claim LIVE readiness.

## 2026-06-30 - BACKTEST profile metric integrity surgery report

### Why this patch was needed
The latest dashboard artifact showed DEFAULT_FILTERS with `accepted_count=0` and zero orders in `order_backtest_summary.csv`, while root comparison/leaderboard outputs reported 12,221 accepted trades and `OVERTRADE_RISK`. That contradicted canonical order evidence and could rank a no-trade profile as strategy performance.

### Root cause
Profile comparison fell back to lifecycle-derived rows when summary values were zero or when lifecycle diagnostic exports contained many rows. That blurred lifecycle event count, rejected signal count, accepted trade count, and executed outcome count.

### Files changed
- `src/alphaforge/dashboard/backtest_control.py`: added canonical accepted-trade selection from summary/order evidence, no-trade handling, accepted distribution isolation, and leaderboard ranking safeguards.
- `backtest_order.py`: added guardrail reject reason breakdown/examples and gate-funnel comparability labels.
- `tests/test_backtest_profile_comparison.py`: added regression coverage for metric integrity and no-trade leaderboard behavior.
- `tests/test_strategy_quality_guardrails.py`: added guardrail explainability and gate-funnel labeling coverage.
- `CHANGELOG.md`, `VERSION.md`, `REPORT.md`: documented behavior, risks, and validation.

### Runtime behavior changes
BACKTEST dashboard/profile reporting now uses canonical executed trade evidence only. No trading gates were loosened and no PAPER/LIVE order path was changed.

### Lifecycle changes
Lifecycle exports remain intact. Reporting now explicitly separates lifecycle event count from accepted trade count and never counts `SIGNAL_CREATED`, `SIGNAL_REJECTED`, `SYMBOL_REJECTED`, or `ORDER_REJECTED` as accepted trades.

### Persistence changes
No database migration. CSV/JSON artifacts gain explanatory fields but existing core files remain in place.

### Export/schema changes
Profile comparison JSON includes `accepted_trades_source`, `lifecycle_event_count`, and `rejected_row_count`. Guardrail evidence includes `guardrail_reject_breakdown`, `top_guardrail_reject_reasons`, and `representative_guardrail_reject_examples`. Gate-funnel rows include scope/comparability notes.

### Tests added
Added tests for zero accepted count with 12k lifecycle rows, rejected lifecycle states not counted, accepted effective RR distribution count zero for no-trade profiles, no `OVERTRADE_RISK` from lifecycle rows, ALL_FILTERS_OFF executed count/outcomes/PnL, and default gate funnel comparability disclosure.

### Tests executed
- `pytest -q tests/test_backtest_profile_comparison.py -q`
- `pytest -q tests/test_strategy_quality_guardrails.py tests/test_backtest_profile_comparison.py -q`

### Risks
Existing downstream consumers may see accepted trade counts drop to zero where previous artifacts were inflated by lifecycle events. This is intended and safer.

### Remaining limitations
The patch does not replay guardrail-rejected candidates as trades, because that would create fake counterfactual PnL.

### Migration concerns
Consumers should read accepted counts from summary/order evidence and treat lifecycle row counts as diagnostics only.

### Push recommendation
Safe to push after tests pass; do not claim LIVE readiness.

## 2026-06-30 - Purpose-specific environment profiles surgery report

### Why this patch was needed
The single `.env.example` mixed BACKTEST diagnostics, PAPER evaluation, and LIVE preparation defaults, increasing the risk of mode confusion and threshold misuse.

### Root cause
Environment variables were meaningful but presented in one template without purpose-specific threshold tuning or copy guidance.

### Files changed
- `.env.example`: retained as safe medium PAPER-oriented default with profile pointers.
- `.env.test.example`: added loose BACKTEST/PAPER diagnostic profile marked NOT FOR LIVE.
- `.env.medium.example`: added balanced PAPER/default profile.
- `.env.live.example`: added hardened LIVE preparation profile with fail-closed real-order guards.
- `README.md`: documented profile purposes and copy commands for Windows PowerShell and macOS/Linux.
- `tests/test_env_example_profiles.py`: added profile validation coverage.
- `CHANGELOG.md`, `VERSION.md`, `REPORT.md`: operational documentation updates.

### Runtime behavior changes
None. This patch changes example configuration templates and validation tests only.

### Lifecycle changes
None. Lifecycle state transitions and persistence behavior are unchanged.

### Persistence changes
None. No database schema or CSV export contract changed.

### Export/schema changes
None.

### Tests added
Added tests for all four env examples existing, containing core variables, avoiding real-looking secrets, keeping LIVE stricter than TEST on critical thresholds, marking TEST as diagnostic/non-LIVE, and documenting all profiles in README.

### Tests executed
- `pytest tests/test_env_example_profiles.py -q`
- `pytest -q`

### Risks
Operators must still choose the right profile intentionally; LIVE examples remain templates and do not establish readiness evidence.

### Remaining limitations
The audit did not wire new variables because no cosmetic variables were added; existing reserved/unwired notes remain unchanged.

### Migration concerns
None for runtime code. Local users may copy the profile matching their workflow instead of the generic `.env.example`.

### Push recommendation
Push after tests pass; do not claim LIVE readiness.


## 2026-06-30 Strategy Quality Guardrails Surgery Report

### Why this patch was needed
Recent DEFAULT/DYNAMIC BACKTEST diagnostics showed excessive accepted trades, long loss streaks, score=10 saturation, STOP_TOO_WIDE softening leakage, and high-vol variance. Raw positive PnL alone was insufficient because score=10 no longer separated winners from losers and late daily-symbol caps hid weaker near-miss quality.

### Root cause
DEFAULT_FILTERS had evidence exports but lacked acceptance-time strategy-quality controls for same-day clusters, realized SL streaks, score saturation, and high-vol cost/variance.

### Files changed
- `backtest_order.py`: BACKTEST strategy-quality guardrail config, acceptance checks, explicit reject evidence, and profile-quality evidence exports.
- `.env.example`: documented guardrail and profile PASS/FAIL thresholds.
- `tests/test_strategy_quality_guardrails.py`: regression coverage.
- `CHANGELOG.md`, `VERSION.md`, `REPORT.md`: operational documentation.

### Runtime behavior changes
DEFAULT_FILTERS BACKTEST now rejects overactive daily/symbol/regime clusters, pauses after consecutive SLs, applies secondary checks to score>=9.8 candidates, and restricts high-vol acceptance. LIVE order placement is unchanged.

### Lifecycle changes
New guardrail rejections are persisted/exported as `SIGNAL_REJECTED` rows with `DAILY_TRADE_FREQUENCY_GUARD`, `LOSS_STREAK_PAUSE`, `SYMBOL_CLUSTER_GUARD`, `SCORE_SATURATION_GUARD`, `HIGH_VOL_GUARD`, `HIGH_VOL_OVERTRADE`, or `HIGH_VOL_EXECUTION_COST`.

### Persistence/export changes
Added `strategy_quality_guardrails.json/csv` plus summary fields for `profile_quality_status`, `profile_quality_reasons`, thresholds, and accepted before/after counts. No DB schema migration is required.

### Tests added/executed
Added unit tests for trade-frequency, loss-streak, score-saturation, high-vol, profile PASS/FAIL, diagnostic-only profile, and env coverage.

### Risks and limitations
Before-guardrail PnL/profit-factor/drawdown are exported as null because replaying rejected trades as accepted would create fake counterfactual performance. PAPER remains reject-heavy by design until calibrated evidence improves; this patch does not force PAPER trades.

### Migration concerns
Existing dashboards consuming `order_backtest_summary.csv` should tolerate appended columns. New guardrail reject reasons should be added to downstream reason allowlists if any exist.

### Push recommendation
Push after tests pass; do not claim LIVE readiness.

## 2026-06-30 - RejectedShadowEvaluation fixture alignment

### Why the patch was needed
CI reported `test_top_quality_improvement_note_explains_would_sl_dominance` failing because its direct `RejectedShadowEvaluation` constructor calls did not include newly required execution diagnostic fields.

### Root cause
The test fixture used named arguments for only the fields needed by the assertion and was not updated when `RejectedShadowEvaluation` required spread, liquidity, volatility, TP-hit, cost-penalty, and execution-ok diagnostics.

### Files changed
- `tests/test_dashboard_app.py`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime behavior changes
None. Production logic is unchanged.

### Lifecycle changes
None.

### Persistence changes
None.

### Export/schema changes
None.

### Tests added
No new test case; the existing dashboard fixture now uses a local helper with deterministic execution diagnostic values.

### Tests executed
- `pytest tests/test_dashboard_app.py::test_top_quality_improvement_note_explains_would_sl_dominance -q`
- `pytest tests/test_dashboard_app.py -q`
- `pytest -q`

### Risks
None beyond the dashboard module being import-skipped in environments missing optional dashboard dependencies.

### Remaining limitations
No production behavior was re-audited in this fixture-only change.

### Migration concerns
None.

### Push recommendation
Safe to push.

## 2026-06-30 - PR243/env DEFAULT_FILTERS overtrade audit diagnostics

### Why the patch was needed
After PR243/env changes, the comparable TOP 20 BACKTEST accepted count jumped from 11 to 354 and net expectancy turned negative. The dashboard did not make the gate-funnel failure, score=10 SL dominance, or missing drawdown obvious enough to prevent DEFAULT_FILTERS from looking strategy-quality.

### Root cause
Code audit found PR243 itself primarily changed dashboard dynamic-universe validation and selected artifact parsing, not the order gate. The acceptance jump is most consistent with existing environment/config behavior around STOP_TOO_WIDE softening and threshold calibration: score=10 saturation allowed many high-score wide/high-vol candidates to survive while STOP_TOO_WIDE disappeared from dominant reject reasons and DAILY_SYMBOL_TRADE_LIMIT became the later visible limiter. The patch records this as diagnostic evidence instead of changing strategy decisions.

### Files changed
- `backtest_order.py`
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_dashboard_app.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
No accepted/rejected decision behavior changed. BACKTEST now exports gate-funnel, equity-curve, and per-symbol/regime acceptance diagnostics and the dashboard shows blocking-level warnings for overtrade and score saturation risk.

### Lifecycle changes
No lifecycle states or transitions changed. Accepted terminal lifecycle rows are read in timestamp order for equity-curve and streak diagnostics only.

### Persistence changes
No SQLite schema or PAPER/LIVE persistence changes. BACKTEST CSV artifact set is extended with diagnostic-only files.

### Export/schema changes
Added optional BACKTEST exports: `equity_curve.csv`, `default_gate_funnel.csv`, and `symbol_regime_acceptance_diagnostics.csv`. `order_backtest_summary.csv` now includes drawdown/streak/profit-factor metrics and explicit return/net-PnL unit labels.

### Tests added
Added tests proving score saturation JSON renders in the dashboard table, overtrade and score saturation warnings fire, drawdown metrics are computed, STOP_TOO_WIDE and zero-reject gates are visible in the funnel, and WOULD_SL-dominated near misses do not become quality-improvement recommendations.

### Tests executed
- `pytest tests/test_dashboard_app.py -q` (skipped in this environment because optional dashboard deps were import-skipped)
- `pytest tests -q`
- `python -m py_compile backtest_order.py src/alphaforge/dashboard/backtest_control.py`

### Risks
This is diagnostic-only and does not fix the negative expectancy root calibration. If env leaves STOP_TOO_WIDE softening permissive or score=10 saturation uncalibrated, DEFAULT_FILTERS can still overtrade; the dashboard now flags that as not strategy-quality.

### Remaining limitations
The exact prior-run artifact was not present locally, so before/after attribution is based on git/env code audit and latest run metrics provided. Re-run the TOP 20 comparison to generate the new evidence artifacts.

### Migration concerns
No migration required. Consumers that parse summary CSVs should tolerate added columns.

### Push recommendation
Safe to push as diagnostic guardrails. Do not promote LIVE readiness.

## 2026-06-30 - BACKTEST dashboard dynamic top-volume universe validation

### Why the patch was needed
Leaving SYMBOLS blank with MAX SYMBOLS set failed dashboard validation even though the BACKTEST runner already supports selecting a top-volume universe when no fixed symbols are provided.

### Root cause
The dashboard form required at least one parsed symbol before considering MAX SYMBOLS, and command construction always emitted `--symbols` with the parsed symbol list. That made dynamic universe requests fail early or risk passing an invalid empty fixed-symbol argument.

### Files changed
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `backtest_order.py`
- `tests/test_dashboard_backtest_dynamic_universe.py`
- `tests/test_dashboard_app.py`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime behavior changes
The BACKTEST dashboard now accepts explicit symbols, or blank symbols with a positive MAX SYMBOLS for dynamic top-volume universe selection. Dynamic requests omit `--symbols` and pass `--max-symbols` to the BACKTEST runner. Explicit symbol requests still pass the fixed symbol list.

### Lifecycle changes
None. No lifecycle states, reject decisions, or order lifecycle transitions changed.

### Persistence changes
No SQLite schema, CSV export contract, PAPER persistence, or LIVE persistence changes. Dashboard run metadata now records dynamic-vs-explicit universe mode for BACKTEST runs.

### Export/schema changes
No required exporter schema changes. Dashboard reporting can replace the placeholder dynamic symbol display with actual exported selected symbols when the summary metadata includes them.

### Tests added
Added BACKTEST dashboard validation and command-boundary tests for dynamic universe acceptance, invalid blank/zero MAX SYMBOLS, explicit symbols with MAX SYMBOLS, omission of empty `--symbols`, `--max-symbols 20`, and BACKTEST-only mode preservation.

### Tests executed
- `pytest -q tests/test_dashboard_backtest_dynamic_universe.py`

### Risks
Dynamic universe selection depends on existing runner/exchange metadata behavior and historical data availability. The patch does not weaken filters, does not touch PAPER/LIVE runtime, and does not add Binance live order calls.

### Remaining limitations
If the exporter omits selected symbol metadata, the dashboard displays the dynamic MAX_SYMBOLS label rather than reconstructing symbols.

### Migration concerns
None. `backtest_order.py` keeps `--top-n` and adds `--max-symbols` as an alias for dashboard clarity.

### Push recommendation
Safe to push after targeted BACKTEST dashboard tests. LIVE remains NOT READY.

## 2026-06-30 - DEFAULT_FILTERS accepted-reason scope and STOP_TOO_WIDE recoverable diagnostics

### Why the patch was needed
The selected DEFAULT_FILTERS main panel showed accepted=10 and baseline/rescue accepted counts of 9/1, but accepted-reason breakdown could display aggregate comparison counts such as BASELINE=36 and SHORT_BREAKDOWN_RESCUE=4. That made the selected strategy panel internally inconsistent.

### Root cause
The dashboard trusted summary-level accepted-reason breakdown before deriving counts from selected `backtest_orders.csv`, allowing profile-comparison aggregate or wrong-scope summary values to leak into the selected main panel.

### Files changed
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_backtest_profile_comparison.py`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime behavior changes
The selected BACKTEST main panel now derives accepted-reason breakdown from the selected profile's `backtest_orders.csv` when available. It falls back to summary counts only when the parsed count total matches the selected accepted count. Profile-comparison aggregate data is not used for the selected main panel.

### Lifecycle changes
No lifecycle transitions changed. Rejected and accepted rows are still read from exported BACKTEST artifacts only.

### Persistence changes
No SQLite, CSV export schema, PAPER, or LIVE persistence changes.

### Export/schema changes
No required exporter schema changes. The dashboard consumes existing `backtest_orders.csv`, `order_backtest_summary.csv`, `rejected_shadow.csv`, and calibration summary artifacts.

### Tests added
Extended the DEFAULT_FILTERS profile fixture to prove selected accepted=10, baseline/rescue=9/1, accepted-reason breakdown BASELINE=9 and SHORT_BREAKDOWN_RESCUE=1, and aggregate ALL_FILTERS_OFF/summary counts do not leak into the selected main panel.

### Tests executed
- `pytest -q tests/test_backtest_profile_comparison.py`
- `pytest -q tests/test_dashboard_app.py`
- `pytest -q tests/test_backtest_order_scanner.py::test_stop_too_wide_split_metrics_are_exported`

### Risks
STOP_TOO_WIDE recoverable candidates are reporting-only. The patch does not loosen STOP_TOO_WIDE, increase accepted trades, change PAPER/LIVE runtime behavior, or treat ALL_FILTERS_OFF as strategy performance.

### Remaining limitations
The recoverable table depends on rejected-shadow artifacts. Missing shadow outcomes remain unknown rather than fabricated.

### Migration concerns
None. Existing artifacts continue to load, with stricter protection against wrong-scope summary reason counts.

### Push recommendation
Safe to push after targeted dashboard tests. LIVE remains NOT READY.

## 2026-06-30 - DEFAULT_FILTERS selected-profile artifact parser

### Why the patch was needed
The profile comparison leaderboard could read the uploaded run, but the main dashboard Backtest Result panel showed core DEFAULT_FILTERS metrics as unavailable because it did not resolve the selected profile directory or accepted diagnostics according to the real artifact schema from run `20260630T164308Z`.

### Root cause
The overview result model treated comparison output as leaderboard-only and did not populate the main panel from `profiles/DEFAULT_FILTERS`. Accepted diagnostics also relied on lifecycle-derived paths and did not explicitly fall back to `backtest_orders.csv` plus `lifecycle_calibration_summary.json` when `accepted_orders.csv` was absent.

### Files changed
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_backtest_profile_comparison.py`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime behavior changes
Profile-comparison dashboard results now default `selected_profile_name` to `DEFAULT_FILTERS` and resolve `selected_profile_dir` as `data/backtest/dashboard/<run_id>/profiles/DEFAULT_FILTERS` for real dashboard runs. The main panel reads summary metrics from `order_backtest_summary.csv`, accepted trades from `backtest_orders.csv`, accepted diagnostics/distributions from `lifecycle_calibration_summary.json` with `backtest_orders.csv` fallback, rejected diagnostics from `rejected_orders.csv`, optional rejected-shadow and signal-quality evidence from the profile directory, and filter state from `backtest_filter_state.json`.

### Lifecycle changes
No lifecycle state machine or runtime transition logic changed. The patch only reads exported lifecycle/calibration evidence for dashboard reporting.

### Persistence changes
No SQLite schema migration and no PAPER/LIVE persistence changes. Missing artifact reporting now names the exact expected path and fallback files checked.

### Export/schema changes
No exporter schema changes. The supported dashboard artifact schema is the run root with `backtest_profile_leaderboard.csv/json`, `backtest_run_metadata.json`, and selected profile artifacts under `profiles/DEFAULT_FILTERS/`, including `order_backtest_summary.csv`, `backtest_orders.csv`, `rejected_orders.csv`, `lifecycle_calibration_summary.json`, `backtest_filter_state.json`, `signal_quality_summary.json`, and optional shadow/quality CSVs.

### Tests added
Added a regression fixture matching the `20260630T164308Z` schema, asserting DEFAULT_FILTERS selection, profile-dir resolution, accepted/rejected counts, reject rate, win/loss/open, net PnL, baseline/rescue metrics, accepted diagnostics without `accepted_orders.csv`, rejected diagnostics from `rejected_orders.csv`, corrected leaderboard average trades/day, and stress-test-only ALL_FILTERS_OFF labeling.

### Tests executed
- `pytest -q tests/test_backtest_profile_comparison.py`

### Risks
This is BACKTEST/dashboard reporting only. It does not validate positive expectancy, weaken safety gates, change live order paths, or make ALL_FILTERS_OFF strategy performance.

### Remaining limitations
If optional shadow or signal-quality files are absent, the dashboard reports the absence rather than fabricating diagnostics. Existing historical artifacts with malformed warnings may still need tolerant parsing by consumers outside this dashboard path.

### Migration concerns
None for SQLite or live runtime. Artifact consumers should tolerate the new result fields `selected_profile_name`, `selected_profile_dir`, and `artifact_warnings`.

### Push recommendation
Safe to push after targeted dashboard tests and a full relevant test pass. LIVE remains NOT READY.

## 2026-06-30 - BACKTEST evidence rendering contract replacement

### Why the patch was needed
PR 239 was not merged because its table-by-table dashboard edits caused ping-pong regressions where completed BACKTEST evidence was hidden and failed BACKTEST runs could render misleading diagnostic evidence.

### Root cause
The selected BACKTEST template section did not have one explicit completed-vs-failed rendering contract. Individual diagnostic blocks were guarded independently, so later edits could hide completed-run headings or leak empty diagnostics into failed runs.

### Files changed
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_dashboard_app.py`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime behavior changes
The dashboard selected BACKTEST panel now has one top-level contract: completed runs render the full selected BACKTEST evidence chain, while failed/non-completed runs render only `SELECTED_BACKTEST_UNAVAILABLE_DUE_TO_FAILURE` plus failure details/warnings when available. PAPER SQL panels remain outside that selected BACKTEST evidence section.

### Lifecycle changes
No lifecycle state transition logic changed. Completed-run lifecycle-derived diagnostics remain visible; failed-run selected diagnostics are explicitly unavailable.

### Persistence changes
No SQLite schema or artifact persistence behavior changed.

### Export/schema changes
No artifact parsing semantics, CSV exports, or schema fields changed.

### Tests added/executed
Updated dashboard regression coverage to assert completed selected BACKTEST HTML includes accepted trade diagnostics, rejection reasons, shadow comparison, near-miss evidence, and required diagnostic headings. Updated failed selected BACKTEST coverage to assert diagnostic headings are absent when the selected run fails.

### Risks and limitations
This is a dashboard template/rendering patch only. It does not prove strategy expectancy, tune thresholds, weaken gates, or alter accepted trade counts.

### Migration concerns
None.

### Push recommendation
Safe to push as a clean replacement PR for the BACKTEST Evidence Rendering Contract Phase after dependency-complete dashboard test execution. LIVE remains NOT READY.


## 2026-06-30 - BACKTEST daily timeframe support and truthful interval errors

### Why the patch was needed
Dashboard-launched BACKTEST runs for `Timeframe=1d` failed closed with `Unsupported interval=1d`, but the dashboard mapped that backend capability error to a misleading Binance data-shortage message.

### Root cause
The historical data interval map only recognized intraday intervals up to `1h`, while the dashboard allowed `4h` and `1d`. The dashboard error classifier treated all historical data exceptions as insufficient-data failures and did not persist enough failed-run metadata to audit the requested timeframe or filter state.

### Files changed
- `src/alphaforge/historical_market_data.py`
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `backtest_order.py`
- `tests/test_historical_market_data.py`
- `tests/test_dashboard_app.py`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime behavior changes
BACKTEST historical loading now supports Binance-compatible `4h` and `1d` klines. Unsupported intervals raise `UNSUPPORTED_TIMEFRAME` with the requested interval, supported intervals, and source function. Dashboard validation remains BACKTEST-scoped and uses backend-supported intervals. PAPER/LIVE behavior is unchanged.

### Lifecycle changes
No lifecycle state transition semantics changed. Failed pre-run BACKTEST panels now show `SELECTED_BACKTEST_UNAVAILABLE_DUE_TO_FAILURE` instead of implying selected BACKTEST diagnostics exist.

### Persistence changes
No SQLite schema migration. Dashboard BACKTEST runs now write additive `backtest_run_metadata.json` evidence containing requested/effective timeframe, last-n-days, effective start/end, symbols, failure reason, requested profile, enabled/disabled optional filters, and whether filter state was applied before failure.

### Export/schema changes
Successful summary CSV rows now include additive requested/effective timeframe/window/symbol fields. Coverage errors include returned and required candle counts.

### Tests added/executed
Added regression tests for `_interval_ms("1d")`, `4h`, daily pagination, unsupported timeframe classification, dashboard failed metadata, and failed-dashboard diagnostic isolation.

### Risks and limitations
This patch does not tune strategy thresholds, weaken safety gates, or increase accepted trade count. Network-dependent live Binance validation remains environment-sensitive.

### Migration concerns
None for SQLite. Artifact consumers should tolerate additive metadata keys.

### Push recommendation
Safe to push after full pytest and requested smoke BACKTEST commands complete. LIVE remains NOT READY.


## 2026-06-30 - BACKTEST profile comparison runner

### Why the patch was needed
The existing comparison artifact could only describe the current run and marked other profiles as not run. That was sufficient for switch audit evidence but could not compare filter profiles over the same BACKTEST inputs.

### Root cause
The dashboard had a single BACKTEST subprocess path and no artifact-first coordinator for repeated profile executions.

### Files changed
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `docs/backtest_profile_comparison.md`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime behavior changes
Added an opt-in BACKTEST-only comparison runner that executes profile sub-runs under `profiles/<profile>/` with the same symbols, timeframe, balance, max-symbol cap, date window, and data source. Default single-profile dashboard behavior is unchanged when the checkbox is not selected.

### Lifecycle changes
No lifecycle state machine changes. Comparison artifacts consume existing per-profile lifecycle exports.

### Persistence changes
No SQLite schema migration. New JSON/CSV artifacts are written under the dashboard BACKTEST output directory.

### Export/schema changes
Added `backtest_filter_profile_comparison.json`, `backtest_profile_leaderboard.json`, and `backtest_profile_leaderboard.csv` for comparison mode, including objective-score components, warnings, artifact paths, and bucket diagnostics.

### Tests added/executed
Local validation included Python compilation and targeted pytest execution.


### Pre-merge safety audit correction
The initial comparison coordinator copied the single-profile command after UI filter disables had been appended, which could contaminate DEFAULT/STRICT/diagnostic profile sub-runs when the dashboard UI had custom disabled filters. It also relied on each subprocess computing `last_n_days` relative to its own clock. The patch now builds an immutable base BACKTEST command, appends profile-specific filter switches only per profile, and passes one fixed `--start`/`--end` window to every sub-run.

### Risks and limitations
The 30/90/180/365 multi-window matrix is scaffolded only; non-selected windows are marked NOT_RUN. Diagnostic guard profiles currently preserve default thresholds and export warnings/labels rather than changing global config. Drawdown is not fabricated when unavailable.

### Migration concerns
None for SQLite. Artifact consumers should tolerate new comparison keys.

### Push recommendation
Safe to push after targeted dashboard/backtest tests pass. LIVE remains NOT READY.

# AlphaForge Technical Surgery Report

## 2026-06-30 - Dashboard BACKTEST SHORT_BREAKDOWN_RESCUE switch

### Why this patch was needed
Operators had to manually set `ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED` outside the dashboard to compare baseline and rescue-enabled BACKTEST runs. That made dashboard evidence incomplete and increased the risk of stale shell state contaminating comparisons.

### Root cause
The rescue experiment existed in the backtest runner but the dashboard request model, form, subprocess environment, filter-state artifacts, and result rendering did not carry or show the selected experiment state.

### Files changed
- `.env.example`
- `backtest_order.py`
- `src/alphaforge/config_registry.py`
- `src/alphaforge/dashboard/__init__.py`
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_dashboard_rescue_switch.py`
- `tests/test_dashboard_app.py`
- `CHANGELOG.md`
- `VERSION.md`
- `REPORT.md`

### Runtime behavior changes
Dashboard BACKTEST requests now include a disabled-by-default `SHORT_BREAKDOWN_RESCUE experiment` checkbox. Launching a dashboard backtest passes a scoped subprocess environment override: `false` for baseline and `true` only when selected. The command also adds `--rescue-enabled` when selected. The supported dashboard runner remains `backtest_order.py`; no `python -m alphaforge.backtest.runner` module exists in this repository.

### Lifecycle changes
No lifecycle transition semantics changed. Rescue remains constrained by existing BACKTEST-only acceptance checks and does not activate in PAPER/LIVE.

### Persistence/export/schema changes
No SQLite schema migration. `backtest_filter_state.json` and `.csv` now include SHORT_BREAKDOWN_RESCUE experiment evidence, including BACKTEST-only scope and default-off status. Summary CSV metadata includes the selected rescue state and dashboard result rendering exposes baseline/rescue accepted counts, PnL, combined PnL, and accepted-reason breakdown.

### Tests added/executed
Added dashboard rescue-switch tests for disabled/enabled scoped env values, default-off behavior, BACKTEST-only settings scope, filter-state artifact evidence, dashboard result rendering, and existing rescue-disabled/rescue-enabled baseline behavior.

### Risks and limitations
Rescue-enabled BACKTEST results are experimental comparison evidence, not production readiness. Candidate quality gate CSV behavior remains reporting-only. Optional FastAPI/httpx absence still skips HTML rendering coverage in minimal environments.

### Migration concerns
None for SQLite. Existing artifact consumers should tolerate the added JSON fields and CSV columns.

### Push recommendation
Safe to push after the targeted tests pass. Do not promote rescue to PAPER/LIVE and do not infer LIVE readiness from rescue-enabled BACKTEST acceptance.

---

## 2026-06-30 - BACKTEST filter-state audit and filters-off damage diagnostics
## 2026-06-30 - BACKTEST SHORT Breakdown Rescue Reporting Experiment

## Why the patch was needed
The latest DEFAULT all-filters-on BACKTEST produced only 11 accepted trades from 1,436 candidates, while `candidate_quality_gates` showed a concentrated SHORT + BREAKDOWN_DOWN + BREAKOUT/NORMAL-stop cluster with positive rejected-shadow expectancy. A global filter loosen would be unsafe, so the patch adds a reporting-first, opt-in rescue path for that narrow hypothesis only.

## Root cause
The dashboard/export layer already exposed the SHORT breakdown quality gate as reporting-only evidence, but there was no controlled way to test reduced-size activation without disabling broad reject filters or changing baseline behavior.

## Files changed
- `.env.example`
- `backtest_order.py`
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_backtest_order_scanner.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

## Runtime behavior changes
DEFAULT BACKTEST behavior remains unchanged because `ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED=false`. When explicitly enabled, BACKTEST may rescue only SHORT `BREAKDOWN_DOWN` candidates in BREAKOUT/NORMAL-compatible conditions whose first reject reason/gate is allowed and whose execution context passes conservative checks.

## Lifecycle changes
Rescued trades use the normal simulation lifecycle and are marked with `accepted_reason=SHORT_BREAKDOWN_RESCUE`, `original_reject_reason`, `rescue_size_multiplier`, `rescue_effective_rr`, and JSON `rescue_decision_context`.

## Persistence changes
No SQLite migration is required. Existing lifecycle/export metadata fields carry rescue evidence. Summary exports include rescue diagnostics and baseline-vs-rescue PnL separation.

## Export/schema changes
`.env.example` adds the BACKTEST-only rescue variables. `backtest_filter_state` now includes a `backtest_only_experiments` section identifying the rescue switch as BACKTEST-only. The dashboard summary separates BASELINE accepted trades, RESCUE accepted trades, and reporting-only gates.

## Tests added
Regression tests prove disabled baseline rejection, enabled rescue rows, SHORT-only eligibility, LOW_SCORE LONG exclusion, metadata population, filter-state BACKTEST-only labeling, and PAPER/LIVE non-activation.

## Tests executed
- `pytest -q tests/test_backtest_order_scanner.py -k 'short_breakdown_rescue or rescue_enabled_only_backtest or rescue_disabled'`

## Risks
The rescue gate is still experimental and depends on rejected-shadow/backtest diagnostics. Spread/slippage may be estimated. Enabling the rescue can change accepted count and PnL in BACKTEST only.

## Remaining limitations
`ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_MIN_SHADOW_EXPECTANCY` is exported/configured for operator audit but live per-candidate activation currently gates on candidate execution context rather than recomputing a per-run shadow aggregate inside the decision loop.

## Migration concerns
No database migration required. Artifact consumers should tolerate additive JSON/CSV fields.

## Push recommendation
Safe to push as a BACKTEST-only reporting-first experiment. Do not enable for LIVE; LIVE remains not ready.

## 2026-07-01 - Rejected forward/shadow outcome evidence phase

### Why this patch was needed
The BTCUSDT 30d/1h zero-accepted audit still had `evidence_quality=PARTIAL` because rejected rows did not have canonical first-touch forward outcome evidence. That made it impossible to separate correct selectivity from possible overblocking without changing thresholds.

### Root cause / audit findings
- Existing rejected shadow outcomes were built in `backtest_order.evaluate_rejected_shadow`, persisted from `main()` as `rejected_shadow.csv`, and summarized by `build_rejected_shadow_summary`.
- Existing shadow rows were generated only for `_is_actionable_rejected_order()` rows with side plus entry/sl/tp geometry, so symbol-level rejects and missing-geometry rejects were skipped rather than exported with explicit unavailable reasons.
- Existing artifacts with shadow fields included `rejected_shadow.csv`, `rejected_shadow_summary.csv`, lifecycle rows after `_attach_rejected_shadow_to_lifecycle`, dashboard near-miss views, and quality diagnostics derived from rejected shadow rows.
- LOW_SCORE, HIGH_VOL_GUARD, and STOP_TOO_WIDE could receive legacy shadow outcomes only when candidate geometry existed. TOO_CHOPPY and WEAK_TREND_AND_NO_RANGE_EDGE generally lacked candidate geometry because symbol-level rejection happens before signal candidate construction.
- Missing shadow outcomes were therefore caused by a mix of candidate geometry unavailable, symbol-level rejects before candidate construction, missing TP/SL geometry, and rejected rows not being passed into the legacy actionable-only shadow simulator. Forward candle scarcity is now represented as `INSUFFICIENT_FORWARD_BARS`.
- The new canonical simulator uses only candles with `candle.timestamp > rejected_timestamp`, so the reject candle/current bar is excluded from first-touch evidence. This is diagnostic forward labeling only and is not used to decide the original reject.
- Feature calculation for the original reject is unchanged by this patch. Forward labeling is isolated after the reject timestamp; exported `future_leakage_risk` is `PASS` for that first-touch window rule, while symbol selector feature lookback audit remains separately documented in symbol diagnostics.
- Existing legacy shadow cost treatment used effective RR and cost penalty from execution reject flags. The new canonical artifact records `cost_penalty`, `gross_shadow_r`, `effective_shadow_r_after_costs`, and `shadow_net_expectancy_r`.
- The canonical outcome vocabulary now distinguishes `WOULD_TP`, `WOULD_SL`, `WOULD_TIMEOUT`, `WOULD_AMBIGUOUS`, `INSUFFICIENT_FORWARD_BARS`, `NO_TP_SL_GEOMETRY`, and `SYMBOL_REJECT_NO_CANDIDATE_GEOMETRY`.

### Files changed
- `backtest_order.py`: added canonical rejected forward outcome simulation, LOW_SCORE and symbol-reject forward summaries, HIGH_VOL_GUARD/STOP_TOO_WIDE confirmation fields, JSON/CSV artifact export, and zero-accepted root-cause forward evidence fields.
- `src/alphaforge/dashboard/backtest_control.py`: added optional parsing for low-score forward summary, symbol-reject forward summary, and rejected-forward artifact path.
- `src/alphaforge/dashboard/templates/overview.html`: surfaced compact forward summaries and the canonical rejected-forward artifact path without dumping raw rows.
- `tests/test_rejected_forward_outcomes.py`: added regression coverage for historical-safe forward windows, TP/SL/timeout/ambiguous outcomes, missing geometry, symbol reject no-geometry classification, cost penalty subtraction, summaries, and incomplete evidence quality.
- `CHANGELOG.md`, `VERSION.md`, and this `REPORT.md`: documented diagnostic-only behavior, persistence/export impact, risks, and testing.

### Runtime behavior changes
Rejected forward outcomes are diagnostic-only and do not change default BACKTEST, PAPER, or LIVE acceptance behavior. No production threshold relaxation is recommended unless forward outcomes show positive effective expectancy after execution costs and controlled adverse excursion.

Default BACKTEST decision counts remain driven by the existing reject/accept flow. PAPER and LIVE order paths are untouched.

### Lifecycle changes
No lifecycle transition semantics changed. Rejected lifecycle states remain `SIGNAL_REJECTED` or `SYMBOL_REJECTED`; the new forward artifacts annotate counterfactual evidence without converting rejects into trades.

### Persistence / export / schema changes
No SQLite schema change was introduced. CSV/JSON exports now include:
- `rejected_forward_outcomes.csv`
- `rejected_forward_outcomes.json`
- `low_score_forward_summary.csv`
- `low_score_forward_summary.json`
- `symbol_reject_forward_summary.csv`
- `symbol_reject_forward_summary.json`
- expanded `zero_accepted_root_cause_summary.csv/json` fields for forward evidence completeness and by-reason outcome/expectancy diagnostics.

### Forward simulation source functions
- `backtest_order.evaluate_rejected_forward_outcome`
- `backtest_order.build_rejected_forward_outcomes`
- `backtest_order.build_low_score_forward_summary`
- `backtest_order.build_symbol_reject_forward_summary`
- `backtest_order.build_rejected_forward_confirmation_summary`

### Forward window used
The canonical artifact uses `forward_window_bars=240`. `forward_window_minutes` is derived from the selected interval; for 1h this is 14,400 minutes. Only bars strictly after the rejected timestamp are evaluated.

### LOW_SCORE forward outcome summary
LOW_SCORE rows are split into forward-evaluable versus unavailable, near-threshold versus far-below-threshold, and effective-shadow-R after costs. The recommended action remains conservative: keep the LOW_SCORE threshold unless future diagnostic evidence shows positive effective expectancy after costs, controlled adverse excursion, sufficient sample size, and no SL dominance.

### Symbol-level forward outcome summary
TOO_CHOPPY and WEAK_TREND_AND_NO_RANGE_EDGE are summarized separately. When symbol-level rejects lack entry/sl/tp geometry, rows are exported as `SYMBOL_REJECT_NO_CANDIDATE_GEOMETRY` instead of fabricating TP/SL. The recommended action is safe pre-reject geometry capture before any threshold discussion.

### HIGH_VOL_GUARD and STOP_TOO_WIDE confirmation
The zero-accepted root-cause summary now includes forward-evaluable counts, WOULD_TP/WOULD_SL counts, and mean effective shadow R for HIGH_VOL_GUARD and STOP_TOO_WIDE. These remain confirmation diagnostics only; neither guard is relaxed.

### Zero-accepted root-cause update
The root-cause summary now reports rejected forward evaluable/unavailable counts, unavailable reason distribution, LOW_SCORE and symbol forward verdicts, HIGH_VOL/STOP_TOO_WIDE confirmations, by-reason shadow expectancy, by-reason shadow outcome distribution, evidence quality, evidence-quality reasons, conservative next action, and `production_threshold_change_recommended=false`.

### Risk assessment / limitations
- Symbol-level rejects may still be unevaluable until safe pre-reject candidate geometry is captured.
- First-touch ordering inside one OHLC candle is unknowable; such rows are `WOULD_AMBIGUOUS`, not optimistic TP wins.
- Network validation against Binance can fail in restricted environments; offline validation confirms artifact creation but not latest BTCUSDT market evidence.
- This patch does not prove strategy expectancy or LIVE readiness.

### Recommendation / push decision
Keep thresholds unchanged. Use these artifacts to determine whether rejected candidates were actually positive expectancy after execution costs before considering any diagnostic-only profile expansion. Do not relax production thresholds from count evidence alone.

## 2026-07-01 - PR259 rejected forward summary enrichment patch

### Why this patch was needed
Review found that PR259's first rejected-forward evidence pass needed PR257-compatible threshold and selector enrichment before merge. The prior LOW_SCORE near/far split used a loose `score_gap_to_threshold >= -1.0` rule, and the forward rows did not always carry the diagnostic context needed to audit LOW_SCORE or symbol-level rejects.

### Root cause
The rejected-forward artifact was built directly from rejected rows without normalizing PR257 LOW_SCORE threshold metadata and selector diagnostics into the canonical forward row. This made near-threshold counts too broad, made `would_accept_if_low_score_disabled_mean_shadow_r` use all LOW_SCORE evaluable rows instead of the counterfactual subset, and made symbol forward means vulnerable to missing selector metrics.

### Files changed
- `backtest_order.py`: added LOW_SCORE forward metadata extraction, selector metric enrichment, PR257-compatible 5% near-threshold bucketing, corrected counterfactual-disabled subset expectancy, and evidence-quality reasons for missing LOW_SCORE gaps or symbol metrics.
- `tests/test_rejected_forward_outcomes.py`: added regressions for 6.37/7.5 far classification, 7.2/7.5 near classification, positive gap far classification, subset-only counterfactual expectancy, metadata preservation, selector metric preservation, non-zero symbol means, and missing-evidence quality reasons.
- `CHANGELOG.md`, `VERSION.md`, and this `REPORT.md`: documented the review fix and remaining diagnostic-only scope.

### Runtime behavior changes
No acceptance behavior changed. Rejected forward outcomes remain diagnostic-only and do not alter default BACKTEST, PAPER, LIVE, thresholds, or canonical rejected decisions.

### Lifecycle changes
None. Existing rejected lifecycle rows remain unchanged; the patch only enriches exported forward diagnostics.

### Persistence / export / schema changes
No database migration. Additive artifact fields now include LOW_SCORE threshold metadata (`score_threshold_source`, score scale fields, mismatch/correction flags, counterfactual-disabled flag), `near_threshold_definition`, `above_threshold_or_unknown_count`, `low_score_gap_source_distribution`, counterfactual-disabled forward counts, selector metric fields, and missing-metric evidence reasons.

### Tests executed
- `python -m pytest tests/test_rejected_forward_outcomes.py -q`
- `python -m pytest tests -k "backtest or rejected or shadow or forward or low_score or symbol or root_cause or dashboard" -q`
- `python -m pytest -q`
- `python -m py_compile backtest_order.py src/alphaforge/dashboard/backtest_control.py`

### Risks / remaining limitations
If historical rejected rows did not persist score or selector diagnostics, the new artifact honestly marks those gaps as unavailable and downgrades evidence quality instead of inferring fake values. Symbol-level thresholds should still not be relaxed without safe pre-reject candidate geometry and positive effective expectancy after costs.

### Push recommendation
Safe to merge as a diagnostic correctness patch. No production threshold relaxation is recommended.
