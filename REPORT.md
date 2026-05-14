Version{}
# AlphaForge Technical Status Report

## Executive Summary

AlphaForge has progressed beyond a Phase-1-only SQL scaffold. The codebase now includes symbol selection, a deterministic decision/reject pipeline, runtime orchestration, and backtest lifecycle export logic. However, this remains a prototype-level system and is not ready for production live trading.

## Current Phase Assessment

- The previous README documented AlphaForge as "Phase 1" only.
- Current code appears materially closer to **Phase 3.5 / Phase 4**: symbol selection + runtime + decision engine prototypes.
- Live execution readiness is not achieved.

## Implemented Components

- `src/alphaforge/runtime.py`
  - Runtime orchestrator with periodic scan loop, heartbeat loop, mode switch (`BACKTEST`/`PAPER`/`LIVE`), reject persistence hook, lifecycle-event hook, and paper execution simulation.

- `src/alphaforge/ai_brain.py`
  - Deterministic scoring pipeline (`score_signal`), decision planning (`choose_order_plan`), explanation builder, SQL persistence of signals/decisions/features, and expectancy stat updates.

- `src/alphaforge/symbol_selector.py`
  - Rule-based symbol filtering/scoring with reject reasons, diagnostics, and ranked output.

- `src/alphaforge/execution.py`
  - Execution-context assembly: slippage estimate, spread handling, latency, liquidity, funding, and volatility regime annotations.

- `src/alphaforge/persistence.py`
  - Persistence helper module. Present and used by the project, but detailed behavior still needs deeper review before live readiness.

- `backtest_order.py`
  - Backtest scanning, candidate generation, lifecycle row export, rejected-order shadow evaluation, and report artifact generation.

- `tests/`
  - Test coverage exists for runtime, decision logic, schema, trading modes, and backtest scanner behavior.

## Decision Pipeline Assessment

- Signals are scored in `AIBrain.score_signal` via weighted quality components:
  - setup quality
  - regime alignment
  - expectancy
  - momentum
  - liquidity
  - volatility fit
  - risk/reward quality

- Penalties include:
  - spread
  - funding
  - fakeout risk
  - recent loss streak
  - slippage
  - latency
  - funding execution penalty

- Reject/accept decisions are determined by score threshold and expectancy gate:

```text
accepted = total_score >= min_accept_score and expectancy_edge >= 0.5
```
