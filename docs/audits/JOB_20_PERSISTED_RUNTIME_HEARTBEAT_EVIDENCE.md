# JOB-20 — Persisted Runtime Heartbeat & Freshness Evidence

**Audit date:** 2026-05-23  
**Base:** `dev` after merged PR #165 at `a97a6c3df84cdc7bcdb89fc8d6ef133e1ab4c41e`  
**Posture:** additive observability and readiness evidence only  
**LIVE verdict:** still fail-closed unless every independent qualification gate passes

## Gap closed

PR #165 correctly rendered runtime heartbeat as missing because the dashboard had no persisted evidence produced by the trading runtime. Dashboard health could not be treated as runtime health. JOB-20 adds the missing evidence contract without making the dashboard a writer or control surface.

## Persisted contract

The runtime-owned `runtime_heartbeats` table is created additively on the first eligible runtime heartbeat write. Existing SQLite data is not deleted, rewritten or migrated destructively.

| Column | Evidence purpose |
|---|---|
| `runtime_instance_id` | Distinguishes a current process instance from historical rows |
| `execution_mode` | Records `PAPER` or `LIVE`; `BACKTEST` does not write heartbeat evidence |
| `heartbeat_ts` | UTC timestamp evaluated for freshness |
| `scanner_source` | Records scanner provenance already known by runtime |
| `runtime_state` | `OPERATING` is eligible; `STOPPING` fails closed |
| `last_scan_ts`, `last_decision_ts` | Read-only recency context |
| `active_positions_count`, `pending_orders_count` | Runtime-reported visibility context |
| `evidence_status` | Marks measured runtime heartbeat evidence |
| `payload_json` | Allowlisted runtime metrics only |

## Freshness contract

Default maximum age: **120 seconds**. Evaluation is deterministic and fail-closed:

| State | Meaning |
|---|---|
| `FRESH` | Latest mode-qualified row is well formed, operating and within age limit |
| `STALE` | Latest mode-qualified row exceeds the age limit |
| `MISSING` | No mode-qualified row exists |
| `INVALID` | Timestamp, evidence state or runtime state is unusable |
| `FUTURE_DATED` | Timestamp is materially ahead of evaluation time |

The evaluator uses the most recently persisted row for the required mode. It does not search backward for an older favorable row after a newer stopping or malformed row exists.

## Runtime ownership boundary

Only the trading runtime produces heartbeat rows. It emits heartbeat evidence in `PAPER` and `LIVE` operation, using one `runtime_instance_id` per orchestrator instance. `BACKTEST` does not produce heartbeat evidence.

For LIVE startup, the runtime can emit its own liveness evidence while it is executing the guarded qualification path, before trading worker loops begin. That heartbeat can satisfy only the liveness sub-check. A failed independent readiness gate causes a latest `STOPPING` row and the runtime remains blocked.

## Dashboard boundary

The dashboard reads persisted evidence through its existing SQLite read-only connection path. It does not:

- create a missing runtime database;
- create or update heartbeat rows;
- run runtime or exchange probes;
- add order, LIVE-activation, configuration or emergency-control mutations.

`GET /api/v1/runtime/status` displays timestamp, mode, runtime instance, state and freshness reason when evidence exists. `GET /api/v1/readiness/probes` evaluates a LIVE-qualified heartbeat row and exposes its fail-closed freshness state.

## Qualification boundary

A fresh `PAPER` heartbeat is valid runtime visibility evidence but is never a LIVE qualification substitute. LIVE qualification requires a fresh persisted `LIVE` heartbeat plus all pre-existing readiness, parity, reconciliation, observability, rollback, deployment and acknowledgement gates.

## Tests specified in this increment

- fresh PAPER heartbeat is visible in dashboard runtime status;
- missing, stale, future-dated and malformed evidence fails closed;
- dashboard reading does not add heartbeat rows;
- equal timestamp rows select the most recently persisted row deterministically;
- LIVE readiness rejects missing, stale and PAPER-only evidence;
- fresh LIVE evidence satisfies only the heartbeat sub-check and cannot bypass another failed gate;
- runtime-owned PAPER writer persists evidence and BACKTEST does not;
- heartbeat payload persists allowlisted metrics only.

## Risk assessment

This change creates write volume proportional to the existing runtime heartbeat interval and adds one read-only dashboard query surface. It does not alter selection logic, scoring, effective reward/risk calculations, regime behavior, position sizing, order submission behavior or exchange access. The dominant operational risk is stale-row accumulation, which is observable and can be addressed later with an archival/retention policy after evidence behavior is stable.