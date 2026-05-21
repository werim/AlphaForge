

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
