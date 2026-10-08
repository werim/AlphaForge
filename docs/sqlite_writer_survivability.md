# SQLite writer-family survivability matrix

Issue: #571

## Invariant

Transient SQLite `BUSY/LOCKED` contention must not become an unexplained runtime/scanner death. Authoritative mutation/evidence must either commit exactly once or leave the runtime explicitly fail-closed/recovery-required. Non-transient schema, constraint, programming, or integrity errors remain fatal.

The canonical runtime SQLite contract remains:

- `journal_mode=WAL`
- `busy_timeout=30000`
- `synchronous=NORMAL`
- `foreign_keys=ON`

Runtime-critical bounded retries use `alphaforge.sqlite_safety.run_sqlite_write_with_retry()`. A failed BUSY transaction is rolled back, its connection is invalidated/closed, and the next attempt uses a fresh connection.

## Writer matrix

| Writer family | Primary call site | Authority | BUSY policy | Exhausted behavior | Scanner/runtime | Restart/idempotence evidence |
|---|---|---|---|---|---|---|
| Storage retention / archive removal | `storage_policy.StorageController` | Bounded maintenance with immutable scoped archival | Canonical writer guard + fresh BUSY retries | Explicit pressure blocker; new scans stop; exposure management stays | recovery-required / supervised | #620 archive/manifest/removal crash tests; cycle/hash capabilities |
| Runtime heartbeat | `runtime_heartbeat.py`, `runtime._heartbeat_loop` | Operational safety evidence | Existing bounded contention handling | degraded; sustained failure -> recovery-required terminalization | controlled | heartbeat identity/state snapshots |
| Reconciliation evidence | `runtime_state.persist_reconciliation_cycle` | Safety authority | Existing bounded fresh transaction retry | `ReconciliationPersistenceFailure`; execution remains blocked | controlled | cycle identity is idempotent |
| Canonical reject bundle | `runtime._persist_reject` | Authoritative reject/evidence | Existing #550 bounded fresh-connection retry | `REJECT_PERSISTENCE_FAILED`, evidence degraded, recovery-required | scanner survives | stable reject/signal/decision/lifecycle IDs |
| Normal lifecycle | runtime callback from `_emit_lifecycle_event` | Mandatory lifecycle evidence | #569 bounded fresh-connection retry | `LIFECYCLE_PERSISTENCE_FAILED`, recovery-required | scanner survives | stable lifecycle identity/upsert |
| PAPER pending position | `runtime._persist_pending_paper_position` | Authoritative PAPER position/risk state | #571 shared bounded fresh-connection retry | `PAPER_POSITION_PERSISTENCE_FAILED`; no phantom open position; recovery-required | scanner controlled | `trade_id` unique + `INSERT OR IGNORE` |
| Execution ownership schema/acquire/release | `execution_ownership.py` | Mutation fencing authority | #571 shared bounded fresh-connection retry | BUSY is explicit ownership-persistence failure; execution blocked; release may defer | controlled/fail-closed | account scope + fencing token |
| Geometry diagnostic | `runtime._persist_geometry_diagnostic` | Non-authoritative diagnostic | #571 shared bounded fresh-connection retry | diagnostic missing is explicit via incomplete evidence + metric; no favorable decision | scanner survives | deterministic observation ID |
| LIVE_PRECHECK decision evidence | `runtime._persist_live_precheck_evidence` | Required non-mutating readiness evidence | #571 shared bounded fresh-connection retry | `LIVE_PRECHECK_EVIDENCE_PERSISTENCE_FAILED`; no PASS without row | controlled/fail-closed | stable `live_precheck:<signal_id>` decision ID |
| Campaign maintenance/qualification | `burnin_campaign.py` | Campaign/evidence control plane | Existing `_with_fresh_lock_retry` + read-only progress rules | skip/defer or explicit operational failure by call site | supervisor controlled | campaign/run identity |
| Reject resolver | `burnin_campaign.BurnInCampaignRunner`, `burnin_resolver.py` | Forward-outcome evidence | Supervisor cycle catches lock exhaustion; campaign retry helpers used around authoritative maintenance | cycle deferred/explicit failure | runtime remains alive | pending-label identity/claim state |
| Agent shadow persistence | `agents/persistence.py` | Non-authoritative shadow | Existing bounded busy retry | shadow persistence error/defer only | production authority unaffected | run/stage identities |
| AIBrain internal persistence | `ai_brain.py::_run_decision_pipeline` | Secondary/internal diagnostic persistence | persistence exception logged and does not approve by itself | warning; canonical runtime evidence remains separate | scanner continues | stable signal/decision IDs |
| Burn-in closed outcome/periodic metrics | runtime burn-in helpers | Qualification evidence | errors mark burn-in evidence incomplete; not treated as PASS | evidence incomplete / fail-closed qualification | scanner generally continues | stable outcome/metric IDs |
| Runtime startup/attachment | campaign attach/bootstrap/state startup writes | Identity/safety bootstrap | classification depends on operation | `STARTUP_BLOCKED` or fatal non-transient; never favorable | runtime does not enter OPERATING without identity | campaign/run/config identity |
| burnin_ops CLI mutations | `burnin_ops.py` | Operator control plane | short-lived connections; explicit operation failure | command fails, runtime authority unchanged | out-of-process | operational event identity |

## Failure classifications

Runtime SQLite writes must resolve to one of these classes:

- **RETRYABLE_TRANSIENT** — SQLite BUSY/LOCKED; bounded fresh-connection retry.
- **RECOVERY_REQUIRED** — mandatory runtime evidence/mutation could not be durably completed after bounded retry. Trading authorization fails closed.
- **STARTUP_BLOCKED** — identity/safety bootstrap could not be proven before runtime operation.
- **DIAGNOSTIC_DEGRADED** — non-authoritative evidence could not persist; never changes a reject into an accept.
- **FATAL_NON_TRANSIENT** — malformed schema, missing table/column, integrity/programming error, invalid transition contract, or other non-BUSY database failure.

## Regression ownership

The protected `SQLite contention regression gate` must include:

- #550 atomic reject persistence tests;
- #569 lifecycle contention tests;
- #571 writer-family survivability tests;
- heartbeat/reconciliation/legacy contention tests;
- Phase-8 lock tests.

A new runtime SQLite writer is incomplete until its row is added to this matrix and its BUSY-exhaustion behavior has a deterministic test.
