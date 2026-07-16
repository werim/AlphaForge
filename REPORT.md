# AlphaForge Phase 9 PR 280 Surgery Report

## Why the patch was needed
The initial Phase 9 operations CLI contained structural placeholders: launch could succeed from the campaign-start heartbeat, critical preflight checks were hard-coded PASS, recovery/audit checks used non-evidence booleans, watchdog coverage was narrow, and final release could treat non-canonical qualification aliases as canary review.

## Root cause
Phase 9 wrapped Phase 8 primitives but did not yet verify the operational evidence produced by the actual runtime worker, resolver, dashboard SQL query, immutable source rows, or canonical qualification snapshot.

## Files changed
- `src/alphaforge/burnin_ops.py`: hardens preflight, launch, attachment verification, health/watchdog, recovery drill, integrity audit, daily report, and finalize logic.
- `src/alphaforge/dashboard/queries.py`: fixes campaign count helper to query using an open connection and continues exposing read-only Phase 9 evidence.
- `tests/test_phase9_burnin_ops.py`: expands regression coverage for foreground/detached launch, fail-closed preflight, recovery preservation, source immutability, watchdog detections, audit violations, canonical canary decisions, checksums, and LIVE safety.
- `REPORT.md`, `CHANGELOG.md`, `VERSION.md`: document stricter Phase 9 behavior and remaining risk.

## Runtime behavior changes
- Detached launch now requires the worker process to remain alive and a `PHASE8_CAMPAIGN_ATTACHED` event after launch, runtime instance evidence, heartbeat at/after worker start, and active run parity before returning success.
- Foreground launch calls `BurnInCampaignRunner.run_foreground()` instead of reporting success from campaign creation.
- Preflight compares actual PAPER runtime identity against candidate campaign identity and blocks critical UNKNOWN/UNAVAILABLE checks.
- Watchdog never resumes automatically; it persists incidents and moves unhealthy campaigns to `RECOVERY_REQUIRED`.

## Lifecycle changes
- Recovery drill verifies the old active continuation moves to `RECOVERY_REQUIRED`, exactly one new continuation is created, campaign start time is unchanged, and restart count increments once.

## Persistence changes
- Existing additive Phase 9 tables are retained.
- Added `burnin_source_evidence_hashes` to capture source run row IDs and hashes for immutability verification.

## Export/schema changes
- Integrity audit now persists non-placeholder checks for post-decision candles, same-candle ambiguity, provider-failure expiry, aggregate hash linkage, dashboard parity, and immutable source hashes.
- Final package checksums are reproducible and release decisions require canonical `CANARY_QUALIFIED`.

## Tests added
- Foreground launch invokes the runner.
- Detached launch waits for attach and fails without attach.
- Preflight cannot pass an unverified critical runtime identity check.
- Recovery starts a new worker and preserves exact pending IDs.
- Source evidence immutability hash changes are detected.
- Watchdog detects backlog growth and repeated provider failures.
- Audit detects pre-decision candle use, dashboard/SQL mismatch, and stored aggregate hash mismatch.
- `CANARY_QUALIFIED` is required; `PASS` aliases do not qualify for canary review.
- Final checksums reproduce and LIVE decisions remain impossible.

## Tests executed
- `python -m py_compile src/alphaforge/burnin_ops.py`
- `pytest -q tests/test_phase9_burnin_ops.py tests/test_phase8_burnin_campaign.py tests/test_phase8_reject_resolver.py tests/test_phase8_position_resolver.py tests/test_runtime.py tests/test_dashboard_app.py`
- `pytest -q`

## Risks and remaining limitations
- Real multi-day PAPER evidence must still be collected before canary review.
- Preflight and launch are now intentionally stricter and can fail closed on provider, runtime identity, or worker-attachment evidence gaps.
- Source immutability is enforced after the first audit captures baseline hashes; older campaigns need an initial audit baseline.
- No LIVE readiness or production approval is introduced.

## Migration concerns
- Schema changes are additive. Existing Phase 8/9 campaign rows remain compatible, but first audit of an existing campaign will establish immutable source hash baselines.

## Push recommendation
Push after CI repeats the full suite; do not merge if operational tests regress or if any LIVE activation path appears.
