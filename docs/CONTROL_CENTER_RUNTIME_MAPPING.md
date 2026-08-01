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
| Reject rows/reason quality | pending reject resolver | `ControlCenterService.rows` | `burnin_pending_reject_labels.reject_reason` | 10 s | optional table; empty reason is marked `MISSING`, not renamed UNKNOWN |
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

## Configuration and security boundary

Required operational configuration is `ALPHAFORGE_DB_PATH`, `ALPHAFORGE_PROJECT_ROOT`, `ALPHAFORGE_PYTHON_EXECUTABLE`, `ALPHAFORGE_CONTROL_TOKEN`, and PAPER `ALPHAFORGE_EXECUTION_MODE`. When the dashboard is explicitly constructed with a SQLite URL, that URL supplies the DB path for compatibility; there is no fabricated database fallback. The control token is accepted only in `X-AlphaForge-Control-Token`, compared in constant time, and never passed to or recorded by the subprocess. The API binds campaign IDs, rejects IDs outside a conservative grammar, uses no shell, accepts no arbitrary CLI options or log paths, serializes campaign operations with a non-blocking per-campaign lock, limits subprocess time, sanitizes output, appends an operation audit JSONL record, then re-reads canonical state.

SQLite lock errors are `DB_LOCKED`; unavailable data is never converted to numeric zero. Read health, preflight success, campaign state, worker evidence, drift, recovery, and evidence completeness are separate signals. A successful CLI exit alone is never success.

## PowerShell startup and manual verification

```powershell
$env:ALPHAFORGE_DB_PATH = 'C:\AlphaForge\alphaforge.db'
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

Do not resume a recovery-required or drifted campaign; the API rejects it. Retry a `DB_LOCKED` read only after identifying the writer/transaction rather than substituting cached state. Stop is unsupported: canonical code has pause and runtime shutdown behavior but no campaign `STOPPED` lifecycle/CLI command, so the API does not disguise pause as stop.
