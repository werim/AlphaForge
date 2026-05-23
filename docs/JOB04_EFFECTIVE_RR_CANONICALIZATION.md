# JOB-04 — Effective RR Canonicalization

Status: canonical execution-adjusted RR contract introduced.

No threshold loosening.
No trade-frequency increase.
No architecture rewrite.

---

# Objective

Ensure AlphaForge distinguishes between:

```text
raw_rr
```

and:

```text
effective_rr
```

where:

```text
effective_rr = raw_rr - execution_cost_penalties
```

Execution penalties include:

- spread,
- slippage,
- latency,
- funding,
- liquidity degradation.

---

# Why this matters

A strategy can appear profitable while being unexecutable.

This is one of the most common failure modes in:

- retail crypto bots,
- optimistic backtests,
- candle-only simulators.

A system that stores:

```text
effective_rr == raw_rr
```

while non-zero execution costs exist is not execution-aware.

It is merely cost-blind.

---

# Current code-backed state

Existing shared cost model already existed in:

```text
src/alphaforge/execution.py
```

through:

```python
build_execution_cost_model(...)
```

This already computes:

- spread_penalty
- slippage_penalty
- latency_penalty
- funding_penalty
- liquidity_penalty

and:

```text
total_penalty
```

This is healthy.

However, multiple paths still used:

```text
raw_rr fallback masquerading as effective_rr
```

especially in persistence and reject rows.

---

# Canonical helper added

New module:

```text
src/alphaforge/effective_rr.py
```

New contract:

```python
calculate_effective_rr(raw_rr, execution_ctx)
```

Returns:

- raw_rr
- effective_rr
- total penalty
- individual penalty components
- missing execution fields
- execution completeness classification

This creates a single canonical semantic contract for:

- BACKTEST
- PAPER
- LIVE pre-submit checks
- reject analytics
- expectancy analytics
- SQL diagnostics

---

# Canonical formula

```text
effective_rr = max(raw_rr - total_execution_penalty, 0)
```

Where:

```text
total_execution_penalty =
spread_penalty
+ slippage_penalty
+ latency_penalty
+ funding_penalty
+ liquidity_penalty
```

---

# Important integrity rules

## Rule 1

raw_rr must never be overwritten.

---

## Rule 2

effective_rr must always represent:

```text
execution-adjusted RR
```

never merely copied raw_rr.

---

## Rule 3

Missing execution fields must remain visible.

Never silently hide:

- missing spread
- missing slippage
- missing latency
- missing liquidity

behind optimistic fallback values.

---

# Diagnostics added

File:

```text
sql/diagnostics/job04_effective_rr_canonicalization.sql
```

Includes:

1. raw_rr/effective_rr variability
2. suspicious raw_rr fallback cases
3. low effective_rr distribution
4. execution_ctx_missing by RR bucket

---

# Expected healthy output

Healthy execution-aware runtime should show:

```text
effective_rr != raw_rr
```

for many rows where:

- spread > 0
- slippage > 0
- latency > 0
- funding != 0

If nearly all rows show:

```text
effective_rr == raw_rr
```

then execution realism is likely fake or incomplete.

---

# Remaining blockers

Still remaining after JOB-04:

1. Some persistence paths still need migration to canonical helper.
2. execution_ctx_missing enforcement is incomplete.
3. BACKTEST/PAPER/LIVE still partially divergent.
4. lifecycle parity still not exact.
5. symbol selection still not fully execution-aware.

---

# Next mandatory job

```text
JOB-05 — Execution Context Population
```

because canonical RR only matters if execution context itself is trustworthy.

---

# Production stance

Current state:

```text
Execution-aware semantics are now explicitly defined.
```

But:

```text
production execution realism still depends on complete execution context propagation.
```
