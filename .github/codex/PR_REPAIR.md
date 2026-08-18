# AlphaForge Codex PR Repair

This repository uses a guarded GitHub Actions workflow to turn trusted PR repair instructions into code changes without relying on `@codex` mention behavior.

## Activation

The workflow file must live on the repository default branch (`main`) because it listens for `issue_comment` events.

For an open same-repository PR targeting `dev`, the repository owner can comment:

```text
/codex-repair <precise repair instruction>
```

The workflow can also be started manually through `workflow_dispatch` with a PR number and instruction.

## Required secret

Add an Actions secret named:

```text
OPENAI_API_KEY
```

The key is supplied only to the isolated `openai/codex-action` job. It is not passed to the fresh runner that applies and pushes the resulting patch.

## Safety boundary

The workflow:

- accepts automatic repair requests only from GitHub actor `werim`;
- operates only on open, non-draft, same-repository PRs targeting `dev`;
- starts from the exact PR head SHA and rejects stale output if the branch moves;
- runs Codex with `permission-profile: :workspace` and `safety-strategy: drop-sudo`;
- requires Codex to return a raw git patch rather than pushing directly;
- applies that patch on a separate clean runner;
- blocks automatic changes to `.github/**`, `AGENTS.md`, `.env*`, dependency/packaging files, and the primary production configuration/threshold files;
- blocks direct enabling of protected LIVE authority flags;
- never merges a PR automatically;
- relies on the repository's normal PR CI after the repair commit is pushed.

Human review remains mandatory for LIVE enablement, production threshold/config changes, credentials/secrets, autonomous production learning, or weakened risk/reconciliation/authorization/reject guardrails.

## Supervisor integration

The AlphaForge supervisor should post `/codex-repair ...` instead of `@codex ...` when a code change is required. A repair is considered launched only when the GitHub Actions workflow run exists; a code-review comment alone is not implementation evidence.
