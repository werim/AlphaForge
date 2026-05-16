# AlphaForge Version Review Skill

## Purpose

Compare multiple Codex-generated versions for the same AlphaForge task and choose the safest push candidate.

Optimize for:
- architecture safety
- runtime stability
- lifecycle correctness
- persistence integrity
- execution realism
- tests
- documentation accuracy

Never optimize for:
- largest diff
- most features
- most ambitious rewrite
- fake completeness

## Required Inputs

Review:
- original Codex task prompt
- V1 diff/summary
- V2 diff/summary
- V3 diff/summary
- test results
- AGENTS.md
- VERSION.md
- REPORT.md
- CHANGELOG.md
- README.md

## Review Dimensions

Score each version 0-10 on:
- Architecture safety
- Runtime safety
- Lifecycle integrity
- Persistence safety
- Quant realism
- Test coverage
- Documentation quality
- Push safety

## Automatic Rejection Criteria

Reject any version that:
- creates duplicate alphaforge packages
- rewrites architecture unnecessarily
- bypasses runtime flow
- hides rejected decisions
- adds fake placeholder execution values
- changes schema without documentation
- lacks tests for behavior changes
- leaves VERSION.md / REPORT.md / CHANGELOG.md stale
- overstates live readiness

## Required Output

# Version Review Verdict

## Ranking
1. Best:
2. Second:
3. Reject:

## Comparison Table

## Per-Version Findings

## Best Version to Push

## Required Fixes Before Push

## Final Decision

Final decision must be one of:
- PUSH
- PUSH AFTER SMALL FIXES
- DO NOT PUSH
- RE-RUN CODEX WITH NARROWER PROMPT

## Philosophy

Rejecting unsafe versions is alpha.

The safest patch is not the biggest patch.
A small correct patch beats a large unstable rewrite.
