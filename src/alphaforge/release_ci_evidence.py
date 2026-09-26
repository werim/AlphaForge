from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from alphaforge.release_gates import build_release_snapshot, persist_release_snapshot

DEFAULT_REPOSITORY = "werim/AlphaForge"
DEFAULT_API_ROOT = "https://api.github.com"
WORKFLOW_PATH = ".github/workflows/test.yml"
REQUIRED_SUCCESS_STEPS = (
    "Full regression suite",
    "Protected safety mutation gate",
    "Run offline backtest",
    "Verify backtest outputs",
)

JsonGetter = Callable[[str], Mapping[str, Any]]


def _github_json_get(url: str, *, timeout: float = 15.0) -> Mapping[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "AlphaForge-release-evidence",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed GitHub API root by default
        return json.loads(response.read().decode("utf-8"))


def _failure(reason: str, *, commit_sha: str, repository: str, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": "FAIL",
        "source": "GITHUB_ACTIONS_PUSH",
        "repository": repository,
        "head_sha": commit_sha,
        "event": None,
        "workflow_path": WORKFLOW_PATH,
        "run_id": None,
        "run_number": None,
        "full_regression_suite": None,
        "required_steps": {},
        "blocking_reasons": [reason],
        "details": dict(details or {}),
    }


def verify_github_actions_full_tests(
    *,
    commit_sha: str,
    repository: str = DEFAULT_REPOSITORY,
    run_id: int | None = None,
    api_root: str = DEFAULT_API_ROOT,
    json_get: JsonGetter | None = None,
) -> dict[str, Any]:
    """Verify an exact-commit GitHub push run whose full regression suite actually ran."""
    getter = json_get or _github_json_get
    expected_sha = str(commit_sha or "").strip()
    if not expected_sha or expected_sha.upper() == "UNKNOWN":
        return _failure("EXPECTED_COMMIT_MISSING", commit_sha=expected_sha, repository=repository)

    root = api_root.rstrip("/")
    try:
        if run_id is not None:
            run = dict(getter(f"{root}/repos/{repository}/actions/runs/{int(run_id)}"))
            candidates = [run]
        else:
            query = urlencode({"head_sha": expected_sha, "event": "push", "per_page": 20})
            payload = getter(f"{root}/repos/{repository}/actions/runs?{query}")
            candidates = [dict(item) for item in list(payload.get("workflow_runs") or [])]
    except Exception as exc:
        return _failure(
            "GITHUB_ACTIONS_QUERY_FAILED",
            commit_sha=expected_sha,
            repository=repository,
            details={"error_type": exc.__class__.__name__},
        )

    eligible = [
        run for run in candidates
        if str(run.get("head_sha") or "") == expected_sha
        and str(run.get("event") or "").lower() == "push"
        and str(run.get("path") or "") == WORKFLOW_PATH
        and str((run.get("repository") or {}).get("full_name") or repository) == repository
    ]
    if not eligible:
        return _failure("MATCHING_PUSH_WORKFLOW_NOT_FOUND", commit_sha=expected_sha, repository=repository)

    eligible.sort(key=lambda row: int(row.get("run_attempt") or 0), reverse=True)
    run = eligible[0]
    run_status = str(run.get("status") or "").lower()
    conclusion = str(run.get("conclusion") or "").lower()
    if run_status != "completed":
        return _failure(
            "MATCHING_PUSH_WORKFLOW_NOT_COMPLETED",
            commit_sha=expected_sha,
            repository=repository,
            details={"run_id": run.get("id"), "status": run_status},
        )
    if conclusion != "success":
        return _failure(
            "MATCHING_PUSH_WORKFLOW_NOT_SUCCESSFUL",
            commit_sha=expected_sha,
            repository=repository,
            details={"run_id": run.get("id"), "conclusion": conclusion},
        )

    selected_run_id = int(run.get("id"))
    try:
        jobs_payload = getter(f"{root}/repos/{repository}/actions/runs/{selected_run_id}/jobs?per_page=100")
    except Exception as exc:
        return _failure(
            "GITHUB_ACTIONS_JOBS_QUERY_FAILED",
            commit_sha=expected_sha,
            repository=repository,
            details={"run_id": selected_run_id, "error_type": exc.__class__.__name__},
        )
    jobs = [dict(job) for job in list(jobs_payload.get("jobs") or [])]
    job = next((row for row in jobs if str(row.get("name") or "") == "test"), None)
    if job is None:
        return _failure(
            "TEST_JOB_NOT_FOUND",
            commit_sha=expected_sha,
            repository=repository,
            details={"run_id": selected_run_id},
        )
    if str(job.get("status") or "").lower() != "completed" or str(job.get("conclusion") or "").lower() != "success":
        return _failure(
            "TEST_JOB_NOT_SUCCESSFUL",
            commit_sha=expected_sha,
            repository=repository,
            details={"run_id": selected_run_id, "job_conclusion": job.get("conclusion")},
        )

    steps = {str(step.get("name") or ""): str(step.get("conclusion") or "").lower() for step in list(job.get("steps") or [])}
    required = {name: steps.get(name) for name in REQUIRED_SUCCESS_STEPS}
    failed_steps = [name for name, value in required.items() if value != "success"]
    if failed_steps:
        return _failure(
            "REQUIRED_FULL_TEST_STEP_NOT_SUCCESSFUL",
            commit_sha=expected_sha,
            repository=repository,
            details={"run_id": selected_run_id, "failed_steps": failed_steps, "required_steps": required},
        )

    return {
        "status": "PASS",
        "source": "GITHUB_ACTIONS_PUSH",
        "repository": repository,
        "head_sha": expected_sha,
        "head_branch": str(run.get("head_branch") or ""),
        "event": "push",
        "workflow_path": WORKFLOW_PATH,
        "run_id": selected_run_id,
        "run_number": run.get("run_number"),
        "run_attempt": run.get("run_attempt"),
        "workflow_conclusion": conclusion,
        "full_regression_suite": required["Full regression suite"],
        "required_steps": required,
        "blocking_reasons": [],
    }


def persist_campaign_full_test_evidence(
    engine: Engine,
    *,
    campaign_id: str,
    phase: str = "PHASE6",
    repository: str = DEFAULT_REPOSITORY,
    run_id: int | None = None,
    api_root: str = DEFAULT_API_ROOT,
    json_get: JsonGetter | None = None,
) -> dict[str, Any]:
    """Verify current campaign commit CI and persist it into the release-gate snapshot."""
    with engine.connect() as conn:
        campaign = conn.execute(text("""
            SELECT campaign_id, release_id, git_commit
            FROM burnin_campaigns
            WHERE campaign_id = :campaign_id
        """), {"campaign_id": campaign_id}).mappings().first()
    if campaign is None:
        raise ValueError("CAMPAIGN_NOT_FOUND")

    release_id = str(campaign["release_id"])
    git_commit = str(campaign["git_commit"] or "")
    evidence = verify_github_actions_full_tests(
        commit_sha=git_commit,
        repository=repository,
        run_id=run_id,
        api_root=api_root,
        json_get=json_get,
    )
    snapshot = build_release_snapshot(engine, release_id=release_id, phase=phase)
    snapshot.evidence["full_tests"] = evidence
    persist_release_snapshot(engine, snapshot)
    return {
        "campaign_id": campaign_id,
        "release_id": release_id,
        "phase": phase,
        "git_commit": git_commit,
        "full_tests": evidence,
        "release_gate_status": snapshot.status,
        "release_gate_blockers": snapshot.blocking_reasons,
    }


def _database_url(path: str) -> str:
    return "sqlite+pysqlite:///" + str(Path(path).expanduser().resolve())


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify and persist commit-scoped GitHub full-test evidence")
    parser.add_argument("--db", required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--phase", default="PHASE6")
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--run-id", type=int)
    args = parser.parse_args()
    engine = create_engine(_database_url(args.db), future=True)
    try:
        result = persist_campaign_full_test_evidence(
            engine,
            campaign_id=args.campaign_id,
            phase=args.phase,
            repository=args.repository,
            run_id=args.run_id,
        )
    finally:
        engine.dispose()
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    raise SystemExit(0 if result["full_tests"]["status"] == "PASS" else 2)


if __name__ == "__main__":
    main()
