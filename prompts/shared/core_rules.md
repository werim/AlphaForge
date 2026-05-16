# AlphaForge Core Rules

You are working on AlphaForge.

Primary objective:
Preserve long-term positive expectancy after execution costs.

Never optimize for:
- trade count
- cosmetic completeness
- unnecessary rewrite
- mock simplicity

Always optimize for:
- lifecycle correctness
- execution realism
- persistence integrity
- runtime stability
- architectural safety

Hard constraints:
- no duplicate alphaforge package
- no toy runtime replacement
- preserve BACKTEST/PAPER/LIVE alignment
- preserve existing architecture
- patch real files only
- update VERSION.md REPORT.md CHANGELOG.md
- add tests for behavioral changes

Reject:
- placeholder logic
- hardcoded scoring
- fake execution fields
- silent exception handling
