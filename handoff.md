## MERGED

- PR #366 — state-direction shadow evaluation — MERGED
- PR #367 — BACKTEST lifecycle/scoring parity — MERGED

## COMPLETED P0

BACKTEST/PAPER lifecycle + authoritative scoring parity is complete at code-path level:

- offline simulator bypass removed
- lifecycle/reject persistence fixed
- BACKTEST uses authoritative AIBrain scoring semantics
- shared scoring normalization exists
- historical scoring is timestamp-bounded
- no LIVE exchange/network dependency introduced
- PAPER/LIVE scoring semantics preserved

## KNOWN HISTORICAL FALLBACKS

Still unavailable or incomplete historically:

- timestamp-bounded expectancy cohorts
- historical orderbook snapshots
- historical funding snapshots
- true historical MTF source snapshots
- PAPER fill/latency evidence when not captured historically

## NEXT P0

timestamp-bounded historical expectancy + execution-context contract

## WORKING RULE

Before implementation, define an explicit as-of SQL/data contract that prevents future-outcome leakage.
