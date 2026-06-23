# Decision Lifecycle Contract

AlphaForge uses SQL as the source of truth for signal, decision, order, reject, entry, exit, and export lifecycle evidence. CSV files, dashboards, reports, and analytics must derive lifecycle rows from SQL (`trade_lifecycle_events`, `signals`, and `order_decisions`) rather than shadow in-memory structures.

## Canonical lifecycle states

The canonical states are defined in `src/alphaforge/lifecycle_contract.py` and are the only states accepted for new lifecycle persistence/export rows:

- `SIGNAL_CREATED` — a candidate signal was created and persisted with identity, score/RR evidence when available, expectancy bucket, and execution context availability.
- `SIGNAL_REJECTED` — the signal failed decision or reject-engine validation before an order should be placed.
- `WAITING_ENTRY_ZONE` — the signal passed initial decision validation and is waiting for entry conditions.
- `ENTRY_TRIGGERED` — the entry condition became true and an order decision may be formed.
- `ORDER_PLACED` — an order intent was accepted by the runtime path for submission/simulation.
- `ORDER_REJECTED` — order creation/submission was blocked or rejected after signal acceptance.
- `POSITION_OPENED` — execution evidence indicates the position opened.
- `POSITION_CLOSED` — the position reached a terminal close outcome; close reason must carry `TP_HIT`, `SL_HIT`, timeout/open-at-end, cancellation, or protective-exit detail where known.
- `ENTRY_TIMEOUT` — the entry condition/order did not complete within its validity window.
- `CANCELLED` — the lifecycle was intentionally cancelled before a normal close.

## Legacy/internal state mapping

Legacy/internal labels must be mapped explicitly before export or persistence as new canonical truth:

- `CREATED` maps to `SIGNAL_CREATED` only for backwards compatibility.
- `SIGNAL_ACCEPTED` maps to `WAITING_ENTRY_ZONE`.
- `SYMBOL_REJECTED` maps to `SIGNAL_REJECTED`.
- `ORDER_CANCELLED` and `CANCELED` map to `CANCELLED`.
- `EXPIRED` maps to `ENTRY_TIMEOUT`.
- `TP_HIT`, `SL_HIT`, and `OPEN_AT_END` are terminal close reasons and map to `POSITION_CLOSED` as lifecycle state; the precise close reason must remain in payload/close fields.

New exports must not emit `CREATED` as the first lifecycle state. Unknown lifecycle states must be rejected rather than silently persisted.

## Required fields

Lifecycle rows should preserve, when available:

- stable identity: `event_id`, `signal_id`, `lifecycle_id`, `order_id`, `trade_id`/position identity
- timing: `event_ts`, `created_at`, and `lifecycle_seq` where order matters
- mode: `BACKTEST`, `PAPER`, `LIVE_PRECHECK`, or `LIVE`
- canonical `lifecycle_state` plus `event_type`/`state` compatibility fields
- decision evidence: `decision`, `reject_reason`, `cancel_reason`, score, raw RR, effective RR, expectancy bucket
- execution context: spread, slippage, funding, liquidity, latency, volatility regime, evidence status, and an explicit missing/unavailable marker
- payload details needed to audit close reason, failure reason, reconciliation reason, or incident context

Unknown execution cost is not zero cost. If spread, slippage, funding, latency, liquidity, or volume evidence is unavailable, persist it as unavailable/null and mark execution context missing; do not write fake zero values.

## Accepted and rejected flow

Accepted flow:

`SIGNAL_CREATED -> WAITING_ENTRY_ZONE -> ENTRY_TRIGGERED -> ORDER_PLACED -> POSITION_OPENED -> POSITION_CLOSED`

Rejected/blocking flow:

- pre-order: `SIGNAL_CREATED -> SIGNAL_REJECTED`
- order-stage: `SIGNAL_CREATED -> WAITING_ENTRY_ZONE -> ENTRY_TRIGGERED -> ORDER_REJECTED`
- timeout/cancel: `WAITING_ENTRY_ZONE -> ENTRY_TIMEOUT` or any supported active state to `CANCELLED`

Rejected decisions are valuable data and must remain persisted/exportable with `reject_reason` rather than being dropped.

## BACKTEST vs PAPER vs LIVE

- `BACKTEST` must use the canonical vocabulary and derive CSV exports from SQL persistence. It must not create a shortcut lifecycle that starts with legacy `CREATED`, hides rejects, or substitutes perfect execution costs.
- `PAPER` must use the same decision/reject lifecycle semantics as backtest where possible, while using paper execution evidence rather than live order submission.
- `LIVE` remains `NOT_READY`; this contract does not enable live trading, relax risk gates, increase trade frequency, or imply production readiness. LIVE use still requires readiness gates, reconciliation, kill-switch, no-submit prechecks, and operator controls.
