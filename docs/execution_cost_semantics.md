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

The weighted mean scales price and quantity before summing, so individually
finite fills cannot overflow the intermediate notional or total quantity into
`inf/inf`. A result that still cannot be represented as a finite positive
price fails validation. Boolean price or quantity values are invalid evidence.

Executable raw RR from expected-fill geometry must also be finite. A
positive-risk geometry whose reward/risk quotient overflows returns `0.0` at
that boundary, so it cannot pass the minimum effective-RR gate on an infinite
value.

## Fees and other costs

The shared `evaluate_signal_decision()` boundary gates on
`build_decision_rr_metrics()` before quality/execution decisions, using expected
executable entry and the residual cost penalty. Entry slippage already represented
by expected fill is credited once; it must not be charged a second time. The
protected BACKTEST projection reads that actual decision stage. Legacy simulator
rows without this stage retain their explicit legacy provenance and are not
equivalent realized-PnL or three-mode parity evidence.

Fee and funding values are critical cost evidence in the pre-submit safety
contract. Missing fees are recorded in the cost model's missing fields rather
than silently becoming a zero-fee assumption. The existing
`REJECT_UNKNOWN_EXECUTION_CONTEXT` policy controls rejection of missing evidence;
its default remains fail-closed. A fee schedule may be `CONFIGURED` even when
market microstructure must be measured. Unknown funding is not a zero rate.
`UNAVAILABLE_BLOCKING` and `INVALID_FAKE_ZERO` cannot prove the protected parity
contract even when all compared surfaces contain the same invalid status.

Equal economic numbers do not erase provenance: PAPER expected-fill evidence is
`MODELLED`, whereas estimated BACKTEST/LIVE_PRECHECK expectations are `ESTIMATED`.
The existing strict parity checker continues to expose that difference; this
change does not convert it into a three-mode PASS or a LIVE authorization.

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

### Legacy realized cash ledger (CASH_LEDGER_V1)

`backtest_order.finalize` and SQL-backed PAPER closure share the cash identities in
`realized_accounting`: directional filled quantity times price movement gives gross
PnL; initial dollar risk is filled quantity times absolute filled-entry/initial-stop
distance. Complete cash costs are subtracted once, and net R divides by that risk.
The legacy `risk_pct` argument historically allocates **notional**, as does
`BacktestPortfolioState.notional_for`; this repair does not reinterpret it as a
stop-defined risk budget or enlarge exposure. `risk_scale` scales final quantity
once. Explicit `notional` overrides the base allocation before scaling.

Legacy candle simulation still fills at planned entry, uses SL-first diagnostics
for ambiguous candles, and explicitly liquidates at the last close on TIMEOUT.
Ambiguity cannot supply complete realized evidence. No partial-exit/managed-stop
lifecycle is implemented in this adapter, so parity with PAPER management is
unsupported. SQL PAPER replay remains idempotent; legacy equity and portfolio
cash deduplicate identified closed positions and use close-time ordering.

Historical candle/decision costs are not realized cash observations. Callers may
supply `realized_costs_usd` and an attributable `realized_cost_source` for an
individual full closure: entry/exit spread, entry/exit fee, exit slippage, signed
funding over the actual holding interval, and latency impact. Entry slippage must
be explicitly zero because the supplied fill already embeds its price effect.
Optional volatility/liquidity cash penalties remain explicit when applicable.
No historical fee/funding schedule or duration-independent funding is invented.
The caller owns scope and observation provenance; a numeric model estimate is
not independent historical measurement or statistical qualification.

Without these cash facts, `net_pnl_usdt`, net return percent, costs and net R stay
null with `realized_evidence_status=INCOMPLETE`. Equity, drawdown, profit factor,
expectancy and profile ranking cannot turn missing net cash into zero. A portfolio
with unknown realized cash fails subsequent risk checks. Price-only diagnostics
remain explicitly named `legacy_price_return_pct` and `legacy_approx_pnl_usdt`;
they are not net profit. SQL metadata and CSV retain the accounting version and
cost source. Existing historical rows are never rewritten or upgraded.

The walk-forward BACKTEST adapter requires `CASH_LEDGER_V1`, `COMPLETE` and an
attributable realized cost source in persisted diagnostics. Numeric historical
price-return fields alone cannot inherit this contract; legacy artifacts remain
unchanged and are rejected for cost-complete validation.

### Quality-gate diagnostics (QUALITY_GATE_DIAGNOSTIC_V2)

`_quality_gate_metrics` selects rejected shadow records using future `WOULD_TP`
outcomes. This is retrospective reporting, not causal selection, realized net R,
a deployable expected profit, or PAPER/LIVE acceptance/promotion authority.
Existing selection thresholds and reporting counts are preserved; `accepted_count`
in this namespace counts retrospective diagnostic records only.

V2 keeps baseline realized cash in `quality_gate_baseline_realized_net_pnl_usdt`.
Omitted/invalid baseline is unavailable, not zero. Counterfactual effective-RR mean
and size-adjusted total are explicitly R fields; the latter uses the configured
diagnostic size multiplier, not candidate position sizing or a cash conversion.
No sample means unavailable expectancy and an empty R sum of zero. Invalid scale
means unavailable scaled R. Candidate-provided dollar risk and risk_scale are
retained as unverified diagnostic inputs, never inferred from balance/notional.

`quality_gate_counterfactual_cash_usdt` and the retained compatibility key
`baseline_plus_quality_gate_net_pnl` are always null: there is no attributable
causal closure/cost model to convert this outcome-conditioned sample to cash.
Even supplied candidate risk or partial cost fields cannot establish that model.
Effective RR already contains decision-time cost modeling; this diagnostic does
not subtract costs again or claim it is canonical realized net R. The #647 cash
ledger remains the only realized accounting authority.

Schema, units, selection basis, missing-evidence statuses and false decision-time/
promotion flags travel through both summary CSV and signal-quality JSON. Dashboard
consumers retain their existing reporting-only surface. Old reports are not
rewritten or retroactively assigned V2 provenance; an old mixed-unit number is
not repaired or validated by its continued presence in a historical artifact.
