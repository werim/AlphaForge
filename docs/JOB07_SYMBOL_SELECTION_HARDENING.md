# JOB-07 — Symbol Selection Hardening

Status: execution-aware symbol selection hardening applied.

No threshold loosening.
No trade-frequency increase.

This job intentionally increases rejection pressure under poor execution conditions.

---

# Objective

Transform symbol selection from:

```text
movement detection
```

into:

```text
execution-aware opportunity filtering
```

A symbol with:

- high spread,
- spoofed orderbook,
- fake breakout probability,
- unstable funding,
- excessive correlation,
- panic conditions,

is not a good opportunity even if price moves aggressively.

---

# Existing healthy foundation

`src/alphaforge/symbol_selector.py`
already included:

- volume filter,
- spread filter,
- liquidity filter,
- volatility filter,
- chop filter,
- panic filter,
- trend/range classification.

This was already stronger than many retail scanners.

---

# Hardening added

New reject dimensions:

- SPOOF_RISK
- FAKEOUT_RISK
- FUNDING_ANOMALY
- CORRELATION_OVEREXPOSURE
- LOW_ORDERBOOK_ALIGNMENT

New config gates:

- max_spoof_risk
- max_fakeout_risk
- max_abs_funding_rate_pct
- max_correlation_exposure
- min_abs_orderbook_imbalance

---

# Why these matter

## SPOOF_RISK

Orderbook imbalance can be fake.

Aggressive spoofing environments create:

- false breakouts,
- poor fills,
- momentum traps.

---

## FAKEOUT_RISK

A technically valid structure can still fail due to:

- thin liquidity,
- unstable participation,
- exhaustion behavior.

Rejecting fakeout-prone setups improves survivability.

---

## FUNDING_ANOMALY

Extreme funding often signals:

- crowded positioning,
- squeeze conditions,
- unstable continuation quality.

This does not always mean reversal.

But it increases execution uncertainty.

---

## CORRELATION_OVEREXPOSURE

Multiple symbols can secretly represent:

```text
one trade
```

Correlation-aware rejection prevents:

- hidden leverage,
- cluster drawdowns,
- synchronized failure.

---

## LOW_ORDERBOOK_ALIGNMENT

Weak or neutral orderbook imbalance can indicate:

- no directional sponsorship,
- poor continuation probability,
- unreliable trigger quality.

---

# New scoring behavior

Added:

```text
microstructure_penalty
```

This penalizes:

- spoof risk,
- fakeout risk,
- funding anomaly,
- correlation overexposure.

The selector is now:

```text
more selective
```

rather than:

```text
more active
```

This is intentional.

---

# Regime improvements

Selector now explicitly emits:

```text
PANIC
TREND
RANGE
UNFAVORABLE
```

instead of binary trend/non-trend behavior.

---

# Expected healthy behavior

Healthy hardened selector should:

- reduce low-quality executions,
- reduce thin-liquidity participation,
- reduce chop exposure,
- reduce fake breakout entries,
- reduce correlated exposure clusters.

Trade count may decrease.

This is expected and healthy.

---

# Remaining blockers

Still remaining:

1. PAPER/LIVE parity still needs stronger enforcement.
2. exchange safety gates still incomplete.
3. reconciliation layer still needs hardening.
4. adaptive risk allocation still absent.
5. live readiness evidence still incomplete.

---

# Next mandatory job

```text
JOB-08 — Paper/Live Parity Guard
```

because a hardened selector is only trustworthy if:

```text
PAPER and LIVE use the same decision contract
```

without hidden bypasses.

---

# Production stance

Current state:

```text
symbol selection is now significantly more execution-aware
```

But:

```text
execution safety still depends on parity and runtime enforcement.
```
