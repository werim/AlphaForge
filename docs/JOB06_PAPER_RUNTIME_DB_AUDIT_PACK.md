# JOB-06 — Paper Runtime DB Audit Pack

Status: reusable PAPER runtime SQLite forensic audit pack added.

No threshold loosening.
No trade-frequency increase.
No architecture rewrite.

---

# Objective

Turn PAPER runtime SQLite artifacts into a trustworthy forensic evidence source.

The audit pack answers:

```text
Is the PAPER runtime producing believable execution-aware decision data?
```

rather than merely:

```text
Did rows get written?
```

---

# Why this matters

Without DB-level auditability:

- expectancy can be fake,
- reject quality cannot be trusted,
- lifecycle realism cannot be verified,
- execution-awareness may only exist in code comments.

Production-grade systems require:

```text
artifact-backed validation
```

not optimistic assumptions.

---

# Audit pack added

File:

```text
sql/diagnostics/job06_paper_runtime_db_audit.sql
```

Usage:

```bash
sqlite3 data/alphaforge_runtime.sqlite < sql/diagnostics/job06_paper_runtime_db_audit.sql
```

Read-only.
No schema mutation.

---

# What this audit proves

The audit can prove:

- PAPER rows exist,
- final vs ai_internal rows are separated,
- reject rate is measurable,
- reject reasons exist,
- score variability exists,
- raw_rr/effective_rr variability exists,
- lifecycle rows exist,
- execution context completeness is measurable,
- duplicate/conflicting final decisions exist or not,
- lifecycle ordering anomalies exist or not,
- suspicious raw_rr fallback cases exist or not.

---

# What this audit cannot prove

Without a real runtime PAPER artifact, this audit cannot prove:

- exchange connectivity quality,
- real spread realism,
- real slippage realism,
- actual latency quality,
- actual profitability,
- actual forward expectancy,
- real fill quality,
- live survivability.

It validates:

```text
data integrity and realism consistency
```

not market profitability.

---

# Key diagnostics

## 1. Reject rate

Healthy execution-aware systems are selective.

Extremely low reject rates may indicate:

- weak filtering,
- bypassed reject engine,
- placeholder acceptance,
- execution-unaware logic.

---

## 2. Score variability

If:

```text
distinct_score_values <= 1
```

then:

```text
SCORING_OR_REGIME_PIPELINE_FAILURE
```

is likely.

---

## 3. effective_rr realism

Rows where:

```text
effective_rr == raw_rr
```

while costs are non-zero are suspicious.

This can indicate:

- raw_rr fallback,
- fake execution realism,
- incomplete persistence migration.

---

## 4. execution_ctx_missing

High:

```text
execution_ctx_missing_ratio
```

means execution realism is not trustworthy.

---

## 5. lifecycle ordering

Healthy chains resemble:

```text
SIGNAL_CREATED
-> SIGNAL_ACCEPTED
-> WAITING_ENTRY_ZONE
-> ENTRY_TRIGGERED
-> ORDER_PLACED
-> POSITION_OPENED
-> POSITION_CLOSED
```

or:

```text
SIGNAL_CREATED
-> SIGNAL_REJECTED
```

Large anomalies indicate lifecycle integrity drift.

---

# Classification framework

The audit outputs one of:

```text
HEALTHY_SELECTIVITY
DATA_INTEGRITY_FAILURE
EXECUTION_CONTEXT_FAILURE
SCORING_OR_REGIME_PIPELINE_FAILURE
INSUFFICIENT_SAMPLE
```

---

# Expected healthy PAPER artifact

Healthy PAPER runtime evidence should show:

- non-trivial reject rate,
- meaningful score variability,
- meaningful effective_rr variability,
- low duplicate final rows,
- low UNKNOWN reject reasons,
- lifecycle completeness,
- explicit execution context completeness.

---

# Remaining blockers after JOB-06

Still remaining:

1. symbol selection still partially heuristic.
2. backtest/runtime parity still incomplete.
3. exchange safety gates not fully enforced.
4. live reconciliation not fully hardened.
5. adaptive risk allocation not implemented.

---

# Next mandatory job

```text
JOB-07 — Symbol Selection Hardening
```

because execution-aware systems fail when symbol selection ignores:

- liquidity depth,
- spoof risk,
- volatility fit,
- fakeout probability,
- correlation exposure.

---

# Production stance

Current state:

```text
PAPER runtime auditability is now measurable.
```

But:

```text
real production readiness still depends on sustained artifact quality over time.
```
