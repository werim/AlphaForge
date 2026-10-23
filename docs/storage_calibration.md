# Offline full-replay storage calibration harness

`python -m alphaforge.storage_calibration` is a diagnostic laboratory replay,
not PAPER admission, an execution runtime, or an automatic growth-budget setter.
It never changes `STORAGE_GROWTH_BUDGET_BYTES_PER_SEC` or the storage gates.
A completed synthetic fixture is not a future workload upper bound. Both
synthetic and recorded replay results always say `admission_eligible=false` and
`recommended_growth_budget_bytes_per_sec=null`.

The current canonical contract (`StoragePolicy`, `full_replay_duration_budget`,
`disk_budget`) accepts a positive **operator** rate, not a verified replay
certificate. It does not establish when synthetic evidence bounds future market
load. Consequently this implementation does not invent that authority or solve
an unmeasured budget by writing a favorable number. Representative empirical
coverage and an independently justified conservative bound remain necessary.

## Workload and production semantics

A versioned JSON plan binds full clean code SHA/tree and module hashes, canonical
config/strategy/universe/cost hashes, policy hash, workload hash/provenance,
interpreter binary/version, SQLite version, host hash and filesystem device/fsid
and block sizes. Environment drift, source edits (including during replay),
wrong filesystem, missing provenance, mismatched hashes, duplicate event IDs,
nonmonotonic/nonfinite times or an incomplete declared window fail closed.
The environment is explicit nonsecret canonical config; dotenv and credentials
are not loaded. PAPER, non-LIVE, dynamic universe, 1h/15m/1m, management shadow ON,
full replay and storage enabled are mandatory. Registry aliases are resolved by
the existing config loader, not an alternative config authority.

Events have `id`, `kind`, operation-time `at` seconds, and `data`. The declared
start timestamp and window are explicit. Supported adapters:

| kind | Canonical authority |
| --- | --- |
| universe | build_selected_universe; persist_universe_selection/link |
| observation | persist_burnin_observation, scope forced to the isolated lab |
| reject | persist_pending_reject_label; canonical reject ownership required |
| position | persist_pending_position |
| shadow | PaperPositionManagementShadowProvider using stale_market_data_sec |
| resolve | resolve_campaign_batch and resolve_campaign_positions |
| heartbeat / snapshot | canonical heartbeat/snapshot persistence, lab identities |

A synthetic campaign/run **metadata lineage** is created solely inside the new
laboratory DB using canonical campaign helpers; no worker/execution runtime,
preflight, account reconciliation or real campaign is launched. Canonical
REJECTED observations and campaign-run ownership are not bypassed. This isolated
lab is never usable as real qualification or authentication evidence.

`StorageController.check` runs on the configured operation-time maintenance
cadence; its existing archive/retention eligibility, protections, writer
arbitration and checkpoint code execute unchanged. No mock SQL writer,
stats-only switch, manual evidence deletion, vacuum, WAL pragma weakening or
competing archive implementation is used. Archive paths, when configured, must
be precisely `<new-output>/archive`; external destinations are refused. Created
laboratory campaigns remain nonterminal/protected, so archive **success** is not
fabricated. Canonical archive tests cover successful eligible archival separately.
An empty archive directory in the Mini target policy stays empty.

The example generator uses the configured scan cadence, a declared synthetic
candidate count and a six-hour **logical** window. It also supplies one explicit
synthetic observation/reject/position/snapshot/shadow/resolver lifecycle when the
window contains the configured reject horizon. Short examples report missing
surfaces rather than consuming future candles. Candidate cardinality,
payload sizes, eligibility, decisions, accepted/rejected mix, lifecycle costs,
shadow actions, 240-bar unresolved windows and concurrency must be covered by a
reviewed recorded/full workload before claiming representativeness. The harness
supports the adapters; it does not manufacture missing captured market evidence.
The focused fixture tests exercise all adapters, including canonical TP closure,
reject ownership and immutable shadow behavior, without being calibration data.

Accelerated replay is not six hours of wall-clock operational health. Controller
wall-time aging, real provider activity, full runtime concurrency and periodic
writers are not reconstructed by an input timestamp alone. Missing surfaces or
less than 21,600 logical seconds make the worker incomplete. Even a structurally
complete replay never promotes itself to an admission rate.

## Measurements and isolation

Per committed event, samples are taken **before and after** maintenance. They
include DB/WAL/SHM, all local archive and diagnostic files, filesystem allocated
blocks, page/freelist/live allocation, journal/WAL-autocheckpoint settings and host
free bytes. Canonical checkpoint reports include frames/backlog/busy results.
A parent observes the output filesystem every ~50 ms during writes/maintenance,
including archive transient files. Component and aggregate sampled peaks survive
WAL truncation/reuse. No average is called an upper bound; no rate is emitted.
Raw samples and maintenance reports persist separately; summaries use streaming
peaks and bounded recent reports.

A fresh real parent directory, atomic `0600 O_EXCL` attempt reservation and fresh
`0700` child output directory isolate the laboratory. The reservation is fsynced
**before** DB/archive/output preparation. It is never deleted on error/timeout.
A second worker reservation prevents direct worker re-entry. The worker receives
no credentials/process config, only the selected plan and minimal environment;
its DB paths are hardcoded below its output. Outgoing socket connects are refused
at the application boundary. This is not an OS network-policy attestation.

The owned child has wall timeout, output-log bounds, a per-file RLIMIT_FSIZE,
aggregate sampled logical/allocated-byte bounds and host reserve checks. Abort
kills the owned child PID; no unrelated process/group is signalled. A minimal
fresh directory is sufficient for these disposable offline fixtures: canonical
SQLite uses a normal file DB; it has no disk-image dependency. No hdiutil command
is invoked. The historical hdiutil create failure remains INCONCLUSIVE.

The worker also watches its original supervisor identity and exits on parent
loss, even if publishing its failure note fails. This is the same watchdog used
by the dependent real-calibration implementation, not a quota or hard real-time
lease. The durable consumed claim without a final result remains incomplete;
it never authorizes retry/resume. Structured credential fields and signed-query
secrets are redacted from diagnostics as well as plain key/value text.

These are **observed aggregate bounds**, not a kernel filesystem quota. A write
or archive burst may exceed the aggregate bound before the next sample; hidden
peaks remain an explicit coverage limit. This implementation does not claim hard
physical containment or safe future capacity. If a later actual experiment
requires a strict aggregate cap, provision an independently authorized quota or
bounded volume first; a DMG is one option, not a demonstrated requirement for
this harness. No image or security policy is created/changed here.

Parent stderr/stdout are size-limited, redacted and saved `0600`, together with
returncode/timeout/abort cause. Failed preparation has a persistent sibling
result even if the child directory could not be made. Missing/partial/cross-scope
worker evidence never succeeds or creates a rate. Permission/storage errors while
publishing evidence remain failures, not simulated durable results. Existing
paths/results/attempts must be preserved; choose a new reviewed attempt, never
reuse the old Issue631 approval, launcher or claim.

## Review-only commands (not executed for a real calibration)

Use a **new clean checkout of this harness commit**, not Mini's verified
67bb0b5 staging. That staging and its shared venv must stay unchanged. Select an
interpreter whose runtime requirements match; a private venv may be used. A plan
created on another interpreter/host/filesystem cannot be silently reused.

```sh
cd /path/to/new-clean-harness-worktree
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/src"
PY=/path/to/verified-private-python
CONTROL=/absolute/existing/fresh-control-directory
# First prepare explicit nonsecret config JSON matching the staged target:
# config 0c1969da..., strategy 031acf34..., universe 1832b565..., cost 9f4ee93f...
# Position-management shadow must be true in that JSON; no secret dotenv copy.
$PY -m alphaforge.storage_calibration example \
  --config-input /absolute/path/config.nonsecret.json \
  --candidate-count 32 --output "$CONTROL/plan.json"
# Inspect the plan, input source/limits and current code SHA before execution.
DIGEST=$(shasum -a 256 "$CONTROL/plan.json" | awk '{print $1}')
$PY -m alphaforge.storage_calibration run \
  --plan "$CONTROL/plan.json" --plan-sha256 "$DIGEST" \
  --output "$CONTROL/new-laboratory"
```

This illustrative plan declares 21,600 operation seconds, a 600-second child wall
limit, 1 GiB sampled output bound and per-file size limit, 608,174,080-byte host
reserve floor and 64 KiB per stdout/stderr stream. The canonical policy reserve
(free reserve + high-minus-low + WAL + duration*operator-rate) is checked as well.
Input JSON is bounded to 256 MiB/100,000 events; generated workloads that cannot
fit must not be downsampled and called representative. Existing input storage
already reduces measured host free space; control-plane claim/result files are
outside the measured child output and must be included in an operator's total
filesystem budget. These are diagnostic resource limits, not measured capacity
or relaxed production watermarks. The Mini full-replay policy stays 1 GiB/700 MiB
and R=0; no future rate/limit is preselected.

`example` is illustrative: one lifecycle and a fixed candidate cardinality do
not represent all live payload distributions or concurrency. Short, failed or
partial coverage exits nonzero; a structurally complete diagnostic may exit zero
but its admission flag still stays false. A reviewed recorded workload may use
the same schema/adapters, without gaining automatic admission authority. No actual six-hour measurement or workload generation was performed in
this development task. Tests use only tiny temporary fixtures.

Abort on source/config/environment/input drift, reservation/path collisions,
unknown resource state, time/log/file/aggregate/host-reserve violation, canonical
storage blocking, malformed candles/lifecycle evidence or integrity/FK failure.
Keep the claim, logs, input, samples, maintenance and result. Do not resume, clear
recovery, delete the directory automatically or alter policy to turn it green.
