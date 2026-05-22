# AlphaForge Dashboard

Python-only, read-only operations panel for AlphaForge. It uses FastAPI, Jinja2 templates and local browser assets. It does not start the trading runtime, submit orders, activate LIVE mode, change configuration or mutate the kill switch.

## Current screens

| Path | Purpose |
|---|---|
| `/` | Configured safety state, reject summary and recent lifecycle events |
| `/rejects` | Reject distribution and incomplete persistence-row warnings |
| `/lifecycle` | Recent event list and per-signal timeline |
| `/readiness` | Most recently persisted LIVE readiness report, if one exists |
| `/health` | Service heartbeat for the dashboard process only |

JSON read endpoints:

- `GET /api/v1/runtime/status`
- `GET /api/v1/rejects/summary`
- `GET /api/v1/lifecycle/{signal_id}`
- `GET /api/v1/readiness/latest`

## Safety boundary

This initial dashboard branch deliberately omits:

- order submission, cancellation or amendment endpoints,
- LIVE activation controls,
- kill-switch mutation controls,
- configuration editing,
- private exchange credential handling,
- external exchange probes,
- automatic runtime startup.

The panel reports runtime process status as `UNVERIFIED` until persisted runtime heartbeat evidence is implemented. Missing readiness evidence is displayed as `NOT_AVAILABLE`, never as PASS.

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

## Running runtime separately in PAPER mode

```bash
cd ~/Projects/AlphaForge
source .venv/bin/activate
export EXECUTION_MODE=PAPER
python -m alphaforge.runtime
```

The dashboard reads the configured runtime database URL used by AlphaForge. With default configuration, this is the project runtime SQLite database under `data/runtime/`.

## Remote access later

For remote access from another trusted device, prefer a private VPN such as Tailscale rather than opening port `8000` through the router. Authentication and RBAC should be added before any remote operational actions or write controls are introduced.
