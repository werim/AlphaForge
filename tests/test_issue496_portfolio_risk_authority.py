from __future__ import annotations

import inspect
from pathlib import Path

import alphaforge.live_readiness as live_readiness
import alphaforge.order as order
import alphaforge.portfolio_risk as canonical
import alphaforge.portfolio_risk_engine as experimental
import alphaforge.runtime as runtime


def test_duplicate_engine_is_explicitly_non_authoritative():
    assert experimental.AUTHORITY_STATUS == "EXPERIMENTAL_NON_AUTHORITATIVE"
    assert experimental.PRODUCTION_AUTHORITY == "alphaforge.portfolio_risk"
    assert callable(canonical.evaluate_portfolio_risk)


def test_production_package_does_not_import_experimental_portfolio_engine():
    root = Path(runtime.__file__).resolve().parent
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "portfolio_risk_engine.py":
            continue
        source = path.read_text(encoding="utf-8")
        if "alphaforge.portfolio_risk_engine" in source:
            offenders.append(str(path.relative_to(root)))
    assert offenders == []


def test_runtime_and_shared_order_path_import_canonical_portfolio_authority():
    runtime_source = inspect.getsource(runtime)
    order_source = inspect.getsource(order)
    assert "from alphaforge.portfolio_risk import" in runtime_source
    assert "from alphaforge.portfolio_risk import" in order_source
    assert "portfolio_risk_engine" not in runtime_source
    assert "portfolio_risk_engine" not in order_source


def test_readiness_names_evidence_not_a_nonexistent_shared_engine():
    source = inspect.getsource(live_readiness.LiveReadinessEvaluator)
    assert "backtest_and_paper_have_canonical_portfolio_risk_evidence" in source
    assert "backtest_and_paper_share_portfolio_risk_engine" not in source
    assert "authority=alphaforge.portfolio_risk" in source
