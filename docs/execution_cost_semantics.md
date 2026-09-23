# Canonical execution-cost semantics

AlphaForge uses one canonical price-deviation contract for execution evidence:

```text
strategy entry -> expected fill -> actual fill
```

This contract is evidence semantics. It does not change strategy scoring, thresholds,
position sizing, order selection, authorization, or whether an order is submitted.

## Canonical quantities

For LONG positions, higher fills are adverse. For SHORT positions, lower fills are
adverse. All price deltas are therefore side-normalized so **positive means adverse
execution** and negative means improvement.

Let `s = +1` for LONG and `s = -1` for SHORT.

```text
expected_execution_cost_price
  = s * (expected_fill - entry)

realized_execution_deviation_price
  = s * (actual_fill - expected_fill)

total_realized_execution_cost_price
  = s * (actual_fill - entry)
```

When an actual fill exists:

```text
total_realized_execution_cost
  = expected_execution_cost
  + realized_execution_deviation
```

All percentage quantities use **strategy entry** as the denominator:

```text
cost_pct = side_normalized_price_delta / entry
cost_bps = cost_pct * 10_000
```

Do not use `expected_fill` or `actual_fill` as an alternate denominator.

## Evidence timing

Decision/pre-submit evidence may contain:

- `entry`
- `expected_fill`
- expected execution cost
- expected-fill provenance
- decision timestamp

It must not depend on future fill information.

Fill-time evidence may additionally contain:

- `actual_fill`
- realized execution deviation
- total realized execution cost
- actual-fill provenance
- fill timestamp

When actual fill evidence is missing, realized and total quantities are **NULL /
UNAVAILABLE**, never numeric zero.

## Provenance

Canonical provenance values are:

- `ACTUAL`: exchange/runtime-observed realized fill
- `ESTIMATED`: pre-submit modeled expectation
- `ASSUMED`: explicit assumption
- `MODELLED`: deterministic simulator evidence, especially PAPER
- `UNAVAILABLE`: no defensible evidence

PAPER simulated fills use `MODELLED`; they must never be labeled exchange-observed
`ACTUAL`. LIVE adapter fills may be labeled `ACTUAL` only after a fill exists.

## PAPER and LIVE parity

Both PAPER and LIVE evidence are normalized through
`build_execution_cost_semantics()`.

- PAPER: expected fill = modelled; simulated actual fill = modelled.
- LIVE: expected fill = estimated pre-submit; actual fill = exchange/runtime result.
- LIVE_PRECHECK: no actual fill is manufactured.

The definition of each metric is identical across modes; only provenance differs.

## Partial fills

When a fill ledger is available, canonical actual fill is quantity-weighted:

```text
weighted_fill = sum(price_i * quantity_i) / sum(quantity_i)
```

Invalid/non-positive fill rows are not converted to zero. Missing fill ledgers leave
realized evidence unavailable.

## Fees and other costs

The pre-submit `evaluate_execution_safety()` contract rejects supplied non-finite,
malformed or boolean spread, slippage, latency, liquidity, funding, fee and orderbook
numbers as `EXECUTION_CONTEXT_UNAVAILABLE`. Their names appear in `missing_fields`
and the active evidence status is `UNAVAILABLE_BLOCKING`. This invalid-input guard
also applies when `REJECT_UNKNOWN_EXECUTION_CONTEXT` is false; that option only
relaxes missing evidence, not corrupt supplied numbers. Optional absent fields keep
their existing policy. Invalid effective RR is returned as `None` and rejected with
`LOW_EFFECTIVE_RR` and `FINITE_RR_REQUIRED` evidence. Valid finite inputs and all
configured thresholds retain their existing behavior.

Fees, funding, spread assumptions, latency penalties, liquidity penalties, and
volatility penalties remain independently attributable.

The three canonical price-deviation quantities above **do not include fees**.
Higher-level net-cost / effective-RR metrics may combine separate components, but must
not relabel them as fill-price deviation.

## Legacy `actual_slippage_pct`

The existing `closed_trade_reviews.actual_slippage_pct` column is retained only for
compatibility. Where AlphaForge writes it from canonical execution metrics, its meaning
is:

```text
TOTAL_REALIZED_EXECUTION_COST_PCT
= side-normalized strategy entry -> actual fill cost
```

It is **not** the same as `realized_execution_deviation_pct`
(expected fill -> actual fill). New code should use the explicit canonical fields in
`execution_metrics`.

Legacy review fallbacks must not infer a realized fill by copying `entry_price`.

## Persistence

Canonical evidence can be persisted as the `execution_cost_semantics` object in
runtime/lifecycle/provenance payloads. The object records:

- entry
- expected fill
- actual fill when available
- all price / percentage / bps decompositions
- expected- and actual-fill provenance
- decision and fill timestamps
- reference-price and sign conventions
- fee treatment

Historical rows are not backfilled or reinterpreted without explicit provenance.
