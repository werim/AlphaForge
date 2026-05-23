# JOB-01 — Canonical Decision Pipeline Audit

Status: audit-only, fail-closed, no threshold or trade-frequency changes.

## Executive summary

AlphaForge is currently PARTIALLY_SHARED rather than fully canonical across BACKTEST, PAPER, and LIVE.

The core divergence is intentional but important: BACKTEST uses the order-runtime path through `backtest_order.py::scan_symbol_backtest(...)` into `src/alphaforge/order.py::run_order_cycle(...)`, while PAPER/LIVE runtime uses `src/alphaforge/runtime.py::_process_symbol(...)` with `AIBrain.before_real_order(...)` after symbol selection and runtime risk gates. PAPER and LIVE are closer to each other than either is to BACKTEST.

This audit does not claim production readiness. It records code-backed blockers and gives the minimal safe patch sequence for JOB-02 through JOB-06.

## Canonical target pipeline

```text
market data source
  -> symbol/candidate selection
  -> signal payload construction
  -> score calculation
  -> reject/risk gates
  -> raw RR calculation
  -> execution context construction
  -> effective RR calculation
  -> final order decision
  -> lifecycle persistence
  -> execution adapter
  -> reconciliation / post-trade lifecycle
```

Mode-specific behavior should be limited to:

- market-data source,
- historical replay vs public exchange scanner,
- execution adapter / simulator,
- reconciliation provider.

The decision contract itself should not fork by mode.

## Actual mode map

| Subsystem | BACKTEST | PAPER | LIVE | Classification |
|---|---|---|---|---|
| Market/candidate source | `backtest_order.py` historical candles, `_build_market_ctx(...)`, `_build_symbol_market_data(...)` | runtime scanner, usually exchange public scanner unless safe override | exchange public scanner required by provenance gate | PARTIAL_PIPELINE_DIVERGENCE |
| Symbol selection | `select_symbol(...)` / backtest loop helpers | `select_symbols(..., include_rejected=True)` in runtime scan | same as PAPER after LIVE scanner checks | PARTIAL_PIPELINE_DIVERGENCE |
| Signal construction | `CandidateOrder` and order runtime candidate | `_build_signal(...)` inside runtime | same runtime path as PAPER | PARTIAL_PIPELINE_DIVERGENCE |
| Score | backtest/order-runtime diagnostics from `run_order_cycle(...)` and market ctx heuristic fields | `AIBrain.score_signal(...)` through `before_real_order(...)` | side-effect-free qualification uses `score_signal(...)`, runtime uses `before_real_order(...)` | PARTIAL_PIPELINE_DIVERGENCE |
| Final reject/accept decision | `run_order_cycle(...)`, then extra backtest execution reject helper | runtime risk gates + `AIBrain.before_real_order(...)` order plan decision | same as PAPER plus LIVE startup gates | PARTIAL_PIPELINE_DIVERGENCE |
| Effective RR | shared cost model is used in parts; backtest has `_execution_reject_flags(...)` wrapper | runtime final reject persists effective_rr as signal risk_reward fallback in some paths | same runtime semantics | PARTIAL_PIPELINE_DIVERGENCE |
| Lifecycle | rich backtest CSV rows through `simulate_candidate(...)` and `process_backtest_result(...)` | runtime `_emit_lifecycle_event(...)` canonical path | same runtime path plus reconciliation/incident states | PARTIAL_PIPELINE_DIVERGENCE |
| Persistence | backtest lifecycle rows persisted via `_persist_lifecycle_rows(...)` and `save_trade_lifecycle_event(...)` | runtime callbacks plus `AIBrain` internal/final decision persistence | same as PAPER when configured, with readiness gates | PARTIAL_PIPELINE_DIVERGENCE |
| Execution | historical outcome simulator | paper fill simulator with configured slippage | real adapter required before loop | HEALTHY_MODE_SPECIFIC_ADAPTER_BOUNDARY |

## Code-backed findings

### 1. BACKTEST decision path

`backtest_order.py::scan_symbol_backtest(...)` builds historical market context, constructs `OrderExecutionContext(mode=TradingMode.BACKTEST, ...)`, calls `run_order_cycle(...)`, and only returns a `CandidateOrder` when `result.status == executed`.

`backtest_order.py::process_backtest_result(...)` then maps rejected/executed results into lifecycle rows and rejected exports. It also runs an extra execution rejection layer through `_execution_reject_flags(...)` before simulating accepted candidates.

Classification: `PARTIAL_PIPELINE_DIVERGENCE`.

### 2. PAPER/LIVE runtime decision path

`src/alphaforge/runtime.py::_scan_once(...)` calls `select_symbols(candidates, {"include_rejected": True})` and processes only tradable rows.

`src/alphaforge/runtime.py::_process_symbol(...)` emits `SIGNAL_CREATED`, runs runtime risk gates, builds a signal payload, and calls `self.ai_brain.before_real_order(...)`. Rejected plans persist reject payloads and emit `SIGNAL_REJECTED`; accepted plans emit canonical pre-execution lifecycle states before `_execute(...)`.

Classification: `PARTIAL_PIPELINE_DIVERGENCE` versus BACKTEST, but PAPER and LIVE are mostly shared after scanner/startup safety boundaries.

### 3. Score calculation divergence

BACKTEST uses market-context heuristic fields and `run_order_cycle(...)` diagnostics. PAPER/LIVE use `AIBrain.score_signal(...)` through `before_real_order(...)`. LIVE qualification now has a side-effect-free pre-submit evaluator calling `score_signal(...)`, `choose_order_plan(...)`, and `explain_decision(...)`.

Risk: score distributions may not be comparable between historical replay and runtime unless a shared scoring contract is introduced.

Classification: `SCORING_OR_REGIME_PIPELINE_FAILURE` risk, not proven data failure without artifact outputs.

### 4. RR and effective RR divergence

Backtest market context computes raw `rr` in `_build_market_ctx(...)`. Backtest execution rejection uses `_execution_reject_flags(...)`, which delegates to `build_execution_cost_model(...)` and subtracts penalties from raw RR. Runtime persistence currently uses risk_reward / rr fallback for some effective_rr fields, so final stored effective_rr may not always represent a fully recomputed execution-adjusted value.

Risk: executable profitability can be overstated or inconsistently diagnosed when effective_rr is stored as raw RR fallback.

Classification: `PARTIAL_PIPELINE_DIVERGENCE` and `EXECUTION_CONTEXT_FAILURE` risk when execution context is missing or placeholder-like.

### 5. Reject persistence

BACKTEST rejected rows are emitted in `process_backtest_result(...)` for signal rejects and execution rejects. Runtime rejected rows are persisted through `_persist_reject(...)` callbacks and AIBrain internal/final decision rows. Recent patches separated `phase=final` from `ai_internal_*` rows.

Risk: downstream SQL must filter canonical final rows with `COALESCE(phase,'final')='final'` to avoid double-counting internal AI rows.

Classification: `PERSISTENCE_INTEGRITY_FAILURE` risk if queries do not respect phase semantics.

### 6. Execution context propagation

BACKTEST now derives or estimates several context fields (`volume_24h_usdt`, `spread_pct`, `funding_rate_pct`, liquidity/volatility fields). Missing backtest execution fields use explicit `UNAVAILABLE_BACKTEST` sentinels in lifecycle rows. Runtime derives market context from scanner diagnostics and selected candidate inputs.

Risk: any 0.0 fallback in upstream scanner/candidate construction can masquerade as valid execution context. JOB-05 must enforce unavailable-vs-zero semantics.

Classification: `EXECUTION_CONTEXT_FAILURE` risk when fields are absent, zero-filled, or not represented in persisted rows.

## Lifecycle state map

Canonical trading lifecycle currently targeted by audit:

```text
SIGNAL_CREATED
  -> SIGNAL_REJECTED

SIGNAL_CREATED
  -> SIGNAL_ACCEPTED
  -> WAITING_ENTRY_ZONE
  -> ENTRY_TRIGGERED
  -> ORDER_PLACED
  -> POSITION_OPENED
  -> POSITION_CLOSED
```

Runtime also uses incident/reconciliation/error states for operational safety:

```text
ENTRY_TIMEOUT
ORDER_REJECTED
ERROR
RECONCILIATION_REPAIR
```

Backtest `simulate_candidate(...)` expands accepted candidates through historical trigger and terminal TP/SL/TIMEOUT simulation. Runtime `_execute(...)` maps paper/live execution result status into lifecycle events.

## Persistence flow

```text
BACKTEST
  process_backtest_result / simulate_candidate
  -> LifecycleRow list
  -> _persist_lifecycle_rows(...)
  -> save_trade_lifecycle_event(...)

PAPER/LIVE
  RuntimeOrchestrator._process_symbol(...)
  -> _persist_reject(...) callback
  -> save_order_decision(...)
  -> _emit_lifecycle_event(...) callback
  -> save_trade_lifecycle_event(...)

AIBrain internal audit
  before_real_order(...)
  -> _persist_decision(...)
  -> order_decisions phase=ai_internal_*
```

Canonical final decision analytics should prefer final runtime rows and avoid treating AI-internal rows as executable final decisions.

## Production blockers

1. No single canonical `evaluate_signal_to_order(...)` contract across BACKTEST/PAPER/LIVE.
2. BACKTEST and runtime use materially different decision engines (`run_order_cycle` vs `AIBrain.before_real_order`).
3. Effective RR storage can fall back to raw RR in runtime reject payloads.
4. Execution context completeness is not yet enforced as a hard audit invariant across all modes.
5. Lifecycle semantics are improved but still mode-shaped: historical simulator rows and runtime event rows are not guaranteed identical for the same candidate.
6. LIVE remains fail-closed and not production-ready without real adapter, full readiness evidence, observability, rollback, and protective-order lifecycle proof.

## Minimal safe patch sequence

1. JOB-02: Make BACKTEST lifecycle realism complete and explicitly comparable to runtime lifecycle states.
2. JOB-03: Enforce reject persistence integrity and canonical-final SQL filters.
3. JOB-04: Canonicalize effective RR calculation and persistence from one shared function.
4. JOB-05: Enforce execution context population with `UNAVAILABLE` / `execution_ctx_missing=1`, never silent 0.0.
5. JOB-06: Add runtime SQLite audit pack for PAPER DB artifacts.
6. After JOB-01 through JOB-06: introduce a shared `evaluate_signal_to_order(...)` adapter layer, but only after diagnostics prove current rows are trustworthy.

## Tests required next

These are documented as required follow-up tests because JOB-01 is audit-only:

- rejected signals persist correctly with signal_id, symbol, reject_reason, score, raw_rr, effective_rr;
- score is non-constant across different market contexts;
- effective_rr differs from raw_rr when cost penalties are non-zero;
- lifecycle ordering validates accepted and rejected branches;
- execution context missingness is explicit and queryable;
- duplicate final decision rows are prevented or unambiguously phased;
- backtest emits rejected lifecycle rows;
- PAPER/LIVE/BACKTEST parity evidence proves where the same canonical function is used.

## Final classification

Overall: `PARTIAL_PIPELINE_DIVERGENCE`.

BACKTEST is no longer only a raw TP/SL simulator, because it emits richer signal/order lifecycle rows and rejects. However, it is not yet a fully lifecycle-accurate simulator of the same runtime decision engine because it does not use the exact same AIBrain/runtime decision path as PAPER/LIVE.

Production stance: fail closed. Do not claim LIVE readiness from this audit.
