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
from urllib.parse import urlencode, urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, build_opener
from urllib.request import Request

MARK = "alpha-delivery-verifier:v1"
SHA = re.compile(r"^[a-f0-9]{40}$")
FINISHED = re.compile(r"\bRESULT\s*:\s*(DONE|PARTIAL|BLOCKED|DEFERRED)\b", re.I)
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
BASE = "https://api.github.com"


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise ValueError("insecure GitHub log redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.remove_header("Authorization")
        return redirected


class GitHub:
    def __init__(self, repo: str, token: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("Invalid repository")
        self.repo, self.token = repo, token

    def call(self, method: str, path: str, body: dict | None = None, *, text=False):
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
        with build_opener(SafeRedirect()).open(request, timeout=20) as response:
            return response.read().decode("utf-8") if text else json.load(response)

    def get(self, path: str):
        return self.call("GET", path)

    def log(self, job_id: int):
        return self.call("GET", f"/repos/{self.repo}/actions/jobs/{int(job_id)}/logs", text=True)

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
        raise RuntimeError("GITHUB_PAGINATION_INCOMPLETE")


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
        if run.get("head_sha") == sha
    ]
    return max(matched, key=lambda run: (run.get("created_at", ""), run.get("run_attempt", 1)), default=None)


def tested_checkout(api: GitHub, jobs: list[dict]) -> str | None:
    """Read checkout's own log interval, not workflow head_sha or test output."""
    tests = [job for job in jobs if job.get("name") == "test"]
    if len(tests) != 1:
        return None
    job = tests[0]
    steps = [step for step in job.get("steps", [])
             if str(step.get("name", "")).startswith("Run actions/checkout@")
             and step.get("conclusion") == "success"]
    if len(steps) != 1 or not steps[0].get("started_at") or not steps[0].get("completed_at"):
        return None
    try:
        from datetime import datetime
        start = datetime.fromisoformat(steps[0]["started_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(steps[0]["completed_at"].replace("Z", "+00:00"))
        found, pending = [], False
        for line in api.log(job["id"]).lstrip("\ufeff").splitlines():
            timestamp, _, content = line.partition(" ")
            try:
                when = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+(?=Z$)", r"\1", timestamp).replace("Z", "+00:00"))
            except ValueError:
                pending = False
                continue
            # Jobs API truncates step bounds to seconds; logs retain fractions.
            if not start <= when.replace(microsecond=0) <= end:
                pending = False
                continue
            if pending and SHA.fullmatch(content.strip()):
                found.append(content.strip())
            pending = bool(re.fullmatch(r"\[command\]\S*git log -1 --format=%H", content.strip()))
        return found[0] if len(found) == 1 else None
    except (KeyError, OSError, ValueError):
        return None


def code_identity(api: GitHub, pr: dict, run: dict, checkout_sha: str | None = None) -> str:
    expected, metadata = pr["head"]["sha"], run.get("head_sha", "")
    if not isinstance(expected, str) or not isinstance(metadata, str) or not SHA.fullmatch(expected) or not SHA.fullmatch(metadata):
        return "INVALID_SHA"
    if metadata != expected:
        return "SHA_MISMATCH"
    if not checkout_sha or not SHA.fullmatch(checkout_sha):
        return "UNAVAILABLE_TESTED_CHECKOUT"
    if expected == checkout_sha:
        return "EXACT_HEAD_SHA"
    if run.get("event") != "pull_request":
        return "CHECKOUT_SHA_MISMATCH"
    try:
        one = api.get(f"/repos/{api.repo}/git/commits/{expected}")
        two = api.get(f"/repos/{api.repo}/git/commits/{checkout_sha}")
        if expected not in [parent.get("sha") for parent in two.get("parents", [])]:
            return "UNRELATED_CHECKOUT"
        if (SHA.fullmatch(one["tree"]["sha"])
                and one["tree"]["sha"] == two["tree"]["sha"]):
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
        f"- Source binding: {verdict['identity']}; actual tested checkout: {verdict.get('tested_checkout_sha') or 'UNAVAILABLE'}.",
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


def _publish(api: GitHub, pr: dict, message: str, sha: str):
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
            return "UPDATED"
        return "UNCHANGED"
    api.post(endpoint, {"body": message})
    return "CREATED"


def publish(api: GitHub, pr: dict, message: str, sha: str, run: dict | None = None):
    # Recheck after logs/files API work, immediately before comment delivery.
    latest = api.get(f"/repos/{api.repo}/pulls/{pr['number']}")
    if latest.get("state") != "open" or latest["head"]["sha"] != sha:
        return "STALE_HEAD_NOT_PUBLISHED"
    if run is not None:
        current = run_for_sha(api, latest)
        keys = ("id", "run_attempt", "status", "conclusion")
        if not current or any(current.get(k) != run.get(k) for k in keys):
            return "STALE_CI_NOT_PUBLISHED"
    try:
        return _publish(api, pr, message, sha)
    except RuntimeError as error:
        if str(error) != "GITHUB_PAGINATION_INCOMPLETE":
            raise
        return "COMMENT_LOOKUP_INCOMPLETE_NOT_PUBLISHED"
    except HTTPError as error:
        if error.code != 403:
            raise
        # A transport failure is not a failed CI verdict or a successful delivery.
        # Old logs omitted this response: retain bounded diagnostic evidence now.
        try:
            detail = json.loads(error.read(8192)).get("message", "")
        except (ValueError, AttributeError):
            detail = ""
        headers = error.headers or {}
        return "COMMENT_WRITE_FORBIDDEN " + json.dumps({
            "http_status": 403, "message": str(detail)[:300],
            "request_id": headers.get("X-GitHub-Request-Id"),
            "rate_limit_remaining": headers.get("X-RateLimit-Remaining"),
            "retry_after": headers.get("Retry-After"),
        }, sort_keys=True)


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
                candidates.append((pr, run_for_sha(api, pr) or run))
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
        checkout = tested_checkout(api, jobs)
        verdict = evaluate(run, jobs, code_identity(api, pr, run, checkout), changed, pr["base"]["ref"])
        verdict["tested_checkout_sha"] = checkout
        report = format_report(pr, run, verdict)
        delivery = publish(api, pr, report, pr["head"]["sha"], run)
        if delivery.startswith("STALE_"):
            verdict["status"] = "STALE_EVIDENCE_NOT_VERIFIED"
            report = format_report(pr, run, verdict)
        reports.append(f"PR #{pr['number']}: {verdict['status']}\n{report}\n\nComment delivery: {delivery}")
    return reports


def main() -> int:
    event = os.environ["GITHUB_EVENT_NAME"]
    payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    api = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    reports = handle(api, event, payload)
    summary = "\n\n".join(reports) if reports else "No actionable Codex PR delivery / exact-SHA CI found."
    receipt = dict(schema="alpha-delivery-receipts-v1", reports=reports)
    if os.getenv("DELIVERY_RECEIPTS_PATH"):
        Path(os.environ["DELIVERY_RECEIPTS_PATH"]).write_text(json.dumps(receipt, indent=2)+"\n")
    print(summary)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as out:
            out.write(summary + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
