# JOB-22 Persisted Rollback / Emergency No-Submit Evidence

## Objective

Close the lowest-risk remaining emergency-control evidence gap after JOB-20 runtime heartbeat persistence and JOB-21 PAPER audit integrity hardening. LIVE readiness must not accept configured or in-memory rollback booleans as proof that emergency controls work.

## Pre-coding findings

- `LiveReadinessEvaluator._check_operational(...)` required rollback evidence fields but accepted them from the provided operational snapshot without persisted provenance verification.
- The real kill-switch barrier already existed in `RuntimeOrchestrator._process_symbol(...)` through `_evaluate_runtime_risk(...)`, before `_execute(...)` can submit an order.
- The canonical reconciliation engine already emits fail-closed findings and dry-run/shadow remediation recommendations.
- The dashboard already exposed a read-only readiness probe matrix; `rollback_ready` needed a persisted evidence surface rather than a new control route.

## Implementation

### Persisted evidence contract

Added `src/alphaforge/rollback_evidence.py` with append-only evidence storage in `live_rollback_validation_evidence`.

Stored validation fields include:

- `validation_id`
- `recorded_at`
- `evidence_status`
- `rollback_evidence_source`
- `kill_switch_block_verified`
- `no_submit_on_kill_switch_verified`
- `fail_closed_reconciliation_verified`
- `repair_actions_non_mutating_verified`
- `execution_mutation_attempt_count`
- `blocking_reasons`
- allowlisted `evidence_payload`

The loader fails closed for missing, stale, future-dated, malformed, incomplete or mutation-bearing evidence.

### Deterministic validator

`run_deterministic_rollback_validation(...)` exercises existing safety paths only:

1. Builds a LIVE orchestrator with the global kill switch active and a tracking execution adapter.
2. Runs a deterministic candidate through the real `_process_symbol(...)` guard path.
3. Verifies rejection reason `GLOBAL_KILL_SWITCH`, a rejected lifecycle outcome, zero executions and zero adapter `submit` calls.
4. Passes an orphan-order snapshot through `ReconciliationEngine`.
5. Verifies fail-closed findings and remediation recommendations that remain `dry_run=True`, `shadow_mode=True`, and operator-approval-required.
6. Compares operational incident row counts before and after validation, so startup validation cannot silently contaminate incident history.

This validator must be explicitly invoked. It is not wired as an uncontrolled runtime side effect.

### LIVE readiness consumption

Updated `src/alphaforge/live_readiness.py` so `rollback_ready` consumes fresh persisted rollback evidence. Optimistic rollback fields supplied only in an in-memory observability snapshot no longer qualify LIVE.

### Read-only dashboard visibility

Added `src/alphaforge/dashboard/rollback_queries.py` and extended the existing readiness probe matrix so the `rollback_ready` row displays:

- persisted evidence surface,
- evidence status and source,
- kill-switch proof,
- no-submit proof,
- fail-closed reconciliation proof,
- non-mutating repair proof,
- execution mutation attempt count,
- blocking reasons.

No dashboard action route, validation trigger, kill-switch toggle, LIVE activation control, order control or configuration mutation endpoint was introduced.

## Tests

Added or updated regression coverage for:

- missing rollback evidence failing closed,
- deterministic no-submit proof persistence,
- stale and future-dated evidence failing closed,
- failed/mutating evidence being unable to report `COMPLETE`,
- optimistic snapshot flags without persisted rollback evidence failing qualification,
- dashboard showing missing persisted evidence as a blocker,
- dashboard showing completed no-submit evidence read-only.

GitHub Actions validation:

- workflow: `Tests`
- run: `#903`
- conclusion: `success`
- validated head commit: `c55036e710125bfa1064d323469e0fb0a11a3c2c`

## Explicit safety posture

- No LIVE trading enablement.
- No exchange transport invocation.
- No submit/cancel/modify/replace/close behavior added.
- No score, raw RR, effective RR, threshold, regime or symbol-selection change.
- No trade-frequency increase.
- No dashboard mutation control.
- Validation evidence is proof of a safety check, not permission to trade.

## Remaining blockers

LIVE remains **NOT LIVE-READY** unless every independent readiness blocker passes, including runtime heartbeat freshness, mode parity, reconciliation evidence, observability/alert evidence, operator/canary/shadow controls, and all other qualification checks.
