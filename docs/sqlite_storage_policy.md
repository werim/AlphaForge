# Bounded SQLite storage (#620)

The storage authority is `alphaforge.storage_policy`. All settings are typed in
`config_registry.py`, frozen in runtime config, and included in new campaign
config hashes. Changing the policy requires a new campaign identity; existing
campaigns do not silently inherit new retention semantics.

## Units, thresholds and operation

Byte settings use bytes, with binary defaults: high 1 GiB, low 700 MiB. The
physical trigger includes the main DB and WAL. SHM is reported separately.
Deleting rows creates reusable pages; it does not promise a smaller OS file.
Low-water attainment means allocated live pages are within the low budget.
`REUSABLE_CAPACITY` explicitly reports a physically large but reusable DB;
maintenance does not keep deleting because its filename still exceeds 1 GiB.

The default policy checks every 60 seconds, retains at least seven days of
telemetry, and removes at most 1,000 rows in a 0.2-second transaction budget.
A cycle whose complete ownership graph exceeds the row budget stays protected;
the operator can increase the budget deliberately. Archive backup is separately
bounded to 30 seconds; it never holds the SQLite writer lock while copying,
hashing or checking integrity. A cross-process archive file lock prevents two
publishers from overwriting the same partial or verified snapshot.

Read-only inventory:

```bash
PYTHONPATH=src python -m alphaforge.storage_policy inventory /absolute/path/runtime.db
```

This reports DB path/device/inode, DB/WAL/SHM bytes, page counts, reusable/live
allocation, dbstat table/index allocations where available, oldest timestamps,
rows, eligibility/preservation reasons, declared campaign artifacts, DB-owned
backup sidecars and durable archive locations. Artifact inventory is bounded;
missing locations and truncated measurements are explicit. Inventory/status
never starts cleanup, checkpoints or qualification materialization.

## Dependency and eligibility rules

Automatically dispensable telemetry is limited to aged healthy **standalone**
heartbeats with a later equivalent healthy state, no position/order exposure,
no foreign-key consumers and no campaign-bound runtime instance. The latest
heartbeat and degraded/recovery states remain. State snapshots, reconciliation,
health transitions, incidents, decisions, outcomes and qualification evidence
remain protected. A database containing qualification/audit dependencies is
conservatively protected until the graph can prove removal safe.

For terminal universe evidence, only COMPLETED campaigns older than the minimum
age can be eligible. Failed/unfinished campaigns, workers, ambiguous run links,
nonterminal or open runs, unresolved forward labels/positions, outstanding
positions/orders/incidents, qualifications/audits, shared selector cycles,
unknown selector dependencies, or missing clean shutdown evidence block
archival/removal. Normal selection/ranking, costs, PnL and risk thresholds are
unchanged.

A full WAL-aware SQLite backup preserves canonical release/run/SHA/config
identity and every source table. Integrity, foreign keys, row counts, schema
hash and campaign domain identity are verified. A fsynced JSON manifest and a
second read-only verification precede durable `storage_archives` publication.
No deletion is authorized by a partial file, an unpublished manifest, a missing
archive or a checksum/count/schema/domain mismatch.

Source cleanup removes only eligible universe cycles, candidates and their
campaign links. Other canonical campaign evidence remains operational. Every
batch rechecks live dependencies inside `BEGIN IMMEDIATE`, verifies exact source
rows against the immutable archive, and removes links before child/parent rows.
Ordinary UPDATE remains forbidden. Ordinary DELETE remains forbidden. A
transactional schema migration changes only the delete guards to permit exact
cycle/hash/campaign capabilities. Capability insertion requires a temporary
connection function and unpredictable token; capabilities exist only within the
removal transaction and disappear on commit/rollback. The function is revoked
in `finally`. Archive work does not disable normal writer immutability.

Crash before the manifest leaves only unpublished work and no deletion. Crash
after verification preserves the durable archive for the next attempt. Crash
during removal rolls back the whole batch. Restart uses the same verified
archive; no previous cycle is deleted twice. Completed publication is never
overwritten merely to regenerate a manifest.

Set `ALPHAFORGE_STORAGE_ARCHIVE_DIR` to an existing cold-storage directory with
sufficient capacity. No configured safe destination means protected evidence
stays and a concrete blocker is reported. A full snapshot can be much larger
than the rows reclaimed. Same-device archives are accounted separately and do
not create host free space; capacity checks include DB size and reserve. No
archive or backup is silently deleted to meet a target.

To locate replay evidence:

```bash
PYTHONPATH=src python -m alphaforge.storage_policy replay-location /absolute/path/runtime.db --campaign-id camp_...
```

The read-only locator verifies the archive and returns its path/checksum, or an
explicit unavailable reason. Run existing read-only replay/report tooling
against that archive path. Source readiness and dynamic reject-label consumers
report archived evidence as requiring replay; they cannot turn a partially
removed scope into favorable empty evidence. Campaign resume, runtime attachment
and qualification materialization refuse an archived operational scope.

## Disk pressure and WAL

Startup reserve is the configured free reserve (256 MiB), high-minus-low growth
headroom, current WAL bytes, plus configured growth-budget bytes/second times
known duration. The growth budget defaults to zero: it is an operator budget,
not a fabricated measured growth rate. The runtime also checks periodically.
The old fixed 100 MiB preflight threshold is replaced by this shared authority.

Maintenance uses canonical writer arbitration and bounded fresh-connection
BUSY retries. WAL, busy_timeout=30000, synchronous=NORMAL and foreign_keys=ON
remain intact. PASSIVE checkpoints report frames, progress and readers retaining
backlog. When no reader blocks progress, a zero-wait SQLite TRUNCATE checkpoint
can reclaim WAL capacity; its busy timeout is restored afterward. No code unlinks
WAL/SHM. No online VACUUM is performed. Reclaiming OS
space requires separately capacity-checked offline compaction or storage
provisioning; reusable-page success is never reported as physical shrinkage.

If reserve is insufficient or protected live data prevents reclaiming the
budget, new evidence-producing scans and entries stop. The runtime records a
recovery-required transition before exhaustion. With open positions/orders,
position management and reconciliation stay alive and exposure is preserved.
An empty, reconciled campaign requests normal supervised shutdown only after its
recovery state is durably written. Unknown exposure keeps supervision alive.
If reserve is exhausted despite the gate, SQLite FULL is a concrete maintenance
blocker. Missing mandatory recovery persistence remains explicit and incomplete;
it cannot authorize shutdown or favorable evidence. The state write runs off the
event loop so position management can keep operating. Removing pressure does not automatically clear recovery
or reopen execution. Unknown/nontransient persistence errors remain fatal.

A controller holds one current diagnostic report; it creates no append-only
maintenance table. Reports distinguish threshold, logical target, disk reserve,
rows/batches, verified archives, checkpoint backlog, protected scope and next
action. PAPER/FAST/SOAK acceptance remains a separate gated workflow.

## Deterministic storage evidence

The terminal three-cycle fixture removed nine source rows after verified replay.
Allocated live bytes changed from 790,528 to 716,800; reusable bytes changed from
0 to 94,208. These measurements include the added archival manifest/schema
overhead and demonstrate capacity reuse, not host-scale savings or a GB/day
forecast. The test prints its current before/after measurements for review.
