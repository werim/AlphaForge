# AlphaForge LIVE Readiness Roadmap

**Status:** Current completion roadmap with preserved historical baseline
**Created:** 2026-05-22  
**Baseline branch:** `dev`  
**Baseline version:** `0.3.25-dev`  
**Current verdict:** ❌ **NOT LIVE-READY**

## Completion roadmap without Mac mini

This 26 October 2026 review separates portable software, target-environment
acceptance and LIVE readiness. The dated baseline findings and proposed sequence
below are historical, not a current defect queue or an authorization. Current
code/config, exact-source tests and the active PR/issue handoff take precedence.
Use [the repository map](../README.md#repository-navigation) and
[verification router](TEST_PROTOCOL.md#development-verification-routing) for
implementation/test ownership; this roadmap adds no parallel authority.

Statuses: **VERIFIED** means the stated scope has attributable evidence;
**IMPLEMENTED BUT UNVERIFIED** means implementation exists but its required
behavioral evidence is absent; **INCOMPLETE** means required work/evidence remains;
**BLOCKED** identifies a concrete external prerequisite; **UNKNOWN** means it has
not been established. Software verification never promotes physical acceptance.

| Phase | Required outcome | Current evidence/status | Dependencies | Work possible without Mini | Acceptance criterion |
| --- | --- | --- | --- | --- | --- |
| Portable core | Canonical strategy/MTF decisions, executable economics, portfolio risk, lifecycle/persistence, recovery/reconciliation | VERIFIED only within exact-source CI/regression scope; whole-release validation INCOMPLETE | Final source/config; existing router | Focused regressions, deterministic replay, isolated integration/fault checks and CI; fix demonstrated defects | Required tests and parity pass for final source/config; unknown state remains blocked |
| Historical strategy | Leakage-safe OOS with attributable post-cost capital performance | INCOMPLETE; positive realized cash cannot establish capital return/drawdown | Historical contract; attributable data/capital/flows/equity and complete runtime scope | Validate supplied artifacts; prepare authority requirements; no fabricated ledger/backfill | Admissible split/search lineage and complete economics; current adapter's INCOMPLETE/FAIL retained |
| Target storage | Native APFS/sandbox proof and representative runtime calibration | BLOCKED for fresh acceptance; #637 is unmerged; old failures/proofs remain historical | Mini access; fresh dedicated backend; exact source/config/dependencies and proof | Inspect software contracts; prepare bounded package in existing #637 handoff | Required native proof, actual six-hour coverage and independent terminal audit; MEASURED alone is not admission |
| Operational release | Reproducible deploy, observable healthy runtime, fault/recovery, rollback and storage admission | IMPLEMENTED BUT UNVERIFIED for complete target acceptance | Final release identity; storage certificate/budget; authenticated read-only reconciliation | Dependency/import checks, isolated recovery tests, release/runbook review | Applicable G0–G10 plus #421 FAST/public SOAK and #349 forward qualification; identity-bound reports, cleanup and no unresolved blockers |
| Economic baseline | Mature fresh dynamic-universe PAPER evidence (#344) | INCOMPLETE | Qualified storage/host; new campaign/release; canonical execution/risk evidence | Prepare bounded acceptance queries; inspect existing evidence read-only | Mature attributable trade/reject outcomes, complete costs, portfolio/reconciliation/storage evidence; no cross-SHA mixing |
| Learning software | Immutable challengers → OOS promotion/rollback → durable epochs (#578 → #579 → #580) | INCOMPLETE; not qualified by core CI | Mature #344 verdict and stable predecessor contracts | Specify implementation-ready tasks below; preserve dependency gates | Each issue's adversarial/restart/idempotence tests; no active policy mutation from learning reader |
| LIVE readiness | Verified operational/capital safety and permitted exchange behavior | BLOCKED; complete target execution capability UNKNOWN in this review | All mandatory acceptance; separately scoped execution/security work and operator authorization | Non-mutating readiness evaluation; no exchange mutation | Mandatory evidence independently verified; no readiness result enables LIVE |

### Shortest dependency paths

1. **Portable software:** reuse matching CI/regression evidence → fix only
   demonstrated gaps → exact-final-source focused/required checks → isolated FAST.
   Full autonomous product completion also requires #344 → #578 → #579 → #580;
   Mini loss does not invalidate portable core evidence, and core CI does not
   satisfy those missing products or their economic input gate.
2. **Target acceptance:** revalidate native source/config/dependencies/ownership →
   fresh backend and proof → authenticated read-only/canonical preflight → bounded
   actual calibration → independent terminal audit → separately reviewed scoped
   growth budget/admission → exact-final-release public SOAK and fresh PAPER gates.
   Keep #637 unmerged until its protected acceptance contract is cleared.
3. **LIVE readiness:** completed software plus target/economic/operational gates →
   non-mutating readiness assessment → separately designed/authorized venue
   execution acceptance, if required → separate LIVE authorization. This document
   authorizes no Testnet or LIVE submit/cancel/modify/funds action.

### Implementation-ready remaining tasks

- **Historical capital authority:** extend the existing realized-accounting and
  walk-forward authorities only after defining attributable opening capital,
  external flows, concurrent/open-position marks, complete closure population
  and matching campaign/run/release/config identities. Never substitute notional,
  initial risk or a balance literal. Acceptance must reject missing/mixed scope,
  duplicate realization, incomplete marks/flows, future leakage and inconsistent
  cash; portfolio returns/drawdown remain null until the authority is complete.
- **#578:** after #344, reuse canonical trade/reject evidence and parameter owners;
  produce bounded immutable challengers or NO_CHANGE/INSUFFICIENT_EVIDENCE. Test
  incomplete costs, stale/cross-scoped rows, overfit concentration and bound
  violations. Do not implement tuning against an unqualified baseline.
- **#579:** consume immutable challengers through the existing split/lineage
  contract; one transactional PAPER champion/promotion/rollback authority. Test
  OOS reuse, insufficient evidence, concurrent promotion and crash/restart.
- **#580:** consume stable #578/#579 contracts with persisted fenced epochs,
  unchanged-evidence no-op and bounded retry/compute. Test duplicate triggers,
  ownership takeover and crashes at every transition; no autonomous LIVE path.
- **Operational acceptance:** use existing readiness, reconciliation, recovery,
  storage and qualification authorities. Require measured resource/heartbeat/feed
  and evidence-health observations, rollback, process containment and consistent
  terminal copies. A passed mock provider is not authenticated reconciliation.

These are acceptance tasks under the existing issues, not a second task queue.
No new production subsystem is justified merely by the absence of Mini access.

### Portable, equivalent and mandatory physical evidence

Ordinary SQLite persistence, replay, concurrency, failure classification and
safety parity can be checked in isolated local execution/CI. Their evidence is
limited to the actual interpreter, dependencies, OS and filesystem tested.
Synthetic clocks and providers provide useful fault-invariant evidence, not
wall-clock health, authenticated exchange state or representative economics.

Linux containers do not reproduce macOS sandbox/APFS quota behavior. A local
Apple Silicon Mac does not certify the designated Intel Mini's identity,
resources, native sandbox, watchdog/process lifetime or six-hour workload.
Another host is admissible only if the existing acceptance contract explicitly
permits it; never silently substitute it.

The bounded physical package remains in the single
[PR #637 handoff](https://github.com/werim/AlphaForge/pull/637), including pinned
source/config/dependencies, stage order, new-backend identity prerequisites,
resource/time/log bounds, abort rules, proof/plan/claim binding, preservation and
terminal audit. Fresh target execution is **NOT_RUN** here. Do not reconnect,
provision, probe, prepare/run, reuse consumed claims, inject faults, resume
protected PAPER/SOAK or merge #637 as a side effect of this portable review.
Mutable native prerequisites must be refreshed when access is restored; historical
staging or physical success cannot be inherited by a new source/run.

## Purpose

This document is the standing reference for future AlphaForge LIVE-readiness updates. It is intentionally defensive: real capital must not be exposed until execution-adjusted expectancy, audit integrity, exchange correctness, and operational fail-closed controls have been demonstrated through PAPER and Testnet evidence.

Success is stable positive expectancy after spread, slippage, latency, fees, liquidity degradation and operational failure modes. Trade count, raw win rate, and isolated PnL spikes are not readiness evidence.

## Verified Progress at Baseline (historical)

The `dev` branch currently includes useful groundwork:

- Read-only PAPER/LIVE exchange scanner wiring, while BACKTEST/offline paths retain safe scanner capability.
- Binance Futures spread derivation from public `bookTicker` bid/ask rather than optimistic fabricated spread.
- Runtime persistence improvements for `signal_id`, rejected-decision completeness, final vs AI-internal audit semantics, and diagnostic lifecycle errors.
- A LIVE connectivity gate and a LIVE qualification subsystem.
- Centralized environment/config loading for a subset of runtime and exchange settings.

These improvements are preparation only. They do not authorize LIVE operation.

## Blocking Findings at Baseline (historical)

### P0-1: Placeholder/safe scanner LIVE block can be bypassed through wrapper wiring

`RuntimeOrchestrator.start()` checks the function name `_safe_market_scanner`, while `_build_runtime_from_env()` supplies `_runtime_market_scanner`, which can internally return safe scanner data when `ALPHAFORGE_RUNTIME_SAFE_SCANNER=1`. LIVE must validate resolved scanner provenance, not merely the callable wrapper name.

**Required outcome:** LIVE with any placeholder/safe/mock scanner source fails before loops start.

### P0-2: Runtime bootstrap does not wire a real execution adapter

`_execute()` rejects LIVE execution when `real_execution_adapter` is absent, but bootstrap does not attach one. A setup could reach accepted execution and only then fail.

**Required outcome:** LIVE fails at startup if a validated real/testnet execution adapter is not present.

### P0-3: Binance connectivity check does not validate the same market path used by the scanner

Runtime scanner uses Binance Futures `/fapi/v1/...` endpoints. Connectivity currently checks a spot `/api/v3/ticker/bookTicker` endpoint. Spot health cannot qualify Futures readiness.

**Required outcome:** LIVE health verifies the exact Futures venue and mandatory data endpoints used by runtime.

### P0-4: Default Binance base URL is inconsistent with Futures scanning

Scanner expects Futures endpoints, while the typed config fallback uses `https://api.binance.com`; `.env.example` contains `https://fapi.binance.com`. Omitting the environment override can produce incorrect endpoint resolution.

**Required outcome:** Futures is the canonical default and configuration tests prove it.

### P0-5: Persisted `effective_rr` is not truly execution-adjusted

The execution cost model exists, but persisted `effective_rr` is effectively populated as raw `risk_reward` in the decision path. Costs influence portions of scoring without producing the single auditable execution-adjusted RR gate required for LIVE.

**Required outcome:** `effective_rr = raw_rr - spread_penalty - slippage_penalty - latency_penalty - funding_penalty - liquidity_penalty - fee_penalty`, with fail-closed handling for missing context and a configured minimum threshold.

### P0-6: Real scanner does not produce a validated trade plan/RR

Runtime defaults missing RR to `2.0`, while the exchange scanner does not produce structure-based stop, target or RR values. Real market candidates must not inherit synthetic RR defaults.

**Required outcome:** accepted candidates carry a validated trade plan; missing trade plan is rejected or left watching.

### P0-7: LIVE qualification includes declared-success snapshots rather than measured evidence

Current qualification invocation passes positive mode parity, reconciliation and observability snapshots as supplied constants. Readiness must be backed by observed database, exchange and monitoring evidence.

**Required outcome:** readiness checks persist measured evidence and never qualify LIVE from hardcoded positive values.

### P0-8: Documented safety environment values are not all wired into runtime behavior

The environment template lists `ALPHAFORGE_ALLOW_LIVE_ORDERS`, `ALPHAFORGE_DRY_RUN`, daily loss/risk-per-trade limits, `MIN_EFFECTIVE_RR`, max expected slippage, correlation limit, unknown-expectancy rejection and fee inputs. These must either be wired and tested or explicitly marked unavailable/reserved.

**Required outcome:** no safety flag is represented as active unless code and tests prove enforcement.

## P1 Readiness Gaps at Baseline (historical)

### Selector rejects are not complete canonical SQL/lifecycle evidence

Candidates rejected before `_process_symbol()` are summarized but do not necessarily become final decision and lifecycle rows. This understates real rejection behavior and corrupts learning/readiness metrics.

**Required outcome:** each rejected candidate creates auditable `SIGNAL_CREATED -> SIGNAL_REJECTED` evidence with explicit reason and context.

### PAPER does not yet constitute execution-adjusted expectancy proof

PAPER needs closed-position lifecycle, fee/slippage-adjusted net PnL, adaptive learning updates, and SQL-backed stats context before it can support LIVE qualification.

**Required outcome:** PAPER produces sufficient completed, cost-adjusted, reconciled samples.

### Market/execution context contract is incomplete

A canonical context is needed for exchange source, market type, bid/ask, spread, expected slippage, latency, liquidity, imbalance, funding, volatility, regime, spoof/absorption features, timestamps and completeness state.

**Required outcome:** missing mandatory context is a deterministic reject, never silently replaced by optimistic zeros.

## Venue Policy at Baseline (historical)

- Initial executable venue: Binance USDT-M Futures only.
- Hyperliquid remains observation-only or disabled for trading until bid/ask or orderbook quality, funding, execution adapter, fill/reconciliation and cost-model evidence are implemented.
- LIVE universe begins with the most liquid approved instruments only after Testnet and canary gates pass.

## Delivery Program at Baseline (historical)

### Stage 0: Lock LIVE Fail-Closed

Implement and test:

- Bind `ALPHAFORGE_ENABLE_LIVE_TRADING`, `ALPHAFORGE_ALLOW_LIVE_ORDERS`, and `ALPHAFORGE_DRY_RUN` to runtime enforcement.
- Reject LIVE startup when safe/mock scanner provenance is resolved, including override-through-wrapper cases.
- Reject LIVE startup when a real execution adapter is absent.
- Enforce global kill switch both before submit and inside runtime flow.

**Exit condition:** No LIVE runtime loop or submit path starts unless every hard safety gate is explicitly satisfied.

### Stage 1: Venue and Public Market-Data Correctness

Implement and test:

- Use `https://fapi.binance.com` as canonical Binance Futures base URL fallback.
- Make connectivity check the same Futures venue/endpoints required by scanner.
- Verify funding health from an actual funding endpoint.
- Persist market source and market type on candidate/decision evidence.
- Keep Hyperliquid non-executable while context is incomplete.

**Exit condition:** Scanner and health gate prove the same executable market path.

### Stage 2: Execution-Adjusted RR and Trade Planning

Implement and test:

- Connect one canonical cost model to decision gating and persistence.
- Add fee penalty and execution-context completeness.
- Produce measured or conservative expected slippage and latency context.
- Introduce structure-based stop/target/raw RR planning.
- Remove acceptance through fallback RR values.
- Wire `MIN_EFFECTIVE_RR` and reject reasons.

**Required reject reasons:**

- `MISSING_TRADE_PLAN`
- `EXECUTION_CONTEXT_INCOMPLETE`
- `LOW_EFFECTIVE_RR`
- `HIGH_EXPECTED_SLIPPAGE`
- `HIGH_SPREAD`
- `HIGH_LATENCY`
- `THIN_LIQUIDITY`
- `UNKNOWN_EXPECTANCY`

**Exit condition:** Every accepted candidate has a complete, cost-adjusted and auditable execution proposition.

### Stage 3: Lifecycle and SQL Audit Integrity

Implement and test:

- Persist selector-level rejects as canonical final decision rows.
- Emit complete lifecycle transitions for rejects and accepts.
- Retain AI-internal vs final decision separation.
- Persist penalty decomposition and context completeness.
- Use explicit unavailable states rather than zero-filled unknown execution values.

**Exit condition:** Zero incomplete canonical final rows and zero invalid lifecycle transitions over validation runs.

### Stage 4: PAPER Evidence Program

Implement and test:

- Closed PAPER position tracking with TP, SL, timeout/cancel and protective exits.
- Net PnL after fees, spread and slippage.
- Closed-trade adaptive learning and non-empty SQL-backed `stats_ctx`.
- Rejected-signal shadow outcomes.
- Measured readiness snapshots rather than constant successful values.

**Minimum gate before Testnet:**

| Metric | Requirement |
|---|---:|
| Canonical final decisions | >= 1,000 |
| Completed PAPER trades | >= 100 |
| Incomplete critical audit rows | 0 |
| Invalid lifecycle transitions | 0 |
| Accepted trades with unknown execution context | 0 |
| Accepted trades below minimum effective RR | 0 |
| Reconciliation orphan/duplicate fill findings | 0 |
| Net expectancy after modeled costs | Positive |
| Max drawdown | Inside predefined risk budget |

### Stage 5: Binance Futures Testnet Execution

Implement and test:

- Signed Testnet execution adapter.
- Submit, cancel, query order, query position and fetch fills.
- Client-order-ID idempotency.
- Timeout/missing-ack reconciliation.
- Partial-fill handling.
- Process restart state recovery.
- Kill-switch cancellation and no-new-order behavior.

**Minimum gate before any real-money canary:**

| Metric | Requirement |
|---|---:|
| Testnet submitted orders | >= 100 |
| Duplicate order events | 0 |
| Unreconciled ambiguous execution states | 0 |
| Missing fill audit rows | 0 |
| Kill-switch failures | 0 |
| Execution-cost persistence gaps | 0 |

### Stage 6: Micro-Canary LIVE

Initial real-money operation, only after all earlier gates, must use:

- Binance Futures only.
- Very small risk budget with hard per-trade and daily loss limits.
- One concurrent position maximum.
- Highly liquid, explicitly approved symbol universe.
- Stricter effective RR threshold than PAPER.
- `UNKNOWN_EXPECTANCY` rejection.
- Operator acknowledgement on each deployment/startup.
- Tested emergency exit and kill-switch behavior.

**Automatic stop triggers:**

- Daily loss limit hit.
- Orphan order/position or duplicate fill detected.
- Unexpected slippage breach.
- Incomplete execution context on an otherwise acceptable trade.
- Exchange health degradation.
- Persistence write failure.
- Lifecycle transition error.
- Kill-switch activation.

## Planned PR Sequence at Baseline (historical)

| Order | Proposed PR | Priority |
|---:|---|---:|
| 1 | Fail-close LIVE bootstrap and remove safe-scanner bypass | P0 |
| 2 | Align Binance Futures config and connectivity gate | P0 |
| 3 | Wire real execution-cost model and effective RR gate | P0 |
| 4 | Persist selector rejects and complete lifecycle audit | P1 |
| 5 | Add PAPER position-close and expectancy learning loop | P1 |
| 6 | Replace synthetic readiness snapshots with measured evidence | P1 |
| 7 | Implement Binance Futures Testnet execution adapter | P2 |
| 8 | Add canary risk envelope and LIVE kill-switch controls | P3 |

No LIVE run should occur until PRs 1 through 4 are complete. No real-money operation should be considered until PRs 1 through 6, the PAPER evidence program, and Testnet validation are complete.

## Baseline Code Locations Reviewed

- `src/alphaforge/runtime.py`
- `src/alphaforge/config.py`
- `src/alphaforge/exchange_market_scanner.py`
- `src/alphaforge/exchange_connectivity.py`
- `src/alphaforge/execution.py`
- `src/alphaforge/ai_brain.py`
- `src/alphaforge/symbol_selector.py`
- `src/alphaforge/live_readiness.py`
- `tests/test_runtime.py`
- `tests/test_exchange_market_scanner.py`
- `tests/test_exchange_connectivity.py`
- `.env.example`
- `VERSION.md`

## Update Protocol

After a coherent readiness milestone, update the current roadmap and the single
active PR/issue handoff with:

1. PR/commit reference and date.
2. Which blocker or stage item it addresses.
3. Tests run and their results.
4. New risks or regressions discovered.
5. Revised readiness verdict.

The verdict remains **NOT LIVE-READY** until measured evidence satisfies all mandatory gates.
