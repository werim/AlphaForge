# AlphaForge Verification Protocol

This protocol is permanent and fail-closed. Missing or stale evidence is never converted into `PASS`.

## Development verification routing

Start with [AGENTS.md](../AGENTS.md#persistent-context-and-session-handoff) and the
active PR/issue handoff. Use the existing [repository map](../README.md#repository-navigation)
to locate authorities and tests, without rereading unrelated subsystems.

The deterministic [System Verification Router](../src/alphaforge/system_verification.py)
owns path risk, invariant/test selection, required T0–T4 tiers and advisory model
routing. Do not reproduce these rules in a separate workflow. Generate its plan
with resolved full commit SHAs for the task base and current clean HEAD:

```bash
PYTHONPATH=src .venv/bin/python -m alphaforge.system_verification plan \
  --base <full-base-sha> --head <full-head-sha> \
  --output /private/tmp/alphaforge-verification-plan.json
```

Review `changed_files`, `affected_invariants`, `required_tiers` and `selected_tests`.
The Git diff planner covers committed changes; review working-tree/untracked
changes separately before committing, then regenerate the final-head plan.
Run focused regressions first, adjacent tests when justified, and broader
qualification when required. Stop at an authoritative failure and inspect its
smallest relevant evidence slice. Reuse exact-SHA/config/profile results only
when valid; do not rerun a full suite merely to rebuild session context.

Use the router's `route-failure <input-json>` command for bounded failure packets
and advisory reasoning escalation. Test count and long deterministic waits do not
justify stronger models. Escalate analysis only for unresolved semantic complexity
or cross-authority uncertainty; never change the baseline model automatically.
LLM advice cannot override deterministic gates or authorize LIVE.

The existing [Tests workflow](../.github/workflows/test.yml) consumes the plan on
PRs, retains mandatory safety guards, runs full regression on non-PR events and
deduplicates successful exact-SHA manual dispatches. Do not dispatch/poll/rerun CI
to measure efficiency. Record local evidence and pending CI separately in the
single handoff; observe operator CI, merge and protected SOAK restrictions.
T0–T4 development routing does not waive any applicable G0–G10 release gate below.

## Release evidence gates

| Gate | Required evidence | Status vocabulary |
|---|---|---|
| G0 Compile / diff | compile, `git diff --check` | PASS / FAIL |
| G1 Focused tests | readiness and changed-module tests | PASS / FAIL |
| G2 Adjacent regressions | lifecycle, persistence, expectancy, and backtest tests | PASS / FAIL |
| G3 GitHub CI | required CI checks for the commit | PASS / INCOMPLETE |
| G4 Offline BACKTEST | deterministic, no-network canonical path | PASS / FAIL |
| G5 Historical BACKTEST | fixed BTCUSDT/ETHUSDT, 1h, 30-day comparison | PASS / INCOMPLETE |
| G6 PAPER smoke | new campaign, 30–60 minutes, fresh release identity | PASS / INCOMPLETE |
| G7 PAPER evidence | separate evidence campaign with complete persistence | PASS / INCOMPLETE |
| G8 7-day burn-in | duration, lifecycle, cost, reject, and reconciliation evidence | PASS / INCOMPLETE |
| G9 Fault/recovery | measured drills and fail-closed recovery | PASS / FAIL |
| G10 Final readiness | all mandatory gates explicitly pass | PASS / INCOMPLETE |

The protocol also records `NOT_APPLICABLE` and `NOT_OBSERVABLE`; neither is a passing state. A paused historical campaign may support diagnostics, but cannot be reused for G6–G8.

## Combined #366/#367/#368 verification

The evidence chain is: `candidate → authoritative AIBrain scoring → non-authoritative state-direction shadow → ACCEPT/REJECT → lifecycle persistence → outcome resolution → timestamp-bounded expectancy`.

- #366: state-direction shadow is isolated, non-authoritative, and has no order mutation path; MTF/shadow evidence is separately identified.
- #367: BACKTEST traverses the real decision path, uses authoritative scoring, persists concrete reject reasons and lifecycle rows, and runs offline without network.
- #368: expectancy readers require `decision_time <= T`, non-null `resolved_at`, and `resolved_at <= T`; unresolved or future outcomes are excluded and cannot change prior as-of results.

## BACKTEST acceptance criteria

The offline smoke must prove no network access, deterministic repeatability, lifecycle export, rejected rows with reasons, authoritative scoring, and explicit `UNAVAILABLE_*` fields rather than fabricated zero execution values. The historical run records candidates, accepted/rejected counts, reason and score/RR distributions, gross/net R, cost drag, effective RR, lifecycle completeness, and expectancy evidence completeness. Trade-count increases are not a success criterion.

## PAPER campaign rule

G6 and G7 always use a new database and release identity. Never resume, mutate, resolve, migrate, backfill, or reuse a paused campaign evidence database.
