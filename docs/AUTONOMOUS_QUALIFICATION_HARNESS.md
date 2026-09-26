# Autonomous Qualification Harness

## Purpose and isolation

The harness exercises AlphaForge's existing PAPER runtime, reconciliation, resolver, campaign, watchdog, qualification, and export paths without discovering or reusing a campaign database. Every invocation creates a directory named `alphaforge-qualification-*`, a new `qualification.sqlite3`, and an `artifacts` directory beneath the selected output root.

The harness constructs `RuntimeConfig` directly with `execution_mode=PAPER`, `live_trading_enabled=false`, and `allow_live_orders=false`. While it runs, the process-local database and execution environment variables point at the new qualification database and are restored during teardown. It never loads a production database path, attaches to an existing runtime, or starts an external worker process.

FAST is a deterministic accelerated qualification suitable for CI. SOAK accepts 6 through 24 hours, spaces the same deterministic injections over the requested wall-clock duration, and runs the production public market scanner plus clean read-only reconciliation and heartbeat probes between faults. Public scanning needs no exchange credentials and cannot submit orders. `--market-data synthetic` keeps the same schedule with deterministic offline market ticks when an external network is intentionally unavailable.

## Commands

Run accelerated FAST qualification:

```bash
.venv/bin/python -m alphaforge.autonomous_qualification \
  --mode fast \
  --output-root /private/tmp/alphaforge-autonomous-qualification
```

Run a six-hour SOAK qualification:

```bash
.venv/bin/python -m alphaforge.autonomous_qualification \
  --mode soak \
  --soak-hours 6 \
  --market-data public \
  --output-root /private/tmp/alphaforge-autonomous-qualification
```

Use `--json` for the full machine-readable result on stdout. Each run also writes `qualification-report.json` and `qualification-report.md` beneath its unique artifact directory. Final reports pin the Git commit captured when the harness was initialized, record the report-time HEAD and whether it changed during the run, and create a transactionally consistent `artifacts/qualification.sqlite3` snapshot with SHA-256 and `PRAGMA quick_check` metadata before teardown. Evidence references point at that preserved database artifact rather than the disposable runtime database path. Exit status is `0` for `PASS`, `1` for `NEEDS_FIX`, and `2` for a harness setup/environment `BLOCKED` result. A product-path scenario exception is captured as `NEEDS_FIX`, followed by automatic teardown.

## Architecture

`AutonomousQualificationHarness` owns one isolated SQLAlchemy engine and creates a separate campaign/run/runtime identity for every scenario. Provider faults enter through `BinanceReadonlyReconciliationProvider`; safety and recovery assertions observe `RuntimeOrchestrator`, `BurnInCampaignRunner`, persisted snapshots/events, and the final PAPER execution boundary. Campaign expiry and restart scenarios use the existing atomic campaign terminalizer and continuation logic. The watchdog, qualification, and export checks read the same isolated evidence and verify exported checksums and row counts.

Each scenario records injection and recovery times, provider classification, expected and observed behavior, invariant results, exact SQLite table/ID references, verdict, and recovery latency. Worker start and exit reasons are persisted and summarized. All open scenario contexts are terminalized and all database connections are disposed in `finally` cleanup.

## Fault injection matrix

| Fault | Injected path | Expected result |
|---|---|---|
| DNS `gaierror` | Signed read-only reconciliation | `TRANSIENT_TRANSPORT`; block execution; stay alive; recover only on committed CLEAN |
| `URLError` | Signed read-only reconciliation | Same transient recovery contract |
| Timeout | Signed read-only reconciliation | Same transient recovery contract |
| Connection reset | Signed read-only reconciliation | Same transient recovery contract |
| HTTP 429 | Signed read-only reconciliation | Same transient recovery contract |
| HTTP 5xx | Signed read-only reconciliation | Same transient recovery contract |
| HTTP 401/auth | Signed read-only reconciliation | `PERMANENT_AUTH_OR_PROTOCOL`; immediate atomic pause |
| Malformed response | Reconciliation response validation | `PERMANENT_AUTH_OR_PROTOCOL`; immediate atomic pause |
| Transient grace expiry | Runtime elapsed-time policy | Atomic campaign/run/mapping pause with outage reason |
| SQLite write contention | Reconciliation transaction | Remain blocked, replay deferred audit, recover after durable CLEAN |
| Stale heartbeat | Runtime heartbeat persistence | Explicit stale `RECOVERY_REQUIRED` evidence |
| Delayed candle/resolver data | Burn-in resolver | Keep resolver alive, block final execution, require CLEAN reconciliation |
| Reconciliation unavailable | Runtime without provider, then restored provider | Fail closed and remain alive until durable CLEAN |
| Worker restart/recovery | Campaign continuation state | Explicit old-worker exit and new continuation lineage |
| Duplicate/replayed write | Reconciliation cycle identity | Idempotent replay and exactly one persisted failure identity |

## Invariant matrix

| Area | Enforced assertion |
|---|---|
| Execution | Unknown exchange state and unavailable read-only state produce zero new PAPER execution |
| Final boundary | A previously accepted in-flight decision is cancelled if the last `_execute()` gate is unsafe |
| Status | An unavailable/unknown/fail-closed snapshot cannot report `OPERATING` |
| Transient outage | Runtime and resolver remain alive inside the grace window |
| Recovery | Resolver success alone cannot resume execution; a committed CLEAN reconciliation and explicit recovery row are required |
| Permanent failure | Auth/protocol failures cannot consume the transient grace window |
| Grace expiry | Campaign, active run, and campaign-run mapping become `PAUSED` atomically with an outage reason |
| Watchdog | A recovered failure sequence resets; a later outage starts a new count |
| Persistence | Each reconciliation attempt remains auditable; lock recovery preserves deferred evidence and cycle identities stay unique |
| Rejects | Canonical rejected decisions and persisted rejects remain equal where persistence is expected |
| Lifecycle | Every harness worker has one explicit start and one explicit terminal reason |
| Qualification/export | Qualification remains readable; export row counts and SHA-256 checksums match the isolated database |

## Existing nine backtest/trade-quality failures

The nine failures recorded by the prior POST363 work were re-run individually under the repository's normal operator environment and with their intended canonical values. Each is classified as an **obsolete test**: the assertion depended on a fixed default while the production function correctly honored values loaded from `.env`. None exercised transient provider recovery, reconciliation, persistence contention, or campaign lifecycle code.

| Test area | Count | Classification | Resolution |
|---|---:|---|---|
| Backtest filter switch daily-symbol case | 1 | Obsolete test | Pass the expected symbol limit explicitly |
| Rescue config/global order threshold | 1 | Obsolete test | Assert rescue construction preserves the configured threshold |
| BACKTEST/PAPER low effective-RR parity | 1 | Obsolete test | Pass the canonical effective-RR threshold explicitly |
| BACKTEST/PAPER missing-expectancy parity | 1 | Obsolete test | Enable unknown-expectancy blocking explicitly |
| Phase 1–3 major reject reasons | 1 | Obsolete test | Enable unknown-expectancy blocking explicitly |
| Trade-quality missing expectancy | 1 | Obsolete test | Pass the fail-closed policy explicitly |
| Trade-quality symbol/global daily limits | 2 | Obsolete test | Pass both expected limits explicitly |
| Trade-quality softened stop/low effective RR | 1 | Obsolete test | Pass the expected effective-RR threshold explicitly |

The corrected nine tests pass in the ordinary repository environment. The complete suite passes without using environment overrides.

## Wall-clock SOAK release gate

The 6–24 hour SOAK records a resource and safety sample every 30 seconds in `soak-resource-samples.jsonl`. Public market-data scans run every tenth sample; read-only reconciliation, resolver ticks, campaign and runtime heartbeats, state snapshots, and lineage checks continue at the 30-second cadence. Fault scenarios are spaced evenly across the requested wall-clock duration, rather than injected together at startup.

Each sample checks worker liveness, a runtime heartbeat age no greater than 120 seconds, CLEAN reconciliation, resolver success, compatible RUNNING campaign/run/mapping state, reject persistence parity, fail-closed status correctness, and bounded queue depths. The report includes actual elapsed wall-clock seconds, the sample log, reconciliation and scan latency, process RSS high-water trend, database/artifact growth, pending position/reject backlog, deferred reconciliation queue depth, and SQLite lock-retry exhaustion count. RSS high-water is a conservative memory indicator; it does not measure memory returned to the operating system.

The SOAK resource gate flags RSS growth over 128 MiB, artifact growth over 64 MiB, database growth above the larger of 64 MiB or 160 KiB per persisted sample, strictly increasing RSS high-water across the last twelve samples by more than 8 MiB, or queue/backlog depth over 32. The per-sample database budget accounts for mandatory append-only audit evidence and still flags an abnormal write rate. These are qualification guardrails, not production configuration changes. Public scans record every availability result in campaign events. A single empty scan must recover by the next five-minute probe, no more than 5% of scans may be empty, and the feed must be available at completion. Consecutive empty scans, any failed safety sample, unexplained worker exit, inconsistent lineage, missing recovery evidence, or wall-clock duration shorter than requested yields `NEEDS_FIX`. The scanner suppresses the underlying provider exception, so an empty probe is honestly classified `UNKNOWN` until the scanner gains diagnostics.

A release may proceed to a **new** real PAPER burn-in only when the full repository suite passes in an isolated test checkout, FAST passes in a new temporary workspace, the full public SOAK finishes `PASS`, and its report shows no safety invariant failures, resource flags, persistence gaps, unexplained exits, or campaign lineage drift. This gate does not authorize LIVE trading or launch a burn-in itself.

## Historical validation and current limits

The validation counts and six-hour runs below are historical evidence for the
commits that produced them. They do not qualify a newer SHA; release qualification
must run FAST and public SOAK again on the exact code/config being released.

The focused runtime, reconciliation, contention, campaign, watchdog, qualification, readiness, and harness set passed 325 tests for the initial harness. The SOAK sampling/feed-gap regressions passed 12 tests. The updated full suite in a new isolated `/private/tmp` checkout passed 1,535 tests with 3 skips. The fresh FAST qualification passed all 15 injected faults and 17 total scenario/cross-scenario checks with zero invariant failures, persistence gaps, unexplained exits, or inconsistent campaign lineage.

FAST does not prove multi-hour stability. Public SOAK market-data probes depend on external exchange availability; synthetic SOAK is an offline substitute and cannot prove that availability. Signed-account reconciliation remains deterministic because the qualification harness never consumes production credentials. A persistent SQLite outage cannot make new audit evidence durable until the writer recovers; execution remains blocked during that interval. These qualifications do not establish LIVE readiness. The first completed six-hour public run in `/private/tmp/alphaforge-autonomous-qualification/alphaforge-qualification-gp339ja9` produced `NEEDS_FIX`: two of 72 scans returned no rows, while all 720 continuous safety samples and resource checks passed. The harness now audits each empty scan and its recovery. The final real six-hour public run at `/private/tmp/alphaforge-autonomous-qualification/alphaforge-qualification-z54y1qgr` produced `PASS` after 21,602.472 seconds: 720 safe samples, 15 passing fault scenarios, one empty public scan explicitly recovered at the next probe, no unexplained worker exit, no persistence or lineage gap, and no resource-growth flag. Its machine and human reports are in the isolated artifact directory.
