# Historical validation evidence contract

Historical strategy evidence is admissible only when it is produced through the
fail-closed contract in `alphaforge.walk_forward`. It is research evidence; it
does not authorize LIVE and it does not replace a fresh PAPER qualification.

## Segment roles and provenance

Each chronological, non-overlapping segment has exactly one role:

- `TRAIN_CALIBRATION` supplies fitting or calibration inputs;
- `VALIDATION_OOS` evaluates the frozen result out of sample;
- `UNTOUCHED_TEST` is an optional final holdout.

Every segment records a full git commit SHA, strategy/config hashes, data hash,
window identity, universe identity, role, freeze time, and explicit calibration
source segment IDs. An OOS or test segment may reference only earlier calibration
segments. Each report carries the complete immutable segment records and their
deterministic contract identity.

Every OOS/test segment also records search/selection lineage: lineage identity,
candidate variant identities, evaluation count, whether the evidence influenced
selection, and whether it is declared fresh. Missing lineage fails closed.
Evidence reused across variants or used to select a model/threshold is retained
as research evidence but reported as `INADMISSIBLE_REUSED_OOS`, never as an
unqualified historical pass. `UNTOUCHED_TEST` requires exactly one evaluation,
one candidate variant, no selection influence, and an explicit fresh declaration.

`WalkForwardContract.calibrate` requires a target OOS/test segment and passes the
calibrator only that target's declared calibration rows. Evaluation rows and
later rows therefore cannot enter the fitted values through this API. Config
payloads are hashed when each segment is frozen, before its window starts.

## Universe contract

Use `UniverseProvenance.point_in_time` only with timestamped membership evidence.
It validates each row against membership at that row's timestamp. If that data is
not available, use `UniverseProvenance.fixed` with a stable source identity. A
fixed universe is reported with `survivorship_bias_protected=false`; it cannot
silently claim point-in-time protection.

## Economic and reporting contract

Rows must carry canonical pre-submit candidate RR, executable raw RR, remaining
execution penalty, effective RR, RR basis, and execution-cost semantics. The
report reuses the production semantic invariant validator and canonical cost
reference, denominator, sign, and fee-treatment constants. It does not implement
OOS-specific costs or recalculate a more favorable RR.

Reports retain row-level evidence and separate aggregates by:

- role, window, and segment;
- role and symbol;
- role and regime.

Reports use `walk_forward_validation_v2`. Each row retains
`position_net_return_pct` (net cash / filled notional × 100), `net_r` (net cash /
initial executable dollar risk), and `net_pnl_usdt`. These units are distinct.
The adapter reconciles persisted fill, stop, quantity, exit, cost components and
risk using the shared `realized_accounting` authority; it does not replace that
ledger. Aggregates sum only validated realized USDT cash. Percentages and R are
never summed or averaged as portfolio performance. The old aggregate
`net_return` field is explicitly null, rather than silently acquiring new units.

Any negative realized-cash OOS or untouched-test segment makes
`historical_validation=FAIL`, regardless of calibration profits. Positive or
zero cash is `INCOMPLETE`: existing exports do not establish attributable
window opening capital, complete capital flows, concurrent/open-position marked
equity, complete closure population, or matching run/release/config snapshots.
A frozen segment declaration and a CSV digest do not supply that missing runtime
provenance. `capital_return_pct`, `equity_curve` and `max_drawdown_pct` remain
null for every group/window. No balance literal, position notional, initial risk,
manifest claim or common denominator is substituted. This deliberately removes
the former positive historical PASS until an attributable capital authority is
available. Different windows cannot inherit one another's capital.

Legacy percentage-only rows expose unknown cash, not zero. Empty segments,
nonfinite/inconsistent economics, missing required costs, mixed run/profile/cost
source identities, duplicate position realization and holdings crossing frozen
segment boundaries fail closed. Search-selection inadmissibility retains
precedence. Historical reports and campaign outcomes are not rewritten.

Promotion evidence remains explicitly distinct:

- historical calibration: present only as historical research evidence;
- historical OOS: FAIL, INCOMPLETE, or INADMISSIBLE_REUSED_OOS from separated evidence;
- future PAPER: `REQUIRED_NOT_PROVIDED`;
- OOS replaces fresh PAPER: `false`;
- LIVE authorized: `false`.

## Existing BACKTEST evidence adapter

`alphaforge.historical_validation` reads the canonical `decision_evidence.csv`
artifact offline. Its manifest carries the complete contract plus the exact CSV
SHA256 and minimum effective-RR reporting threshold. The adapter accepts terminal
BACKTEST outcomes only and requires persisted candidate RR, executable raw RR,
remaining execution penalty, effective RR, RR basis, and execution-cost semantics.
Legacy or incomplete economics fail closed; the adapter never reconstructs them.

Current BACKTEST decisions obtain those RR-stage values at decision time from
`build_decision_rr_metrics`, the same expected-fill geometry and residual-cost
authority used by the runtime. The values are carried through the stable
signal/lifecycle identity into terminal `decision_evidence` rows and the CSV
export. The historical adapter only reads them; it does not recalculate or
upgrade planned-entry legacy evidence.

Generate a machine-readable report with:

```bash
python -m alphaforge.historical_validation \
  --manifest /path/to/walk_forward_manifest.json \
  --output /path/to/walk_forward_report.json
```

The manifest schema is `walk_forward_backtest_adapter_v1`. Artifact paths are
resolved relative to the manifest, and the CSV content must match the declared
SHA256. This command does not run a backtest, tune parameters, start PAPER, or
authorize LIVE.


The input manifest remains `walk_forward_backtest_adapter_v1` for compatibility;
reading it produces a version-2 report and never upgrades old evidence. Current
SQL exports already carry the required cash/cost/size facts in typed fields and
`diagnostics_json`; no database schema or trading behavior changes are required.
Closure timestamps are persisted epoch milliseconds; row timestamps are normalized
to UTC. Holding duration must be nonnegative and cannot reach into another frozen
segment. Partial/mixed lifecycle evidence is not qualified through this adapter.

## Historical capital evidence: producer and consumer contract

**Design status: NOT_IMPLEMENTED.** This section defines the missing input
contract; it does not add an accepted manifest field, a capital ledger, a schema,
a producer or a historical PASS path. Current version-2 reports retain null
capital return/equity/drawdown and INCOMPLETE capital evidence. Unknown manifest
fields are not evidence. A correct CSV digest proves bytes, not population
completeness or the truth of a capital assertion.

### Current authority and evidence gap

| Required fact | Existing canonical authority | Persisted provenance | Gap | Smallest safe action |
| --- | --- | --- | --- | --- |
| Window opening capital | `runtime.RuntimeOrchestrator._paper_portfolio_risk_state`; `portfolio_risk` | Decision-time equity/available-balance fields in `decision_evidence`; runtime config | Configured PAPER initial equity is a sizing/risk seed, not an attributable opening event at every historical window | Require a source-bound opening valuation and its capital origin; never adopt a balance literal |
| External capital flows | No producer in the inspected historical/accounting/risk path | No exhaustive flow stream or completeness watermark in the consumed export | Missing flows cannot mean zero; an empty list cannot establish no deposits/withdrawals | Require a reconciled flow population, including attributable evidence for a zero-flow interval |
| Concurrent/open-position marked equity | Runtime risk reconstruction and exposure snapshots | Campaign outcomes, pending positions, portfolio risk fields, drawdown events | Risk equity sums completed realized outcomes onto a configured seed; it does not mark every open position or establish historical valuation coverage | Define complete boundary/as-of valuations over the full position inventory before consumption |
| Complete realized closure population | `realized_accounting`; BACKTEST lifecycle SQL and export | `decision_evidence` run/position/lifecycle IDs, cost-complete closure diagnostics; PAPER outcome tables | Canonical rows prove individual closures; a selected export can omit another closure even with a valid digest | Reconcile the exported closure ID set against the authoritative scoped inventory in a consistent snapshot |
| Campaign/run/release/config/strategy/universe/source | `burnin_runs`, campaign/run lineage; frozen `walk_forward.Segment`; cost provenance | These authorities retain identities separately; BACKTEST export retains run/profile/position/cost facts | Segment declarations do not authenticate the producing runtime or bind an absent capital series | Require an explicit verified mapping to producer records; do not copy current identities into historical gaps |
| Time boundaries and valuation completeness | UTC segment/holding checks and persisted closure timestamps | Closure times and derived holding interval, segment boundaries | No opening/closing marks, exhaustive valuation series or source watermark for capital | Require time-complete source evidence and consistent as-of snapshots; future or missing values remain unavailable |

`burnin_drawdown_events` contains risk peak/trough aggregates, not an exhaustive
marked-equity series. PAPER risk reconstruction deliberately includes campaign
continuations; that is valid for its risk purpose but does not certify a frozen
historical window. Decision-time risk snapshots, exposure counts, notional,
initial stop risk, percentages and manifest declarations cannot fill these gaps.
Existing protection against mixed cash runs/profiles/cost sources is necessary
but not a full capital-to-runtime provenance binding.

### Required typed inputs for a future producer

The following are semantic requirements, **not a currently supported JSON API**.
A future implementation must define a versioned strict schema at its existing
persistence boundary, refusing coercion of booleans, nonfinite money, ambiguous
times, missing IDs and unsupported currency/product semantics.

- **Scope:** immutable source snapshot/digest and producer version; account or
  simulated-capital scope; execution mode; source SHA; campaign/release and exact
  allowed run IDs; config/strategy/universe/execution-cost identities; window and
  segment IDs; settlement currency and units. BACKTEST needs an explicit
  source-bound equivalent of runtime lineage; PAPER identities cannot be silently
  fabricated for it. Segment identities must match producer evidence, not merely
  be internally consistent declarations.
- **Opening/closing valuation:** unique persisted event ID, UTC effective and
  observed timestamps, source provenance, settled cash, position inventory and
  equity components, plus the attribution of opening capital to the producing
  account/simulation initialization and earlier reconciled events. The opening
  valuation must exist at the actual window boundary; a later balance is not a
  substitute.
- **Flows:** unique source event ID, signed settlement-currency amount, effective
  and observed timestamps, kind and origin. Supply independently reconciled
  coverage/high-watermark evidence for the entire interval, including explicit
  zero-flow coverage. Costs, realized PnL and position transfers are not deposits.
- **Marks:** each valuation references every open position's stable ID, side,
  remaining quantity, entry/cost state and source-bound mark. Quote effective
  time must not exceed valuation time; availability must not exceed the report's
  as-of cutoff. Enforce the existing applicable configured freshness bound and
  record its provenance. Missing/stale/cross-scoped marks invalidate that
  valuation; do not carry forward a favorable value or infer flat exposure.
- **Closure inventory:** authoritative entry/open/partial/closed position sets,
  terminal lifecycle IDs and source snapshot watermark, with exact reconciliation
  to consumed closure IDs. Replayed IDs cannot realize cash twice. A CSV count or
  the producer's unchecked `complete=true` cannot prove an exhaustive set.
- **Coverage:** explicit UTC start/end/as-of cutoff, boundary ordering and
  valuation schedule, source watermarks and gap diagnostics; consistent snapshot
  identity across flows, position inventory, closures and valuations. No torn
  reads, inferred calendar coverage or marks borrowed from a different run.

Source records must be persisted by the producing simulation/accounting path at
the time the facts are known. Historical reports/campaigns are immutable: absence
of a producer cannot be repaired by adding today's config, synthetic flows or
invented marks to yesterday's evidence.

### Consumer validation and arithmetic ownership

Before admitting capital metrics, the existing historical adapter/report must:

1. Validate typed inputs, source integrity, identity mapping, snapshot consistency,
   chronological ordering and the half-open segment interval `[start, end)`.
   Opening inventory is the state immediately before interval events; the closing
   boundary is the state after all events strictly before `end`. Tie ordering must
   be attributable to source sequence; reject ambiguous boundary valuations.
   Opening exposure must retain earlier entry provenance without importing that
   entry's realized result into another segment. This does not relax the current
   adapter's prohibition on holdings crossing frozen segments.
2. Reconcile the full closure and open-position populations. Reuse
   `realized_accounting.position_cash_geometry` and `realized_cash_ledger` through
   the existing closure validator. Use each cost exactly once; never subtract an
   additional modeled cost from already-net realized cash. Do not build a second
   historical cost/realization implementation.
3. Reconcile settled cash changes to source flows and canonical realized cash,
   then validate producer equity components against the complete position marks.
   Cash agreement alone cannot establish equity, capital return or drawdown.
   Partial exits, funding and concurrent positions require source-complete
   accounting; a closed-trade-only curve cannot stand in for marked equity.
4. Admit only an explicitly defined metric scope. The smallest proposed initial
   scope is one homogeneous settlement-currency portfolio/window with proven
   zero external flows and complete attributable boundary/marked valuations.
   Nonzero flows, cross-currency or unsupported instruments remain unavailable
   until a separately tested flow-adjustment/valuation method is owned by the
   canonical accounting path. No arbitrary denominator is introduced here.
5. Report each metric's actual valuation coverage. Even a complete sampled
   equity series cannot claim an intrainterval worst drawdown. Symbol/regime
   summaries must not inherit the whole portfolio's denominator or drawdown.
   A scalar net-cash sign is not a capital performance or readiness PASS.
6. Preserve existing verdict precedence: inadmissible search/OOS reuse first,
   then negative admissible complete segment cash FAIL; otherwise current
   INCOMPLETE. Validated capital metrics would still not waive any strategy,
   economic, PAPER or release requirement. Legacy percentage-only rows remain
   diagnostic and cannot inherit money or capital evidence.

### Acceptance and next executable dependency

A future producer/consumer integration must cover missing opening capital, flow
coverage, marks and closure inventory; cross-window/run/config/source inputs;
nonfinite cash, duplicate realization and double-counted costs; inconsistent cash;
concurrent/partial exposure; stale/future marks; and torn/as-of snapshots. Test
both persistence and consumption, including crash/restart/idempotence. Positive
cash without complete capital evidence remains INCOMPLETE; negative cash and
search inadmissibility retain their current precedence. Rehashing a subset export
or declaring a complete manifest cannot certify its omitted population.

**Next dependency:** define and implement source-bound opening/flow/position-mark
and population-coverage production in the existing BACKTEST simulation/accounting
path, starting with a fully recorded, homogeneous zero-flow window. First prove
that its actual source can persist the facts above; then add the strict schema
and transactional persistence plus an adapter to the existing report. Do not
build a dormant ledger before the source and valuation semantics exist. No Mini
is needed for that software task, but it is not solved by current risk snapshots
or available closure exports. Actual historical data remains unavailable until
such a producer supplies attributable records. #344 and subsequent learning,
physical acceptance and LIVE authorization remain independent requirements.
