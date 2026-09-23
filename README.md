# AlphaForge

AlphaForge is an **execution-aware, regime-aware, evidence-driven trading research and PAPER execution platform**. Its operating objective is not trade count or raw win rate; it is **positive expectancy after real-world execution costs, capital preservation, and reproducible evidence**.

AlphaForge is not an indicator bot and it is not currently authorized for production LIVE trading.

## Core Principles

- **Capital preservation first.** No trade is preferable to an unjustified trade.
- **Fail closed on missing authority.** Missing execution, portfolio-risk, reconciliation, runtime, or identity evidence must not be converted into a passing default.
- **Execution-aware expectancy.** Spread, slippage, fees, funding, latency, liquidity, volatility and fill geometry can invalidate an otherwise attractive setup.
- **Regime awareness.** Signal quality is evaluated in market/MTF/regime context rather than from one isolated indicator.
- **Reject-first filtering.** Selectivity and explicit reject evidence are part of the edge.
- **Evidence integrity.** Decisions, lifecycle events, accepted trades, rejects, resolver outcomes, readiness and recovery are SQL-backed and scoped.
- **Deterministic and restart-safe behavior.** Runtime recovery, campaign continuation and finalized outcomes are designed to be reconstructable and idempotent.
- **Shadow is not authority.** State-direction shadow, adaptive calibration and agent traces are observational/diagnostic unless an explicit authoritative path says otherwise.

## Current Operating Modes

| Mode | Current role | Order mutation |
|---|---|---|
| **BACKTEST** | Historical/offline research and lifecycle/evidence generation. | No live exchange mutation. |
| **PAPER** | Authoritative simulated execution path with runtime, execution-safety, portfolio-risk, reconciliation and burn-in evidence. | Simulated only. |
| **LIVE_PRECHECK** | Non-mutating production-like precheck path. Requires measured execution evidence where the policy requires it and keeps a mutation trap active. | Submit/cancel/modify is forbidden. |
| **LIVE** | Enum/config surface retained for guarded future use. | **Currently blocked by the runtime safety boundary; not authorized for real orders.** |

The canonical mode setting is managed by [`src/alphaforge/config_registry.py`](src/alphaforge/config_registry.py). PAPER success, readiness evidence, or a permissive local flag does not by itself authorize LIVE trading.

## Decision Pipeline

The authoritative production-oriented PAPER/LIVE_PRECHECK path is, at a high level:

```text
market data
  -> MTF / regime / market context
  -> canonical candidate geometry
  -> expected fill and executable raw RR
  -> runtime/time/recovery safety checks
  -> deterministic scoring / AIBrain decision
  -> effective-RR and execution-safety gates
  -> portfolio-risk evaluation
  -> scoped decision + lifecycle evidence
  -> PAPER simulated execution OR LIVE_PRECHECK no-submit evidence
  -> accepted/rejected resolver
  -> qualification / readiness / audit
```

BACKTEST, PAPER and LIVE_PRECHECK still have separate orchestration/side-effect paths, but current `dev` includes a fail-closed `PreSubmitInvariant` contract that compares the protected decision semantics for the same frozen event: decision, primary reject reason, all failed gates, score, candidate RR, executable raw RR, effective RR, threshold provenance, execution-evidence status, portfolio decision and pre-submit terminal lifecycle state. P0-A/P0-B/P0-C/P1-A/P1-B from [#421](https://github.com/werim/AlphaForge/issues/421) are merged; remaining full-system verification work starts at P1-C.

## Execution-Aware Risk/Reward

AlphaForge distinguishes three prices:

- **planned entry** — strategy geometry before execution;
- **expected fill** — decision-time executable estimate used to re-evaluate entry geometry;
- **actual fill** — post-execution evidence, available only after a PAPER/real fill exists.

The canonical execution-cost contract is implemented in [`src/alphaforge/execution.py`](src/alphaforge/execution.py). Decision-time runtime geometry recalculates **executable raw RR from the expected fill**, stop and target. It then applies only the **remaining execution penalty**, avoiding double-counting entry slippage already embedded in the expected fill.

The execution-cost model can include spread, slippage, fees, funding, latency, liquidity and volatility penalties. [`src/alphaforge/effective_rr.py`](src/alphaforge/effective_rr.py) owns the shared execution-adjusted RR calculation; [`src/alphaforge/runtime.py`](src/alphaforge/runtime.py) owns the expected-fill geometry used by the production-oriented runtime path.

Actual fill evidence is recorded separately from the decision-time estimate so realized execution deviation can be audited without leaking future information into the original decision.

## Reject Engine

Rejects are explicit evidence, not a generic “no trade” bucket.

Current reject families include:

- scoring/expectancy gates such as `LOW_SCORE`, `LOW_P_WIN`, `LOW_CONFIDENCE` and `NEGATIVE_EXPECTANCY_AFTER_COSTS`;
- execution gates such as `EXECUTION_CONTEXT_UNAVAILABLE`, `INVALID_FAKE_ZERO`, `SPREAD_TOO_HIGH`, `SLIPPAGE_TOO_HIGH`, `HIGH_TOTAL_COST`, `THIN_LIQUIDITY`, `HIGH_LATENCY`, `EXCESSIVE_VOLATILITY`, `FUNDING_TOO_HIGH` and `LOW_EFFECTIVE_RR`;
- portfolio gates such as `MAX_DAILY_LOSS`, `MAX_ROLLING_DRAWDOWN`, daily trade limits, `CORRELATION_OVEREXPOSURE`, `LOSS_CLUSTER_ACTIVE` and `UNKNOWN_PORTFOLIO_RISK`;
- runtime/time gates such as invalid, stale or materially future market timestamps, kill-switch and recovery/reconciliation blockers.

The canonical execution-safety contract records both a **primary reject reason** and **`all_failed_gates` / failed-gate evidence** when multiple execution gates fail together. Thresholds and their observed values are persisted with the evidence; do not infer current thresholds from this README.

Supplied numeric execution evidence must be finite and non-boolean. NaN, infinities,
malformed numbers and booleans fail with `EXECUTION_CONTEXT_UNAVAILABLE`, even when
missing-context rejection is disabled. Invalid effective RR fails `LOW_EFFECTIVE_RR`
and remains unavailable in the safety result rather than becoming numeric zero.

## Evidence Model

AlphaForge treats SQL-backed evidence as the source of truth. CSVs, dashboards and reports are derived views.

The current evidence model includes:

- final decision and lifecycle evidence;
- reject evidence and pending reject-forward labels;
- accepted PAPER position/fill/outcome evidence;
- execution-context and execution-cost evidence;
- resolver outcomes for accepted and rejected decisions;
- campaign/run/release/runtime/mode identity and provenance;
- runtime heartbeat, recovery and reconciliation evidence;
- qualification/readiness and audit evidence.

Campaign/burn-in evidence uses explicit identity and provenance so stale or cross-campaign rows do not silently satisfy current qualification. Runtime/readiness surfaces additionally resolve the current mode, campaign, run, release and runtime instance where required.

Shadow/adaptive/agent evidence — including state-direction shadow — is not order authority and must not silently overwrite canonical decisions or thresholds. System Audit Fabric output is likewise audit/diagnostic evidence, not a replacement for canonical decision authority.

For read-only SQL inspection, [`docs/SQLcheat.md`](docs/SQLcheat.md) is the canonical operator reference.

## Resolver & Forward Outcome Semantics

[`src/alphaforge/burnin_resolver.py`](src/alphaforge/burnin_resolver.py) uses shared candle-window normalization for reject-forward and accepted PAPER outcome resolution.

Current invariants include:

- complete, ordered candle coverage is required before terminal classification;
- missing/gapped or partial windows remain pending/open for retry;
- malformed/conflicting candle evidence fails the window closed;
- future candles are excluded — no look-ahead;
- same-candle TP+SL is explicit ambiguous intrabar evidence, not a clean win/loss;
- finalized outcomes are not reclassified on retry;
- reject-quality attribution is authoritative only when the forward label is execution-aligned and otherwise eligible; legacy/shadow labels remain diagnostic.

## Runtime Safety

Current runtime safety mechanisms include:

- invalid, stale and materially future market-data timestamp checks;
- canonical execution-context validation and protected execution-cost gates;
- PAPER portfolio-risk state reconstructed from scoped persisted evidence where a campaign is attached;
- drawdown, daily-loss, loss-cluster, exposure, correlation, cooldown and trade-count controls;
- global kill switch;
- read-only exchange reconciliation and orphan/stale exposure detection;
- fail-closed runtime recovery after unclean/unknown state;
- provider-failure classification: only known transient transport failures receive recovery grace, while execution remains blocked until safe recovery evidence exists.

Do not hardcode operating thresholds in documentation or scripts. The typed source of truth is [`src/alphaforge/config_registry.py`](src/alphaforge/config_registry.py).

Managed float settings must be finite: NaN and infinities fail configuration
validation before range comparisons. The rule applies to environment values and
dashboard overrides; an invalid dashboard update leaves the existing file unchanged.

## Campaign / Run / Release Scoping

Burn-in/runtime attachment identity is intentionally stronger than a database path.

Current campaign attachment identity includes, as applicable:

- `campaign_id` and `burnin_run_id`;
- `release_id`;
- `execution_mode`;
- `config_hash`, `strategy_config_hash`, `universe_hash`;
- `git_commit`;
- `execution_cost_config_hash` for campaign/runtime parity;
- runtime instance identity on readiness/runtime evidence.

Continuation runs in the same campaign can contribute to restart-safe campaign evidence; unrelated campaigns must not contaminate current risk state or readiness.

## Readiness / Qualification

Readiness is an **evidence gate**, not a profitability guarantee and not LIVE authorization.

[`src/alphaforge/live_readiness.py`](src/alphaforge/live_readiness.py) evaluates scoped lifecycle, reject, execution, portfolio-risk, reconciliation, no-submit, rollback and operational evidence. Current runtime code still blocks real LIVE mutation even when non-mutating readiness evidence is strong.

The isolated [autonomous qualification harness](docs/AUTONOMOUS_QUALIFICATION_HARNESS.md) provides:

- **FAST** — deterministic accelerated PAPER qualification suitable for CI;
- **SOAK** — 6–24 hour qualification with the same fault/invariant schedule and optional public market-data probes.

A historical FAST/SOAK result is not proof for a newer commit. Fresh qualification
must be bound to the code/config under evaluation. [#421](https://github.com/werim/AlphaForge/issues/421)
remains the full-system verification tracker. Current `CHATGPT` includes the
P0-A/B/C, P1-A/B/C/D and P2-A regression families. P2-B has a focused mutation
gate, P2-C has seeded safety properties, and P2-D now replays two golden cases
through the production PAPER evidence chain. Remaining coverage and final release
qualification are tracked separately.

## Testing

The `Tests` workflow (`.github/workflows/test.yml`) runs on pushes to `CHATGPT`,
`dev`, and `main`, and on pull requests targeting `dev` or `main`. Qualify each
pushed commit using its exact SHA; an older or cancelled run is not evidence
for the current commit. The separate `Python application` workflow targets
`main` only and is not the CHATGPT qualification workflow.

The repository uses `pytest` for unit, persistence, integration-style and production-path regression coverage. Current tests include lifecycle/reject persistence, execution-cost semantics, production execution-safety gates, authoritative PAPER portfolio-risk state, accepted/rejected resolver integrity, invalid/stale/future market-time fail-closed behavior, protected BACKTEST/PAPER/LIVE_PRECHECK pre-submit semantic parity, reconciliation contention/recovery, and the autonomous FAST/SOAK qualification harness.

The [targeted safety mutation gate](docs/safety_mutation_testing.md) runs in the
`Tests` workflow. It verifies named production safety guards against disposable
source mutations and reports assertion-backed results for the current commit.
Seeded property tests cover geometry, partial fills, adverse costs, time bounds
and campaign/run/release identity with reproducible case sequences.

Not all desired full-system verification families are complete. [#421](https://github.com/werim/AlphaForge/issues/421) currently tracks:

- full-chain replay for the other golden scenarios;
- bounded load/performance qualification;
- fresh exact-head FAST plus public 6h SOAK release evidence.

The SQLite contention, real crash/restart, golden-scenario-pack, targeted
mutation and seeded property families have dedicated tests. The golden pack freezes scenario
expectations; profitable LONG and high-spread cases also have deterministic production-chain
replay, while the other scenarios remain P2-D work.

## Repository Navigation

| Area | Canonical entry points |
|---|---|
| Runtime orchestration | [`src/alphaforge/runtime.py`](src/alphaforge/runtime.py) |
| Execution context / cost semantics / execution safety | [`src/alphaforge/execution.py`](src/alphaforge/execution.py), [`src/alphaforge/effective_rr.py`](src/alphaforge/effective_rr.py) |
| Order/lifecycle decision helpers | [`src/alphaforge/order.py`](src/alphaforge/order.py) |
| Cross-surface protected decision invariant | [`src/alphaforge/decision_invariant.py`](src/alphaforge/decision_invariant.py) |
| Portfolio risk | [`src/alphaforge/portfolio_risk.py`](src/alphaforge/portfolio_risk.py) |
| Resolver | [`src/alphaforge/burnin_resolver.py`](src/alphaforge/burnin_resolver.py) |
| Qualification/readiness | [`src/alphaforge/burnin_qualification.py`](src/alphaforge/burnin_qualification.py), [`src/alphaforge/live_readiness.py`](src/alphaforge/live_readiness.py) |
| Recovery/reconciliation | [`src/alphaforge/runtime_state.py`](src/alphaforge/runtime_state.py), [`src/alphaforge/reconciliation.py`](src/alphaforge/reconciliation.py) |
| Typed config source of truth | [`src/alphaforge/config_registry.py`](src/alphaforge/config_registry.py) |
| BACKTEST CLI | [`backtest_order.py`](backtest_order.py) |
| Canonical SQL/operator queries | [`docs/SQLcheat.md`](docs/SQLcheat.md) |
| Lifecycle contract | [`docs/decision_lifecycle_contract.md`](docs/decision_lifecycle_contract.md) |
| FAST/SOAK qualification | [`docs/AUTONOMOUS_QUALIFICATION_HARNESS.md`](docs/AUTONOMOUS_QUALIFICATION_HARNESS.md) |
| LIVE-readiness roadmap / deeper context | [`docs/LIVE_READINESS_ROADMAP.md`](docs/LIVE_READINESS_ROADMAP.md) |

Historical implementation detail belongs in [`CHANGELOG.md`](CHANGELOG.md), [`REPORT.md`](REPORT.md) and [`VERSION.md`](VERSION.md), not in this overview.

## Quick Start

AlphaForge requires **Python 3.11+**.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

Windows PowerShell activation:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest -q
```

Environment profiles are purpose-specific: `.env.test.example` is for BACKTEST diagnostics, `.env.medium.example` is the balanced PAPER/default evaluation profile, and `.env.live.example` is hardened LIVE-readiness preparation without LIVE authorization. `.env.example` remains the canonical PAPER template. Keep real secrets local.

CI/offline-safe backtest smoke:

```bash
python backtest_order.py --ci --interval 1h --last-n-days 7 --symbols BTCUSDT --output-dir data/backtests/ci_smoke
```

`--ci` implies the deterministic offline path; it does not require live exchange mutation.

For PAPER/burn-in operational commands, environment selection, reconciliation and qualification workflows, use the linked runbooks/docs rather than bypassing the canonical configuration registry.

## Safety Notice

- AlphaForge is development/PAPER-oriented.
- Real LIVE trading is **not automatically authorized and is currently blocked by the runtime safety boundary**.
- Do not bypass configuration, execution-safety, portfolio-risk, reconciliation, readiness or kill-switch controls.
- Missing authoritative evidence must fail closed rather than be replaced with optimistic zeros/defaults.
- Historical backtest, PAPER or qualification performance does not guarantee future performance.
