# AlphaForge Phase 7 Burn-in Runbook

## Scope
Phase 7 collects SQL-backed PAPER and non-mutating LIVE_PRECHECK evidence for cost-adjusted expectancy, regime robustness, reject quality, calibration, drawdown/loss clusters, execution degradation, concentration risk, and canary suspension.

## Explicit LIVE boundary
`ExecutionMode.LIVE` remains disabled. A `CANARY_QUALIFIED` Phase 7 verdict permits only continued non-mutating LIVE_PRECHECK operation. It never permits real order submission, cancellation, modification, or a promotion to LIVE.

## Default qualification thresholds
The typed defaults in `BurnInThresholds` require: 7 days observed duration, 500 decisions, 50 accepted trades, 30 closed trades, 50 rejected forward outcomes, 3 covered regimes, 20 samples per material regime, 50 calibration samples, lower confidence bound expectancy >= 0.01, max drawdown <= 8%, cost drag/trade <= 0.20R, slippage degradation <= 1.5x baseline, false reject rate <= 35%, calibration error <= 12%, symbol concentration <= 35%, top trade contribution <= 30%, and regime concentration <= 55%.

## Evidence requirements
1. Run writable bootstrap before writing Phase 7 evidence.
2. PAPER and LIVE_PRECHECK runtimes create immutable continuation `burnin_runs` with git commit, config hash, strategy config hash, universe hash, source provenance, symbols, intervals, duration, sample counts, and completeness statuses.
3. PAPER/LIVE_PRECHECK persist final decision observations; PAPER persists accepted closed-trade outcome evidence only when canonical lifecycle reaches `POSITION_CLOSED`. Entry fills and `POSITION_OPENED` remain open evidence and do not count toward closed-trade thresholds. Accepted closed trades require explicit spread, entry/exit slippage, fees, funding, latency, volatility, liquidity, total execution cost, net R, and net PnL. Missing critical costs block qualification.
4. Persist rejected decisions as pending observations at decision time; persist rejected forward outcomes only after the configured horizon completes with TP/SL/timeout/handled-ambiguous labels, hypothetical net R after costs, and complete evidence.
5. Persist regime, execution, calibration, drawdown, and concentration-relevant evidence before generating a qualification snapshot.

## Suspension conditions
Suspend a qualified canary when rolling/lower-bound expectancy drops below threshold, drawdown breaches, spread/slippage/latency/fill quality degrades, reject value collapses, calibration drifts, reconciliation fails, stale data/runtime errors cluster, mutation attempts occur, evidence persistence fails, operator acknowledgement expires, rollback/runbook evidence invalidates, or symbol/trade/regime concentration limits are breached. LIVE_PRECHECK stops continued canary scanning safely, persists STOPPING/suspension evidence, and keeps the mutation trap active. Multiple reason codes are preserved as separate rows.

## Dashboard interpretation
Use `/burnin` or `/api/v1/burnin/latest`. Missing evidence is shown as unavailable rather than zero. Status values are unavailable, insufficient, failed, qualified, or suspended. Blockers are authoritative and should be resolved before further canary operation.

## Exports
Use `export_burnin_evidence(db_path, output_dir, burnin_run_id)` to generate deterministic SQL-derived artifacts: `burnin_summary.json`, `burnin_qualification.json`, `burnin_regime_metrics.csv`, `burnin_execution_metrics.csv`, `burnin_reject_quality.csv`, `burnin_calibration.csv`, `burnin_drawdowns.csv`, and `burnin_suspension_events.csv`.

## Operator workflow
1. Start PAPER burn-in only after Phase 1-6 gates are healthy.
2. Verify source provenance is real/read-only exchange or PAPER runtime evidence, not synthetic.
3. Generate periodic Phase 7 snapshots.
4. Treat `BURN_IN_INSUFFICIENT` as keep collecting evidence.
5. Treat `BURN_IN_FAILED` as stop promotion and investigate blockers.
6. Treat `CANARY_QUALIFIED` as permission for continued non-mutating LIVE_PRECHECK only; LIVE_PRECHECK requires a prior release-scoped qualified PAPER snapshot and must not start a disconnected empty lineage; each restart allocates the next persisted continuation sequence and never overwrites a prior run.
7. Treat `CANARY_SUSPENDED` as fail-closed; stop canary operation and resolve all persisted reason codes.

## Remaining blockers for real LIVE
Real LIVE remains blocked until future phases define and verify a separate explicit real-order enablement process. Phase 7 evidence is necessary promotion evidence, not sufficient LIVE readiness.

## Phase 8 PAPER Burn-in Campaign Workflow

1. Create a release-scoped campaign:
   `PYTHONPATH=src python -m alphaforge.burnin_cli create --release-id <release> --duration-days 7 --symbols BTCUSDT,ETHUSDT --intervals 1h,15m`
2. Start collection:
   `PYTHONPATH=src python -m alphaforge.burnin_cli start --campaign-id <campaign>`
3. Resume after restart:
   `PYTHONPATH=src python -m alphaforge.burnin_cli resume --campaign-id <campaign>`
   Resume preserves previous run evidence, allocates the next continuation sequence, increments restart count, and records recovery evidence.
4. Check status:
   `PYTHONPATH=src python -m alphaforge.burnin_cli status --campaign-id <campaign> --json`
5. Generate qualification evidence:
   `PYTHONPATH=src python -m alphaforge.burnin_cli qualify --campaign-id <campaign>`
6. Export the evidence bundle:
   `PYTHONPATH=src python -m alphaforge.burnin_cli export --campaign-id <campaign> --output-dir artifacts/phase8`

### Config drift handling
If campaign config, strategy config, universe/timeframes, or release association changes, the campaign must pause/fail closed. Incompatible evidence must not be combined; create a new campaign for incompatible evidence.

### Reject-label resolution
Rejected candidates are persisted as pending labels with entry/stop/target, horizon, costs, regime, reason, provenance, and due time. Resolution only uses candles after the decision timestamp. Same-candle TP/SL reachability is `AMBIGUOUS`, not a win or loss.

### PAPER position outcome resolution
Open PAPER positions stay open until canonical exit evidence exists. Planned RR is not realized R. Missing exit costs mark evidence incomplete and block complete qualification evidence.

### Qualification interpretation
Campaign completion and qualification are separate. `COMPLETED + BURN_IN_FAILED` is valid. `CANARY_QUALIFIED` is not LIVE readiness and does not enable real orders.

### Recovery procedures
After unclean shutdown, run `resume` with the same campaign ID. Inspect dashboard `/campaign` or `/api/v1/burnin/campaign` for restart count, active run, pending backlog, blockers, warnings, last heartbeat, and last error.

### Remaining blockers for real LIVE
Real LIVE remains disabled until lifecycle integrity, reject quality, persistence integrity, reconciliation, execution realism, and sustained PAPER qualification are independently verified.

### Phase 8 PR 275 Operational Patch
Campaign qualification must be generated from all compatible continuation runs through the aggregate Phase 7 qualification path. Operators must not treat metric-row presence as sufficient evidence; all Phase 7 blockers remain authoritative at campaign scope.

The campaign worker resolver tick should run periodically during PAPER burn-in. It resolves due pending reject labels using canonical post-decision candles, records resolver batch events, triggers qualification, and pauses the campaign if resolver failures exceed the configured threshold.

Rejected candidates with missing entry, stop, target, side, decision timestamp, horizon, or execution-cost assumptions are incomplete evidence. They must not receive guessed stop/target geometry and must not count as completed forward outcomes.

### Phase 8 PR 278 Worker Operation
Run foreground campaign workers with one persistence backend. The worker must start runtime, resolver, and maintenance loops together; resolver progress must not depend on manual `resolve` commands.

Before runtime starts, campaign attachment compares runtime release/config/strategy/universe/execution-cost hashes and requires PAPER mode. Any mismatch pauses the campaign and blocks startup with a Phase 8 campaign drift reason.

Detached worker invocations using `--db` must point to the campaign database so campaign state, runtime observations, pending labels, resolver events, and qualification snapshots remain in one lineage.

### Phase 8 PR 279 CLI Worker Semantics
Use `start --foreground` for an in-process PAPER campaign worker or `start --detach` for a subprocess worker. `resume` supports the same flags. A start/resume without a worker mode fails closed and must not leave the campaign `RUNNING`.

Continuation allocation belongs to start/resume. Worker processes attach to the active campaign run and must not allocate a second continuation. Detached workers must receive the exact `--db` path used by the operator.

A worker invoked without an active campaign run is invalid. Operators must create a campaign and use `start --foreground`, `start --detach`, `resume --foreground`, or `resume --detach` so the continuation exists before worker attachment.

### Phase 8 PR 279 Canonical Identity and Market Data
Campaign identity must be built with the shared Phase 8 identity helper from the same runtime configuration used at attachment time. CLI-created campaigns load the current environment config before hashing.

Resolver workers must use a read-only canonical market-data provider. Provider outages are not completed evidence and must not be converted into expired outcomes. Only a genuine empty completed market window may become `EXPIRED` with explicit `NO_CANDLES_IN_MARKET_WINDOW` evidence.

### Phase 8 PR 279 Effective PAPER Slippage Identity
Campaign identity must include the effective PAPER slippage used by the runtime simulator. Operators must create a new campaign if PAPER slippage settings change; runtime attachment will pause/refuse an existing campaign with `PHASE8_CAMPAIGN_EXECUTION_COST_DRIFT` rather than mixing incompatible execution-cost evidence.

## Phase 9 PAPER Burn-in Operations

Phase 9 adds production-like PAPER burn-in orchestration only. It never enables LIVE order submission and every release decision is limited to incomplete, failed, suspended, or qualified-for-canary-review.

Canonical Linux/macOS commands:

```bash
python -m alphaforge.burnin_ops preflight --release-id phase9-YYYYMMDD --symbols BTCUSDT,ETHUSDT --intervals 1h
python -m alphaforge.burnin_ops launch --release-id phase9-YYYYMMDD --duration-days 7 --symbols BTCUSDT,ETHUSDT --intervals 1h --detach
python -m alphaforge.burnin_ops status --campaign-id <campaign-id>
python -m alphaforge.burnin_ops health --campaign-id <campaign-id>
python -m alphaforge.burnin_ops watch --campaign-id <campaign-id>
python -m alphaforge.burnin_ops recovery-drill --campaign-id <campaign-id>
python -m alphaforge.burnin_ops pause --campaign-id <campaign-id>
python -m alphaforge.burnin_ops resume --campaign-id <campaign-id>
python -m alphaforge.burnin_ops audit --campaign-id <campaign-id>
python -m alphaforge.burnin_ops report --campaign-id <campaign-id> --output-dir artifacts/burnin/<campaign-id>
python -m alphaforge.burnin_ops finalize --campaign-id <campaign-id> --output-dir artifacts/burnin/<campaign-id>/final
```

Windows PowerShell equivalents:

```powershell
python -m alphaforge.burnin_ops preflight --release-id phase9-YYYYMMDD --symbols BTCUSDT,ETHUSDT --intervals 1h
python -m alphaforge.burnin_ops launch --release-id phase9-YYYYMMDD --duration-days 7 --symbols BTCUSDT,ETHUSDT --intervals 1h --detach
python -m alphaforge.burnin_ops status --campaign-id <campaign-id>
python -m alphaforge.burnin_ops health --campaign-id <campaign-id>
python -m alphaforge.burnin_ops watch --campaign-id <campaign-id>
python -m alphaforge.burnin_ops recovery-drill --campaign-id <campaign-id>
python -m alphaforge.burnin_ops pause --campaign-id <campaign-id>
python -m alphaforge.burnin_ops resume --campaign-id <campaign-id>
python -m alphaforge.burnin_ops audit --campaign-id <campaign-id>
python -m alphaforge.burnin_ops report --campaign-id <campaign-id> --output-dir artifacts/burnin/<campaign-id>
python -m alphaforge.burnin_ops finalize --campaign-id <campaign-id> --output-dir artifacts/burnin/<campaign-id>/final
```

Default campaign profile: PAPER execution mode, Binance Futures read-only klines, one canonical interval such as `1h`, bounded USDT symbol universe, no forced acceptance, no diagnostic threshold relaxation, no ALL_OFF/rescue profile, and real execution-cost identity from runtime configuration. Blocking preflight failures prevent startup.

### Phase 9 PR 280 hardened operator notes

Detached launch is successful only after worker attachment evidence is present: live PID, `PHASE8_CAMPAIGN_ATTACHED` after launch start, runtime instance ID, heartbeat at or after worker start, and active run parity. Use `--attach-timeout-seconds` to adjust the wait window in slow environments:

```bash
python -m alphaforge.burnin_ops launch --release-id phase9-YYYYMMDD --duration-days 7 --symbols BTCUSDT,ETHUSDT --intervals 1h --detach --attach-timeout-seconds 120
```

PowerShell:

```powershell
python -m alphaforge.burnin_ops launch --release-id phase9-YYYYMMDD --duration-days 7 --symbols BTCUSDT,ETHUSDT --intervals 1h --detach --attach-timeout-seconds 120
```

Finalization can qualify only canonical `CANARY_QUALIFIED` Phase 8 qualification snapshots, with completion, integrity, aggregate-hash linkage, healthy state, and bounded backlog all passing. `PASS` or `QUALIFIED` aliases do not qualify a campaign for canary review.
# Safe configuration remediation (PowerShell)

The fixer is dry-run by default and reports every proposed mutation without
printing credential values. It modifies only the repository `.env`; apply mode
creates a timestamped backup and uses atomic replacement.

```powershell
# Review the deterministic plan
python -m alphaforge.config_fix

# Apply only AUTO_FIX_SAFE actions
python -m alphaforge.config_fix --apply

# Verify the complete contract
python -m alphaforge.config_check

# After PASS, exercise read-only reconciliation
python -m alphaforge.binance_reconciliation_check --symbols BTCUSDT ETHUSDT
```

Process environment has precedence over `.env`. The fixer will not pretend an
`.env` edit can remove such an override; inspect and clear it in the current
PowerShell session, then rerun the dry-run:

```powershell
Get-ChildItem Env:BINANCE*
Remove-Item Env:BINANCE_TESTNET -ErrorAction SilentlyContinue
```

Secret-bearing variable clear commands are intentionally never printed.
