# Issue #549 — PULLBACK vs REENTRY_READY OOS research

Status: **INCONCLUSIVE — retain current production policy**

Base inspected: `dev@0d6e18b91ac458df63fd24b452af6530ea484a4c`.

The production guided-entry policy remains unchanged:

- `CONTINUATION`
- `PULLBACK`
- `REENTRY_READY`

The research-only stricter variant is:

- `CONTINUATION`
- `REENTRY_READY`
- `PULLBACK` => wait/evidence-only

## Why there is no production change

The repository does not currently contain an admissible frozen #506/#546
historical artifact that simultaneously provides:

- exact git/config/data/universe identity;
- canonical post-cost `net_r`, `mfe_r`, and `mae_r`;
- explicit setup phase for each evaluated row;
- OOS validation rows;
- a fresh, single-variant untouched-test segment with canonical
  `SearchSelectionLineage`.

The PAPER cluster that motivated #549 is diagnostic evidence, not an untouched
historical holdout, so it cannot authorize an entry-policy change.

`alphaforge.pullback_policy_research` now provides a research-only evaluator.
It imports the production `REGIME_GUIDED_SETUP_PHASES` baseline and the
canonical #506 `SearchSelectionLineage` admissibility rule rather than copying
either authority. It compares the incumbent and stricter variants on OOS rows,
then evaluates only the selected strict variant on the untouched holdout. Missing,
reused, hash-mismatched, duplicate, or economically incomplete evidence produces
`INCONCLUSIVE`.

A future `PASS_STRICT` result is research evidence only. It does not alter
PAPER/LIVE/LIVE_PRECHECK behavior, relax any threshold, replace fresh PAPER
qualification, or authorize LIVE. A separate production-semantic PR with
BACKTEST/PAPER/LIVE_PRECHECK parity regressions is required before any behavior
change.
