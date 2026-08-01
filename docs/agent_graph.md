# Shadow Agent Graph — Phase A

## Purpose and scope

Phase A adds typed, deterministic software-agent contracts—not chat personas—and a non-authoritative trace graph. The legacy runtime remains the sole source of PAPER/LIVE/BACKTEST decisions and execution. No market, signal, quality, risk, execution, verification, reflection, or portfolio business logic is implemented here; absent handlers produce explicit `SKIPPED / STAGE_HANDLER_NOT_REGISTERED` evidence.

The fixed order is `MARKET → SIGNAL → QUALITY → RISK → EXECUTION → VERIFICATION → REFLECTION → PORTFOLIO`. Registration is explicit. Graph generation, recursion, LLM calls, self-prompting, threshold changes, exchange calls, order planning/submission/cancellation/simulation, and production cutover are prohibited.

## Shadow controls

| Variable | Default | Meaning |
|---|---:|---|
| `ALPHAFORGE_AGENT_GRAPH_ENABLED` | `false` | Opt in to the runtime shadow hook. |
| `ALPHAFORGE_AGENT_GRAPH_SHADOW` | `true` | Phase A must remain shadow-only. |
| `ALPHAFORGE_AGENT_GRAPH_MAX_STEPS` | `12` | Maximum handler invocations. |
| `ALPHAFORGE_AGENT_GRAPH_MAX_REFLECTION_RETRIES` | `1` | Maximum permitted reflection retry index. Phase A does not request retries. |
| `ALPHAFORGE_AGENT_GRAPH_STAGE_TIMEOUT_SECONDS` | `5` | Per-handler timeout. |
| `ALPHAFORGE_AGENT_GRAPH_PERSIST_TRACES` | `true` | Store shadow traces when persistence is available. |
| `ALPHAFORGE_AGENT_GRAPH_MAX_PENDING_RUNS` | `64` | Bound the single-worker queue; overload drops the newest trace. |
| `ALPHAFORGE_AGENT_GRAPH_DATABASE_URL` | `sqlite+pysqlite:///data/runtime/alphaforge_agent_shadow.db` | Isolated trace database. |

The hook runs only when enabled **and** shadow is true. It receives a JSON snapshot after the legacy decision is produced and enqueues it without awaiting agent work on the order path. One worker consumes the bounded queue and serializes trace transactions. At capacity, the deterministic overload policy retains older queued evidence and drops the newest trace. Queue depth, dropped/deferred traces, persistence retry count, lock-wait milliseconds, and worker count are exposed in runtime metrics and heartbeat evidence. Errors are diagnostic and cannot approve, reject, delay, retry, or mutate the legacy decision. Cancellation is propagated for clean shutdown. No graph flag participates in Phase 8/9 campaign identity because these controls cannot affect execution-critical decisions; including them would unexpectedly split an otherwise identical burn-in campaign.

## SQL evidence and safety policy

`agent_runs` stores queryable run identity, mode, symbol, status, timing, shadow marker, legacy-reference hash, config hash, and orchestrator version. `agent_stage_events` stores queryable stage/status/timing/hash fields, while reason-code and evidence detail use deterministic JSON. Controlled initialization bootstraps the isolated shadow database once only when the feature is enabled; repository construction performs no DDL. Writes are parameterized, short, transactional, single-worker serialized, and use bounded `SQLITE_BUSY` retry. Duplicate decision/stage/retry identities are ignored deterministically. The canonical runtime `init_db` does not create agent tables. These tables are separate from all order, trade, lifecycle, burn-in, reconciliation, and authorization tables. Unavailable optional values remain SQL `NULL` or JSON `null`; they are never fabricated as zero.

A handler hard reject is represented by `status=REJECT` plus `evidence.hard_reject=true`; every later stage becomes `SKIPPED / UPSTREAM_HARD_REJECT`. The fixed stage list, step budget, timeout, and configured retry ceiling prevent unbounded execution. Persistence failures appear on the immutable run result and never escape into legacy trading.

## Rollout

- **Phase B:** implement read-only Market/Signal/Quality handlers with parity evidence.
- **Phase C:** implement advisory Risk/Execution planning without adapters or mutation.
- **Phase D:** add Verification/Reflection and Portfolio advisory behavior with bounded policies.
- **Phase E:** separately reviewed cutover proposal only after sustained shadow parity, lifecycle/persistence validation, safety authorization, and rollback evidence. Phase A grants no LIVE readiness.
