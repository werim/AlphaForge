# JOB-08 — Paper/Live Parity Guard

Status: PAPER/LIVE parity contract formalized.

No threshold loosening.
No trade-frequency increase.
No architecture rewrite.

LIVE fail-closed posture remains mandatory.

---

# Objective

Guarantee that:

```text
PAPER and LIVE use the same decision intelligence
```

rather than:

```text
different hidden logic paths
```

The only acceptable major difference between PAPER and LIVE should be:

```text
execution adapter behavior
```

not:

- scoring,
- reject logic,
- lifecycle semantics,
- effective_rr semantics,
- symbol selection.

---

# Existing healthy architecture

`src/alphaforge/runtime.py`
already contained:

- LIVE scanner provenance gate,
- exchange connectivity gate,
- live qualification gate,
- reconciliation fail-closed behavior,
- `_build_mode_parity_evidence(...)`.

This is strong infrastructure.

---

# New parity contract

Added:

```text
src/alphaforge/parity_guard.py
```

Canonical parity fields:

- decision
- reason
- order_type
- confidence
- score
- effective_rr

These fields define:

```text
behavioral equivalence
```

between PAPER and LIVE pre-submit evaluation.

---

# Why this matters

A system can appear profitable in PAPER while secretly using:

- different filters,
- different reject logic,
- different execution assumptions,
- different lifecycle semantics.

This creates:

```text
parity illusion
```

which is one of the most dangerous trading-system failure modes.

---

# New helper behavior

`compare_paper_live_precheck(...)`
now explicitly measures:

- missing parity fields,
- mismatched parity fields,
- sample-level parity pass/fail.

And:

`summarize_parity(...)`
produces:

```text
COMPLETE
or
INCOMPLETE
```

parity evidence.

---

# Important production rule

LIVE mode must never bypass:

- selector hardening,
- reject engine,
- execution-aware RR,
- lifecycle integrity,
- reconciliation gates.

If LIVE uses shortcuts while PAPER uses stricter logic:

```text
paper expectancy becomes fiction
```

---

# SQL diagnostics added

File:

```text
sql/diagnostics/job08_paper_live_parity_guard.sql
```

Includes:

1. mode distribution
2. final decision parity
3. lifecycle parity coverage
4. reject reason divergence
5. suspicious effective_rr drift

---

# Expected healthy behavior

Healthy parity evidence should show:

- low mismatch count,
- low missing field count,
- similar effective_rr distributions,
- similar reject distributions,
- similar lifecycle coverage.

Some divergence is expected due to:

- live fills,
- latency,
- reconciliation,
- exchange timing.

But:

```text
core decision semantics must remain aligned
```

---

# Remaining blockers

Still remaining:

1. exchange safety gates still incomplete.
2. reconciliation hardening still partial.
3. adaptive risk allocation absent.
4. sustained live evidence absent.
5. full live readiness report not complete.

---

# Next mandatory job

```text
JOB-09 — Exchange Safety Gates
```

because parity alone is insufficient if:

- exchange health is degraded,
- stale market data leaks through,
- reconciliation cannot fail closed.

---

# Production stance

Current state:

```text
PAPER/LIVE behavioral parity is now explicitly measurable.
```

But:

```text
true production survivability still depends on exchange-side safety enforcement.
```
