# AlphaForge Phase 9 PR 280 Follow-up Surgery Report

## Why the patch was needed
Reviewer follow-up identified four remaining blockers: source evidence immutability froze active RUNNING continuations too aggressively, clock skew was not measured against a canonical external source, recovery drill could resume after failed worker termination, and NULL aggregate evidence hashes could satisfy hash-linkage checks.

## Root cause
Phase 9 hardening treated all source runs as immutable immediately, used local clock progression as a weak proxy for skew, performed recovery resume after stop attempts without fail-fast enforcement, and compared aggregate hashes without requiring stored non-null evidence.

## Files changed
- `src/alphaforge/burnin_ops.py`: append-only vs terminal source baseline semantics, Binance read-only clock skew, fail-fast recovery termination, explicit aggregate hash missing/mismatch audit checks, non-null final hash linkage.
- `tests/test_phase9_burnin_ops.py`: adds RUNNING append-only, terminal immutability, clock skew PASS/FAIL/UNAVAILABLE, failed termination, and NULL/mismatch hash tests.
- `CHANGELOG.md`, `REPORT.md`, `VERSION.md`: documents the follow-up hardening.

## Runtime behavior changes
- RUNNING source runs are audited append-only: existing row IDs and row hashes must remain present and unchanged, while new rows may be appended and folded into the baseline.
- Terminal source runs (`RECOVERY_REQUIRED`, `COMPLETED`, `FAILED`, `SUSPENDED`) are fully immutable: additions, deletions, or mutations fail audit.
- Preflight clock skew is measured against Binance Futures read-only server time and fails closed when unavailable or above the configured threshold.
- Recovery drill requires a worker PID, live worker, RUNNING active run, and confirmed process termination before resume; failure records an incident and does not create a continuation or worker.

## Lifecycle changes
- Recovery drill now prevents continuation creation unless the prior RUNNING worker is confirmed stopped.
- Old continuations transitioned to `RECOVERY_REQUIRED` receive terminal source baselines so post-recovery mutation is auditable.

## Persistence changes
- `burnin_source_evidence_hashes` now stores `run_status` and `baseline_reason` alongside row-hash snapshots and evidence hashes.
- Audit reports explicit `AGGREGATE_EVIDENCE_HASH_MISSING` and `AGGREGATE_EVIDENCE_HASH_MISMATCH` violations.

## Export/schema changes
- Schema changes are additive via `ALTER TABLE` fallbacks. Existing baseline rows are compatible but may lack run status until refreshed by audit.

## Tests added
- Healthy RUNNING evidence growth does not trigger mutation.
- Existing RUNNING rows cannot change or disappear.
- Terminal source runs cannot change.
- Clock skew PASS/FAIL/UNAVAILABLE paths.
- Failed worker termination creates no continuation and launches no worker.
- NULL and mismatched aggregate hashes fail audit.

## Tests executed
- `python -m py_compile src/alphaforge/burnin_ops.py`
- `pytest -q tests/test_phase9_burnin_ops.py`

## Risks and remaining limitations
- Real preflight now depends on provider time availability and should fail closed during provider outages.
- Existing campaigns should run one audit to seed source baselines before relying on mutation detection.
- LIVE remains unavailable and cannot be approved by Phase 9 decisions.

## Migration concerns
- Additive columns on `burnin_source_evidence_hashes` are backward compatible. First audit after deployment refreshes baseline metadata.

## Push recommendation
Push after full suite confirms no regressions. Do not merge if provider-time fail-closed behavior or terminal immutability regresses.
