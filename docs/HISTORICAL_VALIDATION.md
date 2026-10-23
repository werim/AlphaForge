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
