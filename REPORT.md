# AlphaForge Phase 9 Surgery Report

## Why the patch was needed
Phase 8 created campaign persistence and continuation aggregation, but operators still lacked a single production-like PAPER burn-in workflow with fail-closed preflight, operational health checks, recovery drill evidence, integrity audits, SQL-derived reporting, and a machine-readable release decision package.

## Root cause
Burn-in validation was available as lower-level primitives and tests, not as an operational launch/audit/finalize control surface suitable for a multi-day PAPER campaign.

## Files changed
- `src/alphaforge/burnin_ops.py`: new Phase 9 CLI and operational evidence layer.
- `src/alphaforge/dashboard/queries.py`: read-only campaign operations fields for dashboard/API visibility.
- `tests/test_phase9_burnin_ops.py`: Phase 9 regression coverage.
- `RUNBOOK.md`, `REPORT.md`, `CHANGELOG.md`, `VERSION.md`: operational documentation updates.

## Runtime behavior changes
- Adds `python -m alphaforge.burnin_ops` commands for preflight, launch, status/health, watchdog, recovery drill, audit, report, pause/resume, and finalize.
- Launch remains PAPER-only and validates commit, branch/cleanliness, database, schema, market data, identity, kill-switch/LIVE disablement, duplicate campaign state, recovery state, and disk space before creating a campaign.

## Lifecycle changes
- Phase 9 does not collapse lifecycle states or synthesize trades. It observes existing Phase 8 source runs, pending reject labels, pending PAPER positions, closures, and qualification snapshots.

## Persistence changes
- Adds additive SQLite tables for preflight reports, health history, incidents, recovery drills, integrity audits, and release decisions.
- Existing Phase 8 tables and aggregate behavior are preserved.

## Export/schema changes
- Preflight and integrity audit emit JSON/CSV.
- Daily report emits JSON/CSV/Markdown.
- Finalize emits `release_decision.json`, `final_manifest.json`, checksums, and the Phase 8 evidence bundle.

## Tests added
- Preflight rejects non-PAPER mode.
- Health reports SQL-derived counters and detects RUNNING without a live worker.
- Watchdog transitions unhealthy campaign evidence to recovery-required.
- Audit detects incomplete/missing-cost outcomes.
- Final decision never emits LIVE readiness decisions.

## Tests executed
- `python -m py_compile src/alphaforge/burnin_ops.py src/alphaforge/burnin_campaign.py src/alphaforge/burnin_resolver.py src/alphaforge/runtime.py`
- `pytest -q tests/test_phase9_burnin_ops.py`

## Risks and remaining limitations
- Real multi-day validation still requires an operator-run PAPER campaign with reachable Binance read-only market data.
- Preflight network validation can fail closed during provider outage.
- Watchdog intentionally does not auto-restart workers; operator resume is required.
- Phase 9 infrastructure qualification is not a profitability claim and cannot approve LIVE trading.

## Migration concerns
- Additive tables are created with `CREATE TABLE IF NOT EXISTS`; existing campaign evidence remains compatible.

## Push recommendation
Push to `dev` after full repository tests pass in the target CI/runtime environment.
