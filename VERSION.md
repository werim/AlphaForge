# AlphaForge Version Status

## Current Version
- **Version:** `0.3.8-dev`
- **Date:** `2026-05-16`
- **Basis:** Consolidated from current README and REPORT documentation.

## Phase Estimate
- **Estimated Phase:** **Phase 3.5**
- **Maturity Summary:**
  - Phase 1 (SQL-first foundation): Mostly implemented.
  - Phase 2 (Decision/reject engine): Partially implemented.
  - Phase 3 (Symbol selection): Implemented prototype.
  - Phase 4 (Paper runtime): Implemented prototype.
  - Phase 5 (Lifecycle-accurate backtest): Incomplete, with recent SQL-backed lifecycle/export hardening.
  - Phase 6+ (analytics hardening/live readiness/adaptive learning): Partial to early groundwork.

## BACKTEST / PAPER / LIVE Alignment
- **BACKTEST:** Uses runtime-style decision/lifecycle flow, now with SQL-backed lifecycle export path and improved effective RR persistence semantics.
- **PAPER:** Runtime path exists and contract parity checks have been expanded against backtest outputs.
- **LIVE:** Code path exists but is explicitly **not production-ready**.
- **Alignment Verdict:** **Partial alignment** between BACKTEST and PAPER; LIVE remains structurally present but operationally immature.

## Lifecycle Coverage
- Lifecycle persistence and export are now documented as SQL-backed for backtest lifecycle CSV generation.
- Rejected lifecycle rows are documented as persisted/exported in the current patch set.
- Open-at-end remains represented by timeout/open outputs rather than introducing synthetic lifecycle state inflation.
- **Coverage Verdict:** **Improved but not fully complete end-to-end across all optional/edge-field nuances**.

## Execution Realism Coverage
- Decision/reject engine includes execution-aware context usage and effective RR semantics.
- Effective RR persistence bug was fixed in lifecycle persistence path to avoid raw RR misuse when effective RR is available.
- Unavailable execution context semantics remain explicit (null/sentinel) rather than synthetic defaults.
- **Coverage Verdict:** **Meaningful execution realism present, but still prototype-level and incomplete for live-grade rigor**.

## Persistence Status
- SQLAlchemy/Alembic persistence foundation exists.
- Backtest lifecycle CSV generation is documented as reading persisted SQL lifecycle rows.
- `execution_ctx_missing` persistence semantics were normalized toward canonical integer-style behavior with legacy tolerance notes.
- **Status:** **Operational with migration/backward-compatibility caveats for legacy DB representations**.

## Known Critical Risks
1. **Live readiness risk:** LIVE execution path is not production-safe (controls, operational safeguards, reconciliation maturity not yet at live standard).
2. **Parity risk:** Full contract/lifecycle parity across all optional fields and timestamp typing nuances is still incomplete.
3. **Migration risk:** Existing SQLite databases with legacy `execution_ctx_missing` text representations may require migration/rebuild strategies.
4. **Data-source dependency risk:** Backtest universe/top-N behavior can still rely on live endpoint availability unless fixture mode is used.

## Last Audit Date
- **2026-05-16** (documentation audit based on repository README + REPORT state).

## Live-Readiness Verdict
- **Verdict:** ❌ **NOT LIVE-READY**.
- **Reason:** Lifecycle/persistence hardening has improved, but parity completeness, migration maturity, and operational safeguards remain below live deployment requirements.


## Contract Lockdown (Gen1)
- Decision/lifecycle contract fields are now emitted with canonical runtime lifecycle event names and UTC `Z` timestamps.
- Reject reasons are normalized through a shared contract utility and persisted explicitly.
- Deterministic lifecycle transition guardrails now mark invalid transitions as `ERROR` instead of silently coercing to CREATED.


## Generation 2 Persistence Note
- SQLite migrations now apply non-destructive lifecycle/persistence hardening and legacy bool/text/int normalization at init time.
- Backtest export path now performs explicit SQLite↔CSV integrity verification before completing.


## 2026-05-16 Audit Update (Generation 3)
- Runtime maturity: execution-realism hardening in progress.
- BACKTEST/PAPER/LIVE alignment: shared effective RR and reject semantics improved for order gate path.
- Lifecycle coverage: rejection lifecycle persistence unchanged and preserved.
- Execution realism coverage: explicit cost penalties + context completeness classification added.
- Known critical risks: threshold calibration by regime/volatility/liquidity bands is still conservative-default.
- Live readiness verdict: NOT LIVE READY pending broader calibration and integration validation.

## Generation 4 Status (2026-05-16)
- **Generation:** 4 — Runtime Safety Controls & Reconciliation Layer (initial deterministic implementation).
- **Runtime maturity:** Improved from prototype-only orchestration to guarded orchestration with fail-closed pre-trade gates and explicit execution failure lifecycles.
- **Reconciliation readiness:** Partial; deterministic reconciliation journaling is implemented for timeout/error/missing-ack states with snapshot payloads, but active order remediation remains limited.
- **Operational readiness notes:** PAPER safety posture improved materially; LIVE remains **not ready** pending richer exposure/correlation datasets, exchange remediation completeness, and soak testing.


## Generation 5 Status (2026-05-17)
- **Generation:** 5 — Live Readiness Qualification & Controlled Enablement.
- **Runtime maturity:** deterministic LIVE qualification gate introduced with fail-closed behavior.
- **BACKTEST/PAPER/LIVE alignment:** unchanged decision semantics; LIVE adds explicit readiness gate before orchestration start.
- **Lifecycle coverage:** qualification checks enforce orphan/transition/reject/exit completeness validations on persisted lifecycle rows.
- **Execution realism coverage:** statistical sanity checks now detect constant RR/score placeholder-like behavior before LIVE enablement.
- **Known critical risks:** exchange-side active remediation remains limited; qualification snapshots rely on operator-provided observability signals.
- **Live readiness verdict:** ❌ **NOT LIVE-READY by default**; LIVE allowed only when all gates pass and operator acknowledgement is explicit.

## Generation 6 Status (2026-05-17)
- **Generation:** 6 — Backtest CSV schema-drift hardening.
- **Runtime maturity:** improved export robustness under evolving lifecycle/decision/execution row schemas.
- **BACKTEST/PAPER/LIVE alignment:** unchanged decision logic; export contract now safer against additive row fields in BACKTEST outputs.
- **Lifecycle coverage:** unchanged lifecycle semantics; lifecycle/export visibility improved by preventing schema-mismatch export aborts.
- **Execution realism coverage:** unchanged calculations; execution-context fields are now reliably exportable when present.
- **Live readiness verdict:** ❌ **NOT LIVE-READY** (unchanged).

## Generation 6 Status (2026-05-17)
- **Generation:** 6 — Exchange-Reconciled Live Control Plane (deterministic supervision layer).
- **Runtime maturity:** added continuous reconciliation loop with bounded interval/timeout and fail-closed escalation path.
- **BACKTEST/PAPER/LIVE alignment:** same orchestration path can run reconciliation in PAPER/LIVE without forcing exchange calls in tests.
- **Lifecycle coverage:** reconciliation findings now emit explicit `RECONCILIATION_REPAIR` lifecycle events with incident evidence payloads.
- **Execution realism coverage:** detection for orphan orders/positions, stale orders, and lifecycle divergence with deterministic repair recommendations.
- **Known critical risks:** exchange snapshot source currently uses adapter-provided/persisted runtime state; full venue-native fill lineage ingestion remains a Gen7 blocker.
- **Live readiness verdict:** ❌ **NOT LIVE-READY** without production exchange telemetry wiring, operator repair approvals, and extended soak validation.

## Generation 7 Status (2026-05-17)
- **Generation:** 7 — Runtime Bootstrap Entrypoint & Safe Startup Loop.
- **Runtime maturity:** module-level runtime is now directly executable with async bootstrap and graceful shutdown path.
- **BACKTEST/PAPER/LIVE alignment:** shared orchestrator path preserved; mode parsing now env-driven at runtime bootstrap.
- **Lifecycle coverage:** unchanged lifecycle semantics; runtime liveness now ensures lifecycle emission loops can run continuously once scanner/feed is provided.
- **Execution realism coverage:** unchanged decision economics; RR wiring confirmed to preserve dynamic upstream RR when provided, with 2.0 fallback only for missing/invalid input.
- **Live readiness verdict:** ❌ **NOT LIVE-READY** (unchanged; bootstrap does not alter readiness gate requirements).
- **Generation:** 7 — Production-grade Environment Template & Safety Defaults.
- **Runtime maturity:** operational configuration posture improved; no core execution-flow rewrite.
- **BACKTEST/PAPER/LIVE alignment:** documentation and env mode controls now explicitly separated with conservative defaults.
- **Lifecycle coverage:** unchanged lifecycle semantics; safer operator guidance reduces accidental LIVE misuse.
- **Execution realism coverage:** env template now includes explicit spread/slippage/liquidity/effective-RR gate controls.
- **Live readiness verdict:** ❌ **NOT LIVE-READY by default** (explicitly enforced by default env posture).

## 2026-05-17 Audit Update (Backtest lifecycle accounting)
- Backtest lifecycle decision labeling no longer classifies `SIGNAL_CREATED` as accepted; it is persisted as `PENDING` until a terminal outcome exists.
- `SYMBOL_REJECTED` lifecycle rows are persisted as rejected decisions.
- Backtest summary accounting now uses per-signal terminal decisions and counts orders from `ORDER_PLACED` events only.
- Live readiness verdict remains unchanged: **NOT LIVE-READY**.

## Generation 8 Status (2026-05-17)
- **Generation:** 8 — Setup Quality Diagnostics & Gate Traceability.
- **Runtime maturity:** improved observability for candidate setup quality and gate-failure provenance.
- **BACKTEST/PAPER/LIVE alignment:** reject gate logic remains shared; diagnostics now expose first/all failed gates for better parity debugging.
- **Lifecycle coverage:** unchanged lifecycle transitions; export-level diagnostic completeness improved for rejected and accepted candidate rows.
- **Execution realism coverage:** improved measurement/reporting (effective-vs-raw RR percentiles and context-driven rejection slicing).
- **Known critical risks:** setup generation remains heuristic/breakout-biased; diagnostics illuminate but do not yet remediate structural setup weakness.
- **Live readiness verdict:** ❌ **NOT LIVE-READY** (unchanged).


## Generation 9 Status (2026-05-17)
- **Generation:** 9 — Adaptive Learning Data Foundation (deterministic, SQL-first, shadow-only).
- **Runtime maturity:** adaptive persistence/analytics groundwork added without enabling autonomous behavior changes.
- **BACKTEST/PAPER/LIVE alignment:** review data model shared; no mode-specific live-call dependencies introduced.
- **Lifecycle coverage:** rejected and closed outcomes are now persistable as explicit learning review rows.
- **Execution realism coverage:** review schema includes spread/slippage/liquidity/volatility/effective-RR context for survivability analysis.
- **Known critical risks:** rejected-signal forward outcome labels are still mostly null until dedicated post-window evaluator is implemented.
- **Live readiness verdict:** ❌ **NOT LIVE-READY** (unchanged; adaptive remains non-active by default).
## 2026-05-17 Hotfix Status (Regime gate initialization)
- Trade-quality regime gate now initializes deterministically before first use across shared decision flow.
- BACKTEST/PAPER/LIVE contract alignment unchanged; fix removes crash-only divergence in candidate evaluation.
- Lifecycle coverage unchanged.
- Persistence semantics unchanged.
- Live readiness verdict remains: ❌ **NOT LIVE-READY**.

## 2026-05-18 Audit Update (Backtest lifecycle summary reconciliation)
- Main backtest summary counters now treat `total_candidates` as signal-level candidates (`SIGNAL_CREATED` + `SYMBOL_REJECTED`) and compute `accepted_count`/`rejected_count` from terminal reject states.
- `total_orders` now represents accepted pending order objects (`WAITING_ENTRY_ZONE`) instead of candidate-level totals.
- Lifecycle outcome buckets (`triggered_orders`, `not_triggered_orders`, `tp_hits`, `sl_hits`, `open_at_end`) are reconciled from lifecycle terminal states for accepted orders.
- Backtest quality summary now uses signal-level candidate denominator (from `SIGNAL_CREATED`) and signal-scoped reject accounting for consistency with order summary.
- Live readiness verdict remains: ❌ **NOT LIVE-READY**.

## 2026-05-18 Hotfix Status (Backtest quality summary + execution metrics persistence)
- Backtest quality summary now counts plain candidate decision rows directly when lifecycle `SIGNAL_CREATED` rows are absent, while preserving signal-scoped denominator behavior when lifecycle rows are present.
- Adaptive closed-trade persistence now writes legacy `execution_metrics` JSON alongside structured review payload fields.
- SQLite init/migration now ensures `closed_trade_reviews.execution_metrics` exists for backward-compatible read paths.
- Live readiness verdict remains: ❌ **NOT LIVE-READY**.

## Generation N+2 Foundation Status (2026-05-18)
- **Generation:** N+2 foundation — deterministic forward-window reject telemetry and scoped adaptive reject-learning aggregation.
- **Runtime maturity:** telemetry layer improved; no autonomous threshold tuning enabled.
- **BACKTEST/PAPER/LIVE alignment:** deterministic evaluator logic is replay-safe and side-effect free for decision path (post-decision analytics only).
- **Lifecycle coverage:** rejected/accepted lifecycle rows can now be evaluated with deterministic forward labels for later persistence/export wiring.
- **Execution realism coverage:** execution quality bucket classification added for forward-eval telemetry slicing.
- **Known critical risks:** forward-eval SQL persistence/export wiring remains partial; adaptive stats breadth currently reject-review centric for advanced scopes.
- **Last audit date:** 2026-05-18.
- **Live readiness verdict:** ❌ **NOT LIVE-READY** (unchanged).

## Generation N+2 Wiring Status (2026-05-18)
- Deterministic terminal-trigger forward evaluator is now wired into backtest output generation.
- Immutable calibration snapshot persistence contract is introduced with idempotent insert semantics.
- Adaptive scope ingestion keys are validated across all requested bucket dimensions.
- Adaptive/live threshold mutation remains disabled.
- **Generation:** N+2 wiring — terminal forward evaluator trigger + immutable calibration snapshot persistence.
- **Determinism posture:** forward evaluation triggered post-terminal lifecycle only; bounded lookahead retained; no decision-path feedback.
- **Persistence posture:** additive `calibration_snapshots` table with idempotent uniqueness guard.
- **Export posture:** forward labels, adaptive scope stats, and calibration rows emitted as additive CSV outputs.
- **Live readiness verdict:** ❌ **NOT LIVE-READY** (adaptive thresholds remain non-live).

## 2026-05-19 Probabilistic Scoring Update
- **Version:** `0.3.9-dev`
- Added probability-weighted decision semantics (`p_win`, `p_tp_hit`, `p_sl_hit`, `p_entry_trigger`, `p_fakeout`, `p_regime_fit`, `p_execution_success`, `confidence`, `calibrated_score`) in the shared AIBrain path for runtime phases.
- Reject semantics now include probability-based reasons (`LOW_P_WIN`, `LOW_EXECUTION_PROBABILITY`, `LOW_CONFIDENCE`, `NEGATIVE_EXPECTANCY_AFTER_COSTS`, `HIGH_FAKEOUT_PROBABILITY`, `LOW_REGIME_FIT_PROBABILITY`).
- Live readiness verdict remains **NOT LIVE READY**.
