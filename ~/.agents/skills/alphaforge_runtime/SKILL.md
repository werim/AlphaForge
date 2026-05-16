# AlphaForge Runtime Skill

## Purpose

Safely modify AlphaForge runtime behavior while preserving:
- lifecycle integrity
- execution realism
- persistence consistency
- BACKTEST/PAPER/LIVE alignment

The runtime is execution-aware infrastructure.

Do not treat it as:
- a toy simulator
- a pure TP/SL engine
- a cosmetic backtest generator

---

# Mandatory Context

Always read first:

1. AGENTS.md
2. VERSION.md
3. REPORT.md
4. CHANGELOG.md

Then inspect:
- runtime entrypoints
- lifecycle flow
- persistence layer
- execution layer
- tests

---

# Mandatory Workflow

1. Inspect current architecture
2. Identify exact files involved
3. Explain current behavior
4. Identify risks
5. Propose minimal patch
6. Apply minimal patch only
7. Run targeted tests
8. Run broader tests if feasible
9. Update documentation
10. Summarize remaining risks

Never skip inspection.

---

# Runtime Principles

Preserve:
- lifecycle ordering
- reject visibility
- execution penalties
- persistence semantics
- runtime alignment

Avoid:
- behavioral drift
- fake execution realism
- hidden persistence changes
- silent lifecycle mutation

---

# Lifecycle Rules

Expected lifecycle:

SIGNAL_CREATED
→ SIGNAL_VALIDATED
→ SIGNAL_REJECTED | WAITING_ENTRY_ZONE
→ ENTRY_TRIGGERED
→ ORDER_PLACED
→ PARTIAL_FILL
→ FILLED
→ TP_HIT / SL_HIT / CANCELLED / OPEN_AT_END

Do not:
- collapse lifecycle into CREATED
- skip rejected persistence
- force trades automatically
- bypass validation states

Lifecycle transitions must remain auditable.

---

# Execution Rules

Execution realism is mandatory.

Always consider:
- spread
- slippage
- volatility
- liquidity
- latency
- funding
- regime conditions

Effective RR matters more than raw RR.

Detect and avoid:
- hardcoded score values
- hardcoded RR values
- fake zero execution costs
- unrealistic fills
- all-zero execution fields

If execution data is unavailable:
- mark unavailable/null explicitly
- do not silently fake values

---

# Persistence Rules

Do not silently change:
- SQLite schema semantics
- CSV export semantics
- lifecycle persistence behavior
- reject persistence behavior

Verify:
- backward compatibility
- field completeness
- export integrity
- lifecycle ordering

Always persist:
- reject_reason
- cancel_reason
- lifecycle states
- execution context if available

---

# Testing Rules

Every runtime behavior change requires tests.

Mandatory coverage:
- lifecycle ordering
- reject persistence
- export integrity
- schema consistency
- score variability
- RR variability
- runtime alignment
- BACKTEST/PAPER consistency

Prefer:
real-flow tests

Avoid:
mock-only validation

---

# Documentation Rules

After any runtime change update:

- VERSION.md
- REPORT.md
- CHANGELOG.md

REPORT.md must explain:
- why the patch was needed
- exact behavior changed
- lifecycle impact
- persistence impact
- compatibility risks
- migration concerns
- tests executed
- remaining limitations

Do not leave documentation stale.

---

# Output Format

## Files Changed

## Current Runtime Behavior

## Runtime Changes Applied

## Lifecycle Impact

## Persistence Impact

## Tests Added

## Tests Executed

## Risks

## Remaining Limitations

## Push Recommendation

---

# Hard Constraints

Do NOT:
- rewrite architecture
- create duplicate alphaforge packages
- introduce toy runtime replacements
- bypass existing runtime flow
- remove regression tests casually

Always:
- patch real files only
- preserve alignment across modes
- prefer minimal safe changes
- explain uncertainty honestly

---

# Runtime Philosophy

A technically profitable but execution-unrealistic runtime is invalid.

Capital preservation matters more than:
- trade frequency
- cosmetic performance
- theoretical RR

Weak execution invalidates otherwise good setups.
