#!/usr/bin/env python3
"""Metadata-only, fail-closed, event-driven verification of Codex PR delivery.

Run from a trusted default-branch GitHub Actions checkout. Never checkout or
execute PR-provided code. This verifies CI identity/gates, NOT code semantics.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MARK = "alpha-delivery-verifier:v1"
SHA = re.compile(r"^[a-f0-9]{40}$")
FINISHED = re.compile(r"\bRESULT\s*:\s*(DONE|PARTIAL|BLOCKED|DEFERRED)\b", re.I)
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
BASE = "https://api.github.com"


class GitHub:
    def __init__(self, repo: str, token: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("Invalid repository")
        self.repo, self.token = repo, token

    def call(self, method: str, path: str, body: dict | None = None):
        if not path.startswith("/") or "://" in path:
            raise ValueError("Invalid API path")
        request = Request(
            BASE + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "alphaforge-delivery-verifier-v1",
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=20) as response:
            return json.load(response)

    def get(self, path: str):
        return self.call("GET", path)

    def post(self, path: str, body: dict):
        return self.call("POST", path, body)

    def patch(self, path: str, body: dict):
        return self.call("PATCH", path, body)

    def pages(self, path: str, key: str | None = None):
        # Hard bound prevents unbounded API usage and notification storms.
        separator = "&" if "?" in path else "?"
        for page in range(1, 4):
            data = self.get(f"{path}{separator}per_page=100&page={page}")
            rows = data[key] if key else data
            for row in rows:
                yield row
            if len(rows) < 100:
                return


def eligible(pr: dict, repo: str) -> bool:
    head = pr.get("head") or {}
    head_repo = head.get("repo") or {}
    body = pr.get("body") or ""
    return (
        pr.get("state") == "open"
        and head_repo.get("full_name") == repo
        and (str(head.get("ref", "")).startswith("codex/")
             or "VERIFY: ALPHAFORGE" in body)
    )


def run_for_sha(api: GitHub, pr: dict):
    ref = pr["head"]["ref"]
    sha = pr["head"]["sha"]
    query = urlencode({"branch": ref, "per_page": 100})
    runs = api.get(f"/repos/{api.repo}/actions/workflows/test.yml/runs?{query}")
    matched = [
        run for run in runs.get("workflow_runs", [])
        if run.get("head_sha") == sha and run.get("status") == "completed"
    ]
    return max(matched, key=lambda run: (run.get("created_at", ""), run.get("run_attempt", 1)), default=None)


def code_identity(api: GitHub, pr: dict, run: dict) -> str:
    expected, actual = pr["head"]["sha"], run.get("head_sha", "")
    if not SHA.fullmatch(expected) or not SHA.fullmatch(actual):
        return "INVALID_SHA"
    if expected == actual:
        return "EXACT_HEAD_SHA"
    # GitHub pull_request builds sometimes test a synthetic merge commit.
    # Tree equality proves code equality only; commit identities stay distinct.
    if run.get("event") != "pull_request":
        return "SHA_MISMATCH"
    try:
        one = api.get(f"/repos/{api.repo}/git/commits/{expected}")
        two = api.get(f"/repos/{api.repo}/git/commits/{actual}")
        if one["tree"]["sha"] == two["tree"]["sha"]:
            return "TREE_EQUIVALENT_MERGE"
    except (KeyError, OSError, ValueError):
        return "UNVERIFIABLE_MERGE"
    return "DIFFERENT_TREE"


def required_steps(event: str, base_ref: str = "main") -> set[str]:
    required = {
        "Protected safety mutation gate",
        "Run offline backtest",
        "Verify backtest outputs",
    }
    if event == "pull_request":
        required.update({
            "Cross-surface invariant gate",
            "System audit fabric gate",
        })
        if base_ref != "main":
            required.update({"System verification routing gate", "Routed PR verification"})
    else:
        required.add("Full regression suite")
    return required


def evaluate(run: dict, jobs: list[dict], identity: str, changed: list[dict], base_ref: str = "main") -> dict:
    test_jobs = [job for job in jobs if job.get("name") == "test"]
    steps = {step["name"]: step.get("conclusion", step.get("status", "UNKNOWN"))
             for job in test_jobs for step in job.get("steps", [])}
    required = required_steps(run.get("event", ""), base_ref)
    missing = sorted(name for name in required if steps.get(name) != "success")
    production_files = [
        f.get("filename", "") for f in changed
        if f.get("filename", "").endswith(".py")
        and not f.get("filename", "").startswith("tests/")
    ]
    regression_files = [
        f.get("filename", "") for f in changed
        if f.get("filename", "").startswith("tests/")
    ]
    if run.get("status") != "completed":
        status = "PENDING_CI"
    elif run.get("conclusion") not in ("success",):
        status = "CI_FAILED_OR_NOT_SUCCESSFUL"
    elif identity not in ("EXACT_HEAD_SHA", "TREE_EQUIVALENT_MERGE"):
        status = "INCOMPLETE_SOURCE_IDENTITY"
    elif len(test_jobs) != 1 or test_jobs[0].get("conclusion") != "success" or missing:
        status = "INCOMPLETE_CI_GATES"
    elif production_files and not regression_files:
        status = "REVIEW_REQUIRED_NO_CHANGED_REGRESSION"
    else:
        status = "CI_EVIDENCE_VERIFIED"
    return {
        "status": status,
        "identity": identity,
        "missing": missing,
        "steps": {name: steps.get(name, "NOT_RUN") for name in sorted(required)},
        "changed_production_files": len(production_files),
        "changed_regression_files": len(regression_files),
    }


def format_report(pr: dict, run: dict, verdict: dict) -> str:
    sha = pr["head"]["sha"]
    number = pr["number"]
    run_id = run["id"]
    lines = [
        f"<!-- {MARK} pr={number} sha={sha} -->",
        "### AlphaForge automated delivery verification",
        "",
        f"**RESULT: {verdict['status']}** — automated CI/source-metadata verification only.",
        f"- PR: #{number}; current head: {sha}",
        f"- CI: https://github.com/{pr['base']['repo']['full_name']}/actions/runs/{run_id}"
        f" ({run.get('event')}, {run.get('conclusion')}, attempt {run.get('run_attempt', 1)})",
        f"- Source binding: {verdict['identity']}.",
        "- Required steps: " + ", ".join(
            f"{name}={result}" for name, result in verdict["steps"].items()
        ),
        f"- Changed production Python files: {verdict['changed_production_files']};"
        f" regression files: {verdict['changed_regression_files']}.",
        "- Missing/non-success required steps: "
        + (", ".join(verdict["missing"]) or "none"),
        "",
        "**NOT independently verified:** implementation semantics, fakeout bounds,"
        " historical provenance, mutation kill counts, code-level financial"
        " conservation, real fills, PAPER/LIVE readiness and capital-consumer admission."
        " A successful CI is not proof of these claims. Manual semantic/source audit"
        " is required before any safety or merge decision.",
        "",
        "No branch/code/CI was changed and no merge was performed by this verifier.",
    ]
    return "\n".join(lines)


def publish(api: GitHub, pr: dict, message: str, sha: str):
    number = pr["number"]
    marker = f"<!-- {MARK} pr={number} sha={sha} -->"
    endpoint = f"/repos/{api.repo}/issues/{number}/comments"
    existing = next(
        (comment for comment in api.pages(endpoint)
         if marker in (comment.get("body") or "")
         and (comment.get("user") or {}).get("login") == "github-actions[bot]"),
        None,
    )
    if existing:
        if existing.get("body") != message:
            api.patch(f"/repos/{api.repo}/issues/comments/{existing['id']}", {"body": message})
        return
    api.post(endpoint, {"body": message})


def handle(api: GitHub, event: str, payload: dict) -> list[str]:
    candidates: list[tuple[dict, dict]] = []
    if event == "workflow_run":
        run = payload.get("workflow_run", {})
        if (
            (run.get("name") != "Tests")
            or run.get("status") != "completed"
            or (run.get("head_repository") or {}).get("full_name") != api.repo
        ):
            return []
        prs = list(api.pages(f"/repos/{api.repo}/pulls?state=open"))
        associated = {p.get("number") for p in run.get("pull_requests", [])}
        for pr in prs:
            if eligible(pr, api.repo) and (
                pr["head"]["sha"] == run.get("head_sha")
                or pr["number"] in associated
            ):
                candidates.append((pr, run))
    elif event == "issue_comment":
        issue = payload.get("issue") or {}
        comment = payload.get("comment") or {}
        body = comment.get("body") or ""
        if not (issue.get("pull_request") and "TASK: #" in body and FINISHED.search(body)):
            return []
        number = issue["number"]
        pr = api.get(f"/repos/{api.repo}/pulls/{number}")
        author = (comment.get("user") or {}).get("login")
        if comment.get("author_association") not in TRUSTED_ASSOCIATIONS and author != (pr.get("user") or {}).get("login"):
            return []
        if eligible(pr, api.repo):
            run = run_for_sha(api, pr)
            if run:
                candidates.append((pr, run))
    elif event == "workflow_dispatch":
        raw = (payload.get("inputs") or {}).get("pr_number", "")
        if not str(raw).isdigit():
            return []
        pr = api.get(f"/repos/{api.repo}/pulls/{int(raw)}")
        if eligible(pr, api.repo):
            run = run_for_sha(api, pr)
            if run:
                candidates.append((pr, run))
    else:
        return []

    reports = []
    for pr, run in candidates:
        # Do not publish stale evidence after the head has moved.
        latest = api.get(f"/repos/{api.repo}/pulls/{pr['number']}")
        if latest["head"]["sha"] != pr["head"]["sha"] or latest.get("state") != "open":
            continue
        jobs = list(api.pages(f"/repos/{api.repo}/actions/runs/{run['id']}/jobs", key="jobs"))
        changed = list(api.pages(f"/repos/{api.repo}/pulls/{pr['number']}/files"))
        verdict = evaluate(run, jobs, code_identity(api, pr, run), changed, pr["base"]["ref"])
        report = format_report(pr, run, verdict)
        publish(api, pr, report, pr["head"]["sha"])
        reports.append(f"PR #{pr['number']}: {verdict['status']}\n{report}")
    return reports


def main() -> int:
    event = os.environ["GITHUB_EVENT_NAME"]
    payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    api = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    reports = handle(api, event, payload)
    summary = "\n\n".join(reports) if reports else "No actionable Codex PR delivery / exact-SHA CI found."
    print(summary)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as out:
            out.write(summary + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
