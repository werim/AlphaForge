from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

GitRunner = Callable[[list[str], Path], str]


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.check_output(
        ["git", *args],
        cwd=cwd,
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=5,
    ).strip()


def checkout_identity(
    repo_path: str | Path = ".",
    *,
    git_runner: GitRunner | None = None,
) -> dict[str, Any]:
    root = Path(repo_path).expanduser().resolve()
    runner = git_runner or _git
    try:
        commit = str(runner(["rev-parse", "HEAD"], root) or "").strip()
        status = str(runner(["status", "--porcelain=v1"], root) or "")
    except Exception as exc:
        return {
            "status": "UNAVAILABLE",
            "git_commit": None,
            "clean": False,
            "repo_path": str(root),
            "reason": f"GIT_IDENTITY_UNAVAILABLE:{exc.__class__.__name__}",
        }
    if not commit:
        return {
            "status": "UNAVAILABLE",
            "git_commit": None,
            "clean": False,
            "repo_path": str(root),
            "reason": "GIT_COMMIT_MISSING",
        }
    clean = not bool(status.strip())
    return {
        "status": "PASS" if clean else "DIRTY",
        "git_commit": commit,
        "clean": clean,
        "repo_path": str(root),
        "reason": None if clean else "WORKTREE_DIRTY",
    }


def require_campaign_checkout(
    campaign_git_commit: str,
    *,
    repo_path: str | Path = ".",
    git_runner: GitRunner | None = None,
) -> dict[str, Any]:
    expected = str(campaign_git_commit or "").strip()
    if not expected or expected.upper() in {"UNKNOWN", "UNKNOWN_GIT_COMMIT"}:
        raise ValueError("CAMPAIGN_GIT_COMMIT_MISSING")
    identity = checkout_identity(repo_path, git_runner=git_runner)
    if identity["status"] == "UNAVAILABLE":
        raise ValueError(str(identity["reason"]))
    if not identity["clean"]:
        raise ValueError("WORKTREE_DIRTY")
    if str(identity["git_commit"]) != expected:
        raise ValueError(
            f"CAMPAIGN_CHECKOUT_COMMIT_MISMATCH:{identity['git_commit']}!={expected}"
        )
    return identity
