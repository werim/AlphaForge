---
name: alphaforge-runtime-safety-audit
description: "Use when auditing AlphaForge PAPER runtime, campaign health, merge/release safety, campaign continuity, persistence integrity, runtime blocker investigations, or deciding whether to start, continue, pause, or stop a runtime. Evidence-driven and conservative; green CI alone is never sufficient proof of runtime safety."
---

# AlphaForge Runtime Safety Audit

## Purpose

Determine whether the current AlphaForge PAPER runtime, campaign, or release state is operationally safe to start, continue, pause, or stop.

This skill is intentionally conservative and evidence-driven. A green CI result is necessary but never sufficient proof that the runtime is safe.

## Core principles

- Preserve evidence before any destructive repair or state-changing action.
- Prefer read-only investigation first.
- Never invent missing runtime facts.
- Separate observed facts from inference.
- Treat capital safety, experiment validity, data integrity, and reproducibility as higher priority than uptime.
- PAPER mode does not justify ignoring correctness, persistence, or lifecycle problems.
- Missing evidence is not a reason to guess.

## Scope

Use this skill for:
- audit current runtime
- is the PAPER campaign safe to continue?
- check campaign health
- verify runtime after merge
- investigate runtime blocker
- decide whether to stop the campaign

## Standard workflow

### 1. Establish repository state

- Report current branch.
- Report HEAD commit SHA.
- Report working tree status.
- Identify relevant recent merges or PRs.
- Detect whether local code differs from origin/dev.
- Do not modify the repository unless explicitly instructed.

Read-only evidence to consider:
- git branch --show-current
- git rev-parse HEAD
- git status --short --branch
- git log --oneline --decorate -n 20
- git fetch --all --prune (if network access is available and safe)
- git status -sb
- git rev-list --left-right --count origin/dev...HEAD

### 2. Establish runtime state

- Identify the currently active or most recent PAPER campaign.
- Determine campaign ID, phase, runtime mode, start time, and current status when evidence exists.
- Inspect canonical runtime evidence, campaign summaries, logs, database state, heartbeats, execution records, and generated artifacts that are relevant.
- Never invent missing runtime facts.

Evidence to gather, when available:
- runtime config or active process metadata
- campaign table rows or summaries
- database state and heartbeat timestamps
- runtime log entries for startup, state transitions, and failures
- generated artifacts or dumps used by the runtime
- any persisted run metadata, IDs, or lock files

### 3. Check operational integrity

Review for:
- stale or missing heartbeat
- process/runtime mismatch
- campaign release mismatch
- schema or migration mismatch
- missing required database columns
- invalid or contradictory campaign state
- recovery-required state
- NO_TRADABLE_SYMBOLS_AFTER_SELECTION or equivalent selection failures
- execution evidence gaps
- PAPER/live mode confusion
- duplicate or conflicting processes
- stale PID/lock/state files
- data integrity problems
- runtime using code different from the audited HEAD
- hidden failures masked by green CI

### 4. Check release safety

Determine whether recent changes can affect:
- runtime execution
- order generation
- symbol selection
- portfolio/accounting state
- persistence
- campaign continuation
- recovery behavior
- risk controls
- migrations or database compatibility

Review whether changes could create operational divergence between BACKTEST, PAPER, and LIVE behavior.

### 5. Apply the root-cause standard

Do not accept a patch merely because symptoms disappeared.

Determine whether available evidence supports correction of the actual root cause.

Explicitly distinguish:
- confirmed root cause
- likely cause
- symptom
- unresolved uncertainty

### 6. Decide on verdict

Return exactly one operational verdict:
- SAFE_TO_CONTINUE
- SAFE_TO_START
- PAUSE_AND_INVESTIGATE
- STOP_RUNTIME
- INSUFFICIENT_EVIDENCE

Use STOP_RUNTIME when continuing may corrupt state, invalidate experiment results, create uncontrolled execution behavior, or materially compromise capital-safety assumptions.

Use INSUFFICIENT_EVIDENCE rather than guessing.

### 7. Require evidence for material conclusions

Every material conclusion must state the evidence used:
- file/path
- command result
- database/query result
- log/artifact
- commit or PR
when available.

Clearly separate observed facts from inference.

## Decision rules

- If a campaign is operating with stale heartbeats, contradictory state, or missing execution evidence, do not assume the system is healthy.
- If runtime state cannot be reconciled with the audited code or DB schema, fail conservatively.
- If evidence suggests a real risk to experiment validity, data integrity, or capital safety, do not continue just to gather more data.
- If destructive recovery or state repair is required, identify it explicitly and wait for instruction before taking action.

## Required output format

Use this exact structure:

Runtime Safety Audit

Repository:
- Branch:
- HEAD:
- Origin sync:
- Working tree:

Runtime:
- Mode:
- Campaign:
- Status:
- Evidence freshness:

Findings:
- BLOCKER:
- HIGH:
- MEDIUM:
- LOW:

Root cause assessment:
- Confirmed:
- Suspected:
- Unresolved:

Operational verdict:
<one verdict only>

Reason:
<concise explanation>

Required actions before continuation:
<numbered actions, only when needed>

Evidence:
<concise evidence list>

## Safety rules

- PAPER mode does not justify ignoring correctness problems.
- Never recommend continuing merely to collect more data when existing evidence suggests corrupted or invalid runtime state.
- Preserve evidence before destructive repair.
- Prefer read-only investigation first.
- Never silently reset, delete, migrate, rewrite, restart, or terminate runtime state.
- If a destructive or state-changing action is required, identify it explicitly and wait for instruction.
- Treat capital safety, experiment validity, data integrity, and reproducibility as higher priority than uptime.

## AlphaForge-specific constraints

- Runtime behavior must be execution-aware and regime-aware.
- Lifecycle correctness is mandatory.
- Rejected decisions are valuable data and must be persisted.
- Do not treat higher activity as a sign of improvement.
- Backtest must not become a fake outcome simulator.
- Persistence integrity is critical.
- Placeholder and fake realism are unacceptable.

## Evidence-first review checklist

Before declaring any state safe:

- Confirm repository state and branch alignment.
- Confirm active or recent campaign details.
- Confirm runtime/log/DB evidence matches the audited HEAD and deployment state.
- Check the reject, lifecycle, and persistence paths for gaps.
- Check for code drift, stale state, or hidden runtime mismatches.
- Check whether the system can continue without invalidating data or capital assumptions.
- Document unresolved uncertainty instead of guessing.

## Example prompts

- audit current runtime
- is the PAPER campaign safe to continue?
- check campaign health
- verify runtime after merge
- investigate runtime blocker
- decide whether to stop the campaign

## Good practice

Keep conclusions grounded in observable evidence and conservative safety thresholds. This skill is designed to stop unsafe continuation, not to optimize uptime.
