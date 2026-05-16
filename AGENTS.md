# AlphaForge AGENTS.md

## Identity

AlphaForge is an execution-aware quantitative trading research and runtime system.

Primary objective:
Preserve long-term positive expectancy after real-world execution costs.

The system is:
- probabilistic
- execution-aware
- regime-aware
- persistence-sensitive
- lifecycle-driven
- defensive before aggressive

Never optimize for:
- trade count
- cosmetic completeness
- large rewrites
- fake backtest smoothness
- temporary PnL spikes

Always optimize for:
- execution realism
- lifecycle correctness
- runtime stability
- persistence integrity
- reject quality
- capital preservation
- behavioral consistency

---

# Repository Rules

## Architecture

Do NOT:
- rewrite the architecture unnecessarily
- create duplicate alphaforge packages
- introduce toy/minimal/mock runtime replacements
- bypass existing runtime flow
- replace production paths with simplified examples

Always:
- inspect before editing
- patch real files only
- preserve existing runtime/backtest structure
- prefer minimal safe patches
- preserve compatibility where possible

---

# Runtime Alignment

BACKTEST, PAPER, and LIVE modes should behave consistently wherever possible.

Avoid:
- divergent decision logic
- different reject logic between modes
- different lifecycle semantics
- inconsistent persistence behavior

The same signal should ideally pass through:
- the same decision engine
- the same reject engine
- the same lifecycle semantics
- the same persistence contract

BACKTEST must not become:
- a fake outcome simulator
- a hardcoded TP/SL generator
- a shortcut around runtime validation

---

# Lifecycle Integrity

Lifecycle accuracy is mandatory.

Expected lifecycle progression:

SIGNAL_CREATED
→ SIGNAL_VALIDATED
→ SIGNAL_REJECTED | WAITING_ENTRY_ZONE
→ ENTRY_TRIGGERED
→ ORDER_PLACED
→ PARTIAL_FILL
→ FILLED
→ TP_HIT / SL_HIT / CANCELLED / OPEN_AT_END

Do NOT:
- collapse lifecycle into CREATED
- hide rejected decisions
- skip lifecycle transitions silently
- force every signal into a trade

Rejected decisions are valuable data.

Persist:
- reject_reason
- cancel_reason
- lifecycle state
- timestamps
- execution context if available

---

# Execution Realism

Execution realism is more important than theoretical profitability.

Always consider:
- spread
- slippage
- volatility
- liquidity
- latency
- funding
- market regime

Effective RR matters more than raw RR.

Reject trades if execution conditions invalidate expectancy.

Avoid:
- hardcoded score values
- hardcoded RR values
- fake spread/funding placeholders
- unrealistic perfect fills

If data is unavailable:
- explicitly mark unavailable/null
- do not silently use fake defaults

---

# Reject Engine

Rejecting weak trades is alpha.

Possible reject reasons include:
- LOW_EFFECTIVE_RR
- HIGH_SPREAD
- EXCESSIVE_VOLATILITY
- THIN_LIQUIDITY
- REGIME_MISMATCH
- SPOOF_RISK
- MOMENTUM_EXHAUSTION
- EXECUTION_RISK
- LOW_CONFIDENCE
- CORRELATION_OVEREXPOSURE

Rejected signals must be:
- persisted
- exportable
- testable
- auditable

---

# Persistence Rules

Persistence integrity is critical.

Do NOT:
- silently change schema behavior
- introduce schema drift carelessly
- break CSV exports
- omit lifecycle states from persistence
- drop rejected rows silently

Always verify:
- SQLite consistency
- CSV export consistency
- lifecycle ordering
- field completeness
- backward compatibility risks

---

# Placeholder Detection

Aggressively detect and remove fake placeholder behavior.

Suspicious patterns include:
- constant score values
- constant RR values
- constant expectancy buckets
- all-zero execution fields
- identical lifecycle outcomes
- missing rejected rows

Do not mask missing logic with fake realism.

---

# Testing Rules

Every behavioral change requires tests.

Mandatory test areas:
- lifecycle ordering
- reject persistence
- export integrity
- schema consistency
- score variability
- RR variability
- runtime alignment
- BACKTEST/PAPER consistency

Avoid tests that:
- validate only mocks
- bypass real flow
- ignore persistence behavior

Regression tests are valuable.
Do not remove them casually.

---

# Documentation Rules

After any code change update:

- VERSION.md
- REPORT.md
- CHANGELOG.md

Documentation must reflect:
- runtime behavior changes
- lifecycle changes
- persistence changes
- schema changes
- compatibility risks
- migration concerns
- tests added/executed
- remaining known risks

Do not leave documentation stale.

---

# VERSION.md Requirements

VERSION.md should contain:
- current version
- current phase
- runtime maturity
- BACKTEST/PAPER/LIVE alignment
- lifecycle coverage
- execution realism coverage
- known critical risks
- last audit date
- live readiness verdict

Keep VERSION.md concise and operational.

---

# REPORT.md Requirements

REPORT.md should contain:
- why the patch was needed
- root cause
- files changed
- runtime behavior changes
- lifecycle changes
- persistence changes
- export/schema changes
- tests added
- tests executed
- risks
- remaining limitations
- migration concerns
- push recommendation

REPORT.md is the technical surgery report.

---

# CHANGELOG.md Requirements

CHANGELOG.md should contain:
- Added
- Changed
- Fixed
- Removed
- Breaking Changes
- Known Issues

Keep entries concise and human-readable.

---

# Safety Rules

Never recommend LIVE trading readiness unless:
- lifecycle integrity is verified
- reject engine is functioning
- persistence integrity is validated
- BACKTEST/PAPER behavior is aligned
- execution realism exists
- tests are passing consistently

Missing a trade is acceptable.
Forcing low-quality trades is forbidden.

Capital preservation comes before activity.

---

# Preferred Workflow

1. Audit
2. Planning
3. Minimal patch
4. Tests
5. Documentation update
6. Re-audit
7. Push decision

Prefer:
small safe improvements

over:
large unstable rewrites

---

# Output Expectations

When modifying code:
- explain exact files changed
- explain exact behavior changed
- explain risks
- explain persistence impact
- explain lifecycle impact
- explain compatibility impact

Do not hide uncertainty.
Do not pretend placeholder logic is realistic.

Execution-aware honesty is mandatory.
