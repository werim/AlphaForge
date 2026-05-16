# AlphaForge Version Status

## Current Version
- **Version:** `0.3.5-dev`
- **Date:** `2026-05-16`
- **Basis:** Consolidated from current README and REPORT documentation.

## Phase Estimate
- **Estimated Phase:** **Phase 3.5**
- **Maturity Summary:**
  - Phase 1 (SQL-first foundation): Mostly implemented.
  - Phase 2 (Decision/reject engine): Partially implemented.
  - Phase 3 (Symbol selection): Implemented prototype.
  - Phase 4 (Paper runtime): Implemented prototype.
  - Phase 5 (Lifecycle-accurate backtest): Incomplete, with recent SQL-backed lifecycle/export hardening.
  - Phase 6+ (analytics hardening/live readiness/adaptive learning): Partial to early groundwork.

## BACKTEST / PAPER / LIVE Alignment
- **BACKTEST:** Uses runtime-style decision/lifecycle flow, now with SQL-backed lifecycle export path and improved effective RR persistence semantics.
- **PAPER:** Runtime path exists and contract parity checks have been expanded against backtest outputs.
- **LIVE:** Code path exists but is explicitly **not production-ready**.
- **Alignment Verdict:** **Partial alignment** between BACKTEST and PAPER; LIVE remains structurally present but operationally immature.

## Lifecycle Coverage
- Lifecycle persistence and export are now documented as SQL-backed for backtest lifecycle CSV generation.
- Rejected lifecycle rows are documented as persisted/exported in the current patch set.
- Open-at-end remains represented by timeout/open outputs rather than introducing synthetic lifecycle state inflation.
- **Coverage Verdict:** **Improved but not fully complete end-to-end across all optional/edge-field nuances**.

## Execution Realism Coverage
- Decision/reject engine includes execution-aware context usage and effective RR semantics.
- Effective RR persistence bug was fixed in lifecycle persistence path to avoid raw RR misuse when effective RR is available.
- Unavailable execution context semantics remain explicit (null/sentinel) rather than synthetic defaults.
- **Coverage Verdict:** **Meaningful execution realism present, but still prototype-level and incomplete for live-grade rigor**.

## Persistence Status
- SQLAlchemy/Alembic persistence foundation exists.
- Backtest lifecycle CSV generation is documented as reading persisted SQL lifecycle rows.
- `execution_ctx_missing` persistence semantics were normalized toward canonical integer-style behavior with legacy tolerance notes.
- **Status:** **Operational with migration/backward-compatibility caveats for legacy DB representations**.

## Known Critical Risks
1. **Live readiness risk:** LIVE execution path is not production-safe (controls, operational safeguards, reconciliation maturity not yet at live standard).
2. **Parity risk:** Full contract/lifecycle parity across all optional fields and timestamp typing nuances is still incomplete.
3. **Migration risk:** Existing SQLite databases with legacy `execution_ctx_missing` text representations may require migration/rebuild strategies.
4. **Data-source dependency risk:** Backtest universe/top-N behavior can still rely on live endpoint availability unless fixture mode is used.

## Last Audit Date
- **2026-05-16** (documentation audit based on repository README + REPORT state).

## Live-Readiness Verdict
- **Verdict:** ❌ **NOT LIVE-READY**.
- **Reason:** Lifecycle/persistence hardening has improved, but parity completeness, migration maturity, and operational safeguards remain below live deployment requirements.
