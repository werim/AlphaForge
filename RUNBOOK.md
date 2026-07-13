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
