# Storage admission evidence and first-measurement contract

## Verified boundary

Normal dynamic PAPER full replay is admitted by
`storage_policy.full_replay_duration_budget` and `disk_budget`, called by
`burnin_ops.preflight` before DB bootstrap. For duration T > 0, the first requires
an operator-supplied integer R > 0 and P0 + floor(T * R) < high, where P0 is the
existing main + WAL physical size. Equality fails. The reserve is
free_reserve + (high - low) + current_WAL + floor(T * R).

**There is no accepted measurement-file format, evidence verifier, automatic
rate estimator, or calibration launch mode in the current production gate.** A
positive integer is accepted computationally; that is not verification of its
origin or sufficiency. Do not describe a proposed packet as an existing canonical
certificate. An operator remains responsible for the rate's evidence and scope.

At R=0 the normal launch cannot collect its own initial measurement: preflight
returns STORAGE_GROWTH_BUDGET_UNMEASURED before starting the workload. This is a
bootstrap dependency, not a defect in that fail-closed gate. T=0, shorter duration,
fixed universe, stats-only, fake R or calling `create/start` directly would evade
the contract and are not calibration solutions.

The #636 harness is an independently bounded **offline diagnostic**, so it has
no dependency on successful normal launch. Both synthetic and recorded replay
remain admission=false/budget=null. Even a complete six-hour logical replay with
all adapters is not six hours of actual runtime. Its strongest result is an
empirical physical measurement of those canonical writer inputs on that host.
It cannot establish real scanner/decision payload distributions, concurrency,
periodic runtime writers, wall-time retention, authenticated reconciliation or
a future growth upper bound. Changing the provenance label cannot supply these.

## Existing evidence and target

#636 head b03ca222798b2426cd89e0352fe8c1a5a021a161 has successful exact-head
[Tests 39588901548](https://github.com/werim/AlphaForge/actions/runs/39588901548):
verification routing and routed PR tests succeeded; **Full regression suite was
SKIPPED**, as was dispatch-dedupe. No rerun was requested. dev at this inspection
is 8d92f678adbf4e8e43ebd40e1f148970f8217cef, the #635 merge; #636 is still open.
Neither result qualifies a subsequent source change.

The reviewed Mini target has full replay, dynamic universe, PAPER, LIVE=false,
1h/15m/1m and management shadow enabled. Its R=0 identities are:

| Identity | SHA-256 |
| --- | --- |
| config | 0c1969dac810bf7da465769636f5a1ea18fd3ba00961c30e0d4394903cdb0b5c |
| strategy | 031acf34b8c233867bbbc800128b4619a344418bb33c7907396d00b9e1ad059e |
| universe | 1832b565a3b446f3bdaa6dc75fe136ac9a38a082412ed9f25c4fe1361b14a1b0 |
| costs | 9f4ee93f794322ae236fd2b5eb7d768de5ffc55920345f43d8c6692665305504 |

These are a recorded target, not a new runtime observation. Recompute them on
the eventual measurement host. Policy high=1,073,741,824 B, low=734,003,200 B,
free_reserve=268,435,456 B, R=0, stats_only=false, archive_dir empty, T=21,600 s.
Base reserve is 608,174,080 B + WAL. Empty-DB arithmetic R < high/T is only a
necessary ceiling, never a measured rate. Source staging at 67bb0b5 is preserved;
new code and its eventual runtime fingerprint need their own full SHA.

Issue631 stats-only data has different retention, configuration and workload;
it cannot establish this full-replay rate. The old hdiutil failure remains
INCONCLUSIVE without its stderr. Do not reuse that launcher, approval or attempt.

## Proposed bounded real-runtime calibration contract

This is a **design for a distinct experiment**, not an implemented exception in
normal preflight and not authorization to execute it. A future implementation
must satisfy the complete contract below before exposing a runnable real-runtime
measurement command. Do not publish a fictional `calibrate live` command now.
The existing replay command below is executable, but has narrower scope.

1. **Purpose and authority.** A fresh `STORAGE_CALIBRATION_ONLY` lineage and DB,
   separate from ordinary PAPER admission, old campaigns and staging. Run the
   actual canonical dynamic scanner, decision/execution PAPER path, resolver,
   shadow, heartbeat, snapshots, maintenance and reconciliation. All normal
   source/config/non-LIVE/account/recovery/public-data conditions remain
   necessary; no credential or security-policy changes. The calibration capacity
   decision is based on independently verified finite physical containment,
   not a substitute positive rate. Ordinary launch continues to require R > 0.
   Calibration cannot produce qualification, readiness or auto-continuation;
   that exclusion must also apply to direct qualification, export and restart.
   No generic caller-selectable `skip_storage`, fake rate or hidden launch flag.
2. **Scope.** At least 21,600 contiguous wall seconds at the target scan,
   resolver, maintenance and snapshot cadences; dynamic **candidate set before
   selection**, selected symbols, payload bytes, decisions, pending rejects,
   positions, fills, all shadow actions, terminal boundaries and backlog counts
   must be observed. Preserve the 240-bar unresolved horizon; do not manufacture
   fills/terminal candles to reduce retained rows. An absent action is
   NOT_OBSERVED, not zero-cost proof. Record bursts, provider stalls, reader
   holds, checkpoint overlap and actual concurrent writers. If the six-hour
   window does not cover material writers/backlog or intended load, the result
   is insufficient; extending/repeating it requires a new bounded attempt.
3. **Isolation and physical bounds.** A separately provisioned, verifiably
   bounded filesystem/volume, with all mutable DB/WAL/SHM, archives, runtime
   artifacts, temporary files and logs contained or separately capped. Canonical
   maintenance, watermark, WAL settings and archive eligibility stay unchanged.
   Verify mount/backing identity, capacity, allocation and absence of protected
   data before reservation. A directory and sampled limits are not a hard quota.
   DMG is not a SQLite requirement; a quota or dedicated finite device is also
   possible. Sparse image virtual size alone does not prove host containment.
   Include backing allocation/metadata and archive-copy transients; refuse an
   unverified backend rather than estimating its overshoot.
4. **Budget.** Keep the 1 GiB DB+WAL high watermark and 700 MiB low watermark.
   Declare a finite independently enforced total volume cap C covering all
   writers, separate input bytes I, control/log cap L, provisioning/metadata
   bound M and maximum unallocated backing growth B. Before any preparation,
   host free must cover I+L+M+B+608,174,080 B plus the declared WAL/checkpoint
   reserve. Independently monitor the actual host backing filesystem, not just
   free space inside an image. Unknown C/I/L/M/B or inability to preserve reserve
   is BLOCKED. Numeric values must come from reviewed provisioning and workload;
   this document does not invent them. Abort before the applicable watermark or
   reserve is exhausted, using an enforced write/batch bound; a sampled observer
   alone cannot guarantee the reserve against unsampled writes. If the complete
   workload does not fit unchanged policy, report TARGET_UNATTAINABLE/INCOMPLETE.
5. **Identity and one attempt.** Before disk preparation atomically reserve and
   fsync an O_EXCL claim on a separate bounded control filesystem. Bind unique
   attempt, clean full source SHA/tree/module hashes, exact nonsecret canonical
   config/policy/strategy/universe/cost hashes, input/capture manifest hashes,
   actual interpreter binary/version/dependencies, SQLite/pragmas, host,
   filesystem/mount/backing, runtime instance/campaign/run and all resource/time
   limits. Credentials are excluded. Measure source before and after. Captured
   evidence must record origin, collection time, provider/authentication state
   and coverage; hashes bind bytes but do not authenticate their producer.
6. **Lifecycle and crash.** One supervisor owns one worker. No detached survivor,
   automatic retry, resume, recovery-clear or old attempt reuse. Preparation
   failure consumes the claim. Persist redacted bounded stdout/stderr, returncode,
   timeout, phase and failure reason outside the measured volume. Supervisor loss
   must stop the owned worker through an independently tested watchdog/lease;
   worker loss or missing final evidence is INCOMPLETE. Enforce wall deadline
   plus separately bounded shutdown grace. At end stop safely, preserve unresolved
   PAPER state and DB/WAL/SHM; do not backfill or force-close it. Never delete the
   claim, detach/delete an image or clean evidence automatically. Power/disk-full
   loss may prevent a final write: durable claim with no complete result is
   INCOMPLETE and cannot become a rate on restart.
7. **Measurement.** Persist time series of main/WAL/SHM, logical and allocated
   bytes, page/freelist/live allocation, archive and copy transients, all other
   artifacts, host free/reserve, WAL frames/busy/backlog, checkpoint requests and
   results. Keep pre-maintenance and post-maintenance peaks separately, with the
   actual sampling gap and independently enforced cap. Never subtract WAL
   truncation/reuse, archive deletion or freelist reuse from accumulated positive
   growth to make a favorable rate. An average is not an upper bound. Actual
   archive success is only credited for canonically eligible scopes; active
   protected evidence remains protected. Empty archive policy does not cover a
   future nonempty archive policy. Require integrity/FK checks and complete
   source/config/lineage closure.

A real calibration entrypoint therefore needs integration and focused tests for
ordinary R=0 rejection, containment admission, non-qualification in every reader,
all-path isolation, supervisor death, deadline, preparation race, resource abort,
missing completion and restart refusal. **That entrypoint does not exist in
#636 and is not added by this evidence-contract/watchdog change.** Running replay is not a
substitute. No actual measurement should be scheduled from this document alone.

## Evidence that can support the operator's admission decision

A complete real-runtime packet can support a narrowly scoped operator rate
review; it is not automatic PASS. Include raw series and writer/load coverage,
DB+WAL peak and retained growth, burst and unresolved-backlog envelope,
checkpoint/reader worst case, full artifact/archive/host budget, integrity and
failure evidence. Explain how the chosen conservative R covers the intended
six-hour workload and its unobserved variance; if that bound cannot be justified,
retain UNMEASURED/INCOMPLETE. A finite quiet period cannot guarantee unbounded
future market load. Unsupported scope must be constrained through a separately
reviewed policy or measured further, never silently assumed covered.

Once independently reviewed, changing R requires **new prospective config and
policy hashes**. Keep the R=0 measurement identity and verify that only the growth
budget differs, with strategy/universe/cost identities unchanged. Re-evaluate
canonical duration and disk budgets using actual prospective P0/WAL and T;
include SHM/archive/artifact/host reserves separately because the duration formula
only models DB+WAL. A numerical PASS is necessary, not a complete admission proof.
Fresh reconciliation/recovery and normal preflight remain separate prerequisites;
measurement does not authorize a PAPER start. Any source/config/policy/workload
change requires revalidation; old CI/fingerprints cannot be relabelled.

## One existing measurement command, deliberately narrower

The only implemented route available today is recorded **offline replay**. It
can obtain empirical canonical-writer disk/WAL/SHM/checkpoint measurements for a
reviewed captured input without invoking normal launch or its circular gate.
It cannot supply the real-runtime packet above. The following is an unexecuted
command, conditional on a new clean checkout of the intended harness SHA, a
host-bound reviewed plan and fresh absolute output path; never use Mini staging.
No qualifying recorded plan, enforced containment or real-runtime entrypoint is
claimed to exist. The command refuses a nonexistent plan, wrong hash or collision.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/src" \
  /path/to/verified-private-venv/bin/python -m alphaforge.storage_calibration run \
  --plan /absolute/fresh-control/recorded-plan.json \
  --plan-sha256 "$(shasum -a 256 /absolute/fresh-control/recorded-plan.json | awk '{print $1}')" \
  --output /absolute/isolated-filesystem/new-recorded-laboratory
```

Plan review must precede this command: kind RECORDED_REPLAY, source capture
manifest and representativeness, exact host/interpreter/filesystem/SHA and target
hashes, 21,600 logical seconds, all required adapters and explicit missing
writers. Existing harness bounds are 256 MiB input/100,000 events; the illustrative
limits are 600 wall seconds, 1 GiB sampled aggregate/per-file bound, 608,174,080 B
reserve and 64 KiB per log. They are diagnostic bounds, not a hard quota or
real-runtime limits. Parent checks free >= max_bytes+reserve, observes at ~50 ms,
kills only its owned child on timeout/resource failure, and preserves claim/result.
The original #636 worker had no parent-loss watchdog. This follow-up adds a
child-side liveness guard that exits with code 74 and attempts a SUPERVISOR_LOST
note after supervisor loss. This is not an OS-enforced death signal or hard
real-time guarantee; an unavailable/suspended Python watchdog cannot establish
physical containment. Missing parent result remains INCOMPLETE. The replay
command still cannot satisfy the real-runtime contract.
Missing/failed/partial output, sparse scope or unsampled peak is never admission.

The single next dependency is implementing/reviewing the distinct bounded
real-runtime contract above, including its physical-backend proof and crash
containment. It must be completed before a real measurement command can honestly
be offered. No gate was relaxed and no empirical/synthetic result was promoted.
