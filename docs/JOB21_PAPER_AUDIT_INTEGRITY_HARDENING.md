# JOB-21 PAPER Audit Integrity Hardening

## Objective

Harden PAPER runtime SQL evidence and repair the narrow lifecycle transition defect revealed by the latest exported audit reports. This increment is defensive and observation-oriented: it does not change thresholds, scoring, effective-RR formulas, order submission behavior, LIVE activation, or trade frequency targets.

## Evidence reviewed

The exported PAPER audit snapshot showed:

- 3,210 final decisions and 3,210 final rejections.
- 5,869 PAPER lifecycle rows, of which 5,807 were `ERROR`.
- `raw_rr` and `effective_rr` persisted as constant `2.0` where present.
- JOB-05/JOB-09 audit statements failed because they queried execution-measurement columns on `trade_lifecycle_events` that are actually persisted on `order_decisions`.
- JOB-19 top-level audit file was rejected by the CSV runner because it contained sqlite shell dot commands.
- JOB-06 emitted `HEALTHY_SELECTIVITY` despite corrupted lifecycle evidence and incomplete execution-evidence verification.

## Root causes established from code

### Lifecycle ERROR cascade

`RuntimeOrchestrator` retains the last lifecycle state by symbol. For a repeatedly rejected symbol, the first cycle reaches `SIGNAL_REJECTED`; the next fresh signal begins with `SIGNAL_CREATED`, but the lifecycle transition contract previously prohibited `SIGNAL_REJECTED -> SIGNAL_CREATED`. The emitter converts invalid transitions to `ERROR`, and subsequent cycles remain contaminated.

The narrowly scoped fix permits a fresh `SIGNAL_CREATED` observation after terminal `SIGNAL_REJECTED` or recorded `ERROR`. This makes independent signal instances observable again without permitting any trade execution or bypassing any rejection gate.

### SQL schema mismatch

Execution-cost measurements such as `spread_pct`, `expected_slippage_pct`, `latency_ms`, and `funding_rate_pct` are persisted on `order_decisions`. The old JOB-05 and JOB-09 queries attempted to read some of these fields from `trade_lifecycle_events`, producing schema errors instead of evidence.

### Optimistic classification

The previous JOB-06 classification helper checked identity, reject reasons, a context-missing marker, and score variation, but did not fail on dominant lifecycle `ERROR` evidence, rejected decisions lacking reject lifecycle evidence, absence of accepted samples, or constant effective RR evidence.

## Changes

- `src/alphaforge/contracts.py`
  - Permits terminal rejected/error observation chains to begin a new `SIGNAL_CREATED` instance for symbol-keyed runtime tracking.
- `sql/diagnostics/job05_execution_context_population.sql`
  - Measures execution-cost coverage using `order_decisions`; queries lifecycle only for state-marker evidence.
- `sql/diagnostics/job09_exchange_safety_gates.sql`
  - Measures spread/funding/latency gates on `order_decisions`; adds lifecycle ERROR concentration evidence.
- `sql/diagnostics/job06_paper_runtime_db_audit.sql`
  - Adds lifecycle corruption and reject reconciliation checks; emits fail-closed classifications before any healthy candidate classification.
- `sql/diagnostics/job19_paper_reject_rate_decision_quality_audit.sql`
  - Replaces sqlite shell commands and multi-purpose top-level content with one CSV-runner-safe canonical summary query.
- `tests/test_job21_audit_integrity.py`
  - Adds regression checks for lifecycle restart and SQL/read-only classification contracts.

## Intentionally not changed

- No acceptance threshold or scoring calculation changes.
- No raw/effective RR formula changes.
- No order submit/cancel/modify path changes.
- No LIVE activation or readiness relaxation.
- No rewriting of historic corrupt lifecycle rows.

## Remaining blocker

The PAPER accepted path currently emits `ORDER_PLACED` before execution and `_execute()` emits `ORDER_PLACED` again. Because the uploaded sample contains no accepted final decisions, this defect is not the cause of the observed all-reject ERROR cascade. It remains a separate lifecycle-accuracy blocker to patch and validate before interpreting any accepted PAPER sample as healthy.

## Post-check requirement on the real runtime DB

After this patch is merged and a fresh PAPER sample is generated, rerun `run_sql_audits.py` against the current runtime SQLite DB. A credible improvement requires:

- JOB-05, JOB-06, JOB-09, and JOB-19 to execute without SQL errors.
- New reject cycles to produce `SIGNAL_CREATED -> SIGNAL_REJECTED`, not `ERROR` chains.
- `HEALTHY_SELECTIVITY_CANDIDATE` to remain impossible until lifecycle, execution evidence, accepted sampling, and effective-RR realism are all verified.
