# JOB19 — PAPER Runtime Reject-Rate Integrity and Decision-Quality Audit

## Objective

Measure whether completed PAPER runtime rejection behavior is trustworthy and execution-aware before any calibration discussion. A high rejection rate is not a defect by itself; incomplete audit rows, missing execution context, inconsistent lifecycle evidence, or degenerate score/RR data are defects or blockers.

## Scope

This job adds a read-only SQLite diagnostic query pack. It does not change trading thresholds, score or RR calculations, scanner selection, execution submission, lifecycle writes, schema, or LIVE readiness behavior.

## Evidence boundary

The repository contains the schema and audit contract, but the completed PAPER runtime database is an external runtime artifact. This job does not claim an empirical verdict until `sql/paper_runtime_decision_audit.sql` is run against the actual latest PAPER database/session.

Current schema supports this audit because `order_decisions` includes canonical/audit-layer fields such as `phase`, `mode`, `decision`, `reject_reason`, `score`, `rr`, `effective_rr`, execution context and direct execution metric columns after SQLite bootstrap migration. The query pack counts canonical decisions through `LOWER(COALESCE(NULLIF(TRIM(phase), ''), 'final')) = 'final'` so AI/internal layers do not silently inflate the final reject rate.

## Run procedure

1. Locate the runtime SQLite URL from the environment or runtime bootstrap configuration used for the completed PAPER run.
2. Ensure the runtime application has already called `init_db()` on that database so additive compatibility migrations have run.
3. Open the selected database in SQLTools.
4. Open and execute `sql/paper_runtime_decision_audit.sql`.
5. For a specific PAPER session, edit `job19_parameters` at the beginning of the SQL file with the inclusive UTC `start_ts` and `end_ts` for that run, then rerun.
6. Export or paste the result blocks into the Job19 review before considering Job20.

The SQL file is plain SQL and is intended to run in SQLTools. In the standalone `sqlite3` command-line shell, `.headers on` and `.mode column` may optionally be run before loading the file.

## Diagnostic sections

| Section | Question answered |
|---|---|
| `00_SCOPE` | Which PAPER time window and row population is being analyzed? |
| `01_PHASE_DECOMPOSITION` | Are internal audit layers present alongside canonical final decisions? |
| `02_CANONICAL_DECISION_TOTALS` | What is the non-inflated final reject rate? |
| `03_FINAL_DECISIONS_BY_SYMBOL` | Are decisions concentrated by symbol? |
| `04_REJECT_REASON_QUALITY` | Are reject reasons specific, empty, or `UNKNOWN`? |
| `05_REJECTED_FIELD_COMPLETENESS` | Are rejected canonical rows auditable? |
| `06_DUPLICATE_DECISION_ID` | Are exact decision identities duplicated? |
| `07_CONFLICTING_FINAL_SIGNAL_DECISIONS` | Does a signal have duplicated/conflicting final decisions? |
| `08_SCORE_RR_EFFECTIVE_RR_VARIABILITY` | Are score/RR/effective RR suspiciously constant or absent? |
| `09_DIRECT_EXECUTION_CONTEXT_AVAILABILITY` | Are first-class execution-cost fields populated? |
| `10_JSON_EXECUTION_CONTEXT_AVAILABILITY` | Are context-only market fields present in valid JSON context? |
| `11_LIFECYCLE_STATE_DISTRIBUTION` | Which PAPER lifecycle states were recorded? |
| `12_REJECTED_WITHOUT_REJECTION_EVENT` | Were final rejects left without matching lifecycle rejection evidence? |
| `13_ORDER_PLACED_WITHOUT_ACCEPTED_FINAL_DECISION` | Was an order placed without an accepted final decision? |
| `14_TERMINAL_STATE_WITHOUT_ORDER_PLACED` | Is there a terminal lifecycle state without prior placement evidence? |
| `15_VERDICT_INPUT_COUNTS` | What minimum counts feed the verdict? |

## Verdict classification

Use the outputs to classify the session, with more than one classification allowed:

| Classification | Evidence pattern |
|---|---|
| `HEALTHY_SELECTIVITY` | Canonical reject reasons and lifecycle evidence are complete; execution context/effective RR are trustworthy; sparse acceptance is not itself a failure. |
| `DATA_INTEGRITY_FAILURE` | Empty/unknown reject reasons, missing identities, duplicate/conflicting final rows, or lifecycle integrity failures. |
| `EXECUTION_CONTEXT_FAILURE` | Missing/placeholder-like execution cost fields or absent context makes effective profitability unverifiable. |
| `SCORING_OR_REGIME_PIPELINE_FAILURE` | Score or required adaptive data is degenerate/absent where pipeline behavior requires variability. Constant RR is an observation until its contract is confirmed. |
| `INSUFFICIENT_SAMPLE` | Too few canonical final decisions for expectancy interpretation. |

## Safety constraints retained

- Do not loosen rejection thresholds based only on a high reject rate.
- Do not treat zero spread, zero slippage, or zero latency as valid measurements without source confirmation.
- Do not combine AI/internal audit rows with canonical final decisions when calculating reject rate.
- Do not use this audit as LIVE-readiness evidence.

## Follow-up gate

Job20 should be defined only after running this SQL against the latest PAPER database. A persistence repair is justified only by proven missing/conflicting audit rows. A calibration study is justified only after execution context and effective RR are trustworthy.
