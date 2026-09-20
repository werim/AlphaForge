# AlphaForge Verification Protocol

This protocol is permanent and fail-closed. Missing or stale evidence is never converted into `PASS`.

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
