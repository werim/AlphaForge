# AlphaForge: Execution-Aware Trading Research/Runtime Prototype

AlphaForge is a SQL-first, execution-aware futures trading research/runtime prototype. It includes deterministic signal decision logic, symbol selection, runtime orchestration, and backtest tooling. It does **not** currently meet production live-trading standards.

> **LIVE safety gate:** AlphaForge is **not LIVE-ready by default**. Real-order LIVE use is unsafe unless the local final readiness aggregator records `LIVE_REAL_ORDERS_READY` with every lifecycle, reject persistence, execution realism, exchange connectivity, authenticated reconciliation, no-submit precheck, kill-switch, rollback, heartbeat/alert/incident, dashboard/RBAC/secrets, TimesFM non-ordering, PAPER burn-in, full-test, and operator acknowledgement gate passing. PAPER success alone is never LIVE readiness.

## Current Status

- SQL-first foundation exists (SQLAlchemy models, Alembic migrations, persistence modules).
- Symbol selection exists as a scored/reject-aware prototype selector.
- Deterministic AI decision/reject engine exists (`AIBrain`) with persisted decision features.
- Runtime orchestrator exists with `BACKTEST`, `PAPER`, and `LIVE` mode handling in code.
- Backtest lifecycle tooling exists but lifecycle/export fidelity is still incomplete.
- Live trading is **not production-ready**.

## Phase Status

| Phase | Scope | Conservative Status |
|---|---|---|
| Phase 1 | SQL-first foundation | Mostly implemented |
| Phase 2 | Decision/reject engine | Partially implemented |
| Phase 3 | Symbol selection | Implemented prototype |
| Phase 4 | Paper runtime | Implemented prototype |
| Phase 5 | Lifecycle-accurate backtest | Incomplete |
| Phase 6 | Analytics/persistence hardening | Partial |
| Phase 7 | Live execution readiness | Not ready |
| Phase 8 | Adaptive learning/optimizer | Early groundwork only |

## Not Production Ready

> **Warning**
> AlphaForge should currently be treated as a research/runtime prototype. Do not assume production-grade controls, exchange-failure handling, reconciliation, or operational safeguards for live capital deployment.

## Repository Highlights

- Runtime orchestration: `src/alphaforge/runtime.py`
- Deterministic decision engine: `src/alphaforge/ai_brain.py`
- Symbol selection: `src/alphaforge/symbol_selector.py`
- Execution context helpers: `src/alphaforge/execution.py`
- Persistence and schema modules: `src/alphaforge/persistence.py`, `src/alphaforge/models/`, `alembic/`
- Backtest runner/export script: `backtest_order.py`

## Documentation Index

- [Repository operating rules](AGENTS.md)
- [Current version and readiness snapshot](VERSION.md)
- [Technical patch report](REPORT.md)
- [Change history](CHANGELOG.md)

## Setup

### macOS / Linux

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
```

## Environment Configuration

### Environment profiles

AlphaForge now ships purpose-specific example profiles so BACKTEST diagnostics, PAPER evaluation, and LIVE preparation do not share one ambiguous template. Keep real secrets out of example files and commit only templates.

| Profile | Purpose | Safety posture |
|---|---|---|
| `.env.test.example` | Loose BACKTEST / local diagnostic runs to verify whether strategies can produce trades | NOT for LIVE; LIVE disabled, real orders blocked, diagnostic filters are intentionally looser |
| `.env.medium.example` | Balanced PAPER/default evaluation and dashboard experimentation | LIVE disabled, realistic execution-cost and risk defaults |
| `.env.live.example` | Hardened LIVE readiness preparation | Fail-closed defaults; real orders remain disabled until credentials, readiness evidence, and operator guards are explicitly supplied locally |
| `.env.example` | Safe default template | Mirrors the medium PAPER-oriented profile and points to the purpose-specific templates |

Copy exactly one profile to `.env` before running local workflows.

Windows PowerShell:

```powershell
Copy-Item .env.test.example .env
Copy-Item .env.medium.example .env
Copy-Item .env.live.example .env
```

macOS/Linux:

```bash
cp .env.test.example .env
cp .env.medium.example .env
cp .env.live.example .env
```

Recommended use:

1. Use `.env.test.example` when a BACKTEST or local PAPER diagnostic needs looser score, RR, trend/chop, spread, and universe limits to determine whether the strategy can produce auditable decisions. It remains unsafe for LIVE and keeps real-order gates closed.
2. Use `.env.medium.example` or `.env.example` for normal PAPER observation, dashboard backtests, and balanced evaluation with realistic costs, slippage, spread, funding, cooldown, and position limits.
3. Use `.env.live.example` only for hardened LIVE preparation. It requires explicit local credentials and readiness evidence, keeps `REJECT_UNKNOWN_EXPECTANCY=true`, preserves strict risk/cost/staleness guards, and does not enable live trading or live orders by default.

### Binance environment and endpoint configuration

For normal Binance USD-M Futures **production** or **testnet** operation, set `BINANCE_ENVIRONMENT` and leave `BINANCE_BASE_URL` and `BINANCE_WS_URL` blank. AlphaForge resolves the canonical endpoints automatically. Explicit endpoint overrides are intended for `demo` or other operator-verified custom endpoints, not routine production/testnet use.

| Use | `BINANCE_ENVIRONMENT` | `BINANCE_BASE_URL` | `BINANCE_WS_URL` | Resolved canonical endpoints |
|---|---|---|---|---|
| Production | `production` | leave blank | leave blank | REST `https://fapi.binance.com`, WS `wss://fstream.binance.com` |
| Futures Testnet | `testnet` | leave blank | leave blank | REST `https://testnet.binancefuture.com`, WS `wss://stream.binancefuture.com` |
| Demo/custom | `demo` | explicit operator-verified URL required | explicit operator-verified URL required for runtime websocket use | supplied explicitly by operator |

Recommended production `.env` fragment:

```dotenv
BINANCE_ENVIRONMENT=production
BINANCE_API_KEY=<your_production_readonly_key>
BINANCE_API_SECRET=<your_production_readonly_secret>
BINANCE_BASE_URL=
BINANCE_WS_URL=
```

Recommended Futures Testnet `.env` fragment:

```dotenv
BINANCE_ENVIRONMENT=testnet
BINANCE_API_KEY=<your_testnet_key>
BINANCE_API_SECRET=<your_testnet_secret>
BINANCE_BASE_URL=
BINANCE_WS_URL=
```

Do not copy production credentials into testnet or testnet credentials into production. Reconciliation credentials and the selected Binance environment must refer to the same account environment.

**Important precedence rule:** existing process environment variables override repository `.env` values. A stale shell export such as `BINANCE_ENVIRONMENT=production` or an explicit `BINANCE_BASE_URL=https://fapi.binance.com` can therefore override a `.env` that says `BINANCE_ENVIRONMENT=testnet`.

Before changing environments, clear stale Binance overrides from the current shell if needed.

macOS/Linux:

```bash
unset BINANCE_ENVIRONMENT BINANCE_TESTNET BINANCE_BASE_URL BINANCE_WS_URL BINANCE_API_KEY BINANCE_API_SECRET
```

Windows PowerShell:

```powershell
Remove-Item Env:BINANCE_ENVIRONMENT,Env:BINANCE_TESTNET,Env:BINANCE_BASE_URL,Env:BINANCE_WS_URL,Env:BINANCE_API_KEY,Env:BINANCE_API_SECRET -ErrorAction SilentlyContinue
```

Verify the effective reconciliation configuration without printing credentials:

```bash
python - <<'PY'
from alphaforge.env_contract import bootstrap_environment
from alphaforge.config import load_reconciliation_settings

bootstrap_environment()
c = load_reconciliation_settings()

print("environment:", c.environment)
print("base_url:", c.base_url)
print("key_loaded:", bool(c.api_key))
print("secret_loaded:", bool(c.api_secret))
print("key_source:", c.sources.get("BINANCE_API_KEY"))
print("secret_source:", c.sources.get("BINANCE_API_SECRET"))
PY
```

For testnet, the expected environment/endpoint pair is:

```text
environment: testnet
base_url: https://testnet.binancefuture.com
```

For production, the expected pair is:

```text
environment: production
base_url: https://fapi.binance.com
```

Do not run a burn-in launch when authenticated reconciliation is unavailable or points at the wrong Binance environment. Preflight must fail closed until the selected account environment and reconciliation evidence agree.

Mode switching uses the canonical `ALPHAFORGE_EXECUTION_MODE` value (`BACKTEST`, `PAPER`, or `LIVE`) plus the backward-compatible `EXECUTION_MODE` alias. Never assume PAPER success means LIVE readiness.

LIVE trading can lose capital quickly from slippage, spread expansion, latency, exchange-side failures, and incomplete reconciliation. Do not enable LIVE unless lifecycle integrity, reject persistence, authenticated reconciliation, no-submit prechecks, execution-risk thresholds, kill-switch behavior, rollback evidence, alerting, and operator acknowledgement are validated in your environment.

## Run migrations

```bash
alembic upgrade head
```

## Run tests

```bash
pytest -q
```

## Exact Running Commands

Run these from the repository root after activating `.venv` and installing the package in editable mode.

### Backtest

Backtest entrypoint: `backtest_order.py`.

```bash
python backtest_order.py --interval 1h --last-n-days 30 --symbols BTCUSDT,ETHUSDT --output-dir data/backtests/manual_1h_30d
```

Refresh Binance historical cache for the requested range:

```bash
python backtest_order.py --interval 1h --last-n-days 30 --symbols BTCUSDT,ETHUSDT --output-dir data/backtests/manual_1h_30d --force-refresh
```

CI/offline smoke backtest without network calls:

```bash
python backtest_order.py --ci --interval 1h --last-n-days 7 --symbols BTCUSDT --output-dir data/backtests/ci_smoke
```

BACKTEST-only SHORT breakdown rescue comparison, disabled by default unless explicitly enabled:

```bash
ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED=true python backtest_order.py --interval 1h --last-n-days 30 --symbols BTCUSDT,ETHUSDT --output-dir data/backtests/rescue_on
```

Windows PowerShell equivalent:

```powershell
$env:ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED="true"
python backtest_order.py --interval 1h --last-n-days 30 --symbols BTCUSDT,ETHUSDT --output-dir data/backtests/rescue_on
Remove-Item Env:ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED
```

### PAPER runtime

PAPER runtime entrypoint: `python -m alphaforge.runtime`.

macOS / Linux:

```bash
ALPHAFORGE_MODE=PAPER python -m alphaforge.runtime
```

Windows PowerShell:

```powershell
$env:ALPHAFORGE_MODE="PAPER"
python -m alphaforge.runtime
```

For a deterministic smoke path using the safe placeholder scanner:

```bash
ALPHAFORGE_MODE=PAPER ALPHAFORGE_RUNTIME_SAFE_SCANNER=1 python -m alphaforge.runtime
```

### Dashboard

Dashboard entrypoint: `alphaforge.dashboard.app:create_app`.

```bash
python -m uvicorn alphaforge.dashboard.app:create_app --factory --host 127.0.0.1 --port 8000
```

Then open:

```text
http://127.0.0.1:8000
```

If you want the dashboard to read a specific SQLite DB, set `ALPHAFORGE_DATABASE_URL` before starting it, using the same value expected by `load_config_from_env()`.

## Shortcut Scripts

PowerShell shortcuts:

```powershell
.\scripts\run_backtest.ps1 -Interval 1h -Days 30 -Symbols BTCUSDT,ETHUSDT
.\scripts\run_paper.ps1
.\scripts\run_dashboard.ps1 -Port 8000
```

Bash shortcuts:

```bash
bash scripts/run_backtest.sh 1h 30 BTCUSDT,ETHUSDT
bash scripts/run_paper.sh
bash scripts/run_dashboard.sh 8000
```

These shortcuts are thin wrappers around the exact commands above. They do not bypass `.env`, migrations, readiness gates, or LIVE safeguards.

## PAPER burn-in report

Generate deterministic PAPER runtime diagnostics from a SQLite runtime database without changing thresholds or enabling live order flow:

```bash
python -m alphaforge.paper_burnin --db path/to/paper_runtime.db --out reports/paper_burnin
```

The command writes `paper_burnin_summary.csv`, `paper_burnin_report.md`, and `paper_burnin_blockers.json`. Missing evidence is reported as a blocker; this report never promotes LIVE readiness by itself.

## Next Development Priority

1. Unify `BACKTEST` / `PAPER` / `LIVE` decision lifecycle contract as much as possible.
2. Persist rejected signals/orders consistently across modes.
3. Fix lifecycle export accuracy (event ordering, statuses, and rejection visibility).
4. Ensure score/RR fields are computed from context and not hardcoded placeholders.
5. Populate execution-context fields where data exists; otherwise mark as unavailable explicitly.
6. Add regression tests for rejected lifecycle rows and lifecycle completeness.

## Adaptive Learning Foundation (Generation 9)
- AlphaForge now includes a deterministic, SQL-first adaptive learning foundation in `src/alphaforge/adaptive_learning.py`.
- This patch adds passive review persistence/analytics only (closed trade + rejected signal reviews, adaptive stats, shadow threshold recommendations).
- No unconstrained ML behavior is introduced; no active threshold application is enabled by default.

## Mode-aware configuration and Dashboard Settings

AlphaForge managed engine settings now have a typed source of truth in `src/alphaforge/config_registry.py`. Effective precedence is: process environment variables > Dashboard override file (`config/runtime_overrides.json`) > `.env.local` > `.env` > typed defaults. Dashboard Settings edits local override values only and does not write secrets.

Settings are grouped as Trade Quality Filters, Execution Cost Filters, Runtime Risk Limits, Backtest Settings, and Mode / Safety. Trade-quality filters can affect BACKTEST/PAPER/LIVE. Runtime risk limits such as `ALPHAFORGE_MAX_TRADES_GLOBAL_PER_DAY` and `ALPHAFORGE_MAX_TRADES_SYMBOL_PER_DAY` are PAPER/LIVE runtime/session controls and are ignored by BACKTEST by default; BACKTEST caps must use explicit `ALPHAFORGE_BACKTEST_*` settings. LIVE remains readiness-guarded and cannot be enabled from the generic Settings page.

### Environment audit and Windows PowerShell acceptance

The repository-root `.env` is loaded before canonical configuration; an existing process variable wins. Audit without printing secrets:

```powershell
$env:PYTHONPATH = "src"
python -m alphaforge.config_audit
if ($LASTEXITCODE -ne 0) { throw "environment contract failed" }
```

Validate production/testnet selection and explicit override precedence:

```powershell
$env:BINANCE_ENVIRONMENT = "testnet"
Remove-Item Env:BINANCE_TESTNET -ErrorAction SilentlyContinue
Remove-Item Env:BINANCE_BASE_URL -ErrorAction SilentlyContinue
Remove-Item Env:BINANCE_WS_URL -ErrorAction SilentlyContinue
python -c "from alphaforge.config import load_config_from_env; c=load_config_from_env().binance; print(c.environment,c.base_url,c.ws_url,c.resolution_source)"
$env:BINANCE_BASE_URL = "https://operator-verified-rest.example"
$env:BINANCE_WS_URL = "wss://operator-verified-ws.example"
python -m alphaforge.config_audit
```

Run matching-environment, read-only Phase 9 reconciliation preflight (enter credentials only in the current process; never commit them):

```powershell
$env:ALPHAFORGE_EXECUTION_MODE = "PAPER"
$env:ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION = "true"
$env:BINANCE_ENVIRONMENT = "testnet" # or demo with both operator-verified explicit URLs
$env:BINANCE_API_KEY = Read-Host "Read-only Binance key"
$env:BINANCE_API_SECRET = Read-Host "Read-only Binance secret"
python -m alphaforge.burnin_ops --db data/runtime/phase9.db preflight --release-id env-contract --symbols BTCUSDT --intervals 1h
Remove-Item Env:BINANCE_API_KEY,Env:BINANCE_API_SECRET
```

The command must report `PASS` and COMPLETE reconciliation evidence against the selected account environment; unavailable/mismatched evidence fails closed. It does not establish LIVE readiness.

`ALPHAFORGE_ALLOW_LIVE_ORDERS=true` is never sufficient by itself: the final adapter boundary also requires LIVE trading enablement, operator acknowledgement, qualification, clean reconciliation, and an inactive kill switch. The default is false. Prefer `ALPHAFORGE_REQUIRE_REGIME_ALIGNMENT` over deprecated `ENABLE_REGIME_FILTER`, and `ALPHAFORGE_ENABLE_ORDERBOOK_FILTER` over deprecated `ENABLE_ORDERBOOK_FILTER`.

### Canonical runtime database and PAPER reconciliation

Fresh runtime persistence defaults to `data/runtime/alphaforge_runtime.db`; repository-root `alphaforge.db` is legacy and is neither migrated nor deleted automatically. `ALPHAFORGE_DATABASE_URL` remains canonical, `ALPHAFORGE_DB_PATH` is a burn-in compatibility override, and explicit `--db` wins for supporting CLIs. Normal PAPER enables signed read-only Binance reconciliation and preflight requires usable non-placeholder credentials; this does not authorize LIVE order mutation.
