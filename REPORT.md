## 2026-05-22 Patch Addendum — Minimal follow-up: startup incident persistence rollback + defensive evidence parsing

### Why the patch was needed
- LIVE qualification startup was persisting reconciliation findings into `reconciliation_incidents`, creating false operational history during preflight gating.
- Mode parity numeric parsing could raise on malformed evidence payloads and risk aborting readiness flow instead of persisting fail-closed reports.
- Forensic sanitation over-redacted benign keys containing `signed`, including legitimate metadata.

### Root cause
- `_run_live_qualification_gate()` persisted canonical findings unconditionally when provider evidence was COMPLETE.
- `_check_runtime()` used direct `int(...)` casts for evidence counters.
- `_sanitize_runtime_snapshot()` blocked keys using substring `signed` rather than sensitive key semantics/value redaction.

### Files changed
- `src/alphaforge/runtime.py`
- `src/alphaforge/live_readiness.py`
- `tests/test_live_readiness.py`
- `tests/test_live_readiness_security_regression.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- LIVE qualification startup still fails closed on canonical reconciliation findings (orphan/duplicate/fail-closed) but no longer writes startup findings into `reconciliation_incidents`.
- Qualification startup now explicitly reports `incident_persistence_verified=false`.
- Invalid parity numeric evidence values (`None`, `''`, `N/A`, malformed strings) now fail closed without exceptions; readiness report persistence continues.

### Lifecycle/persistence/schema impact
- No lifecycle transition changes.
- No schema changes.
- `live_readiness_reports` persistence remains intact even for invalid parity evidence payloads.

### Tests added/updated
- Added fail-closed parity parsing regression with persisted readiness report.
- Added LIVE qualification regression using canonical orphan/duplicate snapshot and asserting no incident rows written at startup.
- Strengthened forensic redaction regression to assert `assigned_symbols` retention and signed/auth/signature redaction.

### Risks / remaining limitations
- LIVE still not ready; observability evidence remains intentionally blocking without complete measured proof.
- This patch does not alter scoring, RR, thresholds, trade frequency, adapter behavior, or order submission paths.

### Push recommendation
- Merge as minimal follow-up patch restoring startup persistence semantics while preserving fail-closed LIVE qualification.

## 2026-05-22 Patch Addendum — Evidence-based parity/operational readiness checks

### Why the patch was needed
- LIVE readiness still accepted placeholder booleans for parity/observability/rollback without persisted, measurable operational evidence.

### Root cause
- `LiveReadinessEvaluator` runtime/operational checks were boolean shortcuts (`all(mode_parity.values())`, `alerts_configured`, `rollback_ready`) with no structured evidence sufficiency contract.

### Files changed
- `src/alphaforge/live_readiness.py`
- `src/alphaforge/runtime.py`
- `tests/test_live_readiness.py`
- `VERSION.md`
- `CHANGELOG.md`
- `REPORT.md`

### Runtime behavior changes
- Mode parity now fail-closes unless evidence is COMPLETE and satisfies minimum sample, zero mismatch, zero missing-field, and no-submit verification constraints.
- Observability and rollback checks now require explicit measured evidence fields rather than static booleans.
- Forensic snapshot export now sanitizes runtime snapshot keys that look like credentials/signatures/auth headers.

### Lifecycle/persistence/schema impact
- No schema changes.
- Existing `live_readiness_reports.report_payload` persists structured evidence details safely.

### Security/execution safety
- No real order submission/cancel/modify/close path introduced.
- No exchange mutation path added.
- Reconciliation remediation posture remains dry-run/non-mutating.

### Remaining limitations / blockers
- Alert delivery verification is not implemented and remains an explicit readiness blocker.
- Real execution readiness remains unavailable.
- LIVE remains NOT LIVE-READY.

### Push recommendation
- Merge as minimal fail-closed evidence hardening increment.

## 2026-05-22 Patch Addendum — LIVE canonical reconciliation evidence-chain hardening

### Why the patch was needed
- LIVE qualification consumed provider snapshot fields directly and could trust optimistic orphan/duplicate counters without canonical runtime-intent comparison.

### Root cause
- Canonical reconciliation ownership was split: provider returned summary counters while readiness gate relied on those counters instead of reconciliation findings produced by AlphaForge runtime logic.

### Files changed
- `src/alphaforge/runtime.py`
- `src/alphaforge/reconciliation.py`
- `src/alphaforge/live_readiness.py`
- `tests/test_reconciliation.py`
- `tests/test_live_readiness.py`
- `tests/test_runtime.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- LIVE qualification now treats authenticated provider as raw read-only exchange evidence source only.
- LIVE qualification converts provider snapshot to canonical reconciliation snapshot and runs `ReconciliationEngine.reconcile(...)` against runtime intended orders/lifecycle state.
- Provider-supplied `orphan_orders` / `orphan_positions` / `duplicate_fills` values are ignored for qualification decisions.
- LIVE readiness now fails closed when provider evidence is incomplete and when canonical fail-closed findings are present.

### Lifecycle/persistence/schema impact
- No schema changes.
- Reconciliation findings continue to persist through existing `reconciliation_incidents` persistence layer, including duplicate-fill incidents.

### Security/redaction impact
- No API keys/secrets/signatures added to incident payloads; persisted payloads contain only normalized safe reconciliation evidence.

### Remaining limitations / blockers
- Remediation suggestions remain dry-run/operator-review only.
- No order create/cancel/modify/close behavior introduced.
- LIVE remains blocked by broader readiness requirements and missing production execution/operational evidence.

### Push recommendation
- Merge as minimal fail-closed P0/P1 patch.

## 2026-05-22 Patch Addendum — Authenticated Binance READ-ONLY reconciliation provider

### Why the patch was needed
- LIVE qualification/readiness required authenticated reconciliation provider evidence, but no provider existed.

### Root cause
- The runtime had a reconciliation provider contract and fail-closed requirement, but no authenticated Binance USER_DATA implementation.

### Files changed
- `src/alphaforge/binance_reconciliation_provider.py`
- `src/alphaforge/runtime.py`
- `src/alphaforge/config.py`
- `src/alphaforge/config/__init__.py`
- `tests/test_binance_reconciliation_provider.py`
- `tests/test_runtime_env_config.py`
- `.env.example`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- Added read-only Binance reconciliation snapshot support with signed GET-only USER_DATA calls.
- Runtime wires provider only in LIVE when `ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION=true` and complete credentials are configured.
- LIVE fails closed with explicit missing/partial credential errors when reconciliation is enabled but credentials are incomplete.

### Security/redaction behavior
- API secret and signature are never persisted in evidence snapshots.
- Failure payloads are sanitized to class-level redacted errors.

### Orphan coverage strategy
- Uses global `/fapi/v3/positionRisk` and global `/fapi/v1/openOrders` to preserve orphan discovery capability.
- Uses bounded symbol-scoped `/fapi/v1/userTrades` only for tracked/open-position symbols.

### Lifecycle/persistence/schema impact
- No schema changes.
- No execution-path changes.

### Tests executed
- `pytest -q tests/test_binance_reconciliation_provider.py tests/test_runtime_env_config.py::test_live_reconciliation_enabled_requires_credentials`

### Remaining limitations / blockers
- No real order submission adapter exists.
- Mode parity/observability/rollback readiness evidence remains unverified.
- LIVE remains blocked/not ready by design.

### Push recommendation
- Merge as minimal authenticated read-only reconciliation evidence increment.

## 2026-05-22 Patch Addendum — LIVE qualification evidence fail-closed + scanner/reconciliation provenance hardening

### Why the patch was needed
- LIVE qualification still used optimistic hardcoded evidence payloads that could pass checks without measured runtime proof.
- LIVE reconciliation logic used in-memory runtime state snapshots only, which is insufficient as exchange-state evidence.
- Runtime bootstrap referenced `scanner_source` at construction time without deterministic assignment on all paths.

### Root cause
- `_run_live_qualification_gate()` supplied static pass-biased snapshots for mode parity, reconciliation, and observability.
- `_reconcile_runtime_state()` always built snapshots from `_pending_orders`/`_active_positions` regardless of mode.
- `_build_runtime_from_env()` passed `scanner_source` without guaranteed initialization.
- LIVE startup scanner checks were blacklist-based; UNKNOWN/unverified provenance could remain ambiguous.

### Files changed
- `src/alphaforge/runtime.py`
- `src/alphaforge/live_readiness.py`
- `tests/test_runtime.py`
- `tests/test_live_readiness.py`
- `tests/test_exchange_connectivity.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- LIVE startup now requires explicit allowlisted scanner provenance and blocks unverified/unknown sources fail-closed.
- Runtime bootstrap now assigns deterministic scanner provenance (`SAFE_PLACEHOLDER` for safe override, otherwise `EXCHANGE_PUBLIC_MARKET_DATA`).
- LIVE qualification now uses fail-closed evidence defaults and records explicit missing evidence reasons:
  - `MODE_PARITY_UNVERIFIED`
  - `LIVE_RECONCILIATION_PROVIDER_MISSING`
  - `OBSERVABILITY_EVIDENCE_UNVERIFIED`
  - `ROLLBACK_EVIDENCE_UNVERIFIED`
- LIVE reconciliation now requires an explicit reconciliation provider and blocks when absent.

### Lifecycle/persistence/schema impact
- No lifecycle schema changes.
- No persistence schema rewrite; readiness report payload now carries explicit missing-evidence details in existing `live_readiness_reports` table.

### Tests added/updated
- Added/updated scanner provenance and bootstrap determinism tests.
- Added fail-closed readiness evidence tests including persisted report detail checks.
- Updated LIVE connectivity runtime tests to set explicit allowlisted scanner provenance when testing connectivity gate behavior.

### Risks / limitations
- No authenticated exchange snapshot provider was introduced in this patch.
- LIVE remains intentionally blocked until real reconciliation provider evidence is available.
- No order placement capability was added.

### Push recommendation
- Merge as minimal P0 fail-closed hardening before any further LIVE enablement work.

## 2026-05-22 Patch Addendum — P0 LIVE startup scanner/adapter guards + Binance Futures gate consistency

### Why the patch was needed
- LIVE startup safety checks could be bypassed by runtime scanner wrapper indirection and did not fail early when no real execution adapter existed.
- Binance runtime scanner used Futures endpoints while config default/connectivity checks could still validate Spot assumptions.

### Root cause
- LIVE scanner guard relied on function `__name__` rather than resolved scanner provenance.
- LIVE adapter guard existed only inside execution path, after loops started.
- Binance default host and connectivity probe endpoint family were inconsistent with Futures runtime scanner endpoints.

### Files changed
- `src/alphaforge/runtime.py`
- `src/alphaforge/config/__init__.py`
- `src/alphaforge/exchange_connectivity.py`
- `tests/test_runtime.py`
- `tests/test_config_layer.py`
- `tests/test_exchange_connectivity.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- LIVE startup now blocks when resolved scanner source is safe/placeholder/mock/offline/synthetic and raises:
  - `LIVE mode blocked: safe/placeholder market scanner is not allowed`
- LIVE startup now blocks pre-loop when `real_execution_adapter` is missing and raises:
  - `LIVE mode blocked: real execution adapter is not configured`
- Binance connectivity now validates Futures endpoints (`/fapi/v1/ticker/bookTicker`, `/fapi/v1/premiumIndex`, optional `/fapi/v1/time`) and only marks connected when Futures orderbook+funding checks pass.
- Binance default base URL now resolves to `https://fapi.binance.com` when `BINANCE_BASE_URL` is unset.

### Lifecycle/persistence/schema impact
- No lifecycle schema changes.
- No persistence schema changes.

### Tests added/updated
- Added/updated regression tests for LIVE scanner-wrapper block, LIVE missing adapter startup block, Binance Futures default host, Futures-only connectivity endpoint checks, funding fail-closed behavior, and Spot-only non-qualification.
- Updated stale runtime expectation tests to validate effective behavior rather than wrapper function-name assumptions.

### Tests executed
- `pytest -q tests/test_runtime.py`
- `pytest -q tests/test_config_layer.py`
- `pytest -q tests/test_exchange_connectivity.py`
- `pytest -q tests/test_exchange_market_scanner.py`
- `pytest -q`

### Risks / limitations
- This patch does not introduce real order submission and does not change acceptance thresholds.
- LIVE readiness remains blocked by additional unresolved requirements outside this P0 patch.

### Push recommendation
- Merge as a minimal fail-closed safety patch before further LIVE transition work.

## 2026-05-22 Patch Addendum — Binance Futures bookTicker spread derivation hardening

### Why the patch was needed
- Binance scanner still used Spot `/api/v3/ticker/24hr` and relied on ticker bid/ask fields directly, leaving Futures consistency and spread provenance weaker than intended.

### Root cause
- Scanner endpoint mix was split between Spot and Futures families and did not explicitly require Futures `bookTicker` for spread derivation.

### Files changed
- `src/alphaforge/exchange_market_scanner.py`
- `tests/test_exchange_market_scanner.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- Binance scan now uses Futures endpoints consistently:
  - `/fapi/v1/ticker/24hr`
  - `/fapi/v1/ticker/bookTicker`
  - `/fapi/v1/premiumIndex`
- `entry` now uses conservative price selection `min(last_price, mid)` where `mid=(bid+ask)/2`.
- `spread_pct` and `spread_bps` are now derived from `bookTicker` bid/ask only.
- If `bookTicker` data is unavailable or malformed for a symbol, that symbol is skipped (fail-closed; no optimistic spread synthesis).
- PAPER/LIVE runtime wiring remains unchanged from v2.

### Lifecycle/persistence/schema impact
- No lifecycle changes.
- No persistence schema changes.

### Tests added/updated
- Updated scanner tests to cover Futures endpoint family, spread mapping from `bookTicker`, malformed payload fail-closed behavior, and deterministic URL assertions.

### Tests executed
- `pytest -q tests/test_exchange_market_scanner.py tests/test_runtime.py::test_build_runtime_uses_exchange_scanner_for_paper tests/test_runtime.py::test_build_runtime_keeps_safe_scanner_for_backtest`

### Risks / limitations
- Binance symbol coverage may decrease temporarily when `bookTicker` is incomplete for some symbols; this is intentional fail-closed behavior.
- Hyperliquid support remains mids-only as in v2.

### Push recommendation
- Recommended to merge as a small safe follow-up focused on spread realism and endpoint consistency.

## 2026-05-21 Patch Addendum — PAPER/LIVE read-only exchange scanner alignment

### Why the patch was needed
- Runtime PAPER/LIVE bootstrap scanner used deterministic placeholder BTC input, preventing real exchange market-data rehearsal.

### Root cause
- `_build_runtime_from_env()` always wired `_safe_market_scanner` regardless of execution mode.

### Files changed
- `src/alphaforge/runtime.py`
- `src/alphaforge/exchange_market_scanner.py`
- `tests/test_runtime.py`
- `tests/test_exchange_market_scanner.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- PAPER and LIVE now share `scan_exchange_markets(config)` read-only scanner path using public endpoints.
- BACKTEST continues to use `_safe_market_scanner` by default to avoid live dependency.
- Offline smoke override available via `ALPHAFORGE_RUNTIME_SAFE_SCANNER=1`.
- LIVE fail-closed protections remain: placeholder scanner block, exchange-connectivity gate, qualification gate, and required real execution adapter.

### Lifecycle/persistence/schema impact
- No lifecycle schema changes.
- No persistence schema changes.

### Tests added/updated
- Added `tests/test_exchange_market_scanner.py`.
- Added runtime bootstrap scanner wiring tests for PAPER and BACKTEST.

### Tests executed
- `pytest -q tests/test_exchange_market_scanner.py tests/test_runtime.py::test_build_runtime_uses_exchange_scanner_for_paper tests/test_runtime.py::test_build_runtime_keeps_safe_scanner_for_backtest`

### Risks / limitations
- Hyperliquid public scan currently provides mids-only (limited spread/volume detail), so selection may naturally reject more symbols; this is fail-safe.
- Public endpoint shape changes upstream could reduce candidate availability, which intentionally fail-closes to fewer/no trades.

### Push recommendation
- Recommended to merge as a minimal execution-rehearsal alignment patch without threshold loosening.

## 2026-05-21 Patch Addendum — LIVE connectivity default fail-closed + startup contradiction resolution

### Why the patch was needed
- LIVE startup safety messaging and behavior were inconsistent across recent summaries.
- LIVE connectivity gating existed but was optional-by-default, which is not fail-closed for production startup.

### Root cause
- `RuntimeConfig.require_exchange_connectivity_for_live` defaulted to `False`.
- `_build_runtime_from_env()` did not wire exchange connectivity env config into `RuntimeConfig`.

### Files changed
- `src/alphaforge/runtime.py`
- `tests/test_exchange_connectivity.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- Confirmed existing LIVE fail-closed guard remains in `RuntimeOrchestrator.start()` for `_safe_market_scanner`.
- LIVE exchange connectivity gate now defaults to required (`require_exchange_connectivity_for_live=True`).
- LIVE connectivity gate can still be explicitly bypassed for tests/overrides via config/env.
- PAPER and BACKTEST behavior remain unchanged.

### Lifecycle/persistence/schema impact
- No lifecycle changes.
- No persistence schema changes.

### Tests added/updated
- Added `test_live_startup_requires_exchange_connectivity_by_default`.
- Added `test_paper_start_does_not_require_exchange_connectivity_by_default`.
- Added `test_live_can_only_skip_connectivity_when_explicitly_configured_for_test_or_override`.
- Existing `test_live_start_blocks_placeholder_bootstrap_scanner` remains as guard proof.

### Risks / limitations
- Connectivity gate quality depends on upstream exchange health probe coverage/quality.
- Explicit override can still disable gate; this is intentional for deterministic tests.

### Push recommendation
- Recommended to merge as minimal fail-closed LIVE startup safety patch.

## 2026-05-21 Patch Addendum — LIVE placeholder scanner fail-closed gate

### Why the patch was needed
- LIVE bootstrap could be started with `_safe_market_scanner`, a deterministic local placeholder feed intended only for offline wiring checks.

### Root cause
- Runtime LIVE startup gates validated readiness/connectivity (when enabled) but did not explicitly forbid placeholder/mock scanner wiring.

### Files changed
- `src/alphaforge/runtime.py`
- `tests/test_runtime.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- `RuntimeOrchestrator.start()` now blocks LIVE startup with: `LIVE mode blocked: placeholder/mock scanner is not allowed` when scanner function resolves to `_safe_market_scanner`.

### Lifecycle/persistence/schema impact
- No lifecycle schema changes.
- No persistence schema changes.

### Tests added
- `test_live_start_blocks_placeholder_bootstrap_scanner` in `tests/test_runtime.py`.

### Tests executed
- `pytest tests/test_runtime.py -q`

### Risks / limitations
- Name-based guard targets known placeholder bootstrap scanner and does not yet classify all possible custom mock scanners.

### Push recommendation
- Recommended to merge as a minimal fail-closed LIVE safety patch.

## 2026-05-21 Patch Addendum — Exchange connectivity safety + offline deterministic tests

### Why the patch was needed
- Exchange adapter wiring checks were missing from deterministic tests, leaving LIVE startup safety under-validated.

### Root cause
- No shared exchange connectivity contract existed for Binance/Hyperliquid health checks, and no opt-in integration marker boundary was defined.

### Files changed
- `src/alphaforge/exchange_connectivity.py`
- `src/alphaforge/runtime.py`
- `tests/test_exchange_connectivity.py`
- `pyproject.toml`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- Added `check_exchange_connectivity(exchange_name)` returning explicit `ExchangeHealth` contract fields.
- Added optional LIVE connectivity gate (`require_exchange_connectivity_for_live`) that fail-closes runtime startup when required exchange checks fail.
- Exchange failures are explicit and never replaced with fake zeros.

### Persistence/schema impact
- No schema migration required.

### Tests added
- Offline mocked Binance success/failure connectivity tests.
- Offline mocked Hyperliquid success/failure connectivity tests.
- Runtime LIVE block regression when exchange connectivity is unhealthy.
- Secret-leak guard assertion for exchange health payloads.
- Opt-in integration tests (`@pytest.mark.integration`) for live public endpoint checks.

### Tests executed
- `pytest tests/test_exchange_connectivity.py -q`
- `pytest tests/test_runtime.py -q`
- `pytest tests/test_sqlite_schema_bootstrap.py -q`
- `pytest -q`

### Risks / limitations
- LIVE connectivity gate is config-controlled (`False` by default) to preserve existing deterministic startup behavior.
- Integration checks remain network-dependent and are skipped unless explicitly enabled.

### Push recommendation
- Recommended to merge; adds deterministic coverage and optional live safety checks without loosening trade logic.


## 2026-05-21 Patch Addendum — Runtime order_decisions audit semantics + mode correction

### Why the patch was needed
- Runtime rejected signals were being persisted twice into `order_decisions` without explicit semantic separation, and the AI/internal `:real:` row used `mode=BACKTEST` even during PAPER runtime.
- This made rejected-decision reporting ambiguous and vulnerable to double-counting.

### Root cause
- `AIBrain._persist_decision(...)` hardcoded mode to `BACKTEST` and used `phase=real` for internal AI audit writes, which looked like canonical final runtime rows.
- Runtime final reject persistence did not explicitly mark canonical finality and often omitted score/RR enrichment fields.
- Reporting checks counted all rejected rows in `order_decisions`, including internal AI audit rows.

### Files changed
- `src/alphaforge/ai_brain.py`
- `src/alphaforge/runtime.py`
- `src/alphaforge/live_readiness.py`
- `tests/test_runtime.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- AI/internal decision persistence now uses runtime-resolved mode from signal/market context and marks internal rows as `phase=ai_internal_real`/`phase=ai_internal_virtual`.
- Runtime canonical rejected persistence is explicitly marked `phase=final` and enriched with score/RR/effective_RR when available.
- Runtime signal payload now propagates runtime mode into AI decision persistence context.

### Persistence/schema impact
- No schema migration required.
- Contract clarified inside existing `order_decisions` structure:
  - canonical runtime final decision rows: `phase=final` (or null legacy)
  - AI/internal audit rows: `phase` prefixed with `ai_internal_`

### Reporting/counting impact
- Live-readiness persistence and reject-rate checks now count only canonical final decision rows (`COALESCE(phase,'final')='final'`), preventing internal AI audit rows from inflating rejected totals.

### Tests added/updated
- Added PAPER runtime regression test validating:
  - runtime-created rejected rows are never persisted with `mode=BACKTEST`
  - canonical final PAPER rejected row has populated key fields (`signal_id`, `symbol`, `reject_reason`, `score`, `rr` where available)
  - final rejected count remains exactly one per runtime signal despite AI/internal audit row persistence
  - AI/internal row remains present but explicitly non-final via `phase=ai_internal_*`

### Risks
- Low-to-moderate: behaviorally safe and backward-compatible, but downstream queries that assumed all `phase=real` rows are final should migrate to canonical-final filtering.

### Remaining limitations
- Historical rows created before this patch may still carry ambiguous `phase` semantics.

### Migration concerns
- Consumers/reports that aggregate `order_decisions` should prefer canonical-final filter (`COALESCE(phase,'final')='final'`) to avoid legacy internal-row double counts.

### Push recommendation
- Recommended to merge; this patch hardens audit semantics without dropping internal AI audit information.


## 2026-05-21 Patch Addendum — runtime duplicate rejected-row completeness fix

### Why the patch was needed
- Runtime rejected candidates were producing a second `order_decisions` row (`decision_id` containing `:real:`) with missing `symbol` and missing `reject_reason`, creating inconsistent duplicate audit rows.

### Root cause
- `AIBrain._persist_decision(...)` inserted into `order_decisions` without populating key rejected-row fields (`symbol`, `reject_reason`, plus score/RR audit context), while runtime reject persistence already wrote a fully-populated reject row.

### Files changed
- `src/alphaforge/ai_brain.py`
- `tests/test_runtime.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- AI decision persistence now writes `symbol`, `mode`, `score`, `rr`, and canonical `reject_reason` into `order_decisions` rows, including `phase=real` rejected rows.
- Existing runtime `signal_id` propagation remains preserved.
- Thresholds/scoring/reject logic are unchanged.

### Persistence/schema impact
- No schema migration required.
- Existing `:real:` rows remain valid decision records, now complete for audit usage rather than sparse duplicates.

### Tests added/updated
- Added regression test ensuring rejected runtime decision rows never persist empty `symbol`/`reject_reason`, and specifically guarding against incomplete `:real:` paired rows.

### Risks
- Low: localized persistence payload enrichment only.

### Push recommendation
- Safe to merge as runtime audit-integrity hardening.
## 2026-05-21 Patch Addendum — Runtime identity propagation + diagnostic lifecycle hardening

### Why the patch was needed
- Runtime persistence showed repeated `REJECTED/UNKNOWN` decisions with missing `signal_id`, and repeated `ERROR` lifecycle rows with empty diagnostics, making incident auditing unreliable.

### Root cause
- Runtime reject callback persisted `reason` without mapping it to `reject_reason`, so canonical reject reason collapsed to `UNKNOWN`.
- Runtime candidate identity (`signal_id`) was not guaranteed before reject/lifecycle persistence callbacks.
- Runtime decision pipeline exceptions were not converted into diagnostic-rich lifecycle error payloads.
- AI decision persistence used a low-entropy decision id (`{signal_id}:{phase}`), causing row upserts to collapse repeated runtime decisions.

### Files changed
- `src/alphaforge/runtime.py`
- `src/alphaforge/ai_brain.py`
- `tests/test_runtime.py`
- `tests/test_ai_feature_dedupe.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- Runtime now resolves a stable non-empty `signal_id` before persistence/lifecycle emission for each candidate and propagates it through reject and lifecycle callbacks.
- Runtime reject persistence now writes explicit `reject_reason` from concrete gate/decision reason instead of dropping to `UNKNOWN`.
- Runtime decision exceptions now emit `ERROR` lifecycle events with `failure_reason` and structured `incident_payload` (exception type/message, symbol, signal_id, phase).

### Persistence/schema impact
- No schema changes.
- Decision-id generation now uses a stable hash over `(signal_id, phase, market_ts|timestamp)` so repeated runtime decisions persist as distinct rows when market timestamp changes.

### Tests added/updated
- Added runtime regression checks for non-empty reject `signal_id` and preserved reject reason semantics.
- Added runtime regression check for exception-to-ERROR lifecycle diagnostics persistence fields.
- Updated AI dedupe test to use fixed `market_ts` for deterministic same-decision upsert.
- Added AI regression check verifying repeated runtime decisions persist consistently in both `order_decisions` and `ai_decision_features`.

### Risks
- Moderate, localized persistence-identity behavior change: decision row cardinality increases for distinct runtime timestamps by design (improves auditability).

### Push recommendation
- Safe to merge as an auditability and persistence-integrity hardening patch.


## 2026-05-21 Patch Addendum — lifecycle persistence strict bool success contract

### Why the patch was needed
- Two Phase 1/2/3 foundation tests asserted identity (`is True`) on `save_trade_lifecycle_event(...)` success, but the helper returned integer-like row IDs/rowcount values (e.g., `1`).

### Root cause
- `save_trade_lifecycle_event(...)` exposed database row identity/rowcount semantics instead of a strict public success/failure boolean contract.

### Files changed
- `src/alphaforge/persistence.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- On successful lifecycle upsert + commit, `save_trade_lifecycle_event(...)` now returns literal `True`.
- Existing SQL statements, `ON CONFLICT` behavior, event_id auto-generation, and commit flow are unchanged.

### Lifecycle/persistence/schema impact
- No schema changes.
- No lifecycle state vocabulary changes.
- Persisted lifecycle rows remain queryable as before (including `lifecycle_state` and `reject_reason`).

### Tests executed
- `pytest tests/test_phase123_foundations.py::test_save_trade_lifecycle_event_persists_state -q`
- `pytest tests/test_phase123_foundations.py::test_trade_lifecycle_generates_event_id_when_missing -q`
- `pytest tests/test_phase123_foundations.py -q`

### Risks / limitations
- Minimal and localized: only success return type was normalized from integer-like to strict bool.

### Push recommendation
- Safe to merge as a contract-correctness patch.

## 2026-05-21 Patch Addendum — Rejected-shadow directional TP/SL hardening

### Why the patch was needed
- Rejected-shadow analytics showed asymmetric behavior: LONG rejected rows produced normal WOULD_TP/WOULD_SL distribution while SHORT rows were near-zero WOULD_TP despite accepted SHORT trades reaching `TP_HIT`.

### Root cause
- `simulate_rejected_counterfactual(...)` used LONG-style TP/SL checks for all sides (`high>=tp`, `low<=sl`) and did not branch on `candidate.side`.

### Files changed
- `backtest_order.py`
- `tests/test_backtest_order_scanner.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Behavior changes
- Rejected-shadow TP/SL touch logic is now side-aware:
  - LONG: TP on `high>=tp`, SL on `low<=sl`.
  - SHORT: TP on `low<=tp`, SL on `high>=sl`.
- Conservative same-candle ambiguity convention is now explicit in-code and identical across both sides: if both TP and SL are touched within a candle, classify as SL to avoid optimistic bias.

### Lifecycle/persistence/schema impact
- No lifecycle state/schema changes.
- No CSV schema changes.
- No score threshold, RR gate, or accepted-order generation logic changes.

### Tests added/updated
- Added rejected-counterfactual tests for LONG/SHORT TP/SL directionality and same-candle ambiguity.
- Added SHORT regression test for `evaluate_rejected_shadow(...)` validating `WOULD_TP` + `effective_tp_hit=True` under passing filters.
- `tests/test_backtest_order_scanner.py` passes fully.

### Risks / limitations
- Intrabar order is still unavailable from OHLC alone; conservative SL-priority tie-break remains a designed approximation.

### Push recommendation
- Safe and recommended: minimal, focused correctness patch for rejected-shadow SHORT outcome evaluation without gate loosening.



## 2026-05-21 Patch Addendum — Runtime/AIBrain SQLite thread-safety

### Why the patch was needed
Runtime dispatched AI decisioning via `asyncio.to_thread`, but decision persistence used a shared SQLAlchemy `Session`, triggering SQLite thread-affinity failures.

### Root cause
AIBrain `_persist_decision` wrote using `self.session` regardless of calling thread, violating SQLite constraint that connection-bound objects stay on creating thread.

### Files changed
- `src/alphaforge/runtime.py`
- `src/alphaforge/ai_brain.py`
- `src/alphaforge/persistence.py`
- `tests/test_runtime.py`
- `VERSION.md`
- `CHANGELOG.md`
- `REPORT.md`

### Runtime behavior changes
- Removed `asyncio.to_thread` wrapping around runtime decision call (`before_real_order`).
- Added session-per-operation persistence path in AIBrain when `session_factory` is supplied.

### Persistence impact
- `_persist_decision` now opens a short-lived session, commits/rolls back, and closes it when using `session_factory`.
- Backward compatibility preserved for existing injected-session usage.

### Tests added
- `test_ai_brain_persistence_uses_short_lived_sessions_across_to_thread`

### Tests executed
- `pytest -q`

### Risks / limitations
- No threshold, scoring, or reject-gate logic changes.
- LIVE readiness unchanged; this is a thread-safety and persistence-correctness patch.



## 2026-05-20 Phase 6.1 Audit-trail canonicalization

### Why changes were needed
Runtime, persistence, and export paths still emitted mixed lifecycle vocabularies (`ENTRY_PENDING`/`ENTRY_SUBMITTED` etc.) and had partially silent persistence failure behavior. This undermined a single audit-truth contract across PAPER/BACKTEST/persistence rows.

### Lifecycle behavior before / after
- **Before:** accepted PAPER runtime emitted extended runtime states (`ENTRY_PENDING`, `ENTRY_SUBMITTED`, `ENTRY_ACKNOWLEDGED`, ...), while backtest/export paths centered on canonical order lifecycle names.
- **After:** accepted PAPER runtime now emits canonical progression: `SIGNAL_CREATED -> WAITING_ENTRY_ZONE -> ENTRY_TRIGGERED -> ORDER_PLACED` then `POSITION_OPENED` on fills. Rejected PAPER/runtime risk gates emit `SIGNAL_CREATED -> SIGNAL_REJECTED` deterministically.

### Persistence behavior before / after
- **Before:** helper writes could throw/short-circuit depending on schema differences and could be effectively placeholder-like in edge schemas.
- **After:** `save_order_decision` and `save_trade_lifecycle_event` perform durable insert attempts and fail closed (`None`/`False`) on SQL errors, enabling runtime detection. Runtime lifecycle persistence callback now raises when lifecycle persistence fails (detectable fail-closed preparation for LIVE hardening).

### Runtime impact
- Canonical lifecycle ordering is now explicit in PAPER accept/reject paths and tests.
- Reconciliation flow remains intact; timeout-like execution now uses canonical `ENTRY_TIMEOUT` before reconciliation escalation.

### Compatibility / migration / schema implications
- SQLite compatibility preserved; no destructive migration added.
- Existing extended lifecycle event support in `contracts.py` is retained for compatibility while canonical states are now preferred for core audit flow.
- Persistence helpers continue to tolerate optional columns/tables by returning failure state instead of crashing entire run path.

### Tests added/updated
- Added PAPER lifecycle sequence tests for accepted canonical flow and reject ordering.
- Updated runtime tests to assert `ORDER_PLACED` emission and `SIGNAL_CREATED` first semantics.
- Full suite passing (`177 passed`).

### Remaining blockers
- Full LIVE fail-closed exchange execution wiring remains out of scope (still blocked).
- Some non-core extended lifecycle states remain in reconciliation/ops channels for incident observability and must be converged in future phases if full canonical-only contract is required.
## 2026-05-20 Patch Addendum — SQLite additive schema bootstrap hardening

### Why the patch was needed
- Runtime/backtest persistence on existing SQLite files failed because table schemas lagged behind current write paths.
- `CREATE TABLE IF NOT EXISTS` did not modify existing tables, so additive columns (`order_decisions.phase`, `ai_decision_features.decision_id`, etc.) remained missing.

### Root cause
- Schema evolution introduced new columns without an idempotent additive migration pass for pre-existing SQLite DB files.

### Affected tables
- `order_decisions`
- `ai_decision_features`
- `trade_lifecycle_events`
- `closed_trade_reviews`
- `schema_migrations`

### Files changed
- `src/alphaforge/persistence.py`
- `tests/test_sqlite_schema_bootstrap.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Added migrations/bootstrap behavior
- Added SQLite helpers for table-existence checks, column introspection, and additive per-column migration.
- `init_db()` now runs idempotent SQLite runtime schema repair after base table creation.
- Migration logs emitted when columns are added.

### Why create_all()/CREATE TABLE IF NOT EXISTS was insufficient
- SQLite `CREATE TABLE IF NOT EXISTS` only creates missing tables; it does not reconcile missing columns on existing tables.

### Test coverage
- Legacy `order_decisions` schema repaired and write-path verified.
- Legacy `ai_decision_features` schema repaired and write-path verified.
- Double `init_db()` idempotency and data preservation verified.

### Threshold/regression confirmation
- No changes to score thresholds, RR gates, spread/slippage limits, reject logic, or AI decision semantics.

### Push recommendation
- Safe to merge as additive, SQL-first backward-compatibility hardening for persistence stability.

## 2026-05-20 Patch Addendum — Runtime bootstrap smoke scanner + execution mode default

### Why the patch was needed
- Runtime bootstrap scanner returned `[]`, so startup wiring could not exercise symbol selection, AI decisioning, lifecycle emission, or persistence callbacks.
- Runtime startup defaulted to BACKTEST when `EXECUTION_MODE` was absent, which is unsafe for expected operator posture.

### Root cause
- `_safe_market_scanner` was implemented as an empty no-op list.
- `execution_mode_from_env(None)` and `RuntimeConfig.execution_mode` defaulted to `BACKTEST`.

### Files changed
- `src/alphaforge/runtime.py`
- `tests/test_runtime.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- Bootstrap scanner now returns one deterministic local-only smoke-test candidate with required selector/risk/AI fields.
- Runtime mode resolution now uses `EXECUTION_MODE` with default PAPER semantics.
- No exchange connectivity added; no real order submission path added.

### Lifecycle/persistence impact
- Startup smoke flow now can generate lifecycle/reject persistence artifacts via existing callbacks.
- Lifecycle contract and transition logic unchanged.

### Export/schema impact
- None.

### Tests added
- None.

### Tests executed
- `python -m compileall src/alphaforge/runtime.py`
- `python -m pytest tests -q`

### Risks
- Minimal: deterministic smoke candidate could be unexpectedly accepted/rejected depending on environment thresholds, but remains local-only and non-exchange.

### Remaining limitations
- Scanner is explicitly bootstrap smoke-only, not a live market scanner.

### Migration concerns
- None.

### Push recommendation
- Safe to merge as runtime bootstrap safety/alignment fix.

# AlphaForge Forensic Audit Report — Backtest Lifecycle Behavior (2026-05-19)

## 2026-05-19 Patch Addendum — Remaining pytest failures (targeted hotfix)

### Why the patch was needed
- Remaining backtest scanner failures showed spread-unit inconsistency in symbol gating and calibration snapshot insert schema mismatch (`payload_json` absent on current SQLite table).
- A constructor compatibility regression required optional defaults for `ForwardWindowEvaluation` in idempotency tests.

### Root cause
- `select_symbol(...)` treated spread thresholds with stale percent-point configuration (`0.12`) and scoring shape that let `0.0035` pass as strong spread.
- Backtest summary calibration insert expected `payload_json` column although in-memory initialized schema did not guarantee it.
- `ForwardWindowEvaluation` required fields not always supplied by test fixtures intended to validate persistence/idempotency semantics.

### Files changed
- `src/alphaforge/symbol_selector.py`
- `backtest_order.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- No production risk filter loosening: spread gate is stricter and unit-correct (`max_spread_pct=0.0025` as fraction).
- Calibration snapshot export insert is schema-compatible across current table variants (no hard dependency on `payload_json`).

### Lifecycle/persistence impact
- Lifecycle persistence remains SQL-backed and deterministic; event ID uniqueness behavior is unchanged.
- Effective RR precedence in lifecycle persistence remains `row.effective_rr` fallback to `row.rr`.

### Tests executed
- `python -m pytest tests/test_backtest_order_scanner.py -q`
- `python -m pytest -q`

### Risks / limitations
- This is a localized fix; no architectural rewrite.
- LIVE readiness remains unchanged and not recommended.

### Push recommendation
- Safe to merge as a defensive consistency fix with preserved reject rigor.

## Executive Summary

AlphaForge is **not failing because it generates zero signals**; it is failing because the current backtest signal stream is mostly low-quality, heavily long-biased, and then aggressively filtered by intentionally strict quality/execution gates. The observed lifecycle pattern (`SYMBOL_REJECTED` + `SIGNAL_REJECTED` + a very small `ORDER_REJECTED`, zero placed trades) is consistent with code behavior.

Primary root causes:
1. **Candidate generation is structurally long-only in backtest path** (`side="LONG"`, `BREAKOUT_UP` defaults).
2. **Score thresholding is intentionally high** (base `min_score=7.5`) versus observed score distribution centered around ~3–4.
3. **Symbol regime filters are strict and front-load rejections** (`TOO_CHOPPY`, `WEAK_TREND_AND_NO_RANGE_EDGE`).
4. **Execution penalty model can materially compress effective RR**, and a separate backtest-only execution penalty path exists with different formula than runtime real-order path.
5. **Order geometry (SL width) is fragile for late-breakout candles**, causing `STOP_TOO_WIDE` in survivors.

Reject engine appears **mostly directionally correct** (high rejected-loss share aligns with defensive objective), but calibration and unit consistency likely require tightening.

---

## System Flow

### Actual Decision Pipeline (Backtest)
1. Universe/symbol data loaded and scored by symbol selector.
2. Symbol-level rejects can emit `SYMBOL_REJECTED`.
3. For scannable bars, `_build_market_ctx(...)` creates candidate fields (entry/sl/tp/rr/score/regime/side).
4. `run_order_cycle(...)` in shared order runtime does:
   - `build_order_candidate(...)`
   - `evaluate_trade_quality(...)`
   - if accepted, `execute_order_candidate(...)`
5. Backtest script maps decisions into lifecycle rows and persists/exports.

### Lifecycle transitions currently seen in your extraction
Your extracted states match the early-gate flow where most candidates die before execution:
- `SYMBOL_REJECTED`
- `SIGNAL_CREATED`
- `SIGNAL_REJECTED`
- `ORDER_REJECTED` (for execution/effective-RR rejections after initial signal quality acceptance)

This is coherent with the gating architecture and with very low pass-through.

---

## Signal Generation Audit

### Where candidates are generated
- `backtest_order.py`
  - `_build_market_ctx(...)`
  - `scan_symbol_backtest(...)`

### Why many candidates are low quality
- Backtest score formula is heuristic and momentum-candle biased:
  - `score = clamp(3.0 + breakout_strength*500 + range_pct, 0..10)`
- Many bars will cluster in mid/low scores unless breakout extension is significant.
- Quality gate baseline is high (`MIN_SCORE_BASE=7.5`) in shared order engine.

### Why SHORT candidates are absent
- `_build_market_ctx(...)` hardcodes:
  - `setup_type="BREAKOUT_UP"`
  - `setup_reason="CLOSE_ABOVE_PREV_HIGH"`
  - `side="LONG"`
- No mirrored bearish builder is invoked in this path.

Conclusion: absence of SHORTs is architectural in the current backtest candidate builder, not merely a logging artifact.

---

## Score System Audit

### Where score is calculated
- Backtest candidate score: `backtest_order.py::_build_market_ctx(...)`
- Trade gate thresholding: `src/alphaforge/order.py::evaluate_trade_quality(...)`
- Adaptive threshold source: `src/alphaforge/order.py::compute_adaptive_thresholds(...)`

### Dominant scoring features
- Breakout extension ratio (`close > prev_high`) and candle range.
- This rewards **impulse intensity**, not necessarily executable expectancy after cost.

### Why score may not correlate strongly with shadow TP outcomes
- Score is not calibrated to forward realized outcomes in the same function.
- Execution/geometry frictions are evaluated later by separate gates.
- A strong momentum candle can score high while simultaneously implying bad SL geometry or poor effective RR after costs.

### Is ~7.5 threshold intentional?
Yes. `MIN_SCORE_BASE = 7.5` and adaptive logic shifts around that baseline.

---

## Regime Engine Audit

### Where TOO_CHOPPY / WEAK_TREND_AND_NO_RANGE_EDGE are enforced
- `src/alphaforge/symbol_selector.py::select_symbol(...)`
  - `TOO_CHOPPY` when `chop_score > max_chop_score`
  - `WEAK_TREND_AND_NO_RANGE_EDGE` when neither clean trend nor range-edge condition holds

### Strictness assessment
Given your distribution, filters are probably functioning as designed (defensive posture), but may be over-conservative combined with long-only breakout sourcing.

### Why REGIME_MISMATCH can show high TP yet still be blocked
- Regime compatibility checks in `evaluate_trade_quality(...)` are categorical and structural.
- Some mismatched setups can still hit TP in raw terms, but policy blocks them to avoid unstable regime-transfer behavior.
- If effective expectancy after costs remains negative, rejection remains philosophically consistent.

---

## Execution & Effective RR Audit

## Formulas located

### Backtest-local rejection helper
`backtest_order.py::_execution_reject_flags(rr, market_ctx)`:
- `execution_penalty = (slippage + spread) * 50`
- `effective_rr = max(rr * (1 - execution_penalty), 0)`

### Shared runtime cost model
`src/alphaforge/execution.py::build_execution_cost_model(...)`:
- `spread_penalty = spread_pct * 25`
- `slippage_penalty = expected_slippage_pct * 30`
- `latency_penalty = (latency_ms/1000) * 0.2`
- `funding_penalty = abs(funding_rate_pct) * 2.5`
- `liquidity_penalty = (1 - liquidity_score) * 0.6`
- `total_penalty = sum(above)`

`src/alphaforge/order.py::_effective_rr(...)`:
- `effective_rr = max(raw_rr - total_penalty, 0)`

### Key architectural finding
There are **two different effective-RR formulations** in the codebase (multiplicative backtest helper vs additive runtime model). This can create calibration mismatch in diagnostics and rejection interpretation.

### Unit consistency concern (spread_pct meaning)
- `_spread_pct_from_prices` returns fraction: `(ask-bid)/mid`.
- Backtest estimator `_estimate_backtest_spread_pct` returns values like `0.015` baseline.
- In these formulas, `0.015` behaves like **1.5% (fraction)**, not 0.015%.

If your external interpretation assumed percent points (0.015%), penalties will look unexpectedly harsh. Current code treats spread/slippage as fractional rates.

### Is effective_rr collapse legitimate or buggy?
Likely **combination**:
- Partly legitimate under conservative penalties and long-breakout timing.
- Partly suspicious if any pipeline provides spread/slippage in percent units while formulas expect fractional units.
- Existence of dual formulas increases risk of inconsistent collapse behavior.

---

## Order Geometry Audit

### Where stop distance and RR are built
- `backtest_order.py::_build_market_ctx(...)`
  - `sl = min(now.low, prev.low)`
  - `risk = entry - sl`
  - `rr` is heuristic, then `tp = entry + rr*risk`
- Stop-width gate in `evaluate_trade_quality(...)`:
  - `sl_pct = abs(entry-sl)/entry*100`
  - reject if `sl_pct > MAX_SL_PCT` (default 1.5)

### Why high-score candidates fail STOP_TOO_WIDE
Late breakout bars can widen structural SL distance quickly. Score can be high from impulse strength while SL% breaches cap.

### Could retry shaping help?
Yes, minimally:
- add bounded order-shaping retries (e.g., entry pullback bands, capped SL relocation, adaptive TP rebalance) **before** terminal reject.
- keep fail-closed final gate unchanged.

---

## Backtest Architecture Audit (vs PAPER/LIVE)

- Backtest uses shared `run_order_cycle(...)` quality gate path (good alignment).
- But backtest script still has extra local mechanics and evaluation helpers not identical to live order path.
- Effective RR math divergence (noted above) is a material alignment risk.
- Lifecycle persistence exists and is richer than earlier versions, but current observed run indicates early-stage-only transitions due to no accepted orders.

Assessment: architecture is partially aligned, but **not fully unified** in execution-penalty semantics and candidate construction realism.

---

## Lifecycle Architecture Trace

### Declared lifecycle model
Shared lifecycle enum/contract includes:
- `SIGNAL_CREATED` → `WAITING_ENTRY_ZONE` → `ENTRY_TRIGGERED` → `ORDER_PLACED` → close states
- plus reject/cancel/error states.

### Observed-only subset explanation
Because all candidates fail before order survival, only early reject states appear. Missing downstream states are a consequence of gate outcomes, not necessarily missing enum definitions.

### Potential bypass/missing practical states in this run
- No `WAITING_ENTRY_ZONE`/`ORDER_PLACED` terminal trades observed due to zero acceptances.
- Backtest realism for partial fills/advanced execution remains simplified relative to live complexity.

---

## Expectancy System Audit

### Where expectancy bucket is calculated
- `backtest_order.py::_bucket_expectancy(...)`
- Candidate expectancy in `_build_market_ctx(...)`: `((score/10)-0.5)*(rr-1.0)`

### Connection quality
- Bucket is persisted/propagated through lifecycle rows.
- But score formula and expectancy formula are tightly coupled heuristics, not empirically calibrated to realized forward bins in this module.

Conclusion: wiring exists; calibration linkage to realized expectancy is weak.

---

## Root Cause Matrix

| Symptom | Root cause | Severity | Confidence | Impacted files/functions |
|---|---|---:|---:|---|
| No SHORT candidates | Backtest builder hardcodes LONG/bullish setup | High | High | `backtest_order.py::_build_market_ctx` |
| Massive LOW_SCORE rejects | High min score (7.5+) vs low-mid heuristic score distribution | High | High | `src/alphaforge/order.py::compute_adaptive_thresholds`, `evaluate_trade_quality`; `backtest_order.py::_build_market_ctx` |
| Survivors fail STOP_TOO_WIDE | Breakout entries + structural SL from bar lows exceed 1.5% cap | Medium-High | High | `backtest_order.py::_build_market_ctx`; `src/alphaforge/order.py::evaluate_trade_quality` |
| Effective RR compressed heavily | Conservative penalties + possible unit interpretation mismatch + dual formulas | High | Medium-High | `src/alphaforge/execution.py::build_execution_cost_model`; `src/alphaforge/order.py::_effective_rr`; `backtest_order.py::_execution_reject_flags` |
| REGIME_MISMATCH still has TP winners | Categorical regime gate blocks structurally even when some raw winners occur | Medium | Medium | `src/alphaforge/order.py::evaluate_trade_quality`; `src/alphaforge/symbol_selector.py` |
| Backtest/PAPER/LIVE not perfectly aligned | Shared gate exists, but auxiliary backtest logic diverges in places | Medium | Medium | `backtest_order.py`; `src/alphaforge/order.py` |

---

## Recommended Minimal Fixes (No Architecture Rewrite)

1. **Add mirrored SHORT candidate builder** in backtest path with bearish breakout/pullback logic parity.
2. **Unify effective-RR formula usage** (single source of truth from `build_execution_cost_model`).
3. **Add explicit unit contract checks** for spread/slippage inputs (fraction vs percent).
4. **Introduce bounded order-shaping retry** before `STOP_TOO_WIDE` final reject.
5. **Instrument score-to-outcome calibration reports** without lowering defensive rejects blindly.
6. **Keep reject protections on**, but expose gate attribution and distributions by regime/setup/side.

---

## Recommended Diagnostics

Add metrics/logging/persistence fields:
- `score_rank_pct`, `score_decile`, `raw_rr`, `effective_rr`, `cost_penalty_total`, and decomposed penalties.
- `spread_unit_assumed` (`fraction`/`percent_points`) and raw source fields.
- `first_blocking_gate`, `all_failed_gates`, `regime_ok`, `sl_pct`.
- Side coverage metrics: long/short candidate counts pre/post each gate.
- Calibration outputs: TP/SL/timeout rates by score decile, regime, setup, side.

---

## Tests To Add

1. **SHORT generation tests**
   - assert both LONG and SHORT candidate creation under mirrored market patterns.
2. **Score variability + calibration tests**
   - verify score distribution is non-degenerate and monotonicity vs outcome isn’t inverted.
3. **Effective RR unit sanity tests**
   - explicit cases for spread/slippage in fraction vs percent-point inputs.
4. **Formula alignment tests**
   - ensure backtest effective-RR diagnostics match shared runtime model.
5. **Lifecycle transition tests**
   - validate complete path coverage and state legality under accepted/rejected branches.
6. **Rejected shadow export tests**
   - verify reject reason + shadow outcome + penalty decomposition persistence.
7. **Expectancy calibration tests**
   - bucket assignment consistency and realized expectancy drift alerts.

---

## Final Assessment

AlphaForge’s current backtest underperformance is a **combination problem**:
- It is **finding many weak candidates** (and mostly only LONG-type candidates).
- It is **correctly rejecting most of them** under defensive policy.
- A small set of stronger candidates then often fail **geometry + effective-RR** gates.
- Execution penalties may be **partly too harsh or inconsistently interpreted** due to unit/formula ambiguity.
- Score is **not sufficiently calibrated** to executable post-cost expectancy.

So the dominant failure mode is not a single bug; it is: **long-only candidate construction + scoring/calibration mismatch + strict execution/geometry gating, with potential penalty unit inconsistency amplifying final rejection.**


## Patch Update — 2026-05-19

- Implemented minimal mirrored SHORT candidate construction in backtest market-context builder.
- Aligned backtest execution reject effective-RR calculation to shared additive execution-cost model.
- Added spread/slippage unit normalization and explicit unit-assumption fields for diagnostics/export.
- Added regression tests for SHORT candidate emission and percent-point spread normalization behavior.
# PAPER Runtime Persistence and SQLite Bootstrap Investigation (2026-05-19)

## Root Cause Summary
- Tables were not visible primarily because PAPER runtime can point at an unexpected SQLite target (`:memory:` default or non-resolved relative path) while SQLTools inspected a different file.
- Runtime bootstrap already called `init_db(...)` (which issues `CREATE TABLE IF NOT EXISTS`), but startup lacked fail-fast logging to prove path/schema/table state.
- Runtime heartbeat counters stayed at zero because the default runtime scanner returns an empty candidate list, so symbol selection and decision generation never progressed.
- Prior runtime bootstrap did not wire reject/lifecycle callbacks to persistence, so runtime-generated reject/lifecycle data was not persisted by default even if events occurred.

## Exact Files / Functions Investigated
- `src/alphaforge/runtime.py`
  - `_build_runtime_from_env()`
  - `_scan_once()`
  - `_heartbeat_loop()`
- `src/alphaforge/persistence.py`
  - `init_db()`
  - `_apply_sqlite_migrations()`
  - `save_order_decision()`
  - `save_trade_lifecycle_event()`
- `src/alphaforge/symbol_selector.py`
  - `select_symbols()` / `select_symbol()`

## Why No Tables Appeared
- Schema creation function exists and is mode-agnostic: `init_db(...)` always executes DDL list with `CREATE TABLE IF NOT EXISTS`.
- It is invoked during runtime bootstrap (`_build_runtime_from_env`) before orchestrator starts.
- Empty decision flow does NOT skip schema init.
- Practical failure mode was observability/path mismatch: no explicit absolute DB path and no post-init table logging, making SQLTools likely pointed at a different DB file.

## Why PAPER Decisions Were Not Generated
- Runtime env bootstrap scanner currently returns `[]` in `_safe_market_scanner`; therefore:
  - `symbols_selected=0`
  - `decisions_generated=0`
  - `rejects_persisted=0`
  - `lifecycle_events=0`
- This is fail-closed and expected with no market candidates; not a permissiveness bug.

## Determination (a–e)
- (a) **Yes**: PAPER was not selecting symbols (`symbols_selected=0`) due to empty candidate list.
- (b) N/A in observed env bootstrap path (no symbols selected).
- (c) Previously possible in runtime path because callbacks were not wired by default; now fixed in bootstrap wiring.
- (d) SQL persistence was partially skipped for runtime events pre-patch (no default reject/lifecycle callbacks); now enabled when `ALPHAFORGE_PERSISTENCE_ENABLED=true`.
- (e) **Likely contributing factor**: SQLite path mismatch (relative/in-memory vs SQLTools target) due to missing absolute-path diagnostics; now fixed with explicit resolved path logging.

## BACKTEST vs PAPER/LIVE Persistence Comparison
- Table creation: all modes via shared `init_db(...)` when bootstrapped through runtime env path.
- Lifecycle writes: available via runtime `on_lifecycle_event` callback; now wired in bootstrap for runtime modes.
- Reject writes: available via runtime `on_reject_persist` callback; now wired in bootstrap for runtime modes.
- Orders/executions: execution counters/lifecycle emit in runtime; order decision persistence is callback-dependent and now wired in bootstrap path.
- Rejected decision consistency: improved by default callback wiring; remains dependent on runtime producing rejections.

## Patch Plan Executed
1. Add deterministic startup diagnostics for DB URL/path/schema/tables and persistence flag.
2. Ensure runtime bootstrap wires lifecycle/reject callbacks to SQLite persistence functions.
3. Add zero-selection diagnostics and gate blockers in scan + heartbeat.
4. Add tests for bootstrap schema/path/zero-selection diagnostics.

## Code Changes Made
- Implemented runtime bootstrap logging: configured DB URL, resolved absolute DB URL, schema init success, discovered table names.
- Added `persistence_enabled` metric and heartbeat surfacing.
- Added scan-time reject reason aggregation and explicit gate blockers for `NO_MARKET_CANDIDATES` and `NO_TRADABLE_SYMBOLS_AFTER_SELECTION`.
- Wired runtime bootstrap callbacks to `save_trade_lifecycle_event(...)` and `save_order_decision(...)`, guarded by `ALPHAFORGE_PERSISTENCE_ENABLED`.

## Tests Added
- PAPER bootstrap creates key schema tables even with empty decision cycle.
- Runtime bootstrap logs absolute SQLite DB path.
- Zero-selected-symbol scan records explicit gate blocker + rejection summary.

## Remaining Risks
- Default env scanner still returns no candidates; runtime remains inert unless real scanner/universe feed is wired.
- Persistence callbacks now wired, but successful rows still depend on runtime generating lifecycle/reject events.
- SQLTools/operator must verify they open the same resolved DB path logged by runtime.

## 2026-05-19 Rejected Shadow + Reject Gate Audit Patch

### Why this patch was needed
- Rejected-shadow analysis surfaced potential LOW_SCORE score-scale confusion and limited visibility into reason-level missed-opportunity structure.
- STOP_TOO_WIDE rejects showed non-trivial hypothetical TP opportunities that required bounded rescue diagnostics rather than global gate loosening.

### Root cause
- Exported reject rows lacked explicit gate-score provenance fields.
- Rejected-shadow summary was aggregate-only and not grouped with per-reason profitability/cost structure.
- Spread unit normalization was not consistently enforced for all market-data ingestion paths.

### Files changed
- `backtest_order.py`
- `tests/test_backtest_order_scanner.py`
- `CHANGELOG.md`
- `VERSION.md`
- `REPORT.md`

### Runtime behavior changes
- Added gate-score observability fields to rejected exports and shadow exports.
- Added STOP_TOO_WIDE rescue simulation diagnostics with bounded size reduction and post-cost effective-RR recomputation.
- Added grouped reject-reason shadow diagnostics.

### Persistence / export changes
- `rejected_orders.csv`: adds `gate_score`.
- `rejected_shadow.csv`: adds `low_score_gate_score` and rescue telemetry fields.
- `rejected_shadow_summary.csv`: adds `reject_reason_diagnostics` JSON payload.

### Risks / limitations
- Rescue path is diagnostic-only and intentionally conservative; it does not auto-accept trades.
- Top symbol/regime outputs are frequency-based and do not imply production allocation guidance.

## Dev Branch Design Compliance Audit (2026-05-20)

### Current status
- **Overall:** PARTIAL compliance. Core execution-aware components and persistence exist, but full shared signal-to-order contract parity across BACKTEST/PAPER/LIVE is incomplete.

### What works
- BACKTEST uses shared `run_order_cycle(...)` for candidate quality gating before simulation/execution lifecycle expansion.
- Execution-cost model computes additive penalties (spread, slippage, latency, funding, liquidity) and effective RR, with explicit missing-field handling.
- Rejected decisions/lifecycle events persist with reject reasons and execution-context flags/sentinels.
- Runtime has explicit pre-trade risk gates and lifecycle persistence paths.

### What failed / gaps found
- Runtime path still primarily uses `ai_brain.before_real_order(...)` and does not exclusively use the same `run_order_cycle(...)` decision path used in backtest.
- Naming/contract mismatch versus target contract (`SignalCandidate`, `ProbabilityDecision`, `evaluate_signal_to_order(...)`) remains partially semantic rather than exact API parity.
- Regime vocabulary support is partial; not all requested regime labels are first-class states in decision gates.

### Exact files/functions inspected
- `backtest_order.py`: `scan_symbol_backtest`, `simulate_candidate`, `process_backtest_result`, `_execution_reject_flags`.
- `src/alphaforge/order.py`: `run_order_cycle`, `build_order_candidate`, `evaluate_trade_quality`, `_effective_rr`.
- `src/alphaforge/runtime.py`: `_scan_once`, `_process_symbol`, `_execute`.
- `src/alphaforge/execution.py`: `build_execution_context`, `build_execution_cost_model`, `normalize_pct_input`.
- `src/alphaforge/persistence.py`: order/lifecycle persistence helpers and schema fields used by tests.

### Patches applied
- Fixed backtest lifecycle progression regression in `simulate_candidate(...)` that removed `WAITING_ENTRY_ZONE` from emitted state sequence.
  - Removed accidental overwrite forcing first lifecycle row from `SIGNAL_CREATED -> WAITING_ENTRY_ZONE` back to `SIGNAL_CREATED -> SIGNAL_CREATED`.

### Remaining risks
- Shared decision API parity is still architectural-partial across runtime vs backtest.
- Probabilistic fields exist in AI decision flow, but order-runtime gate remains primarily heuristic-threshold based.
- Regime mapping breadth is limited relative to requested taxonomy.

### Tests run
- `pytest -q`

### Test results
- **Before patch:** 1 failing test (`test_backtest_lifecycle_does_not_start_directly_at_created`).
- **After patch:** full suite passing.

### Known limitations
- This patch intentionally avoids large architecture rewrites to preserve safety and existing runtime behavior.
- No live-exchange dependency was added to backtest paths.

### Next recommended generation
1. Introduce explicit shared contract types (`SignalCandidate`, `ProbabilityDecision`) and a canonical `evaluate_signal_to_order(...)` API in `src/alphaforge/order.py`.
2. Route runtime `_process_symbol` through that shared evaluator pre-AI execution planning, preserving execution-mode-specific adapters.
3. Add parity tests proving BACKTEST and PAPER/LIVE use the same evaluator and reject-reason taxonomy.

## 2026-05-20 Patch Addendum — Backtest lifecycle/persistence/reporting defect fix

### Why the patch was needed
- Backtest lifecycle persistence could violate a deployed unique key `(signal_id,event_ts,lifecycle_state)`.
- Summary counters under-reported orders despite `ORDER_PLACED` lifecycle rows.
- Lifecycle CSV ordering could be nondeterministic under timestamp ties.

### Root cause
- Upsert conflict target was tied to `event_id` only.
- Summary counters used WAITING/timeout counts rather than unique triggered/placed lifecycle keys.
- Export query sorted only by timestamp/event id.

### Files changed
- `src/alphaforge/persistence.py`
- `backtest_order.py`
- `tests/test_phase123_foundations.py`
- `tests/test_backtest_order_scanner.py`
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- `save_trade_lifecycle_event(...)` now prefers upsert by `(signal_id,event_ts,lifecycle_state)` and falls back to `event_id` compatibility path.
- Backtest summary now computes:
  - `total_orders` from unique `ORDER_PLACED` keys
  - `triggered_orders` from unique `ENTRY_TRIGGERED` keys
  - `not_triggered_orders` from WAITING keys that never trigger/place
- Lifecycle export ordering is stable by `event_ts, symbol, signal_id, lifecycle_seq, lifecycle_state, event_id`.
- LOW_SCORE rescue/watch fields are exported as diagnostics-only and do not alter accepted/order/PnL metrics.

### Lifecycle / persistence / schema impact
- No schema loosening and no constraint removal.
- Idempotent lifecycle replay now supports both uniqueness layouts (`event_id` and composite lifecycle key).

### Tests executed
- `pytest -q` (pass).
- Offline backtest smoke + CSV assertions for duplicate IDs, ordering semantics, WAITING-before-trigger, and summary count reconciliation.

### Threshold stance
- Global score threshold and scoring model were **not loosened or changed**.

---

## 2026-05-21 Patch Addendum — PR #114 merge conflict resolution (Phase 6.1 canonicalization)

### Why the patch was needed
- PR #114 required conflict-focused reconciliation with current dev behavior while preserving the Phase 6.1 lifecycle/persistence contract.

### Files changed
- `src/alphaforge/runtime.py`
- `src/alphaforge/persistence.py`
- `tests/test_runtime.py`
- `CHANGELOG.md`
- `REPORT.md`
- `VERSION.md`

### Runtime/lifecycle changes
- PAPER accepted flow now emits canonical pre-execution states: `SIGNAL_CREATED -> WAITING_ENTRY_ZONE -> ENTRY_TRIGGERED -> ORDER_PLACED`.
- Rejected path remains `SIGNAL_CREATED -> SIGNAL_REJECTED`.
- Runtime lifecycle persistence callback now fails closed if lifecycle SQL persistence returns failure.

### Persistence changes
- `save_order_decision(...)` now catches SQL/commit failures and returns explicit failure (`None`).
- `save_trade_lifecycle_event(...)` now returns explicit `False` if both upsert strategies fail or commit fails.

### Tests added/executed
- Added runtime tests for PAPER canonical lifecycle sequence and lifecycle persistence failure detectability.
- Executed:
  - `python -m py_compile src/alphaforge/runtime.py src/alphaforge/order.py src/alphaforge/ai_brain.py src/alphaforge/persistence.py backtest_order.py`
  - `pytest -q`


## 2026-05-21 Patch Addendum — pytest compatibility fixes (persistence + lifecycle)

### Why the patch was needed
- Current persistence helper/API behavior diverged from legacy tests/contracts (`fetch_expectancy_stat` shape and legacy compatibility columns).
- Backtest accepted lifecycle progression could transition from `SIGNAL_ACCEPTED` directly to `ENTRY_TRIGGERED`.

### Root cause
- `fetch_expectancy_stat` had been broadened to metadata dict output rather than preserving scalar legacy return contract.
- SQLite bootstrap did not consistently guarantee all legacy compatibility columns across existing DBs.
- `simulate_candidate(...)` emitted `ENTRY_TRIGGERED` with `status_before='SIGNAL_ACCEPTED'` instead of waiting-state continuity.

### Files changed
- `src/alphaforge/persistence.py`
- `backtest_order.py`
- `VERSION.md`
- `CHANGELOG.md`
- `REPORT.md`

### Behavior changes
- Restored `fetch_expectancy_stat(...) -> float | None` semantics and added `fetch_expectancy_stat_detail(...)` for detailed exports/metadata callers.
- Added idempotent schema repair coverage for legacy compatibility columns in `order_decisions` and `trade_lifecycle_events`.
- `save_order_decision(...)` now mirrors serialized payload into compatibility `payload` column and preserves rejected payload details.
- `save_trade_lifecycle_event(...)` now populates compatibility `trade_id/state/payload` and returns inserted/upserted row id.
- Backtest accepted lifecycle now emits `WAITING_ENTRY_ZONE` before `ENTRY_TRIGGERED` in market/limit trigger paths.

### Threshold/regression confirmation
- No score thresholds changed.
- No reject/accept logic changed.
- No scoring logic changes.

### Tests executed
- `pytest -q tests/test_persistence_fetch_expectancy.py`
- `pytest -q tests/test_persistence_patch1.py`
- `pytest -q tests/test_phase123_foundations.py::test_backtest_lifecycle_does_not_start_directly_at_created`
- `pytest -q`

## 2026-05-21 Patch
Root cause: runtime/exchange/backtest parsed env independently with hardcoded defaults.
Changes: introduced centralized config loading and rewired runtime/exchange/backtest defaults.
Tests: pytest -q tests/test_config_layer.py tests/test_runtime_env_config.py tests/test_exchange_connectivity.py


## 2026-05-21 Patch Addendum — Runtime/env failing-test triage (stability verification only)

### Why the patch was needed
- Reported post-pull failures targeted runtime env aliasing, runtime DB path/bootstrap behavior, PAPER rejected-row persistence semantics, and adaptive-learning stats counts.

### Root cause
- No deterministic code defect reproduced on current branch.
- The previously observed `assert 941 == 60` symptom in adaptive stats is consistent with non-isolated/stale DB data contamination rather than scoring/threshold logic drift.
- Current runtime env alias + DB resolution tests pass and indicate canonical/alias precedence and absolute path logging behavior are intact.

### Files changed
- `VERSION.md`
- `REPORT.md`
- `CHANGELOG.md`

### Runtime behavior changes
- None (verification-only documentation update).

### Lifecycle/persistence/schema impact
- None.

### Tests executed
- `pytest tests/test_adaptive_learning_foundation.py::test_adaptive_stats_and_shadow_thresholds -vv --tb=long`
- `pytest tests/test_runtime.py::test_runtime_module_bootstrap_builds_from_env -vv --tb=long`
- `pytest tests/test_runtime.py::test_runtime_logs_absolute_db_path -vv --tb=long`
- `pytest tests/test_runtime.py::test_paper_runtime_rejected_rows_use_paper_mode_and_single_final_count -vv --tb=long`
- `pytest tests/test_runtime_env_config.py::test_runtime_env_aliases_for_threshold_and_positions -vv --tb=long`
- `pytest tests/test_runtime_env_config.py -q`
- `pytest tests/test_adaptive_learning_foundation.py -q`
- `pytest tests/test_runtime.py -q`
- `pytest -q`

### Risks / limitations
- The specific missing test node (`test_runtime_rejected_decisions_do_not_persist_incomplete_rows`) no longer exists under that name, implying test rename/removal drift between failure report and current branch.
- Intermittent failures can still recur if external env vars or persistent sqlite files leak across test runs in non-isolated environments.

### Push recommendation
- Safe to merge as audit/traceability documentation update; no behavioral/runtime code change included.

## Patch 2026-05-22
- Runtime/backtest path now uses deterministic historical Binance Futures replay data with explicit source labeling.
- Added cache metadata coverage validation and loud failures for incomplete historical coverage.
- Added unit tests for pagination, dedupe, incomplete coverage failures, cache coverage checks, and funding anti-leak joins.
