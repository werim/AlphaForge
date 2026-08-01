# Control Center canonical runtime mapping

The Control Center is a backend-only, PAPER-only adapter. It does not create campaigns, invent runtime state, migrate SQLite, or expose SQLite to a browser. Reads use SQLite `mode=ro`, `query_only`, a 250 ms busy timeout, bound values, schema inspection, and structured query-error conversion.

## Source, timestamp, freshness, and schema map

`generated_at` is only response construction time. It is never evidence freshness. Every endpoint has a `freshness` map; the backward-compatible top-level `observed_at`, `age_seconds`, and `is_stale` mirror its first entry. Missing timestamps produce null values and explicit availability. Invalid and future timestamps produce `INVALID_TIMESTAMP` and `CLOCK_SKEW`, respectively. Naive timestamps are interpreted as UTC and all valid output is UTC.

`ALPHAFORGE_CONTROL_STALE_AFTER_SECONDS` controls all evidence stale comparisons and defaults explicitly to 120 seconds. This is a Control Center operational threshold, not a claim that all canonical sources share an update cadence.

| API group | Canonical data source | Canonical observation timestamp | Safe query/order fallback | Missing timestamp/schema behavior |
|---|---|---|---|---|
| Campaign list/active | `burnin_campaigns`; Phase 9 `ACTIVE_CAMPAIGN_STATUSES` | `created_at`; active response may use `last_heartbeat_at`, `started_at`, then `created_at` | row ordering: `created_at`, then `id`; `id` is never treated as time | null freshness if no timestamp; zero active is `NO_ACTIVE_CAMPAIGN`, multiple active fails closed |
| Status campaign | canonical campaign row and continuation mapping | `last_operator_activity_at`, `completed_at`, `started_at`, then `created_at` | none outside verified columns | null/unknown; heartbeat never makes campaign freshness implicitly fresh |
| Worker heartbeat | campaign worker attachment | `burnin_campaigns.last_heartbeat_at` | none | null plus `UNAVAILABLE_IN_SCHEMA`; invalid/future fail closed; worker is never HEALTHY without fresh heartbeat, PID and process existence |
| Decisions/metrics | deduplicated `burnin_observations` joined through `burnin_campaign_runs` | maximum `burnin_observations.observed_at` | decision counts remain available if only timestamp is absent | metric freshness null; zero denominator rates are null |
| Rejects | canonical `burnin_observations` with `decision=REJECTED`; reason from safely parsed `metrics_json.reject_reason` | `observed_at`, then `id` for ordering only | no safe order gives `UNAVAILABLE_IN_SCHEMA` | malformed JSON and missing reason are separate quality counts; explicit `UNKNOWN` remains a real label |
| Pending reject labels | `burnin_pending_reject_labels` | not presented as reject history | count only | exposed separately as `UNFINALIZED_FORWARD_LABEL_QUEUE`, never as total history |
| Positions | `burnin_pending_position_outcomes` | `created_at`, fallback `entry_time`, then `resolved_at` | only verified internal allowlist columns | no safe timestamp/order gives `UNAVAILABLE_IN_SCHEMA`; `qty` is never assumed |
| Events | `burnin_campaign_events` | `event_time` | none | no safe timestamp/order gives `UNAVAILABLE_IN_SCHEMA` |
| Preflight | latest `burnin_preflight_reports` | `generated_at` | row ordering may fall back to `id`; `id` is never treated as time | if neither order column exists, report is unavailable; if only `id` exists freshness is null/unavailable |
| Logs | fixed worker stdout/stderr paths | filesystem modification time | none | absent/unreadable logs are `DATA_UNAVAILABLE`; bounded 500-line tail and redaction remain enforced |
| Health DB probe | read-only `SELECT 1` | no canonical timestamp | none | freshness is null with `NOT_TIMESTAMPED`; successful query does not fabricate freshness |

## Recovery-required contract

Resume remains valid only from `PAUSED`. Before spawning a subprocess, Control Center combines exact canonical evidence:

1. `burnin_campaigns.campaign_status == RECOVERY_REQUIRED`;
2. the active continuation's `burnin_campaign_runs.status == RECOVERY_REQUIRED`;
3. an exact canonical `RECOVERY_REQUIRED` event for that active continuation; and
4. the latest lineage-matching `runtime_state_snapshots.recovery_action_required` flag.

The runtime snapshot must have `campaign_id`, `burnin_run_id`, `recovery_action_required`, and a verified `timestamp`/`id` ordering column. Missing or non-lineage evidence makes resumability unknown, and resume fails closed with `RECOVERY_REQUIRED`; it is never inferred clean from a PAUSED string alone. `last_error` is used separately for the established config-drift set, not as a loose recovery substring heuristic.

## Aggregate contamination

The existing Phase 9 health helper currently infers contamination from run-name text, which is not canonical enough for this API. No independent persisted contamination flag, manifest field, or repository helper was found. Consequently the API returns `aggregate_contamination: null` and `aggregate_contamination_availability: DATA_UNAVAILABLE`; a run ID containing `aggregate` is not evidence.

## Actual read SQL and schema safety

Schema inspection uses `sqlite_master` and `PRAGMA table_info`. Dynamic identifiers are selected only from internal, schema-verified allowlists. All values remain bound parameters. Representative queries are:

```sql
SELECT * FROM burnin_campaigns WHERE campaign_id=?;
SELECT * FROM burnin_campaigns WHERE campaign_status IN (?,...);
SELECT * FROM burnin_campaign_runs WHERE campaign_id=? ORDER BY continuation_sequence;
SELECT decision, COUNT(DISTINCT observation_id) FROM burnin_observations WHERE burnin_run_id IN (?,...) GROUP BY decision;
SELECT * FROM burnin_observations WHERE burnin_run_id IN (?,...) AND UPPER(decision)=? ORDER BY "<verified observed_at|id>" DESC LIMIT ?;
SELECT * FROM "<verified evidence table>" WHERE campaign_id=? ORDER BY "<verified allowlisted timestamp>" DESC LIMIT ?;
```

Every execute and fetch path goes through the same SQLite error boundary. `database is locked` and `database table is locked` become `DB_LOCKED`; other SQLite failures become sanitized `BACKEND_UNREACHABLE`. SQL text and database paths are not returned.

## Process-safe control lock and security

Pause and resume retain constant-time `X-AlphaForge-Control-Token` verification, strict campaign-ID grammar, sole-active-campaign matching, state/drift/recovery gates, argument-list subprocess execution, `shell=False`, timeout, redaction, audit, postcondition read, and resume worker verification.

A per-campaign atomic directory lease now lives below the validated project artifact root at `artifacts/burnin/control_center_locks/<campaign>.lock`. `mkdir` is atomic across Uvicorn processes and Windows. Owner PID, operation ID, and creation time are recorded. A dead-owner lease or a lease older than `max(2 * command timeout, 60 seconds)` is reclaimable; every acquired lease is released in `finally`. An incomplete lease is reclaimed only after the same bound. This avoids an unbounded crash deadlock without adding a dependency. Campaign ID validation prevents traversal.

Canonical CLI commands remain:

```text
python -m alphaforge.burnin_cli --db <db> --json pause --campaign-id <id>
python -m alphaforge.burnin_cli --db <db> --json resume --campaign-id <id> --detach
```

There is no canonical burn-in stop CLI or general `STOPPED` campaign state, so no stop endpoint exists.

## PowerShell manual validation

```powershell
$env:ALPHAFORGE_DB_PATH = 'C:\AlphaForge\alphaforge.db'
$env:ALPHAFORGE_PROJECT_ROOT = 'C:\AlphaForge'
$env:ALPHAFORGE_PYTHON_EXECUTABLE = 'C:\AlphaForge\.venv\Scripts\python.exe'
$env:ALPHAFORGE_EXECUTION_MODE = 'PAPER'
$env:ALPHAFORGE_CONTROL_STALE_AFTER_SECONDS = '120'
$bytes = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$env:ALPHAFORGE_CONTROL_TOKEN = [Convert]::ToBase64String($bytes)
Set-Location $env:ALPHAFORGE_PROJECT_ROOT
& $env:ALPHAFORGE_PYTHON_EXECUTABLE -m uvicorn alphaforge.dashboard.app:create_app --factory --host 127.0.0.1 --port 8000
```

Perform non-destructive reads before any operator action:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
Invoke-RestMethod http://127.0.0.1:8000/api/runtime
$active = Invoke-RestMethod http://127.0.0.1:8000/api/campaigns/active
$id = $active.data.campaign_id
Invoke-RestMethod "http://127.0.0.1:8000/api/campaigns/$id/status"
Invoke-RestMethod "http://127.0.0.1:8000/api/campaigns/$id/rejects"
Invoke-RestMethod "http://127.0.0.1:8000/api/campaigns/$id/positions"
Invoke-RestMethod "http://127.0.0.1:8000/api/campaigns/$id/events"
Invoke-RestMethod "http://127.0.0.1:8000/api/campaigns/$id/logs"
Invoke-RestMethod http://127.0.0.1:8000/api/preflight/latest
```

Only in an approved PAPER window, after status reports `recovery.required = false` and expected freshness:

```powershell
$headers = @{ 'X-AlphaForge-Control-Token' = $env:ALPHAFORGE_CONTROL_TOKEN }
Invoke-RestMethod -Method Post -Headers $headers "http://127.0.0.1:8000/api/campaigns/$id/pause"
Invoke-RestMethod "http://127.0.0.1:8000/api/campaigns/$id/status"
Invoke-RestMethod -Method Post -Headers $headers "http://127.0.0.1:8000/api/campaigns/$id/resume"
Invoke-RestMethod "http://127.0.0.1:8000/api/campaigns/$id/status"
```

No real Windows runtime database or active worker was available during implementation. Fixture tests are not live acceptance, and no running campaign was paused or resumed automatically.
