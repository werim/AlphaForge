# AlphaForge Phase 1

SQL-first scaffold for V3 master spec + merged V2 constraints.

## Included in Phase 1
- Python project scaffold (`src/` layout)
- SQLAlchemy 2.x typed ORM
- SQLite dev + PostgreSQL prod config
- Alembic migrations
- Mandatory Phase 1 tables:
  - candles, indicator_snapshots, regime_states, strategy_signals
  - selector_decisions, order_intents, risk_decisions, trade_lifecycle_events
  - positions, orders, closed_trades
  - rejection_audit, order_decision_audit, config_snapshots
  - strategy_performance, regime_performance
  - optimizer_trials, optimizer_results, runtime_state, exchange_symbols
- `exchange_symbols` expanded spec fields and composite uniqueness
- Enum/check constraints + foreign-key lineage
- JSONB for PostgreSQL with SQLite-compatible JSON fallback
- Append-only triggers for audit/config tables (SQLite + PostgreSQL)
- Tests for imports, table presence, constraints, append-only behavior

## Setup
```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
alembic upgrade head
pytest -q
```

## Out of scope in Phase 1
- live trading
- real order placement
- strategy execution
- backtest engine
