# JOB-03 — Reject Persistence Integrity

Status: audit + diagnostics hardening.

This job does NOT loosen thresholds, increase trade frequency, or redesign the architecture.

The purpose is to guarantee that every rejected decision is:

- complete,
- canonical,
- queryable,
- non-duplicated,
- distinguishable from internal AI telemetry.

---

# Executive summary

AlphaForge reject persistence has improved substantially compared to the earlier duplicate/incomplete runtime state.

Current `dev` already contains:

- canonical `signal_id` generation,
- `phase=ai_internal_*` separation,
- reject review persistence,
- lifecycle reject events,
- final-vs-internal row distinction.

However, reject analytics can still become corrupted if downstream SQL or exports:

- count AI-internal rows as executable final decisions,
- accept `UNKNOWN` reject reasons silently,
- treat raw RR fallback as execution-adjusted effective RR,
- fail to validate lifecycle completeness.

This job formalizes the integrity rules and adds reusable diagnostics.

---

# Current persistence architecture

## Runtime final decision persistence

Runtime final decisions are persisted through runtime/orchestrator paths and canonical final rows.

Expected semantics:

```text
phase = final OR NULL (legacy)
```

These rows represent executable system decisions.

---

## AI internal telemetry persistence

`src/alphaforge/ai_brain.py::_persist_decision(...)`
persists internal scoring telemetry rows.

Current semantics:

```text
phase = ai_internal_real
phase = ai_internal_virtual
```

These rows are:

- useful for diagnostics,
- useful for explainability,
- NOT canonical executable final decisions.

This separation is healthy and should remain.

---

# Code-backed findings

## 1. Internal rows are intentionally separated

`AIBrain._persist_decision(...)` now explicitly writes:

```python
phase=f"ai_internal_{phase}"
```

This is a major integrity improvement because it prevents:

- runtime analytics double-counting,
- reject-rate inflation,
- false duplicate interpretation.

Classification:

```text
HEALTHY_SHARED_PIPELINE
```

for internal telemetry separation.

---

## 2. Reject reason canonicalization exists but still needs auditing

`canonical_reject_reason(...)` is used before persistence.

However, downstream diagnostics still must detect:

- blank reject reasons,
- UNKNOWN fallback abuse,
- conflicting reasons for same signal.

Classification:

```text
PARTIAL_PIPELINE_DIVERGENCE
```

because integrity enforcement still depends on downstream SQL discipline.

---

## 3. effective_rr can still degrade to raw RR fallback

Some persistence paths write:

```python
effective_rr = signal.get("risk_reward")
```

rather than guaranteed execution-adjusted RR.

Impact:

A row can appear execution-aware while actually storing raw RR.

This is especially dangerous for:

- reject quality analysis,
- expectancy calibration,
- spread/slippage realism.

Classification:

```text
EXECUTION_CONTEXT_FAILURE
```

risk.

---

## 4. Lifecycle reject rows and final decisions are not guaranteed 1:1

Backtest lifecycle rows and runtime final decision rows can diverge.

Examples:

- lifecycle reject exists but no final decision row,
- final decision row exists but no canonical lifecycle reject row,
- reject reasons differ.

This is diagnosable via the new SQL reconciliation query.

Classification:

```text
LIFECYCLE_INTEGRITY_FAILURE
```

risk.

---

# Required integrity invariants

A canonical final rejected row must contain:

| Field | Required |
|---|---|
| signal_id | YES |
| symbol | YES |
| mode | YES |
| decision=REJECTED | YES |
| reject_reason | YES |
| score | YES |
| rr/raw_rr | YES |
| effective_rr | YES |
| created_at | YES |
| execution context or explicit missingness | YES |

---

# SQL diagnostics added

File:

```text
sql/diagnostics/job03_reject_persistence_integrity.sql
```

Includes:

1. final rejected row completeness
2. final vs ai_internal separation
3. duplicate final reject detection
4. conflicting final decisions
5. rejected lifecycle completeness
6. raw_rr == effective_rr suspicious cases
7. UNKNOWN/blank reject reason drilldown
8. lifecycle vs final reconciliation

---

# Expected healthy outputs

Healthy runtime/backtest artifacts should show:

```text
missing_signal_id = 0
missing_symbol = 0
missing_reject_reason = 0
unknown_reject_reason ~= near zero
duplicate_final_rejected_rows = 0
```

and:

```text
final rows clearly separated from ai_internal rows
```

---

# Production blockers still remaining

1. effective_rr not fully canonicalized yet
2. execution context completeness not enforced everywhere
3. lifecycle/final parity not guaranteed
4. backtest/runtime still partially divergent
5. reject analytics can still be corrupted by careless SQL

---

# Minimal safe next step

Next mandatory job:

```text
JOB-04 — Effective RR Canonicalization
```

because reject-quality analytics are not trustworthy until:

```text
effective_rr
!=
raw_rr
when execution penalties exist
```

---

# Production stance

Current state:

```text
Improved auditability
!=
production readiness
```

The system is now much closer to trustworthy forensic analysis, but executable expectancy realism still depends on JOB-04 and JOB-05.
