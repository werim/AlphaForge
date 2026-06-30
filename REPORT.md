# AlphaForge Technical Surgery Report

## 2026-06-30 - Dashboard BACKTEST SHORT_BREAKDOWN_RESCUE switch

### Why this patch was needed
Operators had to manually set `ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED` outside the dashboard to compare baseline and rescue-enabled BACKTEST runs. That made dashboard evidence incomplete and increased the risk of stale shell state contaminating comparisons.

### Root cause
The rescue experiment existed in the backtest runner but the dashboard request model, form, subprocess environment, filter-state artifacts, and result rendering did not carry or show the selected experiment state.

### Files changed
- `.env.example`
- `backtest_order.py`
- `src/alphaforge/config_registry.py`
- `src/alphaforge/dashboard/__init__.py`
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_dashboard_rescue_switch.py`
- `tests/test_dashboard_app.py`
- `CHANGELOG.md`
- `VERSION.md`
- `REPORT.md`

### Runtime behavior changes
Dashboard BACKTEST requests now include a disabled-by-default `SHORT_BREAKDOWN_RESCUE experiment` checkbox. Launching a dashboard backtest passes a scoped subprocess environment override: `false` for baseline and `true` only when selected. The command also adds `--rescue-enabled` when selected. The supported dashboard runner remains `backtest_order.py`; no `python -m alphaforge.backtest.runner` module exists in this repository.

### Lifecycle changes
No lifecycle transition semantics changed. Rescue remains constrained by existing BACKTEST-only acceptance checks and does not activate in PAPER/LIVE.

### Persistence/export/schema changes
No SQLite schema migration. `backtest_filter_state.json` and `.csv` now include SHORT_BREAKDOWN_RESCUE experiment evidence, including BACKTEST-only scope and default-off status. Summary CSV metadata includes the selected rescue state and dashboard result rendering exposes baseline/rescue accepted counts, PnL, combined PnL, and accepted-reason breakdown.

### Tests added/executed
Added dashboard rescue-switch tests for disabled/enabled scoped env values, default-off behavior, BACKTEST-only settings scope, filter-state artifact evidence, dashboard result rendering, and existing rescue-disabled/rescue-enabled baseline behavior.

### Risks and limitations
Rescue-enabled BACKTEST results are experimental comparison evidence, not production readiness. Candidate quality gate CSV behavior remains reporting-only. Optional FastAPI/httpx absence still skips HTML rendering coverage in minimal environments.

### Migration concerns
None for SQLite. Existing artifact consumers should tolerate the added JSON fields and CSV columns.

### Push recommendation
Safe to push after the targeted tests pass. Do not promote rescue to PAPER/LIVE and do not infer LIVE readiness from rescue-enabled BACKTEST acceptance.

---

## 2026-06-30 - BACKTEST filter-state audit and filters-off damage diagnostics

### Why this patch was needed
The latest dashboard run intentionally unchecked every optional BACKTEST filter and showed high acceptance with severe negative expectancy. The dashboard did not yet make the run profile, hard safety gates, or switch-to-reject mapping auditable in every artifact.

### Root cause
Optional BACKTEST switches were real decision switches, but generated artifacts did not explicitly distinguish optional filters from always-on hard safety gates. A filters-off diagnostic could therefore be misread as strategy performance instead of damage attribution.

### Files changed
- `backtest_order.py`
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_backtest_filter_switches.py`
- `docs/backtest_filter_switch_audit.md`
- `CHANGELOG.md`
- `VERSION.md`
- `REPORT.md`

### Runtime behavior changes
BACKTEST writes filter-state and diagnostic artifacts for each run. Default filter thresholds are unchanged. PAPER/LIVE switch behavior is unchanged.

### Lifecycle changes
No lifecycle transition semantics changed. Rejected rows and accepted diagnostics remain persisted/exported through existing lifecycle artifacts.

### Persistence/export/schema changes
CSV/JSON artifacts are append-only additions: `backtest_filter_state.json`, `backtest_filter_state.csv`, `backtest_filter_profile_comparison.json`, `accepted_trade_loss_diagnostics.json`, and `accepted_trade_loss_diagnostics.csv`. No SQLite schema migration is required.

### Tests added/executed
Added regression tests for all-off filter-state recording, `NEGATIVE_EXPECTANCY` hard safety persistence, artifact-only comparison scaffolding, and accepted loss diagnostics.

### Risks and limitations
The comparison artifact records the current run and marks other profiles as not run; a complete 30/90/180/365 comparison still requires separate profile runs. Score=10 saturation remains diagnostic-only and does not tune thresholds.

### Migration concerns
None for SQLite. Consumers may optionally read the new JSON/CSV artifacts.

### Push recommendation
Safe to push for BACKTEST diagnostic transparency. Do not treat filters-off results as LIVE readiness.

---


## Why this patch was needed
The latest dashboard BACKTEST diagnostics showed very high rejection, but accepted BTC/ETH 90d 15m trades still had negative net PnL and many SL_HIT outcomes. Accepted effective RR averaged only about 1.58, score=10 was not reliably predictive, and disabling or weakening gates could hide low-quality acceptance.

## Root cause
Accepted quality was not hardened enough after execution costs: raw RR could satisfy the `RR_TOO_LOW` branch while effective RR stayed near the old 1.10 floor, and the backtest execution reject helper still had a hardcoded 1.10 LOW_EFFECTIVE_RR threshold. Score saturation is diagnostic evidence, not proof of expectancy. REGIME_MISMATCH near-miss diagnostics indicate the regime gate is protective and should stay enabled by default.

## Files changed
- `.env.example`
- `src/alphaforge/config_registry.py`
- `src/alphaforge/order.py`
- `backtest_order.py`
- `tests/test_backtest_filter_switches.py`
- `tests/test_backtest_order_scanner.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

## Runtime behavior changes
- Raised typed default `MIN_EFFECTIVE_RR` to 1.60 across BACKTEST/PAPER/LIVE unless explicitly overridden.
- `RR_TOO_LOW` now rejects when either raw RR is below `MIN_RR` or execution-adjusted RR is below `MIN_EFFECTIVE_RR`.
- BACKTEST-only disabled-filter experiments still work, but diagnostics now expose disabled-filter acceptance evidence.

## Lifecycle changes
No lifecycle state was removed. The patch preserves SIGNAL_CREATED, SIGNAL_REJECTED, accepted diagnostics, rejected distributions, near-miss diagnostics, execution-cost summaries, and config snapshots.

## Persistence changes
No SQLite schema migration was introduced. New diagnostics are summary/export fields derived from existing lifecycle/order rows.

## Export/schema changes
`backtest_quality_summary.csv` can now include accepted-trade quality diagnostics, score calibration diagnostics, and disabled-filter acceptance evidence as serialized summary values. Existing CSV columns remain append-style summary metrics.

## Tests added
- Effective-RR-aware RR_TOO_LOW gating and BACKTEST-only bypass behavior.
- REGIME_MISMATCH enabled by default.
- Accepted quality diagnostics by score/effective-RR/symbol and score=10 saturation evidence.
- Disabled-filter acceptance evidence in quality summaries.

## Tests executed
- `python -m pytest -q`

## Risks
- The stricter default can reduce accepted trades materially. This is intentional and aligned with capital preservation.
- Score calibration remains weak; this patch exposes diagnostics rather than overfitting new score filters to one BTC/ETH run.
- Existing local/dashboard overrides can still set lower thresholds; config snapshots must be reviewed for override evidence.

## Remaining limitations
- No new curve-fit filters were added for side, symbol, hour, or specific score buckets.
- Full dashboard smoke results depend on available Binance/network data and runtime fixture duration.

## Migration concerns
Operators relying on previous default `MIN_EFFECTIVE_RR=1.10` must explicitly override it if they want legacy behavior. Such loosening should be treated as an experiment and documented in config snapshots.

## Push recommendation
Push after the full test suite passes and a dashboard BACKTEST smoke run confirms accepted count, win/loss/open, net PnL, disabled filters, config snapshot path, accepted loss clusters, and score calibration summary are visible.
