"""Keep the development branch eligible for exact-commit qualification."""

from pathlib import Path
import re


def test_chatgpt_push_runs_existing_qualification_workflow() -> None:
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/test.yml").read_text()
    push = re.search(r"(?ms)^  push:\n(.*?)(?=^  \w|^\S)", workflow)
    assert push is not None
    assert re.search(r"(?m)^      - CHATGPT\s*$", push.group(1))
    assert "run: pytest -q" in workflow
    assert "python -m compileall -q src" in workflow
    assert "backtest_order.py --ci --offline" in workflow
    assert "python scripts/run_safety_mutations.py" in workflow
