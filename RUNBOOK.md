# AlphaForge Phase 6 Operator Runbook

## Startup checklist
- Confirm `EXECUTION_MODE` is `PAPER` or `LIVE_PRECHECK`; real `LIVE` order submission remains blocked for Phase 6.
- Confirm the persisted runtime control state, latest readiness report, runtime heartbeat, and release gate snapshot exist.
- Confirm the global kill switch is inactive before starting a non-mutating canary and active before any emergency stop test.
- Confirm exchange connectivity used for LIVE_PRECHECK is read-only and no submit/cancel/modify credentials are exercised.

## Pre-canary checklist
- Generate a unique `release_id` for the candidate build.
- Persist `runbook_evidence` with this file hash.
- Persist a non-mutating rollback verification event: kill switch verified, runtime stop verified, recovery state documented.
- Record test evidence and paper burn-in evidence in the release gate snapshot.
- Require an operator acknowledgement for the same `release_id` using the exact text:
  `I acknowledge AlphaForge Phase 6 canary risk and LIVE real orders remain disabled; release_id=<release_id>`
- Confirm canary scope: allowed symbols, maximum symbols, maximum notional, maximum risk percentage, and duration.

## Canary monitoring checklist
- Run canary only as `LIVE_PRECHECK` / no-submit.
- Monitor release blockers, operator ack status, reject spikes, runtime errors, reconciliation status, stale data, and mutation-attempt count.
- Stop immediately on `CANARY_SYMBOL_SCOPE_VIOLATION`, `CANARY_NOTIONAL_LIMIT`, `CANARY_RISK_LIMIT`, `CANARY_DURATION_EXCEEDED`, `CANARY_REJECT_SPIKE`, `CANARY_RUNTIME_ERROR_LIMIT`, `CANARY_OPERATOR_ACK_MISSING`, `CANARY_EVIDENCE_MISSING`, or `CANARY_MUTATION_ATTEMPT`.

## Kill switch procedure
- Activate the dashboard/runtime kill switch.
- Verify `runtime_control_state.kill_switch_active=1` and runtime status is `KILL_SWITCH_ACTIVE` or stopped.
- Verify readiness fails closed while the kill switch is active.
- Do not clear the kill switch until incident review and persisted evidence are complete.

## Rollback procedure
- Keep rollback non-mutating: do not submit, cancel, or modify exchange orders.
- Stop the runtime supervisor and confirm no running mode remains.
- Activate kill switch and persist rollback verification evidence.
- Confirm recovery state and reconciliation evidence are written to SQL.
- Revert the deployment using the operator-approved deployment mechanism for the environment, then rerun LIVE_PRECHECK only.

## Incident response
- Persist incident context, runtime snapshot, release snapshot, canary event, and readiness report.
- Treat missing evidence as unsafe and fail closed.
- Escalate any mutation attempt as a release blocker.

## Stale data response
- Stop canary when market data age exceeds configured thresholds.
- Persist stale-data symbols and fail-closed reason.
- Resume only after fresh data and a new release snapshot are persisted.

## Orphan order response
- Do not mutate exchange state automatically.
- Persist reconciliation findings and operator-reviewed repair recommendations.
- Keep repair actions dry-run/shadow until a future explicit live-order phase.

## Reconciliation failure response
- Stop canary on unresolved reconciliation mismatches.
- Persist mismatch count, orphan counts, and read-only exchange evidence.
- Require operator review before another canary run.

## Emergency stop
- Activate kill switch.
- Stop runtime.
- Persist rollback verification event.
- Export latest release, readiness, canary, runtime, and lifecycle evidence.

## Post-run evidence export
- Export `release_gate_snapshots`, `operator_acknowledgements`, `canary_run_events`, `rollback_verification_events`, `runbook_evidence`, latest readiness report, runtime state snapshots, and lifecycle/reject evidence.

## Merge/release checklist
- Tests passing evidence is recorded.
- Paper burn-in evidence is acceptable.
- Runbook hash is persisted.
- Rollback dry-run evidence is persisted.
- Canary remains LIVE_PRECHECK/no-submit.
- `live_order_submission_enabled=false`.
- Real LIVE orders remain blocked pending a future explicit phase.
