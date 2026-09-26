from __future__ import annotations

from sqlalchemy import create_engine, text

from alphaforge.release_ci_evidence import (
    REQUIRED_SUCCESS_STEPS,
    persist_campaign_full_test_evidence,
    verify_github_actions_full_tests,
)
from alphaforge.release_gates import ensure_release_gate_schema, build_release_snapshot, persist_release_snapshot


def _successful_run(commit_sha: str, *, event: str = "push", conclusion: str = "success") -> dict:
    return {
        "id": 12345,
        "name": "Tests",
        "head_branch": "dev",
        "head_sha": commit_sha,
        "path": ".github/workflows/test.yml",
        "run_number": 1883,
        "run_attempt": 1,
        "event": event,
        "status": "completed",
        "conclusion": conclusion,
        "repository": {"full_name": "werim/AlphaForge"},
    }


def _successful_job() -> dict:
    steps = [{"name": name, "conclusion": "success"} for name in REQUIRED_SUCCESS_STEPS]
    return {"name": "test", "status": "completed", "conclusion": "success", "steps": steps}


def test_full_test_verifier_requires_completed_push_and_real_full_regression_steps() -> None:
    commit = "a" * 40

    def getter(url: str):
        if "/actions/runs?" in url:
            return {"workflow_runs": [_successful_run(commit)]}
        if "/jobs?" in url:
            return {"jobs": [_successful_job()]}
        raise AssertionError(url)

    evidence = verify_github_actions_full_tests(commit_sha=commit, json_get=getter)
    assert evidence["status"] == "PASS"
    assert evidence["source"] == "GITHUB_ACTIONS_PUSH"
    assert evidence["event"] == "push"
    assert evidence["head_sha"] == commit
    assert evidence["full_regression_suite"] == "success"
    assert all(value == "success" for value in evidence["required_steps"].values())


def test_full_test_verifier_rejects_pr_run_where_full_regression_is_skipped() -> None:
    commit = "b" * 40

    def getter(url: str):
        if "/actions/runs?" in url:
            return {"workflow_runs": [_successful_run(commit, event="pull_request")]}
        raise AssertionError(url)

    evidence = verify_github_actions_full_tests(commit_sha=commit, json_get=getter)
    assert evidence["status"] == "FAIL"
    assert evidence["blocking_reasons"] == ["MATCHING_PUSH_WORKFLOW_NOT_FOUND"]


def test_full_test_verifier_rejects_skipped_full_regression_step() -> None:
    commit = "c" * 40

    def getter(url: str):
        if "/actions/runs?" in url:
            return {"workflow_runs": [_successful_run(commit)]}
        if "/jobs?" in url:
            job = _successful_job()
            for step in job["steps"]:
                if step["name"] == "Full regression suite":
                    step["conclusion"] = "skipped"
            return {"jobs": [job]}
        raise AssertionError(url)

    evidence = verify_github_actions_full_tests(commit_sha=commit, json_get=getter)
    assert evidence["status"] == "FAIL"
    assert evidence["blocking_reasons"] == ["REQUIRED_FULL_TEST_STEP_NOT_SUCCESSFUL"]
    assert "Full regression suite" in evidence["details"]["failed_steps"]


def test_campaign_full_test_evidence_is_commit_scoped_and_survives_later_snapshot(tmp_path) -> None:
    db = tmp_path / "full-test.db"
    engine = create_engine(f"sqlite+pysqlite:///{db}", future=True)
    ensure_release_gate_schema(engine)
    commit = "d" * 40
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE burnin_campaigns(
                campaign_id TEXT PRIMARY KEY,
                release_id TEXT NOT NULL,
                git_commit TEXT NOT NULL
            )
        """))
        conn.execute(
            text("INSERT INTO burnin_campaigns(campaign_id,release_id,git_commit) VALUES ('camp','rel',:sha)"),
            {"sha": commit},
        )

    def getter(url: str):
        if "/actions/runs?" in url:
            return {"workflow_runs": [_successful_run(commit)]}
        if "/jobs?" in url:
            return {"jobs": [_successful_job()]}
        raise AssertionError(url)

    result = persist_campaign_full_test_evidence(engine, campaign_id="camp", json_get=getter)
    assert result["full_tests"]["status"] == "PASS"
    assert result["full_tests"]["head_sha"] == commit

    later = build_release_snapshot(engine, release_id="rel", phase="PHASE6")
    assert later.evidence["full_tests"]["status"] == "PASS"
    assert later.evidence["full_tests"]["head_sha"] == commit
    persist_release_snapshot(engine, later)
