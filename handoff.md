## MERGED

- PR #366 — state-direction shadow evaluation — MERGED
- PR #367 — BACKTEST lifecycle/scoring parity — MERGED
- PR #368 — timestamp-bounded expectancy evidence — MERGED

## COMPLETED P0

BACKTEST/PAPER lifecycle + authoritative scoring parity is complete at code-path level:

- offline simulator bypass removed
- lifecycle/reject persistence fixed
- BACKTEST uses authoritative AIBrain scoring semantics
- shared scoring normalization exists
- historical scoring is timestamp-bounded
- timestamp-bounded expectancy evidence is complete
- no LIVE exchange/network dependency introduced
- PAPER/LIVE scoring semantics preserved

## KNOWN HISTORICAL FALLBACKS

Still unavailable or incomplete historically:

- historical orderbook snapshots
- historical funding snapshots
- true historical MTF source snapshots
- PAPER fill/latency evidence when not captured historically

## NEXT P0

prospective timestamped execution-context capture

## WORKING RULE

Before implementation, define an explicit as-of SQL/data contract that prevents future-outcome leakage.
