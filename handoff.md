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

## LIVE READINESS AGENT V1 CONTINUATION (2026-09-20)

- `feat/live-readiness-agent-v1` contains a read-only gate engine and focused tests.
- The paused campaign `camp_76d8ac53157c1337` is immutable historical evidence; no new PAPER work reuses it.
- Offline smoke and fixed 30-day BTCUSDT/ETHUSDT 1h historical BACKTESTs completed in disposable `/private/tmp` outputs.
- Historical run: 1,436 candidates, 0 accepted, 1,436 rejected; quality profile `FAIL`; evidence is insufficient for readiness.
- Permanent G0–G10 verification protocol is in `docs/TEST_PROTOCOL.md`.
- Remaining P0: CI evidence, a fresh G6 PAPER smoke campaign, then separate G7 evidence campaign; neither may be created until operator requests it.
