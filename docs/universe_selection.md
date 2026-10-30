# Canonical universe selection

`alphaforge.symbol_selector.build_selected_universe` is the single protected
authority for autonomous symbol eligibility and ranking in BACKTEST, PAPER and
LIVE_PRECHECK. Portfolio allocation, correlation exposure and account capacity
remain downstream portfolio-risk concerns.

The authority consumes `UniverseConstraints` plus one timestamp-bound batch
market snapshot. Required evidence is symbol/instrument identity, observation
time, 24-hour quote volume, spread and volatility. A configured market-cap
filter additionally requires a value, source and observation time from the
`MarketCapSnapshotProvider` boundary. AlphaForge has no built-in market-cap
source and never substitutes zero or silently disables a configured filter.

Eligibility is applied before ranking: quote/instrument eligibility, explicit
exclusions, market-cap bounds, minimum volume, unsafe spread/slippage,
liquidity safety, abnormal measured funding and the top-N liquid pool. Missing, stale, malformed and
non-finite required inputs become explicit `UNAVAILABLE`, `STALE` or `INVALID`
states. Optional slippage or regime evidence receives a recorded uncertainty
penalty; it never becomes a favorable zero.

`opportunity_ranking_v1` is deterministic and intentionally simple:

```text
score =
    0.40 * relative log-volume quality
  + 0.30 * spread quality
  + 0.15 * canonical liquidity quality
  + 0.15 * relative volatility opportunity
  + regime fit
  - expected-slippage penalty
  - unavailable-optional-evidence penalty (slippage, funding or regime)
```

Unsafe spread, slippage or liquidity is an eligibility failure and therefore
cannot be offset by volatility. Scores are ordered descending and symbol name
is the stable secondary key. Provider order, mapping order and set iteration do
not affect the result.

Every cycle is written to immutable `universe_selection_cycles` and
`universe_selection_candidates` rows. The record contains the full candidate
snapshot, selection and exclusion reasons, component scores, evidence states,
config/strategy/universe/evidence hashes, exact Git identity, execution mode,
provider provenance and schema/ranking versions. Replaying an identical bound
snapshot is idempotent; a conflicting payload for the same cycle identity
fails closed.
