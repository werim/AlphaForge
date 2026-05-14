# AlphaForge: Execution-Aware Trading Research/Runtime Prototype

AlphaForge is a SQL-first, execution-aware futures trading research/runtime prototype. It includes deterministic signal decision logic, symbol selection, runtime orchestration, and backtest tooling. It does **not** currently meet production live-trading standards.

## Current Status

- SQL-first foundation exists (SQLAlchemy models, Alembic migrations, persistence modules).
- Symbol selection exists as a scored/reject-aware prototype selector.
- Deterministic AI decision/reject engine exists (`AIBrain`) with persisted decision features.
- Runtime orchestrator exists with `BACKTEST`, `PAPER`, and `LIVE` mode handling in code.
- Backtest lifecycle tooling exists but lifecycle/export fidelity is still incomplete.
- Live trading is **not production-ready**.

## Phase Status

| Phase | Scope | Conservative Status |
|---|---|---|
| Phase 1 | SQL-first foundation | Mostly implemented |
| Phase 2 | Decision/reject engine | Partially implemented |
| Phase 3 | Symbol selection | Implemented prototype |
| Phase 4 | Paper runtime | Implemented prototype |
| Phase 5 | Lifecycle-accurate backtest | Incomplete |
| Phase 6 | Analytics/persistence hardening | Partial |
| Phase 7 | Live execution readiness | Not ready |
| Phase 8 | Adaptive learning/optimizer | Early groundwork only |

## Not Production Ready

> **Warning**
> AlphaForge should currently be treated as a research/runtime prototype. Do not assume production-grade controls, exchange-failure handling, reconciliation, or operational safeguards for live capital deployment.

## Repository Highlights

- Runtime orchestration: `src/alphaforge/runtime.py`
- Deterministic decision engine: `src/alphaforge/ai_brain.py`
- Symbol selection: `src/alphaforge/symbol_selector.py`
- Execution context helpers: `src/alphaforge/execution.py`
- Persistence and schema modules: `src/alphaforge/persistence.py`, `src/alphaforge/models/`, `alembic/`
- Backtest runner/export script: `backtest_order.py`

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

## Run migrations

```bash
alembic upgrade head
```

## Run tests

```bash
pytest -q
```

## Next Development Priority

1. Unify `BACKTEST` / `PAPER` / `LIVE` decision lifecycle contract as much as possible.
2. Persist rejected signals/orders consistently across modes.
3. Fix lifecycle export accuracy (event ordering, statuses, and rejection visibility).
4. Ensure score/RR fields are computed from context and not hardcoded placeholders.
5. Populate execution-context fields where data exists; otherwise mark as unavailable explicitly.
6. Add regression tests for rejected lifecycle rows and lifecycle completeness.
