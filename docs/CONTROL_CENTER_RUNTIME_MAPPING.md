# Control Center canonical runtime mapping

The Control Center is a backend-only, PAPER-only adapter. It does not create campaigns, invent runtime state, migrate the database, or expose SQLite to a browser. All reads use SQLite `mode=ro`, `query_only`, a 250 ms busy timeout, bound parameters, and schema inspection before optional tables are queried.

## API/UI source map

| UI/API field | Canonical source | File/function | Table/column or CLI | Refresh guidance | Schema compatibility |
|---|---|---|---|---|---|
| Campaign ID/status/release | campaign repository | `burnin_campaign.get_campaign`; Control Center `_campaign` | `burnin_campaigns.campaign_id`, `campaign_status`, `release_id` | 5 s | required; `SCHEMA_MISMATCH` if absent |
| Active campaign | Phase 9 active status contract | `burnin_ops.ACTIVE_CAMPAIGN_STATUSES`; `ControlCenterService.active` | canonical active statuses in `burnin_campaigns` | 5 s | zero rows is `NO_ACTIVE_CAMPAIGN`; multiple rows fail closed |
| Active/continuation runs | campaign repository | `burnin_campaign.start_or_resume_campaign`; `status` | `burnin_campaign_runs`, `active_run_id`, `continuation_sequence` | 5 s | run table/identity columns required |
| Worker evidence | campaign worker attachment | `burnin_cli._launch_detached_worker`; `status` | `worker_pid`, `worker_started_at`, `last_heartbeat_at`, process existence | 5 s | missing optional worker columns are null; never inferred HEALTHY |
| Decision totals/rates | canonical observations | `burnin_campaign.aggregate_campaign`; `status` | `burnin_observations.decision` joined through campaign runs | 10 s | unavailable table/columns yields `UNAVAILABLE_IN_SCHEMA`; zero denominator yields null rates |
| Reject history/count/reason quality | canonical observations | `ControlCenterService.rejects` | rejected `burnin_observations`; `observation_id`; `metrics_json.reject_reason` | 10 s | deduplicated by canonical ID before limit; historical schemas without the ID explicitly use raw-row semantics; pending labels are excluded |
| Paper positions | pending position resolver | `ControlCenterService.rows` | `burnin_pending_position_outcomes` | 10 s | selects existing columns only (`qty` is not assumed) |
| Target/duration/evidence | campaign record | `ControlCenterService.status` | campaign row: `target_*`, `expected_duration_seconds`, `observed_duration_seconds`, `evidence_completeness_status` | 10 s | absent optional columns remain absent, never zero-filled |
| Config drift/recovery | Phase 9 contract | `CONFIG_DRIFT_REASONS`; `status` | `last_error`, `campaign_status` | 5 s | explicit boolean derived from canonical values |
| Aggregate contamination/duplicate sequence | continuation evidence | `status` | run IDs and `continuation_sequence` | 10 s | derived only from available canonical run rows |
| Preflight | Phase 9 preflight repository | `burnin_ops.preflight`; `preflight` | latest `burnin_preflight_reports` row | 30 s | missing table/row and malformed JSON reported separately |
| Events | campaign event repository | `burnin_campaign.event`; `rows` | `burnin_campaign_events` | 5 s | optional schema availability metadata |
| stdout/stderr | detached worker launcher | `burnin_cli._launch_detached_worker`; `logs` | fixed `artifacts/burnin/<campaign>/worker.{stdout,stderr}.log` | 5 s | bounded 500-line tail, fixed paths, secret redaction |
| Pause | canonical CLI | `burnin_cli.main`, `pause_campaign` | `python -m alphaforge.burnin_cli --db <db> --json pause --campaign-id <id>` | operator action | RUNNING only; postcondition PAUSED |
| Resume | canonical CLI continuation | `burnin_cli.main`, `start_or_resume_campaign` | `python -m alphaforge.burnin_cli --db <db> --json resume --campaign-id <id> --detach` | operator action | PAUSED only; drift/recovery blocked; RUNNING and worker health rechecked |

No canonical campaign PnL, cosmetic progress percentage, command-line identity for an already-running PID, or general STOPPED campaign state was found. Those fields and a stop endpoint are deliberately absent. Worker health uses PID existence plus heartbeat freshness, persisted start time, and campaign state; because process command-line/start-time identity is not canonical persistence, evidence that does not meet all available checks is `UNKNOWN` rather than `HEALTHY`.

## Actual read SQL

The implementation schema-inspects `sqlite_master` and `PRAGMA table_info` first. Runtime reads are the following (dynamic `IN` placeholders contain one bound placeholder per canonical run/status ID; table/order identifiers come only from internal allowlists):

```sql
SELECT * FROM burnin_campaigns WHERE campaign_id=?;
SELECT * FROM burnin_campaigns WHERE campaign_status IN (?,...) ORDER BY created_at DESC;
SELECT * FROM burnin_campaigns ORDER BY created_at DESC, id DESC;
SELECT * FROM burnin_campaign_runs WHERE campaign_id=? ORDER BY continuation_sequence;
SELECT decision,COUNT(*) n FROM burnin_observations WHERE burnin_run_id IN (?,...) GROUP BY decision;
SELECT * FROM "<allowlisted evidence table>" WHERE campaign_id=? ORDER BY "<allowlisted time column>" DESC LIMIT ?;
SELECT * FROM burnin_preflight_reports ORDER BY generated_at DESC,id DESC LIMIT 1;
```

### Reject result semantics

`reject_total` is the count of the entire campaign's canonical rejected-observation set before `LIMIT`. When `observation_id` exists, a windowed SQL CTE retains one row per canonical ID before both count/aggregation and pagination; the most recent SQLite row for that ID wins. When that reliable identity is absent, no estimated composite key is invented: `deduplication.applied=false`, `key=null`, and `semantics=RAW_OBSERVATION_ROWS_NO_RELIABLE_UNIQUE_KEY`. Pending reject labels are a resolver queue and are never unioned into history.

`returned_count`, `limit`, and `pagination.has_more` describe only the returned page. `reason_distribution_scope=campaign_distribution` means reason counts and nullable rates are calculated over the entire same deduplicated campaign set, not the page. Missing/blank reason (`reason_quality=MISSING`), malformed metrics JSON (`MALFORMED_METRICS_JSON`), and an explicitly persisted `UNKNOWN` (`EXPLICIT`) remain distinct. With `reject_total=0`, the distribution is empty; no artificial zero percentage is emitted.

## Configuration and security boundary

Required operational configuration is `ALPHAFORGE_DB_PATH`, `ALPHAFORGE_PROJECT_ROOT`, `ALPHAFORGE_PYTHON_EXECUTABLE`, `ALPHAFORGE_CONTROL_TOKEN`, and PAPER `ALPHAFORGE_EXECUTION_MODE`. When the dashboard is explicitly constructed with a SQLite URL, that URL supplies the DB path for compatibility; there is no fabricated database fallback. The control token is accepted only in `X-AlphaForge-Control-Token`, compared in constant time, and never passed to or recorded by the subprocess. The API binds campaign IDs, rejects IDs outside a conservative grammar, uses no shell, accepts no arbitrary CLI options or log paths, serializes campaign operations with a non-blocking per-campaign lock, limits subprocess time, sanitizes output, appends an operation audit JSONL record, then re-reads canonical state.

Pause success has two independent postconditions: the campaign must be canonically `PAUSED`, and the active continuation plus persisted PID/process evidence must establish worker `STOPPED`. The service polls for at most `ALPHAFORGE_CONTROL_PAUSE_WORKER_TIMEOUT_SECONDS` (default `2.0`) at `ALPHAFORGE_CONTROL_PAUSE_WORKER_POLL_INTERVAL_SECONDS` (default `0.1`). A still-live PID is `PROCESS_PRESENT`; ambiguous PID/run evidence is `UNKNOWN`. Both produce `PARTIAL_FAILURE`, never success. Audit rows preserve previous/verified campaign and worker states, verification source, timeout, and result separately. PID start-time/command-line identity is not canonical persistence, so ambiguous PID-reuse evidence remains fail-closed.

Operation serialization combines the in-process lock with a filesystem directory lease. The lease stores a random operation owner token atomically inside its newly-created directory. Release owner-checks the token and owner-verifies an atomic rename before deletion, so an old process cannot delete a replacement owner's lease. `ALPHAFORGE_CONTROL_LEASE_STALE_SECONDS` defaults to `120.0`; stale takeover renames and revalidates the displaced owner before creating the replacement. Missing/malformed metadata is never assumed stale and requires operator review.

SQLite lock errors are `DB_LOCKED`; unavailable data is never converted to numeric zero. Read health, preflight success, campaign state, worker evidence, drift, recovery, and evidence completeness are separate signals. A successful CLI exit alone is never success.

## PowerShell startup and manual verification

```powershell
$env:ALPHAFORGE_DB_PATH = 'data/runtime/alphaforge_runtime.db'
$env:ALPHAFORGE_PROJECT_ROOT = 'C:\AlphaForge'
$env:ALPHAFORGE_PYTHON_EXECUTABLE = 'C:\AlphaForge\.venv\Scripts\python.exe'
$bytes = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$env:ALPHAFORGE_CONTROL_TOKEN = [Convert]::ToBase64String($bytes)
$env:ALPHAFORGE_EXECUTION_MODE = 'PAPER'
Set-Location $env:ALPHAFORGE_PROJECT_ROOT
& $env:ALPHAFORGE_PYTHON_EXECUTABLE -m uvicorn alphaforge.dashboard.app:create_app --factory --host 127.0.0.1 --port 8000
```

In a second terminal, perform non-destructive reads first:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
Invoke-RestMethod http://127.0.0.1:8000/api/runtime
$active = Invoke-RestMethod http://127.0.0.1:8000/api/campaigns/active
$id = $active.data.campaign_id
Invoke-RestMethod "http://127.0.0.1:8000/api/campaigns/$id/status"
Invoke-RestMethod "http://127.0.0.1:8000/api/campaigns/$id/rejects"
Invoke-RestMethod "http://127.0.0.1:8000/api/campaigns/$id/positions"
Invoke-RestMethod "http://127.0.0.1:8000/api/campaigns/$id/logs"
Invoke-RestMethod http://127.0.0.1:8000/api/preflight/latest
```

Only with an approved operational window, use the exact active ID and preserve the token header:

```powershell
$headers = @{ 'X-AlphaForge-Control-Token' = $env:ALPHAFORGE_CONTROL_TOKEN }
Invoke-RestMethod -Method Post -Headers $headers "http://127.0.0.1:8000/api/campaigns/$id/pause"
Invoke-RestMethod "http://127.0.0.1:8000/api/campaigns/$id/status"
Invoke-RestMethod -Method Post -Headers $headers "http://127.0.0.1:8000/api/campaigns/$id/resume"
Invoke-RestMethod "http://127.0.0.1:8000/api/campaigns/$id/status"
```

Do not resume a recovery-required or drifted campaign; the API rejects it. Retry a `DB_LOCKED` read only after identifying the writer/transaction rather than substituting cached state. Stop is unsupported: canonical code has pause and runtime shutdown behavior but no campaign `STOPPED` lifecycle/CLI command, so the API does not disguise pause as stop. The bounded polling, process exit/detach, PID-reuse handling, and directory lease takeover have fixture coverage but have not been accepted against a live Windows worker/filesystem runtime.

## Frontend compatibility, CORS, and deployment audit

Compatibility aliases use the same service functions and response envelope as the canonical routes: `/api/runtime/status` aliases `/api/runtime`, `/api/campaigns/current` aliases `/api/campaigns/active`, and `/api/campaigns/{campaign_id}` aliases its `/status` route. The backend base URL for a separately deployed local frontend is therefore normally `http://127.0.0.1:8000`; this repository contains no separate AlphaForge Control Center SPA source, base-URL configuration, API error-code mapper, or API refresh/retry adapter to audit. The bundled dashboard JavaScript refreshes only same-origin HTML partials every 10 seconds, ignores non-success responses, and retries on the next scheduled interval. Consequently, mock-free compatibility with an external frontend cannot be claimed from this repository alone; its deployed build must be acceptance-tested against these documented endpoints and the existing `{data,source,observed_at,generated_at,age_seconds,is_stale}` envelope.

`ALPHAFORGE_CONTROL_CORS_ORIGINS` is the only source of the comma-separated exact-origin allowlist. Unset or empty configuration trusts no cross-origin browser origin; same-origin requests continue normally. Local development must opt in explicitly, for example `ALPHAFORGE_CONTROL_CORS_ORIGINS=http://127.0.0.1:5173`. Wildcards and invalid URL forms fail configuration and are never combined with credentials. A hosted HTTPS frontend can still be blocked from a local HTTP backend by browser mixed-content or Private Network Access policy even when its exact origin is allowed; CORS configuration does not override those browser security layers.

`/api/health` independently reports backend, database, runtime, active-campaign, worker, and control-action status. A successful read-only database probe does not imply worker health. Operator diagnostics expose hostname, operating system, backend version, git identity, database modification time, resolved project root, and a redacted database identity (filename, size, and SHA-256 of the resolved path rather than the raw path). Runtime responses obtain execution mode from the validated AlphaForge configuration; non-PAPER control calls remain rejected.

## Canonical freshness and recovery boundary

`generated_at` is only the API response construction timestamp. It is never reused as evidence time. Endpoint `observed_at` comes from a persisted timestamp belonging to that source (for example `burnin_observations.observed_at`, preflight `generated_at`, event time, or bounded log file modification time). `ALPHAFORGE_CONTROL_FRESHNESS_SECONDS` sets the threshold and defaults to 120 seconds. Missing evidence time returns null age/staleness with `DATA_UNAVAILABLE`; malformed time returns `INVALID_TIMESTAMP`; future-dated evidence returns `CLOCK_SKEW`. Combined status/health payloads expose per-source freshness for worker heartbeat, attachment, and database file evidence rather than inventing one shared fresh timestamp.

Worker `HEALTHY` requires the canonical active campaign/run mapping to be RUNNING, a living persisted PID, a non-future fresh heartbeat, persisted `worker_started_at`, and a matching `PHASE8_CAMPAIGN_ATTACHED` event after worker start whose details contain the active run and runtime instance identities. Missing or ambiguous attachment/PID-reuse evidence is `UNKNOWN`. No authoritative aggregate-contamination field exists, so the API returns `aggregate_contamination=null` with `aggregate_contamination_availability=DATA_UNAVAILABLE`; run names are never interpreted as evidence.

Recovery and zero-exposure terminalization remain exclusively owned by canonical `alphaforge.burnin_ops` (the PR #312 recovery path). Control Center neither updates recovery state nor exposes terminalization. Resume rejects `RECOVERY_REQUIRED` or recovery-marked campaigns, config drift, inconsistent/duplicate active continuation mappings, unverified predecessor-worker stop identity, multiple active campaigns, nonzero CLI exits, and failed postconditions. Runtime SQLite access remains URI `mode=ro`, `PRAGMA query_only=ON`, schema-checked, parameterized, and bounded by the short read busy timeout; only the sanitized operation audit artifact and owner-token lease are written outside runtime state.

Composite endpoints (`health`, `runtime`, campaign status, and control-operation responses) intentionally return `observed_at`, `age_seconds`, and `is_stale` as null with `freshness_state=MULTI_SOURCE` and `availability=AVAILABLE`. They do not invent a shared timestamp; canonical component states remain in `source_freshness`. Single-source endpoints retain persisted-source `FRESH`, `STALE`, `CLOCK_SKEW`, `INVALID_TIMESTAMP`, or `DATA_UNAVAILABLE` semantics.
