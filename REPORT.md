# AlphaForge Technical Surgery Report

## 2026-06-30 - BACKTEST SHORT Breakdown Rescue Reporting Experiment

## Why the patch was needed
The latest DEFAULT all-filters-on BACKTEST produced only 11 accepted trades from 1,436 candidates, while `candidate_quality_gates` showed a concentrated SHORT + BREAKDOWN_DOWN + BREAKOUT/NORMAL-stop cluster with positive rejected-shadow expectancy. A global filter loosen would be unsafe, so the patch adds a reporting-first, opt-in rescue path for that narrow hypothesis only.

## Root cause
The dashboard/export layer already exposed the SHORT breakdown quality gate as reporting-only evidence, but there was no controlled way to test reduced-size activation without disabling broad reject filters or changing baseline behavior.

## Files changed
- `.env.example`
- `backtest_order.py`
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_backtest_order_scanner.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

## Runtime behavior changes
DEFAULT BACKTEST behavior remains unchanged because `ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED=false`. When explicitly enabled, BACKTEST may rescue only SHORT `BREAKDOWN_DOWN` candidates in BREAKOUT/NORMAL-compatible conditions whose first reject reason/gate is allowed and whose execution context passes conservative checks.

## Lifecycle changes
Rescued trades use the normal simulation lifecycle and are marked with `accepted_reason=SHORT_BREAKDOWN_RESCUE`, `original_reject_reason`, `rescue_size_multiplier`, `rescue_effective_rr`, and JSON `rescue_decision_context`.

## Persistence changes
No SQLite migration is required. Existing lifecycle/export metadata fields carry rescue evidence. Summary exports include rescue diagnostics and baseline-vs-rescue PnL separation.

## Export/schema changes
`.env.example` adds the BACKTEST-only rescue variables. `backtest_filter_state` now includes a `backtest_only_experiments` section identifying the rescue switch as BACKTEST-only. The dashboard summary separates BASELINE accepted trades, RESCUE accepted trades, and reporting-only gates.

## Tests added
Regression tests prove disabled baseline rejection, enabled rescue rows, SHORT-only eligibility, LOW_SCORE LONG exclusion, metadata population, filter-state BACKTEST-only labeling, and PAPER/LIVE non-activation.

## Tests executed
- `pytest -q tests/test_backtest_order_scanner.py -k 'short_breakdown_rescue or rescue_enabled_only_backtest or rescue_disabled'`

## Risks
The rescue gate is still experimental and depends on rejected-shadow/backtest diagnostics. Spread/slippage may be estimated. Enabling the rescue can change accepted count and PnL in BACKTEST only.

## Remaining limitations
`ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_MIN_SHADOW_EXPECTANCY` is exported/configured for operator audit but live per-candidate activation currently gates on candidate execution context rather than recomputing a per-run shadow aggregate inside the decision loop.

## Migration concerns
No database migration required. Artifact consumers should tolerate additive JSON/CSV fields.

## Push recommendation
Safe to push as a BACKTEST-only reporting-first experiment. Do not enable for LIVE; LIVE remains not ready.
