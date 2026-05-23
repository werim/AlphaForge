# AlphaForge Dashboard

Python-only, read-only operations panel for AlphaForge. It uses FastAPI, Jinja2 templates and local browser assets. It does not start the trading runtime, submit orders, activate LIVE mode, change configuration or mutate the kill switch.

## Current screens

| Path | Purpose |
|---|---|
| `/` | Configured safety state, reject summary and recent lifecycle events |
| `/rejects` | Reject distribution and incomplete persistence-row warnings |
| `/lifecycle` | Recent event list and per-signal timeline |
| `/readiness` | Most recently persisted LIVE readiness report plus expected-probe evidence coverage matrix |
| `/health` | Service heartbeat for the dashboard process only |

JSON read endpoints:

- `GET /api/v1/runtime/status`
- `GET /api/v1/rejects/summary`
- `GET /api/v1/lifecycle/{signal_id}`
- `GET /api/v1/readiness/latest`
- `GET /api/v1/readiness/probes`

## Operations & Readiness Console audit increment

The merged dashboard initially surfaced persisted readiness checks only when a readiness report already existed. It did not enumerate the expected readiness evidence universe or distinguish an absent probe from a failed probe. The lowest-risk next increment is read-only evidence coverage visibility:

- an explicit catalog of the 26 existing `LiveReadinessEvaluator` checks and the not-yet-implemented persisted runtime heartbeat probe;
- fail-closed statuses: `PASS`, `FAIL`, `NO_EVIDENCE`, `MISSING_IN_REPORT`, and `MISSING_PROBE`;
- critical-gap count and a clearly rendered control boundary;
- no execution, configuration, LIVE activation or kill-switch write endpoint.

This increment audits whether expected probes are represented in persisted evidence. It does not run external probes, assert market execution quality, generate a readiness report or qualify LIVE.

### Missing probes, controls and data surfaces identified

| Target console surface | Existing before increment | State after increment | Safe next step, not implemented here |
|---|---|---|---|
| Persisted runtime heartbeat / liveness | Explicitly `UNVERIFIED`; no evidence table | Displayed as `MISSING_PROBE` | Add persisted heartbeat production and freshness logic in a separate runtime-reviewed change |
| Readiness expected-check coverage | Only raw latest-report checks shown | Catalog coverage matrix and missing-check visibility | Link probe history/freshness after evidence persistence is stable |
| Mode parity, reconciliation, alert and rollback evidence | Present inside latest report payload only | Mapped into observable coverage categories | Add dedicated evidence-detail drill-down only if backed by stable stored contracts |
| Order/LIVE/kill-switch/config mutation controls | Deliberately absent | Still deliberately absent | Keep absent until authenticated RBAC, audit log and fail-closed approval workflow exist |
| External exchange/runtime active probing from dashboard | Absent | Still absent | Prefer runtime-produced persisted evidence; do not let UI become an execution surface |

## Safety boundary

This initial dashboard branch deliberately omits:

- order submission, cancellation or amendment endpoints,
- LIVE activation controls,
- kill-switch mutation controls,
- configuration editing,
- private exchange credential handling,
- external exchange probes,
- automatic runtime startup.

The panel reports runtime process status as `UNVERIFIED` until persisted runtime heartbeat evidence is implemented. Missing readiness evidence is displayed as `NOT_AVAILABLE`, never as PASS. The readiness probe matrix likewise remains `INCOMPLETE` when a probe is missing, a report is absent, or an expected check is absent from the latest report.

For the current SQLite runtime database, the dashboard opens an existing database through a read-only SQLite URI. If the runtime database does not exist yet, the dashboard displays empty/unavailable states without creating a runtime database file or running migrations.

## Local macOS start

```bash
cd ~/Projects/AlphaForge
git checkout Dashboard
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn alphaforge.dashboard.app:create_app --factory --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000` in Safari or Chrome.

`127.0.0.1` binds the panel only to the local Mac. Do not expose the dashboard directly to the public internet.

## Install as a macOS service

The included installer creates a Python virtual environment when needed, installs the dashboard package and creates a user `launchd` service named `com.alphaforge.dashboard`:

```bash
cd ~/Projects/AlphaForge
git checkout Dashboard
bash scripts/install_dashboard_macos.sh
```

Expected result:

```text
AlphaForge Dashboard installed and started on http://127.0.0.1:8000
```

Check status:

```bash
launchctl print gui/$(id -u)/com.alphaforge.dashboard
```

View logs:

```bash
tail -f logs/dashboard.out.log logs/dashboard.err.log
```

Stop the dashboard service:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.alphaforge.dashboard.plist
```

The installer does not install or launch the trading runtime. Runtime should remain a separate process and start in PAPER mode while the panel is being validated.

## Windows PC installation

PowerShell installer requirements:

- Windows PowerShell 5.1 or later.
- Python 3.11 or later available through `py -3` or `python`.
- Repository checked out on the `Dashboard` branch.

Open PowerShell in the AlphaForge project directory:

```powershell
Set-Location E:\Projeler\AlphaForge
git fetch origin Dashboard
git switch Dashboard
git pull --ff-only origin Dashboard
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install_dashboard_windows.ps1
```

The installer:

- creates `.venv` if needed,
- installs the project and dashboard dependencies,
- creates a current-user Scheduled Task named `AlphaForge Dashboard`,
- starts the dashboard on `http://127.0.0.1:8000`,
- writes output to `logs\dashboard.windows.log`,
- does not start the AlphaForge trading runtime.

Open the dashboard:

```text
http://127.0.0.1:8000
```

Check the auto-start task:

```powershell
Get-ScheduledTask -TaskName "AlphaForge Dashboard"
Get-ScheduledTaskInfo -TaskName "AlphaForge Dashboard"
```

Read dashboard logs:

```powershell
Get-Content .\logs\dashboard.windows.log -Wait
```

Stop or remove the dashboard task:

```powershell
Stop-ScheduledTask -TaskName "AlphaForge Dashboard"
Unregister-ScheduledTask -TaskName "AlphaForge Dashboard" -Confirm:$false
```

Install without creating an automatic logon task:

```powershell
.\scripts\install_dashboard_windows.ps1 -NoScheduledTask
```

Install on another localhost port:

```powershell
.\scripts\install_dashboard_windows.ps1 -Port 8080
```

The Windows runner binds only to `127.0.0.1`. Do not change it to `0.0.0.0` or expose it to the public internet before authentication and access-control controls exist.

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

The dashboard reads the configured runtime database URL used by AlphaForge. With default configuration, this is the project runtime SQLite database under `data/runtime/`.

## Remote access later

For remote access from another trusted device, prefer a private VPN such as Tailscale rather than opening port `8000` through the router. Authentication and RBAC should be added before any remote operational actions or write controls are introduced.
