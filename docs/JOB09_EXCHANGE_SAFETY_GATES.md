# JOB-09 — Exchange Safety Gates

Status: exchange-side fail-closed safety contract introduced.

No threshold loosening.
No trade-frequency increase.

Unsafe exchange conditions must block execution.

---

# Objective

Protect AlphaForge from:

- stale orderbooks,
- degraded connectivity,
- latency spikes,
- spread explosions,
- websocket drift,
- unstable funding conditions,
- clustered API failures.

A technically valid setup is still invalid if:

```text
market infrastructure quality is degraded
```

---

# Existing healthy infrastructure

`src/alphaforge/exchange_connectivity.py`
already contained:

- public exchange health checks,
- orderbook verification,
- funding verification,
- latency measurement,
- secret leak protection,
- exchange capability awareness.

This was already a strong foundation.

---

# New safety contract

Added:

```text
src/alphaforge/exchange_safety_gates.py
```

New fail-closed evaluation:

```python
evaluate_exchange_safety(...)
```

Outputs:

- allowed
- reject_reasons
- warnings
- diagnostics

---

# New reject conditions

Added:

- EXCHANGE_UNAVAILABLE
- PUBLIC_MARKET_DATA_UNAVAILABLE
- ORDERBOOK_UNAVAILABLE
- FUNDING_UNAVAILABLE
- LATENCY_SPIKE
- SPREAD_EXPANSION
- FUNDING_ANOMALY
- ORDERBOOK_STALE
- WEBSOCKET_STALE
- API_ERROR_CLUSTER

---

# Why this matters

Many systems fail not because:

```text
signal logic was wrong
```

but because:

```text
execution environment degraded
```

Examples:

- websocket freeze,
- delayed orderbook,
- exchange partial outage,
- stale market data,
- spread explosion during panic.

Execution-aware systems must fail closed.

---

# Important production rule

Missing or uncertain exchange state must never become:

```text
implicitly safe
```

Unknown safety state should bias toward:

```text
rejection
```

not optimistic execution.

---

# Diagnostics added

File:

```text
sql/diagnostics/job09_exchange_safety_gates.sql
```

Includes:

1. exchange connectivity evidence
2. stale/dangerous market context
3. suspicious optimistic execution rows
4. reject reason concentration

---

# Expected healthy behavior

Healthy runtime behavior should show:

- rejection during stale conditions,
- lower participation during spread expansion,
- fail-closed behavior under degraded connectivity,
- visible safety reject reasons.

Trade count may decrease.

This is healthy.

---

# Remaining blockers

Still remaining:

1. reconciliation hardening still partial.
2. adaptive risk allocation absent.
3. sustained live evidence absent.
4. live readiness report incomplete.

---

# Next mandatory job

```text
JOB-10 — Lifecycle Reconciliation
```

because even healthy exchanges can still create:

- orphan orders,
- uncertain fills,
- duplicate fills,
- state divergence.

---

# Production stance

Current state:

```text
exchange-side safety is now explicitly measurable and fail-closed.
```

But:

```text
true survivability still depends on reconciliation integrity under real runtime stress.
```
