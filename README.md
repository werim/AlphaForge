# AlphaForge (Phase 1)

Phase 1 implements only the SQL-first foundation:
- Python scaffold
- SQLAlchemy 2.x typed ORM
- SQLite (dev) / PostgreSQL (prod) configuration
- Alembic migrations
- Mandatory schema including `exchange_symbols`
- `market_type` support (`USDT_M`, `COIN_M`)
- Binance USDT-M and COIN-M client interfaces (contract stubs only)
- Symbol discovery contract
- Config snapshot persistence
- Immutable append-only audit/config tables via DB trigger policy
- Schema/constraint tests

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

## Run migration

```bash
alembic upgrade head
```

## Run tests

```bash
pytest -q
```

## Out of scope for Phase 1

- Live trading
- Real order placement
- Strategy execution
- Backtest engine
- Optimizer logic
