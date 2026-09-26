# AlphaForge Full-System Production Semantics Audit

Tracking issue: #488  
Audit branch: `CHATGPT/full-system-audit`

## Audit rules

This phase is audit-only. No threshold promotion, no gate loosening, no trade-count optimization, and no silent production behavior changes. A surface is PASS only with code + test/evidence support. Otherwise use GAP, BUG, or UNPROVEN.

Severity: P0 capital/execution safety; P1 evidence/fail-closed integrity; P2 strategy/economic semantics; P3 observability/maintainability.

## Initial coverage / risk matrix

| Domain | Component / invariant | Status | Severity | Evidence / finding | Follow-up |
|---|---|---:|---:|---|---|
| Config/SSOT | Campaign thresholds/config identity are frozen and provenance-bound | PASS | P1 | Burn-in preflight/campaign identity machinery records config, strategy and execution-cost hashes; POST485 runtime evidence separately validated current MIN_RR/MIN_EFFECTIVE_RR values. | Continue cross-surface registry audit. |
| Market data | stale/future/replay/empty/provider failure fail closed | UNPROVEN | P0 | Runtime has replay/equal execution-candle suppression and provider health/recovery paths, but full failure matrix not yet traced end-to-end. | Trace source→selector→runtime→readiness tests. |
| MTF | neutral vs unavailable semantics | PASS | P1 | #484 introduced explicit MTF_REGIME_NEUTRAL and post-merge PAPER runtime produced neutral rather than unavailable. | Verify BACKTEST/LIVE_PRECHECK parity. |
| Geometry authority | UNAVAILABLE guided geometry must not expose canonical geometry/RR | PASS | P1 | #485 clears non-authoritative guided geometry metrics; fresh POST485 PAPER evidence validated NULL canonical geometry/RR. | Keep regression in golden chain. |
| Geometry authority | legacy/regime-guided 1.2-base geometry must not contaminate structural evidence | GAP | P1 | Legacy/regime-guided builders contain `1.2 + breakout_strength*25 + body/open*8`; structural MTF path derives independent stop/target. Historical pre-#485 leak demonstrated contamination risk. | Prove every production consumer distinguishes structural vs legacy/shadow authority. |
| Stop risk | STOP_TOO_TIGHT/WIDE basic planned-entry percentage arithmetic | PASS | P2 | `sl_pct=abs(candidate.entry-candidate.sl)/candidate.entry*100`; MIN/MAX gates are explicit. | Boundary/side tests still required. |
| Stop risk | executable expected-fill parity for stop-distance gate | GAP | P0 | Trade-quality stop gate uses planned `candidate.entry`, while execution RR later moves entry to expected fill. No proof stop risk is re-evaluated on executable geometry. | Define authoritative risk basis; test LONG/SHORT adverse fill cases before changing behavior. |
| Stop risk | STOP_TOO_WIDE volatility/ATR compatibility | GAP | P2 | Wide stop is fixed MAX_SL_PCT gate; ATR is checked independently, not normalized into stop-distance policy. | Economic/strategy audit; do not tune threshold during audit. |
| Stop risk | wide-stop softening is reachable and coherent | UNPROVEN | P1 | Softening requires score threshold (default/config example 9.0) plus effective RR; current score normalization has dual 0–1/0–10 behavior. Need reachable-path proof. | Add path analysis/tests; verify risk_scale is actually consumed downstream. |
| Stop risk | BACKTEST/PAPER parity | GAP | P1 | STOP_TOO_WIDE is explicitly optional/bypassable in BACKTEST but active in PAPER/LIVE unless configured otherwise. | Golden tests must distinguish intentional experiment bypass from production parity claims. |
| RR/execution | expected-fill movement not double charged | PASS | P0 | Execution-cost semantics tests cover structural→expected-fill executable RR→remaining penalty; #369/#374 evidence paths exist. | Extend persistence/resolver round-trip. |
| RR/evidence | effective_rr arithmetic and gate observed values | GAP | P1 | `validate_pre_submit_semantics` exists on dev and checks arithmetic/gate observations, but audit has not yet proven it is enforced at every production/CI boundary. | Reconcile with open #486; trace call sites and fail-closed behavior. |
| Decision invariants | identical-but-wrong cross-surface payload must fail | UNPROVEN | P1 | Cross-surface projector/parity exists; semantic validator exists, but system-golden integration/enforcement is not yet established by search. | #486 remains authoritative follow-up until proven complete. |
| Persistence | runtime decision retains semantic meaning in DB | GAP | P1 | Runtime persists canonical metrics and decision evidence; #485 exposed a prior cross-field leak. | Full compute→payload→DB→readback invariant test required. |
| Reject evidence | EXPECTED_FILL_RUNTIME_PARITY authority | PASS | P1 | Runtime/resolver/campaign/calibration explicitly distinguish EXPECTED_FILL_RUNTIME_PARITY from PLANNED_ENTRY_LEGACY/LEGACY_SCANNER_SHADOW. | Verify mature fresh POST485 outcomes when available. |
| Resolver | aligned outcomes cannot mix legacy/shadow evidence | PASS | P1 | Campaign/calibration filters require EXPECTED_FILL_RUNTIME_PARITY and attribution; SQLcheat documents same authority. | Failure/ambiguous/expiry lifecycle audit remains. |
| Qualification | freshness means evidence-hash equality, not wall-clock recency | PASS | P3 | `qualification_snapshot_fresh` compares snapshot aggregate_evidence_hash with current aggregate hash; explains young-but-stale snapshots. | Document semantics if operator confusion persists. |
| Lifecycle/recovery | restart/replay/orphan/partial/ambiguous handling | UNPROVEN | P0 | Golden fixtures and runtime/reconciliation structures exist, but coverage is not yet evidence of full production semantic correctness. | Trace each scenario through persistence and readiness. |
| System golden | required scenario inventory exists | PASS | P2 | #421 golden suite lists profitable/losing/reject, execution, stale/future, MTF, ambiguous, partial, outage, restart, orphan, resolver scenarios. | Meta-audit fixture realism and semantic assertions. |
| System golden | fixtures validate economic/cross-field relationships | GAP | P1 | Existing fixture/projection architecture can represent fixed values; must prove semantic validator is invoked, not merely projection/parity. | Integrate #486 semantics into golden/full-chain if absent. |
| Economic validation | expectancy after costs / reject quality controls promotion | UNPROVEN | P0 | Qualification/calibration machinery exists; prior evidence-quality audits correctly withheld promotion when identity/coverage failed. Full current-dev chain not yet audited. | Trace qualification formulas, eligibility and LCB/cost-drag evidence. |

## First audit conclusions

1. Do **not** remove STOP_TOO_WIDE. The safety intent is valid, but current implementation is not yet proven execution-parity correct.
2. The highest-risk stop gap is not the 1.5% value itself; it is the authority mismatch between planned-entry stop percentage and expected-fill executable geometry.
3. BACKTEST can bypass STOP_TOO_WIDE, so a green BACKTEST alone cannot establish PAPER/LIVE stop-risk correctness.
4. Semantic validation code now exists, but until call-site enforcement and golden-chain failure behavior are proven, #486 remains open and the audit marks the protection UNPROVEN/GAP.
5. Qualification `fresh=false` with a low age can be expected: freshness is evidence-hash identity, not simply elapsed seconds.

## Next audit tranche

P0/P1 first:
- execution-aware stop-risk authority and risk_scale consumption;
- semantic-invariant enforcement call sites (#486);
- market-data fail-closed chain;
- lifecycle/recovery failure injection;
- persistence and resolver round-trip authority.

No production fix should be made until the relevant audit finding is fully characterized and assigned.
