# JOB-05 — Execution Context Population

Status: execution-context completeness contract introduced.

No threshold loosening.
No trade-frequency increase.
No architecture rewrite.

---

# Objective

Ensure AlphaForge execution realism is based on:

```text
explicit execution context
```

rather than:

```text
silent optimistic defaults
```

Execution-aware systems fail when:

- spread is fake,
- slippage is zero-filled,
- latency is ignored,
- liquidity is assumed,
- missing data silently becomes 0.0.

This job makes missingness explicit and queryable.

---

# Canonical execution-context contract

New module:

```text
src/alphaforge/execution_context_contract.py
```

Defines required fields:

- volume_24h_usdt
- spread_pct
- expected_slippage_pct
- latency_ms
- liquidity_score
- funding_rate_pct
- orderbook_imbalance
- volatility_regime

Adds canonical audit helper:

```python
with_execution_context_audit(ctx)
```

which enriches context with:

```text
execution_ctx_missing
execution_ctx_missing_fields
execution_ctx_completeness
```

---

# Why this matters

Before JOB-05:

```text
0.0
```

could ambiguously mean:

- truly zero,
- unknown,
- unavailable,
- placeholder.

This is dangerous because:

```text
fake low costs
=> fake high effective_rr
=> fake expectancy
```

---

# Canonical semantics

## complete

All required execution fields exist.

---

## partial

Some fields missing.

Still analyzable.

Must remain visible.

---

## unavailable

Most execution fields missing.

These rows should not be treated as trustworthy execution realism evidence.

---

# Important integrity rule

Never silently convert:

```text
UNKNOWN
UNAVAILABLE
None
```

into:

```text
0.0
```

for execution realism calculations.

---

# Diagnostics added

File:

```text
sql/diagnostics/job05_execution_context_population.sql
```

Includes:

1. execution context completeness by mode
2. missing execution field detail
3. placeholder/unavailable marker distribution
4. suspicious optimistic rows
5. execution completeness by lifecycle state

---

# Expected healthy output

Healthy runtime/backtest artifacts should show:

```text
execution_ctx_missing_ratio
```

decreasing over time.

And:

```text
high effective_rr rows
```

should rarely coexist with:

```text
execution_ctx_missing = 1
```

---

# Code-backed findings

Current architecture already contains:

- execution cost model,
- spread/slippage normalization,
- volatility regime estimation,
- liquidity-aware penalties.

This is healthy.

However:

- some fields still use optimistic defaults,
- some persistence paths do not expose completeness explicitly,
- backtest/runtime completeness is still partially divergent.

---

# Remaining blockers

Still remaining:

1. runtime/backtest must fully adopt canonical execution-context audit helper.
2. effective_rr persistence migration still incomplete.
3. symbol selection still partially independent from execution completeness.
4. paper/live parity not fully proven.
5. exchange health and stale data protection still need dedicated gating.

---

# Next mandatory job

```text
JOB-06 — Paper Runtime DB Audit Pack
```

because runtime PAPER artifacts must now prove:

- reject realism,
- lifecycle consistency,
- execution completeness,
- effective_rr variability,
- persistence integrity.

---

# Production stance

Current state:

```text
Execution-context missingness is now explicit.
```

But:

```text
full execution realism still depends on runtime adoption and exchange-quality data.
```
