# Issue #565 — REGIME_GUIDED signal-quality research

## Verdict

**INCONCLUSIVE. No production-semantic promotion is justified by this PR.**

This change creates the evidence machinery needed to test H1–H6 without changing
the current REGIME_GUIDED decision authority. No threshold, score weight, SL/TP,
sizing, portfolio-risk, order-flow, LIVE authorization, or active PAPER campaign
state is changed.

No new calibration/OOS/untouched-holdout run is claimed here. The existing
POST563T01 accepted examples are execution-geometry evidence, not proof that any
new signal feature or timeframe stack has positive expectancy. A future frozen
research run must bind git SHA, config hash, data hash, universe hash, stack ID,
segment role, and segment ID before this report can move beyond INCONCLUSIVE.

## Implemented research surface

- H1: raw 1h MA delta, ATR denominator, and volatility-normalized regime strength.
- H2: deterministic 15m HH/HL vs LH/LL state, ATR-normalized pullback depth,
  support/resistance distance, swing age, and range-compression ratio.
- H3: executable stop distance divided by setup ATR; structural SL is never
  widened/narrowed by the research code.
- H4: deterministic reclaim and two-bar persistence confirmation variants.
- H5: calibration buckets, Brier score, ECE, Wilson 95% observed-rate interval,
  plus side/setup/regime breakdown from complete non-ambiguous resolved labels.
- H6: explicit 1h→15m→1m, 4h→1h→15m, 1d→4h→1h, and optional
  1d-context + 4h→1h→15m research identities.

All feature functions consume closed candles at-or-before the decision timestamp.
Missing ATR/structure/confirmation evidence stays NULL/None. Future candles are
excluded. The research SQLite store refuses a database containing campaign/run
authority tables and uses deterministic upserts.

## Timeframe semantics

Canonical freshness support is extended only so 4h and 1d closed-candle contexts
can be classified correctly by the existing MTF freshness gate. This does not
make wider stacks authoritative. The version-controlled files under
`config/research/mtf/` are experiment specifications, not runtime defaults.

The current fixed direction thresholds are intentionally not changed in this
issue. Wider-stack evidence must first show whether volatility-normalized
features make those thresholds comparable. The frozen identity therefore binds
a `timeframe_semantics_hash` and requires each non-baseline stack to be marked
`NORMALIZED` or `JUSTIFIED`; an `UNREVIEWED` wider stack is promotion-blocked
even if its headline metrics look better. Until then, any wider-stack result is
research-only and cannot be promoted merely because it trades less or shows
higher headline RR/win rate.

## Promotion boundary

A report may return `SUPPORTED_FOR_SEPARATE_PRODUCTION_CHANGE` only when the
same frozen candidate stack beats the baseline on mean net R after costs in both
OOS and untouched holdout, does not worsen max drawdown in either segment, and
meets an identity-bound minimum complete sample count. That verdict still only
supports opening a separate production-semantic issue; it never mutates current
authority.

Otherwise the allowed outcomes remain `PASS_WITH_FINDINGS` or `INCONCLUSIVE`.

## Required next evidence

1. Freeze calibration, OOS, and untouched-holdout identities before evaluation.
2. Run baseline and candidate stacks over identical symbol/date universes and
   identical non-timeframe controls.
3. Persist complete/ambiguous/incomplete outcome status separately.
4. Compare net R after costs, MFE/MAE, drawdown/tail loss, cost drag, holding
   time, and stability by symbol/side/setup/regime.
5. Keep reused windows explicitly marked as reused; never relabel them untouched.
6. If evidence supports one narrow policy change, open a separate issue with
   BACKTEST/PAPER/LIVE_PRECHECK parity regressions and fresh PAPER qualification.

Current production baseline remains authoritative.
