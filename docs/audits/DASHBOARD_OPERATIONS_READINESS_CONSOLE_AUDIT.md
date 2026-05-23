# Dashboard vs Operations & Readiness Console Audit

**Audit date:** 2026-05-23  
**Base branch:** `dev` at `0efb5cb881008d564982e8413a03927620ee21b4`  
**Implementation posture:** lowest-risk read-only increment only  
**LIVE verdict:** BLOCKED / not established by dashboard visibility

## Audit question

Does the merged read-only AlphaForge dashboard expose enough evidence to act as an Operations & Readiness Console without becoming an execution or privileged control surface?

## Observed merged dashboard surface

Before this increment the dashboard exposed:

- configured safety state and a hard-coded honest runtime status of `UNVERIFIED`;
- rejection summary and incomplete rejected-row visibility;
- lifecycle lists and per-signal timeline;
- the latest persisted LIVE readiness report, when present;
- no order, LIVE activation, kill-switch mutation or configuration write routes.

That surface preserves safety, but the readiness page showed only checks present inside an already-written readiness report. It did not show whether an expected readiness probe was absent, omitted from a report, or simply lacked evidence.

## Gap inventory

| Area | Required console evidence/control | Merged state before patch | Risk classification | Treatment in this increment |
|---|---|---|---|---|
| Runtime liveness | Persisted runtime heartbeat with freshness status | Runtime rendered as `UNVERIFIED`; heartbeat not implemented | Critical missing probe | Render explicit `runtime_heartbeat = MISSING_PROBE` |
| Readiness coverage | Expected probe inventory vs latest persisted report | Only raw checks from existing report | Critical data-surface gap | Add read-only expected-probe matrix API/UI |
| Missing report fields | Detection when an evaluator check disappears from latest report | Not visible | Critical regression visibility gap | Status `MISSING_IN_REPORT` |
| No current evidence | Honest distinction between failed and absent evidence | Readiness absence shown, but not per expected probe | Important data-surface gap | Status `NO_EVIDENCE` per probe |
| Mode parity | PAPER/LIVE parity evidence | Persisted in report payload only | Important drill-down limitation | Categorize in coverage matrix; no new probe execution |
| Reconciliation | provider/evidence/orphan/duplicate/fail-closed evidence | Persisted in report payload only | Critical drill-down limitation | Categorize in coverage matrix; no exchange action |
| Alert / observability | alert delivery and measured observability evidence | Persisted in report payload only | Critical drill-down limitation | Categorize in coverage matrix |
| Rollback / emergency control | rollback proof and kill-switch safety proof | Persisted in report payload only | Critical drill-down limitation | Categorize in coverage matrix |
| Operational actions | order, LIVE activation, kill switch or config mutation | Deliberately absent | Correct safety boundary, not a defect | Retain omission and surface boundary |
| External active probes in UI | exchange/runtime probing from dashboard process | Deliberately absent | Correct safety boundary for this increment | Retain omission; prefer runtime-persisted evidence |

## Lowest-risk implementation selected

Implemented an observation-only readiness probe coverage matrix:

- `READINESS_PROBE_CATALOG` declares the 26 currently emitted `LiveReadinessEvaluator` checks plus the known absent persisted runtime heartbeat probe.
- `fetch_readiness_probe_matrix()` maps the latest persisted report into fail-closed statuses: `PASS`, `FAIL`, `NO_EVIDENCE`, `MISSING_IN_REPORT`, `MISSING_PROBE`.
- `GET /api/v1/readiness/probes` exposes this matrix without creating tables, migrating data or running probes.
- `/readiness` renders expected probe count, critical gaps, coverage status and the intentionally omitted mutation-control boundary.

## Why this is the safest next increment

This change improves detection of missing proof without altering any AlphaForge trading or qualification decision. It does not:

- submit, cancel, modify or close orders;
- start runtime or activate LIVE;
- alter the kill switch or configuration;
- change score, RR, reject thresholds, regime logic or execution-cost logic;
- run external exchange calls;
- write or migrate runtime database evidence.

## Tests added

- Missing readiness report and absent heartbeat remain fail-closed, appear as gaps, and do not create a missing runtime database.
- A partial persisted readiness report surfaces a missing expected evaluator check as `MISSING_IN_REPORT` while preserving visible passing evidence.
- Existing route safety test continues to prohibit execution or LIVE-mutation route fragments.

## Remaining recommended increments, deliberately not implemented

1. Add a runtime-owned persisted heartbeat contract with freshness expiry and fail-closed stale handling.
2. Once persisted contracts are stable, add read-only evidence detail panes for parity, reconciliation, alert delivery and rollback proof history.
3. Continue to omit operational write controls unless authentication, role authorization, two-step acknowledgement and immutable audit logging are designed and tested.

## Verdict

The merged dashboard is a safe read-only observation shell, not yet a complete Operations & Readiness Console. The implemented increment closes the most immediate visibility blind spot, namely expected readiness evidence coverage, while keeping LIVE and execution actions outside the UI security boundary.