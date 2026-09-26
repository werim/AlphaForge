# AlphaForge Full-System Production Semantics Audit

Tracking issue: #488  
Audit branch: `CHATGPT/full-system-audit`

## Executive status

Audit phase is read-only: no threshold promotion, gate loosening, target manipulation or trade-count optimization.

**Current verdict: NOT LIVE READY.** Real LIVE order submission remains disabled by the current Phase6 runtime path, which limits present blast radius. PAPER remains useful for evidence generation, but current burn-in qualification/readiness evidence has P0 gaps that must be fixed before any promotion claim.

### Findings opened by this audit

| Issue | Severity | Finding |
|---|---:|---|
| #489 | P0 | STOP_TOO_WIDE softening emits risk_scale, BACKTEST applies it, PAPER/LIVE runtime notional does not. |
| #492 | P0 | Stop-distance safety uses planned entry while RR uses executable expected-fill geometry. |
| #493 | P0 | Burn-in execution qualification can PASS with NULL/unmeasured spread/slippage/latency/fill degradation evidence. |
| #494 | P0 | Burn-in drawdown writer can encode unknown drawdown as synthetic 0% + resolved, allowing PASS. |
| #490 | P1 | Readiness parity proves surface presence/limited equality, not protected capital-risk semantics. |
| #491 | P1 | PAPER candidate notional is execution-affecting but outside canonical config-registry SSOT. |
| #495 | P1 | decision_evidence.raw_rr means candidate RR in BACKTEST but executable RR in PAPER runtime. |
| #496 | P1 | Duplicate portfolio-risk engines: production uses portfolio_risk.py; adaptive portfolio_risk_engine.py appears test-only/non-authoritative. |
| #497 | P1 | Documented shared trade-quality authority diverges from RuntimeOrchestrator production gate implementation. |
| #486 | P1 | Existing open semantic-invariant issue remains required: production-wide enforcement is incomplete/unproven. |

## Audit principles

A surface is PASS only with code plus test/evidence support. Otherwise it is BUG, GAP or UNPROVEN.

Severity:
- P0: capital/execution safety or qualification can materially misrepresent readiness.
- P1: evidence/fail-closed/authority integrity.
- P2: strategy/economic semantics.
- P3: observability/maintainability.

## Coverage / risk matrix

| Domain | Protected invariant | Status | Sev | Evidence / conclusion |
|---|---|---:|---:|---|
| LIVE mutation boundary | Real orders cannot be submitted by current Phase6 runtime | PASS | P0 | LIVE startup invokes explicit real-order disable path; mutation-trap/kill-switch/no-submit tests exist; changelog states LIVE remains NOT LIVE-READY. |
| Market-time integrity | malformed, stale, materially future or replayed execution candles cannot trade | PASS | P0 | Production `_process_symbol` tests cover malformed/future/stale/epoch-unit timestamps; equal/replayed closed candles are suppressed. |
| Provider outage | provider failure is not silently converted into normal empty market | PASS | P0 | Public scan supervision distinguishes provider failure; transient grace/recovery and fail-closed recovery tests exist. |
| MTF authority | neutral regime is distinct from unavailable evidence | PASS | P1 | #484 introduced MTF_REGIME_NEUTRAL; post-merge PAPER validation observed correct semantics. |
| Structural geometry | canonical guided MTF stop/target are independent market structure, not MIN_RR target construction | PASS | P1 | `build_structural_geometry_with_diagnostics` computes reward/risk from entry/structural stop/target and deliberately excludes MIN_RR. |
| Legacy geometry | legacy 1.2-base formula cannot become authoritative guided evidence | PASS post-#485 / historical contamination | P1 | 1.2 formula remains in legacy/scanner geometry; canonical guided MTF uses MTF_SETUP_STRUCTURE. #485 clears leaked non-authoritative guided fields. |
| Stop gate arithmetic | planned stop % is computed deterministically | PASS | P2 | `abs(entry-stop)/entry*100`, explicit MIN/MAX checks. |
| Stop gate execution basis | stop viability must reflect executable expected-fill risk | **BUG #492** | P0 | RR chain moves entry to expected fill; stop gate remains planned-entry based. |
| Wide-stop scaling | softened wide stop must actually reduce capital at risk | **BUG #489** | P0 | BACKTEST applies diagnostics risk_scale to notional; RuntimeOrchestrator PAPER path uses unscaled candidate_notional. |
| Wide-stop BACKTEST parity | BACKTEST experiments cannot masquerade as PAPER policy | GAP | P1 | STOP_TOO_WIDE can be disabled/bypassed in BACKTEST only. Valid research feature, but parity claims must exclude bypass runs. |
| Stop/volatility relation | fixed stop policy vs ATR/volatility is economically justified | UNPROVEN | P2 | MAX_SL_PCT and ATR gates are independent. No evidence yet that 1.5% is optimal; audit does not tune it. |
| RR geometry | expected-fill geometry adjusts raw RR before residual costs | PASS | P0 | #369 semantics and tests prove structural→expected-fill executable RR. |
| RR cost arithmetic | expected-entry movement is not double charged | PASS | P0 | Remaining penalty excludes the entry movement already embedded in executable geometry; tests cover no-double-count. |
| Execution evidence | missing/nonfinite/fake-zero execution context fails closed at decision boundary | PASS | P0 | `evaluate_execution_safety`, INVALID_FAKE_ZERO and LIVE_PRECHECK blocking tests cover this. |
| Burn-in execution qualification | qualification PASS requires measured degradation evidence | **BUG #493** | P0 | Periodic writer stores NULL execution metrics with STABLE when reconciliation CLEAN; qualification does not block NULL ratios. |
| Burn-in drawdown qualification | unknown drawdown cannot equal measured zero drawdown | **BUG #494** | P0 | Periodic writer stores NULL peak/trough + drawdown_pct=0 + resolved=1; qualification interprets zero/resolved. |
| Closed-trade expectancy | incomplete cost evidence cannot enter qualification expectancy | PASS | P0 | Closed trades require evidence_complete, gross/net R, total execution cost and all CRITICAL_COST_FIELDS; missing evidence blocks qualification. |
| Expectancy promotion | positive point estimate alone is insufficient | PASS | P0 | Qualification requires lower-confidence-bound expectancy and bounded cost drag. |
| Reject quality | legacy/shadow/infrastructure rejects cannot contaminate aligned reject calibration | PASS | P1 | EXPECTED_FILL_RUNTIME_PARITY + attribution filters exist in resolver/campaign/calibration; identity linking is strict. |
| Reject forward window | no pre-decision/future-window leakage; gaps/duplicates/malformed data fail incomplete | PASS | P1 | Resolver uses `decision_ts < candle_ts <= due_at`, validates closed candles, gaps and duplicate conflicts. |
| Ambiguous TP/SL | ambiguous outcome cannot be forced into correct/incorrect reject evidence | PASS | P1 | Ambiguous outcome remains separate and evidence/attribution logic avoids binary correctness. |
| Resolver identity | retries/orphans/cross-campaign rows cannot silently count | PASS | P1 | Canonical reject decision + pending label + campaign/run link required; conflicts fail. |
| Portfolio state | restart restores exposure/daily PnL/drawdown/loss streak/trade counts from DB | PASS | P0 | PAPER portfolio risk-state tests rebuild from campaign evidence and fail closed on incomplete state. |
| Portfolio sizing authority | one production risk engine exists and is clearly authoritative | **GAP #496** | P1 | `portfolio_risk.py` is production; `portfolio_risk_engine.py` adaptive sizing is apparently only directly consumed by tests. |
| Candidate sizing SSOT | execution-affecting candidate notional is canonical config/provenance | **GAP #491** | P1 | Runtime paper_candidate_notional default is material but absent from config_registry. |
| Shared decision authority | protected gates have one implementation across modes | **GAP #497** | P1 | Runtime duplicates several quality gates rather than consuming one authoritative trade-quality decision path. |
| Semantic invariants | internally impossible evidence fails even when surfaces agree | **GAP #486** | P1 | Validator exists and parity calls it, but no production-wide single-surface enforcement call site was found. |
| Decision evidence RR stages | candidate/executable/effective RR retain distinct meanings | **BUG #495** | P1 | decision_evidence.raw_rr is candidate RR in BACKTEST and executable RR in runtime. |
| Readiness mode parity | PASS means equal protected risk/execution semantics | **GAP #490** | P1 | Existing mode parity compares a limited field set; portfolio “shared engine” check can pass from row presence. |
| Persistence failure | failed canonical decision persistence blocks further execution | PASS | P0 | Runtime sets PHASE7_BURNIN_PERSISTENCE_FAILURE; reconciliation execution blocker consumes fail_closed_reason. |
| Partial fills | exposure uses actual partial filled quantity/notional | PASS | P0 | Full-chain tests verify partial notional and active-position state. |
| Reconciliation/orphans | unknown exchange state/orphans/recovery block execution | PASS | P0 | Runtime reconciliation blocker + full-chain/provider recovery tests cover fail-closed behavior. |
| Qualification freshness | fresh means current aggregate evidence identity, not merely young timestamp | PASS | P3 | freshness is aggregate evidence-hash equality; low age can correctly be stale after evidence changes. |
| Runtime metric semantics | decisions_generated equals all canonical decisions | GAP | P3 | Counter increments after AIBrain decision; pre-AI canonical rejects can exist while decisions_generated=0. Name is misleading, not decision-loss evidence. |
| Calibration | missing calibration evidence cannot PASS | PASS | P1 | no rows => sample 0 / worst error sentinel; insufficient/fail closed. |
| Regime qualification | UNKNOWN regime cannot be declared PASS | PASS | P1 | explicit UNKNOWN_REGIME_CANNOT_PASS blocker. |
| System golden scenario inventory | major failure modes are represented | PASS | P2 | #421 fixtures include stale/future, execution unavailable, MTF conflict, ambiguous, partial fill, outage, restart, orphan, delayed resolver. |
| Golden semantic depth | green fixtures prove all internal economic relationships | GAP | P1 | #486/#490 show fixture/parity checks can miss semantics not projected into protected contract. |

## Architecture map

Current authoritative PAPER decision/evidence chain:

`PUBLIC market scanner`
→ market timestamp validation / replay suppression
→ symbol selection
→ MTF 1h regime / 15m setup structure / 1m execution confirmation
→ structural entry/stop/target
→ candidate RR
→ expected-fill executable geometry
→ executable raw RR
→ remaining execution penalties
→ effective RR
→ execution safety
→ runtime quality/stop gates
→ portfolio-risk hard gates
→ PAPER simulation
→ pending position/reject queues
→ resolver
→ trade/reject outcomes
→ qualification
→ live-readiness/audit.

### Authority fractures discovered

1. Stop risk remains planned-entry while RR is executable-entry aware (#492).
2. Wide-stop risk scaling is not propagated into production notional (#489).
3. Runtime and shared order evaluator duplicate decision policy (#497).
4. Two portfolio-risk modules look authoritative but only one is production-connected (#496).
5. decision_evidence overloads raw_rr with different meanings (#495).
6. readiness parity does not compare all protected risk semantics (#490).
7. qualification execution/drawdown rows can encode missing evidence as healthy/zero (#493/#494).

These fractures explain why individual unit tests can be green while end-to-end semantics remain unsafe.

## STOP_TOO_WIDE audit conclusion

**Do not remove STOP_TOO_WIDE. Do not tune 1.5% from this audit.**

The gate protects a valid risk concern. The current problem is authority and sizing, not proof that the threshold is too strict.

Required order of operations:
1. fix executable stop-risk basis (#492);
2. make softening actually scale production notional (#489);
3. unify decision authority (#497);
4. collect fresh authoritative PAPER evidence;
5. only then evaluate whether MAX_SL_PCT/softening thresholds improve expectancy after costs.

A 17R candidate with STOP_TOO_TIGHT/WIDE can still be a bad executable trade; raw RR alone does not justify bypassing stop-risk controls.

## RR ~1.20 historical clustering conclusion

The historical ~1.20–1.23 cluster came from rows with `geometry_status=UNAVAILABLE` under the pre-#485 worker, not authoritative MTF_SETUP_STRUCTURE. The legacy/regime-guided formula contains a 1.2 additive base. #485 prevents those leaked shadow values from appearing as canonical authoritative guided geometry in fresh runtime.

Therefore the old cluster must **not** be used to infer current market structural RR distribution or to tune MIN_RR.

## Qualification audit conclusion

Qualification has strong foundations:
- minimum duration/sample gates;
- complete-cost closed-trade eligibility;
- LCB expectancy;
- cost drag;
- reject identity/attribution;
- regime coverage;
- calibration;
- reconciliation;
- release/runbook/rollback/full-test evidence.

But #493 and #494 are promotion blockers: execution degradation and drawdown evidence can currently look healthier than the underlying measurements justify.

Until those are fixed and revalidated with fresh evidence, a CANARY qualification should not be treated as sufficient proof of production execution quality.

## Current priority order

### P0 — must close before LIVE promotion
1. #493 measured execution-quality qualification.
2. #494 measured drawdown qualification.
3. #492 executable stop-risk basis.
4. #489 production risk_scale/notional propagation.

### P1 — must close before trusting parity/readiness
5. #497 single decision authority.
6. #490 semantic mode parity/readiness.
7. #486 production-wide internal semantic invariants.
8. #495 explicit RR-stage persistence.
9. #496 single portfolio-risk authority.
10. #491 candidate-notional SSOT.

### P2/P3 after safety/evidence closure
- empirical stop/ATR/volatility policy calibration;
- metric naming/observability cleanup;
- legacy/dead-path quarantine;
- documentation consolidation.

## What is already strong

The audit did **not** find a system that is broadly unsafe. Several important defenses are real and tested:
- LIVE mutation remains disabled;
- kill switch and reconciliation are fail closed;
- market timestamp integrity is strong;
- provider outage recovery is explicit;
- expected-fill/effective-RR cost semantics are materially improved;
- missing/fake-zero decision-time execution context is blocked;
- reject resolver identity and time windows are conservative;
- incomplete trade cost evidence cannot enter expectancy qualification;
- portfolio state survives restart and incomplete state can block;
- legacy/shadow reject evidence is separated from aligned calibration.

The main risk pattern is narrower and architectural: **duplicate authorities and evidence placeholders can make a green surface look more authoritative than the actual executable semantics.**

## Promotion rule

No threshold promotion or LIVE progression should be justified from current burn-in evidence until all P0 findings above are closed and a fresh post-fix campaign proves:
- measured execution degradation evidence;
- measured drawdown evidence;
- executable stop-risk parity;
- scaled notional semantics;
- semantic mode parity;
- clean persistence/reconciliation;
- positive LCB expectancy after complete costs.

This audit optimizes for surviving bad evidence, not generating more trades.
