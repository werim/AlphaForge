"""Focused regression coverage for trusted GitHub delivery verifier."""
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verifier", HERE / "scripts/verify_codex_delivery.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def _job(*, mutation="success", full="success"):
    return [{
        "name": "test", "conclusion": "success",
        "steps": [
            {"name": "Full regression suite", "conclusion": full},
            {"name": "Protected safety mutation gate", "conclusion": mutation},
            {"name": "Run offline backtest", "conclusion": "success"},
            {"name": "Verify backtest outputs", "conclusion": "success"},
            {"name": "Cross-surface invariant gate", "conclusion": "skipped"},
        ],
    }]


def _run(*, sha="a" * 40, conclusion="success", event="workflow_dispatch"):
    return {
        "id": 123, "event": event, "head_sha": sha,
        "status": "completed", "conclusion": conclusion, "run_attempt": 1,
    }


def test_exact_sha_success_is_ci_only_not_semantic_proof():
    verdict = verifier.evaluate(
        _run(), _job(), "EXACT_HEAD_SHA",
        [{"filename": "src/alphaforge/feature.py"},
         {"filename": "tests/test_feature.py"}],
    )
    assert verdict["status"] == "CI_EVIDENCE_VERIFIED"
    report = verifier.format_report(
        {"number": 12, "head": {"sha": "a" * 40},
         "base": {"repo": {"full_name": "werim/AlphaForge"}}},
        _run(), verdict,
    )
    assert "NOT independently verified" in report
    assert "No branch/code/CI was changed" in report


def test_missing_or_skipped_protected_mutation_is_not_pass():
    for state in ("skipped", "failure", "cancelled", None):
        verdict = verifier.evaluate(
            _run(), _job(mutation=state), "EXACT_HEAD_SHA",
            [{"filename": "scripts/example.py"}, {"filename": "tests/test_example.py"}],
        )
        assert verdict["status"] == "INCOMPLETE_CI_GATES"
        assert "Protected safety mutation gate" in verdict["missing"]


def test_stale_or_synthetic_sha_cannot_be_exaggerated():
    result = verifier.evaluate(
        _run(), _job(), "DIFFERENT_TREE", [{"filename": "docs/note.md"}]
    )
    assert result["status"] == "INCOMPLETE_SOURCE_IDENTITY"


def test_green_run_without_regression_change_needs_review():
    verdict = verifier.evaluate(
        _run(), _job(), "EXACT_HEAD_SHA",
        [{"filename": "src/alphaforge/feature.py"}],
    )
    assert verdict["status"] == "REVIEW_REQUIRED_NO_CHANGED_REGRESSION"


def test_failed_ci_is_not_pass():
    verdict = verifier.evaluate(
        _run(conclusion="failure"), _job(), "EXACT_HEAD_SHA", []
    )
    assert verdict["status"] == "CI_FAILED_OR_NOT_SUCCESSFUL"


def test_pr_ci_requires_protected_gates_but_uses_actual_base_contract():
    jobs = _job()
    jobs[0]["steps"].extend([
        {"name": "System verification routing gate", "conclusion": "success"},
        {"name": "Routed PR verification", "conclusion": "success"},
        {"name": "System audit fabric gate", "conclusion": "success"},
    ])
    jobs[0]["steps"][4]["conclusion"] = "success"
    outcome = verifier.evaluate(
        _run(event="pull_request"), jobs, "EXACT_HEAD_SHA",
        [{"filename": "docs/note.md"}],
    )
    assert outcome["status"] == "CI_EVIDENCE_VERIFIED"


def test_unauthorized_comment_does_not_trigger_audit():
    class Fake:
        repo = "werim/AlphaForge"
        def get(self, path):
            assert path.endswith("/pulls/12")
            return {"state": "open", "user": {"login": "owner"},
                    "head": {"repo": {"full_name": self.repo},
                             "ref": "codex/test", "sha": "a" * 40}}
    payload = {
        "issue": {"number": 12, "pull_request": {"url": "ignored"}},
        "comment": {"body": "TASK: #1131H.1 RESULT: DONE",
                    "user": {"login": "untrusted"}, "author_association": "NONE"},
    }
    assert verifier.handle(Fake(), "issue_comment", payload) == []


def test_non_final_comment_and_no_pr_payload_do_nothing():
    class Never:
        def get(self, *args, **kwargs):
            raise AssertionError("No API call should be made")
    payload = {"issue": {"number": 12}, "comment": {"body": "TASK: #1131H.1 RESULT: DONE"}}
    assert verifier.handle(Never(), "issue_comment", payload) == []


def test_opt_in_scope_requires_same_repo_and_codex_or_marker():
    pr = {"state": "open", "head": {"ref": "codex/work",
          "repo": {"full_name": "werim/AlphaForge"}}}
    assert verifier.eligible(pr, "werim/AlphaForge")
    pr["head"]["repo"]["full_name"] = "someone/fork"
    assert not verifier.eligible(pr, "werim/AlphaForge")
