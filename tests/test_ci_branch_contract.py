"""Keep PR checks fast while preserving exact-commit branch qualification."""

from pathlib import Path
import re


def _branches(workflow: str, event: str) -> str:
    match = re.search(
        rf"(?ms)^  {event}:\n(.*?)(?=^  \w|^\S)",
        workflow,
    )
    assert match is not None
    return match.group(1)


def test_ci_runs_fast_on_pr_and_full_qualification_on_final_branches() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github/workflows/test.yml"
    ).read_text()

    push = _branches(workflow, "push")
    pull_request = _branches(workflow, "pull_request")

    # A CHATGPT branch with an open PR must not duplicate the same workflow as
    # both a push and a pull_request run. Exact-commit qualification happens on
    # the protected/final branches after merge.
    assert re.search(r"(?m)^      - dev\s*$", push)
    assert re.search(r"(?m)^      - main\s*$", push)
    assert re.search(r"(?m)^      - CHATGPT\s*$", push) is None
    assert re.search(r"(?m)^      - dev\s*$", pull_request)
    assert re.search(r"(?m)^      - main\s*$", pull_request)

    # PR verification must consume the deterministic routing plan instead of
    # unconditionally replaying every historical protected gate.
    assert "System verification routing gate" in workflow
    assert "Routed PR verification" in workflow
    assert 'plan.get("selected_tests", [])' in workflow
    assert "artifacts/system-verification-plan.json" in workflow
    assert "Shared trade-quality authority gate" not in workflow
    assert "SQLite contention regression gate" not in workflow
    assert "Executable stop-risk gate" not in workflow

    # A manual exact-SHA recheck must not cancel an in-flight automatic run,
    # and it should reuse an already-successful full qualification.
    assert (
        "cancel-in-progress: ${{ github.event_name != 'workflow_dispatch' }}"
        in workflow
    )
    assert "dispatch-dedupe:" in workflow
    assert "Reuse successful exact-SHA qualification" in workflow
    assert ".head_sha ==" in workflow
    assert "should_run=false" in workflow

    # Final-branch qualification remains exhaustive and authoritative.
    assert "Full regression suite" in workflow
    assert "if: github.event_name != 'pull_request'" in workflow
    assert "run: pytest -q" in workflow
    assert "python -m compileall -q src" in workflow
    assert "backtest_order.py --ci --offline" in workflow
    assert "python scripts/run_safety_mutations.py" in workflow

    # CI backtest artifacts are diagnostic, not long-lived qualification
    # authority; keep enough history for debugging without 90-day accumulation.
    assert "retention-days: 14" in workflow
