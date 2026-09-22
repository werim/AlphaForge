# AlphaForge System Audit Fabric

## Scope

System Audit Fabric is a downstream, isolated audit system for AlphaForge. The current implementation covers **Levels 0–2** only:

- **Level 0 — Record:** copy canonical decision/outcome evidence into an immutable audit store.
- **Level 1 — Diagnose:** classify observed ACCEPT/REJECT quality, execution degradation, risk, regime/setup cohorts, expectancy and runtime reliability.
- **Level 2 — Recommend:** produce evidence-gated calibration statistics, offline/shadow policy candidates and human/JSON reports.

The audit system is intentionally not part of the trading loop. AlphaForge runtime and order modules do not import it, and a failed audit cannot block, approve, reject or submit a trade.

## Isolation and authority

The source runtime/campaign SQLite database is opened **read-only** by the standalone CLI. Audit output must use a **separate audit database**. Reusing the same database is rejected.

Level 0 stores immutable, versioned decision envelopes and outcome snapshots. Reject shadow authority is explicit:

- `EXECUTABLE_SHADOW`: canonical geometry exists, reject evidence is attributable, and geometry is execution-aligned.
- `DIAGNOSTIC_SHADOW`: useful for diagnosis but not authoritative for calibration.
- `NON_SIMULATABLE`: canonical executable geometry is unavailable; no trade geometry is invented.

Legacy/non-attributable shadow evidence is never promoted to executable authority.

All Level 1 and Level 2 derived tables are append-only. Missing evidence is reported as UNKNOWN/NO_EVIDENCE or an explicit limitation rather than fabricated as zero or healthy.

## Decision-quality semantics

Observed post-cost outcomes are classified as:

- `GOOD_ACCEPT`
- `FALSE_ACCEPT`
- `GOOD_REJECT`
- `FALSE_REJECT`
- `UNRESOLVED`

The system also reports Decision Policy Net Value and comparable cohorts such as symbol, regime, setup type, side and reject reason. Primary/secondary reject attribution is deterministic; for example, an attributable profitable reject whose primary gate was `REGIME_MISMATCH` is recorded as a false reject with `REGIME_MISMATCH` carrying the primary attribution weight.

One trade is not enough to make a systemic policy claim. Cohort-level findings require evidence and retain confidence bounds where available.

## Recommendation gates

Level 2 uses fixed evidence gates:

| Comparable outcomes | State |
| ---: | --- |
| < 30 | `NO_CHANGE` |
| 30–99 | `OBSERVE_ONLY` |
| 100–249 | `SMALL_RECOMMENDATION_ALLOWED` |
| 250+ | `NORMAL_RECOMMENDATION_ALLOWED` |

A policy candidate is not a production configuration change. Current Level 2 rows enforce:

`production_mutation_allowed = 0`

Candidates require offline replay and shadow-policy qualification before any future promotion process could be considered. Protected safety/integrity controls—such as execution-cost, liquidity and portfolio-risk boundaries—cannot receive relaxation candidates merely because rejected shadow outcomes were profitable.

Numeric threshold changes are not guessed by the auditor. A candidate identifies the control and direction for review; numeric proposal remains deferred to offline replay/evidence.

## Report dimensions

The report keeps separate dimensions instead of collapsing the system into one opaque score:

- data integrity
- regime calibration
- signal quality
- decision quality
- reject quality
- execution quality
- risk quality
- expectancy
- reliability
- system expectancy confidence

When a dimension cannot be supported by current persisted evidence, the report says so. In particular, current Levels 0–2 do not invent missed-candidate counterfactuals, realized-regime confusion labels, or trailing/partial-exit counterfactuals.

## CLI

Run against an existing runtime/campaign database and a different audit database:

```bash
PYTHONPATH=src python -m alphaforge.system_audit_reporting \
  --source-db data/campaign/CAMPAIGN.db \
  --audit-db data/audit/CAMPAIGN.audit.db \
  --burnin-run-id RUN_ID \
  --json-out artifacts/audit/system_audit.json \
  --text-out artifacts/audit/system_audit.txt
```

Campaign-scoped ingestion is also supported:

```bash
PYTHONPATH=src python -m alphaforge.system_audit_reporting \
  --source-db data/campaign/CAMPAIGN.db \
  --audit-db data/audit/CAMPAIGN.audit.db \
  --campaign-id CAMPAIGN_ID
```

The source DB is opened with SQLite `mode=ro`. Operational evidence ingestion is optional; when unavailable, the report records that limitation instead of treating reliability as proven healthy.

## CI contract

The repository test workflow contains a dedicated **System audit fabric gate** that runs the L0, L1, L2 and umbrella isolation-contract tests before the full suite. Full repository tests, offline backtest and backtest-output verification remain required before merge.

## Explicit non-goals

**Level 3** shadow-policy experimentation and **Level 4** automatic calibration/promotion are not active in the current implementation.

There is no direct audit-to-runtime config mutation path, no audit-driven order submission path, and no automatic LIVE authorization path. Those boundaries are deliberate.
