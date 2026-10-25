# Real storage calibration and admission contract

## Authority and scope

`storage_measurement` implements a distinct **STORAGE_CALIBRATION_ONLY** experiment.
It uses `BurnInCampaignRunner`, the actual runtime factory/scanner/public provider,
canonical PAPER lifecycle writers, shadow/resolver and `StorageController`.
It does not replay synthetic/recorded events. Those remain diagnostics under
`storage_calibration` and always produce admission=false/budget=null.

Normal `burnin_ops.preflight`/launch still requires R > 0 and the original strict
DB+WAL duration/reserve calculations. It additionally consumes the reviewed real
measurement certificate through `launch --storage-evidence`. A positive number
alone no longer supplies measured evidence. Zero is still UNMEASURED; equality
with high still fails. No threshold, watermark, stats-only or LIVE gate changes.

Calibration has a separate typed capacity permit based on a verified finite
APFS quota and owned attempt, never a fake positive R. It runs the *same* common
preflight checks (source/config, reconciliation, providers, recovery and non-LIVE)
but returns CALIBRATION_READY, which ordinary launch does not accept. This
breaks the first-measurement dependency without opening ordinary admission.
The runtime runner also requires the owned permit for calibration lineage;
ordinary worker/start/resume cannot recover it after crash. Direct and automatic
qualification and completion are excluded; exported aggregate metrics identify
it as non-qualification evidence. No old campaign is resumed or reused.

The supported experiment is 21,600 actual wall seconds, dynamic universe,
1h/15m/1m, PAPER/non-LIVE, management shadow ON, full replay and R=0.
Canonical config/strategy/universe/cost/policy hashes, clean full SHA/tree/module
hashes, interpreter binary/version, dependency versions, SQLite, hashed hardware
UUID, filesystem identity, APFS volume/container UUID and quota are bound.
Feature checkouts use the existing source-fingerprint release token. Runtime
release and DB context is explicit and restored for in-process callers.

## One physical backend: preprovisioned APFS CapacityQuota

`storage_volume` accepts only a dedicated writable, mounted, role-free APFS volume
with **CapacityQuota exactly equal to the reviewed finite cap**. Unlimited quota,
wrong volume/device/container, unavailable hardware identity, nested mounts or
unsupported platform fail closed. It never provisions a volume/quota, creates or
attaches an image, or calls the old Issue631 launcher. UDRW/sparse images,
directories, sampled bytes and RLIMIT_FSIZE are not this backend.

Read-only geometry inspection is **not physical verification**. The separate
explicit `probe` entrypoint requires an otherwise unused dedicated volume (only
standard macOS metadata directories may exist). Before reserving a durable attempt it verifies that the canonical inner
`sandbox-exec` actually starts and completes its nonce-bound local-write /
outside-EPERM-or-EACCES challenge in the caller's execution context.
It writes several own bounded temporary fill files, with writes at most 8 MiB
and a native free-space check reserving the entire next write above host reserve,
until native ENOSPC/EDQUOT
within a 600-second checked probe deadline (not a hard real-time syscall bound),
with the backing container still above reserve. EFBIG, one-file exhaustion,
insufficient observed aggregate capacity, unknown state or other errors fail.
It removes only its own disposable fill bytes; claim/result and redacted tool
outputs remain. It never deletes evidence, an image or a volume. A used proof
attempt cannot automatically repeat. An explicitly authorized successor uses
`probe --attempt-id <new-id>`: the original terminal control remains at its exact
path/inode, is locked without content changes to serialize volume probes, and
its byte hash is bound into the new O_EXCL control/result. Named proof selection
uses the exact result hash; no mutable “latest” pointer overwrites old evidence.
Unknown files, symlinks, incomplete/cross-volume proof and ID reuse fail closed.
The first legacy attempt remains the required lock anchor. No automatic retry
is added. This destructive-to-free-space test needs
separate operational authorization; mocked tests do not count as a physical run.

`prepare` and `run` refuse absent, failed, stale, hash-mismatched or cross-scoped
physical proof. Proof lifetime is at most 24 hours; a new experiment must have
sufficient remaining lifetime for its full bounded window. Geometry and quota
are rechecked. Native CapacityInUse accounts for volume metadata/background use;
CapacityFree of the actual APFS container, not an image's virtual free space,
is monitored. Quota/filesystem drift aborts.

**Readiness is evidence-derived.** Implementation, geometry inspection and
sandbox/ENOSPC fixtures never imply a VERIFIED physical backend. Until fresh
actual capacity/sandbox proof exists on the target host, measurement is NOT_READY
and no operational measurement command should be supplied. Mutable host-access
and readiness observations belong in the active PR handoff, not this contract.

## Canonical host identity and compatibility

`storage_volume.host_identity()` is the single authority used by volume inspection
and therefore by both the fixture and real guardian/worker plan validation. It
runs `ioreg -rd1 -c IOPlatformExpertDevice -w0`, accepts one complete root object
and exactly one string-valued, canonical-shape `IOPlatformUUID`, and hashes that
exact string as UTF-8 with SHA-256. It does not trim the UUID value, change case,
reformat it or hash the surrounding serialization. Property order and external
spacing do not enter identity. Missing/duplicate/empty/malformed/truncated output,
command failure or the 15-second timeout fails closed, without fallback.

The old archive command used `ioreg -a`. [Apple's implementation](https://github.com/apple-oss-distributions/IOKitTools/blob/main/ioreg.tproj/ioreg.c)
opens `/dev/stdout` with CFWriteStream in archive mode; ordinary text output uses
the inherited stdout stream. This removes the archive file-open dependency while
leaving the existing write sandbox unchanged. A successful comparison on one host
alone is not proof that arbitrary text and plist output are equivalent: tests
cover the supported single UUID-string domain, case preservation, field order,
serialization and refusal cases. Unsupported shapes are rejected.

For valid legacy UUID strings the `host` field and its hash definition are
unchanged; no backend/schema version changes. Invalid legacy values are not
coerced into valid identities. No stored proof, plan or claim is rewritten.
Changed code still requires fresh exact-SHA physical proof and a new plan under
the existing source contract; old ENOSPC evidence is not new-code end-to-end PASS.
Read-only serialization fixtures, even on Mini, are not physical verification.

## Every mutable experiment output is quota-local

The worker and independent guardian inherit a macOS write sandbox: writes outside
the selected quota volume are denied (except /dev/null). Unexpected absolute DB,
log or temporary paths fail instead of escaping. Runtime cwd, injected primary
and agent-shadow DB paths, TMPDIR, archived data and traces are inside
`<volume>/experiment`; nonempty archive_dir must be exactly its archive child.
Run Python without bytecode writes; worker environment explicitly disables them.
Git optional index writes are disabled. Source/dependencies are read from a new
clean checkout, never the protected Mini staging/shared venv.

DB/WAL/SHM, archives/copy transients, samples, diagnostics, runtime logs, temporary
files, source-frozen input and results all share the **kernel aggregate quota**.
There is no automatic removal of DB/WAL/SHM or protected rows. The ordinary
1 GiB high / 700 MiB low policy stays unchanged for the reviewed Mini config.
A larger aggregate cap, if separately provisioned, does not raise that policy.

Claim, bounded stdout/stderr and final result occupy one O_EXCL, 0600 control
file on the same quota volume: four preallocated 1 MiB slots. Claim is fsynced
before further allocation/preparation. Slots have length/hash framing; short,
torn or failed writes cannot become favorable evidence. Logs capture at most
64 KiB each, are redacted and periodically persisted. A bounded CLI summary is
printed; caller-managed terminal capture is outside experiment ownership.
No unbounded host control/log file is created by the measurement.

Before work, backing-container free must cover the declared aggregate cap plus
canonical host reserve. For the recorded Mini R=0 policy, base reserve is
608,174,080 B + current WAL. No C value or growth rate is invented here.
The controller's high watermark, host reserve and backend quota are distinct
constraints. Failure to fit the full workload is INCOMPLETE/TARGET_UNATTAINABLE;
no shorter window, fake rate, policy relaxation or automatic retry follows.

## Lifetime, failure and observation

One supervisor owns one guardian, which owns one worker. The supervisor sends a
lease through a pipe; EOF, supervisor loss or five-second lease expiry kills the
owned worker. Worker parent-liveness and event-loop checks stop it after guardian
or child watchdog loss. Ordinary worker entry requires the original guardian/PID
and O_EXCL worker attempt. Guardian reentry and result overwrite also refuse.
Python liveness checks are not hard real-time death guarantees; **the kernel quota
and inherited write sandbox retain containment while Python is stalled**.

Wall limit is 21,600 + 600 startup + 60 shutdown seconds. The window is measured
from canonical OPERATING heartbeat timestamps, not process startup. At the window
boundary the canonical campaign is paused; unresolved PAPER state is preserved.
No resume/recovery-clear, synthetic close or qualification is performed.

Failures preserve redacted logs, returncode, phase/reason and consumed claim.
Missing result, disk-full publication failure, lost worker, stale lease or torn
control data is incomplete. If power loss prevents final evidence, the durable
claim does not authorize restart. No automatic cleanup/retry occurs.

Physical DB/WAL/SHM and total artifact series are sampled; native quota usage and
container free are recorded. Canonical maintenance is observed before/after;
actual PASSIVE/TRUNCATE checkpoint calls retain both reports and WAL peaks;
verified archive creation is sampled before source pruning. Normal retention,
archive eligibility and SQLite pragmas are unchanged. Integrity/FK/page data is
read once at final assessment, not expensively every second.

Sampled peaks are lower bounds, with the independently enforced quota as physical
containment. Positive DB+WAL deltas accumulate without subtracting checkpoint,
reuse or deletion. An average/observed rate is never called a universal bound.

## Evidence completeness and canonical consumption

Elapsed six hours or successful startup never means complete. Required evidence
includes real scanner source/cadence and candidate rows, observations, rejects,
positions and evidence-complete CLOSED positions, four shadow actions, snapshots,
clean read-only reconciliation, resolver work, actual controller/checkpoint work,
continuous healthy heartbeat coverage, integrity/FK success and no failed/deferred
provider/resolver or blocked storage evidence. Nonempty archive policy additionally
requires observed eligible archival. Unknown/missing surface is insufficient.
Shadow eligibility and candidate cardinality are reported; observed coverage does
not establish all possible future payload distributions/concurrency.

A structurally complete run is MEASURED and still admission=false. **Review is
separate**: an explicit operator rate and scoped representation/bound justification
are required. Rate below observed accumulated positive growth or unable to fit
unchanged policy is rejected. This necessary floor does not calculate a safe rate
or verify the human justification. Quiet/unrepresentative evidence must remain
unaccepted; the operator remains responsible for conservative intended-load scope.

The reviewed certificate says OPERATOR_SCOPED_EMPIRICAL_BUDGET and explicitly
universal_upper_bound=false. It binds immutable artifact hashes and result hash,
measurement SHA/config, prospective R/config hashes, backend/environment, duration
and expiry (no later than the earliest proof/plan/result limit). It authorizes
only a fresh DB on the measured host/quota filesystem, same source/economic
configuration and up to the measured six-hour duration. Retained calibration files
still consume free space. The normal canonical disk/duration budget is recomputed;
ordinary source/recovery/reconciliation/provider gates remain mandatory.

Wrong SHA/config/intervals/host/filesystem, dependency drift, stale/tampered data,
synthetic/failed/partial measurement, existing DB or insufficient reserve rejects
before normal DB bootstrap. Only changing the operator R creates prospective
config/policy identity; strategy/universe/cost hashes remain bound. A certificate
cannot be relabelled for new source or another filesystem. This is bounded scoped
evidence, not G6/G7 qualification, LIVE readiness or launch authorization.

Economic config hashes alone do not identify the storage workload. A review
certificate also requires `prospective_workload_hash`, computed from the typed
nonsecret configuration with the reviewed prospective R. Scan, heartbeat,
reconciliation, maintenance, provider and other effective settings must match
exactly; missing or forged bindings fail closed. Secret fields are excluded by
the canonical registry/secret-name contract; fresh primary DB routing is checked
separately. The typed calibration permit likewise compares the complete profile, allowing only the canonical quota-local agent DB routing
override used by its launcher. For ordinary admission, an enabled agent database
must also be a distinct fresh SQLite file on the measured backend; an external,
shared, existing or in-memory target cannot inherit the primary DB's proof.

## Operator dependency

CLI stages are inspect -> separately authorized physical probe -> prepare ->
separately authorized real run -> independent review -> normal admission with
storage-evidence. Each stage preserves its own irreversible attempt evidence.
Inspect is read-only and reports UNVERIFIED without a proof. Offline/temporary fixtures never constitute these operational stages.

Operational prerequisite: an actual preprovisioned dedicated quota volume with
successful physical capacity/sandbox proof for the intended code/environment.
If no such volume exists, provisioning needs separate authorization; the
measurement entrypoint will not create one. Do not supply an execution command
while that mandatory prerequisite is unverified. Old Issue631 approval, launcher, image failures and stats-only
measurements cannot supply it; the historical hdiutil failure remains INCONCLUSIVE.


## Sandbox execution preflight and retest readiness

The canonical `sandbox_preflight` authority in `storage_volume` runs the unchanged
profile through `/usr/bin/sandbox-exec` using the current interpreter. A structured,
fresh nonce response, successful child exit, matching owned local sentinel and
explicit outside EPERM/EACCES are all required. Nonzero exit after creating the
local sentinel, malformed/contradictory output, missing binary, permission-denied
spawn, timeout, interruption, missing sentinels or unexpected outside creation
fail closed. Diagnostics retain bounded redacted stdout/stderr, reason, returncode,
source SHA and profile/script identity. Only matching own challenge files are
removed; unexpected objects remain for inspection.

The existing measurement CLI exposes `sandbox-preflight` for this non-capacity
check. It writes only tiny temporary challenge files, no claim or fill, and
reports physical acceptance NOT_RUN and admission=false. It must run in the
**actual final caller/worker context before the operator's O_EXCL operation latch**.
A parent, sibling or outer sandbox's denial is not evidence of inner execution.
`probe` repeats the same canonical guard before its own O_EXCL control and before
any fill. CLI failure returns 3 with the diagnostics and physical_attempt_started=false.
That does not reset or refund any operator latch already consumed outside this API.

Do not place the canonical physical probe under an additional sandbox wrapper
without verifying that its inner sandbox invocation is supported in that exact
context. The failed operator wrapper from 24 October 2026 invoked sandbox-exec
around a worker that called `probe`, causing the inner sandbox_apply to fail.
The old outer local-write/denial PASS cannot substitute for canonical acceptance.
Keep this historical harness/evidence immutable; do not rerun or edit it.
The real calibration guardian/worker sandbox remains unchanged by this correction.

Persisted physical proofs now also require the structured canonical sandbox
verification record. Old booleans or ENOSPC alone are insufficient; no historical
control, source SHA or failed result is rewritten or upgraded. O_EXCL and original
control-inode locking still prevent attempt reuse. A failed consumed attempt
remains permanently non-admissible.

Revised plan (documentation only; **no retry authorization**):

1. Preserve old and failed-new volumes, controls, operation latch, logs, hashes and
   inode identities. Recheck them read-only; no deletion, quota change or reuse.
2. Qualify a clean exact new PR head through software CI. New source makes old
   physical proof cross-scoped. PR #637 stays unmerged until physical acceptance.
3. Obtain fresh explicit authorization for one fresh attempt and, if necessary,
   separately authorized dedicated finite-quota provisioning. Never infer either
   permission from software PASS or from the previous consumed authorization.
4. Before consuming a fresh operation ID, run the canonical non-capacity preflight
   in the final worker context. Require strict success, matching source/profile,
   dedicated empty backend identity, finite quota, native reserve, unique ID,
   single ownership and an armed independently owned deadline watchdog. Unknown
   or contradictory evidence stops before fill; it grants no automatic retry.
5. Only within that new authorized scope may canonical capacity probing run.
   Retain complete structured sandbox and aggregate ENOSPC evidence; a bounded
   Python check is not an OS hard-timeout guarantee. Review all required physical
   controls before any subsequent separately authorized runtime stage.

SQLite/WAL/archive containment, six-hour PAPER calibration, seven-day qualification,
LIVE and merging #637 remain separate blocked stages. This software guard neither
proves those physical contracts nor authorizes their execution.
