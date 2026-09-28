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

Any negative OOS or untouched-test segment makes `historical_validation=FAIL`,
regardless of a positive calibration aggregate. Empty declared segments and
noncanonical economic evidence fail closed.

Promotion evidence remains explicitly distinct:

- historical calibration: present only as historical research evidence;
- historical OOS: pass/fail from the separated holdout results;
- future PAPER: `REQUIRED_NOT_PROVIDED`;
- OOS replaces fresh PAPER: `false`;
- LIVE authorized: `false`.
