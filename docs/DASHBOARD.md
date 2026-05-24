# AlphaForge Dashboard

Python-only, read-only operations panel for AlphaForge. It uses FastAPI, Jinja2 templates and local browser assets. It does not start the trading runtime, submit orders, activate LIVE mode, change configuration or mutate the kill switch.

## Current screens

| Path | Purpose |
|---|---|
| `/` | Configured safety state, runtime heartbeat freshness, reject summary and recent lifecycle events |
| `/rejects` | Reject distribution and incomplete persistence-row warnings |
| `/lifecycle` | Recent event list and per-signal timeline |
| `/readiness` | Most recently persisted LIVE readiness report plus expected-probe evidence coverage matrix |
| `/health` | Service heartbeat for the dashboard process only, not trading-runtime liveness |

JSON read endpoints:

- `GET /api/v1/runtime/status`
- `GET /api/v1/rejects/summary`
- `GET /api/v1/lifecycle/{signal_id}`
- `GET /api/v1/readiness/latest`
- `GET /api/v1/readiness/probes`

## Persisted runtime heartbeat evidence — JOB-20

JOB-20 replaces the former hard-coded runtime heartbeat gap with runtime-produced evidence. The dashboard consumes stored rows; it never produces heartbeat rows itself.

The `runtime_heartbeats` evidence contract is additive and includes runtime instance ID, execution mode, heartbeat time, scanner source, runtime state, last scan and decision times, active position and pending order counts, evidence status, and a metrics-only payload. `PAPER` and `LIVE` runtime modes may write evidence. `BACKTEST` does not qualify through persistent heartbeat evidence.

### Freshness and qualification

The conservative default maximum age is **120 seconds**. Evaluation is deterministic and fail-closed:

| State | Meaning | LIVE posture |
|---|---|---|
| `FRESH` | Latest relevant heartbeat is well formed, operating and within the age limit | Can satisfy only the heartbeat sub-check |
| `STALE` | Latest relevant heartbeat is older than the age limit | Blocked |
| `MISSING` | No relevant persisted heartbeat exists | Blocked |
| `INVALID` | Timestamp, evidence marker or runtime state is unusable | Blocked |
| `FUTURE_DATED` | Heartbeat time is materially ahead of evaluation time | Blocked |

A `PAPER` heartbeat can appear in runtime status, but it cannot satisfy a LIVE qualification check. LIVE requires a fresh persisted `LIVE` heartbeat and every other independent readiness gate to pass.

### What heartbeat proves and does not prove

A fresh heartbeat proves that one identified trading runtime instance recently persisted operating evidence in its recorded mode. It does not prove safe market conditions, acceptable execution quality, reconciliation health, positive expectancy, or permission to trade LIVE.

The dashboard `/health` endpoint proves only that the dashboard web process answers requests. It is never substituted for trading runtime health.

## Read-only safety boundary

The dashboard deliberately omits:

- heartbeat write endpoints,
- order submission, cancellation or amendment endpoints,
- LIVE activation controls,
- kill-switch mutation controls,
- configuration editing,
- active exchange probing,
- automatic runtime startup.

The dashboard opens an existing SQLite runtime database through a read-only SQLite URI. If that database does not exist yet, the dashboard displays missing/unavailable states without creating a runtime database file or running migrations.

## Local macOS start

```bash
cd ~/Projects/AlphaForge
git checkout Dashboard
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn alphaforge.dashboard.app:create_app --factory --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000` in Safari or Chrome. Binding to `127.0.0.1` keeps the panel local to the Mac.

## Install as a macOS service

```bash
cd ~/Projects/AlphaForge
git checkout Dashboard
bash scripts/install_dashboard_macos.sh
```

Check status and logs:

```bash
launchctl print gui/$(id -u)/com.alphaforge.dashboard
tail -f logs/dashboard.out.log logs/dashboard.err.log
```

Stop the dashboard service:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.alphaforge.dashboard.plist
```

The installer does not install or launch the trading runtime. Runtime should remain a separate process and start in PAPER mode while the panel is being validated.

## Windows PC installation

Requirements: Windows PowerShell 5.1 or later and Python 3.11 or later available through `py -3` or `python`.

```powershell
Set-Location E:\Projeler\AlphaForge
git fetch origin Dashboard
git switch Dashboard
git pull --ff-only origin Dashboard
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install_dashboard_windows.ps1
```

Check the scheduled task and logs:

```powershell
Get-ScheduledTask -TaskName "AlphaForge Dashboard"
Get-ScheduledTaskInfo -TaskName "AlphaForge Dashboard"
Get-Content .\logs\dashboard.windows.log -Wait
```

Stop or remove it:

```powershell
Stop-ScheduledTask -TaskName "AlphaForge Dashboard"
Unregister-ScheduledTask -TaskName "AlphaForge Dashboard" -Confirm:$false
```

The Windows runner binds only to `127.0.0.1`. Do not expose the dashboard directly to the public internet before access controls are designed and verified.

## Running runtime separately in PAPER mode

macOS / Linux:

```bash
cd ~/Projects/AlphaForge
source .venv/bin/activate
export EXECUTION_MODE=PAPER
python -m alphaforge.runtime
```

Windows PowerShell:

```powershell
Set-Location E:\Projeler\AlphaForge
.\.venv\Scripts\Activate.ps1
$env:EXECUTION_MODE = "PAPER"
python -m alphaforge.runtime
```

The dashboard reads the runtime database configured for AlphaForge. With default configuration, it is under `data/runtime/`.