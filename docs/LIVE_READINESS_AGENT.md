# LIVE Readiness Agent v1

The agent is an observational evidence and gate engine. It is not an execution controller and cannot authorize LIVE.

It opens a target SQLite database with `mode=ro`, enables `query_only`, and installs a deny-write authorizer. It does not import campaign control helpers, write readiness rows, alter `.env`, submit/cancel/amend orders, change scoring or thresholds, or start runtime processes. Reports are separate JSON/text files.

Each gate emits an id, status, reason, source, observed value, expected condition, timestamp, scope identity, and blocker severity. Gate statuses are `PASS`, `BLOCKED`, `NOT_OBSERVABLE`, or `NEEDS_FIX`. A missing or stale source is never a pass. The top-level result is `BLOCKED` when a blocking gate exists; otherwise it remains `READINESS_INCOMPLETE` until all mandatory evidence is explicitly present. The maximum autonomous posture is `READY_FOR_OPERATOR_REVIEW`; the agent never emits `LIVE_ENABLED`.

```bash
python -m alphaforge.live_readiness \
  --db data/campaign/G6MANUAL01.db \
  --campaign-id camp_xxx \
  --json \
  --output-dir artifacts/live_readiness/camp_xxx
```

The paused campaign `camp_76d8ac53157c1337` is historical evidence only. Its 17 rejected decisions, zero accepted decisions, zero resolved labels, stale heartbeat, and insufficient burn-in duration cannot support profitability, reject-quality, or LIVE qualification conclusions.
