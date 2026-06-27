
# Dashboard Settings Hardening Report - 2026-06-27

## Why patch was needed
The first Settings implementation exposed correct groups but left ambiguity around BACKTEST-only active state, canonical env naming, LIVE enable safety, boolean editing, environment locks, and percent-unit interpretation.

## Root cause
Settings rows were keyed to effective runtime mode only, legacy aliases were displayed as primary names, bool parsing accepted unknown strings as false, and dashboard saves did not explicitly respect environment locks/readiness.

## Files changed
- `src/alphaforge/config_registry.py`
- `src/alphaforge/config/__init__.py`
- `src/alphaforge/dashboard/app.py`
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/settings.html`
- `.env.example`
- `tests/test_config_registry.py`
- `tests/test_dashboard_settings.py`
- `tests/test_backtest_config_isolation.py`

## Runtime behavior changes
- Canonical effective-RR and liquidity names are now `ALPHAFORGE_*`; old aliases remain parse-only compatibility inputs.
- Survival defaults are stricter: effective RR 1.60 and raw RR 1.70.
- Environment-sourced settings are read-only in Settings saves.

## Lifecycle changes
No lifecycle state machine changes. Reject quality is hardened by stricter default RR thresholds.

## Persistence changes
Dashboard override persistence remains JSON-based. Reset removes overrides. No SQLite schema change.

## Export/schema changes
Dashboard BACKTEST exports now include config snapshot metadata for active filters, disabled filters, BACKTEST caps, source rows, and a note that PAPER/LIVE runtime caps are ignored by BACKTEST by default.

## Tests added
Added/updated tests for canonical alias precedence, bool validation/UI, environment lock behavior, BACKTEST settings consumption, and conservative threshold defaults.

## Tests executed
Targeted tests passed locally; full suite execution is recorded in final response.

## Risks and remaining limitations
- Some optional dashboard tests skip when FastAPI/httpx are unavailable.
- LIVE remains not ready unless persisted readiness evidence is PASS.

## Migration concerns
Operators should move from `MIN_EFFECTIVE_RR` and `MIN_LIQUIDITY_USD` to canonical `ALPHAFORGE_*` names; aliases remain backward compatible.

## Push recommendation
Merge only after full `python -m pytest -q` remains green in CI.
