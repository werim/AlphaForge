# AlphaForge Audit Skill

## Purpose

Audit AlphaForge for:
- architectural safety
- runtime integrity
- lifecycle correctness
- persistence consistency
- execution realism
- backtest credibility
- live-readiness risk

The audit must prioritize:
- expectancy preservation
- execution-aware realism
- rejection quality
- behavioral consistency

Do not optimize for:
- cosmetic completeness
- feature count
- large rewrites
- fake smooth backtests

---

# Mandatory Context

Always read first:

1. AGENTS.md
2. VERSION.md
3. REPORT.md
4. CHANGELOG.md

Then inspect:
- repository structure
- runtime entrypoints
- persistence layer
- lifecycle flow
- tests
- exports
- backtest behavior

---

# Audit Areas

## 1. Repository Integrity

Check:
- duplicate alphaforge packages
- accidental mock/minimal runtime modules
- orphan files
- stale generated files
- architectural drift
- stale documentation

Flag:
- parallel runtime implementations
- dead code paths
- inconsistent naming
- hidden persistence behavior

---

# 2. Runtime Integrity

Inspect:
- BACKTEST flow
- PAPER flow
- LIVE flow

Verify:
- shared decision logic
- shared reject logic
- lifecycle consistency
- persistence consistency

Detect:
- shortcut logic
- bypassed validation
- silent exception handling
- divergent execution paths

---

# 3. Decision Engine Integrity

Inspect:
- score calculation
- RR calculation
- expectancy calculation
- reject engine
- regime awareness
- execution penalties

Detect:
- hardcoded scores
- hardcoded RR
- fake expectancy
- placeholder execution values
- forced trade creation

---

# 4. Lifecycle Integrity

Expected lifecycle:

SIGNAL_CREATED
→ SIGNAL_VALIDATED
→ SIGNAL_REJECTED | WAITING_ENTRY_ZONE
→ ENTRY_TRIGGERED
→ ORDER_PLACED
→ PARTIAL_FILL
→ FILLED
→ TP_HIT / SL_HIT / CANCELLED / OPEN_AT_END

Flag:
- missing lifecycle states
- CREATED-only lifecycle
- hidden rejected decisions
- lifecycle ordering violations

---

# 5. Persistence Integrity

Inspect:
- SQLite schema
- CSV exports
- lifecycle persistence
- rejected persistence
- schema drift risk

Verify:
- reject_reason
- cancel_reason
- expectancy_bucket
- execution fields
- lifecycle timestamps

Detect:
- missing fields
- all-zero execution fields
- UNKNOWN expectancy saturation
- export inconsistencies

---

# 6. Execution Realism

Inspect:
- spread modeling
- slippage modeling
- latency modeling
- liquidity modeling
- funding modeling

Detect:
- fake zero-cost execution
- unrealistic fills
- missing penalties
- unrealistic RR inflation

---

# 7. Test Quality

Inspect:
- lifecycle tests
- persistence tests
- export tests
- runtime tests
- regression tests

Detect:
- tests validating mocks only
- missing reject tests
- missing lifecycle ordering tests
- flaky tests
- untested persistence paths

---

# Mandatory Output Format

## Executive Summary

## Current Project Phase Estimate

## Critical Findings

## High Risk Findings

## Medium Risk Findings

## Exact Files/Functions Involved

## Runtime Divergence Findings

## Lifecycle Findings

## Persistence Findings

## Execution Realism Findings

## Placeholder/Fake Logic Findings

## Recommended Next Task

## Minimal Safe Patch Scope

## Tests Required

## Risks

## Live Readiness Verdict

---

# Audit Philosophy

A fake-clean backtest is dangerous.

Execution realism matters more than:
- raw profit
- smooth equity curves
- high win rate

Rejected trades are valuable information.

Missing a trade is acceptable.
Forcing weak trades is forbidden.

---

# Hard Constraints

Do NOT:
- patch code
- rewrite architecture
- invent behavior
- assume runtime flow without inspection

Always:
- cite exact files/functions
- identify root causes
- prefer minimal safe recommendations
- flag placeholder logic aggressively
